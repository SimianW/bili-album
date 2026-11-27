"""Several artists in one name field: how they are recognised, written and filed.

Jellyfin (through the ATL tag reader) turns a tag that holds several values into several
artists with no library setting. Splitting one value on delimiters only happens when the owner
turns on the library's custom tag delimiters (default ``/ | ; \\``). So the tool:

- writes ``"/"`` between names wherever it builds a combined name (catalogs, title guesses),
  ``ARTIST_SEP``, the first of Jellyfin's default delimiters;
- writes every name as its own value in ``©ART`` / ``aART`` and in the ``ARTISTS`` /
  ``ALBUMARTISTS`` freeform tags (:mod:`bili_album.tag`), and reads them back joined with ``"/"``;
- files an album under its first album artist (``LIBRARY_DIR/<first>/<album>``).

:func:`split_artists` recognises names joined with Jellyfin's delimiters (``/ | ; \\`` and their
full-width forms), ``、``, and ``feat.`` / ``ft.``. ``&`` and commas are part of many names
(Simon & Garfunkel, Earth, Wind & Fire), so they split only when every part is written in CJK
script, as in ``周杰伦 & 费玉清`` from a storefront. ``NO_SPLIT`` holds names that contain a
delimiter themselves.
"""

from __future__ import annotations

import re

ARTIST_SEP = "/"
NO_SPLIT = ("AC/DC", "AC／DC")
_DELIMS = re.compile(r"\s*[/／|｜;；\\、]\s*|(?:\s+|\s*[(（\[【]\s*)(?:feat\.?|ft\.?|featuring)\s+", re.I)
_CJK_JOIN = re.compile(r"\s*(?:&|＆|,|，)\s*")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")


def _cjk_name(s: str) -> bool:
    return bool(_CJK.search(s)) and not _LATIN.search(s)


def split_artists(value: str) -> list[str]:
    """The names in one artist field, in order, without repeats ("" -> [])."""
    s = (value or "").strip()
    if not s:
        return []
    keep = {}
    for i, name in enumerate(NO_SPLIT):
        if name.casefold() in s.casefold():
            token = f"\x00{i}\x00"
            s = re.sub(re.escape(name), token, s, flags=re.I)
            keep[token] = name
    out: list[str] = []
    for part in _DELIMS.split(s):
        pieces = _CJK_JOIN.split(part)
        if len(pieces) > 1 and not all(_cjk_name(p) for p in pieces if p.strip()):
            pieces = [part]
        for p in pieces:
            for token, name in keep.items():
                p = p.replace(token, name)
            p = p.strip().strip("()（）[]【】").strip()
            if p and p.casefold() not in {x.casefold() for x in out}:
                out.append(p)
    return out


def join_artists(names) -> str:
    """One field for several names: ``"A/B"``."""
    out: list[str] = []
    for n in names:
        for x in split_artists(n):
            if x.casefold() not in {y.casefold() for y in out}:
                out.append(x)
    return ARTIST_SEP.join(out)


def first_artist(value: str) -> str:
    """The first name of a field (the library folder for an album)."""
    names = split_artists(value)
    return names[0] if names else ""
