"""Comments for timestamp hints: the first page in like order, the pinned ones, and every uploader
comment that can be reached cheaply.

Read through ``x/v2/reply`` and ``x/v2/reply/reply`` (yt-dlp's comment extractor walks every page
and does not mark the pinned comment). :func:`collect_comments` takes the caller's JSON getter so
it needs no session of its own; :class:`bili_album.bilibili.Bilibili` passes its ``_api``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

from .hints import parse_timestamps, plausible_list

log = logging.getLogger(__name__)

# Comments: page size, how many like-ordered pages to scan for the uploader's comments while
# pages come back full, and how many threads the uploader replied in to open (pages each).
PAGE_SIZE = 20
COMMENT_PAGES = 5
UPLOADER_THREADS = 5
THREAD_PAGES = 2
# Uploader comments found beyond the first page (previews, threads, extra pages) are kept only
# when they read as a tracklist by the planner's own rule (hints.plausible_list: two or more
# increasing times inside the video), so one-time chatter stays out. Replies lose their
# "回复 @name :" address prefix, which would otherwise end up in a track title.
_REPLY_PREFIX = re.compile(r"^\s*回复\s*@[^:：\n]*[:：]\s*")


@dataclass
class Comment:
    text: str
    author: str
    by_uploader: bool
    pinned: bool
    likes: int = 0
    reply: bool = False   # a reply inside another comment's thread, not a top-level comment


def collect_comments(api: Callable[[str, dict], dict], aid: int, uploader_mid: int,
                     duration: float | None = None) -> list[Comment]:
    """First page of comments in like order plus the pinned ones, and every uploader comment
    that can be reached cheaply:

    1. uploader replies in the reply previews the page already carries (no extra request);
    2. threads the uploader replied in (``up_action.reply``) that hold more replies than the
       preview shows: up to ``UPLOADER_THREADS`` threads, ``THREAD_PAGES`` pages each;
    3. while pages come back full (signed in: 20 per page; anonymous answers stop after the
       top 3), up to ``COMMENT_PAGES`` pages, keeping only the uploader's comments from the
       extra pages.

    Uploader comments from 1-3 count only when ``plausible_list`` accepts their times for a video
    of ``duration`` seconds. Order:
    pinned, then the uploader's, then the rest in like order."""
    url = "https://api.bilibili.com/x/v2/reply"
    try:
        d = api(url, {"type": 1, "oid": aid, "sort": 1, "ps": PAGE_SIZE, "pn": 1})
    except Exception as e:
        log.warning("comments unavailable: %s", e)
        return []
    pinned: list[Comment] = []
    uploader: list[Comment] = []
    rest: list[Comment] = []
    seen: set = set()
    mine = uploader_mid

    def is_mine(r) -> bool:
        return bool(mine) and int(r.get("mid", 0) or 0) == mine

    def add(r, *, pin: bool = False, reply: bool = False, only_mine: bool = False) -> None:
        if not r or r.get("rpid") in seen or (only_mine and not is_mine(r)):
            return
        text = (r.get("content") or {}).get("message", "")
        if reply:
            text = _REPLY_PREFIX.sub("", text, count=1)
        if only_mine and not plausible_list(parse_timestamps(text), duration):
            return
        seen.add(r.get("rpid"))
        c = Comment(text=text,
                    author=(r.get("member") or {}).get("uname", ""),
                    by_uploader=is_mine(r), pinned=pin, likes=int(r.get("like", 0) or 0), reply=reply)
        (pinned if pin else uploader if c.by_uploader else rest).append(c)

    def add_previews(r) -> None:
        for sub in r.get("replies") or []:
            add(sub, reply=True, only_mine=True)

    tops = [(d.get("upper") or {}).get("top")] + list(d.get("top_replies") or [])
    threads = [r for r in tops if r] + list(d.get("replies") or [])
    for r in tops:
        add(r, pin=True)
    for r in d.get("replies") or []:
        add(r)
    for r in threads:
        add_previews(r)

    pn = 1
    while len(d.get("replies") or []) >= PAGE_SIZE and pn < COMMENT_PAGES:
        pn += 1
        try:
            d = api(url, {"type": 1, "oid": aid, "sort": 1, "ps": PAGE_SIZE, "pn": pn})
        except Exception as e:
            log.info("comment page %d unavailable: %s", pn, e)
            break
        for r in d.get("replies") or []:
            add(r, only_mine=True)
            add_previews(r)
            threads.append(r)

    # A preview shows at most three replies; the uploader may have written more than one. The same
    # root can come back as upper.top, in top_replies and in replies: open each thread once.
    todo = list({r.get("rpid"): r for r in threads if (r.get("up_action") or {}).get("reply")
                 and int(r.get("rcount", 0) or 0) > len(r.get("replies") or [])}.values())
    for r in todo[:UPLOADER_THREADS]:
        for tp in range(1, THREAD_PAGES + 1):
            try:
                t = api("https://api.bilibili.com/x/v2/reply/reply",
                        {"type": 1, "oid": aid, "root": r.get("rpid"), "ps": PAGE_SIZE, "pn": tp})
            except Exception as e:
                log.info("replies under comment %s unavailable: %s", r.get("rpid"), e)
                break
            subs = t.get("replies") or []
            for sub in subs:
                add(sub, reply=True, only_mine=True)
            if len(subs) < PAGE_SIZE:
                break
    return pinned + uploader + rest
