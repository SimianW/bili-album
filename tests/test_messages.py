"""Owner-readable messages: every line a person sees is plain Simplified Chinese."""

import re

import pytest

from bili_album.messages import explain, hold_message, problem_zh, problems_zh, rejected_reason_zh, review_error


class BilibiliError(RuntimeError):
    pass


class HttpError(RuntimeError):
    pass


class PlanError(ValueError):
    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


class DownloadError(Exception):
    pass


LATIN_WORD = re.compile(r"[A-Za-z]{4,}")


@pytest.mark.parametrize("exc,stage,code", [
    (BilibiliError("https://api.bilibili.com/x/web-interface/view: code -404 啥都木有"), "plan", "video_not_found"),
    (BilibiliError("https://api.bilibili.com/x/web-interface/view: code 62002 稿件不可见"), "plan", "video_unavailable"),
    (BilibiliError("https://api.bilibili.com/x/player/playurl: code -10403 抱歉您所在地区不可观看！"), "plan",
     "region_locked"),
    (DownloadError("ERROR: [BiliBili] BV1: This video is geo restricted"), "plan", "region_locked"),
    (BilibiliError("cannot find a BV id in 'hello'"), "plan", "bad_link"),
    (HttpError("GET https://api.bilibili.com/x/web-interface/view failed: HTTP 412 from api.bilibili.com"), "plan",
     "rate_limited"),
    (BilibiliError("every audio URL failed: timeout; timeout"), "plan", "download_failed"),
    (BilibiliError("audio download stalled on every URL"), "plan", "download_failed"),
    (HttpError("GET https://api.bilibili.com/x failed: HTTPSConnectionPool: Max retries exceeded "
               "(Caused by NameResolutionError: Failed to resolve 'api.bilibili.com')"), "plan", "network"),
    (ValueError("/w/source.m4a: no audio stream"), "plan", "audio_unusable"),
    (ValueError("/w/x.flac: codec 'opus' cannot be cut losslessly into .m4a"), "plan", "audio_unusable"),
    (HttpError("netease /api/search/get: answer unrelated to the query (throttled?)"), "plan", "catalog_unavailable"),
    (PermissionError(13, "Permission denied", "/opt/jellyfin/media/music/x"), "import", "library_not_writable"),
    (RuntimeError("BILI_ALBUM_LIBRARY_DIR is not configured"), "import", "library_not_configured"),
    (RuntimeError("apply reported problems: track 2: duration 3.0s vs planned 9.0s"), "cut", "cut_check_failed"),
    (KeyError("tracks"), "cut", "unexpected"),
])
def test_explain_codes_and_plain_chinese(exc, stage, code):
    ex = explain(exc, stage)
    assert ex.code == code
    assert not LATIN_WORD.search(ex.zh.replace("Jellyfin", "")), ex.zh     # no English words for the owner
    assert type(exc).__name__ in ex.detail                                  # technical detail kept apart


def test_unexpected_names_the_class_only_in_detail():
    ex = explain(ZeroDivisionError("division by zero"), "plan")
    assert ex.zh.startswith("处理时出现意外错误") and ex.detail == "ZeroDivisionError: division by zero"


@pytest.mark.parametrize("text,zh", [
    ("tracks 3 and 4 overlap", "第 3 首和第 4 首的时间重叠"),
    ("track 2: duration is not positive", "第 2 首时长为零"),
    ("track 2 'x': duration is not positive (5 -> 5)", "第 2 首时长为零"),
    ("track 4: start 10.0 does not increase after track 3", "第 4 首排在第 3 首之前（顺序颠倒）"),
    ("missing title for track(s) 1, 5", "第 1、5 首缺少歌名"),
    ("missing album_artist for track(s) 2", "第 2 首缺少专辑艺人"),
    ("missing album for track(s) 2", "第 2 首缺少专辑名"),
    ("track 7: outside the source audio", "第 7 首超出了音频范围"),
    ("tracks 1 and 2 are not contiguous (4.5s gap is not exported)", "第 1 首和第 2 首之间有 4.5 秒不会导出"),
    ("plan has no tracks", "方案里没有任何曲目"),
    ("something new and strange", "方案里有一处无法自动处理的问题（详情见日志）"),
])
def test_problem_zh(text, zh):
    assert problem_zh(text) == zh


def test_problems_zh_and_plan_error():
    assert problems_zh(["tracks 3 and 4 overlap", "missing artist for track(s) 2"]) == \
        "第 3 首和第 4 首的时间重叠；第 2 首缺少艺人。请修改后再确认。"
    ex = explain(PlanError(["tracks 3 and 4 overlap"]), "cut")
    assert ex.code == "plan_problems" and ex.zh.endswith("第 3 首和第 4 首的时间重叠。请修改后再确认。")


def test_review_error_variants():
    assert review_error("ModelError: POST http://x/v1/chat/completions failed: Read timed out").code == "review_timeout"
    assert review_error("ConfigError: missing configuration: BILI_ALBUM_LLM_API_KEY").code == "review_not_configured"
    r = review_error("ModelError: invalid reply after one retry")
    assert r.code == "review_failed" and "已跳过复核" in r.zh


def test_hold_message():
    assert hold_message(["album: no catalog candidate found for x"])[0] == "not_identified"
    assert hold_message(["track 2: low confidence"], derivative=True)[0] == "derivative"
    assert hold_message(["track 2: low confidence"])[0] == "needs_confirmation"


@pytest.mark.parametrize("why", [
    "4.2 is not one of the measured options for boundary 3",
    "'Yellow' on Live 2003 is 320.0s, the audio is 270.0s (a different recording)",
    "soldier - 5:20AM (520am(司凤版)) is itself a version, not the original",
    "track 3 has a catalog match and nothing contradicts it",
    "could not check: HttpError: offline",
    "a reason nobody anticipated",
])
def test_rejected_reason_fully_chinese(why):
    r = rejected_reason_zh({"edit": {"op": "move_boundary", "boundary": 3}, "why": why})
    assert r.startswith("工具无法核实“移动切点（第 3 个切点）”，未采纳，需要人工确认：")
    assert not LATIN_WORD.search(r), r
