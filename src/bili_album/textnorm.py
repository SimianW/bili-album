"""Name normalisation and fuzzy matching across scripts and widths.

``norm`` folds full-width to half-width (NFKC), fancy Unicode letters
(mathematical bold etc.) to ASCII, Traditional to Simplified Chinese, case,
and punctuation, so ``萬能青年旅店`` == ``万能青年旅店`` and ``Ｈｉ-Ｒｅｓ`` == ``hires``.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

try:
    import opencc  # opencc-python-reimplemented

    _T2S = opencc.OpenCC("t2s")
except Exception:  # pragma: no cover - optional
    _T2S = None

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)
_BRACKETED_SUFFIX = re.compile(
    r"\s*[\(\[（【][^\)\]）】]*(feat|ft\.|with|remaster|version|ver\.|live|demo|edit|mix|伴奏|版)[^\)\]）】]*[\)\]）】]",
    re.IGNORECASE,
)


def fold(s: str) -> str:
    """NFKC + Traditional->Simplified, keeping case and punctuation."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    if _T2S is not None:
        s = _T2S.convert(s)
    return s


@lru_cache(maxsize=8192)
def norm(s: str) -> str:
    s = fold(s).casefold()
    return _PUNCT.sub("", s)


def strip_decorations(s: str) -> str:
    """Drop ``(feat. X)``, ``[Remastered]`` style suffixes."""
    return _BRACKETED_SUFFIX.sub("", s).strip()


_CN_DIGITS = str.maketrans("〇零一二三四五六七八九", "00123456789")


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two names after normalisation (containment counts high)."""
    base = _similarity(a, b)
    if base < 1.0:
        da, db = a.translate(_CN_DIGITS), b.translate(_CN_DIGITS)
        if (da, db) != (a, b):
            base = max(base, _similarity(da, db) - 0.02)
    return base


def _similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    sa, sb = norm(strip_decorations(a)), norm(strip_decorations(b))
    if sa and sb and sa == sb:
        return 0.97
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 2 and short in long_:
        return 0.75 + 0.2 * len(short) / len(long_)
    return SequenceMatcher(None, na, nb).ratio()


def contains(haystack: str, needle: str) -> bool:
    n = norm(needle)
    return bool(n) and n in norm(haystack)
