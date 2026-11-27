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
    assert res.status == "needs_confirmation" and res.error_code == "plan_problems"
    assert res.error == "方案还有问题，暂时不能导入。第 1 首缺少艺人。请修改后再确认。" == res.message
    assert "missing artist" in res.error_detail


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
    assert job.status == "failed" and job.error_code == "unexpected" and "SystemExit" in job.error_detail
    assert job.error == "分析失败：处理时出现意外错误。请稍后重试；如果反复出现，请把日志交给维护者。"
    assert job.events[-1]["status"] == "failed" and job.events[-1]["message"] == job.error
    assert load_job(tmp_path / "w").status == "failed"


def test_no_review_holds_tool_flagged_plan(tmp_path):
    calls = []
    job = ingest("u", tmp_path / "w", review=False, steps=steps(False, calls=calls), config=CFG)
    assert job.status == "needs_confirmation" and calls == ["plan"]


def test_missing_video_message(tmp_path):
    from bili_album.bilibili import BilibiliError
    err = BilibiliError("https://api.bilibili.com/x/web-interface/view: code -404 啥都木有")
    job = ingest("u", tmp_path / "w", steps=steps(False, plan_raises=err), config=CFG)
    assert job.error == "分析失败：找不到这个视频：链接可能有误，或视频已被删除。请检查链接后再试。"
    assert job.error_code == "video_not_found" and "-404" in job.error_detail and "-404" not in job.message


def test_import_messages_unconfirmed_and_normalized(tmp_path):
    @dataclass
    class Imp:
        d: dict

        def to_dict(self):
            return self.d

    def imp_with(d):
        st = steps(False)
        st.import_ = lambda source, cover, replace, config: Imp(d)
        return st

    job = ingest("u", tmp_path / "a", steps=imp_with({"status": "imported_unconfirmed", "album": "范特西",
                                                       "reason_code": "jellyfin_unreachable",
                                                       "reason": "Jellyfin unreachable: refused"}), config=CFG)
    assert job.status == "imported_unconfirmed" and job.message.startswith("文件已放进音乐库，但连不上 Jellyfin")
    assert job.error_detail == "Jellyfin unreachable: refused"
    job = ingest("u", tmp_path / "b", steps=imp_with({"status": "imported", "album": "范特西", "jellyfin_track_count": 10,
                                                       "normalized": [{"field": "AlbumArtists"}]}), config=CFG)
    assert job.message == "已导入 Jellyfin：《范特西》10 首；已把 Jellyfin 里的繁体歌手名改回简体"
    job = ingest("u", tmp_path / "c", steps=imp_with({"status": "imported", "album": "范特西", "jellyfin_track_count": 10,
                                                       "normalize_error": "JellyfinError: 500",
                                                       "warnings": ["could not correct"]}), config=CFG)
    assert "仍可能显示繁体" in job.message and job.warnings == ["could not correct"]


def test_rejected_edit_reason_is_fully_chinese_in_job(tmp_path):
    st = steps(True)

    def review(plan_path, config):
        d = json.loads(plan_path.read_text(encoding="utf-8"))
        d["review"] = {"verdict": "needs_human", "edits_applied": [], "flagged_tracks": [1], "tool_notes": [
            "工具无法核实“修改曲名/艺人（第 1 首）”，未采纳，需要人工确认（title 'X' not found in the catalog "
            "tracklist, recognition, uploader text or a catalog lookup）。"],
            "edits_rejected": [{"edit": {"op": "relabel_track", "track": 1, "title": "X"},
                                "why": "title 'X' not found in the catalog tracklist, recognition, uploader text or "
                                       "a catalog lookup", "reason_zh": "old (English)"}],
            "error": "ModelError: HTTP 504 from proxy"}
        plan_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    st.review = review
    job = ingest("u", tmp_path / "w", steps=st, config=CFG)
    r = job.review["edits_rejected"][0]["reason_zh"]
    assert r == "工具无法核实“修改曲名/艺人（第 1 首）”，未采纳，需要人工确认：在专辑曲目表、音频识别、投稿文字和曲库里都找不到这个名字。"
    assert job.review["tool_notes"] == [r]
    assert job.review["error_zh"].startswith("模型复核失败") and "已跳过复核" in job.review["error_zh"]
