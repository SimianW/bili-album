"""Which links the page accepts, and their canonical form."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

BILIBILI_HOSTS = ("www.bilibili.com", "bilibili.com", "m.bilibili.com")
SHORT_HOSTS = ("b23.tv",)
ALLOWED_HOSTS = BILIBILI_HOSTS + SHORT_HOSTS

BV_ONLY = re.compile(r"^BV[0-9A-Za-z]{10}$")
VIDEO_PATH = re.compile(r"^/video/(BV[0-9A-Za-z]{10})/?$")
SHORT_PATH = re.compile(r"^/([0-9A-Za-z]{4,16})/?$")
# A share text from the app looks like "【标题】 https://b23.tv/xxxx"; pick the link out of it.
EMBEDDED = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:bilibili\.com/video/BV[0-9A-Za-z]{10}[^\s]*|b23\.tv/[0-9A-Za-z]+)")

MAX_LEN = 2000
BAD_URL = "这不像 B 站视频链接。"


class UrlError(ValueError):
    pass


def normalize_url(raw: object) -> str:
    """Return ``https://www.bilibili.com/video/BV…`` or ``https://b23.tv/…``.

    Query strings (``?p=2``, share tracking) are dropped: a job always covers the whole video.

    Raises :class:`UrlError` for anything else.
    """
    if not isinstance(raw, str):
        raise UrlError(BAD_URL)
    s = raw.strip()
    if not s or len(s) > MAX_LEN:
        raise UrlError(BAD_URL)
    if BV_ONLY.match(s):
        return f"https://www.bilibili.com/video/{s}"
    if any(c.isspace() for c in s):
        m = EMBEDDED.search(s)
        if not m:
            raise UrlError(BAD_URL)
        s = m.group(0)
    if "://" not in s:
        s = "https://" + s
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError as e:
        raise UrlError(BAD_URL) from e
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or host not in ALLOWED_HOSTS:
        raise UrlError(BAD_URL)
    if parts.username or parts.password or port not in (None, 80, 443):
        raise UrlError(BAD_URL)
    if host in SHORT_HOSTS:
        m = SHORT_PATH.match(parts.path)
        if not m:
            raise UrlError(BAD_URL)
        return f"https://b23.tv/{m.group(1)}"
    m = VIDEO_PATH.match(parts.path)
    if not m:
        raise UrlError(BAD_URL)
    return f"https://www.bilibili.com/video/{m.group(1)}"


def video_key(url: str) -> str:
    """Identity of a video for the "already queued" check."""
    m = re.search(r"BV[0-9A-Za-z]{10}", url)
    return m.group(0) if m else url
