"""Comment collection: pinned, like order, and the uploader's comments from previews, threads and extra pages.

Offline: ``Bilibili._api`` is replaced by a scripted fake keyed by (endpoint, page, root).
"""

import pytest

from bili_album import bilibili as bb
from bili_album.bilibili import Bilibili, BilibiliError, VideoInfo

UP = 42
LIST = "https://api.bilibili.com/x/v2/reply"
THREAD = "https://api.bilibili.com/x/v2/reply/reply"


def r(rpid, mid, text="", like=0, *, previews=(), up_reply=False, rcount=0):
    return {"rpid": rpid, "mid": mid, "like": like, "rcount": rcount, "content": {"message": text or f"c{rpid}"},
            "member": {"uname": "up" if mid == UP else f"u{mid}"}, "replies": list(previews),
            "up_action": {"like": False, "reply": up_reply}}


class Fake(Bilibili):
    def __init__(self, pages: dict, threads: dict | None = None, fail_threads: bool = False):
        self.pages, self.threads, self.fail_threads = pages, threads or {}, fail_threads
        self.calls: list[tuple] = []

    def _api(self, url, params):
        if url == LIST:
            self.calls.append(("list", params["pn"]))
            return self.pages.get(params["pn"], {"replies": []})
        if url == THREAD:
            self.calls.append(("thread", params["root"], params["pn"]))
            if self.fail_threads:
                raise BilibiliError("412")
            return {"replies": self.threads.get((params["root"], params["pn"]), [])}
        raise AssertionError(url)


def vi(mid=UP):
    return VideoInfo(url="u", bvid="BV1", aid=1, title="t", uploader="up", uploader_mid=mid, description="",
                     duration=600, cover_url="", pubdate=0)


def texts(cs):
    return [c.text for c in cs]


def test_anonymous_page_previews_and_threads():
    pinned = r(1, UP, "pinned tracklist")
    page = {"upper": {"top": pinned}, "top_replies": [pinned],
            "replies": [r(2, 7, "hot", 30, previews=[r(21, UP, "00:00 a 03:00 b"), r(22, 8)]),
                        r(3, 9, "asks for a tracklist", 20, up_reply=True, rcount=12),
                        r(4, 10, "plain", 10)]}
    f = Fake({1: page}, threads={(3, 1): [r(31, 11), r(32, UP, "up answer in thread")]})
    cs = f._comments(vi())
    assert texts(cs) == ["pinned tracklist", "00:00 a 03:00 b", "up answer in thread", "hot",
                         "asks for a tracklist", "plain"]
    assert cs[0].pinned and cs[0].by_uploader and not cs[0].reply
    assert cs[1].by_uploader and cs[1].reply and not cs[1].pinned
    assert not any(c.by_uploader for c in cs[3:])
    # Three replies on page 1: not a full page, so no further pages; one thread opened.
    assert f.calls == [("list", 1), ("thread", 3, 1)]


def test_thread_is_opened_even_when_the_preview_shows_one_uploader_reply():
    page = {"replies": [r(2, 7, previews=[r(21, UP, "seen in preview")], up_reply=True, rcount=40)]}
    f = Fake({1: page}, threads={(2, 1): [r(21, UP, "seen in preview"), r(23, UP, "second up reply")]})
    cs = f._comments(vi())
    assert texts(cs)[:2] == ["seen in preview", "second up reply"]
    assert f.calls == [("list", 1), ("thread", 2, 1)]


def test_thread_fully_shown_in_preview_is_not_opened():
    page = {"replies": [r(2, 7, previews=[r(21, UP, "all here"), r(22, 8)], up_reply=True, rcount=2)]}
    f = Fake({1: page})
    assert "all here" in texts(f._comments(vi()))
    assert f.calls == [("list", 1)]


def test_full_pages_are_scanned_for_uploader_comments_only():
    full = [r(100 + i, 5, f"p1-{i}", 100 - i) for i in range(bb.PAGE_SIZE)]
    p2 = [r(200 + i, 5, f"p2-{i}") for i in range(bb.PAGE_SIZE - 1)] + [r(299, UP, "up on page 2")]
    p3 = [r(300, 6, "short page"), r(301, 6, previews=[r(311, UP, "up preview on page 3")])]
    f = Fake({1: {"replies": full}, 2: {"replies": p2}, 3: {"replies": p3}})
    cs = f._comments(vi())
    assert texts(cs)[:2] == ["up on page 2", "up preview on page 3"]
    assert len(cs) == bb.PAGE_SIZE + 2          # page 1 kept whole, later pages only the uploader's
    assert [c for c in f.calls if c[0] == "list"] == [("list", 1), ("list", 2), ("list", 3)]


def test_page_limit(monkeypatch):
    monkeypatch.setattr(bb, "COMMENT_PAGES", 2)
    full = lambda base: [r(base + i, 5) for i in range(bb.PAGE_SIZE)]  # noqa: E731
    f = Fake({1: {"replies": full(100)}, 2: {"replies": full(200)}, 3: {"replies": full(300)}})
    f._comments(vi())
    assert [c for c in f.calls if c[0] == "list"] == [("list", 1), ("list", 2)]


def test_thread_failures_are_not_fatal_and_threads_are_capped(monkeypatch):
    monkeypatch.setattr(bb, "UPLOADER_THREADS", 2)
    page = {"replies": [r(i, 7, up_reply=True, rcount=9) for i in range(1, 5)]}
    f = Fake({1: page}, fail_threads=True)
    cs = f._comments(vi())
    assert len(cs) == 4
    assert [c for c in f.calls if c[0] == "thread"] == [("thread", 1, 1), ("thread", 2, 1)]


def test_unknown_uploader_marks_nobody():
    page = {"replies": [r(1, 0, "mid zero", previews=[r(2, 0, "preview")])]}
    cs = Fake({1: page})._comments(vi(mid=0))
    assert texts(cs) == ["mid zero"] and not cs[0].by_uploader


def test_first_page_failure_gives_no_comments():
    class Down(Fake):
        def _api(self, url, params):
            raise BilibiliError("down")
    assert Down({})._comments(vi()) == []


@pytest.mark.parametrize("reply, word", [(False, "comment"), (True, "reply")])
def test_plan_hint_detail_names_replies(reply, word, tmp_path):
    from bili_album.bilibili import Comment
    from bili_album.plan import Planner, PlanOptions

    p = Planner.__new__(Planner)
    p.o = PlanOptions(workdir=str(tmp_path))
    v = vi()
    v.comments = [Comment("00:00 a\n03:00 b\n06:00 c", "up", True, False, 5, reply=reply)]
    hl = p._hint_lists(v, 600)
    assert hl and hl[0].detail == f"uploader's {word} by up"
