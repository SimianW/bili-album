"""``bili-album serve``: its arguments and start-up, kept next to the server they configure."""

from __future__ import annotations

import argparse
from pathlib import Path

EVERY_INTERFACE = ("0.0.0.0", "::", "")


def add_serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", action="append", default=None, metavar="ADDR",
                   help="address to listen on; repeat for several (default 127.0.0.1; 0.0.0.0 and :: are refused)")
    p.add_argument("--port", type=int, default=5191, help="port, the same for every address (default 5191)")
    p.add_argument("--data-dir", type=Path, help="job folders (default: .work/web/ in the repo)")
    p.add_argument("--auth", choices=("jellyfin", "none"), default=None,
                   help="sign-in with Jellyfin accounts (default when a Jellyfin URL is configured) or none")
    p.add_argument("--fake", action="store_true",
                   help="replay scripted jobs instead of running the pipeline (no network, no import); "
                        "sign-in accepts any user name with the password 'demo'")
    p.add_argument("--fake-speed", type=float, default=1.0, help="--fake: delay divisor (2 = twice as fast)")
    p.add_argument("--fake-hold", action="append", default=[], metavar="STAGE=SECONDS",
                   help="--fake: stay SECONDS longer in STAGE (plan, review, cut, import); repeatable")


def run_serve(a: argparse.Namespace, ap: argparse.ArgumentParser, repo_root: Path) -> int:
    from .server import serve

    hold = {}
    for item in a.fake_hold:
        stage, _, secs = item.partition("=")
        try:
            hold[stage] = float(secs)
        except ValueError:
            ap.error(f"--fake-hold expects STAGE=SECONDS, got {item!r}")
    hosts = a.host or ["127.0.0.1"]
    seen: list[str] = []
    for h in hosts:
        if h.strip("[]") in EVERY_INTERFACE:
            ap.error("--host must be a specific address (127.0.0.1, ::1 or the Tailscale address), "
                     "not every interface")
        if h.strip("[]") not in seen:
            seen.append(h.strip("[]"))
    if not 0 < a.port < 65536:
        ap.error("--port must be between 1 and 65535")
    data_dir = a.data_dir or repo_root / ".work" / "web"
    return serve(seen, a.port, data_dir, fake=a.fake, fake_speed=a.fake_speed, fake_hold=hold, auth=a.auth)
