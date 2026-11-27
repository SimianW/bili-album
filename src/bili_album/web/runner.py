"""What the job manager needs from the processing pipeline, and two ways to provide it.

A runner works on one job at a time inside the job's work directory and reports
progress through ``emit``. Every keyword is optional; the job manager merges
what it gets into the job:

- ``stage`` + ``state``: one of :data:`STAGES` and ``running | done | held | failed``;
- ``message``: the live line under the job (short, Simplified Chinese);
- ``album``: ``{"title", "artist", "year", "kind", "uploader"}`` (``kind`` is ``single | album``; for a
  single, title and artist are the song's);
- ``tracks``: ``[{"index", "title", "artist", "duration", "flagged"}]``;
- ``review``: ``{"verdict", "reasons", "remaining_concerns", "edits": [{"tracks", "reason"}], "rejected": [str]}``
  (``reason`` / ``rejected`` are the review's Chinese ``reason_zh`` lines);
- ``result``: the import result ``{"album_url", "imported", "confirmed", "folder", "file_count",
  "existing_files"}``;
- ``cover``: path of the cover image, relative to the work directory.

Each call returns an :class:`Outcome`: the job is ``done``, waits for the owner
(``needs_confirmation`` or ``duplicate``) or ``failed`` (``error`` says why, in
words the owner can act on). An exception counts as a failure in the current stage.

``PipelineRunner`` drives :mod:`bili_album.pipeline`; ``fake.FakeRunner`` (``serve --fake``) is the
same class over the real pipeline with scripted stages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


STAGES = ("plan", "review", "cut", "import")
STAGE_NAMES = {"plan": "生成方案", "review": "Sol 复核", "cut": "切分写标签", "import": "导入 Jellyfin"}

Emit = Callable[..., None]


@dataclass
class Outcome:
    status: str  # done | needs_confirmation | duplicate | failed
    error: str = ""


class Runner(Protocol):
    def start(self, url: str, workdir: Path, emit: Emit, options: dict | None = None) -> Outcome:
        """Plan, review, cut and import (or resume after a failure: the work directory is reused).
        ``options`` is what :func:`clean_options` returned for the submitted job."""

    def confirm(self, workdir: Path, edits: list[dict], emit: Emit) -> Outcome:
        """Continue a held job from the cut stage; ``edits`` is ``[{"index", "title", "artist"}]``."""

    def replace(self, workdir: Path, emit: Emit) -> Outcome:
        """Continue a duplicate: import over the existing album."""

    def keep(self, workdir: Path) -> None:
        """End a duplicate without importing."""

    def discard(self, workdir: Path) -> None:
        """Drop a held job's output before its folder is removed."""

    def secrets(self) -> list[str]:
        """Strings that must never reach the browser (scrubbed from every job field)."""


# ----------------------------------------------------------------------------- job options

# Boundary refinement settings the page may send: name -> (type, lowest, highest). The names are
# keyword arguments of bili_album.refine.refine_starts; anything not listed here is refused.
REFINE_LIMITS: dict[str, tuple[type, float, float]] = {
    "search_ms": (int, 200, 5000),
    "silence_db": (float, -80.0, 0.0),
    "deep_silence_db": (float, -90.0, 0.0),
    "pre_roll_ms": (int, 0, 500),
}
_REFINE_ZH = {"search_ms": "搜索范围", "silence_db": "第一轮静音阈值", "deep_silence_db": "第二轮静音阈值",
              "pre_roll_ms": "切点前留白"}


def clean_options(raw: Any) -> dict:
    """Check the options sent with a new job: ``{"refine": bool, "search_ms": int, ...}``.

    Returns ``{"refine": bool, "refine_options": {...}}`` with only what was sent, or ``{}`` for
    nothing. Raises ``ValueError`` with a plain Chinese message for the page."""
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("选项格式不对。")
    unknown = set(raw) - {"refine"} - set(REFINE_LIMITS)
    if unknown:
        raise ValueError("不认识的选项：" + "、".join(sorted(map(str, unknown))) + "。")
    out: dict = {}
    if "refine" in raw:
        if not isinstance(raw["refine"], bool):
            raise ValueError("「精修切点」只能是开或关。")
        out["refine"] = raw["refine"]
    ro: dict = {}
    for name, (kind, lo, hi) in REFINE_LIMITS.items():
        if name not in raw:
            continue
        v = raw[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or (kind is int and v != int(v)):
            raise ValueError(f"「{_REFINE_ZH[name]}」要填数字。")
        if not lo <= v <= hi:
            raise ValueError(f"「{_REFINE_ZH[name]}」要在 {lo:g} 到 {hi:g} 之间。")
        ro[name] = kind(v)
    if ro.get("deep_silence_db", -65.0) > ro.get("silence_db", -42.0):
        raise ValueError("第二轮静音阈值不能高于第一轮。")
    if ro:
        out["refine_options"] = ro
    return out


def plan_options(options: dict) -> dict:
    """The pipeline's ``plan_options`` for a job's cleaned options."""
    return {k: v for k, v in options.items() if k in ("refine", "refine_options")}


# ----------------------------------------------------------------------------- pipeline runner

class PipelineRunner:
    """Runs :mod:`bili_album.pipeline` (commit a1b944e) in the job's work directory.

    ``ingest(url, workdir, on_event, on_unsure="hold", config=)``, ``confirm(workdir, on_event,
    replace=, config=)``, ``discard(workdir)``; events are dicts with ``stage``, ``status``
    (started / finished / failed) and ``message``; the result is an ``IngestResult``. All
    knowledge of those names lives here, in :func:`map_event`, :func:`map_result` and
    :func:`read_plan`. ``api`` replaces the pipeline module (the fake runner uses this).
    """

    def __init__(self, config=None, *, api=None):
        if config is None:
            from ..config import load_config
            config = load_config()
        self.config = config
        self._api_obj = api

    def secrets(self) -> list[str]:
        c = self.config
        # Everything from the configuration except the public Jellyfin address (sent as library_url).
        vals = list(c.secrets()) + [c.llm_base_url, c.llm_model, str(c.library_dir or ""), c.jellyfin_library_path]
        if c.jellyfin_url and c.jellyfin_url != c.jellyfin_public_url:
            vals.append(c.jellyfin_url)
        return [v for v in vals if v and len(v) >= 4]

    def _api(self):
        if self._api_obj is not None:
            return self._api_obj
        from .. import pipeline  # imported late
        return pipeline

    def start(self, url: str, workdir: Path, emit: Emit, options: dict | None = None) -> Outcome:
        kw = {"plan_options": plan_options(options)} if options else {}
        res = self._api().ingest(url, workdir, self._events(workdir, emit), on_unsure="hold", config=self.config,
                                 **kw)
        return self._finish(workdir, res, emit)

    def confirm(self, workdir: Path, edits: list[dict], emit: Emit) -> Outcome:
        apply_edits(plan_path(workdir), edits)
        res = self._api().confirm(workdir, self._events(workdir, emit), config=self.config)
        return self._finish(workdir, res, emit)

    def replace(self, workdir: Path, emit: Emit) -> Outcome:
        res = self._api().confirm(workdir, self._events(workdir, emit), replace=True, config=self.config)
        return self._finish(workdir, res, emit)

    def keep(self, workdir: Path) -> None:
        if (workdir / "job.json").is_file():
            self._api().discard(workdir)

    def discard(self, workdir: Path) -> None:
        if (workdir / "job.json").is_file():
            self._api().discard(workdir)

    def _events(self, workdir: Path, emit: Emit) -> Callable[[Any], None]:
        def on_event(ev: Any) -> None:
            upd = map_event(ev)
            if upd.get("stage") in ("plan", "review") and upd.get("state") == "done":
                # The pipeline saves job.json only after this callback, so read the plan it just wrote.
                upd.update(read_plan(workdir))
            emit(**upd)
        return on_event

    def _finish(self, workdir: Path, res: Any, emit: Emit) -> Outcome:
        out = map_result(res)
        extra = read_plan(workdir, state=res)
        if out.result is not None:
            extra["result"] = out.result
        if extra:
            emit(**extra)
        return Outcome(out.status, out.error)


@dataclass
class MappedResult:
    status: str
    error: str = ""
    result: dict | None = None


def _get(obj: Any, *names: str, default=None):
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if not isinstance(obj, dict) and hasattr(obj, n):
            return getattr(obj, n)
    return default


def map_event(ev: Any) -> dict:
    """Pipeline event (``stage``, ``status`` started/finished/failed, ``message``) -> emit keywords."""
    stage = _get(ev, "stage", default=None)
    kind = _get(ev, "status", default="")
    msg = _get(ev, "message", default="")
    states = {"started": "running", "finished": "done", "failed": "failed"}
    upd: dict = {}
    if stage in STAGES and kind in states:
        upd.update(stage=stage, state=states[kind])
    if msg:
        upd["message"] = str(msg)
    return upd


# pipeline status -> job status; "running" and "discarded" never end a start / confirm call.
RESULT_STATUS = {"imported": "done", "imported_unconfirmed": "done", "done": "done",
                 "needs_confirmation": "needs_confirmation", "duplicate": "duplicate", "failed": "failed"}


def map_result(res: Any) -> MappedResult:
    """``IngestResult`` (object or ``job.json`` dict) -> job status, error line and import result.

    ``error`` is the pipeline's Chinese ``message``: for ``failed`` why it stopped, for a
    ``needs_confirmation`` that carries an ``error`` why ``confirm`` refused the edited plan.
    """
    raw = _get(res, "status", default="failed")
    status = RESULT_STATUS.get(raw, "failed")
    message = str(_get(res, "message", default="") or "")
    error = str(_get(res, "error", default="") or "")
    imp = _get(res, "import_result", default=None)
    result = None
    if isinstance(imp, dict):
        folder = " / ".join(x for x in (imp.get("album_artist"), imp.get("album")) if x)
        result = {
            "album_url": str(imp.get("web_url") or ""),
            "imported": True,
            "confirmed": imp.get("status") == "imported",
            "folder": folder or Path(str(imp.get("dest") or "")).name,
            "file_count": len(imp.get("files") or []),
            "existing_files": len(imp.get("existing") or []),
        }
    elif raw == "done":  # cut but not imported (import switched off)
        result = {"album_url": "", "imported": False, "confirmed": False, "folder": "", "file_count":
                  len(_get(res, "output_files", default=[]) or []), "existing_files": 0}
    err = ""
    if status == "failed":
        err = message or error or f"pipeline ended with status {raw}"
    elif status == "needs_confirmation" and error:
        err = message or error
    return MappedResult(status, err, result)


def plan_path(workdir: Path) -> Path:
    """The plan the pipeline is working on (its own ``job.json`` names it)."""
    state = workdir / "job.json"
    if state.is_file():
        try:
            p = json.loads(state.read_text(encoding="utf-8")).get("plan_path")
            if p:
                return Path(p)
        except (OSError, ValueError):
            pass
    return workdir / "plan.json"


def clean_text(value: str) -> str:
    """An owner-typed title or artist as it is stored: trimmed, Traditional Chinese written Simplified."""
    from ..textnorm import to_simplified
    return to_simplified(value.strip())


def apply_edits(path: Path, edits: list[dict]) -> None:
    """Write the owner's titles / artists into the plan before the pipeline continues.

    The album follows where the owner's edit clearly means it (the import folder is
    ``<album_artist>/<album>``):

    - a single: the artist is also the album artist (``album.artist`` and the track's
      ``album_artist``); the title is also the album title when the album was named after
      the song;
    - an album: the album artist follows only when every track gets the same new artist and
      every track's old artist was the album artist; otherwise only track artists change.
    """
    if not edits:
        return
    plan = json.loads(path.read_text(encoding="utf-8"))
    tracks = plan.get("tracks") or []
    album = plan.setdefault("album", {})
    by_index = {e["index"]: {"title": clean_text(e["title"]), "artist": clean_text(e["artist"])} for e in edits}
    old = {t.get("index"): (t.get("title", ""), t.get("artist", "")) for t in tracks}
    old_album_title, old_album_artist = album.get("title", ""), album.get("artist", "")
    for t in tracks:
        e = by_index.get(t.get("index"))
        if e:
            t["title"], t["artist"] = e["title"], e["artist"]
    if plan.get("kind") == "single" or len(tracks) == 1:
        t = tracks[0] if tracks else None
        e = by_index.get(t.get("index")) if t else None
        if e:
            old_title = old[t.get("index")][0]
            album["artist"] = t["album_artist"] = e["artist"]
            if old_album_title == old_title:
                album["title"] = e["title"]
            if t.get("album", old_title) == old_title:
                t["album"] = e["title"]
    elif tracks and all(t.get("index") in by_index for t in tracks):
        new_artists = {by_index[t["index"]]["artist"] for t in tracks}
        if (len(new_artists) == 1 and old_album_artist not in new_artists
                and all(old[t["index"]][1] == old_album_artist for t in tracks)):
            new = new_artists.pop()
            album["artist"] = new
            for t in tracks:
                t["album_artist"] = new
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def flagged_tracks(plan: dict, state: Any = None) -> set[int]:
    """Track indexes the owner should look at.

    From the pipeline's result when there is one (``flagged_tracks`` and ``tracks[].flagged``),
    otherwise from the reviewed plan's ``review.flagged_tracks`` while it still needs review.
    """
    if state is not None:
        out = {i for i in _get(state, "flagged_tracks", default=[]) or [] if isinstance(i, int)}
        out |= {t.get("index") for t in _get(state, "tracks", default=[]) or []
                if isinstance(t, dict) and t.get("flagged") and isinstance(t.get("index"), int)}
        return out
    rv = plan.get("review") or {}
    if not plan.get("needs_review"):
        return set()
    return {i for i in rv.get("flagged_tracks") or [] if isinstance(i, int)}


def read_plan(workdir: Path, *, path: Path | None = None, state: Any = None) -> dict:
    """Album, tracks, review summary and cover from a plan (default: the pipeline's current one)."""
    path = path or plan_path(workdir)
    if not path.is_file():
        return {}
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    al = plan.get("album") or {}
    src = plan.get("source") or {}
    tracks = plan.get("tracks") or []
    single = plan.get("kind") == "single"
    title, artist = al.get("title", ""), al.get("artist", "")
    if single and tracks:  # a single is shown by its song, not by the album it belongs to
        title, artist = tracks[0].get("title") or title, tracks[0].get("artist") or artist
    out: dict = {"album": {"title": title, "artist": artist, "year": str(al.get("year") or "")[:4],
                           "kind": "single" if single else "album",
                           "uploader": src.get("uploader", "") if plan.get("derivative") else "",
                           # the import folder is <album_artist>/<album_title>
                           "album_title": (tracks[0].get("album") if tracks else "") or al.get("title", ""),
                           "album_artist": (tracks[0].get("album_artist") if tracks else "") or artist}}
    flagged = flagged_tracks(plan, state)
    out["tracks"] = [{"index": t.get("index", i), "title": t.get("title", ""), "artist": t.get("artist", ""),
                      "duration": round(float(t.get("end", 0)) - float(t.get("start", 0)), 3),
                      "flagged": t.get("index", i) in flagged}
                     for i, t in enumerate(tracks, 1)]
    rv = plan.get("review") or {}
    if rv:
        out["review"] = {
            "verdict": rv.get("verdict", ""),
            "reasons": [str(r) for r in rv.get("reasons") or []],
            "remaining_concerns": [str(r) for r in rv.get("remaining_concerns") or []],
            "edits": [{"tracks": [i for i in e.get("tracks") or [] if isinstance(i, int)],
                       "reason": str(e.get("reason_zh") or "")}
                      for e in rv.get("edits_applied") or [] if isinstance(e, dict)],
            "rejected": [str(e["reason_zh"]) for e in rv.get("edits_rejected") or []
                         if isinstance(e, dict) and e.get("reason_zh")],
        }
    cover = al.get("cover_path") or next((t.get("cover_path") for t in tracks if t.get("cover_path")), "")
    if cover:
        p = Path(cover)
        p = p if p.is_absolute() else workdir / p
        try:
            out["cover"] = str(p.resolve().relative_to(workdir.resolve()))
        except (ValueError, OSError):
            pass
    return out
