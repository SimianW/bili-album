"""MP4 tag writing and reading with mutagen.

Artist and album artist hold one value per name (``artists.split_artists``), which Jellyfin reads
as several artists with no library setting, plus the same names in the ``ARTISTS`` /
``ALBUMARTISTS`` freeform tags other taggers use. :func:`read_tags` joins them back with ``"/"``.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .artists import ARTIST_SEP, split_artists

from .media import image_kind

TEXT_ATOMS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album_artist": "aART",
    "album": "\xa9alb",
    "year": "\xa9day",
    "genre": "\xa9gen",
    "comment": "\xa9cmt",
    "composer": "\xa9wrt",
    "lyrics": "\xa9lyr",
}
MULTI = {"artist": "----:com.apple.iTunes:ARTISTS", "album_artist": "----:com.apple.iTunes:ALBUMARTISTS"}
REQUIRED = ("title", "artist", "album", "album_artist", "track")
OPTIONAL = ("disc", "year", "genre", "composer", "lyrics", "comment", "cover")


def write_tags(path: Path, tags: dict, cover: Path | None = None) -> None:
    """``tags`` keys: TEXT_ATOMS names plus track, track_total, disc, disc_total."""
    m = MP4(str(path))
    if m.tags is None:
        m.add_tags()
    t = m.tags
    for key, atom in TEXT_ATOMS.items():
        v = tags.get(key)
        values = (split_artists(str(v)) or [str(v)]) if v and key in MULTI else [str(v)] if v else []
        if values:
            t[atom] = values
        elif atom in t:
            del t[atom]
        if key in MULTI:
            if values:
                t[MULTI[key]] = [MP4FreeForm(x.encode("utf-8")) for x in values]
            elif MULTI[key] in t:
                del t[MULTI[key]]
    if tags.get("track"):
        t["trkn"] = [(int(tags["track"]), int(tags.get("track_total") or 0))]
    if tags.get("disc"):
        t["disk"] = [(int(tags["disc"]), int(tags.get("disc_total") or 0))]
    if cover and cover.exists():
        kind = image_kind(cover)
        if kind in ("jpg", "png"):
            fmt = MP4Cover.FORMAT_PNG if kind == "png" else MP4Cover.FORMAT_JPEG
            t["covr"] = [MP4Cover(cover.read_bytes(), imageformat=fmt)]
    m.save()


def read_tags(path: Path) -> dict:
    m = MP4(str(path))
    t = m.tags or {}
    out: dict = {"duration": round(m.info.length, 3), "bitrate_kbps": int((m.info.bitrate or 0) / 1000),
                 "codec": getattr(m.info, "codec", "")}
    for key, atom in TEXT_ATOMS.items():
        v = t.get(atom)
        out[key] = (ARTIST_SEP.join(map(str, v)) if key in MULTI else str(v[0])) if v else ""
    trk = t.get("trkn")
    out["track"], out["track_total"] = (trk[0] if trk else (0, 0))
    dsk = t.get("disk")
    out["disc"], out["disc_total"] = (dsk[0] if dsk else (0, 0))
    cov = t.get("covr")
    out["cover"] = f"{len(bytes(cov[0]))}B" if cov else ""
    return out
