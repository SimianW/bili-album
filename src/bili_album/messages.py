"""Owner-readable messages: plain Simplified Chinese for what a person sees, technical detail apart.

``explain(exc, stage)`` turns any exception from a pipeline stage into ``Explained(code, zh, detail)``:
``zh`` says what happened and what the owner can do; ``detail`` keeps the class and text for logs;
``code`` is a stable machine-readable key. ``problem_zh`` / ``problems_zh`` translate the tool's
hard-problem lines ("tracks 3 and 4 overlap") into 「第 3 首和第 4 首的时间重叠」.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Explained:
    code: str
    zh: str
    detail: str


Z = {
    "video_not_found": "找不到这个视频：链接可能有误，或视频已被删除。请检查链接后再试。",
    "video_unavailable": "这个视频目前无法访问（可能已删除、仅 UP 主自己可见、需要会员或正在审核）。",
    "region_locked": "这个视频在当前地区无法播放（区域限制），所以无法下载。",
    "bad_link": "这不是可用的 B 站视频链接。请粘贴视频页面的地址（含 BV 号）。",
    "rate_limited": "B 站暂时限制了访问（请求太频繁）。请过几分钟再试。",
    "download_failed": "音频下载反复失败。请稍后再试；如果一直失败，这个视频可能受限。",
    "network": "网络连接失败，连不上需要的服务。请检查网络后再试。",
    "audio_unusable": "这个视频没有可用的音轨，或音频格式不受支持，无法处理。",
    "catalog_unavailable": "音乐曲库暂时查不到（服务繁忙或连不上）。请过几分钟再试。",
    "plan_problems": "方案还有问题，暂时不能导入。",
    "library_not_writable": "无法写入音乐库文件夹（没有权限或磁盘已满）。请检查音乐库文件夹。",
    "library_not_configured": "还没有配置音乐库文件夹，无法导入。",
    "output_exists": "输出文件已存在，为避免覆盖已停止。",
    "jellyfin_unreachable": "文件已放进音乐库，但连不上 Jellyfin，暂时无法确认。稍后在 Jellyfin 里刷新媒体库即可。",
    "not_listed_in_time": "文件已放进音乐库，但 Jellyfin 在规定时间内还没显示这张专辑。稍后刷新媒体库即可。",
    "tracks_incomplete": "文件已放进音乐库，但 Jellyfin 还没显示全部曲目。稍后刷新媒体库即可。",
    "jellyfin_not_configured": "文件已放进音乐库，但没有配置 Jellyfin，无法确认导入结果。",
    "review_failed": "模型复核失败（服务不可用或回答无效），已跳过复核，方案需要人工确认后再导入。",
    "review_timeout": "模型复核超时，已跳过复核，方案需要人工确认后再导入。",
    "review_not_configured": "没有配置复核模型，已跳过复核，方案需要人工确认后再导入。",
    "cut_check_failed": "切分后的检查没通过（时长或标签不对），已停止，没有导入。请重新分析或修改方案后再试。",
    "not_identified": "没能识别出专辑或歌曲。请核对曲名、艺人和专辑后再确认导入，或者放弃。",
    "needs_confirmation": "需要确认：请检查标出的曲目和问题，修改后继续，或者放弃。",
    "derivative": "这是翻唱或改编版本，已按原曲填写歌名和艺人。请确认后继续，或者放弃。",
    "bad_status": "这个任务现在的状态不能继续。",
    "unexpected": "处理时出现意外错误。请稍后重试；如果反复出现，请把日志交给维护者。",
}

_NETWORK = re.compile(r"Failed to resolve|Name or service not known|Temporary failure in name resolution|"
                      r"Network is unreachable|No route to host|Connection refused|Max retries exceeded|"
                      r"ConnectionError|ConnectTimeout|timed out|NewConnectionError|offline:", re.I)


def _detail(exc: BaseException | str) -> str:
    if isinstance(exc, BaseException):
        return f"{type(exc).__name__}: {exc}"[:1000]
    return str(exc)[:1000]


def explain(exc: BaseException | str, stage: str = "") -> Explained:
    """Map an exception raised in ``stage`` (plan / review / cut / import) to a Chinese sentence."""
    detail = _detail(exc)
    name = type(exc).__name__ if isinstance(exc, BaseException) else ""
    text = str(exc)
    problems = getattr(exc, "problems", None)

    def out(code: str, zh: str | None = None) -> Explained:
        return Explained(code, zh or Z[code], detail)

    if "apply reported problems" in text:
        return out("cut_check_failed")
    if problems is not None:                                   # apply.PlanError
        if any("output exists" in p for p in problems):
            return out("output_exists")
        return out("plan_problems", Z["plan_problems"] + problems_zh(problems))
    if isinstance(exc, PermissionError) or "Permission denied" in text or "No space left" in text:
        return out("library_not_writable" if stage == "import" else "unexpected")
    if "LIBRARY_DIR is not configured" in text:
        return out("library_not_configured")
    m = re.search(r"code (-?\d+)", text)
    if name == "BilibiliError" and m:
        code = int(m.group(1))
        if code == -404 or "啥都木有" in text:
            return out("video_not_found")
        if code in (-10403, 6002003) or "地区" in text:
            return out("region_locked")
        if code in (62002, 62004, 62012, -403, 87007, 87008):
            return out("video_unavailable")
        if code == -412:
            return out("rate_limited")
    if "geo" in text.lower() and ("restrict" in text.lower() or "block" in text.lower()):
        return out("region_locked")
    if "cannot find a BV id" in text:
        return out("bad_link")
    if "HTTP 412" in text or "412 Precondition" in text:
        return out("rate_limited")
    if re.search(r"HTTP 404 from api\.bilibili|Video .* does not exist|deleted", text):
        return out("video_not_found")
    if "no audio stream" in text or "cannot be cut losslessly" in text or "no downloadable AAC" in text \
            or "no audio URL" in text or "yt-dlp gave no stream URL" in text:
        return out("audio_unusable")
    if "every audio URL failed" in text or "download stalled" in text or name == "DownloadError":
        return out("download_failed")
    if "netease" in text or "itunes.apple.com" in text:
        return out("catalog_unavailable")
    if _NETWORK.search(text) or name in ("ConnectionError", "ConnectTimeout", "Timeout", "gaierror"):
        return out("jellyfin_unreachable" if stage == "import" else "network")
    return out("unexpected")


def review_error(detail: str) -> Explained:
    """The review stage never raises; ``review.error`` holds the text. Pick the sentence."""
    d = detail or ""
    if "missing configuration" in d:
        return Explained("review_not_configured", Z["review_not_configured"], d)
    if re.search(r"timed out|Timeout|ReadTimeout", d):
        return Explained("review_timeout", Z["review_timeout"], d)
    return Explained("review_failed", Z["review_failed"], d)


def import_reason(code: str) -> str:
    return Z.get(code, Z["unexpected"])


_UNIDENTIFIED = ("no catalog candidate", "no confident catalog match", "recognition scan found nothing",
                 "no album match", "best catalog candidate rejected")


def hold_message(reasons: list[str], derivative: bool = False) -> tuple[str, str]:
    """(code, Chinese line) for a job held after review."""
    if derivative:
        return "derivative", Z["derivative"]
    if any(any(k in r for k in _UNIDENTIFIED) for r in reasons or []):
        return "not_identified", Z["not_identified"]
    return "needs_confirmation", Z["needs_confirmation"]


# ----------------------------------------------------------------------------- hard problems

def _nums(s: str) -> str:
    parts = [p.strip() for p in re.split(r",\s*|\s+and\s+", s) if p.strip()]
    return "、".join(parts)


FIELD_ZH = {"title": "歌名", "artist": "艺人", "album": "专辑名", "album_artist": "专辑艺人"}

_PROBLEMS: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"^plan has no tracks"), lambda m: "方案里没有任何曲目"),
    (re.compile(r"^kind is single but the plan has (\d+) tracks"), lambda m: f"单曲方案里却有 {m[1]} 首"),
    (re.compile(r"^tracks? (\d+)(?: '[^']*'| \"[^\"]*\")?:? ?duration is not positive"),
     lambda m: f"第 {m[1]} 首时长为零"),
    (re.compile(r"^track (\d+).*(?:outside the source|outside the source audio|range .* outside)"),
     lambda m: f"第 {m[1]} 首超出了音频范围"),
    (re.compile(r"^tracks (\d+) and (\d+) overlap"), lambda m: f"第 {m[1]} 首和第 {m[2]} 首的时间重叠"),
    (re.compile(r"^tracks (\d+) and (\d+) are not contiguous \(([\d.]+)s gap"),
     lambda m: f"第 {m[1]} 首和第 {m[2]} 首之间有 {m[3]} 秒不会导出"),
    (re.compile(r"^track (\d+): start .* does not increase after track (\d+)"),
     lambda m: f"第 {m[1]} 首排在第 {m[2]} 首之前（顺序颠倒）"),
    (re.compile(r"^missing (title|artist|album_artist|album) for track\(s\) ([\d, ]+)"),
     lambda m: f"第 {_nums(m[2])} 首缺少{FIELD_ZH[m[1]]}"),
    (re.compile(r"^track (\d+): source audio .* not found"), lambda m: f"第 {m[1]} 首的音频文件找不到了"),
    (re.compile(r"^two tracks map to the same file name"), lambda m: "有两首歌的文件名相同"),
]


def problem_zh(text: str) -> str:
    t = (text or "").strip()
    for pat, fn in _PROBLEMS:
        m = pat.search(t)
        if m:
            return fn(m)
    return "方案里有一处无法自动处理的问题（详情见日志）"


def problems_zh(problems: list[str]) -> str:
    """「第 3 首和第 4 首的时间重叠；第 2 首缺少歌名。请修改后再确认。」"""
    items = list(dict.fromkeys(problem_zh(p) for p in problems))
    if not items:
        return ""
    return "；".join(items) + "。请修改后再确认。"


# ----------------------------------------------------------------------------- rejected edits

OP_ZH = {"choose_album": "更换专辑", "propose_album": "更换专辑", "relabel_track": "修改曲名/艺人",
         "reorder_tracks": "调整曲目顺序", "drop_track": "删除曲目", "split_track": "拆分曲目",
         "move_boundary": "移动切点", "mark_derivative": "标记为衍生版本", "replan": "重新分析"}

_WHY: list[tuple[str, str]] = [
    ("not found in the catalog tracklist", "在专辑曲目表、音频识别、投稿文字和曲库里都找不到这个名字"),
    ("not one of the measured options", "目标时间不是测得的候选切点"),
    ("not one of the split options", "拆分点不是测得的候选点"),
    ("a different recording", "曲库里这首歌的时长与音频不符，可能是另一个录音版本"),
    ("is itself a version", "所给的原曲本身就是翻唱或改编版本"),
    ("found no original", "曲库里找不到所说的原曲"),
    ("found no album", "曲库里找不到这张专辑"),
    ("has no track", "这张专辑里没有这首歌"),
    ("no album candidate", "候选专辑里没有这一项"),
    ("already the chosen album", "这已经是当前选中的专辑"),
    ("nothing contradicts it", "这首歌有曲库匹配，没有证据表明应该删除"),
    ("would have no length", "移动后曲目时长会变成零"),
    ("would come before", "移动后曲目时长会变成零"),
    ("needs a re-plan", "更换多曲目方案的专辑需要重新分析，但现在不能再重新分析"),
    ("re-plan is not available", "已经重新分析过一次，不能再重新分析"),
    ("already requested", "已经要求过重新分析"),
    ("permutation", "调整后的顺序不完整"),
    ("neither recognition nor the catalog length", "调整顺序没有得到音频识别或曲库时长的支持"),
    ("already split", "这一首已经拆分过"),
    ("was dropped", "这一首已经被删除"),
    ("no track", "方案里没有这一首"),
    ("no change", "与现有标签相同，无需修改"),
    ("multi-part", "分P视频没有共同的时间轴，不能移动切点或拆分"),
    ("cannot drop the only track", "不能删除唯一的一首"),
    ("no previous track", "前面没有可以合并的曲目"),
    ("no next track", "后面没有可以合并的曲目"),
    ("original_title is needed", "多曲目视频需要给出原曲名"),
    ("no catalog available", "曲库暂时不可用"),
    ("could not check", "核实时查询曲库失败"),
]


def why_zh(why: str) -> str:
    w = why or ""
    for key, zh in _WHY:
        if key in w:
            return zh
    return "现有证据无法核实"


def rejected_reason_zh(item: dict) -> str:
    """Fully Chinese reason for a rejected edit record ``{"edit": {...}, "why": "..."}``."""
    e = item.get("edit") or {}
    what = OP_ZH.get(e.get("op"), "修改")
    if e.get("op") == "move_boundary":
        what += f"（第 {e.get('boundary')} 个切点）"
    elif e.get("track"):
        what += f"（第 {e.get('track')} 首）"
    return f"工具无法核实“{what}”，未采纳，需要人工确认：{why_zh(item.get('why', ''))}。"
