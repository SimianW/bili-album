"""Fuse every source of evidence into a reviewable ``plan.json``.

Stages (each timed into ``plan.timings``):

fetch      Bilibili page metadata (or offline ``--audio``/``--title``)
download   audio stream(s) and the Bilibili cover
analyze    boundary candidates from the audio (silence / dip / novelty)
catalog    album / track lookup (NetEase, iTunes) and lyrics
recognize  Shazam clips: verify titles, pin track starts, or scan unknown mixes
refine     millisecond refinement of the boundaries between tracks (:mod:`refine`)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import analyze as an
from .align import align, snap
from .analyze import DIGITAL_DB, Candidate, level_between
from .bilibili import Bilibili, Part, VideoInfo, extract_bvid
from .catalog import (DERIVED_TRACK, AlbumMatch, CatAlbum, Catalog, CatTrack, latin_only, rank_releases,
                      release_kind)
from .hints import (HintEntry, HintList, TitleGuess, derivative_cues, fmt_ts, guess_from_hint,
                    guess_from_title, name_like, names_in_prose, parse_timestamps, parse_tracklist_file,
                    part_title, plausible_list)
from .media import audio_info, image_kind, prepare_offline_audio, square_cover
from .net import Http
from .recognize import NullRecognizer, Recognition, Recognizer, ShazamRecognizer
from .refine import refine_starts
from .textnorm import has_han, has_kana, norm, similarity, strip_decorations, strip_translation, to_simplified

log = logging.getLogger(__name__)

SINGLE_MAX = 540.0          # "short" video: about 9 minutes
REVIEW_TRIM = 5.0           # seconds of trimmed head/tail worth a review line
LOW_CONF = 0.6
CUT_MARGIN = 0.3            # a cut stays this far before a recognised recording start
SAMPLE_REQUESTS = 5         # Shazam requests a sampled check of a clean alignment may use
WEAK_STRENGTH = 0.4         # a dip / novelty start weaker than this is a weak signal


_CREDIT = re.compile(r"(?:原唱|翻自|原曲)\s*[:：]?\s*([^()（）\[\]【】/|]+)")


def _title_core(title: str) -> str:
    """Title without bracketed decorations or version tags, normalised: the song's identity."""
    t = re.sub(r"[\(（\[【][^\)）\]】]*[\)）\]】]", "", title or "")
    return norm(strip_decorations(t)) or norm(title)


def _date(al: CatAlbum) -> str:
    return al.release_date or al.year or "9999"


def pick_original(same: list[tuple[CatTrack, CatAlbum]], family: list[tuple[CatTrack, CatAlbum]]
                  ) -> tuple[list[tuple[CatTrack, CatAlbum]], str]:
    """Order candidate originals of a derivative, best first, with the rule that decided.

    ``same``: unmarked same-titled catalog songs; ``family``: every same-titled entry,
    including marked versions (DJ版, 翻唱, 伴奏 ...).

    1. An explicit credit on any entry ("原唱：X", "翻自 X") names the original artist.
    2. Otherwise the original is the earliest release that other artists' same-titled
       versions followed (two or more within the next 12 months): a hit draws covers and
       remakes; an older namesake with no versions after it is a different song.
    3. Otherwise the earliest release.
    The video's own length is never used: a derivative's length is not evidence."""
    if not same:
        return [], ""
    dated = sorted(same, key=lambda x: _date(x[1]))
    credits: dict[str, int] = {}
    for t, al in family:
        for m in _CREDIT.finditer(f"{t.title} {al.title}"):
            name = m.group(1).strip(" ：:,，")
            if name:
                credits[norm(name)] = credits.get(norm(name), 0) + 1
    for name in sorted(credits, key=credits.get, reverse=True):
        hit = [x for x in dated if norm(x[0].artist) == name or name in norm(x[0].artist)]
        if hit:
            return hit + [x for x in dated if x not in hit], f"other catalog entries credit {hit[0][0].artist} " \
                                                              f"as the original ({credits[name]}x)"

    def followers(x) -> int:
        d0 = _date(x[1])
        if d0 == "9999":
            return 0
        y, rest = int(d0[:4]), d0[4:]
        limit = f"{y + 1}{rest}"
        return len({norm(t.artist) for t, al in family
                    if d0 < _date(al) <= limit and norm(t.artist) != norm(x[0].artist)})
    with_f = [x for x in dated if followers(x) >= 2]
    if with_f:
        x = with_f[0]
        return [x] + [y for y in dated if y is not x], (f"earliest same-titled release that other artists' "
                                                        f"versions followed ({followers(x)} within a year)")
    return dated, f"earliest of {len(dated)} same-titled catalog songs (none drew versions within a year)"


TAG_FIELDS = ("title", "artist", "album", "album_artist", "genre", "composer", "lyrics")
_ZH_GENRE = re.compile(r"华语|華語|国语|國語|粤语|粵語|台语|台語|闽南|閩南|mandopop|cantopop|c-?pop|chinese", re.I)
_JA_GENRE = re.compile(r"j-?pop|j-?rock|anime|アニメ|演歌|enka|japan|日本|邦楽", re.I)
# Function words that Chinese uses and Japanese does not (either script).
_ZH_MARKERS = re.compile(r"[你妳们們这這说說吗嗎呢吧么麼没沒谁誰咱哪啊呀]")
_NO_CONVERT_KEYS = {"url", "id", "bvid", "audio_path", "cover_path", "part_paths", "provider", "kind"}


def simplify_plan(plan: dict) -> list[str]:
    """Write every Chinese string of a plan in Simplified script (owner rule: no Traditional
    characters in the output, whatever the official release uses). One choke point, applied
    where tags are final, so catalog, recognition, uploader text, --tracklist and
    --album-hint values all pass through it; ``review`` calls it again before writing.

    Japanese must not be converted. Strings with kana never are (``to_simplified``). For
    kanji-only strings the album decides: Japanese when a title / artist / album name has kana,
    the genre is Japanese, or kana are more than 15% of the lyrics' Han + kana characters;
    Chinese when the genre is Chinese (this wins), a tag string uses Chinese-only function
    words (你 / 們 / 这 / 說 / 吗 / 没 ...), or the lyrics are Han-dominant.
    When neither is clear a string is converted only if that changes nothing; otherwise it is
    left as written and the doubt is returned as review reasons. ``source`` (the raw input
    record), paths, URLs and ids are not touched; everything else (tags, evidence, reasons)
    follows the same decision. Returns review reasons for undecided tag strings."""
    tracks = plan.get("tracks") or []
    album = plan.get("album") or {}
    tags = [album.get(k) or "" for k in ("title", "artist", "genre")]
    tags += [t.get(k) or "" for t in tracks for k in TAG_FIELDS]
    genre = " ".join([album.get("genre") or ""] + [t.get("genre") or "" for t in tracks])
    lyrics = [t.get("lyrics") or "" for t in tracks]
    names = [x for x in tags if x not in lyrics]
    kana_n = sum(len(re.findall(r"[぀-ヿ]", x)) for x in lyrics)
    han_n = sum(len(re.findall(r"[㐀-鿿]", x)) for x in lyrics)
    # A few Japanese words inside Chinese lyrics do not make a Japanese album.
    ja_lyrics = kana_n > 0.15 * max(1, kana_n + han_n)
    zh_genre = bool(_ZH_GENRE.search(genre))
    ja = not zh_genre and (any(has_kana(x) for x in names) or bool(_JA_GENRE.search(genre)) or ja_lyrics)
    zh = zh_genre or any(_ZH_MARKERS.search(x) for x in tags) or (han_n > 0 and not ja_lyrics)
    doubts: list[str] = []

    def conv(x: str, where: str | None) -> str:
        if not isinstance(x, str) or not x:
            return x
        out = to_simplified(x)
        if out == x:
            return x
        if ja and not _ZH_MARKERS.search(x):
            return x                      # kanji of a Japanese release
        if zh or _ZH_MARKERS.search(x):
            return out
        if where:
            doubts.append(f"script: cannot tell whether {where} {x!r} is Chinese or Japanese; left as written "
                          f"(Simplified would be {out!r})")
        return x

    for k in ("title", "artist", "genre"):
        album[k] = conv(album.get(k, ""), f"album {k}")
    for t in tracks:
        for k in TAG_FIELDS:
            if k in t:
                t[k] = conv(t[k], f"track {t.get('index')} {k}" if k != "lyrics" else f"track {t.get('index')} lyrics")

    def walk(o):
        # In place: callers (review) keep references to the track dicts.
        if isinstance(o, dict):
            for k, v in o.items():
                if k in _NO_CONVERT_KEYS or k.endswith("_path"):
                    continue
                o[k] = conv(v, None) if isinstance(v, str) else (walk(v) or v)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                o[i] = conv(v, None) if isinstance(v, str) else (walk(v) or v)
        return None
    for k in list(plan):
        if k != "source":
            v = plan[k]
            plan[k] = conv(v, None) if isinstance(v, str) else (walk(v) or v)
    return doubts


def sample_indices(n: int, k: int = 3) -> list[int]:
    """One track from the middle of each of ``k`` equal parts of the album (first, middle and
    last third for k = 3), so a swapped or missing track anywhere shifts at least one sample's
    label or start."""
    if n <= k:
        return list(range(n))
    return sorted({min(n - 1, int((2 * j + 1) * n / (2 * k))) for j in range(k)})


def place_cut(imp: float, cands: list[Candidate], feats, lo: float = 0.0) -> tuple[float, str]:
    """Where to cut before a recording that recognition says starts at ``imp``.

    Rule: a cut may not land after ``imp - CUT_MARGIN``, unless everything between that point
    and the cut is digital silence (then nothing audible is lost and the gap stays with the
    previous track). If ``imp`` falls in a low-level run, the run's own cut (the end of its
    deepest block, see :mod:`analyze`) is proposed first; otherwise ``imp - CUT_MARGIN``.
    Returns (time, explanation for the evidence)."""
    gap = next((c for c in cands if c.kind == "silence" and c.gap_start - 0.3 <= imp <= c.gap_end + 0.3), None)
    limit = imp - CUT_MARGIN
    if gap:
        new = gap.time
        what = ("digital silence" if gap.digital else f"quiet gap (floor {gap.floor_db:.0f} dB, not silence)")
        note = f"recording starts at {imp:.2f}s inside a {what} {gap.gap_start:.2f}-{gap.gap_end:.2f}s"
    else:
        new = limit
        note = f"recording starts at {imp:.2f}s, no silence there"
    if new > limit:
        lvl = level_between(feats, limit, new) if feats is not None else 0.0
        if lvl <= DIGITAL_DB:
            note += f"; cut {new - imp:+.2f}s from it stays in digital silence"
        else:
            new = limit
            note += (f"; cut held {CUT_MARGIN}s before the recognised start (sound up to {lvl:.0f} dB in between, "
                     "so cutting later would clip the intro)")
    else:
        note += f"; cut {new - imp:+.2f}s from it"
    return max(lo, new), note


@dataclass
class PlanOptions:
    workdir: Path | None = None
    text_hints: bool = True
    cookies: Path | None = None
    album_hint: str | None = None
    tracklist: Path | None = None
    audio: Path | None = None
    title: str | None = None
    description: str = ""
    recognize: bool = True
    refine: bool = True
    refine_options: dict = field(default_factory=dict)   # keyword arguments for refine.refine_starts
    repo_root: Path = field(default_factory=lambda: Path.cwd())


def ev(kind: str, detail: str) -> dict:
    return {"kind": kind, "detail": detail}


class Timer:
    def __init__(self):
        self.t: dict[str, float] = {k: 0.0 for k in ("fetch", "download", "analyze", "catalog", "recognize")}
        self.t0 = time.monotonic()

    def run(self, stage: str, fn, *a, **kw):
        s = time.monotonic()
        try:
            return fn(*a, **kw)
        finally:
            self.t[stage] = self.t.get(stage, 0.0) + time.monotonic() - s

    def as_dict(self) -> dict:
        d = {k: round(v, 2) for k, v in self.t.items()}
        d["total"] = round(time.monotonic() - self.t0, 2)
        return d


class TimedRecognizer(Recognizer):
    def __init__(self, inner: Recognizer, timer: Timer):
        self.inner, self.timer = inner, timer
        self.log: list[dict] = []

    @property
    def available(self):  # type: ignore[override]
        return self.inner.available

    @property
    def calls(self):  # type: ignore[override]
        return getattr(self.inner, "calls", 0)

    @property
    def exhausted(self):  # type: ignore[override]
        return getattr(self.inner, "exhausted", False)

    def recognize(self, audio, start, length=12.0):
        r = self.timer.run("recognize", self.inner.recognize, audio, start, length)
        self.log.append({"audio": Path(audio).name, **r.to_dict(), "implied_start": r.implied_start})
        return r


@dataclass
class Seg:
    """A track under construction."""
    start: float
    end: float
    title: str = ""
    artist: str = ""
    cat: CatTrack | None = None
    audio: Path | None = None           # multipart: the part's own file
    confidence: float = 0.5
    evidence: list[dict] = field(default_factory=list)
    start_kind: str = ""                # what the start boundary rests on
    recog: Recognition | None = None


class Planner:
    def __init__(self, opts: PlanOptions):
        self.o = opts
        self.timer = Timer()
        self.review: list[str] = []
        self.kind_reason = ""
        self.recog_info: dict = {"policy": "none", "tracks_checked": 0, "requests": 0, "reason": ""}
        self.refine_info: dict = {"boundaries": 0, "moved": 0, "methods": {}, "reason": "not run"}
        self.derivative: dict | None = None
        self.reordered = False
        self.feats = None
        self._align_missing: list[int] = []
        self._align_mark = 0

    # ------------------------------------------------------------------ source

    def _source(self, url: str | None) -> tuple[VideoInfo, Path, list[Path]]:
        o = self.o
        if o.audio:
            src = Path(o.audio).resolve()
            h = hashlib.sha1()
            with open(src, "rb") as f:
                h.update(f.read(1 << 20))
            h.update(str(src.stat().st_size).encode())
            bvid = "offline-" + h.hexdigest()[:10]
            self.workdir = Path(o.workdir or o.repo_root / ".work" / bvid).resolve()
            self.workdir.mkdir(parents=True, exist_ok=True)
            self.http = Http(self.workdir / "cache")
            audio = self.timer.run("download", prepare_offline_audio, src, self.workdir / "source.m4a")
            dur = audio_info(audio)["duration"]
            vi = VideoInfo(url="", bvid=bvid, aid=0, title=o.title or src.stem, uploader="", uploader_mid=0,
                           description=o.description or "", duration=dur, cover_url="", pubdate=0,
                           parts=[Part(1, "", dur)])
            return vi, audio, []
        assert url
        pre = Http(None)
        bvid = extract_bvid(url, pre)
        self.workdir = Path(o.workdir or o.repo_root / ".work" / bvid).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.http = Http(self.workdir / "cache")
        self.bili = Bilibili(self.http, o.cookies)
        vi = self.timer.run("fetch", self.bili.info, f"https://www.bilibili.com/video/{bvid}",
                            with_comments=o.text_hints)
        parts: list[Path] = []
        audio = None
        if len(vi.parts) > 1:
            for p in vi.parts:
                parts.append(self.timer.run("download", self.bili.download_audio, vi, self.workdir, page=p.page))
        else:
            audio = self.timer.run("download", self.bili.download_audio, vi, self.workdir)
        self.timer.run("download", self.bili.download_cover, vi.cover_url, self.workdir / "bili_cover.jpg")
        return vi, audio or parts[0], parts

    # ------------------------------------------------------------------ hints

    def _hint_lists(self, vi: VideoInfo, duration: float) -> list[HintList]:
        if not self.o.text_hints:
            return []
        out: list[HintList] = []
        if len(vi.chapters) >= 2:
            out.append(HintList("chapter", "Bilibili chapters",
                                [HintEntry(c["start_time"], c["title"], end=c.get("end_time") or None)
                                 for c in vi.chapters]))
        d = plausible_list(parse_timestamps(vi.description), duration)
        if d:
            out.append(HintList("description", "timestamps in the description", d, by_uploader=True))
        ranked = sorted(vi.comments, key=lambda c: (not (c.pinned and c.by_uploader), not c.by_uploader,
                                                    not c.pinned, -c.likes))
        for c in ranked:
            e = plausible_list(parse_timestamps(c.text), duration)
            if e:
                who = "uploader's " if c.by_uploader else ""
                out.append(HintList("comment", f"{who}{'pinned ' if c.pinned else ''}comment by {c.author}", e,
                                    by_uploader=c.by_uploader, pinned=c.pinned))
        return out

    def _record_candidates(self, matches: list[AlbumMatch]) -> None:
        self.album_candidates = [
            {"score": round(m.score, 3), "reason": m.reason, "provider": m.album.provider, "id": m.album.id,
             "title": m.album.title, "artist": m.album.artist, "release_date": m.album.release_date,
             "tracks": [{"number": t.number, "title": t.title, "artist": t.artist, "duration": t.duration}
                        for t in m.album.tracks]} for m in matches[:10]]

    def _guess(self, vi: VideoInfo) -> TitleGuess:
        g = guess_from_title(vi.title)
        if self.o.album_hint:
            h = guess_from_hint(self.o.album_hint)
            h.year = h.year or g.year
            return h
        return g

    # ------------------------------------------------------------------ main

    def run(self, url: str | None) -> Path:
        vi, audio, part_files = self._source(url)
        self.vi = vi
        self.audio = audio
        self.catalog = Catalog(self.http)
        rec: Recognizer = ShazamRecognizer(self.workdir / "cache") if self.o.recognize else NullRecognizer()
        self.rec = TimedRecognizer(rec, self.timer)
        try:
            plan = self._plan(vi, audio, part_files)
        finally:
            if hasattr(rec, "close"):
                rec.close()
        path = self.workdir / "plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._write_side_files()
        return path

    def _write_side_files(self) -> None:
        """Plain JSON records of the inputs the plan used, for reviewers and tools:
        recognitions.json (every recognition answer, cached or not), album_candidates.json
        (catalog albums or releases considered, best first), text_hints.json (parsed hints)."""
        def dump(name, obj):
            (self.workdir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8")
        try:
            dump("recognitions.json", self.rec.log)
            dump("album_candidates.json", getattr(self, "album_candidates", []))
            dump("text_hints.json", getattr(self, "text_hints", []))
        except Exception as e:  # never fail a plan over side files
            log.warning("side files not written: %s", e)

    def _plan(self, vi: VideoInfo, audio: Path, part_files: list[Path]) -> dict:
        ainfo = audio_info(audio)
        duration = sum(audio_info(p)["duration"] for p in part_files) if part_files else ainfo["duration"]
        self.duration = duration
        guess = self._guess(vi)
        self.guess = guess
        hint_lists = self._hint_lists(vi, duration)
        user_list = parse_tracklist_file(Path(self.o.tracklist).read_text(encoding="utf-8")) \
            if self.o.tracklist else []
        self.text_hints = [{"source": h.source, "detail": h.detail, "by_uploader": h.by_uploader,
                            "pinned": h.pinned, "entries": [e.__dict__ for e in h.entries]} for h in hint_lists]
        if user_list:
            self.text_hints.insert(0, {"source": "tracklist", "detail": "--tracklist file", "by_uploader": True,
                                       "pinned": False, "entries": [e.__dict__ for e in user_list]})

        if part_files:
            kind = "multipart"
            self.kind_reason = f"{len(part_files)} parts (分P); one track per part"
            segs, album = self._multipart(vi, part_files, guess, user_list)
        else:
            kind, segs, album = self._single_or_compilation(vi, audio, duration, guess, hint_lists, user_list)

        return self._assemble(vi, audio, ainfo, part_files, kind, segs, album, duration)

    # ------------------------------------------------------------------ classification

    def _single_or_compilation(self, vi, audio, duration, guess, hint_lists, user_list):
        timed_user = [e for e in user_list if e.start >= 0]
        if len(user_list) >= 2:
            self.kind_reason = f"--tracklist gives {len(user_list)} entries"
            return ("compilation", *self._compilation(audio, duration, guess, hint_lists, user_list))
        if len(user_list) == 1:
            self.kind_reason = "--tracklist gives one entry"
            return ("single", *self._single(vi, audio, duration, guess, user_list[0]))
        primary = hint_lists[0] if hint_lists else None
        if primary:
            self.kind_reason = f"{primary.detail} lists {len(primary.entries)} timestamped tracks"
            return ("compilation", *self._compilation(audio, duration, guess, hint_lists, timed_user))
        if duration <= SINGLE_MAX:
            self.kind_reason = f"duration {fmt_ts(duration)} <= {fmt_ts(SINGLE_MAX)} and no tracklist hints"
            return ("single", *self._single(vi, audio, duration, guess, None))
        # Long video, no hints: the catalog and recognition decide.
        matches = self.timer.run("catalog", self.catalog.find_album, guess.queries(), duration=duration,
                                 artists=guess.artists, works=guess.works)
        self._album_matches = matches
        best = matches[0] if matches else None
        if best and len(best.album.tracks) >= 2 and best.score >= 0.6 and best.duration_score >= 0.4:
            self.kind_reason = (f"long video ({fmt_ts(duration)}) without hints; catalog album fits: "
                                f"{best.reason}")
            return ("compilation", *self._compilation(audio, duration, guess, [], []))
        probes = [self.rec.recognize(audio, duration * f, 12) for f in (0.2, 0.5, 0.8)]
        self._probes = [p for p in probes if p.ok]
        keys = {norm(r.title) for r in probes if r.ok}
        if len(keys) == 1 and sum(r.ok for r in probes) >= 2:
            self.kind_reason = (f"long video ({fmt_ts(duration)}) but recognition at 20/50/80% agrees on one "
                                f"song: {probes[1].title or probes[0].title}")
            return ("single", *self._single(vi, audio, duration, guess, None, probes=probes))
        self.kind_reason = (f"long video ({fmt_ts(duration)}) without hints; recognition found "
                            f"{len(keys)} different songs at 20/50/80%")
        return ("compilation", *self._compilation(audio, duration, guess, [], []))

    # ------------------------------------------------------------------ single

    def _single(self, vi, audio, duration, guess: TitleGuess, user: HintEntry | None, probes=None):
        seg = Seg(0.0, duration, start_kind="start")
        title_guess = user.title if user else (guess.works[0] if guess.works else "")
        artist_guess = user.artist if user else (guess.artists[0] if guess.artists else "")
        if artist_guess and not name_like(artist_guess):
            seg.evidence.append(ev("catalog_duration", f"artist guess {artist_guess!r} from the title does not "
                                   "look like a name; not used"))
            artist_guess = ""
        cues = [] if user else derivative_cues(vi.title)
        if cues:
            self.derivative = {"cues": cues}
        if user:
            seg.evidence.append(ev("catalog_duration", f"--tracklist entry: {user.title}"))
        r = None
        if probes:
            r = next((p for p in probes if p.ok), None)
        elif self.rec.available:
            calls0 = self.rec.calls
            r = self.rec.recognize(audio, max(0.0, duration / 2 - 6), 12)
            if not r.ok and duration > 60:
                r = self.rec.recognize(audio, max(0.0, duration * 0.3), 12)
            self.recog_info.update(policy="single", tracks_checked=1, requests=self.rec.calls - calls0,
                                   reason="one clip from the middle (a second at 30% if the first is unknown)")
        else:
            self.recog_info.update(reason="recognition off or unavailable")
        cands: list[tuple[float, CatTrack, CatAlbum, str]] = []
        if r and r.ok:
            seg.recog = r
            seg.evidence.append(ev("recognition", f"Shazam: {r.title} - {r.artist}"
                                   + (f" ({r.album})" if r.album else "")))
            for sc, t, al in self.timer.run("catalog", self.catalog.find_track, r.title, r.artist, duration):
                cands.append((sc, t, al, "recognition"))
        if title_guess:
            hits = self.timer.run("catalog", self.catalog.find_track, title_guess, artist_guess, duration)
            if artist_guess and not any(similarity(title_guess, t.title) >= 0.8 for _, t, _ in hits[:8]):
                # A wrong artist guess can hide the song: also search the title alone.
                hits += self.timer.run("catalog", self.catalog.find_track, title_guess, "", duration)
            cands += [(sc, t, al, "title") for sc, t, al in hits]
        pool = [(t, al) for _, t, al, _ in cands]
        self.album_candidates = [
            {"score": round(sc, 3), "source": src, "provider": t.provider, "track_title": t.title,
             "track_artist": t.artist, "duration": t.duration, "album": al.title, "album_artist": al.artist,
             "release_date": al.release_date, "id": al.id} for sc, t, al, src in cands[:30]]
        if cues and (title_guess or (r and r.ok)):
            return self._single_derivative(seg, title_guess or r.title, artist_guess, cues, pool, r, duration)
        best = None
        best_score = 0.0
        for sc0, t, al, src in cands[:8] + [c for c in cands[8:] if c[3] == "title"][:8]:
            sc = sc0
            if r and r.ok:
                sc += 0.25 * similarity(r.title, t.title) + 0.15 * similarity(r.artist, t.artist)
            if title_guess:
                sc += 0.2 * similarity(title_guess, t.title)
            if artist_guess:
                sc += 0.1 * similarity(artist_guess, t.artist)
            if t.duration:
                sc += 0.2 * math.exp(-abs(t.duration - duration) / 5.0)
            if sc > best_score:
                best, best_score = (t, al), sc
        rec_agrees = bool(r and r.ok and title_guess and similarity(r.title, title_guess) >= 0.8)
        conf = 0.4
        if best:
            t, al = best
            dur_ok = t.duration and abs(t.duration - duration) <= max(4.0, 0.03 * duration)
            title_ok = (title_guess and similarity(title_guess, t.title) >= 0.8) or \
                       (r and r.ok and similarity(r.title, t.title) >= 0.8)
            if title_ok and (dur_ok or rec_agrees or (r and r.ok and similarity(r.artist, t.artist) >= 0.6)):
                rec_ok = bool(r and r.ok and similarity(r.title, t.title) >= 0.8)
                artist_ok = rec_ok or bool(artist_guess and similarity(artist_guess, t.artist) >= 0.6)
                conf = 0.55 + (0.2 if dur_ok else 0) + (0.2 if rec_ok else 0)
                seg.evidence.append(ev("catalog_duration",
                                       f"{t.provider}: {t.artist} - {t.title} ({t.duration:.1f}s vs {duration:.1f}s)"
                                       f" from album {al.title}"))
                t, al = self._preferred_release(seg, t, al, pool)
                seg.cat = t
                seg.title, seg.artist = t.title, t.artist
                if not artist_ok:
                    # Title + length alone cannot tell a cover / fan version from the original.
                    conf = min(conf, 0.5)
                    seg.evidence.append(ev("catalog_duration", "artist not confirmed: the video title names no "
                                           "artist and recognition " + ("disagrees" if r and r.ok else "found nothing")))
                    self.review.append(
                        f"track 1: matched {t.artist} - {t.title} on title and length only; the video title names "
                        "no artist and recognition " + ("disagrees" if r and r.ok else "found nothing")
                        + ", so this may be a different version (cover, fan edit, AI remake)")
                self._single_album = al
            else:
                seg.evidence.append(ev("catalog_duration", f"best catalog candidate rejected: {t.artist} - "
                                       f"{t.title} ({t.duration:.1f}s vs {duration:.1f}s)"))
        if not seg.title:
            if r and r.ok and (not title_guess or rec_agrees):
                seg.title, seg.artist = r.title, r.artist
                conf = 0.6
            else:
                seg.title = title_guess or vi.title
                seg.artist = artist_guess or (r.artist if r and r.ok and rec_agrees else "")
                conf = 0.35
                self.review.append(f"track 1: no confident catalog match; title {seg.title!r} is the guess from "
                                   "the video title" + (f" (Shazam heard {r.title!r} by {r.artist!r})"
                                                        if r and r.ok else " (recognition found nothing)"))
        if r and r.ok and seg.title and similarity(r.title, seg.title) < 0.6:
            conf = min(conf, 0.45)
            self.review.append(f"track 1: Shazam heard {r.title!r} by {r.artist!r}, plan says {seg.title!r}")
        seg.confidence = conf
        album = getattr(self, "_single_album", None)
        if album and album.id and not album.tracks:
            album = self.timer.run("catalog", self.catalog.album_details, album)
        return [seg], (album if album and album.title else None)

    def _preferred_release(self, seg: Seg, t: CatTrack, al: CatAlbum,
                           pool: list[tuple[CatTrack, CatAlbum]]) -> tuple[CatTrack, CatAlbum]:
        """File a song under the artist's own original album: among catalog releases of the
        same recording (same title and artist, similar length), the artist's own albums come
        first, then singles / EPs named after the song, then compilations (various artists,
        best-ofs, soundtracks, playlists); the earliest release wins within a group. Only the
        top few are fetched to confirm the album artist. A compilation is used only when
        nothing else exists, and then flagged for review."""
        same = [(tt, aa) for tt, aa in pool
                if aa.title and aa.id and not DERIVED_TRACK.search(tt.title)
                and similarity(strip_decorations(tt.title), strip_decorations(t.title)) >= 0.9
                and similarity(tt.artist, t.artist) >= 0.7
                and (not tt.duration or not t.duration or abs(tt.duration - t.duration) <= 10)]
        if not any(tt is t for tt, _ in same):
            same.append((t, al))
        ranked = rank_releases(same)
        fetched = 0
        seen: set[tuple[str, str]] = set()
        for kind, tt, aa in ranked:
            if kind == "compilation" or fetched >= 4:
                break
            if (aa.provider, aa.id) in seen:
                continue
            seen.add((aa.provider, aa.id))
            full = self.timer.run("catalog", self.catalog.album_details, aa) if aa.id else aa
            fetched += 1
            if full.tracks:
                kind = release_kind(full, tt)
                ft = next((x for x in full.tracks
                           if similarity(strip_decorations(x.title), strip_decorations(tt.title)) >= 0.9), None)
                if kind == "compilation" or ft is None:
                    continue
                if not ft.artist:
                    ft.artist = tt.artist
                tt = ft
            if (tt.provider, tt.id) != (t.provider, t.id) or full.title != al.title:
                seg.evidence.append(ev("catalog_duration", f"release: {full.artist} - {full.title} "
                                       f"({full.release_date or full.year or 'undated'}, {kind}) chosen over "
                                       f"{al.artist} - {al.title} ({al.release_date or al.year or 'undated'}): "
                                       "the artist's own earliest album with this song"))
            return tt, full
        if ranked:
            kind, tt, aa = ranked[0]
            full = self.timer.run("catalog", self.catalog.album_details, aa) if aa.id else aa
            self.review.append(f"album: {full.artist} - {full.title} is a compilation; the artist's own album "
                               f"with {t.title!r} was not found in the catalogs")
            return tt, full
        return t, al

    def _single_derivative(self, seg: Seg, title: str, artist: str, cues: list[str],
                           pool: list[tuple[CatTrack, CatAlbum]], r: Recognition | None, duration: float):
        """The upload is a derivative (cover, fan edit, AI remake, rendition, remix): tag it with
        the ORIGINAL song. Among catalog songs with the same title (minus decorations) that are
        not themselves marked as covers / remixes / instrumentals, the earliest release is taken
        as the original; the video's length is not evidence. Always flagged for review."""
        cue_txt = ", ".join(cues)
        family = [(t, al) for t, al in pool if _title_core(t.title) == _title_core(title)]
        same = [(t, al) for t, al in family
                if not DERIVED_TRACK.search(t.title) and not DERIVED_TRACK.search(al.title or "")
                and (not artist or similarity(artist, t.artist) >= 0.6)]
        dated, why = pick_original(same, family)
        seg.confidence = 0.45
        self.derivative.update(title=title)
        if dated:
            t, al = dated[0]
            date = al.release_date or al.year or "undated"
            seg.cat = t
            seg.title, seg.artist = t.title, t.artist
            others = sorted({x.artist for x, _ in same if x.artist and norm(x.artist) != norm(t.artist)})
            seg.evidence.append(ev("catalog_duration", f"derivative ({cue_txt}): original taken as {t.provider}: "
                                   f"{t.artist} - {t.title} ({date}): {why}"
                                   + (f"; same title also by {', '.join(others[:5])}" if others else "")))
            self.derivative.update(original_artist=t.artist, original_title=t.title, original_release=date,
                                   original_rule=why)
            self.review.append(f"track 1: the video looks like a derivative version (cue: {cue_txt}); tagged with "
                               f"the original song {t.artist} - {t.title} ({date}; {why}); confirm the original "
                               "artist")
            self._single_album = al
        else:
            seg.title = title
            seg.artist = artist or (r.artist if r and r.ok else "")
            seg.confidence = 0.35
            seg.evidence.append(ev("catalog_duration", f"derivative ({cue_txt}): no believable original in the "
                                   "catalogs"))
            self.review.append(f"track 1: the video looks like a derivative version (cue: {cue_txt}); no catalog "
                               f"song titled {title!r} looks like the original, so title and artist are a best "
                               "guess")
        if r and r.ok and seg.title and similarity(r.title, seg.title) < 0.6:
            self.review.append(f"track 1: Shazam heard {r.title!r} by {r.artist!r}, plan says {seg.title!r}")
        album = getattr(self, "_single_album", None)
        if album and album.id and not album.tracks:
            album = self.timer.run("catalog", self.catalog.album_details, album)
        return [seg], (album if album and album.title else None)

    # ------------------------------------------------------------------ compilation

    def _analyze(self, audio: Path):
        feats, cands = self.timer.run("analyze", an.analyze, audio, self.workdir)
        self.feats, self.cands = feats, cands
        return feats, cands

    def _compilation(self, audio: Path, duration: float, guess: TitleGuess, hint_lists: list[HintList],
                     user_list: list[HintEntry]):
        feats, cands = self._analyze(audio)
        primary: HintList | None = None
        if user_list:
            primary = HintList("tracklist", "--tracklist file", user_list, by_uploader=True)
        elif hint_lists:
            primary = hint_lists[0]
        titles = [e.title for e in primary.entries] if primary else []
        if not titles and self.o.text_hints:
            titles = names_in_prose(self.vi.description)
        n_hint = len(primary.entries) if primary else None
        matches = getattr(self, "_album_matches", None)
        if matches is None or titles:
            matches = self.timer.run("catalog", self.catalog.find_album, guess.queries(), duration=duration,
                                     artists=guess.artists, works=guess.works, titles=titles, n_tracks=n_hint)
        self._record_candidates(matches)
        best: AlbumMatch | None = matches[0] if matches else None
        album = None
        if best and best.score >= 0.55 and (best.list_score is None or best.list_score >= 0.3 or not primary):
            album = best.album
            self.album_reason = best.reason
            if len(matches) > 1 and matches[1].score > best.score - 0.03 and \
                    norm(matches[1].album.title) != norm(album.title):
                self.review.append(f"album: close second candidate {matches[1].reason}")
        else:
            if best:
                self.review.append(f"album: best catalog candidate rejected ({best.reason}, score {best.score:.2f})")
            else:
                self.review.append("album: no catalog candidate found for " + ", ".join(guess.queries()[:3]))
        self.album_match = best if album else None
        if album:
            for note in self.timer.run("catalog", self.catalog.original_titles, album):
                self.title_notes = getattr(self, "title_notes", []) + [note]
        self._align_mark = len(self.review)
        self._from_catalog_alignment = False

        if primary and primary.has_times and all(e.start >= 0 for e in primary.entries):
            segs = self._from_timestamps(primary, album, duration, cands)
        elif primary and album:
            segs = self._from_titles_and_album(primary, album, duration, cands)
        elif album:
            segs = self._from_album(album, duration, cands)
            self._from_catalog_alignment = True
        else:
            segs = self._recognition_scan(audio, duration, cands)
        segs = self._refine_with_recognition(audio, segs, cands, album)
        if primary and primary.has_times:
            self._check_durations(segs)
        self._fill_from_recognition(segs)
        self._refine_cuts(audio, segs)
        return segs, album

    def _refine_cuts(self, audio: Path, segs: list[Seg]) -> None:
        """Refine every start that is also the previous track's end (see :mod:`refine`).

        Track 1 and starts after unexported material keep their place. A start pinned by
        recognition may still move earlier, but never later than the recognised start minus
        ``CUT_MARGIN`` (or its current place, if that is already later: digital silence)."""
        info = self.refine_info
        if not self.o.refine:
            info.update(reason="refinement off")
            return
        idx = [i for i in range(1, len(segs)) if abs(segs[i - 1].end - segs[i].start) < 1e-6]
        if not idx:
            info.update(reason="no boundary between adjacent tracks")
            return
        bounds = []
        for i in idx:
            s, hi = segs[i], None
            imp = s.recog.implied_start if getattr(s, "pinned", False) and s.recog is not None else None
            if imp is not None:
                hi = max(s.start, imp - CUT_MARGIN)
            bounds.append((segs[i - 1].start + 1.0, hi))
        try:
            res = self.timer.run("refine", refine_starts, audio, [segs[i].start for i in idx],
                                 channels=audio_info(audio)["channels"] or 2, bounds=bounds,
                                 **self.o.refine_options)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log.warning("refinement skipped: %s", e)
            info.update(reason=f"refinement failed: {e}")
            return
        methods: dict[str, int] = {}
        moved = 0
        for i, (new, method) in zip(idx, res):
            s, prev = segs[i], segs[i - 1]
            methods[method] = methods.get(method, 0) + 1
            delta = new - s.start
            if method == "fallback":
                s.evidence.append(ev("refine", f"start {s.start:.3f}s kept: no clear edge within the search window"))
                continue
            if abs(delta) < 0.0005:
                s.evidence.append(ev("refine", f"start {s.start:.3f}s confirmed ({method})"))
                continue
            s.evidence.append(ev("refine", f"start {s.start:.3f}s -> {new:.3f}s ({method}, {delta * 1000:+.0f} ms)"))
            prev.end = new
            s.start = new
            moved += 1
        info.update(boundaries=len(idx), moved=moved, methods=methods,
                    reason=f"{moved} of {len(idx)} boundaries moved")

    def _match_titles(self, titles: list[str], album: CatAlbum | None) -> list[CatTrack | None]:
        if not album:
            return [None] * len(titles)
        used: set[int] = set()
        out: list[CatTrack | None] = []
        for i, t in enumerate(titles):
            scores = [(similarity(t, ct.title), j) for j, ct in enumerate(album.tracks) if j not in used]
            if not scores:
                out.append(None)
                continue
            s, j = max(scores)
            # Same position is a tie-breaker and a fallback when names are noisy.
            if i < len(album.tracks) and i not in used:
                si = similarity(t, album.tracks[i].title)
                if si >= s - 0.05:
                    s, j = si, i
            if s >= 0.6:
                used.add(j)
                out.append(album.tracks[j])
            else:
                out.append(None)
        return out

    def _from_timestamps(self, hl: HintList, album, duration, cands) -> list[Seg]:
        ents = hl.entries
        cats = self._match_titles([e.title for e in ents], album)
        segs: list[Seg] = []
        base_conf = 0.75 if hl.by_uploader or hl.source in ("chapter", "tracklist") else 0.65
        for i, e in enumerate(ents):
            raw = e.start
            if i == 0 and raw <= 3.0:
                t, c = 0.0, None
                kind = "start"
            else:
                t, c = snap(raw, cands, 3.0)
                kind = c.kind if c else "hint"
            s = Seg(t, 0.0, title=e.title, artist=e.artist, cat=cats[i], confidence=base_conf, start_kind=kind)
            s.evidence.append(ev(hl.source if hl.source != "tracklist" else "description",
                                 f"{hl.detail}: {fmt_ts(raw)} {e.title}"))
            if c:
                s.evidence.append(ev(c.kind, f"start snapped {raw:.2f}s -> {t:.2f}s ({c.kind}, strength {c.strength:.2f})"))
            segs.append(s)
        for i, s in enumerate(segs):
            nxt = segs[i + 1].start if i + 1 < len(segs) else None
            e = ents[i]
            if e.end is not None and (nxt is None or e.end < nxt - 1):
                s.end = snap(e.end, cands, 3.0)[0]
            else:
                s.end = nxt if nxt is not None else duration
        # Last track: drop an unusually long tail only if the catalog says so.
        last = segs[-1]
        if last.cat and last.end - last.start > last.cat.duration + 30 and ents[-1].end is None:
            target = last.start + last.cat.duration
            t, c = snap(target, cands, 10.0)
            if c and c.kind == "silence":
                last.end = t
                last.evidence.append(ev("silence", f"outro trimmed at {t:.2f}s (catalog length {last.cat.duration:.1f}s)"))
        self._check_durations(segs)
        return segs

    def _check_durations(self, segs: list[Seg]) -> None:
        """Compare each track's length with its catalog length. Idempotent: re-running after
        boundaries moved replaces the earlier verdict."""
        for s in segs:
            s.confidence -= getattr(s, "len_adj", 0.0)
            s.evidence = [e for e in s.evidence if not e.get("_len")]
            adj = 0.0
            if not s.cat:
                if s.title:
                    adj = -0.1
                    s.evidence.append({**ev("catalog_duration", "no catalog track matched this title"), "_len": 1})
            else:
                L = s.end - s.start
                dev = L - s.cat.duration
                tol = max(6.0, 0.04 * s.cat.duration)
                if abs(dev) <= tol:
                    adj = 0.15
                    msg = f"length {L:.1f}s vs catalog {s.cat.duration:.1f}s ({dev:+.1f}s)"
                else:
                    adj = -0.2
                    msg = f"length {L:.1f}s disagrees with catalog {s.cat.duration:.1f}s ({dev:+.1f}s)"
                s.evidence.append({**ev("catalog_duration", msg), "_len": 1})
            s.len_adj = adj  # type: ignore[attr-defined]
            s.confidence += adj

    def _from_titles_and_album(self, hl: HintList, album: CatAlbum, duration, cands) -> list[Seg]:
        cats = self._match_titles([e.title for e in hl.entries], album)
        sub = CatAlbum(**{**album.__dict__, "tracks": [c for c in cats if c]})
        if len(sub.tracks) < len(hl.entries):
            self.review.append("tracklist: some titles have no catalog duration; aligned the matched ones only")
        return self._from_album(sub, duration, cands, source=hl.detail)

    def _from_album(self, album: CatAlbum, duration, cands, source: str = "") -> list[Seg]:
        exp = [t.duration for t in album.tracks]
        a_tight = align(exp, cands, duration)
        a_loose = align(exp, cands, duration, loose=True)
        a = a_tight if a_tight.mean_abs_dev <= a_loose.mean_abs_dev + 1.0 else a_loose
        self._align_missing = list(a.missing)
        by_time = {round(c.time, 3): c for c in cands}
        segs: list[Seg] = []
        for sg in a.segments:
            if sg.expected_index is None:
                s = Seg(sg.start, sg.end, confidence=0.3, start_kind=sg.start_point.kind)
                s.evidence.append(ev("catalog_duration", "material between catalog tracks with no catalog match"))
                self.review.append(f"segment {fmt_ts(sg.start)}-{fmt_ts(sg.end)} is not in the catalog album "
                                   "(bonus track, skit or ad?)")
            else:
                ct = album.tracks[sg.expected_index]
                s = Seg(sg.start, sg.end, title=ct.title, artist=ct.artist, cat=ct, confidence=0.55,
                        start_kind=sg.start_point.kind)
                dev = sg.deviation or 0.0
                tol = max(6.0, 0.04 * ct.duration)
                s.evidence.append(ev("catalog_duration", f"aligned catalog length {ct.duration:.1f}s to "
                                     f"{sg.end - sg.start:.1f}s ({dev:+.1f}s)" + (f"; titles from {source}" if source else "")))
                if abs(dev) <= tol:
                    s.confidence += 0.15
                else:
                    s.confidence -= 0.15
                    s.length_mismatch = dev  # type: ignore[attr-defined]
            s.start_strength = sg.start_point.strength  # type: ignore[attr-defined]
            for p, which in ((sg.start_point, "start"), (sg.end_point, "end")):
                if p.kind in ("silence", "dip", "novelty"):
                    c = by_time.get(round(p.time, 3))
                    extra = ""
                    if c is not None and c.kind == "silence":
                        extra = ("; digital silence" if c.digital else
                                 f"; quiet gap {c.gap_start:.1f}-{c.gap_end:.1f}s, floor {c.floor_db:.0f} dB, cut at "
                                 "the end of its deepest part")
                    s.evidence.append(ev(p.kind, f"{which} at {p.time:.2f}s ({p.kind}, strength {p.strength:.2f}"
                                         f"{extra})"))
            if sg.start_point.kind == "silence":
                s.confidence += 0.1
            elif sg.start_point.kind == "grid":
                s.confidence -= 0.1
            segs.append(s)
        for idx in a.missing:
            self.review.append(f"catalog track {album.tracks[idx].number} {album.tracks[idx].title!r} "
                               "was not found in the audio")
        return segs

    # ------------------------------------------------------------------ recognition

    def _refine_with_recognition(self, audio: Path, segs: list[Seg], cands, album) -> list[Seg]:
        """Check tracks with Shazam, under a policy that keeps requests low:

        - text hints on: only tracks with confidence < 0.8 (uploader timing is usually right);
        - catalog alignment clean (every catalog track placed, lengths within tolerance, total
          fits): a sample of 3 tracks from the first, middle and last third, plus any track whose
          start rests on a weak signal; every track only if a sample disagrees;
        - otherwise every track.

        After a full check, recognition that consistently contradicts the catalog order or
        presence re-labels the album in heard order and re-aligns (see _reorder_by_recognition).
        """
        info = self.recog_info
        if not self.rec.available:
            for s in segs:
                if s.confidence < 0.8:
                    s.evidence.append(ev("recognition", "recognition unavailable"))
            info.update(policy="none", reason="recognition off or unavailable")
            self._flag_weak_starts(segs)
            return segs
        calls0 = self.rec.calls
        extra: list[int] = []
        if self.o.text_hints:
            todo = [i for i, s in enumerate(segs) if s.confidence < 0.8]
            policy, why = "unsure", "text hints on: only tracks with confidence below 0.8"
        else:
            weak = self._alignment_weakness(segs, album)
            if weak or len(segs) <= 3:
                todo, policy = list(range(len(segs))), "full"
                why = "alignment not clean: " + "; ".join(weak) if weak else "3 tracks or fewer"
            else:
                todo = sample_indices(len(segs))
                extra = [i for i in self._weak_start_indices(segs) if i not in todo]
                policy = "sampled"
                why = (f"clean catalog alignment: sampled tracks {', '.join(str(i + 1) for i in todo)} "
                       "(first, middle and last third)"
                       + (f", plus weak starts {', '.join(str(i + 1) for i in extra)}" if extra else ""))
                todo = sorted(set(todo) | set(extra))
        verdict: dict[int, str] = {}
        for n, i in enumerate(todo):
            left = len(todo) - n - 1
            retry = policy != "sampled" or (self.rec.calls - calls0) + left + 2 <= SAMPLE_REQUESTS + len(extra)
            verdict[i] = self._recognize_seg(audio, segs[i], album, retry=retry)
        if policy == "sampled" and any(v == "disagree" for v in verdict.values()):
            bad = [i + 1 for i, v in verdict.items() if v == "disagree"]
            policy = "full"
            why += f"; sampled track(s) {', '.join(map(str, bad))} disagreed, so every track was checked"
            for i in range(len(segs)):
                if i not in verdict:
                    verdict[i] = self._recognize_seg(audio, segs[i], album, retry=True)
        if album and self._from_catalog_alignment and policy == "full":
            new = self._reorder_by_recognition(audio, segs, album, cands)
            if new is not None:
                segs = new
                why += "; recognition contradicted the catalog order, so tracks were re-labelled and re-aligned"
        for i, s in enumerate(segs):
            self._apply_recognition(i, segs, cands, album)
        self._flag_weak_starts(segs)
        info.update(policy=policy, reason=why, requests=self.rec.calls - calls0,
                    tracks_checked=sum(1 for s in segs if s.recog is not None),
                    checked=[i + 1 for i, s in enumerate(segs) if s.recog is not None])
        return segs

    def _alignment_weakness(self, segs: list[Seg], album) -> list[str]:
        out = []
        if not album:
            out.append("no catalog album")
            return out
        m = getattr(self, "album_match", None)
        if m is not None and m.duration_score < 0.6:
            out.append(f"catalog total length fits poorly (duration score {m.duration_score:.2f})")
        if self._align_missing:
            out.append(f"{len(self._align_missing)} catalog track(s) not placed")
        if any(s.cat is None for s in segs):
            out.append("material without a catalog track")
        mism = [i + 1 for i, s in enumerate(segs) if getattr(s, "length_mismatch", None) is not None]
        if mism:
            out.append(f"length off the catalog for track(s) {', '.join(map(str, mism))}")
        return out

    @staticmethod
    def _weak_start_indices(segs: list[Seg]) -> list[int]:
        return [i for i, s in enumerate(segs) if i > 0 and (
            s.start_kind == "grid" or
            (s.start_kind in ("dip", "novelty") and getattr(s, "start_strength", 1.0) < WEAK_STRENGTH))]

    def _flag_weak_starts(self, segs: list[Seg]) -> None:
        """A start resting on a single weak signal (only the 1 s grid, or a faint dip / novelty
        peak) and not pinned by a recognition offset gets lower confidence and a review line."""
        for i in self._weak_start_indices(segs):
            s = segs[i]
            if getattr(s, "pinned", False):
                continue
            s.confidence -= 0.2
            what = "no boundary candidate (1 s grid)" if s.start_kind == "grid" else \
                f"a faint {s.start_kind} (strength {getattr(s, 'start_strength', 0):.2f})"
            s.evidence.append(ev("catalog_duration", f"weak start: only {what}, no silence nearby and no "
                                 "recognition offset"))
            self.review.append(f"track {i + 1}: start {fmt_ts(s.start)} rests only on {what}; check it")

    def _recognize_seg(self, audio: Path, s: Seg, album, retry: bool = True) -> str:
        """Recognise one segment; returns 'agree', 'disagree', 'unknown' or 'skipped'."""
        L = s.end - s.start
        if L < 8:
            return "skipped"
        # A clip already recognised well inside this segment (the 20/50/80% kind probes) counts.
        r = next((p for p in getattr(self, "_probes", [])
                  if s.start + 3 <= p.clip_start and p.clip_start + p.clip_len <= s.end - 3), None)
        if r is None:
            if not self.rec.available:
                return "skipped"
            clip = s.start + min(25.0, max(2.0, 0.3 * L))
            r = self.rec.recognize(audio, clip, 12)
        if not r.ok and L > 60 and retry and self.rec.available:
            r = self.rec.recognize(audio, s.start + L / 2 - 6, 12)
        s.recog = r
        if not r.ok:
            return "unknown"
        expected = s.cat.title if s.cat else s.title
        if not expected:
            return "unknown"
        return "agree" if self._same_song(r, expected, s.title, L, album, s.cat.artist if s.cat else s.artist) \
            else "disagree"

    def _apply_recognition(self, i: int, segs: list[Seg], cands, album) -> None:
        s = segs[i]
        r = s.recog
        if r is None:
            if not self.rec.available and s.confidence < 0.8:
                s.evidence.append(ev("recognition", "not checked (recognition unavailable or rate limited)"))
            return
        if not r.ok:
            s.evidence.append(ev("recognition", f"no match at {fmt_ts(r.clip_start)}"))
            return
        expected = s.cat.title if s.cat else s.title
        kind = self._match_kind(r, expected, s.title, s.end - s.start, album, s.cat.artist if s.cat else s.artist)
        detail = f"Shazam at {fmt_ts(r.clip_start)}: {r.title} - {r.artist}"
        if r.implied_start is not None:
            detail += f" (recording starts at {r.implied_start:.2f}s)"
        if kind == "translation":
            detail += "; agrees (translated or romanised title of the same recording)"
        elif kind:
            detail += "; agrees"
        else:
            detail += f"; expected {expected!r}"
        s.evidence.append(ev("recognition", detail))
        if expected and not kind:
            s.confidence -= 0.3
            self.review.append(f"track {i + 1}: Shazam heard {r.title!r} by {r.artist!r} at "
                               f"{fmt_ts(r.clip_start)}, plan says {expected!r}")
            return
        if not expected:
            return
        s.confidence += 0.2
        imp = r.implied_start
        if i == 0 or imp is None:
            return
        prev = segs[i - 1]
        if abs(imp - s.start) > 15 or imp <= prev.start + 10:
            return
        s.pinned = True  # type: ignore[attr-defined]
        new, note = place_cut(imp, cands, self.feats, lo=prev.start + 1)
        if abs(new - s.start) > 0.05:
            s.evidence.append(ev("recognition", f"start moved {s.start:.2f}s -> {new:.2f}s: {note}"))
            if abs(prev.end - s.start) < 1e-6:
                prev.end = new
            s.start = new
            s.start_kind = "recognition"
        else:
            s.evidence.append(ev("recognition", f"start {s.start:.2f}s confirmed: {note}"))

    def _heard_index(self, r: Recognition | None, album: CatAlbum) -> int | None:
        if not r or not r.ok:
            return None
        scores = [(similarity(r.title, t.title), j) for j, t in enumerate(album.tracks)]
        sc, j = max(scores) if scores else (0.0, None)
        return j if sc >= 0.7 else None

    def _reorder_by_recognition(self, audio: Path, segs: list[Seg], album: CatAlbum, cands) -> list[Seg] | None:
        """When recognition consistently contradicts the assumed order or presence of catalog
        tracks (two or more segments heard as another track of the same album, or one when a
        catalog track was left out), rebuild the album from what was heard:

        1. every recognised catalog track with its recording start (median of its offsets),
           sorted by that start;
        2. a stretch between two heard tracks (or after the last) that is more than 30 s longer
           than the earlier track's catalog length is probed once more, so a song whose
           segment never got its own clip is still found;
        3. catalog tracks never heard are dropped; the album is re-aligned with the catalog
           lengths in heard order plus the recognised starts as boundary candidates."""
        idx_of = {id(t): j for j, t in enumerate(album.tracks)}
        assigned = [idx_of.get(id(s.cat)) if s.cat else None for s in segs]
        heard = [self._heard_index(s.recog, album) for s in segs]
        contra = [i for i, (a, h) in enumerate(zip(assigned, heard)) if h is not None and h != a]
        if len(contra) < 2 and not (contra and self._align_missing):
            return None
        recs = [s.recog for s in segs if s.recog is not None and s.recog.ok]
        starts = self._heard_starts(recs, album)
        if len(starts) < max(2, (len(segs) + 1) // 2):
            return None
        for _ in range(4):                       # probe unexplained stretches
            seq = sorted(starts.items(), key=lambda kv: kv[1])
            gaps = []
            for k, (j, t0) in enumerate(seq):
                nxt = seq[k + 1][1] if k + 1 < len(seq) else self.duration
                end = t0 + album.tracks[j].duration
                if nxt - end > 30:
                    gaps.append(end)
            if not gaps or not self.rec.available:
                break
            found = False
            for g in gaps:
                r = self.rec.recognize(audio, g + 20, 12)
                if r.ok:
                    recs.append(r)
                    j = self._heard_index(r, album)
                    if j is not None and j not in starts and r.implied_start is not None:
                        starts[j] = r.implied_start
                        found = True
            if not found:
                break
        seq = sorted(starts.items(), key=lambda kv: kv[1])
        order = [album.tracks[j] for j, _ in seq]
        covered = sum(t.duration for t in order)
        if covered < 0.8 * (self.duration - seq[0][1]):
            self.review.append("recognition contradicts the catalog order but leaves part of the audio "
                               "unexplained; kept the catalog order")
            return None
        anchors = [Candidate(t0, 1.0, "recognition", t0, t0) for _, t0 in seq if 0.5 < t0 < self.duration - 1]
        sub = CatAlbum(**{**album.__dict__, "tracks": order})
        del self.review[self._align_mark:]
        new = self._from_album(sub, self.duration, sorted(cands + anchors, key=lambda c: c.time),
                               source="recognition order")
        for s in new:
            s.recog = None
        for r in recs:
            tgt = next((s for s in new if s.start <= r.clip_start < s.end), None)
            if tgt is not None and (tgt.recog is None or not tgt.recog.ok or
                                    (tgt.cat and similarity(r.title, tgt.cat.title) >
                                     similarity(tgt.recog.title, tgt.cat.title))):
                tgt.recog = r
        dropped = [t for j, t in enumerate(album.tracks) if j not in starts]
        self.reordered = True
        self.dropped_tracks = dropped  # type: ignore[attr-defined]
        self.review.append(f"track order follows recognition, not the catalog album ({album.provider}): heard "
                           f"{', '.join(t.title for t in order)}"
                           + (f"; catalog track(s) {', '.join(repr(t.title) for t in dropped)} not in the audio"
                              if dropped else "") + "; track numbers follow the video")
        return new

    def _heard_starts(self, recs: list[Recognition], album: CatAlbum) -> dict[int, float]:
        """Catalog track index -> median recognised recording start."""
        by: dict[int, list[float]] = {}
        for r in recs:
            j = self._heard_index(r, album)
            if j is not None and r.implied_start is not None:
                by.setdefault(j, []).append(r.implied_start)
        return {j: sorted(v)[len(v) // 2] for j, v in by.items()}

    def _same_song(self, r: Recognition, expected: str, alt: str, length: float, album=None,
                   artist: str = "") -> bool:
        return bool(self._match_kind(r, expected, alt, length, album, artist))

    def _match_kind(self, r: Recognition, expected: str, alt: str, length: float, album=None,
                    artist: str = "") -> str:
        """Does a recognition name the expected song? '' (no), 'title', 'catalog' (an alias the
        catalog knows) or 'translation' (same artist, the titles are in different scripts, and
        the recognised title names no other track of the album: a translated or romanised
        title of the same recording, not a disagreement)."""
        if not expected and not alt:
            return ""
        names = [x for x in (expected, alt) if x]
        if max(similarity(r.title, x) for x in names) >= 0.7:
            return "title"
        if album and any(similarity(r.title, t.title) >= 0.7 for t in album.tracks):
            return ""  # another track of this album: a real disagreement
        hits = self.timer.run("catalog", self.catalog.find_track, r.title, r.artist, length)
        if any(sc >= 0.55 and max(similarity(t.title, x) for x in names) >= 0.8 for sc, t, _ in hits[:5]):
            return "catalog"
        if artist and similarity(r.artist, artist) >= 0.6 and \
                latin_only(r.title) != latin_only(expected or alt):
            return "translation"
        return ""

    def _fill_from_recognition(self, segs: list[Seg]) -> None:
        """Unknown segments that Shazam named get catalog metadata from a track search."""
        for i, s in enumerate(segs, 1):
            if s.title or not (s.recog and s.recog.ok):
                continue
            r = s.recog
            s.title, s.artist = r.title, r.artist
            hits = self.timer.run("catalog", self.catalog.find_track, r.title, r.artist, s.end - s.start)
            if hits and hits[0][0] >= 0.6:
                sc, t, al = hits[0]
                s.cat = t
                s.title, s.artist = t.title, t.artist
                s.cat_album = al  # type: ignore[attr-defined]
                s.evidence.append(ev("catalog_duration", f"{t.provider}: {t.artist} - {t.title} "
                                     f"({t.duration:.1f}s) via recognition"))
            s.confidence = max(s.confidence, 0.55)

    def _recognition_scan(self, audio: Path, duration: float, cands) -> list[Seg]:
        if not self.rec.available:
            self.review.append("no album match and recognition unavailable: whole video kept as one track")
            return [Seg(0.0, duration, confidence=0.2, start_kind="start")]
        hop = 45.0 if duration <= 3600 else 60.0
        times = [min(duration - 13, 8.0 + k * hop) for k in range(int((duration - 8) // hop) + 1)]
        obs: list[Recognition] = [self.rec.recognize(audio, t, 12) for t in times if t >= 0]
        groups: list[list[Recognition]] = []
        for r in obs:
            if not r.ok:
                continue
            if groups and norm(groups[-1][-1].title) == norm(r.title):
                groups[-1].append(r)
            else:
                groups.append([r])
        if not groups:
            self.review.append("recognition scan found nothing; whole video kept as one track")
            return [Seg(0.0, duration, confidence=0.2, start_kind="start")]
        segs: list[Seg] = []
        for gi, g in enumerate(groups):
            imps = sorted(r.implied_start for r in g if r.implied_start is not None)
            est = imps[len(imps) // 2] if imps else g[0].clip_start
            prev_last = groups[gi - 1][-1].clip_start + 12 if gi else 0.0
            est = min(max(est, prev_last), g[0].clip_start)
            if gi == 0 and est < 5:
                start, kind = 0.0, "start"
            else:
                start, c = snap(est, cands, 2.0)
                kind = c.kind if c else "recognition"
            s = Seg(start, 0.0, confidence=0.55, start_kind=kind)
            s.recog = g[len(g) // 2]
            s.evidence.append(ev("recognition", f"{len(g)} scan window(s) agree on {g[0].title} - {g[0].artist}"))
            segs.append(s)
        for i, s in enumerate(segs):
            s.end = segs[i + 1].start if i + 1 < len(segs) else duration
        unknown = [r for r in obs if not r.ok]
        if len(unknown) > len(obs) * 0.3:
            self.review.append(f"recognition scan: {len(unknown)}/{len(obs)} windows unidentified")
        return segs

    # ------------------------------------------------------------------ multipart

    def _multipart(self, vi: VideoInfo, files: list[Path], guess: TitleGuess, user_list: list[HintEntry]):
        durs = [audio_info(f)["duration"] for f in files]
        if user_list and len(user_list) == len(files):
            titles = [e.title for e in user_list]
        elif self.o.text_hints:
            titles = [part_title(p.name, vi.title) for p in vi.parts]
        else:
            titles = []
        matches = self.timer.run("catalog", self.catalog.find_album, guess.queries(), duration=sum(durs),
                                 artists=guess.artists, works=guess.works, titles=titles, n_tracks=len(files))
        self._record_candidates(matches)
        best = matches[0] if matches else None
        album = best.album if best and best.score >= 0.55 else None
        if album:
            self.album_reason = best.reason
            self.album_match = best
        elif best:
            self.review.append(f"album: best catalog candidate rejected ({best.reason}, score {best.score:.2f})")
        else:
            self.review.append("album: no catalog candidate found")
        if titles:
            cats = self._match_titles(titles, album)
        elif album and len(album.tracks) == len(files):
            cats = list(album.tracks)
        elif album:
            cats = [min(album.tracks, key=lambda t: abs(t.duration - d)) for d in durs]
        else:
            cats = [None] * len(files)
        segs = []
        for i, (f, d) in enumerate(zip(files, durs)):
            s = Seg(0.0, d, title=titles[i] if titles else "", cat=cats[i], audio=f, confidence=0.7,
                    start_kind="part")
            if titles:
                s.evidence.append(ev("part", f"part {i + 1} name: {vi.parts[i].name if i < len(vi.parts) else titles[i]}"))
            if s.cat:
                s.title, s.artist = s.cat.title, s.cat.artist
            segs.append(s)
        self._check_durations(segs)
        for i, s in enumerate(segs):
            if s.confidence >= 0.8 and self.o.text_hints:
                continue
            if not self.rec.available:
                continue
            r = self.rec.recognize(s.audio, max(0.0, (s.end - s.start) / 2 - 6), 12)
            s.recog = r
            if not r.ok:
                s.evidence.append(ev("recognition", "no match"))
                continue
            expected = s.title
            agree = bool(expected) and self._same_song(r, expected, "", s.end - s.start, album,
                                                       s.cat.artist if s.cat else s.artist)
            s.evidence.append(ev("recognition", f"Shazam: {r.title} - {r.artist}" + ("; agrees" if agree else "")))
            if expected and not agree:
                s.confidence -= 0.3
                self.review.append(f"track {i + 1}: Shazam heard {r.title!r}, plan says {expected!r}")
            elif agree:
                s.confidence += 0.2
        self._fill_from_recognition(segs)
        return segs, album

    # ------------------------------------------------------------------ assembly

    def _cover(self, album: CatAlbum | None) -> str:
        dest = self.workdir / "cover.jpg"
        for p in (self.workdir / "cover.jpg", self.workdir / "cover.png"):
            if p.exists():
                return str(p)
        urls = []
        if album:
            extra = getattr(album, "extra_cover", "")
            for u in (album.cover_url if album.provider == "itunes" else "", extra, album.cover_url):
                if u:
                    if "music.126.net" in u and "param=" not in u:
                        u += "?param=1200y1200"
                    urls.append(u)
        for u in urls:
            try:
                data = self.timer.run("catalog", self.http.get_bytes, u)
            except Exception as e:
                log.info("cover %s failed: %s", u, e)
                continue
            tmp = self.workdir / "cover.download"
            tmp.write_bytes(data)
            kind = image_kind(tmp)
            if kind in ("jpg", "png"):
                final = self.workdir / f"cover.{kind}"
                tmp.replace(final)
                return str(final)
            tmp.unlink(missing_ok=True)
        bili = self.workdir / "bili_cover.jpg"
        if bili.exists():
            try:
                square_cover(bili, dest)
                self.review.append("cover: no catalog artwork; used a centre-cropped Bilibili cover")
                return str(dest)
            except Exception as e:
                log.warning("cover crop failed: %s", e)
        self.review.append("cover: no cover art available")
        return ""

    def _assemble(self, vi, audio, ainfo, part_files, kind, segs: list[Seg], album: CatAlbum | None,
                  duration: float) -> dict:
        if album:
            album = self.timer.run("catalog", self.catalog.enrich, album)
        guess = self.guess
        cover = self._cover(album)
        if album:
            alb_title, alb_artist = album.title, album.artist
        elif kind == "single":
            alb_title, alb_artist = "", ""
        else:
            alb_title = guess.works[0] if guess.works else vi.title
            artists = {norm(s.artist) for s in segs if s.artist}
            named = [a for a in guess.artists if name_like(a)]
            alb_artist = named[0] if named else (segs[0].artist if len(artists) == 1 else
                                                 ("Various Artists" if artists else ""))
        if kind == "single":
            s = segs[0]
            alb_title = alb_title or s.title
            alb_artist = alb_artist or s.artist
        year = (album.release_date or album.year) if album else (guess.year or "")
        genre = album.genre if album else ""
        tracks = []
        missing_req: dict[str, list[int]] = {}
        # Lyrics / composer from the catalog.
        for i, s in enumerate(segs, 1):
            lyr, comp = "", ""
            if s.cat:
                lyr, comp = self.timer.run("catalog", self.catalog.lyrics, s.cat)
            artist = s.artist or (s.cat.artist if s.cat else "")
            ev_list = list(s.evidence)
            conf = s.confidence
            if s.cat and s.cat.title:
                # Catalog spelling wins over uploader text (typos, punctuation, script).
                if s.title and norm(s.title) != norm(s.cat.title):
                    ev_list.append(ev("catalog_duration", f"title {s.title!r} written as the catalog's "
                                      f"{s.cat.title!r}"))
                s.title = s.cat.title
                artist = s.cat.artist or artist
            orig = strip_translation(s.title)
            if orig != s.title:
                ev_list.append(ev("catalog_duration", f"title {s.title!r} written as {orig!r}: the bracketed "
                                  "part is a translation; the original-script title is kept"))
                s.title = orig
            if not artist and alb_artist:
                artist = alb_artist
                conf -= 0.15
                ev_list.append(ev("catalog_duration", "no per-track artist from any provider; "
                                  "filled with the album artist"))
                self.review.append(f"track {i}: artist copied from album artist {alb_artist!r}; confirm")
            number = i
            disc = 1
            if self.reordered:
                pass  # heard order differs from the catalog: number in video order
            elif s.cat and album and s.cat in album.tracks:
                number = s.cat.number or i
                disc = s.cat.disc or 1
            elif kind == "single" and s.cat:
                number = s.cat.number or 1
                disc = s.cat.disc or 1
            t = {
                "index": i,
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "title": s.title,
                "artist": artist,
                "album": alb_title,
                "album_artist": alb_artist,
                "track_number": number,
                "disc_number": disc,
                "year": year,
                "genre": genre,
                "composer": comp,
                "lyrics": lyr,
                "cover_path": cover,
                "catalog": {"provider": s.cat.provider, "id": s.cat.id} if s.cat else {"provider": "", "id": ""},
                "confidence": round(max(0.0, min(1.0, conf)), 2),
                "evidence": [{k: v for k, v in e.items() if not k.startswith("_")} for e in ev_list],
            }
            if s.audio:
                t["audio_path"] = str(s.audio)
            for req in ("title", "artist", "album", "album_artist"):
                if not t[req]:
                    missing_req.setdefault(req, []).append(i)
            if t["confidence"] < LOW_CONF:
                self.review.append(f"track {i} {t['title']!r}: low confidence {t['confidence']:.2f}")
            tracks.append(t)
        for req, idx in missing_req.items():
            self.review.append(f"missing {req} for track(s) {', '.join(map(str, idx))}")
        # Track total / disc total.
        track_total = len(tracks)
        disc_total = 1
        if album and album.tracks:
            track_total = max(len(tracks), len([t for t in album.tracks if t.disc == 1]) if album.disc_total > 1
                              else len(album.tracks))
            disc_total = album.disc_total
        if kind == "single" and album and album.tracks:
            track_total = len(album.tracks)
        if self.reordered:
            track_total, disc_total = len(tracks), 1
        trim_head = round(tracks[0]["start"], 3) if tracks and not part_files else 0.0
        trim_tail = round(duration - tracks[-1]["end"], 3) if tracks and not part_files else 0.0
        if trim_head > REVIEW_TRIM:
            self.review.append(f"intro of {trim_head:.1f}s before track 1 is not exported")
        if trim_tail > REVIEW_TRIM:
            self.review.append(f"outro of {trim_tail:.1f}s after the last track is not exported")
        if kind == "compilation" and album and album.tracks:
            covered = tracks[-1]["end"] - tracks[0]["start"]
            cat_total = sum(s.cat.duration for s in segs if s.cat)
            if cat_total and abs(covered - cat_total) > max(15.0, 0.03 * cat_total):
                self.review.append(f"durations: tracks span {covered:.1f}s but the matched catalog tracks add up "
                                   f"to {cat_total:.1f}s")
        if kind == "compilation" and self.o.text_hints is False:
            pass
        cat_info = {"provider": album.provider, "id": album.id, "url": album.url} if album else \
            {"provider": "", "id": "", "url": ""}
        if self.rec.exhausted:
            self.review.append("recognition: Shazam rate-limited this run; some low-confidence tracks were not "
                               "verified (re-plan later to fill them in from the cache)")
        if self.catalog.errors and not album:
            self.review.append("catalog errors: " + "; ".join(sorted(set(self.catalog.errors))[:3]))
        plan = {
            "version": 1,
            "source": {
                "url": vi.url, "bvid": vi.bvid, "title": vi.title, "uploader": vi.uploader,
                "duration": round(duration, 3),
                "audio_path": "" if part_files else str(audio),
                "audio_format": {"codec": ainfo["codec"], "bitrate_kbps": ainfo["bitrate_kbps"],
                                 "sample_rate": ainfo["sample_rate"]},
                "cover_path": str(self.workdir / "bili_cover.jpg") if (self.workdir / "bili_cover.jpg").exists() else "",
                "parts": max(1, len(part_files)),
                "trim_head": trim_head,
                "trim_tail": trim_tail,
                "text_hints": self.o.text_hints,
                "derivative": bool(self.derivative),
            },
            "kind": kind,
            "kind_reason": self.kind_reason,
            "album": {"title": alb_title, "artist": alb_artist, "year": year, "genre": genre,
                      "track_total": track_total, "disc_total": disc_total, "cover_path": cover,
                      "catalog": cat_info,
                      "match_reason": getattr(self, "album_reason", "")},
            "tracks": tracks,
            "needs_review": bool(self.review),
            "review_reasons": _dedupe(self.review),
            "timings": self.timer.as_dict(),
            "recognition": self.recog_info,
            "refine": self.refine_info,
        }
        if self.derivative:
            plan["source"]["derivative_cues"] = self.derivative.get("cues", [])
            plan["source"]["derivative_info"] = self.derivative
        if getattr(getattr(self, "bili", None), "download_log", None):
            plan["source"]["download"] = self.bili.download_log
        if getattr(self, "title_notes", None):
            plan["album"]["title_notes"] = self.title_notes
        if part_files:
            plan["source"]["part_paths"] = [str(p) for p in part_files]
        doubts = simplify_plan(plan)
        if doubts:
            plan["needs_review"] = True
            plan["review_reasons"] = _dedupe(plan["review_reasons"] + doubts)
        return plan


def _dedupe(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def make_plan(url: str | None, opts: PlanOptions) -> Path:
    return Planner(opts).run(url)
