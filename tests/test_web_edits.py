"""The owner's edits as the pipeline's confirm() receives them: album follows, Simplified script, no blanks."""

import json
import shutil
from pathlib import Path

import pytest

from bili_album.web.fake import FakeRunner
from bili_album.web.jobs import JobError, JobManager
from bili_album.web.runner import apply_edits

V = "https://www.bilibili.com/video/"
SINGLE, EILISH = V + "BV1Hx4y1z7Qm", V + "BV1Wb411m7Eq"
FIX = Path(__file__).parent / "fixtures" / "web"


@pytest.fixture
def run(tmp_path):
    """Hold a scripted job, confirm it with edits, return (plan seen by confirm(), finished job)."""
    runner = FakeRunner(speed=1e6)
    mgr = JobManager(tmp_path / "web", runner, start_worker=False)
    seen = {}
    real_confirm = runner._api_obj.confirm

    def spy(workdir, on_event, **kw):
        seen["plan"] = json.loads((Path(workdir) / "plan.json").read_text(encoding="utf-8"))
        return real_confirm(workdir, on_event, **kw)
    runner._api_obj.confirm = spy

    def go(url, edits):
        j = mgr.submit(url)
        mgr.run_next()
        assert mgr.get(j["id"])["status"] == "needs_confirmation"
        mgr.confirm(j["id"], edits)
        mgr.run_next()
        return seen["plan"], mgr.get(j["id"])
    go.mgr = mgr
    return go


def test_single_artist_becomes_album_artist(run):
    plan, job = run(SINGLE, [{"index": 1, "title": "5:20AM", "artist": "69岁牢二次元"}])
    t = plan["tracks"][0]
    assert (t["artist"], t["album_artist"], plan["album"]["artist"]) == ("69岁牢二次元",) * 3
    assert (t["album"], plan["album"]["title"]) == ("5:20AM", "5:20AM")
    assert job["status"] == "done" and job["result"]["folder"] == "69岁牢二次元 / 5:20AM"
    assert (job["album"]["album_artist"], job["album"]["album_title"]) == ("69岁牢二次元", "5:20AM")


def test_single_title_renames_album_named_after_it(run):
    plan, job = run(SINGLE, [{"index": 1, "title": "5:20AM（司凤版）", "artist": "soldier"}])
    t = plan["tracks"][0]
    assert (t["title"], t["album"], plan["album"]["title"]) == ("5:20AM（司凤版）",) * 3
    assert (t["album_artist"], plan["album"]["artist"]) == ("soldier", "soldier")
    assert job["result"]["folder"] == "soldier / 5:20AM（司凤版）"


def test_single_on_its_album_keeps_album_title(tmp_path):
    p = tmp_path / "plan.json"
    shutil.copy(FIX / "coldplay-yellow.plan.reviewed.json", p)  # song Yellow on album Parachutes
    apply_edits(p, [{"index": 1, "title": "Yellow (Live)", "artist": "Coldplay & Friends"}])
    plan = json.loads(p.read_text())
    t = plan["tracks"][0]
    assert (t["title"], t["album"], plan["album"]["title"]) == ("Yellow (Live)", "Parachutes", "Parachutes")
    assert (t["artist"], t["album_artist"], plan["album"]["artist"]) == ("Coldplay & Friends",) * 3


def test_album_artist_follows_only_when_every_track_changes(run):
    edits = [{"index": i, "title": t, "artist": "比莉·艾利什"} for i, t in enumerate(
        ["COPYCAT", "idontwannabeyouanymore", "my boy", "ocean eyes", "watch", "party favor", "bellyache", "&burn"], 1)]
    plan, job = run(EILISH, edits)
    assert plan["album"]["artist"] == "比莉·艾利什"
    assert {t["album_artist"] for t in plan["tracks"]} == {"比莉·艾利什"}
    assert job["result"]["folder"] == "比莉·艾利什 / dont smile at me"


def test_one_featured_artist_does_not_rename_album_artist(run):
    edits = [{"index": 5, "title": "watch", "artist": "Billie Eilish & Khalid"}]
    plan, _ = run(EILISH, edits)
    assert plan["tracks"][4]["artist"] == "Billie Eilish & Khalid"
    assert plan["album"]["artist"] == "Billie Eilish"
    assert {t["album_artist"] for t in plan["tracks"]} == {"Billie Eilish"}


def test_album_artist_stays_when_a_track_had_another_artist(tmp_path):
    p = tmp_path / "plan.json"
    shutil.copy(FIX / "jaychou-fantasy.plan.reviewed.json", p)
    plan = json.loads(p.read_text())
    plan["tracks"][2]["artist"] = "周杰伦, 某嘉宾"  # a featured track: not every old artist was the album artist
    p.write_text(json.dumps(plan, ensure_ascii=False))
    apply_edits(p, [{"index": t["index"], "title": t["title"], "artist": "Jay Chou"} for t in plan["tracks"]])
    after = json.loads(p.read_text())
    assert {t["artist"] for t in after["tracks"]} == {"Jay Chou"}
    assert after["album"]["artist"] == "周杰伦" and {t["album_artist"] for t in after["tracks"]} == {"周杰伦"}


def test_blank_title_or_artist_is_refused(run):
    mgr = run.mgr
    j = mgr.submit(SINGLE)
    mgr.run_next()
    for bad in ({"index": 1, "title": "  ", "artist": "x"}, {"index": 1, "title": "x", "artist": ""}):
        with pytest.raises(JobError) as e:
            mgr.confirm(j["id"], [bad])
        assert e.value.code == 400 and e.value.message == "歌名和歌手不能为空。"
    assert mgr.get(j["id"])["status"] == "needs_confirmation"


def test_edited_values_are_trimmed_and_simplified(run):
    plan, job = run(SINGLE, [{"index": 1, "title": "  夜曲（現場版） ", "artist": " 周傑倫 "}])
    t = plan["tracks"][0]
    assert (t["title"], t["artist"], plan["album"]["artist"]) == ("夜曲（现场版）", "周杰伦", "周杰伦")
    assert job["tracks"][0]["title"] == "夜曲（现场版）" and job["edited"][0]["artist"] == "周杰伦"


def test_japanese_and_latin_are_left_alone(run):
    plan, _ = run(SINGLE, [{"index": 1, "title": "マリーゴールド", "artist": "あいみょん"}])
    assert (plan["tracks"][0]["title"], plan["tracks"][0]["artist"]) == ("マリーゴールド", "あいみょん")
