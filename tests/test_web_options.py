"""Boundary refinement options sent with a link: checked, stored on the job, passed to the planner."""

import json
import threading
from pathlib import Path

import pytest

from bili_album.config import Config
from bili_album.refine import RefineParams
from bili_album.web.fake import FakeRunner
from bili_album.web.jobs import JobError, JobManager
from bili_album.web.runner import REFINE_LIMITS, Outcome, PipelineRunner, clean_options, plan_options, refine_fields
from bili_album.web.server import load_index, make_server

URL = "https://www.bilibili.com/video/BV14PVVzNE2R"


def test_clean_options_accepts_what_the_page_sends():
    assert clean_options(None) == {} and clean_options({}) == {}
    assert clean_options({"refine": False}) == {"refine": False}
    got = clean_options({"refine": True, "search_ms": 800, "silence_db": -45.5, "deep_silence_db": -70,
                         "pre_roll_ms": 0})
    assert got == {"refine": True, "refine_options": {"search_ms": 800, "silence_db": -45.5,
                                                       "deep_silence_db": -70.0, "pre_roll_ms": 0}}
    assert isinstance(got["refine_options"]["deep_silence_db"], float)
    assert clean_options({"search_ms": 3000.0}) == {"refine_options": {"search_ms": 3000}}


@pytest.mark.parametrize("raw, words", [
    ("fast", "格式"),
    ({"hop_ms": 1}, "不认识"),
    ({"refine": "yes"}, "开或关"),
    ({"search_ms": "800"}, "数字"),
    ({"search_ms": True}, "数字"),
    ({"search_ms": 800.5}, "数字"),
    ({"search_ms": float("nan")}, "数字"),
    ({"search_ms": 100}, "200 到 5000"),
    ({"pre_roll_ms": 501}, "0 到 500"),
    ({"silence_db": 3}, "-80 到 0"),
    ({"silence_db": -60, "deep_silence_db": -50}, "不能高于"),
    ({"deep_silence_db": -30}, "不能高于"),       # against the default first threshold (-42)
])
def test_clean_options_refuses_bad_values(raw, words):
    with pytest.raises(ValueError, match=words):
        clean_options(raw)


def test_plan_options_keeps_only_planner_keys():
    assert plan_options({"refine": False, "refine_options": {"search_ms": 900}, "other": 1}) == \
        {"refine": False, "refine_options": {"search_ms": 900}}


class Recorder:
    def __init__(self):
        self.calls = []

    def start(self, url, workdir, emit, options=None):
        self.calls.append(options)
        return Outcome("done")

    def secrets(self):
        return []


def test_jobs_store_options_and_hand_them_to_the_runner(tmp_path):
    rec = Recorder()
    m = JobManager(tmp_path / "web", rec, start_worker=False)
    j = m.submit(URL, options={"search_ms": 900})
    assert j["options"] == {"refine_options": {"search_ms": 900}}
    m.run_next()
    assert rec.calls == [{"refine_options": {"search_ms": 900}}]
    # Saved with the job, so a restart (and retry) keeps them.
    saved = json.loads((tmp_path / "web" / j["id"] / "job.json").read_text(encoding="utf-8"))
    assert saved["options"] == {"refine_options": {"search_ms": 900}}


def test_jobs_without_options_give_the_runner_none(tmp_path):
    rec = Recorder()
    m = JobManager(tmp_path / "web", rec, start_worker=False)
    m.submit(URL)
    m.run_next()
    assert rec.calls == [{}]


def test_bad_options_are_a_400_and_queue_nothing(tmp_path):
    m = JobManager(tmp_path / "web", Recorder(), start_worker=False)
    with pytest.raises(JobError) as e:
        m.submit(URL, options={"search_ms": 99999})
    assert e.value.code == 400 and "5000" in e.value.message
    assert m.list() == []


def test_pipeline_runner_passes_plan_options(tmp_path):
    seen = {}

    class Api:
        def ingest(self, url, workdir, on_event, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")

    r = PipelineRunner(config=Config(), api=Api())
    with pytest.raises(RuntimeError):
        r.start(URL, tmp_path, lambda **k: None, {"refine": False, "refine_options": {"pre_roll_ms": 20}})
    assert seen["plan_options"] == {"refine": False, "refine_options": {"pre_roll_ms": 20}}
    seen.clear()
    with pytest.raises(RuntimeError):
        r.start(URL, tmp_path, lambda **k: None)
    assert "plan_options" not in seen


def test_pipeline_plan_step_builds_plan_options(tmp_path, monkeypatch):
    from bili_album import pipeline, plan

    got = {}
    monkeypatch.setattr(plan, "make_plan", lambda url, po: got.setdefault("po", po) and Path("plan.json"))
    pipeline._plan_default(URL, tmp_path, {"refine": False, "refine_options": {"search_ms": 700}, "junk": 1})
    po = got["po"]
    assert po.refine is False and po.refine_options == {"search_ms": 700}


def test_http_submit_with_options(tmp_path):
    import http.client

    mgr = JobManager(tmp_path / "web", FakeRunner(speed=1e6), start_worker=False)
    srv = make_server("127.0.0.1", 0, mgr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(body):
            c = http.client.HTTPConnection(*srv.server_address[:2], timeout=5)
            c.request("POST", "/api/jobs", body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            out = r.status, json.loads(r.read())
            c.close()
            return out
        code, body = post({"url": URL, "options": {"pre_roll_ms": 600}})
        assert code == 400 and "0 到 500" in body["error"]
        code, body = post({"url": URL, "options": {"refine": False}})
        assert code == 201 and body["job"]["options"] == {"refine": False}
    finally:
        srv.shutdown()
        srv.server_close()


def test_page_menu_fields_come_from_the_refiner():
    fields = {f["name"]: f for f in refine_fields()}
    p = RefineParams()
    assert {n: f["default"] for n, f in fields.items()} == {n: getattr(p, n) for n in fields}
    assert set(fields) == set(REFINE_LIMITS) and fields["search_ms"]["int"] and not fields["silence_db"]["int"]


def test_page_carries_the_menu_with_its_fields():
    html = load_index().decode("utf-8")
    assert "<!--refine-menu-->" not in html and "__REFINE_FIELDS__" not in html
    assert 'id="refm"' in html and "const BiliRefine" in html and "BiliRefine.options()" in html
    assert html.index("const BiliRefine") < html.index("options = BiliRefine.options()")   # defined before the page uses it
    assert '"name": "search_ms"' in html and '"label": "搜索范围"' in html and '"default": 1500' in html


def test_review_replan_keeps_the_refinement_settings(tmp_path, monkeypatch):
    # Review finding: a re-plan asked for by the review rebuilt PlanOptions without them, so a link
    # sent with refinement off (or a custom pre-roll) was re-planned with the defaults.
    from bili_album import plan as plan_mod
    from bili_album.review import default_replanner
    seen = []

    def fake_make_plan(url, po):
        seen.append(po)
        out = tmp_path / "plan.json"
        out.write_text("{}", encoding="utf-8")
        return out
    monkeypatch.setattr(plan_mod, "make_plan", fake_make_plan)
    plan = {"source": {"url": URL, "text_hints": True, "refine": False, "refine_options": {"pre_roll_ms": 120}}}
    default_replanner(tmp_path / "plan.json")(plan, {"album_hint": "x - y"})
    assert seen[0].refine is False and seen[0].refine_options == {"pre_roll_ms": 120}
    plan["source"] = {"url": URL, "text_hints": True}          # a plan written before refinement existed
    default_replanner(tmp_path / "plan.json")(plan, {})
    assert seen[1].refine is True and seen[1].refine_options == {}


def test_clean_options_refuses_an_overflowing_number():
    for v in (float("inf"), 10 ** 400):            # a huge JSON integer cannot become a float either
        with pytest.raises(ValueError):
            clean_options({"search_ms": v})
