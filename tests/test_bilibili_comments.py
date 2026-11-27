"""Comment collection (comments.py, through Bilibili._comments): pinned, like order, and the uploader's comments from previews, threads and extra pages.

Offline: ``Bilibili._api`` is replaced by a scripted fake keyed by (endpoint, page, root).
"""

import pytest

from bili_album import comments as bb
from bili_album.bilibili import Bilibili, BilibiliError, VideoInfo

UP = 42
TL = "00:00 a\n03:00 b\n06:00 c"          # a tracklist: three times
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
            "replies": [r(2, 7, "hot", 30, previews=[r(21, UP, "preview " + TL), r(22, 8)]),
                        r(3, 9, "asks for a tracklist", 20, up_reply=True, rcount=12),
                        r(4, 10, "plain", 10)]}
    f = Fake({1: page}, threads={(3, 1): [r(31, 11), r(32, UP, "thread " + TL)]})
    cs = f._comments(vi())
    assert texts(cs) == ["pinned tracklist", "preview " + TL, "thread " + TL, "hot",
                         "asks for a tracklist", "plain"]
    assert cs[0].pinned and cs[0].by_uploader and not cs[0].reply
    assert cs[1].by_uploader and cs[1].reply and not cs[1].pinned
    assert not any(c.by_uploader for c in cs[3:])
    # Three replies on page 1: not a full page, so no further pages; one thread opened.
    assert f.calls == [("list", 1), ("thread", 3, 1)]


def test_thread_is_opened_even_when_the_preview_shows_one_uploader_reply():
    page = {"replies": [r(2, 7, previews=[r(21, UP, "seen " + TL)], up_reply=True, rcount=40)]}
    f = Fake({1: page}, threads={(2, 1): [r(21, UP, "seen " + TL), r(23, UP, "second " + TL)]})
    cs = f._comments(vi())
    assert texts(cs)[:2] == ["seen " + TL, "second " + TL]
    assert f.calls == [("list", 1), ("thread", 2, 1)]


def test_thread_fully_shown_in_preview_is_not_opened():
    page = {"replies": [r(2, 7, previews=[r(21, UP, "all here " + TL), r(22, 8)], up_reply=True, rcount=2)]}
    f = Fake({1: page})
    assert "all here " + TL in texts(f._comments(vi()))
    assert f.calls == [("list", 1)]


def test_full_pages_are_scanned_for_uploader_comments_only():
    full = [r(100 + i, 5, f"p1-{i}", 100 - i) for i in range(bb.PAGE_SIZE)]
    p2 = [r(200 + i, 5, f"p2-{i}") for i in range(bb.PAGE_SIZE - 1)] + [r(299, UP, "page 2 " + TL)]
    p3 = [r(300, 6, "short page"), r(301, 6, previews=[r(311, UP, "page 3 " + TL)])]
    f = Fake({1: {"replies": full}, 2: {"replies": p2}, 3: {"replies": p3}})
    cs = f._comments(vi())
    assert texts(cs)[:2] == ["page 2 " + TL, "page 3 " + TL]
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


def test_uploader_chatter_beyond_the_first_page_is_left_out():
    # Review reads only the first 10 comments: thank-you replies must not take those places.
    page = {"replies": [r(2, 7, "viewer list " + TL, 9, previews=[r(21, UP, "谢谢支持"), r(22, UP, "03:00 那段")],
                          up_reply=True, rcount=30)]}
    f = Fake({1: page}, threads={(2, 1): [r(23, UP, "一看就是新粉"), r(24, UP, "full " + TL)]})
    assert texts(f._comments(vi())) == ["full " + TL, "viewer list " + TL]


def test_full_width_colons_count_as_times():
    fw = "00：01爱在西元前\n03：59爸，我回来了\n07：45简单爱"
    page = {"replies": [r(2, 7, previews=[r(21, UP, fw)])]}
    assert texts(Fake({1: page})._comments(vi()))[0] == fw


def test_reply_address_prefix_is_removed():
    page = {"replies": [r(2, 7, previews=[r(21, UP, "回复 @某人 :" + TL), r(22, UP, "回复 @x：" + TL + " again")])]}
    cs = Fake({1: page})._comments(vi())
    assert texts(cs) == [TL, TL + " again", "c2"]


def test_each_thread_is_opened_once_and_the_cap_counts_threads(monkeypatch):
    monkeypatch.setattr(bb, "UPLOADER_THREADS", 2)
    pinned = r(1, UP, "pinned", up_reply=True, rcount=9)
    page = {"upper": {"top": pinned}, "top_replies": [pinned],
            "replies": [pinned, r(2, 7, up_reply=True, rcount=9), r(3, 8, up_reply=True, rcount=9)]}
    f = Fake({1: page})
    f._comments(vi())
    assert [c for c in f.calls if c[0] == "thread"] == [("thread", 1, 1), ("thread", 2, 1)]


def test_uploader_replies_rank_after_every_top_level_comment(tmp_path):
    from bili_album.bilibili import Comment
    from bili_album.plan import Planner, PlanOptions

    p = Planner.__new__(Planner)
    p.o = PlanOptions(workdir=str(tmp_path))
    v = vi()
    full = "\n".join(f"{m:02d}:00 song {m}" for m in range(0, 40, 4))
    v.comments = [Comment("00:00 a\n04:00 b\n08:00 c", "up", True, False, 99, reply=True),
                  Comment(full, "fan", False, False, 3)]
    hl = p._hint_lists(v, 2700)
    assert [h.detail for h in hl] == ["comment by fan", "uploader's reply by up"]


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
