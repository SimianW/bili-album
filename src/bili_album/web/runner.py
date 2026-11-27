"""What the job manager needs from the processing pipeline, and two ways to provide it.

A runner works on one job at a time inside the job's work directory and reports
progress through ``emit``. Every keyword is optional; the job manager merges
what it gets into the job:

- ``stage`` + ``state``: one of :data:`STAGES` and ``running | done | held | failed``;
- ``message``: the live line under the job (short, Simplified Chinese);
- ``album``: ``{"title", "artist", "year", "kind", "uploader"}`` (``kind`` is ``single | album``);
- ``tracks``: ``[{"index", "title", "artist", "duration", "flagged"}]``;
- ``review``: ``{"verdict", "reasons", "remaining_concerns", "edits": [{"index", "reason"}]}``;
- ``result``: the import result ``{"album_url", "confirmed", "folder", "file_count", "existing_files"}``;
- ``cover``: path of the cover image, relative to the work directory.

Each call returns an :class:`Outcome`: the job is ``done``, waits for the owner
(``needs_confirmation`` or ``duplicate``) or ``failed`` (``error`` says why, in
words the owner can act on). An exception counts as a failure in the current stage.

``PipelineRunner`` drives :mod:`bili_album.pipeline`; ``FakeRunner`` (``serve --fake``)
replays scripted jobs so every state of the page can be produced without the network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .urls import video_key

STAGES = ("plan", "review", "cut", "import")
STAGE_NAMES = {"plan": "生成方案", "review": "Sol 复核", "cut": "切分写标签", "import": "导入 Jellyfin"}

Emit = Callable[..., None]


@dataclass
class Outcome:
    status: str  # done | needs_confirmation | duplicate | failed
    error: str = ""


class Runner(Protocol):
    def start(self, url: str, workdir: Path, emit: Emit) -> Outcome:
        """Plan, review, cut and import (or resume after a failure: the work directory is reused)."""

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


# ----------------------------------------------------------------------------- fake runner

def _album(title, artist, year, durations, *, kind="album", uploader="", titles=None, artists=None,
           flagged=()):
    titles = titles or [f"Track {i}" for i in range(1, len(durations) + 1)]
    tracks = [{"index": i, "title": titles[i - 1], "artist": (artists or {}).get(i, artist),
               "duration": float(d), "flagged": i in flagged}
              for i, d in enumerate(durations, 1)]
    return {"title": title, "artist": artist, "year": year, "kind": kind, "uploader": uploader}, tracks


FANTASY = _album("范特西", "周杰伦", "2001", [238, 229, 264, 159, 293, 197, 224, 233, 200, 331], titles=[
    "爱在西元前", "爸，我回来了", "简单爱", "忍者", "开不了口", "上海一九四三", "对不起", "威廉古堡", "双截棍", "安静"])
STARBOY = _album("Starboy", "The Weeknd", "2016",
                 [230, 249, 220, 218, 233, 266, 205, 111, 231, 237, 223, 220, 198, 222, 199, 319, 261, 268], titles=[
                     "Starboy", "Party Monster", "False Alarm", "Reminder", "Rockin'", "Secrets", "True Colors",
                     "Stargirl Interlude", "Sidewalks", "Six Feet Under", "Love to Lay", "A Lonely Night",
                     "Attention", "Ordinary Life", "Nothing Without You", "All I Know", "Die for You",
                     "I Feel It Coming"])
SINGLE = _album("5:20AM", "soldier", "", [121], kind="single", uploader="69岁牢二次元", titles=["5:20AM"], flagged={1})
EILISH = _album("dont smile at me", "Billie Eilish", "", [192, 205, 172, 207, 177, 181, 200, 231], titles=[
    "COPYCAT", "idontwannabeyouanymore", "my boy", "ocean eyes", "watch", "party favor", "bellyache",
    "&burn"], flagged={5})
CANAL = _album("运河", "蛋堡", "", [215, 198, 240, 201, 187, 230, 222, 205, 199, 260, 214, 190], titles=[
    f"运河 {i}" for i in range(1, 13)])
FLOWER = _album("花样年华", "群星", "2000", [182, 214, 197, 240], titles=[
    "Yumeji's Theme", "花样的年华", "Quizás, Quizás, Quizás", "Aquellos Ojos Verdes"])


def _plan_steps(album, tracks):
    n = len(tracks)
    return [
        (0.0, dict(stage="plan", state="running", message="正在读取视频信息。")),
        (1.0, dict(message="正在下载音频和封面。")),
        (1.5, dict(message=f"正在找切点：对照曲库的 {n} 首曲目长度。" if n > 1 else "正在识曲，查找原曲信息。")),
        (1.5, dict(stage="plan", state="done", album=album, tracks=tracks, message="方案已生成。")),
    ]


def _review_steps(n):
    what = f" {n} 首歌的歌名、歌手和切点" if n > 1 else "歌名、歌手和版本"
    return [(0.3, dict(stage="review", state="running", message=f"Sol 正在核对{what}。")), (2.5, {})]


def _cut_steps(n):
    steps = [(0.3, dict(stage="cut", state="running", message=f"正在切第 1 首，共 {n} 首。"))]
    for i in range(2, n + 1):
        steps.append((0.25, dict(message=f"Sol 复核通过。正在切第 {i} 首，共 {n} 首。")))
    steps.append((0.3, dict(stage="cut", state="done", message="切分完成，标签已写好。")))
    return steps


def _import_steps(result):
    return [(0.3, dict(stage="import", state="running", message="正在放入音乐库，等 Jellyfin 扫描。")),
            (1.5, dict(stage="import", state="done", result=result, message="导入完成。"))]


def _jellyfin(album):
    return f"https://jellyfin.example/web/#/details?id=fake-{abs(hash(album['title'])) % 10**8:08d}"


def _result(album, n, confirmed=True):
    return {"album_url": _jellyfin(album) if confirmed else "", "confirmed": confirmed,
            "folder": f"{album['artist']} / {album['title']}", "file_count": n, "existing_files": 0}


@dataclass
class Script:
    """One scripted job: steps before the hold (``first``), after confirm (``after``), and how it ends."""
    first: list
    end: Outcome
    after: list | None = None
    after_end: Outcome | None = None


def _scripts() -> dict[str, Script]:
    def clean(album_tracks, review, *, confirmed=True):
        album, tracks = album_tracks
        n = len(tracks)
        return Script(_plan_steps(album, tracks) + _review_steps(n)
                      + [(0.0, dict(stage="review", state="done", review=review))]
                      + _cut_steps(n) + _import_steps(_result(album, n, confirmed)), Outcome("done"))

    def held(album_tracks, review):
        album, tracks = album_tracks
        n = len(tracks)
        return Script(_plan_steps(album, tracks) + _review_steps(n)
                      + [(0.0, dict(stage="review", state="done", review=review)),
                         (0.0, dict(stage="cut", state="held", message="Sol 拿不准，等你确认。"))],
                      Outcome("needs_confirmation"),
                      after=_cut_steps(n) + _import_steps(_result(album, n)), after_end=Outcome("done"))

    approved = {"verdict": "approved", "reasons": [], "remaining_concerns": [], "edits": []}
    canal_album, canal_tracks = CANAL
    canal = Script(_plan_steps(canal_album, canal_tracks) + _review_steps(12)
                   + [(0.0, dict(stage="review", state="done", review=approved))] + _cut_steps(12)
                   + [(0.3, dict(stage="import", state="running", message="正在检查音乐库。")),
                      (0.6, dict(stage="import", state="held", message="音乐库里已有这张专辑。",
                                 result={**_result(canal_album, 12), "album_url": "", "existing_files": 12}))],
                   Outcome("duplicate"), after=_import_steps(_result(canal_album, 12)), after_end=Outcome("done"))
    return {
        # 范特西: clean album; review and cut are slow enough to watch.
        "BV14PVVzNE2R": clean(FANTASY, approved),
        # Starboy: Sol fixed one title.
        "BV1sT421k7Ps": clean(STARBOY, {"verdict": "edited", "reasons": [], "remaining_concerns": [], "edits": [
            {"index": 16, "reason": "第 16 首歌名「All I Konw」是评论区的笔误，已按曲库改为「All I Know」。"}]}),
        # 5:20AM: a derivative single, held for confirmation.
        "BV1Hx4y1z7Qm": held(SINGLE, {"verdict": "needs_human", "edits": [], "reasons": [
            "标题里有「司凤版」「乐队演奏」，这是二创版本，不是原曲。歌手已按原唱填为 soldier。",
        ], "remaining_concerns": [
            "曲库里还有另外两首同名歌（刀酱、周明明），原唱是根据最早的发行日期选的，请确认。"]}),
        # dont smile at me: one flagged track.
        "BV1Wb411m7Eq": held(EILISH, {"verdict": "needs_human", "edits": [], "reasons": [
            "这个视频的曲序和数字版专辑不同。已按识曲结果把第 5 首改为 watch，后面三首顺延；第 5 首的识曲把握不高，请确认。"],
            "remaining_concerns": []}),
        # Plan fails: the video does not exist.
        "BV1GJ411x7h7": Script([(0.0, dict(stage="plan", state="running", message="正在读取视频信息。")),
                                (1.0, dict(stage="plan", state="failed"))],
                               Outcome("failed", "B 站说这个视频不存在，或者在当前地区看不了。检查链接是否完整，或换一个视频。")),
        # 运河: already in the library.
        "BV1zt411p7Cn": canal,
        # 花样年华: has a cover image; imported, but Jellyfin did not show it in time.
        "BV1Ns411D7pE": clean(FLOWER, approved, confirmed=False),
    }


FAKE_IDS = tuple(_scripts())


class FakeRunner:
    """Replays a script chosen by the video's BV id (unknown ids get the clean 范特西 album).

    ``speed`` divides every delay (2.0 runs twice as fast). ``hold`` keeps a job in one place
    longer, for screenshots: ``{"review": 600}`` waits 600 s before the review stage finishes,
    ``{"cut@6": 600}`` waits after the 6th progress line of the cut stage.
    """

    def __init__(self, speed: float = 1.0, hold: dict[str, float] | None = None):
        self.speed = speed if speed > 0 else 1.0
        self.hold = hold or {}
        self._scripts = _scripts()

    def _script(self, workdir: Path) -> Script:
        state = json.loads((workdir / "fake.json").read_text(encoding="utf-8"))
        return self._scripts.get(state["key"], self._scripts["BV14PVVzNE2R"])

    def _play(self, steps, end: Outcome, workdir: Path, emit: Emit) -> Outcome:
        current, lines = "", 0
        for delay, upd in steps:
            time.sleep(delay / self.speed)
            if upd.get("state") == "running":
                current, lines = upd["stage"], 0
            if upd.get("state") == "done" and current in self.hold:
                time.sleep(self.hold[current])
            if upd:
                if "album" in upd:
                    self._write_cover(workdir, upd["album"])
                    upd = {**upd, "cover": "cover.svg"} if upd["album"]["title"] == "花样年华" else upd
                emit(**upd)
            if upd.get("message"):
                lines += 1
                if f"{current}@{lines}" in self.hold:
                    time.sleep(self.hold[f"{current}@{lines}"])
        return end

    @staticmethod
    def _write_cover(workdir: Path, album: dict) -> None:
        if album["title"] != "花样年华":
            return
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">'
               '<rect width="300" height="300" fill="#6b2430"/><circle cx="150" cy="150" r="90" fill="#9c3a48"/>'
               '<circle cx="150" cy="150" r="22" fill="#6b2430"/></svg>')
        (workdir / "cover.svg").write_text(svg, encoding="utf-8")

    def start(self, url: str, workdir: Path, emit: Emit) -> Outcome:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "fake.json").write_text(json.dumps({"key": video_key(url)}), encoding="utf-8")
        s = self._script(workdir)
        return self._play(s.first, s.end, workdir, emit)

    def confirm(self, workdir: Path, edits: list[dict], emit: Emit) -> Outcome:
        s = self._script(workdir)
        (workdir / "edits.json").write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
        return self._play(s.after or [], s.after_end or Outcome("done"), workdir, emit)

    def replace(self, workdir: Path, emit: Emit) -> Outcome:
        s = self._script(workdir)
        return self._play(s.after or [], s.after_end or Outcome("done"), workdir, emit)

    def keep(self, workdir: Path) -> None:
        pass

    def discard(self, workdir: Path) -> None:
        pass

    def secrets(self) -> list[str]:
        return []


# ----------------------------------------------------------------------------- pipeline runner

class PipelineRunner:
    """Runs :mod:`bili_album.pipeline` in the job's work directory.

    Wired against ``pipeline.py`` as it stood on 2026-11-26 (untracked, sha256 058699a7...):
    ``ingest(url, workdir, on_event, on_unsure="hold", config=)``, ``confirm(workdir, on_event,
    replace=, config=)``, ``discard(workdir)``; events are dicts with ``stage``, ``status``
    (started / finished / failed) and ``message``; the result is an ``IngestResult`` whose
    ``status`` is imported / imported_unconfirmed / duplicate / needs_confirmation / failed / done.
    All name knowledge lives in this class, :func:`map_event` and :func:`map_result`.
    """

    def __init__(self, config=None):
        from ..config import load_config
        self.config = config or load_config()

    def secrets(self) -> list[str]:
        c = self.config
        vals = list(c.secrets()) + [c.llm_base_url, c.jellyfin_url, c.llm_model]
        return [v for v in vals if v and len(v) >= 4]

    @staticmethod
    def _api():
        from .. import pipeline  # imported late: fake mode must not need it
        return pipeline

    def start(self, url: str, workdir: Path, emit: Emit) -> Outcome:
        res = self._api().ingest(url, workdir, self._events(workdir, emit), on_unsure="hold", config=self.config)
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
                upd.update(read_plan(workdir))
            emit(**upd)
        return on_event

    def _finish(self, workdir: Path, res: Any, emit: Emit) -> Outcome:
        out = map_result(res)
        extra = read_plan(workdir)
        if out.result is not None:
            extra["result"] = out.result
        if out.status == "needs_confirmation" and _get(res, "error", default=""):
            # confirm() refused (the edited plan still has hard problems): say why, stay held
            extra["message"] = str(_get(res, "message", default="") or _get(res, "error"))
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
    kind = _get(ev, "status", "kind", default="")
    msg = _get(ev, "message", default="")
    states = {"started": "running", "finished": "done", "failed": "failed"}
    upd: dict = {}
    if stage in STAGES and kind in states:
        upd.update(stage=stage, state=states[kind])
    if msg:
        upd["message"] = str(msg)
    return upd


RESULT_STATUS = {"imported": "done", "imported_unconfirmed": "done", "done": "done",
                 "needs_confirmation": "needs_confirmation", "duplicate": "duplicate", "failed": "failed"}


def map_result(res: Any) -> MappedResult:
    raw = _get(res, "status", default="failed")
    status = RESULT_STATUS.get(raw, "failed")
    imp = _get(res, "import_result", default=None)
    result = None
    if isinstance(imp, dict):
        folder = " / ".join(x for x in (imp.get("album_artist"), imp.get("album")) if x)
        result = {
            "album_url": str(imp.get("web_url") or ""),
            "confirmed": imp.get("status") == "imported",
            "folder": folder or Path(str(imp.get("dest") or "")).name,
            "file_count": len(imp.get("files") or []),
            "existing_files": len(imp.get("existing") or []),
        }
    elif raw == "done":  # cut but not imported (do_import=False): nothing in Jellyfin
        result = {"album_url": "", "confirmed": False, "folder": "", "file_count": 0, "existing_files": 0}
    err = ""
    if status == "failed":
        err = str(_get(res, "message", default="") or _get(res, "error", default="") or "")
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


def apply_edits(path: Path, edits: list[dict]) -> None:
    """Write the owner's titles / artists into the plan before the pipeline continues."""
    if not edits:
        return
    plan = json.loads(path.read_text(encoding="utf-8"))
    by_index = {e["index"]: e for e in edits}
    for t in plan.get("tracks") or []:
        e = by_index.get(t.get("index"))
        if e:
            t["title"], t["artist"] = e["title"], e["artist"]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_plan(workdir: Path) -> dict:
    """Album, tracks, review summary and cover from the plan the pipeline left in ``workdir``."""
    path = plan_path(workdir)
    if not path.is_file():
        return {}
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    al = plan.get("album") or {}
    src = plan.get("source") or {}
    kind = "single" if plan.get("kind") == "single" else "album"
    out: dict = {"album": {"title": al.get("title", ""), "artist": al.get("artist", ""),
                           "year": str(al.get("year") or "")[:4], "kind": kind,
                           "uploader": src.get("uploader", "") if plan.get("derivative") else ""}}
    rv = plan.get("review") or {}
    flagged = _flagged_tracks(plan, rv) if plan.get("needs_review") else set()
    out["tracks"] = [{"index": t.get("index", i), "title": t.get("title", ""), "artist": t.get("artist", ""),
                      "duration": round(float(t.get("end", 0)) - float(t.get("start", 0)), 3),
                      "flagged": t.get("index", i) in flagged}
                     for i, t in enumerate(plan.get("tracks") or [], 1)]
    if rv:
        out["review"] = {
            "verdict": rv.get("verdict", ""),
            "reasons": [str(r) for r in rv.get("reasons") or []],
            "remaining_concerns": [str(r) for r in rv.get("remaining_concerns") or []],
            "edits": [{"index": e.get("track"), "reason": str(e.get("detail") or e.get("op") or "")}
                      for e in rv.get("edits_applied") or []],
        }
    cover = al.get("cover_path") or next((t.get("cover_path") for t in plan.get("tracks") or []
                                         if t.get("cover_path")), "")
    if cover:
        p = Path(cover)
        p = p if p.is_absolute() else workdir / p
        try:
            out["cover"] = str(p.resolve().relative_to(workdir.resolve()))
        except (ValueError, OSError):
            pass
    return out


def _flagged_tracks(plan: dict, rv: dict) -> set[int]:
    import re
    flagged: set[int] = set()
    texts = list(plan.get("review_reasons") or []) + list(rv.get("remaining_concerns") or [])
    texts += [str(r.get("why", "")) + json.dumps(r.get("edit", {})) for r in rv.get("edits_rejected") or []]
    for t in texts:
        for m in re.finditer(r"(?:track|第)\s*(\d+)", str(t)):
            flagged.add(int(m.group(1)))
    return flagged
