"""Big files of finished jobs are deleted, the record and cover stay; nothing outside the job folder."""

import json
import os
import time
from pathlib import Path

import pytest

from bili_album.web.cleanup import clean_job, targets
from bili_album.web.fake import FakeRunner
from bili_album.web.jobs import JobManager

V = "https://www.bilibili.com/video/"


def make_job_dir(root: Path, jid="0123456789ab") -> Path:
    d = root / jid
    w = d / "work"
    (w / "out" / "Album").mkdir(parents=True)
    (w / "cache" / "http").mkdir(parents=True)
    files = {
        "work/source.m4a": 5000, "work/part1.m4a": 300, "work/part12.m4a": 200, "work/features.npz": 700,
        "work/out/Album/01-a.m4a": 1000, "work/out/Album/cover.jpg": 50, "work/cache/http/x.json": 40,
        # kept
        "job.json": 10, "work/job.json": 10, "work/plan.json": 10, "work/plan.unreviewed.json": 10,
        "work/cover.jpg": 20, "work/bili_cover.jpg": 20, "work/candidates.json": 10, "work/fake.json": 5,
        "work/notes.m4a.txt": 5, "work/separt1.m4a": 5,
    }
    for rel, n in files.items():
        (d / rel).write_bytes(b"x" * n)
    return d


KEPT = ["job.json", "work/job.json", "work/plan.json", "work/plan.unreviewed.json", "work/cover.jpg",
        "work/bili_cover.jpg", "work/candidates.json", "work/fake.json", "work/notes.m4a.txt", "work/separt1.m4a"]


def test_selection_and_bytes(tmp_path):
    d = make_job_dir(tmp_path)
    assert sorted(p.relative_to(d).as_posix() for p in targets(d)) == [
        "work/cache", "work/features.npz", "work/out", "work/part1.m4a", "work/part12.m4a", "work/source.m4a"]
    assert clean_job(d) == 5000 + 300 + 200 + 700 + 1000 + 50 + 40
    left = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    assert left == sorted(KEPT)
    assert clean_job(d) == 0  # idempotent


def test_symlinks_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "precious.m4a").write_bytes(b"y" * 999)
    (outside / "deep" / "keep.bin").write_bytes(b"z" * 10)
    d = make_job_dir(tmp_path / "jobs")
    w = d / "work"
    (w / "source.m4a").unlink()
    (w / "source.m4a").symlink_to(outside / "precious.m4a")   # a link in the list: remove the link only
    import shutil
    shutil.rmtree(w / "cache")
    (w / "cache").symlink_to(outside)                          # a linked directory: never entered
    (w / "out" / "escape").symlink_to(outside / "deep")        # a link inside a removed directory
    freed = clean_job(d)
    assert (outside / "precious.m4a").read_bytes() == b"y" * 999
    assert (outside / "deep" / "keep.bin").exists()
    assert not os.path.lexists(w / "source.m4a") and not os.path.lexists(w / "cache") and not (w / "out").exists()
    assert freed == 300 + 200 + 700 + 1000 + 50


def test_work_dir_that_is_a_symlink_is_left_alone(tmp_path):
    real = make_job_dir(tmp_path / "elsewhere")
    d = tmp_path / "jobs" / "0123456789ab"
    d.mkdir(parents=True)
    (d / "work").symlink_to(real / "work")
    assert targets(d) == [] and clean_job(d) == 0
    assert (real / "work" / "source.m4a").exists()


@pytest.fixture
def mgr(tmp_path):
    return JobManager(tmp_path / "web", FakeRunner(speed=1e6), start_worker=False)


def big(mgr, jid):
    w = mgr.workdir(jid)
    w.mkdir(parents=True, exist_ok=True)
    (w / "source.m4a").write_bytes(b"a" * 4096)
    (w / "features.npz").write_bytes(b"b" * 1024)
    return w


def run_with_big_files(mgr, url):
    """Run a scripted job; the big files appear while it runs, like the real planner's."""
    j = mgr.submit(url)
    big(mgr, j["id"])
    mgr.run_next()
    return j["id"]


def test_imported_job_is_cleaned_and_cover_still_served(mgr):
    jid = run_with_big_files(mgr, V + "BV1Ns411D7pE")  # imported, not yet shown by Jellyfin; has a cover
    j = mgr.get(jid)
    assert j["status"] == "done" and j["result"]["imported"] and not j["result"]["confirmed"]
    assert j["cleaned"]["bytes"] >= 4096 + 1024
    w = mgr.workdir(jid)
    assert not (w / "source.m4a").exists() and not (w / "out").exists() and not (w / "features.npz").exists()
    assert (w / "plan.json").exists() and (w / "job.json").exists()
    assert mgr.cover_path(jid) is not None and mgr.cover_path(jid).name == "cover.jpg"
    jid2 = run_with_big_files(mgr, V + "BV14PVVzNE2R")  # imported and confirmed
    assert mgr.get(jid2)["cleaned"] and not (mgr.workdir(jid2) / "source.m4a").exists()


def test_kept_duplicate_is_cleaned(mgr):
    jid = run_with_big_files(mgr, V + "BV1zt411p7Cn")
    assert mgr.get(jid)["status"] == "duplicate"
    assert (mgr.workdir(jid) / "source.m4a").exists()  # 「替换原有专辑」 still needs it
    mgr.keep(jid)
    assert mgr.get(jid)["cleaned"] and not (mgr.workdir(jid) / "source.m4a").exists()


@pytest.mark.parametrize("url, status", [(V + "BV1GJ411x7h7", "failed"), (V + "BV1Hx4y1z7Qm", "needs_confirmation"),
                                         (V + "BV1zt411p7Cn", "duplicate")])
def test_unfinished_jobs_keep_their_files(mgr, url, status):
    jid = run_with_big_files(mgr, url)
    j = mgr.get(jid)
    assert j["status"] == status and j["cleaned"] is None
    assert (mgr.workdir(jid) / "source.m4a").exists()


def test_clean_up_on_start(tmp_path):
    data = tmp_path / "web"
    finished = {"0000000000a1": {"status": "done", "result": {"imported": True}, "kept": False},
                "0000000000a2": {"status": "done", "result": None, "kept": True},
                "0000000000a3": {"status": "done", "result": {"imported": False}, "kept": False},  # cut only
                "0000000000a4": {"status": "failed", "result": None, "kept": False},
                "0000000000a5": {"status": "needs_confirmation", "result": None, "kept": False}}
    for jid, extra in finished.items():
        d = make_job_dir(data, jid)
        job = {"id": jid, "url": V + "BV14PVVzNE2R", "key": "BV14PVVzNE2R", "created": time.time(), "finished": 1.0,
               "stages": {s: "done" for s in ("plan", "review", "cut", "import")}, "tracks": [], "album": None,
               "version": 1, **extra}
        (d / "job.json").write_text(json.dumps(job))
    mgr = JobManager(data, FakeRunner(), start_worker=False)
    cleaned = {jid for jid in finished if mgr.get(jid).get("cleaned")}
    assert cleaned == {"0000000000a1", "0000000000a2"}
    for jid in finished:
        assert (data / jid / "work" / "source.m4a").exists() == (jid not in cleaned)
        assert (data / jid / "work" / "plan.json").exists()
    assert mgr.get("0000000000a1")["cleaned"]["bytes"] == 5000 + 300 + 200 + 700 + 1000 + 50 + 40
    # a second start does nothing more
    mgr2 = JobManager(data, FakeRunner(), start_worker=False)
    assert mgr2.get("0000000000a1")["cleaned"] == mgr.get("0000000000a1")["cleaned"]
