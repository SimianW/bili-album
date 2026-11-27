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


class FakeGitHub:
    def __init__(self):
        self.status = 200
        self.etag = '"etag-1"'
        self.payload: dict | None = {"tag_name": "v0.1.0", "draft": False, "prerelease": False}
        self.requests: list[dict] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                inm = self.headers.get("If-None-Match")
                parent.requests.append({"path": self.path, "if_none_match": inm, "ua": self.headers.get("User-Agent")})
                if parent.status == 404:
                    self.send_response(404)
                    self.end_headers()
                    return
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
    # Remove local tag so tests exercise fetching the tag from origin.
    _git(live, "tag", "-d", "v0.1.0")

    log_file = tmp_path / "calls.log"
    uv_bin = _make_script(tmp_path / "fake-uv", f'echo "uv $*" >> "{log_file}"\n')
    systemctl_bin = _make_script(tmp_path / "fake-systemctl", f'echo "systemctl $*" >> "{log_file}"\n')

    data_dir = tmp_path / "web-data"
    data_dir.mkdir()
    state_file = tmp_path / "state" / "updater.json"

    gh = FakeGitHub()
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
            "gh": gh,
        }
    finally:
        gh.close()


def _cfg(env: dict, **overrides) -> U.UpdaterConfig:
    params = dict(
        repo="SimianW/bili-album",
        live_dir=env["live"],
        data_dir=env["data_dir"],
        state_file=env["state_file"],
        service="bili-album-web.service",
        uv_bin=env["uv"],
        systemctl_bin=env["systemctl"],
        api_base=env["gh"].base_url,
        startup_wait=0.0,
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
    assert (env["live"] / "version.txt").read_text(encoding="utf-8") == "0.1.0\n"
    assert env["log"].read_text(encoding="utf-8").splitlines() == [
        "uv sync --frozen",
        "systemctl --user restart bili-album-web.service",
        "systemctl --user is-active --quiet bili-album-web.service",
    ]

    # Second run sends If-None-Match, gets 304, and does no git/uv/systemctl work.
    res2 = U.run_update(_cfg(env))
    assert res2.action == "up_to_date"
    assert res2.tag == "v0.1.0"
    assert env["gh"].requests[1]["if_none_match"] == '"etag-1"'
    assert len(env["log"].read_text(encoding="utf-8").splitlines()) == 3


def test_defers_while_web_job_is_active_then_installs_on_304(env):
    job_file = _write_job(env["data_dir"], "0123456789ab", "running")

    res1 = U.run_update(_cfg(env))
    assert res1.action == "deferred"
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["log"].exists()

    # Once the job finishes, even a 304 from GitHub completes the deferred install.
    job_file.write_text(json.dumps({"id": "0123456789ab", "status": "done"}), encoding="utf-8")
    res2 = U.run_update(_cfg(env))
    assert res2.action == "updated"
    assert res2.commit == env["c2"]
    assert _git(env["live"], "rev-parse", "HEAD") == env["c2"]
    assert env["gh"].requests[1]["if_none_match"] == '"etag-1"'


def test_rolls_back_when_uv_sync_fails(env):
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
    state = json.loads(env["state_file"].read_text(encoding="utf-8"))
    assert state["installed_commit"] is None


def test_rolls_back_when_service_not_active_after_restart(env):
    bad_systemctl = _make_script(
        env["tmp"] / "bad-systemctl",
        f'echo "systemctl $*" >> "{env["log"]}"\n'
        'case "$*" in *is-active*) exit 3 ;; *) exit 0 ;; esac\n',
    )
    with pytest.raises(U.UpdateError, match="not active after restart"):
        U.run_update(_cfg(env, systemctl_bin=bad_systemctl))

    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert env["log"].read_text(encoding="utf-8").splitlines() == [
        "uv sync --frozen",
        "systemctl --user restart bili-album-web.service",
        "systemctl --user is-active --quiet bili-album-web.service",
        "uv sync --frozen --offline",
        "systemctl --user restart bili-album-web.service",
    ]


def test_refuses_dirty_tracked_checkout(env):
    (env["live"] / "version.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(U.UpdateError, match="uncommitted tracked changes"):
        U.run_update(_cfg(env))
    assert _git(env["live"], "rev-parse", "HEAD") == env["c1"]
    assert not env["log"].exists()


def test_handles_repointed_tag_on_github(env):
    assert U.run_update(_cfg(env)).action == "updated"

    # Force-move v0.1.0 to a new commit on origin and bump GitHub ETag.
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
