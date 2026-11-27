"""Polite HTTP: on-disk cache keyed by request, per-host rate limit, retry with backoff.

Every outbound request in the project goes through :class:`Http` so that
re-running a plan never hits a provider twice for the same question, and so
that no request carries anything but a generic User-Agent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

from . import USER_AGENT

log = logging.getLogger(__name__)

# Minimum seconds between two requests to the same host.
HOST_INTERVAL = {
    "itunes.apple.com": 1.0,
    "music.163.com": 0.5,
    "api.bilibili.com": 0.5,
    "musicbrainz.org": 1.1,
}
DEFAULT_INTERVAL = 0.3

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)


class HttpError(RuntimeError):
    pass


class Http:
    """A cached, rate-limited JSON/bytes client.

    ``cache_dir`` holds one file per request (sha1 of method+url+params+body).
    Failed requests are not cached.
    """

    def __init__(self, cache_dir: Path | str | None, *, user_agent: str = USER_AGENT,
                 offline: bool = False, retries: int = 3, timeout: float = 20.0):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.offline = offline
        self.retries = retries
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- internals -----------------------------------------------------------------

    def _key(self, method: str, url: str, params: Any, data: Any) -> str:
        raw = json.dumps([method, url, params, data], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _wait(self, host: str) -> None:
        interval = HOST_INTERVAL.get(host, DEFAULT_INTERVAL)
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            if now - last < interval:
                time.sleep(interval - (now - last))
            self._last[host] = time.monotonic()

    def _fetch(self, method: str, url: str, *, params=None, data=None, headers=None,
               stateless: bool = False) -> bytes:
        if self.offline:
            raise HttpError(f"offline: {url}")
        host = urlsplit(url).hostname or ""
        delay = 1.0
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait(host)
            try:
                if stateless:
                    # No cookies: some providers degrade answers once they set a tracking cookie.
                    hdrs = {"User-Agent": self.session.headers["User-Agent"], **(headers or {})}
                    r = requests.request(method, url, params=params, data=data, headers=hdrs,
                                         timeout=self.timeout)
                else:
                    r = self.session.request(method, url, params=params, data=data, headers=headers,
                                             timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise HttpError(f"HTTP {r.status_code} from {host}")
                if r.status_code >= 400:
                    # Client errors are not worth retrying.
                    raise HttpError(f"HTTP {r.status_code} from {host}: {r.text[:200]}")
                return r.content
            except (requests.RequestException, HttpError) as e:
                last_err = e
                if isinstance(e, HttpError) and "HTTP 4" in str(e) and "429" not in str(e):
                    break
                if attempt < self.retries:
                    log.debug("retry %s after %s (%s)", url, delay, e)
                    time.sleep(delay)
                    delay *= 2
        raise HttpError(f"{method} {url} failed: {last_err}")

    # -- public --------------------------------------------------------------------

    def get_bytes(self, url: str, *, params=None, headers=None, cache: bool = True,
                  method: str = "GET", data=None) -> bytes:
        key = self._key(method, url, params, data)
        path = self.cache_dir / f"{key}.bin" if (self.cache_dir and cache) else None
        if path and path.exists():
            return path.read_bytes()
        body = self._fetch(method, url, params=params, data=data, headers=headers)
        if path:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
            meta = path.with_suffix(".url")
            meta.write_text(f"{method} {url}?{urlencode(params or {})}\n", encoding="utf-8")
        return body

    def get_json(self, url: str, *, params=None, headers=None, method: str = "GET", data=None,
                 cache: bool = True, validate=None, stateless: bool = False) -> Any:
        """Fetch JSON. ``validate(obj) -> bool`` decides whether the answer may be cached."""
        key = self._key(method, url, params, data)
        path = self.cache_dir / f"{key}.json" if (self.cache_dir and cache) else None
        if path and path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                path.unlink()
        body = self._fetch(method, url, params=params, data=data, headers=headers, stateless=stateless)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as e:
            raise HttpError(f"non-JSON answer from {url}: {body[:120]!r}") from e
        if path and (validate is None or validate(obj)):
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            path.with_suffix(".url").write_text(f"{method} {url}?{urlencode(params or {})}\n",
                                                encoding="utf-8")
        return obj

    def store_json(self, url: str, params, obj, method: str = "GET", data=None) -> None:
        """Put an answer fetched with ``cache=False`` into the cache under its normal key."""
        if not self.cache_dir:
            return
        path = self.cache_dir / f"{self._key(method, url, params, data)}.json"
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        path.with_suffix(".url").write_text(f"{method} {url}?{urlencode(params or {})}\n", encoding="utf-8")

    @property
    def cookies(self) -> CookieJar:
        return self.session.cookies
