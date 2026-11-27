"""Job state machine with the fake runner, run step by step in the test thread."""

import json

import pytest

from bili_album.web.jobs import RESTART_MESSAGE, JobError, JobManager
from bili_album.web.runner import FakeRunner, Outcome

CLEAN = "https://www.bilibili.com/video/BV14PVVzNE2R"
EDITED = "https://www.bilibili.com/video/BV1sT421k7Ps"
SINGLE = "https://www.bilibili.com/video/BV1Hx4y1z7Qm"
FLAGGED = "https://www.bilibili.com/video/BV1Wb411m7Eq"
FAIL = "https://www.bilibili.com/video/BV1GJ411x7h7"
DUP = "https://www.bilibili.com/video/BV1zt411p7Cn"
UNCONFIRMED = "https://www.bilibili.com/video/BV1Ns411D7pE"


@pytest.fixture
def mgr(tmp_path):
    return JobManager(tmp_path / "web", FakeRunner(speed=1e6), start_worker=False)


def status(m, jid):
    return m.get(jid)["status"]


def test_queue_runs_in_submission_order(mgr):
    a = mgr.submit(CLEAN)
    b = mgr.submit(EDITED)
    c = mgr.submit(UNCONFIRMED)
    assert [j["status"] for j in (a, b, c)] == ["queued"] * 3
    assert [mgr.get(j["id"])["ahead"] for j in (a, b, c)] == [0, 1, 2]
    assert mgr.run_next()
    assert status(mgr, a["id"]) == "done" and status(mgr, b["id"]) == "queued"
    assert mgr.get(b["id"])["ahead"] == 0
    mgr.run_next()
    mgr.run_next()
    assert not mgr.run_next()
    done = mgr.get(b["id"])
    assert done["stages"] == {"plan": "done", "review": "done", "cut": "done", "import": "done"}
    assert done["album"]["title"] == "Starboy" and len(done["tracks"]) == 18
    assert done["review"]["edits"][0]["index"] == 16
    assert done["result"]["confirmed"] and done["result"]["album_url"].startswith("https://")
    assert mgr.get(c["id"])["result"]["confirmed"] is False


def test_same_video_not_queued_twice(mgr):
    a = mgr.submit(CLEAN)
    with pytest.raises(JobError) as e:
        mgr.submit(CLEAN)
    assert e.value.code == 409 and e.value.extra["existing_id"] == a["id"]
    mgr.run_next()
    assert mgr.submit(CLEAN)["id"] != a["id"]  # finished: a new import is allowed


def test_cancel_queued_and_running_not_cancellable(mgr, tmp_path):
    a = mgr.submit(CLEAN)
    b = mgr.submit(EDITED)
    mgr.remove(b["id"])
    assert not (tmp_path / "web" / b["id"]).exists()
    with pytest.raises(JobError) as e:
        mgr.get(b["id"])
    assert e.value.code == 404
    mgr.jobs[a["id"]]["status"] = "running"
    with pytest.raises(JobError) as e:
        mgr.remove(a["id"])
    assert e.value.code == 409


def test_hold_confirm_with_edits(mgr, tmp_path):
    j = mgr.submit(FLAGGED)
    mgr.run_next()
    held = mgr.get(j["id"])
    assert held["status"] == "needs_confirmation"
    assert held["stages"] == {"plan": "done", "review": "done", "cut": "held", "import": "pending"}
    assert [t["index"] for t in held["tracks"] if t["flagged"]] == [5]
    assert held["review"]["reasons"]
    with pytest.raises(JobError) as e:
        mgr.confirm(j["id"], [{"index": 99, "title": "x", "artist": "y"}])
    assert e.value.code == 400
    with pytest.raises(JobError):
        mgr.confirm(j["id"], [{"index": 5, "title": " ", "artist": "y"}])
    with pytest.raises(JobError):
        mgr.confirm(j["id"], [{"index": 5, "title": "a", "artist": "b"}, {"index": 5, "title": "a", "artist": "b"}])
    with pytest.raises(JobError):
        mgr.confirm(j["id"], "nope")
    q = mgr.confirm(j["id"], [{"index": 5, "title": "watch ", "artist": "Billie Eilish"},
                              {"index": 6, "title": "party favor", "artist": "B. Eilish"}])
    assert q["status"] == "queued"
    assert mgr.get(j["id"])["tracks"][5]["artist"] == "B. Eilish"
    mgr.run_next()
    done = mgr.get(j["id"])
    assert done["status"] == "done" and done["stages"]["import"] == "done"
    sent = json.loads((tmp_path / "web" / j["id"] / "work" / "edits.json").read_text())
    assert sent[0] == {"index": 5, "title": "watch", "artist": "Billie Eilish"}
    assert done["edited"] == [{"index": 6, "title": "party favor", "artist": "B. Eilish"}]
    with pytest.raises(JobError) as e:
        mgr.confirm(j["id"], [])
    assert e.value.code == 409


def test_confirmed_job_goes_ahead_of_later_submissions(mgr):
    held = mgr.submit(SINGLE)
    mgr.run_next()
    later = mgr.submit(CLEAN)
    mgr.confirm(held["id"], [])
    assert mgr.get(held["id"])["ahead"] == 0 and mgr.get(later["id"])["ahead"] == 1


def test_discard_removes_job(mgr, tmp_path):
    j = mgr.submit(SINGLE)
    mgr.run_next()
    assert status(mgr, j["id"]) == "needs_confirmation"
    mgr.discard(j["id"])
    assert not (tmp_path / "web" / j["id"]).exists()
    assert all(x["id"] != j["id"] for x in mgr.list())
    j2 = mgr.submit(CLEAN)
    with pytest.raises(JobError) as e:
        mgr.discard(j2["id"])
    assert e.value.code == 409


def test_failure_and_retry_reuses_workdir(mgr, tmp_path):
    j = mgr.submit(FAIL)
    mgr.run_next()
    f = mgr.get(j["id"])
    assert f["status"] == "failed"
    assert f["error"] == {"stage": "plan", "message": "B 站说这个视频不存在，或者在当前地区看不了。检查链接是否完整，或换一个视频。"}
    assert f["stages"]["plan"] == "failed"
    marker = tmp_path / "web" / j["id"] / "work" / "fake.json"
    assert marker.is_file()
    r = mgr.retry(j["id"])
    assert r["status"] == "queued" and r["error"] is None and r["stages"]["plan"] == "pending"
    mgr.run_next()
    assert status(mgr, j["id"]) == "failed" and marker.is_file()
    mgr.remove(j["id"])
    assert not (tmp_path / "web" / j["id"]).exists()


def test_runner_exception_is_a_failure_in_current_stage(tmp_path):
    class Boom(FakeRunner):
        def start(self, url, workdir, emit):
            emit(stage="plan", state="done")
            emit(stage="review", state="running", message="Sol 正在核对。")
            raise RuntimeError("model went away")
    m = JobManager(tmp_path / "web", Boom(), start_worker=False)
    j = m.submit(CLEAN)
    m.run_next()
    f = m.get(j["id"])
    assert f["status"] == "failed" and f["error"]["stage"] == "review"
    assert f["stages"] == {"plan": "done", "review": "failed", "cut": "pending", "import": "pending"}


def test_duplicate_replace(mgr):
    j = mgr.submit(DUP)
    mgr.run_next()
    d = mgr.get(j["id"])
    assert d["status"] == "duplicate" and d["result"]["existing_files"] == 12
    assert d["result"]["folder"] == "蛋堡 / 运河" and d["stages"]["import"] == "held"
    mgr.replace(j["id"])
    mgr.run_next()
    done = mgr.get(j["id"])
    assert done["status"] == "done" and done["result"]["confirmed"] and not done["kept"]


def test_duplicate_keep(mgr):
    j = mgr.submit(DUP)
    mgr.run_next()
    k = mgr.keep(j["id"])
    assert k["status"] == "done" and k["kept"] is True
    with pytest.raises(JobError) as e:
        mgr.replace(j["id"])
    assert e.value.code == 409


def test_restart_recovery(tmp_path):
    data = tmp_path / "web"
    m = JobManager(data, FakeRunner(speed=1e6), start_worker=False)
    done = m.submit(CLEAN)
    m.run_next()
    held = m.submit(SINGLE)
    m.run_next()
    running = m.submit(EDITED)
    queued = m.submit(UNCONFIRMED)
    # Simulate a crash in the middle of the review stage.
    r = m.jobs[running["id"]]
    m.queue.remove(running["id"])
    r["status"] = "running"
    r["stages"].update(plan="done", review="running")
    m._save(r)
    (data / "junk").mkdir()
    (data / "0123456789ab").mkdir()
    (data / "0123456789ab" / "job.json").write_text("{not json")

    m2 = JobManager(data, FakeRunner(speed=1e6), start_worker=False)
    assert status(m2, done["id"]) == "done"
    assert status(m2, held["id"]) == "needs_confirmation"
    for jid, stage in ((running["id"], "review"), (queued["id"], "plan")):
        j = m2.get(jid)
        assert j["status"] == "failed"
        assert j["error"] == {"stage": stage, "message": RESTART_MESSAGE}
    assert not m2.queue
    m2.retry(queued["id"])
    m2.run_next()
    assert status(m2, queued["id"]) == "done"


def test_list_groups_and_limits(mgr):
    for i in range(23):
        mgr.submit(f"https://www.bilibili.com/video/BV1xx411c{i:03d}")
        mgr.run_next()
    held = mgr.submit(SINGLE)
    mgr.run_next()
    q = mgr.submit(CLEAN)
    lst = mgr.list()
    assert lst[0]["id"] == held["id"]
    assert lst[1]["id"] == q["id"]
    assert sum(1 for j in lst if j["status"] == "done") == 20


def test_public_view_hides_internal_fields(mgr):
    j = mgr.submit(UNCONFIRMED)
    plain = mgr.submit(CLEAN)
    mgr.run_next()
    mgr.run_next()
    assert mgr.get(plain["id"])["has_cover"] is False and mgr.cover_path(plain["id"]) is None
    pub = mgr.get(j["id"])
    assert "cover" not in pub and "key" not in pub and "action" not in pub
    assert pub["has_cover"] is True
    assert mgr.cover_path(j["id"]).name == "cover.svg"


def test_secrets_are_scrubbed_everywhere(tmp_path):
    secret, base = "sk-test-SECRET-123456", "http://dev-proxy:8317/v1"

    class Leaky(FakeRunner):
        def secrets(self):
            return [secret, base]

        def start(self, url, workdir, emit):
            emit(stage="plan", state="running", message=f"calling {base} with {secret}")
            return Outcome("failed", f"HTTP 401 from {base}: bad key {secret}")
    m = JobManager(tmp_path / "web", Leaky(), start_worker=False)
    j = m.submit(CLEAN)
    m.run_next()
    blob = json.dumps(m.list(), ensure_ascii=False) + (tmp_path / "web" / j["id"] / "job.json").read_text()
    assert secret not in blob and base not in blob
    assert "***" in m.get(j["id"])["error"]["message"]
