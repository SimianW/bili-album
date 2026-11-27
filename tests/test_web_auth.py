"""Sign-in: Jellyfin client (against a fake Jellyfin), sessions, attempt limiting, proxies, listeners."""

import http.client
import json
import logging
import os
import re
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bili_album.web import auth as A
from bili_album.web.fake import FakeRunner
from bili_album.web.jobs import JobManager
from bili_album.web.server import make_servers, make_site

PASSWORD = "correct-horse-battery-staple-91"
API_KEY = "jf-api-key-0000-secret"


# ----------------------------------------------------------------------------- fake Jellyfin

class FakeJellyfin:
    """Just enough of Jellyfin 12.2: AuthenticateByName, Sessions/Logout, Users/{id}."""

    def __init__(self):
        self.users = {"benson": {"id": "u-benson", "pw": PASSWORD, "disabled": False},
                      "simon": {"id": "u-simon", "pw": PASSWORD, "disabled": False},
                      "olduser": {"id": "u-old", "pw": PASSWORD, "disabled": True}}
        self.requests: list[dict] = []
        self.tokens: set[str] = set()
        self.logged_out: list[str] = []
        self.down = False
        fj = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body=None):
                fj.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                    "body": body})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                self._record(body)
                if fj.down:
                    return self._reply(503)
                if any(k.lower().startswith("x-emby") for k in self.headers):
                    return self._reply(400)  # 12.2 rejects X-Emby-* headers
                if self.path == "/Users/AuthenticateByName":
                    u = fj.users.get((body or {}).get("Username", "").lower())
                    if not u or u["pw"] != (body or {}).get("Pw"):
                        return self._reply(401)
                    tok = "jf-session-token-" + u["id"]
                    fj.tokens.add(tok)
                    return self._reply(200, {"User": {"Id": u["id"], "Name": body["Username"],
                                                      "Policy": {"IsDisabled": u["disabled"]}},
                                             "AccessToken": tok, "ServerId": "srv", "SessionInfo": {}})
                if self.path == "/Sessions/Logout":
                    m = re.search(r'Token="([^"]+)"', self.headers.get("Authorization", ""))
                    if not m or m.group(1) not in fj.tokens:
                        return self._reply(401)
                    fj.tokens.discard(m.group(1))
                    fj.logged_out.append(m.group(1))
                    return self._reply(204)
                self._reply(404)

            def do_GET(self):
                self._record()
                if fj.down:
                    return self._reply(503)
                m = re.fullmatch(r"/Users/([\w-]+)", self.path)
                if m and f'Token="{API_KEY}"' in self.headers.get("Authorization", ""):
                    u = next((u for u in fj.users.values() if u["id"] == m.group(1)), None)
                    if not u:
                        return self._reply(404, {"title": "not found"})
                    return self._reply(200, {"Id": u["id"], "Policy": {"IsDisabled": u["disabled"]}})
                self._reply(401)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def jf():
    f = FakeJellyfin()
    yield f
    f.close()


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ----------------------------------------------------------------------------- client

def test_authenticate_success_ends_jellyfin_session(jf, tmp_path):
    a = A.JellyfinAuth(jf.url, API_KEY, A.device_id(tmp_path))
    u = a.authenticate("benson", PASSWORD)
    assert u == A.User("u-benson", "benson")
    assert jf.logged_out == ["jf-session-token-u-benson"] and not jf.tokens
    auth_req = jf.requests[0]
    assert auth_req["path"] == "/Users/AuthenticateByName" and auth_req["body"] == {"Username": "benson", "Pw": PASSWORD}
    h = auth_req["headers"]["Authorization"]
    assert h.startswith('MediaBrowser Client="bili-album", Device="web", DeviceId="') and 'Version="' in h
    assert "Token=" not in h  # the API key is never used for sign-in
    for r in jf.requests:
        assert not any(k.lower().startswith("x-emby") for k in r["headers"])
        assert API_KEY not in json.dumps(r)
    # one device id per installation
    assert A.device_id(tmp_path) == A.device_id(tmp_path)
    assert stat.S_IMODE(os.stat(tmp_path / "device-id").st_mode) == 0o600


@pytest.mark.parametrize("name, pw", [("benson", "wrong"), ("nobody", PASSWORD), ("olduser", PASSWORD)])
def test_authenticate_failures_are_one_kind(jf, tmp_path, name, pw):
    a = A.JellyfinAuth(jf.url, API_KEY, "dev")
    with pytest.raises(A.AuthFailed):
        a.authenticate(name, pw)
    assert not jf.tokens  # a disabled account's session is ended too


def test_authenticate_unavailable(jf):
    jf.down = True
    with pytest.raises(A.AuthUnavailable):
        A.JellyfinAuth(jf.url, API_KEY, "dev").authenticate("benson", PASSWORD)
    with pytest.raises(A.AuthUnavailable):
        A.JellyfinAuth(closed_port_url(), API_KEY, "dev", timeout=2).authenticate("benson", PASSWORD)


def test_user_ok(jf):
    a = A.JellyfinAuth(jf.url, API_KEY, "dev")
    assert a.user_ok("u-benson") is True
    assert a.user_ok("u-old") is False
    assert a.user_ok("u-gone") is False
    jf.down = True
    assert a.user_ok("u-benson") is None
    assert A.JellyfinAuth(closed_port_url(), API_KEY, "dev", timeout=2).user_ok("u-benson") is None
    assert A.JellyfinAuth(jf.url, "wrong-key", "dev").user_ok("u-benson") is None  # our key problem: keep


# ----------------------------------------------------------------------------- sessions

class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_session_token_only_hashed_and_private(tmp_path):
    store = A.SessionStore(tmp_path / "sessions.json", lambda uid: True)
    tok = store.create(A.User("u1", "benson"))
    assert len(tok) >= 43  # 32 random bytes, url-safe base64
    text = (tmp_path / "sessions.json").read_text()
    assert tok not in text and A.token_hash(tok) in text
    assert stat.S_IMODE(os.stat(tmp_path / "sessions.json").st_mode) == 0o600
    assert store.lookup(tok) == A.User("u1", "benson")
    assert store.lookup(tok + "x") is None and store.lookup("") is None and store.lookup(None) is None
    # survives a restart
    assert A.SessionStore(tmp_path / "sessions.json", lambda uid: True).lookup(tok) == A.User("u1", "benson")


def test_session_sliding_expiry(tmp_path):
    clock = Clock()
    store = A.SessionStore(tmp_path / "s.json", lambda uid: True, clock=clock)
    tok = store.create(A.User("u1", "benson"))
    for _ in range(3):  # used every 29 days: stays valid for months
        clock.t += 29 * 86400
        assert store.lookup(tok) is not None
    clock.t += 30 * 86400 + 1
    assert store.lookup(tok) is None
    assert A.token_hash(tok) not in (tmp_path / "s.json").read_text()
    # an expired session is not loaded again either
    tok2 = store.create(A.User("u2", "simon"))
    clock.t += 31 * 86400
    assert A.SessionStore(tmp_path / "s.json", lambda uid: True, clock=clock).lookup(tok2) is None


def test_session_dropped_when_user_disabled_checked_hourly(tmp_path):
    clock, calls, state = Clock(), [], {"ok": True}

    def check(uid):
        calls.append(uid)
        return state["ok"]
    store = A.SessionStore(tmp_path / "s.json", check, clock=clock)
    tok = store.create(A.User("u1", "benson"))
    for _ in range(10):
        clock.t += 60
        assert store.lookup(tok)
    assert calls == []  # not more than once an hour
    clock.t += 3600
    assert store.lookup(tok) and calls == ["u1"]
    state["ok"] = None  # Jellyfin unreachable: keep the session
    clock.t += 3600
    assert store.lookup(tok) and len(calls) == 2
    state["ok"] = False  # deleted or disabled
    clock.t += 3600
    assert store.lookup(tok) is None
    assert store.lookup(tok) is None and len(calls) == 3


def test_session_delete(tmp_path):
    store = A.SessionStore(tmp_path / "s.json", lambda uid: True)
    a, b = store.create(A.User("u1", "x")), store.create(A.User("u2", "y"))
    store.delete(a)
    assert store.lookup(a) is None and store.lookup(b) is not None


# ----------------------------------------------------------------------------- limiter and proxies

def test_limiter_by_name_and_address_and_expiry():
    clock = Clock()
    lim = A.AttemptLimiter(clock=clock)
    for i in range(5):
        assert not lim.blocked("Benson", f"10.0.0.{i}")
        lim.failed("Benson", f"10.0.0.{i}")
    assert lim.blocked("benson", "10.9.9.9")  # same name, any address, case-insensitive
    assert not lim.blocked("simon", "10.9.9.9")
    for i in range(5):
        lim.failed(f"user{i}", "10.1.1.1")
    assert lim.blocked("anyone", "10.1.1.1")  # same address, any name
    clock.t += 299
    assert lim.blocked("benson", "10.9.9.9")
    clock.t += 2
    assert not lim.blocked("benson", "10.9.9.9") and not lim.blocked("anyone", "10.1.1.1")
    for _ in range(4):
        lim.failed("simon", "10.2.2.2")
    lim.succeeded("SIMON")
    lim.failed("simon", "10.3.3.3")
    assert not lim.blocked("simon", "10.4.4.4")


def test_client_address_and_https_rules():
    tp = A.TrustedProxies(["127.0.0.1", "::1", "100.87.146.105"], "10.8.0.0/16 bogus")
    # from the reverse proxy on loopback (either family): the right-most untrusted XFF entry
    assert A.client_address("127.0.0.1", "203.0.113.9", tp) == "203.0.113.9"
    assert A.client_address("::1", "198.51.100.1, 203.0.113.9", tp) == "203.0.113.9"
    assert A.client_address("127.0.0.1", "203.0.113.9, 10.8.1.1", tp) == "203.0.113.9"  # extra trusted hop
    assert A.client_address("::ffff:127.0.0.1", "203.0.113.9", tp) == "203.0.113.9"
    assert A.client_address("127.0.0.1", None, tp) == "127.0.0.1"  # missing header: the peer
    assert A.client_address("127.0.0.1", "  ", tp) == "127.0.0.1"
    assert A.client_address("127.0.0.1", "not-an-ip", tp) == "127.0.0.1"
    # a direct request on the Tailscale listener: the peer is the client, its headers are ignored
    assert A.client_address("100.101.102.103", "1.2.3.4", tp) == "100.101.102.103"
    assert not A.is_https("100.101.102.103", "https", tp)
    assert A.is_https("127.0.0.1", "https", tp) and A.is_https("::1", "HTTPS", tp)
    assert not A.is_https("127.0.0.1", "http", tp) and not A.is_https("127.0.0.1", None, tp)


# ----------------------------------------------------------------------------- HTTP: the page's sign-in

def start(tmp_path, authenticator, hosts=("127.0.0.1",), **kw):
    mgr = JobManager(tmp_path / "web", FakeRunner(speed=1e6), start_worker=False)
    site = make_site(mgr, authenticator=authenticator, hosts=hosts, **kw)
    servers = make_servers(list(hosts), 0, site)
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return mgr, site, servers


def stop(servers):
    for s in servers:
        s.shutdown()
        s.server_close()


def call(srv, method, path, body=None, headers=None, host=None):
    addr = host or srv.server_address[0]
    c = http.client.HTTPConnection(addr, srv.server_address[1], timeout=10)
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    data = json.loads(raw) if raw and (r.getheader("Content-Type") or "").startswith("application/json") else raw
    return r.status, data, r


def cookie_of(r):
    sc = r.getheader("Set-Cookie") or ""
    m = re.match(A.COOKIE + r"=([^;]*)", sc)
    return (m.group(1) if m else None), sc


@pytest.fixture
def site(tmp_path, jf):
    mgr, site, servers = start(tmp_path, A.JellyfinAuth(jf.url, API_KEY, "dev"))
    yield mgr, site, servers[0]
    stop(servers)


def login(srv, name="benson", pw=PASSWORD, headers=None):
    return call(srv, "POST", "/api/login", {"username": name, "password": pw}, headers)


def test_sign_in_flow(site, jf):
    mgr, s, srv = site
    code, body, r = login(srv)
    assert code == 200 and body == {"user": {"name": "benson"}}
    tok, sc = cookie_of(r)
    assert tok and "HttpOnly" in sc and "SameSite=Lax" in sc and "Path=/" in sc and "Secure" not in sc
    assert "Max-Age=2592000" in sc and r.getheader("Cache-Control") == "no-store"
    ck = {"Cookie": f"{A.COOKIE}={tok}"}
    code, body, _ = call(srv, "GET", "/api/jobs", headers=ck)
    assert code == 200 and body["me"] == {"name": "benson"} and body["auth"] is True
    # sign out: session gone, cookie cleared
    code, _, r = call(srv, "POST", "/api/logout", {}, ck)
    assert code == 200 and "Max-Age=0" in cookie_of(r)[1]
    assert call(srv, "GET", "/api/jobs", headers=ck)[0] == 401
    assert tok not in (mgr.data_dir / "sessions.json").read_text()


@pytest.mark.parametrize("name, pw, code, msg", [
    ("benson", "wrong", 401, "用户名或密码不对。"),
    ("nobody", PASSWORD, 401, "用户名或密码不对。"),
    ("olduser", PASSWORD, 401, "用户名或密码不对。"),
    ("", PASSWORD, 400, "请填写用户名和密码。"),
    ("benson", "", 400, "请填写用户名和密码。"),
    ("   ", "x", 400, "请填写用户名和密码。"),
])
def test_sign_in_failures(site, jf, name, pw, code, msg):
    _, _, srv = site
    n = len(jf.requests)
    got, body, r = login(srv, name, pw)
    assert (got, body["error"]) == (code, msg)
    assert r.getheader("Set-Cookie") is None and r.getheader("Cache-Control") == "no-store"
    if code == 400:
        assert len(jf.requests) == n  # nothing sent to Jellyfin


def test_sign_in_when_jellyfin_down(site, jf):
    _, _, srv = site
    jf.down = True
    code, body, _ = login(srv)
    assert (code, body["error"]) == (503, "现在连不上 Jellyfin，稍后再试。")


def test_sign_in_guarded_like_other_writes(site):
    _, _, srv = site
    assert login(srv, headers={"Content-Type": "text/plain"})[0] == 415
    assert login(srv, headers={"Origin": "https://evil.example"})[0] == 403
    assert login(srv, headers={"Origin": "https://music-add.20age1million.com",
                               "Host": "music-add.20age1million.com"})[0] == 200


def test_every_api_route_needs_a_session(site):
    mgr, s, srv = site
    jid = mgr.submit("https://www.bilibili.com/video/BV14PVVzNE2R")["id"]
    routes = [("GET", "/api/jobs"), ("GET", f"/api/jobs/{jid}"), ("GET", f"/api/jobs/{jid}/cover"),
              ("POST", "/api/jobs"), ("POST", f"/api/jobs/{jid}/confirm"), ("POST", f"/api/jobs/{jid}/discard"),
              ("POST", f"/api/jobs/{jid}/retry"), ("POST", f"/api/jobs/{jid}/replace"),
              ("POST", f"/api/jobs/{jid}/keep"), ("DELETE", f"/api/jobs/{jid}"), ("POST", "/api/logout"),
              ("GET", "/api/whatever"), ("PUT", "/api/jobs"), ("HEAD", "/api/jobs")]
    for method, path in routes:
        body = {} if method in ("POST", "DELETE", "PUT") else None
        for ck in ({}, {"Cookie": f"{A.COOKIE}=forged-token-value"}, {"Cookie": "garbage;;=;"}):
            code, data, _ = call(srv, method, path, body, ck)
            assert code == 401, (method, path, ck)
            if method != "HEAD":
                assert data == {"error": "请先登录。"}
    code, page, _ = call(srv, "GET", "/")
    assert code == 200 and b'id="loginview"' in page and jid.encode() not in page
    assert mgr.get(jid)["status"] == "queued"  # nothing happened to it


def test_limit_by_name_through_proxy(site):
    _, _, srv = site
    for i in range(5):
        assert login(srv, "Benson", "wrong", {"X-Forwarded-For": f"203.0.113.{i}"})[0] == 401
    code, body, _ = login(srv, "benson", PASSWORD, {"X-Forwarded-For": "203.0.113.50"})
    assert (code, body["error"]) == (429, "试的次数太多了，5 分钟后再试。")
    assert login(srv, "simon", PASSWORD, {"X-Forwarded-For": "203.0.113.51"})[0] == 200


def test_limit_by_address_and_success_clears_name(site):
    _, _, srv = site
    for i in range(4):
        login(srv, "simon", "wrong", {"X-Forwarded-For": "198.51.100.7"})
    assert login(srv, "simon", PASSWORD, {"X-Forwarded-For": "198.51.100.8"})[0] == 200  # clears 'simon'
    for i in range(5):
        login(srv, f"user{i}", "wrong", {"X-Forwarded-For": "198.51.100.9"})
    assert login(srv, "benson", PASSWORD, {"X-Forwarded-For": "198.51.100.9"})[0] == 429
    assert login(srv, "benson", PASSWORD, {"X-Forwarded-For": "198.51.100.10"})[0] == 200


def test_secure_cookie_only_over_https_from_proxy(site):
    _, _, srv = site
    proxied = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.20",
               "Host": "music-add.20age1million.com", "Origin": "https://music-add.20age1million.com",
               "X-Real-IP": "172.18.0.1", "X-Forwarded-Port": "443", "REMOTE-HOST": "172.18.0.1"}
    code, _, r = login(srv, headers=proxied)
    assert code == 200 and "Secure" in cookie_of(r)[1]
    code, _, r = login(srv, headers={"X-Forwarded-Proto": "http"})
    assert code == 200 and "Secure" not in cookie_of(r)[1]


def test_queue_cap(tmp_path):
    mgr = JobManager(tmp_path / "web", FakeRunner(speed=1e6), start_worker=False)
    for i in range(20):
        mgr.submit(f"https://www.bilibili.com/video/BV1xx411c{i:03d}")
    from bili_album.web.jobs import JobError
    with pytest.raises(JobError) as e:
        mgr.submit("https://www.bilibili.com/video/BV1xx411c999")
    assert (e.value.code, e.value.message) == (429, "排队的任务太多了，稍后再试。")
    mgr.run_next()
    mgr.submit("https://www.bilibili.com/video/BV1xx411c999")


def test_attribution_over_http(tmp_path, jf):
    mgr, s, servers = start(tmp_path, A.JellyfinAuth(jf.url, API_KEY, "dev"))
    srv = servers[0]
    try:
        tb = cookie_of(login(srv, "benson")[2])[0]
        ts = cookie_of(login(srv, "simon")[2])[0]
        code, body, _ = call(srv, "POST", "/api/jobs", {"url": "BV1Hx4y1z7Qm"}, {"Cookie": f"{A.COOKIE}={tb}"})
        jid = body["job"]["id"]
        mgr.run_next()
        call(srv, "POST", f"/api/jobs/{jid}/confirm", {"tracks": []}, {"Cookie": f"{A.COOKIE}={ts}"})
        mgr.run_next()
        j = call(srv, "GET", f"/api/jobs/{jid}", headers={"Cookie": f"{A.COOKIE}={ts}"})[1]["job"]
        assert j["submitted_by"] == {"name": "benson"}
        assert [(a["action"], a["name"]) for a in j["actions"]] == [("confirm", "simon")]
        assert "u-benson" not in json.dumps(j) and "u-simon" not in json.dumps(j)  # ids stay on the server
        stored = json.loads((mgr.data_dir / jid / "job.json").read_text())
        assert stored["submitted_by"] == {"id": "u-benson", "name": "benson"}
        # removal is recorded in the audit log
        fid = call(srv, "POST", "/api/jobs", {"url": "BV1GJ411x7h7"}, {"Cookie": f"{A.COOKIE}={tb}"})[1]["job"]["id"]
        mgr.run_next()
        assert call(srv, "DELETE", f"/api/jobs/{fid}", {}, {"Cookie": f"{A.COOKIE}={ts}"})[0] == 200
        line = json.loads((mgr.data_dir / "audit.log").read_text().splitlines()[-1])
        assert (line["action"], line["job"], line["by"]) == ("remove", fid, "simon")
    finally:
        stop(servers)


def test_nothing_secret_in_responses_logs_or_files(tmp_path, jf, caplog):
    caplog.set_level(logging.DEBUG)
    mgr, s, servers = start(tmp_path, A.JellyfinAuth(jf.url, API_KEY, "dev"))
    srv = servers[0]
    try:
        seen = []
        code, body, r = login(srv)
        tok = cookie_of(r)[0]
        seen.append(json.dumps(body))
        login(srv, "benson", "Wrong-Password-777")
        ck = {"Cookie": f"{A.COOKIE}={tok}"}
        seen.append(json.dumps(call(srv, "POST", "/api/jobs", {"url": "BV14PVVzNE2R"}, ck)[1]))
        mgr.run_next()
        seen.append(json.dumps(call(srv, "GET", "/api/jobs", headers=ck)[1]))
        logs = "\n".join(rec.getMessage() for rec in caplog.records)
        files = ""
        for dirpath, _, names in os.walk(mgr.data_dir):
            for n in names:
                p = os.path.join(dirpath, n)
                if n != "sessions.json" and not n.endswith((".jpg", ".placeholder")):
                    files += open(p, encoding="utf-8", errors="replace").read()
        for secret in (PASSWORD, "Wrong-Password-777", tok, "jf-session-token", API_KEY):
            for where, text in (("response", "".join(seen)), ("log", logs), ("file", files)):
                assert secret not in text, (secret, where)
        assert tok not in (mgr.data_dir / "sessions.json").read_text()
    finally:
        stop(servers)


# ----------------------------------------------------------------------------- listeners

def test_two_loopback_families_share_one_state(tmp_path):
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        mgr, s, servers = start(tmp_path, A.FakeAuth(), hosts=("127.0.0.1", "::1"))
    except OSError:
        pytest.skip("::1 not available here")
    v4, v6 = servers
    try:
        assert v4.server_address[1] == v6.server_address[1]
        tok = cookie_of(login(v4, "benson", "demo")[2])[0]
        jid = call(v4, "POST", "/api/jobs", {"url": "BV14PVVzNE2R"}, {"Cookie": f"{A.COOKIE}={tok}"})[1]["job"]["id"]
        # the same session and the same job through the IPv6 listener
        code, body, _ = call(v6, "GET", "/api/jobs", headers={"Cookie": f"{A.COOKIE}={tok}"}, host="::1")
        assert code == 200 and [j["id"] for j in body["jobs"]] == [jid]
        assert call(v6, "GET", "/api/jobs", host="::1")[0] == 401  # signed out on both listeners
        assert call(v4, "GET", "/api/jobs")[0] == 401
        # from the proxy over ::1: secure cookie, client address from X-Forwarded-For
        code, _, r = call(v6, "POST", "/api/login", {"username": "x", "password": "demo"},
                          {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.30",
                           "Host": "music-add.20age1million.com"}, host="::1")
        assert code == 200 and "Secure" in cookie_of(r)[1]
    finally:
        stop(servers)


def test_two_ipv4_listeners_and_failed_bind(tmp_path):
    mgr, s, servers = start(tmp_path, A.FakeAuth(), hosts=("127.0.0.1", "127.0.0.2"))
    a, b = servers
    try:
        tok = cookie_of(login(a, "benson", "demo")[2])[0]
        jid = call(b, "POST", "/api/jobs", {"url": "BV14PVVzNE2R"}, {"Cookie": f"{A.COOKIE}={tok}"})[1]["job"]["id"]
        assert call(a, "GET", f"/api/jobs/{jid}", headers={"Cookie": f"{A.COOKIE}={tok}"})[0] == 200
    finally:
        stop(servers)
    # an address this machine does not have: the whole start fails, and nothing is left listening
    mgr2 = JobManager(tmp_path / "web2", FakeRunner(), start_worker=False)
    site2 = make_site(mgr2, authenticator=A.FakeAuth(), hosts=("127.0.0.1", "192.0.2.123"))
    port = A.secrets.randbelow(20000) + 30000
    with pytest.raises(OSError):
        make_servers(["127.0.0.1", "192.0.2.123"], port, site2)
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))  # the first socket was closed again
    finally:
        probe.close()
    for bad in ("0.0.0.0", "::", ""):
        with pytest.raises(ValueError):
            make_servers(["127.0.0.1", bad], 0, site2)


def test_serve_refuses_without_jellyfin_unless_auth_none(tmp_path, monkeypatch):
    from bili_album.web.server import serve
    env = tmp_path / "env"
    env.write_text("")
    monkeypatch.setenv("BILI_ALBUM_CONFIG", str(env))
    for k in list(os.environ):
        if k.startswith("BILI_ALBUM_") and k != "BILI_ALBUM_CONFIG":
            monkeypatch.delenv(k)
    with pytest.raises(SystemExit) as e:
        serve(["127.0.0.1"], 0, tmp_path / "data")
    assert "--auth none" in str(e.value)
    with pytest.raises(SystemExit):
        serve(["127.0.0.1"], 0, tmp_path / "data", auth="jellyfin")


def test_cli_refuses_every_interface():
    from bili_album.cli import main
    for bad in ("0.0.0.0", "::", "[::]"):
        with pytest.raises(SystemExit) as e:
            main(["serve", "--fake", "--host", "127.0.0.1", "--host", bad, "--port", "1"])
        assert e.value.code == 2
