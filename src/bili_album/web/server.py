"""HTTP server: ``index.html`` and the JSON API, on the standard library only.

Routes (everything else is 404):

- ``GET /`` -> the page;
- ``GET /api/jobs`` -> ``{"now", "jobs": [...]}``;
- ``POST /api/jobs`` ``{"url"}`` -> 201 ``{"job"}`` (409 + ``existing_id`` if the video is already queued);
- ``POST /api/jobs/<id>/confirm`` ``{"tracks": [{"index", "title", "artist"}]}``;
- ``POST /api/jobs/<id>/{discard,retry,replace,keep}`` ``{}``;
- ``DELETE /api/jobs/<id>`` (queued or failed jobs);
- ``GET /api/jobs/<id>/cover``.

Mutating requests must send ``Content-Type: application/json`` and, if they carry an
``Origin`` header, it must match the ``Host`` they were sent to.
"""

from __future__ import annotations

import json
import logging
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from .jobs import JobError, JobManager
from .urls import UrlError, normalize_url

log = logging.getLogger(__name__)

MAX_BODY = 256 * 1024
JOB_PATH = re.compile(r"^/api/jobs/([0-9a-f]{12})(?:/(confirm|discard|retry|replace|keep|cover))?$")
COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".svg": "image/svg+xml"}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def load_index() -> bytes:
    return resources.files("bili_album.web").joinpath("static/index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "bili-album"
    sys_version = ""
    manager: JobManager
    index: bytes

    # quiet access log: one debug line per request
    def log_message(self, fmt, *args):  # noqa: D401
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str, **extra) -> None:
        self._json(code, {"error": message, **extra})

    def _guard(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._error(415, "需要 Content-Type: application/json。")
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").lower()
            try:
                o = urlsplit(origin)
                ok = o.scheme in ("http", "https") and o.netloc.lower() == host and bool(host)
            except ValueError:
                ok = False
            if not ok:
                self._error(403, "不接受来自其他网页的请求。")
                return False
        return True

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._error(413 if n > MAX_BODY else 400, "请求太大或格式不对。")
            return None
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._error(400, "请求不是合法的 JSON。")
            return None
        if not isinstance(obj, dict):
            self._error(400, "请求必须是 JSON 对象。")
            return None
        return obj

    # ------------------------------------------------------------------ routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, self.index, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
            elif path == "/api/jobs":
                self._json(200, {"now": time.time(), "jobs": self.manager.list()})
            elif (m := JOB_PATH.match(path)) and m.group(2) is None:
                self._json(200, {"job": self.manager.get(m.group(1))})
            elif m and m.group(2) == "cover":
                p = self.manager.cover_path(m.group(1))
                ctype = COVER_TYPES.get(p.suffix.lower()) if p else None
                if not p or not ctype:
                    self._error(404, "没有封面。")
                else:
                    self._send(200, p.read_bytes(), ctype, {"Content-Security-Policy": "default-src 'none'"})
            else:
                self._error(404, "没有这个地址。")
        except JobError as e:
            self._error(e.code, e.message, **e.extra)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/api/jobs" and not (JOB_PATH.match(path) and JOB_PATH.match(path).group(2) not in (None, "cover")):
            self._error(404, "没有这个地址。")
            return
        if not self._guard():
            return
        body = self._body()
        if body is None:
            return
        try:
            if path == "/api/jobs":
                try:
                    url = normalize_url(body.get("url"))
                except UrlError as e:
                    self._error(400, str(e))
                    return
                self._json(201, {"job": self.manager.submit(url)})
                return
            m = JOB_PATH.match(path)
            jid, action = m.group(1), m.group(2)
            if action == "confirm":
                job = self.manager.confirm(jid, body.get("tracks", []))
            elif action == "discard":
                self.manager.discard(jid)
                self._json(200, {"ok": True})
                return
            else:
                job = getattr(self.manager, action)(jid)
            self._json(200, {"job": job})
        except JobError as e:
            self._error(e.code, e.message, **e.extra)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        m = JOB_PATH.match(path)
        if not m or m.group(2) is not None:
            self._error(404, "没有这个地址。")
            return
        if not self._guard():
            return
        try:
            self.manager.remove(m.group(1))
            self._json(200, {"ok": True})
        except JobError as e:
            self._error(e.code, e.message, **e.extra)

    def do_PUT(self):
        self._error(405, "不支持这个方法。")

    do_PATCH = do_PUT


def make_server(host: str, port: int, manager: JobManager) -> ThreadingHTTPServer:
    if host in ("0.0.0.0", "::", ""):
        raise ValueError("refusing to listen on every interface; give a specific address")
    handler = type("BoundHandler", (Handler,), {"manager": manager, "index": load_index()})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(host: str, port: int, data_dir: Path, *, fake: bool = False, fake_speed: float = 1.0,
          fake_hold: dict[str, float] | None = None) -> int:
    if fake:
        from .runner import FakeRunner
        runner = FakeRunner(speed=fake_speed, hold=fake_hold)
    else:
        from .runner import PipelineRunner
        runner = PipelineRunner()
    manager = JobManager(data_dir, runner)
    srv = make_server(host, port, manager)
    shown = f"[{host}]" if ":" in host else host
    print(f"bili-album web: http://{shown}:{srv.server_address[1]}/ (data: {manager.data_dir}"
          f"{', fake runner' if fake else ''})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        manager.stop()
    return 0

