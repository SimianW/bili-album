"""Text hints: timestamp tracklists and album/artist guesses from uploader text.

Everything here is pure (no I/O) so it is easy to unit test.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .artists import join_artists, split_artists
from .textnorm import fold, norm

# ----------------------------------------------------------------------------- timestamps

_TS = re.compile(r"(?<![\d.:])(?:(\d{1,2}):)?(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?(?![\d:])")
_RANGE_SEP = re.compile(r"^\s*(?:-|~|–|—|至|到|→|->)\s*$")
_NUMBERING = re.compile(r"^\s*(?:(?:no|track|tr)\.?\s*)?(\d{1,3})(?:\s*[.、．)）:：\-]\s*|\s+(?=\D))", re.I)
_P_PREFIX = re.compile(r"^\s*p\s*(\d{1,3})\s*[.、:：\-]?\s*", re.I)
_EDGE_PUNCT = " \t　-–—_|｜:：,，、.。;；~·•*#>→"
_BRACKETS = {"《": "》", "「": "」", "『": "』", "〈": "〉", "【": "】", "[": "]", "(": ")", "（": "）"}


@dataclass
class HintEntry:
    start: float
    title: str
    artist: str = ""
    end: float | None = None


@dataclass
class HintList:
    source: str           # chapter | description | comment | part | tracklist
    detail: str
    entries: list[HintEntry]
    by_uploader: bool = False
    pinned: bool = False

    @property
    def has_times(self) -> bool:
        return any(e.start >= 0 for e in self.entries)


def ts_to_seconds(h: str | None, m: str, s: str, frac: str | None = None) -> float:
    v = int(h or 0) * 3600 + int(m) * 60 + int(s)
    if frac:
        v += float("0." + frac)
    return float(v)


def fmt_ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{int(m)}:{s:05.2f}"


def clean_title(raw: str) -> tuple[str, str]:
    """Strip numbering, brackets and separators. Returns (title, artist)."""
    t = raw.strip()
    t = re.sub(r"[（(]\s*[）)]", "", t)          # parens left empty after removing a time
    t = re.sub(r"\[\s*\]", "", t)
    t = t.strip(_EDGE_PUNCT)
    t = _P_PREFIX.sub("", t)
    m = _NUMBERING.match(t)
    if m and len(t) > m.end():
        t = t[m.end():]
    t = t.strip(_EDGE_PUNCT)
    artist = ""
    # "《Title》 - Artist" / "Title - Artist" (spaced dash only, to keep hyphenated names)
    mm = re.match(r"^[《「『](.+?)[》」』]\s*(?:[-–—/]\s*(.+))?$", t)
    if mm:
        t, artist = mm.group(1), (mm.group(2) or "")
    else:
        parts = re.split(r"\s+[-–—/]\s+", t)
        if len(parts) == 2 and parts[0] and parts[1]:
            t, artist = parts
    t = t.strip(_EDGE_PUNCT + "《》「」『』")
    artist = artist.strip(_EDGE_PUNCT + "《》「」『』")
    return t, artist


def _line_entries(line: str) -> list[HintEntry]:
    matches = list(_TS.finditer(line))
    if not matches:
        return []
    # Merge "a - b" ranges.
    items: list[tuple[int, int, float, float | None]] = []
    i = 0
    while i < len(matches):
        m = matches[i]
        st = ts_to_seconds(*m.groups())
        end = None
        span_end = m.end()
        if i + 1 < len(matches) and _RANGE_SEP.match(line[m.end():matches[i + 1].start()]):
            end = ts_to_seconds(*matches[i + 1].groups())
            span_end = matches[i + 1].end()
            i += 1
        items.append((m.start(), span_end, st, end))
        i += 1
    if len(items) == 1:
        a, b, st, end = items[0]
        title, artist = clean_title(line[:a] + " " + line[b:])
        return [HintEntry(st, title, artist, end)] if title else []
    # Several timestamps on one line: decide whether titles follow or precede times.
    before_first = line[: items[0][0]]
    prefix_clean = clean_title(before_first)[0]
    title_first = bool(prefix_clean) and not re.fullmatch(r"[\d\s.、]*", prefix_clean)
    out = []
    for k, (a, b, st, end) in enumerate(items):
        if title_first:
            seg = line[(items[k - 1][1] if k else 0):a]
        else:
            seg = line[b:(items[k + 1][0] if k + 1 < len(items) else len(line))]
        title, artist = clean_title(seg)
        if title:
            out.append(HintEntry(st, title, artist, end))
    return out


def parse_timestamps(text: str) -> list[HintEntry]:
    """Every (time, title) pair in a block of text, in reading order."""
    text = unicodedata.normalize("NFKC", text or "")
    out: list[HintEntry] = []
    for line in re.split(r"[\r\n]+", text):
        out.extend(_line_entries(line))
    return out


def plausible_list(entries: list[HintEntry], duration: float | None) -> list[HintEntry] | None:
    """Keep a timestamp list only if it looks like a tracklist (>= 2 increasing, in range)."""
    if len(entries) < 2:
        return None
    starts = [e.start for e in entries]
    if any(b <= a for a, b in zip(starts, starts[1:])):
        # Allow a list written in order with one duplicate line; otherwise reject.
        uniq = sorted({e.start: e for e in entries}.values(), key=lambda e: e.start)
        if len(uniq) < 2 or [e.start for e in uniq] != sorted(set(starts)) or len(uniq) < len(entries) - 1:
            return None
        entries = uniq
    if duration and (entries[-1].start >= duration + 1 or entries[0].start > 0.5 * duration):
        return None
    if duration and len(entries) >= 3:
        gaps = [b.start - a.start for a, b in zip(entries, entries[1:])]
        if min(gaps) < 15:  # "03:12 this part is great" style chatter
            return None
    return entries


def parse_tracklist_file(text: str) -> list[HintEntry]:
    """``--tracklist`` file: one ``title``, ``mm:ss title`` or ``title - artist`` per line."""
    out = []
    for line in (text or "").splitlines():
        line = unicodedata.normalize("NFKC", line).strip()
        if not line or line.startswith("#"):
            continue
        ents = _line_entries(line)
        if ents:
            out.append(ents[0])
            continue
        title, artist = clean_title(line)
        if title:
            out.append(HintEntry(-1.0, title, artist))
    return out


def part_title(name: str, video_title: str = "") -> str:
    """Bilibili part names like ``p01 01.回`` or ``<video title> p02 Lucky Charm``."""
    s = unicodedata.normalize("NFKC", name or "").strip()
    vt = unicodedata.normalize("NFKC", video_title or "").strip()
    if vt and s.startswith(vt) and len(s) > len(vt):
        s = s[len(vt):]
    s = _P_PREFIX.sub("", s.strip())
    title, _ = clean_title(s)
    return title or s


# ----------------------------------------------------------------------------- title guesses

NOISE_WORDS = [
    "hi-res", "hires", "hi res", "无损", "臻享", "臻品", "整轨", "全专辑", "专辑", "全专", "纯享", "音源", "高音质",
    "高品质", "音质", "hq", "sq", "flac", "24bit", "16bit", "96khz", "192khz", "48khz", "杜比", "全景声",
    "dolby", "atmos", "中字", "中英字幕", "双语字幕", "字幕", "翻译", "歌词", "动态歌词", "分p", "无缝", "经典",
    "最新", "新专", "出道", "官方", "mv", "完整版", "合集", "首发", "循环", "推荐", "珍藏", "典藏", "收藏",
    "4k", "8k", "1080p", "60帧", "母带", "车载", "必听", "神专", "神级", "巅峰", "封神", "宝藏", "全网",
    "音乐", "单曲", "原声", "ost", "lyrics", "lyric", "audio", "official", "full album", "album",
    "华语乐坛", "华语", "欧美", "日语", "韩语", "粤语", "另类", "之作", "节日快乐", "祝大家",
    # Credits and release chatter that are never part of a name ("黄霄雲 原创专辑《…》").
    "原创", "个人原创", "首张", "第一张", "新歌", "新曲", "试听", "黑胶", "纯享版",
]
# Descriptive phrases that uploaders put before the artist ("在专业录音棚听 周杰伦《搁浅》",
# "百万级装备听《Yellow》"): listening set-ups and verbs, not names.
_DESCRIPTIVE = re.compile(
    r"听|聆听|欣赏|感受|体验|沉浸|带你|录音棚|装备|耳机|音响|设备|百万|千万|万元|顶级|发烧|"
    r"\blisten|\bheadphones?\b|\bstudio\b")

# Cues that the upload is a derivative of a song (cover, fan edit, AI remake, band or
# instrument rendition, remix). "X版" counts unless X is a release word (完整版, 无损版 ...).
_DERIVATIVE_WORDS = re.compile(
    r"翻唱|翻弹|翻奏|翻跳|cover|改编|改編|remix|混音版|dj\s*(?:版|remix|mix)|"
    r"(?<![a-z])ai(?![a-z])|演奏|弹奏|弹唱|指弹|八音盒|music\s*box|戏腔|"
    r"piano\s*(?:ver|version|cover)|guitar\s*(?:ver|version|cover)", re.I)
_X_VERSION = re.compile(r"([^\s《》「」『』【】\[\]（）()|｜,，!！?？。~～⚡]{1,8})版")
_NOT_DERIVATIVE_VERSION = re.compile(
    r"^(?:.*(?:完整|高清|无损|官方|正式|录音室|原|原唱|专辑|单曲|标准|豪华|加长|抢先|歌词|字幕|"
    r"音质|纯享|4k|8k|hi-?res|mv|原声|电影|剧场|伴奏|新|现场|live|收藏|典藏|纪念|初回|通常|限定))$", re.I)
_NOISE_RE = re.compile("|".join(sorted((re.escape(w) for w in NOISE_WORDS), key=len, reverse=True)), re.I)
_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")
_WORK_BRACKETS = re.compile(r"[《「『〈]([^》」』〉]{1,80})[》」』〉]")
_META_BRACKETS = re.compile(r"[【\[]([^】\]]{0,60})[】\]]")
_SEP = re.compile(r"\s*(?:[|｜/／·•]|\s{2,}|\s-\s|\s–\s|\s—\s|——|!|！|\?|？|,|，|。|~|～|⚡|❤|♪|☆|★|“|”|\")\s*")


@dataclass
class TitleGuess:
    works: list[str] = field(default_factory=list)      # album or song names, best first
    artists: list[str] = field(default_factory=list)    # best first
    year: str = ""
    extra: list[str] = field(default_factory=list)      # other meaningful fragments

    def queries(self, limit: int = 6) -> list[str]:
        """Search strings, most specific first, de-duplicated (at most ``limit``)."""
        qs: list[str] = []
        for w in self.works[:3]:
            for a in self.artists[:2]:
                qs.append(f"{a} {w}")
        qs.extend(self.works[:3])
        frags = self.extra[:3]
        for a in self.artists[:1]:
            for e in frags[:2]:
                qs.append(f"{a} {e}")
        qs.extend(frags)
        qs.extend(self.artists[:1])
        if self.year:
            for a in self.artists[:1] or self.works[:1]:
                qs.append(f"{a} {self.year}")
        seen, out = set(), []
        for q in qs:
            k = norm(q)
            if k and k not in seen:
                seen.add(k)
                out.append(q.strip())
        return out[:limit]


def _denoise(s: str) -> str:
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[（(][^）)]*[）)]", lambda m: " " if not norm(_NOISE_RE.sub("", m.group(0))) else m.group(0), s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(_EDGE_PUNCT + "()（）[]【】")


def _meaningful(s: str) -> bool:
    n = norm(s)
    if len(n) < 1:
        return False
    if _YEAR.fullmatch(n):
        return False
    # Emoji-only or digits-only fragments are not names.
    return any(ch.isalpha() for ch in n)


def clean_artist(frag: str) -> str:
    """Drop descriptive words around a name: "在专业录音棚听 周杰伦" -> "周杰伦".

    Space-separated tokens that read as a listening set-up or verb phrase are removed;
    a single token that is itself descriptive ("百万级装备听") leaves nothing."""
    toks = [x for x in re.split(r"\s+", frag.strip()) if x]
    keep = [x for x in toks if not _DESCRIPTIVE.search(x.casefold())]
    return " ".join(keep).strip(_EDGE_PUNCT)


def name_like(s: str) -> bool:
    """Could ``s`` be an artist name rather than a sentence fragment from a title?"""
    s = (s or "").strip()
    if not s or len(s) > 40 or _DESCRIPTIVE.search(s.casefold()) or _NOISE_RE.search(s):
        return False
    if len(re.split(r"\s+", s)) > 4:
        return False
    return not re.search(r"[!！?？。，,~～…]", s)


def derivative_cues(title: str) -> list[str]:
    """Words in a video title saying it is a derivative of a song (cover, fan edit,
    AI remake, band or instrument rendition, remix). Empty when none."""
    t = fold(title or "")
    out: list[str] = []
    for m in _DERIVATIVE_WORDS.finditer(t):
        w = m.group(0).strip()
        if w and w.casefold() not in {x.casefold() for x in out}:
            out.append(w)
    for m in _X_VERSION.finditer(t):
        x = m.group(1)
        if not _NOT_DERIVATIVE_VERSION.match(x) and not _NOISE_RE.fullmatch(x):
            w = x + "版"
            if w not in out:
                out.append(w)
    return out


def guess_from_title(title: str) -> TitleGuess:
    t = fold(title)
    g = TitleGuess()
    ym = _YEAR.search(t)
    if ym:
        g.year = ym.group(1)
    for m in _WORK_BRACKETS.finditer(t):
        w = _denoise(m.group(1))
        if _meaningful(w):
            g.works.append(w)
    # Artist: the fragment right before the first work bracket.
    first = _WORK_BRACKETS.search(t)
    if first:
        before = _META_BRACKETS.sub(" ", t[: first.start()])
        before = _NOISE_RE.sub(" | ", before)
        # Separators kept: "周杰伦/费玉清《…》" names two artists joined by "/" (or "／"), and the
        # name right before the bracket must not lose the one before it.
        parts = re.split(f"({_SEP.pattern})", before)
        for i in range(len(parts) - 1, -1, -2):
            frag = clean_artist(_denoise(_YEAR.sub(" ", parts[i])))
            if not _meaningful(frag):
                continue
            names = [frag]
            j = i - 2
            while j >= 0 and len(names) < 4 and parts[j + 1].strip() in ("/", "／"):
                prev = clean_artist(_denoise(_YEAR.sub(" ", parts[j])))
                if not (_meaningful(prev) and name_like(prev) and name_like(names[0])):
                    break
                names.insert(0, prev)
                j -= 2
            g.artists.append("/".join(names))
            break
        if not g.artists:
            # "《Title》- Artist" / "《Title》 by Artist": the name follows the bracket.
            after = _META_BRACKETS.sub(" | ", t[first.end():])
            m = re.match(r"^\s*(?:[-–—]|by\s)\s*([^|｜【\[《]+)", after, re.I)
            if m:
                frag = clean_artist(_denoise(_SEP.split(m.group(1).strip())[0]))
                if _meaningful(frag) and name_like(frag):
                    g.artists.append(frag)
    rest = _WORK_BRACKETS.sub(" | ", t)
    meta = [_denoise(m.group(1)) for m in _META_BRACKETS.finditer(rest)]
    rest = _META_BRACKETS.sub(" | ", rest)
    frags = []
    # Noise words act as separators too: "Starboy全专翻译（无缝） 另类R&B" -> Starboy | R&B.
    rest = _NOISE_RE.sub(" | ", rest)
    for seg in _SEP.split(rest):
        # "2020 - 冀西南林路行" => drop the year and the dash.
        seg = _YEAR.sub(" ", seg)
        seg = re.sub(r"^\s*[-–—]\s*|\s*[-–—]\s*$", "", seg)
        d = clean_artist(_denoise(seg))
        if _meaningful(d) and norm(d) not in {norm(x) for x in g.works + g.artists}:
            frags.append(d)
    explicit = bool(re.search(r"[|｜]|\s[-–—]\s", _META_BRACKETS.sub(" ", t)))
    if not g.works:
        # "Artist | Album" or "Artist - Album" layout without brackets. Without an
        # explicit separator we cannot tell which fragment is which, so every
        # fragment becomes a work candidate and the catalog decides.
        if len(frags) >= 2 and explicit:
            g.artists.append(frags[0])
            g.works.append(frags[1])
            g.extra.extend(frags[2:])
        elif frags:
            g.works.extend(frags[:3])
            g.extra.extend(frags[3:])
    else:
        g.extra.extend(frags)
    g.extra.extend(m for m in meta if _meaningful(m))
    # Several artists in one name: one "/"-joined entry first, then each name (for searches).
    multi: list[str] = []
    for a in g.artists:
        names = split_artists(a)
        multi += [x for x in [join_artists(names)] + (names if len(names) > 1 else []) if x not in multi]
    g.artists = multi
    return g


def guess_from_hint(hint: str) -> TitleGuess:
    """``--album-hint "ARTIST - ALBUM"``."""
    h = unicodedata.normalize("NFKC", hint).strip()
    parts = re.split(r"\s+[-–—]\s+|\s*[|｜]\s*", h, maxsplit=1)
    if len(parts) == 2:
        return TitleGuess(works=[parts[1].strip()], artists=[parts[0].strip()])
    return TitleGuess(works=[h])


def names_in_prose(text: str) -> list[str]:
    """Song names quoted in 《》 inside a description, in order, de-duplicated."""
    out, seen = [], set()
    for m in _WORK_BRACKETS.finditer(fold(text or "")):
        n = m.group(1).strip()
        if n and norm(n) not in seen:
            seen.add(norm(n))
            out.append(n)
    return out
