"""Scripted jobs for ``serve --fake``: the real pipeline with its four stages replaced.

``FakeRunner`` is a :class:`~bili_album.web.runner.PipelineRunner` whose pipeline is
:mod:`bili_album.pipeline` itself, called with scripted :class:`~bili_album.pipeline.Steps`.
Events, ``job.json``, statuses, the hold after review and the hard-problem check in
``confirm`` are therefore the pipeline's own; only the plan, the review, the cut files and the
import result are made up here, in the shapes the real stages write (taken from real
reviewed plans and a real ``job.json``). Nothing touches the network or the music library.

The script is chosen by the video's BV id (see :data:`SCRIPTS`; unknown ids get 范特西).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .runner import PipelineRunner
from .urls import video_key


class BilibiliError(RuntimeError):
    """Same name as :class:`bili_album.bilibili.BilibiliError`, so failure text reads like a real one."""


@dataclass
class Script:
    title: str
    artist: str
    year: str
    durations: list[float]
    titles: list[str]
    kind: str = "compilation"
    uploader: str = "音乐私藏馆"
    genre: str = ""
    review: dict = field(default_factory=dict)     # verdict, reasons, remaining_concerns, edits, rejected, flagged
    derivative: dict | None = None
    import_status: str = "imported"                # imported | imported_unconfirmed | duplicate
    fail_plan: str = ""                            # exception text raised in the plan stage
    overlap: int = 0                               # index of a song that runs 2 s into the next one (a hard problem)
    cover_jpeg: tuple[int, int, int] | None = None


def _edit(op: str, tracks: list[int], reason_zh: str, detail: str, **kw) -> dict:
    return {"op": op, **kw, "detail": detail, "reason_zh": reason_zh, "tracks": tracks}


SCRIPTS: dict[str, Script] = {
    # Clean album: approved as it is.
    "BV14PVVzNE2R": Script("范特西", "周杰伦", "2001-09-14", [238, 229, 264, 159, 293, 197, 224, 233, 200, 331], [
        "爱在西元前", "爸，我回来了", "简单爱", "忍者", "开不了口", "上海一九四三", "对不起", "威廉古堡", "双截棍", "安静"],
        genre="华语流行乐", review={"verdict": "approved", "reasons": [
            "视频简介和评论列出的十首歌及其顺序均与周杰伦的原专辑《范特西》一致，无需修改标签。"]}),
    # Sol fixed one title.
    "BV1sT421k7Ps": Script("Starboy", "The Weeknd", "2016-11-25",
                           [230, 249, 220, 218, 233, 266, 205, 111, 231, 237, 223, 220, 198, 222, 199, 319, 261, 268], [
                               "Starboy", "Party Monster", "False Alarm", "Reminder", "Rockin'", "Secrets", "True Colors",
                               "Stargirl Interlude", "Sidewalks", "Six Feet Under", "Love to Lay", "A Lonely Night",
                               "Attention", "Ordinary Life", "Nothing Without You", "All I Know", "Die for You",
                               "I Feel It Coming"], uploader="欧美音乐搬运", genre="R&B/Soul",
                           review={"verdict": "edited", "reasons": ["曲库和识曲结果都确认这是 The Weeknd 的《Starboy》。"],
                                   "edits": [_edit("relabel_track", [16],
                                                   "第 16 首歌名「All I Konw」是评论区的笔误，已按曲库改为「All I Know」。",
                                                   "track 16: title 'All I Konw' -> 'All I Know' (catalog)",
                                                   track=16, title="All I Know")]}),
    # Derivative single: always held.
    "BV1Hx4y1z7Qm": Script("5:20AM", "soldier", "2024-01-18", [121], ["5:20AM"], kind="single",
                           uploader="69岁牢二次元",
                           review={"verdict": "needs_human", "flagged": [1], "reasons": [
                               "标题里有「司凤版」「乐队演奏」，这是二创版本，不是原曲。歌手已按原唱填为 soldier。"],
                               "remaining_concerns": [
                                   "曲库里还有另外两首同名歌（刀酱、周明明），原唱是根据最早的发行日期选的，请确认。"],
                               "edits": [_edit("mark_derivative", [1],
                                               "这是乐队演奏版，原曲是 soldier《5:20AM》（2024，曲库已核实），已按原曲填写歌名和艺人。"
                                               "衍生版本一律需要人工确认。",
                                               "derivative (other) of soldier - 5:20AM (2024-01-18), confirmed by netease",
                                               version="other", original_title="5:20AM", original_artist="soldier")]},
                           derivative={"version": "other", "original_title": "5:20AM", "original_artist": "soldier",
                                       "original_album": "5:20AM", "original_year": "2024-01-18",
                                       "catalog": {"provider": "netease", "id": "2118204311"}, "source": "llm_review"}),
    # Album with one flagged track.
    "BV1Wb411m7Eq": Script("dont smile at me", "Billie Eilish", "2017-08-11", [192, 205, 172, 207, 177, 181, 200, 231], [
        "COPYCAT", "idontwannabeyouanymore", "my boy", "ocean eyes", "watch", "party favor", "bellyache", "&burn"],
        uploader="欧美音乐搬运", review={"verdict": "needs_human", "flagged": [5], "reasons": [
            "这个视频的曲序和数字版专辑不同。已按识曲结果把第 5 首改为 watch，后面三首顺延；第 5 首的识曲把握不高，请确认。"]}),
    # Plan fails the way a missing video does.
    "BV1GJ411x7h7": Script("", "", "", [], [], fail_plan="https://api.bilibili.com/x/web-interface/view: code -404 啥都木有"),
    # Already in the library.
    "BV1zt411p7Cn": Script("运河", "蛋堡", "2014-06-20", [215, 198, 240, 201, 187, 230, 222, 205, 199, 260, 214, 190],
                           [f"运河 {i}" for i in range(1, 13)], review={"verdict": "approved"}, import_status="duplicate"),
    # Imported, but Jellyfin did not list it in time; has a cover image.
    "BV1Ns411D7pE": Script("花样年华", "群星", "2000-09-29", [182, 214, 197, 240], [
        "Yumeji's Theme", "花样的年华", "Quizás, Quizás, Quizás", "Aquellos Ojos Verdes"],
        review={"verdict": "approved"}, import_status="imported_unconfirmed", cover_jpeg=(107, 36, 48)),
    # Held, with an edit the tool could not check; confirm is refused (tracks 3 and 4 overlap).
    "BV1Kq4y1M7Vd": Script("七里香", "周杰伦", "2004-08-03", [299, 236, 228, 212, 245, 263, 230, 214, 242, 209], [
        "我的地盘", "七里香", "借口", "外婆", "将军", "搁浅", "乱舞春秋", "困兽之斗", "园游会", "止战之殇"],
        overlap=3, review={"verdict": "needs_human", "flagged": [3, 4], "reasons": [
            "第 3、4 首之间的切点和曲库时长对不上。"],
            "rejected": [{"edit": {"op": "move_boundary", "boundary": 3, "to": 689.0},
                          "why": "689.0 is not one of the measured options for boundary 3", "tracks": [3, 4],
                          "reason_zh": "工具无法核实“移动切点（第 3 个切点 → 689.0 秒）”，未采纳，需要人工确认"
                                       "（689.0 is not one of the measured options for boundary 3）。"}]}),
}
DEFAULT = "BV14PVVzNE2R"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def make_plan(url: str, s: Script, workdir: Path) -> dict:
    """A ``plan.json`` in the planner's shape (before review)."""
    cover = workdir / "cover.jpg"
    if s.cover_jpeg:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (300, 300), s.cover_jpeg)
        ImageDraw.Draw(img).ellipse((60, 60, 240, 240), fill=tuple(min(255, c + 50) for c in s.cover_jpeg))
        img.save(cover, "JPEG", quality=85)
    cover_path = str(cover) if s.cover_jpeg else ""
    total = float(sum(s.durations))
    tracks, t0 = [], 0.0
    for i, (d, title) in enumerate(zip(s.durations, s.titles), 1):
        start, end = t0, t0 + float(d)
        if s.overlap == i:
            end += 2.0  # runs into the next track
        tracks.append({
            "index": i, "start": round(start, 3), "end": round(end, 3), "title": title, "artist": s.artist,
            "album": s.title, "album_artist": s.artist, "track_number": i, "disc_number": 1, "year": s.year,
            "genre": s.genre, "composer": "", "lyrics": "", "cover_path": cover_path,
            "catalog": {"provider": "netease", "id": f"fake-{i}"}, "confidence": 0.9,
            "evidence": [{"kind": "catalog_duration", "detail": "scripted by serve --fake"}],
        })
        t0 += float(d)
    return {
        "version": 1,
        "source": {"url": url, "bvid": video_key(url), "title": f"【{s.artist}】{s.title}", "uploader": s.uploader,
                   "duration": total, "audio_path": str(workdir / "source.m4a"), "audio_format": "aac",
                   "cover_path": cover_path, "parts": [], "trim_head": 0.0, "trim_tail": 0.0, "text_hints": True},
        "kind": s.kind,
        "kind_reason": "scripted by serve --fake",
        "album": {"title": s.title, "artist": s.artist, "year": s.year, "genre": s.genre,
                  "track_total": len(tracks), "disc_total": 1, "cover_path": cover_path,
                  "catalog": {"provider": "netease", "id": "fake", "url": ""}, "match_reason": "scripted"},
        "tracks": tracks,
        "needs_review": False,
        "review_reasons": [],
        "timings": {"total": 0.0},
    }


def add_review(plan: dict, s: Script) -> dict:
    """The ``review`` block :func:`bili_album.review.review_plan` writes, and its effect on the plan."""
    r = s.review
    flagged = list(r.get("flagged") or [])
    needs = r.get("verdict") == "needs_human" or bool(r.get("rejected")) or bool(s.derivative)
    plan["review"] = {
        "model": "fake-sol", "verdict": r.get("verdict", "approved"), "confidence": 0.9,
        "edits_applied": list(r.get("edits") or []), "edits_rejected": list(r.get("rejected") or []),
        "reasons": list(r.get("reasons") or []), "remaining_concerns": list(r.get("remaining_concerns") or []),
        "error": None, "timing": {"total_s": 0.0, "model_s": 0.0, "calls": 1}, "usage": {},
        "model_verdict": r.get("verdict", "approved"), "reviewed_at": _now(), "resolved": [],
        "flagged_tracks": flagged if needs else [],
    }
    for e in r.get("edits") or []:
        if e["op"] == "relabel_track":
            plan["tracks"][e["track"] - 1]["title"] = e["title"]
    if s.derivative:
        plan["derivative"] = dict(s.derivative)
        plan["source"]["derivative"] = True
    plan["needs_review"] = needs
    if needs:
        plan["review_reasons"] = ["review: the model asks for a human check"]
    return plan


class FakePipeline:
    """Stands in for the ``bili_album.pipeline`` module: same calls, scripted stages."""

    def __init__(self, speed: float = 1.0, hold: dict[str, float] | None = None):
        self.speed = speed if speed > 0 else 1.0
        self.hold = hold or {}

    def _wait(self, seconds: float, stage: str) -> None:
        time.sleep(seconds / self.speed + self.hold.get(stage, 0.0))

    def _steps(self, workdir: Path):
        from .. import pipeline
        from ..apply import ApplyResult
        from ..library import ImportResult
        from ..review import ReviewOutcome

        key = json.loads((Path(workdir) / "fake.json").read_text(encoding="utf-8"))["key"]
        s = SCRIPTS.get(key, SCRIPTS[DEFAULT])

        def plan(url, wd, opts):
            self._wait(2.0, "plan")
            if s.fail_plan:
                raise BilibiliError(s.fail_plan)
            p = Path(wd) / "plan.json"
            p.write_text(json.dumps(make_plan(url, s, Path(wd)), ensure_ascii=False, indent=2), encoding="utf-8")
            return p

        def review(plan_path, config):
            self._wait(2.5, "review")
            pl = add_review(json.loads(Path(plan_path).read_text(encoding="utf-8")), s)
            Path(plan_path).write_text(json.dumps(pl, ensure_ascii=False, indent=2), encoding="utf-8")
            return ReviewOutcome(plan=pl, review=pl["review"], needs_review=pl["needs_review"], out_path=Path(plan_path),
                                 unreviewed_path=None)

        def cut(plan_path, outdir):
            pl = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            self._wait(0.25 * len(pl["tracks"]), "cut")
            folder = Path(outdir) / (pl["album"]["title"] or "single")
            folder.mkdir(parents=True, exist_ok=True)
            files = []
            for t in pl["tracks"]:
                f = folder / f"{t['index']:02d}-{t['title'].replace('/', '／')}.placeholder"
                f.write_bytes(b"")  # no audio: the fake never cuts anything
                files.append(f)
            return ApplyResult(folder=folder, files=files)

        def import_(source, cover, replace, config):
            self._wait(1.5, "import")
            pl = json.loads((Path(workdir) / "plan.json").read_text(encoding="utf-8"))
            album, artist = pl["album"]["title"], pl["album"]["artist"]
            names = [f"{t['index']:02d}-{t['title']}.m4a" for t in pl["tracks"]]
            st = "imported" if replace else s.import_status
            res = ImportResult(st, album=album, album_artist=artist, dest=f"/fake-library/{artist}/{album}",
                               container_path=f"/fake-container/{artist}/{album}", files=names, replaced=replace)
            if st == "duplicate":
                res.existing = [f"{i:02d}-old.m4a" for i in range(1, len(names) + 1)]
                res.reason = "the library folder already holds files"
            elif st == "imported":
                res.item_id = f"fake{abs(hash(album)) % 10**8:08d}"
                res.jellyfin_track_count = len(names)
                res.web_url = f"https://jellyfin.example/web/#/details?id={res.item_id}&serverId=fake"
            else:
                res.reason = "Jellyfin did not list the album within 180 s"
            return res

        return pipeline, pipeline.Steps(plan=plan, review=review, cut=cut, import_=import_)

    def ingest(self, url, workdir, on_event, **kw):
        Path(workdir).mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "fake.json").write_text(json.dumps({"key": video_key(url)}), encoding="utf-8")
        pipeline, steps = self._steps(workdir)
        return pipeline.ingest(url, workdir, on_event, steps=steps, **kw)

    def confirm(self, workdir, on_event, **kw):
        pipeline, steps = self._steps(workdir)
        return pipeline.confirm(workdir, on_event, steps=steps, **kw)

    def discard(self, workdir):
        from .. import pipeline
        return pipeline.discard(workdir)


class FakeRunner(PipelineRunner):
    """``PipelineRunner`` over :class:`FakePipeline`. ``speed`` divides every delay; ``hold`` maps a
    stage name to extra seconds spent in it (for screenshots of a job in that stage)."""

    def __init__(self, speed: float = 1.0, hold: dict[str, float] | None = None, config=None):
        from ..config import Config
        super().__init__(config=config or Config(), api=FakePipeline(speed, hold))
