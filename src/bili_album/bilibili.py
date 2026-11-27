"""Bilibili access: resolve a link, read page metadata, parts, chapters, comments,
cover, and download the audio stream (AAC only, never re-encoded).

Page metadata comes from the public ``x/web-interface/view`` API (one call gives
description, owner, parts, cover); chapters and the media itself come from the
yt-dlp Python API. Comments come from ``x/v2/reply`` (first page, hot order,
plus the pinned ``upper.top`` comment), because yt-dlp's comment extractor walks
every page and does not mark the pinned comment.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from . import USER_AGENT
from .net import Http, HttpError

log = logging.getLogger(__name__)

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"(?:^|/|\b)av(\d+)", re.IGNORECASE)
AUDIO_FORMATS = "30280/30232/30216/bestaudio[acodec^=mp4a]"


@dataclass
class Part:
    page: int
    name: str
    duration: float
    cid: int = 0


@dataclass
class Comment:
    text: str
    author: str
    by_uploader: bool
    pinned: bool
    likes: int = 0


@dataclass
class VideoInfo:
    url: str
    bvid: str
    aid: int
    title: str
    uploader: str
    uploader_mid: int
    description: str
    duration: float
    cover_url: str
    pubdate: int
    tags: list[str] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)   # [{start_time, end_time, title}]
    comments: list[Comment] = field(default_factory=list)


class BilibiliError(RuntimeError):
    pass


def extract_bvid(url: str, http: Http | None = None) -> str:
    m = BV_RE.search(url)
    if m:
        return m.group(1)
    if "b23.tv" in url and http is not None:
        r = http.session.get(url, allow_redirects=True, timeout=20)
        m = BV_RE.search(r.url)
        if m:
            return m.group(1)
    m = AV_RE.search(url)
    if m and http is not None:
        j = http.get_json("https://api.bilibili.com/x/web-interface/view", params={"aid": m.group(1)})
        if j.get("code") == 0:
            return j["data"]["bvid"]
    raise BilibiliError(f"cannot find a BV id in {url!r}")


class Bilibili:
    def __init__(self, http: Http, cookies: Path | None = None):
        self.http = http
        self.cookies = cookies
        self._warmed = False
        if cookies:
            jar = MozillaCookieJar(str(cookies))
            jar.load(ignore_discard=True, ignore_expires=True)
            for c in jar:
                self.http.session.cookies.set_cookie(c)

    def _warm(self) -> None:
        """Get a ``buvid3`` cookie; raw API calls without it answer HTTP 412."""
        if self._warmed:
            return
        try:
            self.http.session.get("https://www.bilibili.com/", timeout=20)
        except Exception as e:  # pragma: no cover - network
            log.warning("could not warm Bilibili cookies: %s", e)
        self._warmed = True

    def _api(self, url: str, params: dict) -> dict:
        def ok(j):
            return isinstance(j, dict) and j.get("code") == 0
        try:
            j = self.http.get_json(url, params=params, validate=ok)
        except HttpError:
            self._warm()
            j = self.http.get_json(url, params=params, validate=ok)
        if not ok(j):
            self._warm()
            j = self.http.get_json(url, params=params, validate=ok, cache=False)
        if not ok(j):
            raise BilibiliError(f"{url}: code {j.get('code')} {j.get('message')}")
        return j["data"]

    def info(self, url: str, *, with_comments: bool = True) -> VideoInfo:
        self._warm()
        bvid = extract_bvid(url, self.http)
        d = self._api("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
        parts = [Part(p["page"], p.get("part", ""), float(p.get("duration", 0)), p.get("cid", 0))
                 for p in d.get("pages", [])]
        vi = VideoInfo(
            url=f"https://www.bilibili.com/video/{bvid}",
            bvid=bvid, aid=int(d["aid"]), title=d.get("title", ""),
            uploader=(d.get("owner") or {}).get("name", ""),
            uploader_mid=int((d.get("owner") or {}).get("mid", 0)),
            description=d.get("desc", "") or "",
            duration=float(d.get("duration", 0)),
            cover_url=d.get("pic", ""), pubdate=int(d.get("pubdate", 0)),
            parts=parts,
        )
        try:
            tags = self._api("https://api.bilibili.com/x/tag/archive/tags", {"bvid": bvid})
            vi.tags = [t.get("tag_name", "") for t in tags or []]
        except Exception:
            pass
        if len(parts) == 1:
            vi.chapters = self._chapters(bvid, parts[0].cid)
        if with_comments:
            vi.comments = self._comments(vi)
        return vi

    def _chapters(self, bvid: str, cid: int) -> list[dict]:
        try:
            d = self._api("https://api.bilibili.com/x/player/wbi/v2", {"bvid": bvid, "cid": cid})
        except Exception:
            try:
                d = self._api("https://api.bilibili.com/x/player/v2", {"bvid": bvid, "cid": cid})
            except Exception as e:
                log.info("no chapter data: %s", e)
                return []
        out = []
        for vp in d.get("view_points") or []:
            out.append({"start_time": float(vp.get("from", 0)), "end_time": float(vp.get("to", 0)),
                        "title": vp.get("content", "")})
        return out

    def _comments(self, vi: VideoInfo) -> list[Comment]:
        try:
            d = self._api("https://api.bilibili.com/x/v2/reply",
                          {"type": 1, "oid": vi.aid, "sort": 1, "ps": 20, "pn": 1})
        except Exception as e:
            log.warning("comments unavailable: %s", e)
            return []
        out: list[Comment] = []
        seen = set()

        def add(r, pinned=False):
            if not r or r.get("rpid") in seen:
                return
            seen.add(r.get("rpid"))
            out.append(Comment(
                text=(r.get("content") or {}).get("message", ""),
                author=(r.get("member") or {}).get("uname", ""),
                by_uploader=int(r.get("mid", 0)) == vi.uploader_mid,
                pinned=pinned, likes=int(r.get("like", 0))))

        add((d.get("upper") or {}).get("top"), pinned=True)
        for r in d.get("top_replies") or []:
            add(r, pinned=True)
        for r in d.get("replies") or []:
            add(r)
        return out

    # -- media -----------------------------------------------------------------

    def download_audio(self, vi: VideoInfo, workdir: Path, *, page: int | None = None) -> Path:
        """Download one audio stream and remux it to a clean audio-only .m4a."""
        import yt_dlp

        stem = "source" if page is None else f"part{page:03d}"
        final = workdir / f"{stem}.m4a"
        if final.exists() and final.stat().st_size > 0:
            return final
        url = vi.url if page is None else f"{vi.url}?p={page}"
        raw_tmpl = str(workdir / f"{stem}.raw.%(ext)s")
        opts = {
            "format": AUDIO_FORMATS,
            "outtmpl": raw_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "retries": 2,
            "fragment_retries": 2,
            # Bilibili's CDN mirrors answer HTTP 567 to un-ranged GETs and throttle long
            # streams; ranged 10 MB chunks are both accepted and fast.
            "http_chunk_size": 10 * 1024 * 1024,
            "postprocessors": [],
            "fixup": "never",
            # Generic UA; the bare site Referer is what the CDN mirrors accept most reliably.
            "http_headers": {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"},
        }
        if page is None:
            opts["playlist_items"] = "1"
        if self.cookies:
            opts["cookiefile"] = str(self.cookies)
        raw: Path | None = None
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(url, download=True)
                if info.get("entries"):
                    info = info["entries"][0]
                raw = Path(y.prepare_filename(info))
        except Exception as e:
            log.warning("yt-dlp download failed (%s); trying the playurl API directly", str(e)[:200])
            raw = None
        if raw is None or not raw.exists():
            cands = sorted(workdir.glob(f"{stem}.raw.*"))
            cands = [c for c in cands if not c.name.endswith(".part")]
            raw = cands[0] if cands else self._download_playurl(vi, page or 1, workdir / f"{stem}.raw.m4a")
        remux_audio(raw, final)
        raw.unlink(missing_ok=True)
        return final

    def _download_playurl(self, vi: VideoInfo, page: int, dest: Path) -> Path:
        """Fallback: ask playurl for DASH audio and try the base URL and every backup URL."""
        part = next((p for p in vi.parts if p.page == page), vi.parts[0])
        d = self._api("https://api.bilibili.com/x/player/playurl",
                      {"bvid": vi.bvid, "cid": part.cid, "fnval": 4048, "fourk": 1})
        audios = [a for a in ((d.get("dash") or {}).get("audio") or []) if str(a.get("codecs", "")).startswith("mp4a")]
        audios.sort(key=lambda a: a.get("bandwidth", 0), reverse=True)
        for a in audios:
            for u in [a.get("baseUrl") or a.get("base_url")] + list(a.get("backupUrl") or a.get("backup_url") or []):
                if not u:
                    continue
                try:
                    with self.http.session.get(u, headers={"Referer": "https://www.bilibili.com/"}, stream=True,
                                               timeout=30) as r:
                        if r.status_code not in (200, 206):
                            continue
                        with open(dest, "wb") as f:
                            for chunk in r.iter_content(1 << 16):
                                f.write(chunk)
                    return dest
                except Exception as e:
                    log.info("audio URL failed: %s", e)
        raise BilibiliError(f"no downloadable AAC audio for {vi.bvid} p{page}")

    def download_cover(self, url: str, dest: Path) -> Path | None:
        if not url:
            return None
        if dest.exists():
            return dest
        try:
            data = self.http.get_bytes(url.replace("http://", "https://"))
        except Exception as e:
            log.warning("cover download failed: %s", e)
            return None
        dest.write_bytes(data)
        return dest


def remux_audio(src: Path, dst: Path) -> None:
    """Stream-copy the first audio stream into a plain (non-fragmented) .m4a."""
    tmp = dst.with_name(dst.stem + ".tmp.m4a")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src), "-map", "0:a:0", "-c", "copy",
           "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(tmp)]
    subprocess.run(cmd, check=True)
    shutil.move(tmp, dst)
