"""Real pipeline output (copied from live runs into tests/fixtures/web/) through the web mapping,
and the fake runner's scripted plans checked against the same shapes."""

import json
import shutil
from pathlib import Path

import pytest

from bili_album import pipeline
from bili_album.web.fake import SCRIPTS, add_review, make_plan
from bili_album.web.runner import flagged_tracks, map_result, read_plan

FIX = Path(__file__).parent / "fixtures" / "web"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_live_job_json_maps_to_done_not_imported():
    state = load("ingest-fantasy.job.json")
    for res in (state, pipeline.IngestResult.from_dict(state)):  # dict from disk and the dataclass
        out = map_result(res)
        assert out.status == "done" and out.error == ""
        assert out.result == {"album_url": "", "imported": False, "confirmed": False, "folder": "",
                              "file_count": 10, "existing_files": 0}
        assert flagged_tracks({}, res) == set()  # this run predates flagged_tracks: nothing flagged


def test_live_job_json_statuses_map():
    state = load("ingest-fantasy.job.json")
    for raw, want in (("imported", "done"), ("imported_unconfirmed", "done"), ("duplicate", "duplicate"),
                      ("needs_confirmation", "needs_confirmation"), ("failed", "failed"), ("done", "done"),
                      ("discarded", "failed"), ("running", "failed")):
        assert map_result({**state, "status": raw}).status == want
    refused = {**state, "status": "needs_confirmation", "error": "plan has problems: tracks 3 and 4 overlap",
               "message": "方案仍有硬性问题，无法导入：tracks 3 and 4 overlap"}
    assert map_result(refused).error == "方案仍有硬性问题，无法导入：tracks 3 and 4 overlap"
    held = {**state, "status": "needs_confirmation", "error": "", "message": "需要确认：请检查曲目和问题后继续或放弃"}
    assert map_result(held).error == ""


def plan_view(tmp_path, name):
    wd = tmp_path / "work"
    wd.mkdir(exist_ok=True)
    p = wd / "plan.json"
    shutil.copy(FIX / name, p)
    return read_plan(wd, path=p), load(name)


def test_approved_album_with_skipped_edit(tmp_path):
    v, plan = plan_view(tmp_path, "jaychou-fantasy.plan.reviewed.json")
    assert v["album"] == {"title": "范特西", "artist": "周杰伦", "year": "2001", "kind": "album", "uploader": "",
                          "album_title": "范特西", "album_artist": "周杰伦"}
    assert len(v["tracks"]) == 10 and not any(t["flagged"] for t in v["tracks"])
    assert v["tracks"][0] == {"index": 1, "title": "爱在西元前", "artist": "周杰伦",
                              "duration": round(plan["tracks"][0]["end"] - plan["tracks"][0]["start"], 3),
                              "flagged": False}
    assert v["review"]["verdict"] == "approved" and v["review"]["edits"] == [] and v["review"]["rejected"] == []
    # tool_notes / skipped edits are not shown to the owner
    assert plan["review"]["tool_notes"] and "工具未执行" not in json.dumps(v, ensure_ascii=False)
    assert "cover" not in v  # the cover lives outside this work directory: never served


def test_held_derivative_single(tmp_path):
    v, plan = plan_view(tmp_path, "520am-derivative.plan.reviewed.json")
    assert plan["needs_review"] and plan["review"]["flagged_tracks"] == [1]
    assert v["album"] == {"title": "5:20AM", "artist": "soldier", "year": "2024", "kind": "single",
                          "uploader": "69岁牢二次元", "album_title": "5:20AM", "album_artist": "soldier"}
    assert [t["flagged"] for t in v["tracks"]] == [True]
    assert v["review"]["edits"] == [{"tracks": [1], "reason": plan["review"]["edits_applied"][0]["reason_zh"]}]
    assert v["review"]["reasons"] == plan["review"]["reasons"]
    assert v["review"]["remaining_concerns"] == plan["review"]["remaining_concerns"]


def test_edited_single_is_shown_by_its_song(tmp_path):
    v, plan = plan_view(tmp_path, "coldplay-yellow.plan.reviewed.json")
    assert plan["album"]["title"] == "Parachutes"
    assert v["album"]["title"] == "Yellow" and v["album"]["artist"] == "Coldplay" and v["album"]["kind"] == "single"
    assert v["album"]["uploader"] == ""  # not a derivative
    assert (v["album"]["album_artist"], v["album"]["album_title"]) == ("Coldplay", "Parachutes")
    assert v["review"]["verdict"] == "edited"
    assert v["review"]["edits"][0]["reason"].startswith("专辑由「Various Artists - Best 00s Pop Ever」改为")
    assert not any(t["flagged"] for t in v["tracks"])


def test_state_flags_win_over_plan(tmp_path):
    v, _ = plan_view(tmp_path, "jaychou-fantasy.plan.reviewed.json")
    wd = tmp_path / "work"
    state = {"flagged_tracks": [2], "tracks": [{"index": 7, "flagged": True}]}
    v = read_plan(wd, path=wd / "plan.json", state=state)
    assert [t["index"] for t in v["tracks"] if t["flagged"]] == [2, 7]


REAL_PLANS = ("jaychou-fantasy.plan.reviewed.json", "520am-derivative.plan.reviewed.json",
              "coldplay-yellow.plan.reviewed.json")


def _keys(plans, pick):
    out = set()
    for p in plans:
        for d in pick(p):
            out |= set(d)
    return out


@pytest.mark.parametrize("key", list(SCRIPTS))
def test_fake_plans_have_the_real_shape(tmp_path, key):
    s = SCRIPTS[key]
    if s.fail_plan:
        pytest.skip("fails before writing a plan")
    real = [load(n) for n in REAL_PLANS]
    fake = add_review(make_plan("https://www.bilibili.com/video/" + key, s, tmp_path), s)
    top_real = _keys(real, lambda p: [p]) - {"recognition", "timings", "derivative"}
    assert top_real <= set(fake), top_real - set(fake)
    for part in ("album", "source"):
        need = set.intersection(*(set(p[part]) for p in real)) - {"derivative", "derivative_cues", "derivative_info"}
        assert need <= set(fake[part]), (part, need - set(fake[part]))
    track_keys = set.intersection(*(set(t) for p in real for t in p["tracks"]))
    assert all(track_keys <= set(t) for t in fake["tracks"]), track_keys - set(fake["tracks"][0])
    review_keys = set.intersection(*(set(p["review"]) for p in real)) - {"usage", "timing"}
    assert review_keys <= set(fake["review"]), review_keys - set(fake["review"])
    edit_keys = {"op", "detail", "reason_zh", "tracks"}
    for e in fake["review"]["edits_applied"]:
        assert edit_keys <= set(e)
    for e in fake["review"]["edits_rejected"]:
        assert set(e) == {"edit", "why", "tracks", "reason_zh"}
    if s.derivative:
        real_d = load("520am-derivative.plan.reviewed.json")["derivative"]
        assert set(real_d) == set(fake["derivative"])
