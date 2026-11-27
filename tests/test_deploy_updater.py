"""Offline tests for deploy/bili-album-update.py (real git + local fake GitHub API + fake uv/systemctl)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "deploy" / "bili-album-update.py"


def _load_updater():
    spec = importlib.util.spec_from_file_location("bili_album_update", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


U = _load_updater()


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, env=env).strip()


def _make_script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class FakeHTTP:
    """Serves both fake GitHub releases/latest and an optional fake web health endpoint."""

    def __init__(self):
        self.status = 200
        self.etag = '"etag-1"'
        self.payload: dict | None = {"tag_name": "v0.1.0", "draft": False, "prerelease": False}
        self.health_statuses: list[int] = [200]
        self.health_fn = None
        self.requests: list[dict] = []
        self.health_hits = 0
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                if self.path == "/health":
                    if parent.health_fn is not None:
                        code = parent.health_fn()
                    else:
                        idx = min(parent.health_hits, len(parent.health_statuses) - 1)
                        code = parent.health_statuses[idx]
                    parent.health_hits += 1
                    self.send_response(code)
                    self.end_headers()
                    return
                inm = self.headers.get("If-None-Match")
                parent.requests.append({"path": self.path, "if_none_match": inm, "ua": self.headers.get("User-Agent")})
                if parent.status != 200:
                    self.send_response(parent.status)
                    self.end_headers()
                    return
                if inm and inm == parent.etag:
                    self.send_response(304)
                    self.send_header("ETag", parent.etag)
                    self.end_headers()
                    return
                body = json.dumps(parent.payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", parent.etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def env(tmp_path: Path):
    origin = tmp_path / "origin.git"
    subprocess.check_call(["git", "init", "--bare", "-q", str(origin)])

    work = tmp_path / "work"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(work)])
    (work / "version.txt").write_text("0.0.1\n", encoding="utf-8")
    _git(work, "add", "version.txt")
    _git(work, "commit", "-q", "-m", "initial")
    c1 = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "HEAD:main")

    (work / "version.txt").write_text("0.1.0\n", encoding="utf-8")
    _git(work, "add", "version.txt")
    _git(work, "commit", "-q", "-m", "v0.1.0")
    c2 = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "-a", "v0.1.0", "-m", "Release v0.1.0", c2)
    _git(work, "push", "-q", "origin", "HEAD:main", "refs/tags/v0.1.0")

    live = tmp_path / "live"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(live)])
    _git(live, "checkout", "-q", "--detach", c1)
    _git(live, "tag", "-d", "v0.1.0")

    log_file = tmp_path / "calls.log"
    uv_bin = _make_script(tmp_path / "fake-uv", f'echo "uv $*" >> "{log_file}"\n')
    systemctl_bin = _make_script(tmp_path / "fake-systemctl", f'echo "systemctl $*" >> "{log_file}"\n')

    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    state_file = tmp_path / "state" / "updater.json"

    http = FakeHTTP()
    try:
        yield {
            "tmp": tmp_path,
            "origin": origin,
            "work": work,
            "live": live,
            "c1": c1,
            "c2": c2,
            "log": log_file,
            "uv": uv_bin,
            "systemctl": systemctl_bin,
            "data_dir": data_dir,
            "state_file": state_file,
            "gh": http,
        }
    finally:
        http.close()


def _cfg(env: dict, **overrides) -> U.UpdaterConfig:
    params = dict(
        repo="SimianW/bili-album",
        live_dir=env["live"],
        data_dir=env["data_dir"],
        state_file=env["state_file"],
        service="bili-album-web.service",
        health_url=f"{env['gh'].base_url}/health",
        uv_bin=env["uv"],
        systemctl_bin=env["systemctl"],
        api_base=env["gh"].base_url,
        startup_timeout=0.5,
        poll_interval=0.05,
    )
    params.update(overrides)
    return U.UpdaterConfig(**params)


def _write_job(data_dir: Path, job_id: str, status: str) -> Path:
    d = data_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / "job.json"
    f.write_text(json.dumps({"id": job_id, "status": status}), encoding="utf-8")
    return f


def test_updates_to_latest_release_and_uses_etag_on_next_check(env):
    res1 = U.run_update(_cfg(env))
    assert res1.action == "updated"
    assert res1.tag == "v0.1.0"
    assert res1.commit == env["c2"]
    assert res1.previous_commit == env["c1"]
    assert _git(env["live"], "rev-parse", "HEAD") == env["c2"]
    assert env["gh"].health_hits == 1
    assert env["log"].read_text(encoding="utf-8").splitlines() == [
        "uv sync --frozen",
        "systemctl --user restart bili-album-web.service",
        "systemctl --user is-active --quiet bili-album-web.service",
    ]

    res2 = U.run_update(_cfg(env))
    assert res2.action == "up_to_date"
    assert env["gh"].requests[1]["if_none_match"] == '"etag-1"'
    assert len(env["log"].read_text(encoding="utf-8").splitlines()) == 3


def test_defers_while_web_job_is_active_then_installs_on_304(env):
    job_file = _write_job(env["data_dir"], "0123456789ab", "running")

    res1 = U.run_update(_cfg(env))
    assert res1.action == "deferred"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["log"].exists()

    job_file.write_text(json.dumps({"id": "0123456789ab", "status": "done"}), encoding="utf-8")
    res2 = U.run_update(_cfg(env))
    assert res2.action == "updated"
    assert res2.commit == env["c2"]
    assert _git(env["live"], "rev-parse", "HEAD") == env["c2"]
    assert env["gh"].requests[1]["if_none_match"] == '"etag-1"'


def test_defers_and_rolls_back_without_restart_if_job_starts_during_sync(env):
    job_dir = env["data_dir"] / "abcdef012345"
    uv_starts_job = _make_script(
        env["tmp"] / "uv-starts-job",
        f'echo "uv $*" >> "{env["log"]}"\n'
        'case "$*" in *--offline*) exit 0 ;; esac\n'
        f'mkdir -p "{job_dir}"\n'
        f'printf \'{{"id":"abcdef012345","status":"running"}}\' > "{job_dir / "job.json"}"\n',
    )
    res1 = U.run_update(_cfg(env, uv_bin=uv_starts_job))
    assert res1.action == "deferred"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert env["log"].read_text(encoding="utf-8").splitlines() == [
        "uv sync --frozen",
        "uv sync --frozen --offline",
    ]
    assert json.loads(env["state_file"].read_text(encoding="utf-8"))["pending_deploy"] is None

    (job_dir / "job.json").write_text(json.dumps({"id": "abcdef012345", "status": "done"}), encoding="utf-8")
    res2 = U.run_update(_cfg(env))
    assert res2.action == "updated"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c2"]


def test_rolls_back_when_uv_sync_fails_and_skips_retry_until_new_commit(env):
    bad_uv = _make_script(
        env["tmp"] / "bad-uv",
        f'echo "uv $*" >> "{env["log"]}"\n'
        'case "$*" in *--offline*) exit 0 ;; *) echo "boom" >&2; exit 1 ;; esac\n',
    )
    with pytest.raises(U.UpdateError, match="uv sync failed"):
        U.run_update(_cfg(env, uv_bin=bad_uv))

    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert env["log"].read_text(encoding="utf-8").splitlines() == [
        "uv sync --frozen",
        "uv sync --frozen --offline",
    ]

    # Next tick skips the same failed commit instead of retrying endlessly.
    res2 = U.run_update(_cfg(env, uv_bin=bad_uv))
    assert res2.action == "skipped_failed"
    assert len(env["log"].read_text(encoding="utf-8").splitlines()) == 2


def test_rolls_back_when_service_not_active_and_stops_restart_loop(env):
    # Fail is-active only while the live checkout sits on the broken commit c2; succeed after rollback to c1.
    bad_systemctl = _make_script(
        env["tmp"] / "bad-systemctl",
        f'echo "systemctl $*" >> "{env["log"]}"\n'
        f'head=$(git -C "{env["live"]}" rev-parse HEAD)\n'
        f'case "$*" in *is-active*) [ "$head" = "{env["c2"]}" ] && exit 3 || exit 0 ;; *) exit 0 ;; esac\n',
    )
    with pytest.raises(U.UpdateError, match="not active after restart"):
        U.run_update(_cfg(env, systemctl_bin=bad_systemctl))

    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    calls_after_first = env["log"].read_text(encoding="utf-8").splitlines()
    assert calls_after_first == [
        "uv sync --frozen",
        "systemctl --user restart bili-album-web.service",
        "systemctl --user is-active --quiet bili-album-web.service",
        "uv sync --frozen --offline",
        "systemctl --user restart bili-album-web.service",
        "systemctl --user is-active --quiet bili-album-web.service",
    ]

    # Second run skips the failed commit without restarting the live service again.
    res2 = U.run_update(_cfg(env, systemctl_bin=bad_systemctl))
    assert res2.action == "skipped_failed"
    assert env["log"].read_text(encoding="utf-8").splitlines() == calls_after_first

    # Publishing a new commit clears failed_commit and installs cleanly.
    (env["work"] / "version.txt").write_text("0.1.1\n", encoding="utf-8")
    _git(env["work"], "add", "version.txt")
    _git(env["work"], "commit", "-q", "-m", "v0.1.1")
    c3 = _git(env["work"], "rev-parse", "HEAD")
    _git(env["work"], "tag", "v0.1.1", c3)
    _git(env["work"], "push", "-q", "origin", "HEAD:main", "refs/tags/v0.1.1")
    env["gh"].payload = {"tag_name": "v0.1.1", "draft": False, "prerelease": False}
    env["gh"].etag = '"etag-2"'

    res3 = U.run_update(_cfg(env))
    assert res3.action == "updated"
    assert res3.commit == c3
    assert json.loads(env["state_file"].read_text(encoding="utf-8"))["failed_commit"] is None


def test_polls_health_endpoint_and_rolls_back_when_never_ready(env):
    env["gh"].health_statuses = [503, 200]
    res = U.run_update(_cfg(env))
    assert res.action == "updated"
    assert env["gh"].health_hits == 2

    # Next release stays active in systemd but never returns HTTP 200 -> rollback restores c2 and succeeds on c2.
    (env["work"] / "version.txt").write_text("0.2.0\n", encoding="utf-8")
    _git(env["work"], "add", "version.txt")
    _git(env["work"], "commit", "-q", "-m", "v0.2.0")
    c3 = _git(env["work"], "rev-parse", "HEAD")
    _git(env["work"], "tag", "v0.2.0", c3)
    _git(env["work"], "push", "-q", "origin", "HEAD:main", "refs/tags/v0.2.0")
    env["gh"].payload = {"tag_name": "v0.2.0", "draft": False, "prerelease": False}
    env["gh"].etag = '"etag-3"'
    env["gh"].health_fn = lambda: 503 if (env["live"] / "version.txt").read_text(encoding="utf-8").strip() == "0.2.0" else 200
    env["gh"].health_hits = 0

    with pytest.raises(U.UpdateError, match="did not become ready"):
        U.run_update(_cfg(env, startup_timeout=0.15, poll_interval=0.05))
    assert _git(env["live"], "rev-parse", "HEAD") == env["c2"]
    state = json.loads(env["state_file"].read_text(encoding="utf-8"))
    assert state["pending_deploy"] is None
    assert state["failed_commit"] == c3


def test_recovers_interrupted_deploy_even_when_github_unreachable(env):
    # Simulate updater killed right after git checkout --detach c2, before uv sync / restart.
    _git(env["live"], "fetch", "origin", "refs/tags/v0.1.0:refs/tags/v0.1.0")
    _git(env["live"], "checkout", "-q", "--detach", env["c2"])
    U.save_state(
        env["state_file"],
        {
            "repo": "SimianW/bili-album",
            "etag": '"etag-1"',
            "tag": "v0.1.0",
            "installed_tag": None,
            "installed_commit": env["c1"],
            "pending_deploy": {"phase": "deploy", "tag": "v0.1.0", "target_commit": env["c2"], "previous_commit": env["c1"]},
        },
    )
    env["gh"].status = 500  # Recovery runs before GitHub polling.

    res = U.run_update(_cfg(env))
    assert res.action == "updated"
    assert res.previous_commit == env["c1"]
    assert not env["gh"].requests
    state = json.loads(env["state_file"].read_text(encoding="utf-8"))
    assert state["pending_deploy"] is None
    assert state["installed_commit"] == env["c2"]


def test_completes_interrupted_rollback_without_redeploying_failed_target(env):
    _git(env["live"], "fetch", "origin", "refs/tags/v0.1.0:refs/tags/v0.1.0")
    U.save_state(
        env["state_file"],
        {
            "repo": "SimianW/bili-album",
            "etag": '"etag-1"',
            "tag": "v0.1.0",
            "installed_tag": None,
            "installed_commit": env["c1"],
            "failed_commit": env["c2"],
            "failed_tag": "v0.1.0",
            "pending_deploy": {
                "phase": "rollback", "tag": "v0.1.0", "target_commit": env["c2"],
                "previous_commit": env["c1"], "restart_service": True,
            },
        },
    )
    res = U.run_update(_cfg(env))
    assert res.action == "rolled_back"
    assert res.commit == env["c1"]
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert json.loads(env["state_file"].read_text(encoding="utf-8"))["pending_deploy"] is None
    # Next tick sees failed_commit and skips v0.1.0 cleanly.
    assert U.run_update(_cfg(env)).action == "skipped_failed"


def test_rejects_malformed_health_url_before_mutating(env):
    with pytest.raises(U.UpdateError, match="invalid health URL"):
        U.run_update(_cfg(env, health_url="localhost/"))
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["log"].exists()
    assert not env["state_file"].exists()


def test_shipped_systemd_units_match_paths_port_and_rate_budget():
    deploy_dir = SCRIPT_PATH.parent
    web_unit = (deploy_dir / "bili-album-web.service").read_text(encoding="utf-8")
    upd_unit = (deploy_dir / "bili-album-update.service").read_text(encoding="utf-8")
    timer_unit = (deploy_dir / "bili-album-update.timer").read_text(encoding="utf-8")

    assert "WorkingDirectory=%h/Dev/bili-album-live" in web_unit
    assert "--live-dir %h/Dev/bili-album-live" in upd_unit
    assert "--data-dir %h/Dev/bili-album/.work/web" in web_unit
    assert "--data-dir %h/Dev/bili-album/.work/web" in upd_unit
    assert "--port 5191" in web_unit
    assert "--health-url http://127.0.0.1:5191/" in upd_unit
    assert "OnUnitActiveSec=3min" in timer_unit


def test_refuses_dirty_tracked_checkout(env):
    (env["live"] / "version.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(U.UpdateError, match="uncommitted tracked changes"):
        U.run_update(_cfg(env))
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["log"].exists()


def test_handles_repointed_tag_on_github(env):
    assert U.run_update(_cfg(env)).action == "updated"

    (env["work"] / "version.txt").write_text("0.1.0-hotfix\n", encoding="utf-8")
    _git(env["work"], "add", "version.txt")
    _git(env["work"], "commit", "-q", "-m", "hotfix")
    c3 = _git(env["work"], "rev-parse", "HEAD")
    _git(env["work"], "tag", "-f", "v0.1.0", c3)
    _git(env["work"], "push", "-q", "--force", "origin", "refs/tags/v0.1.0")
    env["gh"].etag = '"etag-2"'

    res = U.run_update(_cfg(env))
    assert res.action == "updated"
    assert res.commit == c3
    assert _git(env["live"], "rev-parse", "HEAD") == c3


@pytest.mark.parametrize("bad_tag", ["", "-v0.1.0", "v0.1.0:refs/heads/main", "v0.1.0 ..bad", "v0.1.0@{0}"])
def test_rejects_unsafe_tag_names(env, bad_tag):
    env["gh"].payload = {"tag_name": bad_tag, "draft": False, "prerelease": False}
    with pytest.raises(U.UpdateError, match="invalid"):
        U.run_update(_cfg(env))
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]


def test_no_release_404_is_clean_noop(env):
    env["gh"].status = 404
    res = U.run_update(_cfg(env))
    assert res.action == "no_release"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]


def test_dry_run_makes_no_changes(env):
    res = U.run_update(_cfg(env, dry_run=True))
    assert res.action == "would_update"
    assert res.tag == "v0.1.0"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["state_file"].exists()
    assert not env["log"].exists()


def test_skips_when_lock_is_held(env):
    lock_path = env["state_file"].with_name(f"{env['state_file'].name}.lock")
    with U.acquire_lock(lock_path) as held:
        assert held
        res = U.run_update(_cfg(env))
        assert res.action == "locked"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["gh"].requests
