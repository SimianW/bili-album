"""Music catalogs behind one interface: NetEase Cloud Music and the iTunes Search API.

Both are reachable without keys. MusicBrainz is not used: it rejected a bare
client during setup and wants contact details in the User-Agent, which this
tool does not send.

The main entry point is :meth:`Catalog.find_album`, which searches several
candidate strings, fetches tracklists for the best-named candidates, and ranks
them by name similarity, total-duration fit and (when known) tracklist overlap.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import asdict, dataclass, field

from .net import Http, HttpError
from .textnorm import fold, norm, similarity

log = logging.getLogger(__name__)

NETEASE_HEADERS = {"Referer": "https://music.163.com/"}
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_LRC_TS = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
_EDITION = re.compile(r"伴奏|纯音乐|instrumental|karaoke|off\s*vocal|\binst\.?\b|\(inst\)", re.I)
_FULL_LENGTH = re.compile(r"专辑全长|全长版|full\s*album|album\s*mix|continuous mix", re.I)


@dataclass
class CatTrack:
    title: str
    artist: str
    duration: float
    number: int = 0
    disc: int = 1
    id: str = ""
    provider: str = ""
    composer: str = ""
    lyrics: str = ""


@dataclass
class CatAlbum:
    provider: str
    id: str
    title: str
    artist: str
    url: str = ""
    year: str = ""
    release_date: str = ""
    genre: str = ""
    cover_url: str = ""
    track_count: int = 0
    tracks: list[CatTrack] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(t.duration for t in self.tracks)

    @property
    def disc_total(self) -> int:
        return max((t.disc for t in self.tracks), default=1) or 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total"] = round(self.total, 3)
        return d


@dataclass
class AlbumMatch:
    album: CatAlbum
    score: float
    name_score: float
    duration_score: float
    list_score: float | None
    reason: str


class Provider:
    name = ""

    def search_albums(self, q: str) -> list[CatAlbum]:
        raise NotImplementedError

    def album(self, a: CatAlbum) -> CatAlbum:
        raise NotImplementedError

    def search_tracks(self, q: str) -> list[tuple[CatTrack, CatAlbum]]:
        raise NotImplementedError

    def lyrics(self, t: CatTrack) -> tuple[str, str]:
        """(lyrics, composer)."""
        return "", ""


class NetEase(Provider):
    name = "netease"

    def __init__(self, http: Http):
        self.http = http

    def _get(self, path: str, params: dict | None = None, relevant=None) -> dict:
        def ok(j):
            return isinstance(j, dict) and j.get("code") == 200 and (relevant is None or relevant(j))
        j = self.http.get_json("https://music.163.com" + path, params=params, headers=NETEASE_HEADERS,
                               validate=ok, stateless=True)
        if not ok(j):
            raise HttpError(f"netease {path}: code {j.get('code')}")
        return j

    @staticmethod
    def _summary(a: dict) -> CatAlbum:
        pt = a.get("publishTime") or 0
        year = date = ""
        if pt:
            import datetime as dt
            d = dt.datetime.fromtimestamp(pt / 1000, dt.UTC) + dt.timedelta(hours=8)
            date, year = d.strftime("%Y-%m-%d"), d.strftime("%Y")
        artists = a.get("artists") or ([a["artist"]] if a.get("artist") else [])
        return CatAlbum(
            provider="netease", id=str(a["id"]), title=a.get("name", ""),
            artist=" & ".join(x.get("name", "") for x in artists if x.get("name")) or "",
            url=f"https://music.163.com/#/album?id={a['id']}", year=year, release_date=date,
            cover_url=(a.get("picUrl") or "").replace("http://", "https://"),
            track_count=int(a.get("size") or 0))

    def search_albums(self, q: str) -> list[CatAlbum]:
        j = self._get("/api/search/get", {"s": q, "type": 10, "limit": 10, "offset": 0},
                      relevant=lambda j: _relevant(q, [(a.get("name", ""), (a.get("artist") or {}).get("name", ""))
                                                       for a in (j.get("result") or {}).get("albums") or []]))
        return [self._summary(a) for a in (j.get("result") or {}).get("albums") or []]

    def album(self, a: CatAlbum) -> CatAlbum:
        j = self._get(f"/api/v1/album/{a.id}")
        full = self._summary(j["album"])
        tracks = []
        for s in j.get("songs") or []:
            try:
                disc = int(str(s.get("cd") or "1").split("/")[0] or 1)
            except ValueError:
                disc = 1
            tracks.append(CatTrack(
                title=s.get("name", ""), artist=" & ".join(x["name"] for x in s.get("ar") or [] if x.get("name")),
                duration=(s.get("dt") or 0) / 1000.0, number=int(s.get("no") or 0), disc=disc,
                id=str(s.get("id", "")), provider="netease"))
        full.tracks = drop_full_length(tracks)
        if full.tracks and len({t.disc for t in full.tracks}) == 1:
            for t in full.tracks:
                t.disc = 1
        full.track_count = len(full.tracks)
        return full

    def search_tracks(self, q: str) -> list[tuple[CatTrack, CatAlbum]]:
        j = self._get("/api/search/get", {"s": q, "type": 1, "limit": 20, "offset": 0},
                      relevant=lambda j: _relevant(q, [(s.get("name", ""), " ".join(a.get("name", "") for a in s.get("artists") or []))
                                                       for s in (j.get("result") or {}).get("songs") or []]))
        out = []
        for s in (j.get("result") or {}).get("songs") or []:
            al = s.get("album") or {}
            t = CatTrack(title=s.get("name", ""),
                         artist=" & ".join(x["name"] for x in s.get("artists") or [] if x.get("name")),
                         duration=(s.get("duration") or 0) / 1000.0, number=int(s.get("no") or 0),
                         id=str(s.get("id", "")), provider="netease")
            out.append((t, self._summary({**al, "artists": s.get("artists") or []}) if al.get("id") else
                        CatAlbum("netease", "", "", t.artist)))
        return out

    def lyrics(self, t: CatTrack) -> tuple[str, str]:
        if t.provider != "netease" or not t.id:
            return "", ""
        try:
            j = self._get("/api/song/lyric", {"id": t.id, "lv": 1, "tv": -1})
        except Exception:
            return "", ""
        return parse_lrc((j.get("lrc") or {}).get("lyric", ""))


class ITunes(Provider):
    name = "itunes"

    def __init__(self, http: Http, countries: tuple[str, ...] = ("US", "TW")):
        self.http = http
        self.countries = countries

    def _countries(self, q: str) -> tuple[str, ...]:
        if _CJK.search(q):
            return ("TW", "HK", "US")[: max(2, len(self.countries))]
        return self.countries

    @staticmethod
    def _summary(a: dict, country: str) -> CatAlbum:
        rd = a.get("releaseDate") or ""
        art = (a.get("artworkUrl100") or "").replace("100x100bb", "1200x1200bb")
        return CatAlbum(
            provider="itunes", id=f"{a['collectionId']}@{country}", title=a.get("collectionName", ""),
            artist=a.get("artistName", ""), url=a.get("collectionViewUrl", "").split("?")[0],
            year=rd[:4], release_date=rd[:10], genre=a.get("primaryGenreName", ""), cover_url=art,
            track_count=int(a.get("trackCount") or 0))

    def search_albums(self, q: str) -> list[CatAlbum]:
        out, seen = [], set()
        for c in self._countries(q):
            try:
                j = self.http.get_json("https://itunes.apple.com/search",
                                       params={"term": q, "entity": "album", "limit": 10, "country": c})
            except HttpError as e:
                log.info("itunes %s: %s", c, e)
                continue
            for a in j.get("results") or []:
                if a.get("collectionId") in seen:
                    continue
                seen.add(a.get("collectionId"))
                out.append(self._summary(a, c))
        return out

    def album(self, a: CatAlbum) -> CatAlbum:
        cid, _, country = a.id.partition("@")
        j = self.http.get_json("https://itunes.apple.com/lookup",
                               params={"id": cid, "entity": "song", "country": country or "US"})
        res = j.get("results") or []
        head = next((r for r in res if r.get("wrapperType") == "collection"), None)
        full = self._summary(head, country) if head else a
        tracks = [CatTrack(title=r.get("trackName", ""), artist=r.get("artistName", ""),
                           duration=(r.get("trackTimeMillis") or 0) / 1000.0,
                           number=int(r.get("trackNumber") or 0), disc=int(r.get("discNumber") or 1),
                           id=str(r.get("trackId", "")), provider="itunes")
                  for r in res if r.get("wrapperType") == "track" and r.get("kind") in (None, "song")]
        tracks.sort(key=lambda t: (t.disc, t.number))
        full.tracks = drop_full_length(tracks)
        full.track_count = len(full.tracks)
        return full

    def search_tracks(self, q: str) -> list[tuple[CatTrack, CatAlbum]]:
        out = []
        for c in self._countries(q)[:2]:
            try:
                j = self.http.get_json("https://itunes.apple.com/search",
                                       params={"term": q, "entity": "song", "limit": 15, "country": c})
            except HttpError:
                continue
            for r in j.get("results") or []:
                t = CatTrack(title=r.get("trackName", ""), artist=r.get("artistName", ""),
                             duration=(r.get("trackTimeMillis") or 0) / 1000.0,
                             number=int(r.get("trackNumber") or 0), disc=int(r.get("discNumber") or 1),
                             id=str(r.get("trackId", "")), provider="itunes")
                al = self._summary(r, c) if r.get("collectionId") else CatAlbum("itunes", "", "", t.artist)
                al.track_count = int(r.get("trackCount") or 0)
                out.append((t, al))
        return out


def _relevant(q: str, rows: list[tuple[str, str]]) -> bool:
    """Guard against junk answers (NetEase returns popular items when it throttles)."""
    if not rows:
        return True
    nq = norm(q)
    for name, artist in rows:
        for x in (name, artist):
            nx = norm(x)
            if nx and (nx in nq or nq in nx or similarity(x, q) > 0.5):
                return True
    return False


def drop_full_length(tracks: list[CatTrack]) -> list[CatTrack]:
    """NetEase sometimes lists a 'whole album' track next to the real ones."""
    if len(tracks) < 3:
        return tracks
    total = sum(t.duration for t in tracks)
    keep = []
    for t in tracks:
        rest = total - t.duration
        if _FULL_LENGTH.search(t.title) or (rest > 0 and abs(t.duration - rest) / rest < 0.05):
            continue
        keep.append(t)
    if len(keep) != len(tracks):
        for i, t in enumerate(keep, 1):
            if t.number in (0,) or len({x.number for x in keep}) != len(keep):
                t.number = i
        # Renumber if the dropped track was number 1.
        if keep and keep[0].number != 1 and all(b.number == a.number + 1 for a, b in zip(keep, keep[1:])):
            off = keep[0].number - 1
            for t in keep:
                t.number -= off
    return keep


def parse_lrc(lrc: str) -> tuple[str, str]:
    """Plain lyrics and the composer credit from an LRC text."""
    lines, composer = [], ""
    for raw in (lrc or "").splitlines():
        line = _LRC_TS.sub("", raw).strip()
        if not line:
            continue
        m = re.match(r"^(作曲|曲|composer|music)\s*[:：]\s*(.+)$", line, re.I)
        if m and not composer:
            composer = m.group(2).strip()
            continue
        if re.match(r"^(作词|词|编曲|制作人|lyricist|lyrics|arranger|producer|混音|母带|录音)\s*[:：]", line, re.I):
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    if text in ("纯音乐，请欣赏", "纯音乐, 请欣赏"):
        text = ""
    return text, composer


class Catalog:
    """Album and track lookup across providers. Providers that fail are skipped."""

    def __init__(self, http: Http, providers: list[Provider] | None = None):
        self.http = http
        self.providers = providers if providers is not None else [NetEase(http), ITunes(http)]
        self.errors: list[str] = []

    def _try(self, fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            msg = f"{getattr(fn, '__self__', fn).__class__.__name__}: {e}"
            log.warning("catalog: %s", msg)
            self.errors.append(msg)
            return None

    def find_album(self, queries: list[str], *, duration: float, artists: list[str] = (),
                   works: list[str] = (), titles: list[str] = (), n_tracks: int | None = None,
                   max_fetch: int = 6) -> list[AlbumMatch]:
        """Rank album candidates for a video of ``duration`` seconds."""
        summaries: list[CatAlbum] = []
        seen = set()
        for q in queries:
            for p in self.providers:
                res = self._try(p.search_albums, q) or []
                for a in res:
                    k = (a.provider, a.id)
                    if k not in seen:
                        seen.add(k)
                        summaries.append(a)

        def pre(a: CatAlbum) -> float:
            ns = max([similarity(w, a.title) for w in works] or [0.0])
            ar = max([similarity(x, a.artist) for x in artists] or [0.0])
            if n_tracks and a.track_count:
                ns += 0.15 * math.exp(-abs(a.track_count - n_tracks) / 2)
            if a.track_count <= 1:
                ns -= 0.3
            return ns + 0.5 * ar

        summaries.sort(key=pre, reverse=True)
        fetched: list[CatAlbum] = []
        per_provider: dict[str, int] = {}
        for a in summaries:
            if per_provider.get(a.provider, 0) >= max_fetch:
                continue
            if pre(a) < 0.35:
                continue
            per_provider[a.provider] = per_provider.get(a.provider, 0) + 1
            full = self._try(next(p for p in self.providers if p.name == a.provider).album, a)
            if full and full.tracks:
                fetched.append(full)
        out = [score_album(a, duration=duration, artists=artists, works=works, titles=titles,
                           n_tracks=n_tracks) for a in fetched]
        out.sort(key=lambda m: m.score, reverse=True)
        return out

    def find_track(self, title: str, artist: str = "", duration: float | None = None
                   ) -> list[tuple[float, CatTrack, CatAlbum]]:
        q = f"{artist} {title}".strip()
        res: list[tuple[float, CatTrack, CatAlbum]] = []
        for p in self.providers:
            for t, al in self._try(p.search_tracks, q) or []:
                s = 0.6 * similarity(title, t.title)
                s += 0.25 * (similarity(artist, t.artist) if artist else 0.5)
                if duration and t.duration:
                    s += 0.15 * math.exp(-abs(t.duration - duration) / 6.0)
                res.append((s, t, al))
        res.sort(key=lambda x: x[0], reverse=True)
        return res

    def album_details(self, a: CatAlbum) -> CatAlbum:
        p = next((p for p in self.providers if p.name == a.provider), None)
        if not p or not a.id:
            return a
        return self._try(p.album, a) or a

    def lyrics(self, t: CatTrack, album: CatAlbum | None = None) -> tuple[str, str]:
        """Lyrics and composer, asking NetEase even for tracks found elsewhere."""
        for p in self.providers:
            if p.name == t.provider:
                got = self._try(p.lyrics, t)
                if got and (got[0] or got[1]):
                    return got
        ne = next((p for p in self.providers if isinstance(p, NetEase)), None)
        if ne and t.provider != "netease":
            cands = self._try(ne.search_tracks, f"{t.artist} {t.title}") or []
            for ct, _ in cands[:5]:
                if similarity(ct.title, t.title) > 0.85 and (not t.duration or abs(ct.duration - t.duration) < 8):
                    got = self._try(ne.lyrics, ct)
                    if got:
                        return got
        return "", ""

    def enrich(self, best: CatAlbum) -> CatAlbum:
        """Fill genre / cover / date gaps from the same album at another provider."""
        if best.genre and best.cover_url and best.release_date:
            return best
        for p in self.providers:
            if p.name == best.provider:
                continue
            for a in self._try(p.search_albums, f"{best.artist} {best.title}") or []:
                if similarity(a.title, best.title) > 0.85 and similarity(a.artist, best.artist) > 0.6:
                    genre = a.genre
                    if genre and is_cjk(genre) and best.provider == "netease":
                        genre = fold(genre)  # 華語流行樂 -> 华语流行乐, matching NetEase's script
                    best.genre = best.genre or genre
                    best.release_date = best.release_date or a.release_date
                    best.year = best.year or a.year
                    if not best.cover_url:
                        best.cover_url = a.cover_url
                    if a.provider == "itunes" and a.cover_url:
                        best.extra_cover = a.cover_url  # type: ignore[attr-defined]
                    return best
        return best


def score_album(a: CatAlbum, *, duration: float, artists=(), works=(), titles=(),
                n_tracks: int | None = None) -> AlbumMatch:
    name = max([similarity(w, a.title) for w in works] or [0.0])
    art = max([similarity(x, a.artist) for x in artists] or [0.0])
    # Artist can also appear inside the works list when the title had no clear layout.
    if not artists and works:
        art = max([similarity(w, a.artist) for w in works] + [0.0])
    T = a.total
    dscore = math.exp(-abs(T - duration) / (0.03 * duration + 20)) if T and duration else 0.0
    lscore = None
    if titles:
        hit = sum(1 for t in titles if max((similarity(t, x.title) for x in a.tracks), default=0) > 0.8)
        lscore = hit / max(len(titles), len(a.tracks))
    nscore = 0.65 * name + 0.35 * art
    if lscore is not None:
        score = 0.3 * nscore + 0.3 * dscore + 0.4 * lscore
    else:
        score = 0.5 * nscore + 0.5 * dscore
    if n_tracks and a.tracks and abs(len(a.tracks) - n_tracks) > 1:
        score -= 0.1
    # Tie-break towards the spelling the uploader used (same script: 张杰 rather than 張杰).
    score += 0.015 * same_script(a.artist, artists) + 0.015 * same_script(a.title, works)
    # Instrumental / karaoke editions share names and lengths with the real album.
    if _EDITION.search(a.title) and not any(_EDITION.search(w) for w in works):
        score -= 0.15
    reason = (f"{a.provider}:{a.artist} - {a.title} ({len(a.tracks)} tracks, {T:.0f}s vs video {duration:.0f}s; "
              f"name {nscore:.2f}, duration {dscore:.2f}" + (f", tracklist {lscore:.2f}" if lscore is not None else "")
              + ")")
    return AlbumMatch(a, score, nscore, dscore, lscore, reason)


def same_script(name: str, candidates) -> bool:
    import unicodedata

    n = re.sub(r"\W+", "", unicodedata.normalize("NFKC", name or "")).casefold()
    return bool(n) and any(re.sub(r"\W+", "", unicodedata.normalize("NFKC", c or "")).casefold() == n
                           for c in candidates)


def is_cjk(s: str) -> bool:
    return bool(_CJK.search(fold(s)))
