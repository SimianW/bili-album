"""ingest / confirm / discard with fake stages (offline)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from bili_album.config import Config
from bili_album.pipeline import Steps, confirm, discard, ingest, load_job


def plan_doc(workdir: Path, needs_review: bool) -> dict:
    return {"version": 1, "kind": "compilation", "source": {"duration": 20.0, "audio_path": str(workdir / "a.m4a")},
            "album": {"title": "范特西", "artist": "周杰伦", "year": "2001", "genre": "", "cover_path": "",
                      "track_total": 2},
            "tracks": [{"index": 1, "start": 0.0, "end": 10.0, "title": "爱在西元前", "artist": "周杰伦",
                        "album": "范特西", "album_artist": "周杰伦", "track_number": 1, "confidence": 0.9},
                       {"index": 2, "start": 10.0, "end": 20.0, "title": "简单爱", "artist": "周杰伦",
                        "album": "范特西", "album_artist": "周杰伦", "track_number": 2, "confidence": 0.9}],
            "needs_review": needs_review, "review_reasons": ["track 2: check"] if needs_review else []}


@dataclass
class FakeApply:
    folder: Path
    files: list
    warnings: list = field(default_factory=list)


@dataclass
class FakeImport:
    status: str = "imported"

    def to_dict(self):
        return {"status": self.status, "album": "范特西", "jellyfin_track_count": 2, "existing": ["01-x.m4a"],
                "reason": "", "item_id": "i1", "web_url": "https://m/web/#/details?id=i1&serverId=s"}


def steps(needs_review_after_review: bool, import_status="imported", calls=None, plan_raises=None):
    calls = calls if calls is not None else []

    def plan(url, workdir, opts):
        calls.append("plan")
        if plan_raises:
            raise plan_raises
        p = workdir / "plan.json"
        p.write_text(json.dumps(plan_doc(workdir, True), ensure_ascii=False), encoding="utf-8")
        return p

    def review(plan_path, config):
        calls.append("review")
        d = json.loads(plan_path.read_text(encoding="utf-8"))
        d["needs_review"] = needs_review_after_review
        d["review"] = {"model": "m", "verdict": "needs_human" if needs_review_after_review else "approved",
                       "flagged_tracks": [2] if needs_review_after_review else [],
                       "confidence": 0.8, "reasons": ["理由"], "remaining_concerns": ["请确认第二首"] if
                       needs_review_after_review else [], "edits_applied": [], "edits_rejected": [], "error": None}
        plan_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return None

    def cut(plan_path, outdir):
        calls.append("cut")
        folder = outdir / "范特西"
        folder.mkdir(parents=True)
        files = [folder / "01-爱在西元前.m4a", folder / "02-简单爱.m4a"]
        for f in files:
            f.write_bytes(b"x")
        return FakeApply(folder, files)

    def imp(source, cover, replace, config):
        calls.append(f"import replace={replace}")
        return FakeImport(import_status if not replace else "imported")

    return Steps(plan=plan, review=review, cut=cut, import_=imp)


CFG = Config()


def test_full_run_events_and_state(tmp_path):
    events, calls = [], []
    job = ingest("https://b23.tv/x", tmp_path / "w", events.append, steps=steps(False, calls=calls), config=CFG)
    assert job.status == "imported" and calls == ["plan", "review", "cut", "import replace=False"]
    assert [(e["stage"], e["status"]) for e in events] == [
        ("plan", "started"), ("plan", "finished"), ("review", "started"), ("review", "finished"),
        ("cut", "started"), ("cut", "finished"), ("import", "started"), ("import", "finished")]
    assert all(e["message"] and e["job_id"] == job.job_id for e in events)
    assert events[1]["elapsed_s"] is not None and "范特西" in events[1]["message"]
    saved = load_job(tmp_path / "w")
    assert saved.status == "imported" and saved.import_result["item_id"] == "i1"
    assert saved.review["verdict"] == "approved" and len(saved.tracks) == 2
    assert len(saved.events) == 8


def test_hold_then_confirm_with_edited_titles(tmp_path):
    calls = []
    st = steps(True, calls=calls)
    job = ingest("u", tmp_path / "w", steps=st, config=CFG)
    assert job.status == "needs_confirmation" and calls == ["plan", "review"]
    assert job.review["remaining_concerns"] == ["请确认第二首"] and job.reasons == ["track 2: check"]
    assert job.flagged_tracks == [2] and [t["flagged"] for t in job.tracks] == [False, True]
    assert load_job(tmp_path / "w").flagged_tracks == [2]
    # The page edits a title, then confirms (after a server restart: load from disk).
    p = Path(job.plan_path)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["tracks"][1]["title"] = "简单爱 (修改)"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    done = confirm(tmp_path / "w", steps=st, config=CFG)
    assert done.status == "imported" and done.confirmed and not done.forced_import
    assert done.tracks[1]["title"] == "简单爱 (修改)"
    assert calls == ["plan", "review", "cut", "import replace=False"]


def test_confirm_refuses_broken_plan(tmp_path):
    st = steps(True)
    job = ingest("u", tmp_path / "w", steps=st, config=CFG)
    d = json.loads(Path(job.plan_path).read_text(encoding="utf-8"))
    d["tracks"][0]["artist"] = ""
    Path(job.plan_path).write_text(json.dumps(d), encoding="utf-8")
    res = confirm(job, steps=st, config=CFG)
    assert res.status == "needs_confirmation" and "missing artist" in res.error


def test_on_unsure_import_marks_forced(tmp_path):
    job = ingest("u", tmp_path / "w", on_unsure="import", steps=steps(True), config=CFG)
    assert job.status == "imported" and job.forced_import


def test_duplicate_then_replace(tmp_path):
    calls = []
    st = steps(False, import_status="duplicate", calls=calls)
    job = ingest("u", tmp_path / "w", steps=st, config=CFG)
    assert job.status == "duplicate" and "已有" in job.message
    again = confirm(job, replace=True, steps=st, config=CFG)
    assert again.status == "imported" and calls[-1] == "import replace=True"


def test_no_import_and_discard(tmp_path):
    job = ingest("u", tmp_path / "w", do_import=False, steps=steps(False), config=CFG)
    assert job.status == "done" and Path(job.output_dir).is_dir()
    held = ingest("u", tmp_path / "h", steps=steps(True), config=CFG)
    gone = discard(held)
    assert gone.status == "discarded" and load_job(tmp_path / "h").status == "discarded"


def test_failure_becomes_result_and_callback_errors_ignored(tmp_path):
    def bad_callback(ev):
        raise RuntimeError("page went away")

    job = ingest("u", tmp_path / "w", bad_callback, steps=steps(False, plan_raises=SystemExit(2)), config=CFG)
    assert job.status == "failed" and "SystemExit" in job.error
    assert job.events[-1]["status"] == "failed" and job.events[-1]["message"].startswith("分析失败")
    assert load_job(tmp_path / "w").status == "failed"


def test_no_review_holds_tool_flagged_plan(tmp_path):
    calls = []
    job = ingest("u", tmp_path / "w", review=False, steps=steps(False, calls=calls), config=CFG)
    assert job.status == "needs_confirmation" and calls == ["plan"]
