"""PipelineRunner against the real ``bili_album.pipeline`` with its stages replaced by fakes."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

pipeline = pytest.importorskip("bili_album.pipeline")

from bili_album.config import Config  # noqa: E402
from bili_album.web.jobs import JobManager  # noqa: E402
from bili_album.web.runner import PipelineRunner, map_event, map_result  # noqa: E402

URL = "https://www.bilibili.com/video/BV14PVVzNE2R"


@dataclass
class FakeApply:
    folder: Path
    files: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def make_steps(*, needs_review=False, import_status="imported", log=None):
    log = log if log is not None else []

    def plan(url, workdir, opts):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "cover.jpg").write_bytes(b"\xff\xd8 jpeg")
        p = workdir / "plan.json"
        tracks = [{"index": i, "start": (i - 1) * 100.0, "end": i * 100.0 - 1, "title": f"T{i}", "artist": "A",
                   "album": "Alb", "album_artist": "A", "cover_path": str(workdir / "cover.jpg"), "confidence": 0.9}
                  for i in (1, 2, 3)]
        p.write_text(json.dumps({"kind": "compilation", "album": {"title": "Alb", "artist": "A", "year": "2001-05-01",
                                                                  "cover_path": str(workdir / "cover.jpg")},
                                 "source": {"uploader": "up", "duration": 300}, "tracks": tracks,
                                 "needs_review": False, "review_reasons": []}))
        return p

    def review(plan_path, config):
        plan = json.loads(plan_path.read_text())
        plan["needs_review"] = needs_review
        if needs_review:
            plan["review_reasons"] = ["track 2 'T2': low confidence 0.40"]
        plan["review"] = {"verdict": "needs_human" if needs_review else "edited", "reasons": ["第 3 首按曲库改名。"],
                          "remaining_concerns": ["第 2 首的识曲把握不高。"] if needs_review else [],
                          "edits_applied": [] if needs_review else [{"op": "relabel_track", "track": 3,
                                                                     "detail": "track 3 relabelled"}]}
        plan_path.write_text(json.dumps(plan))
        return SimpleNamespace(review=plan["review"])

    def cut(plan_path, outdir):
        plan = json.loads(plan_path.read_text())
        log.append([(t["title"], t["artist"]) for t in plan["tracks"]])
        outdir.mkdir(parents=True)
        files = []
        for t in plan["tracks"]:
            f = outdir / f"{t['index']:02d}-{t['title']}.m4a"
            f.write_bytes(b"")
            files.append(f)
        return FakeApply(outdir, files)

    def import_(source, cover, replace, config):
        st = "imported" if replace else import_status
        return {"status": st, "album": "Alb", "album_artist": "A", "dest": "/lib/A/Alb",
                "files": ["01-T1.m4a", "02-T2.m4a", "03-T3.m4a"],
                "existing": ["x.m4a", "y.m4a"] if st == "duplicate" else [],
                "web_url": "https://jf.example/web/#/details?id=abc" if st == "imported" else ""}

    return pipeline.Steps(plan=plan, review=review, cut=cut, import_=import_)


class StubModule:
    """``bili_album.pipeline`` with the stage functions injected."""

    def __init__(self, steps):
        self.steps = steps

    def ingest(self, url, workdir, on_event, **kw):
        return pipeline.ingest(url, workdir, on_event, steps=self.steps, **kw)

    def confirm(self, workdir, on_event, **kw):
        return pipeline.confirm(workdir, on_event, steps=self.steps, **kw)

    def discard(self, workdir):
        return pipeline.discard(workdir)


CFG = Config(llm_base_url="http://proxy.internal/v1", llm_model="m-1234", llm_api_key="sk-secret-0000",
             jellyfin_url="http://127.0.0.1:8096", jellyfin_api_key="jf-secret-1111")


def manager(tmp_path, monkeypatch, steps):
    monkeypatch.setattr(PipelineRunner, "_api", staticmethod(lambda: StubModule(steps)))
    return JobManager(tmp_path / "web", PipelineRunner(config=CFG), start_worker=False)


def test_clean_run_maps_everything(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch, make_steps())
    jid = m.submit(URL)["id"]
    m.run_next()
    j = m.get(jid)
    assert j["status"] == "done", j
    assert j["stages"] == {s: "done" for s in ("plan", "review", "cut", "import")}
    assert j["album"] == {"title": "Alb", "artist": "A", "year": "2001", "kind": "album", "uploader": ""}
    assert [t["duration"] for t in j["tracks"]] == [99.0, 99.0, 99.0]
    assert j["review"]["edits"] == [{"index": 3, "reason": "track 3 relabelled"}]
    assert j["result"] == {"album_url": "https://jf.example/web/#/details?id=abc", "confirmed": True,
                           "folder": "A / Alb", "file_count": 3, "existing_files": 0}
    assert m.cover_path(jid).name == "cover.jpg"


def test_hold_confirm_writes_edits_into_plan(tmp_path, monkeypatch):
    log = []
    m = manager(tmp_path, monkeypatch, make_steps(needs_review=True, log=log))
    jid = m.submit(URL)["id"]
    m.run_next()
    j = m.get(jid)
    assert j["status"] == "needs_confirmation"
    assert j["stages"]["cut"] == "held"
    assert [t["index"] for t in j["tracks"] if t["flagged"]] == [2]
    m.confirm(jid, [{"index": 2, "title": "Real Two", "artist": "B"}])
    m.run_next()
    assert m.get(jid)["status"] == "done"
    assert log[-1] == [("T1", "A"), ("Real Two", "B"), ("T3", "A")]


def test_duplicate_then_replace_and_unconfirmed(tmp_path, monkeypatch):
    m = manager(tmp_path, monkeypatch, make_steps(import_status="duplicate"))
    jid = m.submit(URL)["id"]
    m.run_next()
    j = m.get(jid)
    assert j["status"] == "duplicate" and j["result"]["existing_files"] == 2 and j["result"]["folder"] == "A / Alb"
    m.replace(jid)
    m.run_next()
    assert m.get(jid)["result"]["confirmed"] is True

    m2 = manager(tmp_path / "b", monkeypatch, make_steps(import_status="imported_unconfirmed"))
    jid = m2.submit(URL)["id"]
    m2.run_next()
    j = m2.get(jid)
    assert j["status"] == "done" and j["result"]["confirmed"] is False and j["result"]["album_url"] == ""


def test_stage_failure_is_scrubbed(tmp_path, monkeypatch):
    steps = make_steps()

    def bad_review(plan_path, config):
        raise RuntimeError(f"401 from {CFG.llm_base_url} key {CFG.llm_api_key}")
    steps.review = bad_review
    m = manager(tmp_path, monkeypatch, steps)
    jid = m.submit(URL)["id"]
    m.run_next()
    j = m.get(jid)
    assert j["status"] == "failed" and j["error"]["stage"] == "review"
    blob = json.dumps(m.list()) + (tmp_path / "web" / jid / "job.json").read_text()
    for v in CFG.secrets() + [CFG.llm_base_url]:
        assert v not in blob


def test_map_helpers():
    assert map_event({"stage": "cut", "status": "started", "message": "正在切"}) == \
        {"stage": "cut", "state": "running", "message": "正在切"}
    assert map_event({"stage": "x", "status": "started"}) == {}
    assert map_result({"status": "weird"}).status == "failed"
    assert map_result(SimpleNamespace(status="failed", message="分析失败：x", error="E", import_result=None)).error \
        == "分析失败：x"
