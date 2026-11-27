#!/usr/bin/env python3
"""Keep a live bili-album checkout updated to the latest GitHub release.

Polls GitHub's ``/repos/<owner>/<repo>/releases/latest`` endpoint with ``If-None-Match``
(ETag) so idle checks consume zero API quota. When a newer release tag appears, fetches the
tag into the live checkout, waits if any web job is ``queued`` or ``running``, checks out
the release commit (detached HEAD), runs ``uv sync --frozen``, and restarts the systemd user
service with automatic rollback if sync or startup fails.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("bili-album-update")

DEFAULT_REPO = "SimianW/bili-album"
DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_SERVICE = "bili-album-web.service"
USER_AGENT = "bili-album/updater"
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


class UpdateError(Exception):
    """A failure while checking, fetching, installing, or restarting."""


@dataclasses.dataclass(frozen=True)
class UpdaterConfig:
    repo: str = DEFAULT_REPO
    live_dir: Path = dataclasses.field(default_factory=lambda: Path.home() / "Dev/bili-album-live")
    data_dir: Path | None = dataclasses.field(default_factory=lambda: Path.home() / "Dev/bili-album/.work/web")
    state_file: Path = dataclasses.field(default_factory=lambda: Path.home() / ".local/state/bili-album/updater.json")
    service: str = DEFAULT_SERVICE
    uv_bin: str = ""
    systemctl_bin: str = "systemctl"
    api_base: str = DEFAULT_API_BASE
    http_timeout: float = 30.0
    uv_timeout: float = 300.0
    startup_wait: float = 2.0
    dry_run: bool = False


@dataclasses.dataclass(frozen=True)
class ReleaseCheck:
    status: str  # "ok", "not_modified", "none"
    tag: str | None = None
    etag: str | None = None


@dataclasses.dataclass(frozen=True)
class UpdateResult:
    action: str  # "up_to_date", "updated", "would_update", "deferred", "no_release", "locked"
    tag: str | None = None
    commit: str | None = None
    previous_commit: str | None = None


def resolve_uv_bin(configured: str) -> str:
    if configured:
        return configured
    local = Path.home() / ".local/bin/uv"
    if local.is_file():
        return str(local)
    found = shutil.which("uv")
    if found:
        return found
    raise UpdateError("uv executable not found (pass --uv or install ~/.local/bin/uv)")


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"command failed ({cmd[0]}): {exc}") from exc


def _git(live_dir: Path, *args: str, check: bool = True, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    proc = _run(["git", "-C", str(live_dir), *args], timeout=timeout)
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise UpdateError(f"git {args[0]} failed: {detail}")
    return proc


def validate_repo(repo: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise UpdateError(f"invalid GitHub repository name: {repo!r}")
    return repo


def validate_tag(live_dir: Path, tag: object) -> str:
    if not isinstance(tag, str) or not tag or tag.startswith("-") or any(c.isspace() for c in tag):
        raise UpdateError(f"invalid release tag: {tag!r}")
    proc = _git(live_dir, "check-ref-format", f"refs/tags/{tag}", check=False)
    if proc.returncode != 0:
        raise UpdateError(f"invalid git tag format: {tag!r}")
    return tag


def load_state(state_file: Path) -> dict:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable state file %s: %s", state_file, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f"{state_file.name}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, state_file)


@contextlib.contextmanager
def acquire_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def fetch_latest_release(cfg: UpdaterConfig, state: dict) -> ReleaseCheck:
    repo = validate_repo(cfg.repo)
    url = f"{cfg.api_base.rstrip('/')}/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    saved_etag = state.get("etag")
    saved_tag = state.get("tag")
    if state.get("repo") == repo and isinstance(saved_etag, str) and saved_etag and isinstance(saved_tag, str) and saved_tag:
        headers["If-None-Match"] = saved_etag

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=cfg.http_timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            etag = resp.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return ReleaseCheck(status="not_modified", tag=saved_tag, etag=saved_etag)
        if exc.code == 404:
            return ReleaseCheck(status="none")
        raise UpdateError(f"GitHub API returned HTTP {exc.code} for {repo}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"failed to reach GitHub API: {exc}") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UpdateError("GitHub API returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("GitHub API returned unexpected JSON payload")
    if data.get("draft") or data.get("prerelease"):
        return ReleaseCheck(status="none")
    tag = validate_tag(cfg.live_dir, data.get("tag_name"))
    return ReleaseCheck(status="ok", tag=tag, etag=etag)


def active_jobs(data_dir: Path | None) -> list[str]:
    """Return ids of queued or running web jobs (or a placeholder if a job file is unreadable)."""
    if data_dir is None or not data_dir.is_dir():
        return []
    found: list[str] = []
    for entry in sorted(data_dir.iterdir()):
        if not (entry.is_dir() and JOB_ID_RE.fullmatch(entry.name)):
            continue
        job_file = entry / "job.json"
        if not job_file.is_file():
            continue
        try:
            job = json.loads(job_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            log.warning("deferring: could not read %s (%s)", job_file, exc)
            found.append(entry.name)
            continue
        if isinstance(job, dict) and job.get("status") in ACTIVE_JOB_STATUSES:
            found.append(entry.name)
    return found


def _resolve_tag_commit(live_dir: Path, tag: str) -> str | None:
    proc = _git(live_dir, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _ensure_clean_checkout(live_dir: Path) -> None:
    status = _git(live_dir, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if status:
        raise UpdateError(f"live checkout has uncommitted tracked changes in {live_dir}")


def _sync_env(live_dir: Path, uv_bin: str, *, timeout: float, offline: bool = False) -> None:
    cmd = [uv_bin, "sync", "--frozen"]
    if offline:
        cmd.append("--offline")
    proc = _run(cmd, cwd=live_dir, timeout=timeout)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise UpdateError(f"uv sync failed: {detail}")


def _restart_and_verify(cfg: UpdaterConfig) -> None:
    if not cfg.service:
        return
    proc = _run([cfg.systemctl_bin, "--user", "restart", cfg.service], timeout=60.0)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise UpdateError(f"failed to restart {cfg.service}: {detail}")
    if cfg.startup_wait > 0:
        time.sleep(cfg.startup_wait)
    active = _run([cfg.systemctl_bin, "--user", "is-active", "--quiet", cfg.service], timeout=15.0)
    if active.returncode != 0:
        raise UpdateError(f"{cfg.service} is not active after restart")


def _rollback(cfg: UpdaterConfig, uv_bin: str, prev_commit: str, *, restart_service: bool) -> None:
    log.error("rolling back %s to %s", cfg.live_dir, prev_commit[:7])
    try:
        _git(cfg.live_dir, "checkout", "--detach", prev_commit)
    except UpdateError as exc:
        log.error("rollback git checkout failed: %s", exc)
        return
    try:
        _sync_env(cfg.live_dir, uv_bin, timeout=cfg.uv_timeout, offline=True)
    except UpdateError as exc:
        log.warning("rollback offline uv sync failed (%s); retrying online", exc)
        try:
            _sync_env(cfg.live_dir, uv_bin, timeout=cfg.uv_timeout, offline=False)
        except UpdateError as exc2:
            log.error("rollback online uv sync failed: %s", exc2)
    if restart_service and cfg.service:
        proc = _run([cfg.systemctl_bin, "--user", "restart", cfg.service], timeout=60.0)
        if proc.returncode != 0:
            log.error("rollback service restart failed: %s", proc.stderr.strip())


def run_update(cfg: UpdaterConfig) -> UpdateResult:
    if not cfg.live_dir.is_dir():
        raise UpdateError(f"live checkout directory does not exist: {cfg.live_dir}")
    _git(cfg.live_dir, "rev-parse", "--git-dir")

    lock_path = cfg.state_file.with_name(f"{cfg.state_file.name}.lock")
    with acquire_lock(lock_path) as acquired:
        if not acquired:
            log.info("another updater run is already in progress; skipping")
            return UpdateResult(action="locked")
        return _run_update_locked(cfg)


def _run_update_locked(cfg: UpdaterConfig) -> UpdateResult:
    state = load_state(cfg.state_file)
    check = fetch_latest_release(cfg, state)
    if check.status == "none" or check.tag is None:
        log.info("no published GitHub release for %s", cfg.repo)
        return UpdateResult(action="no_release")

    tag = validate_tag(cfg.live_dir, check.tag)
    head = _git(cfg.live_dir, "rev-parse", "HEAD").stdout.strip()
    local_commit = _resolve_tag_commit(cfg.live_dir, tag)

    if (
        check.status == "not_modified"
        and local_commit is not None
        and head == local_commit
        and state.get("installed_tag") == tag
        and state.get("installed_commit") == head
    ):
        log.info("already at latest release %s (%s)", tag, head[:7])
        return UpdateResult(action="up_to_date", tag=tag, commit=head)

    if cfg.dry_run:
        if local_commit is not None and head == local_commit:
            log.info("[dry-run] already at %s (%s)", tag, head[:7])
            return UpdateResult(action="up_to_date", tag=tag, commit=head)
        busy = active_jobs(cfg.data_dir)
        if busy:
            log.info("[dry-run] would defer update to %s (active jobs: %s)", tag, ", ".join(busy))
            return UpdateResult(action="deferred", tag=tag, commit=local_commit)
        log.info("[dry-run] would update %s from %s to %s", cfg.live_dir, head[:7], tag)
        return UpdateResult(action="would_update", tag=tag, commit=local_commit, previous_commit=head)

    # Fetch the release tag whenever GitHub reports a new/changed release or the tag is missing locally.
    # git fetch only writes objects and refs/tags/<tag>; it never mutates the working tree or .venv.
    if check.status == "ok" or local_commit is None:
        _git(cfg.live_dir, "fetch", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
        target_commit = _resolve_tag_commit(cfg.live_dir, tag)
        if target_commit is None:
            raise UpdateError(f"tag {tag} did not resolve to a commit after fetch")
        next_state = {
            "repo": cfg.repo,
            "etag": check.etag,
            "tag": tag,
            "installed_tag": state.get("installed_tag"),
            "installed_commit": state.get("installed_commit"),
            "updated_at": state.get("updated_at"),
        }
        save_state(cfg.state_file, next_state)
        state = next_state
    else:
        target_commit = local_commit

    if head == target_commit:
        state.update(
            repo=cfg.repo,
            etag=check.etag,
            tag=tag,
            installed_tag=tag,
            installed_commit=target_commit,
            updated_at=state.get("updated_at") or time.time(),
        )
        save_state(cfg.state_file, state)
        log.info("checkout already at %s (%s); recorded release state", tag, target_commit[:7])
        return UpdateResult(action="up_to_date", tag=tag, commit=target_commit)

    busy = active_jobs(cfg.data_dir)
    if busy:
        log.info("deferring update to %s (%s): active web job(s): %s", tag, target_commit[:7], ", ".join(busy))
        return UpdateResult(action="deferred", tag=tag, commit=target_commit, previous_commit=head)

    _ensure_clean_checkout(cfg.live_dir)
    uv_bin = resolve_uv_bin(cfg.uv_bin)

    _git(cfg.live_dir, "checkout", "--detach", target_commit)
    try:
        _sync_env(cfg.live_dir, uv_bin, timeout=cfg.uv_timeout)
    except UpdateError:
        _rollback(cfg, uv_bin, head, restart_service=False)
        raise

    try:
        _restart_and_verify(cfg)
    except UpdateError:
        _rollback(cfg, uv_bin, head, restart_service=True)
        raise

    state.update(
        repo=cfg.repo,
        etag=check.etag,
        tag=tag,
        installed_tag=tag,
        installed_commit=target_commit,
        updated_at=time.time(),
    )
    save_state(cfg.state_file, state)
    log.info("updated %s from %s to %s (%s)", cfg.live_dir, head[:7], tag, target_commit[:7])
    return UpdateResult(action="updated", tag=tag, commit=target_commit, previous_commit=head)


def parse_args(argv: list[str] | None = None) -> UpdaterConfig:
    parser = argparse.ArgumentParser(description="Update a live bili-album checkout to the latest GitHub release.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/repo (default: %(default)s)")
    parser.add_argument(
        "--live-dir",
        type=Path,
        default=Path.home() / "Dev/bili-album-live",
        help="Path to the live git checkout/worktree",
    )
    parser.add_argument(
        "--data-dir",
        default=str(Path.home() / "Dev/bili-album/.work/web"),
        help="Web job data directory to check for active jobs (empty string to skip)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path.home() / ".local/state/bili-album/updater.json",
        help="State file storing ETag and installed release",
    )
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help="systemd user unit to restart after updating (empty string to skip)",
    )
    parser.add_argument("--uv", default="", help="Path to uv binary (defaults to ~/.local/bin/uv or PATH)")
    parser.add_argument("--startup-wait", type=float, default=2.0, help="Seconds to wait before checking service status")
    parser.add_argument("--dry-run", action="store_true", help="Check for a newer release without modifying anything")
    args = parser.parse_args(argv)
    return UpdaterConfig(
        repo=args.repo,
        live_dir=args.live_dir.expanduser(),
        data_dir=Path(args.data_dir).expanduser() if args.data_dir else None,
        state_file=args.state_file.expanduser(),
        service=args.service,
        uv_bin=args.uv,
        startup_wait=args.startup_wait,
        dry_run=args.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = parse_args(argv)
    try:
        run_update(cfg)
    except UpdateError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
