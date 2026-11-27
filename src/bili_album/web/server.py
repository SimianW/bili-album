"""HTTP server: ``index.html`` and the JSON API, on the standard library only.

Routes (everything else is 404):

- ``GET /`` -> the page (signed in or not: the page asks the API and shows the sign-in view on 401);
- ``POST /api/login`` ``{"username", "password"}`` -> 200 ``{"user": {"name"}}`` + session cookie;
  401 wrong name / password / disabled account, 429 too many attempts, 503 Jellyfin unreachable,
  400 empty field;
- ``POST /api/logout`` ``{}`` -> ends the session;
- ``GET /api/jobs`` -> ``{"now", "jobs": [...], "me", "auth", "library_url"}`` (``library_url`` only when
  the public Jellyfin address is configured);
- ``POST /api/jobs`` ``{"url", "options"?}`` -> 201 ``{"job"}`` (409 + ``existing_id`` if the video is already
  queued, 429 when 20 jobs are queued, 400 for options outside ``runner.REFINE_LIMITS``); ``options`` may hold
  ``refine`` (bool) and ``search_ms``, ``silence_db``, ``deep_silence_db``, ``pre_roll_ms``;
- ``GET /api/jobs/<id>``, ``GET /api/jobs/<id>/cover``;
- ``POST /api/jobs/<id>/confirm`` ``{"tracks": [{"index", "title", "artist"}]}``;
- ``POST /api/jobs/<id>/{discard,retry,replace,keep}`` ``{}``;
- ``DELETE /api/jobs/<id>`` (queued or failed jobs).

With sign-in on, every ``/api/*`` route except ``/api/login`` answers 401 without a valid session.
Mutating requests (sign-in and sign-out included) must send ``Content-Type: application/json`` and,
if they carry an ``Origin`` header, it must match the ``Host`` header they arrived with.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from . import auth as A
from .jobs import JobError, JobManager
from .runner import refine_fields
from .urls import UrlError, normalize_url

log = logging.getLogger(__name__)

MAX_BODY = 256 * 1024
JOB_PATH = re.compile(r"^/api/jobs/([0-9a-f]{12})(?:/(confirm|discard|retry|replace|keep|cover))?$")
COVER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".svg": "image/svg+xml"}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
EVERY_INTERFACE = ("0.0.0.0", "::", "")


def load_index() -> bytes:
    """index.html with the refinement menu (static/refine-menu.html) and its fields filled in."""
    static = resources.files("bili_album.web").joinpath("static")
    fields = json.dumps(refine_fields(), ensure_ascii=False).replace("</", "<\\/")
    menu = static.joinpath("refine-menu.html").read_text(encoding="utf-8").replace("__REFINE_FIELDS__", fields)
    return static.joinpath("index.html").read_text(encoding="utf-8").replace("<!--refine-menu-->", menu, 1).encode()


@dataclass
class Site:
    """Everything the listeners share: one job manager, one session store, one attempt limiter."""
    manager: JobManager
    library_url: str = ""
    authenticator: object | None = None          # JellyfinAuth / FakeAuth; None = no sign-in
    sessions: A.SessionStore | None = None
    limiter: A.AttemptLimiter = field(default_factory=A.AttemptLimiter)
    trusted: A.TrustedProxies = field(default_factory=lambda: A.TrustedProxies("127.0.0.1"))
    index: bytes = field(default_factory=load_index)

    @property
    def auth_on(self) -> bool:
        return self.authenticator is not None


def make_site(manager: JobManager, *, library_url: str = "", authenticator=None, hosts=("127.0.0.1",),
              trusted_extra: str = "", clock=time.time) -> Site:
    sessions = None
    if authenticator is not None:
        sessions = A.SessionStore(manager.data_dir / "sessions.json", authenticator.user_ok, clock=clock)
    trusted = A.TrustedProxies(hosts, trusted_extra)
    return Site(manager=manager, library_url=public_library_url(library_url), authenticator=authenticator,
                sessions=sessions, limiter=A.AttemptLimiter(clock=clock), trusted=trusted)


class Handler(BaseHTTPRequestHandler):
    server_version = "bili-album"
    sys_version = ""
    site: Site

    # quiet access log: one debug line per request (paths only; bodies are never logged)
    def log_message(self, fmt, *args):  # noqa: D401
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers

    @property
    def manager(self) -> JobManager:
        return self.site.manager

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              cookies: list[str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, cookies: list[str] | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   cookies=cookies)

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

    # ------------------------------------------------------------------ sign-in

    @property
    def peer(self) -> str:
        return self.client_address[0]

    def client_addr(self) -> str:
        return A.client_address(self.peer, self.headers.get("X-Forwarded-For"), self.site.trusted)

    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
        except CookieError:
            return None
        m = c.get(A.COOKIE)
        return m.value if m else None

    def _user(self) -> A.User | None:
        if not self.site.auth_on:
            return None
        return self.site.sessions.lookup(self._token())

    def _by(self, user: A.User | None) -> dict | None:
        return {"id": user.id, "name": user.name} if user else None

    def _cookie(self, value: str, max_age: int) -> str:
        c = f"{A.COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        if A.is_https(self.peer, self.headers.get("X-Forwarded-Proto"), self.site.trusted):
            c += "; Secure"
        return c

    def _gate(self, path: str) -> tuple[bool, A.User | None]:
        """(allowed, user). Sign-in on: every /api/* route except /api/login needs a session."""
        if not path.startswith("/api/") or path == "/api/login" or not self.site.auth_on:
            return True, None
        user = self._user()
        if user is None:
            self._error(401, A.MSG_SIGN_IN)
            return False, None
        return True, user

    def _login(self) -> None:
        if not self.site.auth_on:
            self._error(404, "没有开启登录。")
            return
        if not self._guard():
            return
        body = self._body()
        if body is None:
            return
        name, password = body.get("username"), body.get("password")
        if not isinstance(name, str) or not isinstance(password, str) or not name.strip() or not password:
            self._error(400, A.MSG_EMPTY)
            return
        name = name.strip()
        if len(name) > A.MAX_FIELD or len(password) > 1024:
            self._error(401, A.MSG_BAD)
            return
        addr = self.client_addr()
        lim = self.site.limiter
        if lim.blocked(name, addr):
            self._error(429, A.MSG_LIMIT)
            return
        try:
            user = self.site.authenticator.authenticate(name, password)
        except A.AuthFailed:
            lim.failed(name, addr)
            log.info("sign-in refused (from %s)", addr)
            self._error(401, A.MSG_BAD)
            return
        except A.AuthUnavailable:
            self._error(503, A.MSG_DOWN)
            return
        finally:
            password = ""
        lim.succeeded(name)
        token = self.site.sessions.create(user)
        log.info("signed in: %s (from %s)", user.name, addr)
        self._json(200, {"user": {"name": user.name}},
                   cookies=[self._cookie(token, A.SESSION_DAYS * 86400)])

    def _logout(self, user: A.User | None) -> None:
        if not self._guard():
            return
        if self._body() is None:
            return
        self.site.sessions.delete(self._token())
        log.info("signed out: %s", user.name if user else "?")
        self._json(200, {"ok": True}, cookies=[self._cookie("", 0)])

    # ------------------------------------------------------------------ routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        ok, user = self._gate(path)
        if not ok:
            return
        try:
            if path in ("/", "/index.html"):
                self._send(200, self.site.index, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
            elif path == "/api/jobs":
                body = {"now": time.time(), "jobs": self.manager.list(), "auth": self.site.auth_on,
                        "me": {"name": user.name} if user else None}
                if self.site.library_url:
                    body["library_url"] = self.site.library_url
                self._json(200, body)
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
        ok, user = self._gate(path)
        if not ok:
            return
        if path == "/api/login":
            self._login()
            return
        if path == "/api/logout":
            if not self.site.auth_on:
                self._error(404, "没有开启登录。")
            else:
                self._logout(user)
            return
        if path != "/api/jobs" and not (JOB_PATH.match(path) and JOB_PATH.match(path).group(2) not in (None, "cover")):
            self._error(404, "没有这个地址。")
            return
        if not self._guard():
            return
        body = self._body()
        if body is None:
            return
        by = self._by(user)
        try:
            if path == "/api/jobs":
                try:
                    url = normalize_url(body.get("url"))
                except UrlError as e:
                    self._error(400, str(e))
                    return
                self._json(201, {"job": self.manager.submit(url, by=by, options=body.get("options"))})
                return
            m = JOB_PATH.match(path)
            jid, action = m.group(1), m.group(2)
            if action == "confirm":
                job = self.manager.confirm(jid, body.get("tracks", []), by=by)
            elif action == "discard":
                self.manager.discard(jid, by=by)
                self._json(200, {"ok": True})
                return
            else:
                job = getattr(self.manager, action)(jid, by=by)
            self._json(200, {"job": job})
        except JobError as e:
            self._error(e.code, e.message, **e.extra)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        ok, user = self._gate(path)
        if not ok:
            return
        m = JOB_PATH.match(path)
        if not m or m.group(2) is not None:
            self._error(404, "没有这个地址。")
            return
        if not self._guard():
            return
        try:
            self.manager.remove(m.group(1), by=self._by(user))
            self._json(200, {"ok": True})
        except JobError as e:
            self._error(e.code, e.message, **e.extra)

    def do_PUT(self):
        path = urlsplit(self.path).path
        ok, _ = self._gate(path)
        if ok:
            self._error(405, "不支持这个方法。")

    do_PATCH = do_PUT


def public_library_url(value: str) -> str:
    """The Jellyfin address the owner opens (``BILI_ALBUM_JELLYFIN_PUBLIC_URL``), if it is a plain http(s) URL."""
    try:
        u = urlsplit(value or "")
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return ""
    return value.rstrip("/") + "/"


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class _Server6(_Server):
    address_family = socket.AF_INET6


def make_servers(hosts, port: int, site: Site) -> list[ThreadingHTTPServer]:
    """One listening socket per host, all on the same port (``port=0``: the first picks it), one Site."""
    hosts = list(hosts)
    if not hosts:
        raise ValueError("give at least one address to listen on")
    for h in hosts:
        if h in EVERY_INTERFACE:
            raise ValueError("refusing to listen on every interface; give a specific address")
    handler = type("BoundHandler", (Handler,), {"site": site})
    servers: list[ThreadingHTTPServer] = []
    try:
        for h in hosts:
            cls = _Server6 if ":" in h else _Server
            srv = cls((h, port), handler)
            port = srv.server_address[1]
            servers.append(srv)
    except Exception:
        for s in servers:
            s.server_close()
        raise
    return servers


def make_server(host: str, port: int, manager: JobManager, *, library_url: str = "",
                authenticator=None) -> ThreadingHTTPServer:
    """Single listener (tests). ``library_url`` is the only configuration value the page receives."""
    site = make_site(manager, library_url=library_url, authenticator=authenticator, hosts=(host,))
    return make_servers([host], port, site)[0]


def serve(hosts, port: int, data_dir: Path, *, fake: bool = False, fake_speed: float = 1.0,
          fake_hold: dict[str, float] | None = None, auth: str | None = None) -> int:
    from ..config import load_config
    if isinstance(hosts, str):
        hosts = [hosts]
    config = load_config()
    if fake:
        from .fake import FakeRunner
        runner = FakeRunner(speed=fake_speed, hold=fake_hold, config=config)
    else:
        from .runner import PipelineRunner
        runner = PipelineRunner(config=config)
    mode = auth or ("fake" if fake else ("jellyfin" if config.jellyfin_url else ""))
    if not mode:
        raise SystemExit("bili-album serve: no Jellyfin URL is configured for sign-in; "
                         "give --auth none to run without sign-in")
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if mode == "jellyfin" and fake:
        mode = "fake"  # --fake never talks to a real Jellyfin
    if mode == "jellyfin":
        if not config.jellyfin_url:
            raise SystemExit("bili-album serve: --auth jellyfin needs BILI_ALBUM_JELLYFIN_URL")
        authenticator = A.JellyfinAuth(config.jellyfin_url, config.jellyfin_api_key, A.device_id(data_dir))
    elif mode == "fake":
        authenticator = A.FakeAuth()
    else:
        authenticator = None
    manager = JobManager(data_dir, runner)
    site = make_site(manager, library_url=config.jellyfin_public_url, authenticator=authenticator, hosts=hosts,
                     trusted_extra=A.extra_config_value("BILI_ALBUM_TRUSTED_PROXIES"))
    servers = make_servers(hosts, port, site)  # any address that cannot be bound fails the start
    shown = ", ".join(f"http://{'[' + h + ']' if ':' in h else h}:{s.server_address[1]}/"
                      for h, s in zip(hosts, servers))
    print(f"bili-album web: {shown} (data: {manager.data_dir}; sign-in: {mode}"
          f"{'; fake runner' if fake else ''})", flush=True)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        while all(t.is_alive() for t in threads):
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        manager.stop()
    return 0
