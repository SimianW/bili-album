"""Audio fingerprint recognition behind one small interface.

The implementation uses ``shazamio`` (Shazam's public endpoint; no key). Every
answer is cached on disk keyed by (audio file identity, start, length), the
request rate is limited, a ``RateLimited`` answer backs off, and a request
budget per run keeps long scans polite. If the service is unreachable the
recogniser marks itself unavailable and the planner falls back to the other
evidence.

A Shazam match carries ``offset``: the position inside the reference
recording where our clip starts. ``clip_start - offset`` is therefore where the
recording begins in our audio, which pins track starts to ~0.1 s.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Recognition:
    clip_start: float
    clip_len: float
    title: str = ""
    artist: str = ""
    album: str = ""
    key: str = ""
    offset: float | None = None      # position in the reference recording at clip start
    skew: float | None = None

    @property
    def ok(self) -> bool:
        return bool(self.title)

    @property
    def implied_start(self) -> float | None:
        return None if self.offset is None else self.clip_start - self.offset

    def to_dict(self) -> dict:
        return asdict(self)


class Recognizer:
    """Interface: ``recognize(audio, start, length) -> Recognition``."""

    available = False
    exhausted = False
    calls = 0

    def recognize(self, audio: Path, start: float, length: float = 12.0) -> Recognition:
        return Recognition(start, length)


class NullRecognizer(Recognizer):
    pass


def _file_id(p: Path) -> str:
    st = p.stat()
    h = hashlib.sha1()
    with open(p, "rb") as f:
        h.update(f.read(1 << 16))
    h.update(str(st.st_size).encode())
    return h.hexdigest()[:16]


class ShazamRecognizer(Recognizer):
    def __init__(self, cache_dir: Path, *, min_interval: float = 6.0, budget: int = 60,
                 timeout: float = 20.0, cooldown: float = 75.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.budget = budget
        self.timeout = timeout
        self.calls = 0
        self.failures = 0
        self.rate_limited = 0
        self.exhausted = False
        self.cooldown = cooldown
        self._last = 0.0
        self._ids: dict[Path, str] = {}
        self._loop = asyncio.new_event_loop()
        self._shazam = None
        try:
            from aiohttp_retry import ExponentialRetry
            from shazamio import HTTPClient, Shazam

            # zh-CN answers with original-script names (上海一九四三 / 周杰伦, not "Shanghai 1943 / Jay
            # Chou"); Western titles come back unchanged. The CN endpoint missed songs that TW knows.
            self._shazam = Shazam(language="zh-CN", endpoint_country="TW",
                                  http_client=HTTPClient(retry_options=ExponentialRetry(attempts=1)))
            self.available = True
        except Exception as e:  # pragma: no cover - import failure
            log.warning("shazamio unavailable: %s", e)
            self.available = False

    def close(self) -> None:
        if self._shazam is not None:
            try:
                client = getattr(self._shazam, "http_client", None)
                if client is not None and hasattr(client, "close"):
                    self._loop.run_until_complete(client.close())
            except Exception:
                pass
        self._loop.close()

    def _cache_path(self, audio: Path, start: float, length: float) -> Path:
        fid = self._ids.get(audio) or self._ids.setdefault(audio, _file_id(audio))
        return self.cache_dir / f"shazam-zhCN-TW-{fid}-{start:.2f}-{length:.1f}.json"

    def recognize(self, audio: Path, start: float, length: float = 12.0) -> Recognition:
        audio = Path(audio)
        start = round(max(0.0, start), 2)
        cp = self._cache_path(audio, start, length)
        if cp.exists():
            return Recognition(**json.loads(cp.read_text(encoding="utf-8")))
        res = Recognition(start, length)
        if not self.available or self.calls >= self.budget:
            return res
        with tempfile.TemporaryDirectory() as td:
            clip = Path(td) / "clip.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                            "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", str(clip)], check=True)
            out = self._ask(clip)
        if out is None:
            return res  # transient failure: not cached
        track = out.get("track") or {}
        match = (out.get("matches") or [{}])[0]
        res.title = track.get("title", "") or ""
        res.artist = track.get("subtitle", "") or ""
        res.key = str(track.get("key", "") or "")
        for sec in track.get("sections") or []:
            for md in sec.get("metadata") or []:
                if md.get("title") == "Album":
                    res.album = md.get("text", "")
        if res.title and match.get("offset") is not None:
            res.offset = float(match["offset"])
            res.skew = float(match.get("timeskew") or 0.0)
            # Reject wildly skewed answers (likely wrong).
            if abs(res.skew) > 0.01:
                res.title = res.artist = res.album = ""
                res.offset = None
        cp.write_text(json.dumps(res.to_dict(), ensure_ascii=False), encoding="utf-8")
        return res

    def _ask(self, clip: Path) -> dict | None:
        """One request. Shazam allows a short burst and then refuses for a minute or more:
        after a refusal wait once; if it refuses again, stop recognising for this run."""
        for attempt in range(2):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                return self._loop.run_until_complete(
                    asyncio.wait_for(self._shazam.recognize(str(clip)), self.timeout))
            except Exception as e:
                name = type(e).__name__
                self.failures += 1
                if "RateLimit" in name or "429" in str(e):
                    self.rate_limited += 1
                    if attempt == 0:
                        log.info("shazam rate limited; waiting %.0fs", self.cooldown)
                        time.sleep(self.cooldown)
                        continue
                    log.warning("shazam keeps rate limiting; recognition disabled for this run")
                    self.available = False
                    self.exhausted = True
                    return None
                log.warning("shazam error: %s %s", name, str(e)[:200])
                if self.failures >= 4 and self.calls <= self.failures:
                    self.available = False
                return None
        return None
