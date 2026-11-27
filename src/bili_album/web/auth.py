"""Sign-in for the web page: Jellyfin accounts, the page's own sessions, attempt limiting.

- :class:`JellyfinAuth` checks a user name and password once with
  ``POST /Users/AuthenticateByName`` (Jellyfin 12.2: body ``{"Username", "Pw"}``, answer
  ``{"User": {"Id", "Name", "Policy": {"IsDisabled"}}, "AccessToken", ...}``), keeps only the
  user's id and name and ends the Jellyfin session it just created (``POST /Sessions/Logout``).
  The password and the Jellyfin token are never stored. ``user_ok`` asks, with the configured
  API key, whether the user still exists and is enabled (``GET /Users/{id}``).
- :class:`FakeAuth` (``serve --fake``): any user name with the password ``demo``.
- :class:`SessionStore`: random 256-bit tokens in a cookie; only their SHA-256 is kept, in
  ``sessions.json`` (mode 600). Valid 30 days from last use; the Jellyfin account is
  re-checked at most once an hour.
- :class:`AttemptLimiter`: 5 failures in 5 minutes for one user name or one client address
  block that name / address for 5 minutes after the last failure.
- :func:`client_address`: the socket peer, or the right-most untrusted ``X-Forwarded-For``
  entry when the peer is a trusted proxy.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

COOKIE = "bili_album_session"
SESSION_DAYS = 30
USER_CHECK_EVERY = 3600.0
LAST_SEEN_WRITE_EVERY = 60.0
MAX_FAILURES = 5
FAIL_WINDOW = 300.0
MAX_FIELD = 256

MSG_BAD = "用户名或密码不对。"
MSG_LIMIT = "试的次数太多了，5 分钟后再试。"
MSG_DOWN = "现在连不上 Jellyfin，稍后再试。"
MSG_EMPTY = "请填写用户名和密码。"
MSG_SIGN_IN = "请先登录。"


class AuthFailed(Exception):
    """Wrong user name or password, or a disabled account (one message for all)."""


class AuthUnavailable(Exception):
    """Jellyfin could not be reached or answered with a server error."""


@dataclass(frozen=True)
class User:
    id: str
    name: str


def package_version() -> str:
    try:
        from importlib.metadata import version
        return version("bili-album")
    except Exception:
        return "0"


def device_id(data_dir: Path) -> str:
    """A random id for this installation, created once and kept in the data directory."""
    p = Path(data_dir) / "device-id"
    try:
        v = p.read_text(encoding="ascii").strip()
        if v:
            return v
    except OSError:
        pass
    v = secrets.token_hex(16)
    _write_private(p, v + "\n")
    return v


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- Jellyfin

class JellyfinAuth:
    def __init__(self, base_url: str, api_key: str, device: str, *, timeout: float = 10.0,
                 version: str | None = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.client = (f'MediaBrowser Client="bili-album", Device="web", DeviceId="{device}", '
                       f'Version="{version or package_version()}"')

    def _call(self, method: str, path: str, *, body: dict | None = None, token: str = "") -> tuple[int, dict]:
        auth = self.client + (f', Token="{token}"' if token else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": auth, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AuthUnavailable(type(e).__name__) from None

    def authenticate(self, name: str, password: str) -> User:
        code, body = self._call("POST", "/Users/AuthenticateByName", body={"Username": name, "Pw": password})
        if code >= 500:
            raise AuthUnavailable(f"HTTP {code}")
        if code != 200:
            raise AuthFailed()
        user, token = body.get("User") or {}, body.get("AccessToken") or ""
        try:
            disabled = bool((user.get("Policy") or {}).get("IsDisabled"))
            uid, uname = str(user.get("Id") or ""), str(user.get("Name") or name)
        finally:
            if token:
                try:  # end the Jellyfin session this check created; the token is forgotten either way
                    self._call("POST", "/Sessions/Logout", token=token)
                except AuthUnavailable:
                    log.warning("could not end the Jellyfin sign-in session")
            token = ""
        if disabled or not uid:
            raise AuthFailed()
        return User(uid, uname)

    def user_ok(self, user_id: str) -> bool | None:
        """True: exists and enabled; False: gone or disabled; None: Jellyfin not reachable."""
        try:
            code, body = self._call("GET", f"/Users/{urllib.request.quote(user_id, safe='')}", token=self.api_key)
        except AuthUnavailable:
            return None
        if code == 200:
            return not bool((body.get("Policy") or {}).get("IsDisabled"))
        if code in (400, 404):
            return False
        return None  # 401/403 (key problem) or 5xx: do not drop sessions over our own trouble


class FakeAuth:
    """``serve --fake``: any user name, password ``demo``."""

    PASSWORD = "demo"

    def authenticate(self, name: str, password: str) -> User:
        if not hmac.compare_digest(password.encode("utf-8"), self.PASSWORD.encode("utf-8")):
            raise AuthFailed()
        return User("fake-" + hashlib.sha256(name.lower().encode("utf-8")).hexdigest()[:16], name)

    def user_ok(self, user_id: str) -> bool | None:
        return True


# ----------------------------------------------------------------------------- sessions

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionStore:
    def __init__(self, path: Path, check_user: Callable[[str], bool | None], *, clock=time.time):
        self.path = Path(path)
        self.check_user = check_user
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = self.clock()
        for s in data.get("sessions", []) if isinstance(data, dict) else []:
            if isinstance(s, dict) and isinstance(s.get("hash"), str) and now - float(s.get("last_seen", 0)) \
                    < SESSION_DAYS * 86400:
                self._sessions[s["hash"]] = s

    def _save(self) -> None:
        _write_private(self.path, json.dumps({"sessions": list(self._sessions.values())}, ensure_ascii=False,
                                             indent=1))

    def create(self, user: User) -> str:
        token = secrets.token_urlsafe(32)  # 256 bits
        now = self.clock()
        with self._lock:
            self._sessions[token_hash(token)] = {"hash": token_hash(token), "user_id": user.id, "name": user.name,
                                                 "created": now, "last_seen": now, "last_check": now}
            self._save()
        return token

    def _find(self, token: str) -> dict | None:
        h = token_hash(token)
        found = None
        for k, s in self._sessions.items():  # constant-time compare against every stored hash
            if hmac.compare_digest(k, h):
                found = s
        return found

    def lookup(self, token: str | None) -> User | None:
        if not token or len(token) > 200:
            return None
        now = self.clock()
        with self._lock:
            s = self._find(token)
            if s is None:
                return None
            if now - s["last_seen"] >= SESSION_DAYS * 86400:
                self._sessions.pop(s["hash"], None)
                self._save()
                return None
            need_check = now - s.get("last_check", 0) >= USER_CHECK_EVERY
        if need_check:
            ok = self.check_user(s["user_id"])  # outside the lock: may take a few seconds
            with self._lock:
                s["last_check"] = now
                if ok is False:
                    self._sessions.pop(s["hash"], None)
                    self._save()
                    return None
        with self._lock:
            dirty = need_check or now - s["last_seen"] >= LAST_SEEN_WRITE_EVERY
            s["last_seen"] = now
            if dirty and s["hash"] in self._sessions:
                self._save()
            return User(s["user_id"], s["name"])

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            s = self._find(token)
            if s is not None:
                self._sessions.pop(s["hash"], None)
                self._save()


# ----------------------------------------------------------------------------- attempt limiting

class AttemptLimiter:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    @staticmethod
    def keys(name: str, addr: str) -> list[str]:
        return ["u:" + name.strip().casefold(), "a:" + addr]

    def blocked(self, name: str, addr: str) -> bool:
        now = self.clock()
        with self._lock:
            for k in self.keys(name, addr):
                recent = [t for t in self._fails.get(k, []) if now - t < FAIL_WINDOW]
                if len(recent) >= MAX_FAILURES and now - recent[-1] < FAIL_WINDOW:
                    return True
            return False

    def failed(self, name: str, addr: str) -> None:
        now = self.clock()
        with self._lock:
            for k in self.keys(name, addr):
                lst = [t for t in self._fails.get(k, []) if now - t < FAIL_WINDOW]
                lst.append(now)
                self._fails[k] = lst[-50:]

    def succeeded(self, name: str) -> None:
        with self._lock:
            self._fails.pop(self.keys(name, "")[0], None)


# ----------------------------------------------------------------------------- proxies

def parse_networks(text: str) -> list:
    nets = []
    for part in (text or "").replace(",", " ").split():
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("ignoring invalid trusted proxy entry")
    return nets


class TrustedProxies:
    """Loopback, the server's own bind addresses, and ``BILI_ALBUM_TRUSTED_PROXIES``.

    A request on the Tailscale listener comes from the client's own address, which is none of
    these, so its ``X-Forwarded-*`` headers are ignored."""

    def __init__(self, bind_hosts=("127.0.0.1",), extra: str = ""):
        if isinstance(bind_hosts, str):
            bind_hosts = [bind_hosts]
        self.nets = parse_networks("127.0.0.0/8 ::1") + parse_networks(extra)
        for h in bind_hosts:
            try:
                self.nets.append(ipaddress.ip_network(h.strip("[]")))
            except ValueError:
                pass

    def __contains__(self, addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr.strip().strip("[]"))
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.nets if n.version == ip.version)


def client_address(peer: str, forwarded_for: str | None, trusted: TrustedProxies) -> str:
    if peer not in trusted or not forwarded_for:
        return peer
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for h in reversed(hops):
        if h not in trusted:
            try:
                return str(ipaddress.ip_address(h.strip("[]")))
            except ValueError:
                return peer  # garbage in the header: count it against the proxy itself
    return hops[0] if hops else peer


def is_https(peer: str, forwarded_proto: str | None, trusted: TrustedProxies) -> bool:
    return peer in trusted and (forwarded_proto or "").split(",")[0].strip().lower() == "https"


def extra_config_value(key: str) -> str:
    """A key :mod:`bili_album.config` does not model yet (``BILI_ALBUM_TRUSTED_PROXIES``)."""
    from ..config import DEFAULT_PATH, parse_env_file
    if os.environ.get(key):
        return os.environ[key]
    p = Path(os.environ.get("BILI_ALBUM_CONFIG") or DEFAULT_PATH).expanduser()
    try:
        return parse_env_file(p.read_text(encoding="utf-8")).get(key, "")
    except OSError:
        return ""
