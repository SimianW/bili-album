"""HTTP layer: routes, the Origin / Content-Type guard, cover path safety, no config leaks."""

import http.client
import json
import threading
import time

import pytest

from bili_album.web.jobs import JobManager
from bili_album.web.runner import FakeRunner, Outcome
from bili_album.web.server import make_server

CLEAN = "https://www.bilibili.com/video/BV14PVVzNE2R"
SINGLE = "https://www.bilibili.com/video/BV1Hx4y1z7Qm"
FAIL = "https://www.bilibili.com/video/BV1GJ411x7h7"
DUP = "https://www.bilibili.com/video/BV1zt411p7Cn"


@pytest.fixture
def site(tmp_path):
    mgr = JobManager(tmp_path / "web", FakeRunner(speed=1e6), start_worker=False)
    srv = make_server("127.0.0.1", 0, mgr)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, mgr
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None, raw=None):
    host, port = srv.server_address[:2]
    c = http.client.HTTPConnection(host, port, timeout=5)
    h = {"Content-Type": "application/json"} if body is not None or raw is not None else {}
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    ctype = r.getheader("Content-Type") or ""
    return r.status, (json.loads(payload) if ctype.startswith("application/json") else payload), r


def test_index_and_unknown_paths(site):
    srv, _ = site
    code, body, r = call(srv, "GET", "/")
    assert code == 200 and "导入到 Jellyfin".encode() in body
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    for p in ("/static/index.html", "/static/", "/../pyproject.toml", "/api", "/api/jobs/zzz", "/favicon.ico",
              "/src/bili_album/web/server.py"):
        assert call(srv, "GET", p)[0] == 404, p
    assert call(srv, "GET", "/api/jobs/0123456789ab")[0] == 404
    assert call(srv, "PUT", "/api/jobs", {})[0] == 405


def test_submit_list_and_actions(site):
    srv, mgr = site
    code, body, _ = call(srv, "POST", "/api/jobs", {"url": "BV14PVVzNE2R"})
    assert code == 201 and body["job"]["url"] == CLEAN and body["job"]["status"] == "queued"
    jid = body["job"]["id"]
    code, body, _ = call(srv, "POST", "/api/jobs", {"url": CLEAN + "?p=2"})
    assert code == 409 and body["existing_id"] == jid
    code, body, _ = call(srv, "POST", "/api/jobs", {"url": "https://example.com/video/BV14PVVzNE2R"})
    assert code == 400 and body["error"] == "这不像 B 站视频链接。"
    assert call(srv, "POST", "/api/jobs", {"link": CLEAN})[0] == 400
    code, body, _ = call(srv, "GET", "/api/jobs")
    assert code == 200 and isinstance(body["now"], float) and body["jobs"][0]["id"] == jid
    mgr.run_next()
    assert call(srv, "GET", f"/api/jobs/{jid}")[1]["job"]["status"] == "done"
    # wrong state
    code, body, _ = call(srv, "POST", f"/api/jobs/{jid}/confirm", {"tracks": []})
    assert code == 409 and body["error"]
    assert call(srv, "DELETE", f"/api/jobs/{jid}", {})[0] == 409
    assert call(srv, "POST", "/api/jobs/0123456789ab/retry", {})[0] == 404
    assert call(srv, "POST", f"/api/jobs/{jid}/explode", {})[0] == 404


def test_confirm_discard_retry_replace_keep_over_http(site):
    srv, mgr = site
    s = call(srv, "POST", "/api/jobs", {"url": SINGLE})[1]["job"]["id"]
    mgr.run_next()
    assert call(srv, "POST", f"/api/jobs/{s}/confirm", {"tracks": [{"index": 1, "title": "", "artist": "x"}]})[0] == 400
    assert call(srv, "POST", f"/api/jobs/{s}/confirm", {"tracks": [{"index": "1", "title": "a", "artist": "x"}]})[0] == 400
    code, body, _ = call(srv, "POST", f"/api/jobs/{s}/confirm",
                         {"tracks": [{"index": 1, "title": "5:20AM", "artist": "soldier"}]})
    assert code == 200 and body["job"]["status"] == "queued"
    mgr.run_next()
    assert mgr.get(s)["status"] == "done"

    s2 = call(srv, "POST", "/api/jobs", {"url": "https://www.bilibili.com/video/BV1Wb411m7Eq"})[1]["job"]["id"]
    mgr.run_next()
    assert call(srv, "POST", f"/api/jobs/{s2}/discard", {})[1] == {"ok": True}
    assert call(srv, "GET", f"/api/jobs/{s2}")[0] == 404

    f = call(srv, "POST", "/api/jobs", {"url": FAIL})[1]["job"]["id"]
    mgr.run_next()
    assert call(srv, "POST", f"/api/jobs/{f}/retry", {})[1]["job"]["status"] == "queued"
    mgr.run_next()
    assert call(srv, "DELETE", f"/api/jobs/{f}", {})[1] == {"ok": True}

    d = call(srv, "POST", "/api/jobs", {"url": DUP})[1]["job"]["id"]
    mgr.run_next()
    assert call(srv, "POST", f"/api/jobs/{d}/keep", {})[1]["job"]["kept"] is True
    d2 = call(srv, "POST", "/api/jobs", {"url": DUP})[1]["job"]["id"]
    mgr.run_next()
    assert call(srv, "POST", f"/api/jobs/{d2}/replace", {})[1]["job"]["status"] == "queued"


@pytest.mark.parametrize("headers, want", [
    ({"Content-Type": "text/plain"}, 415),
    ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ({"Content-Type": ""}, 415),
    ({"Origin": "https://evil.example"}, 403),
    ({"Origin": "null"}, 403),
    ({"Origin": "http://127.0.0.1:1"}, 403),
])
def test_guard_rejects_cross_site(site, headers, want):
    srv, mgr = site
    code, body, _ = call(srv, "POST", "/api/jobs", {"url": CLEAN}, headers=headers)
    assert code == want and body["error"]
    assert not mgr.jobs


def test_guard_allows_same_origin_and_no_origin(site):
    srv, mgr = site
    host = "%s:%d" % srv.server_address[:2]
    assert call(srv, "POST", "/api/jobs", {"url": CLEAN}, headers={"Origin": f"http://{host}"})[0] == 201
    assert call(srv, "POST", "/api/jobs", {"url": SINGLE},
                headers={"Content-Type": "application/json; charset=utf-8"})[0] == 201
    jid = next(iter(mgr.jobs))
    assert call(srv, "DELETE", f"/api/jobs/{jid}", {}, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(srv, "DELETE", f"/api/jobs/{jid}", None)[0] == 415
    assert jid in mgr.jobs


def test_bad_bodies(site):
    srv, _ = site
    assert call(srv, "POST", "/api/jobs", raw=b"{not json")[0] == 400
    assert call(srv, "POST", "/api/jobs", raw=b"[1,2]")[0] == 400
    assert call(srv, "POST", "/api/jobs", raw=b"\xff\xfe")[0] == 400
    assert call(srv, "POST", "/api/jobs", raw=b"x" * (300 * 1024))[0] == 413


def test_cover_only_from_inside_job_folder(site, tmp_path):
    srv, mgr = site
    jid = call(srv, "POST", "/api/jobs", {"url": "https://www.bilibili.com/video/BV1Ns411D7pE"})[1]["job"]["id"]
    mgr.run_next()
    code, body, r = call(srv, "GET", f"/api/jobs/{jid}/cover")
    assert code == 200 and r.getheader("Content-Type") == "image/svg+xml" and body.startswith(b"<svg")
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"\xff\xd8 not for you")
    other = call(srv, "POST", "/api/jobs", {"url": SINGLE})[1]["job"]["id"]
    for rel in ("../../secret.jpg", str(secret), "../job.json", f"../../{other}/job.json", "/etc/passwd", ""):
        mgr.jobs[jid]["cover"] = rel
        assert call(srv, "GET", f"/api/jobs/{jid}/cover")[0] == 404, rel
    # a symlink inside the work dir that points outside is refused too
    link = tmp_path / "web" / jid / "work" / "evil.jpg"
    link.symlink_to(secret)
    mgr.jobs[jid]["cover"] = "evil.jpg"
    assert call(srv, "GET", f"/api/jobs/{jid}/cover")[0] == 404
    # a file inside the job folder with a non-image type is not served
    (tmp_path / "web" / jid / "work" / "notes.txt").write_text("x")
    mgr.jobs[jid]["cover"] = "notes.txt"
    assert call(srv, "GET", f"/api/jobs/{jid}/cover")[0] == 404


def test_no_config_value_in_any_response(tmp_path, monkeypatch):
    values = {
        "BILI_ALBUM_LLM_BASE_URL": "http://proxy.internal:8317/v1",
        "BILI_ALBUM_LLM_MODEL": "model-name-xyz",
        "BILI_ALBUM_LLM_API_KEY": "sk-llm-key-0123456789",
        "BILI_ALBUM_JELLYFIN_URL": "http://127.0.0.1:8096",
        "BILI_ALBUM_JELLYFIN_API_KEY": "jf-key-abcdef987654",
    }
    env = tmp_path / "env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    load_config = pytest.importorskip("bili_album.config").load_config
    cfg = load_config(env, environ={})

    class LeakyPipeline(FakeRunner):
        """A pipeline that leaks every config value into its messages and results."""

        def secrets(self):
            from bili_album.web.runner import PipelineRunner
            return PipelineRunner(config=cfg).secrets()

        def start(self, url, workdir, emit):
            leak = " ".join(values.values())
            emit(stage="plan", state="running", message=leak)
            emit(stage="plan", state="done", album={"title": leak, "artist": leak, "year": "", "kind": "album",
                                                      "uploader": leak},
                 tracks=[{"index": 1, "title": leak, "artist": leak, "duration": 1}],
                 review={"reasons": [leak], "remaining_concerns": [leak], "edits": [{"index": 1, "reason": leak}]})
            return Outcome("failed", leak)

    mgr = JobManager(tmp_path / "web", LeakyPipeline(), start_worker=False)
    srv = make_server("127.0.0.1", 0, mgr)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        jid = call(srv, "POST", "/api/jobs", {"url": CLEAN})[1]["job"]["id"]
        mgr.run_next()
        blobs = [json.dumps(call(srv, "GET", p)[1], ensure_ascii=False)
                 for p in ("/api/jobs", f"/api/jobs/{jid}")]
        blobs.append(call(srv, "GET", "/")[1].decode())
        blobs.append((tmp_path / "web" / jid / "job.json").read_text())
        for b in blobs:
            for v in values.values():
                assert v not in b
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_every_interface(tmp_path):
    mgr = JobManager(tmp_path / "web", FakeRunner(), start_worker=False)
    for host in ("0.0.0.0", "::", ""):
        with pytest.raises(ValueError):
            make_server(host, 0, mgr)


def test_worker_thread_runs_queue(tmp_path):
    mgr = JobManager(tmp_path / "web", FakeRunner(speed=1e6))
    try:
        a = mgr.submit(CLEAN)["id"]
        b = mgr.submit(SINGLE)["id"]
        deadline = time.time() + 10
        while time.time() < deadline and mgr.get(b)["status"] != "needs_confirmation":
            time.sleep(0.02)
        assert mgr.get(a)["status"] == "done" and mgr.get(b)["status"] == "needs_confirmation"
    finally:
        mgr.stop()
