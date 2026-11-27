"""Import cut-and-tagged files into the Jellyfin music library, then confirm Jellyfin sees them.

Layout: ``LIBRARY_DIR/<first Album Artist>/<Album>/NN-<Title>.m4a`` plus ``cover.jpg|png``
(a single goes to ``<Artist>/<Album or Title>/01-<Title>.m4a``). Files are copied
into a hidden temporary sibling folder on the same filesystem and renamed into
place, so Jellyfin's real-time monitor never sees half-written files. Nothing
is overwritten silently: an existing album folder gives a ``duplicate`` result
unless ``replace=True``.

After Jellyfin lists the album, :func:`normalize_script` puts back the tool's Simplified
Chinese where Jellyfin's own metadata fetcher (MusicBrainz) replaced or duplicated a value in
Traditional script (周杰倫 next to 周杰伦). It is driven by the tags the tool wrote, touches only the
album just imported and its tracks, and sets ``LockData`` on the items it corrects: Jellyfin 12.2
has no lockable artist field (``LockedFields`` covers Name, Genres, Cast, ...), and without the lock a
metadata refresh with "replace all" brought the Traditional artist back in the live test.

Entry point: :func:`import_album`.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .apply import PlanError, apply_plan, first_artist, sanitize
from .config import Config, load_config
from .media import image_kind
from .net import Http, HttpError
from .tag import read_tags

log = logging.getLogger(__name__)

AUDIO_EXT = {".m4a", ".mp3", ".flac", ".ogg", ".opus", ".wav", ".aac", ".alac", ".wma", ".ape"}
DIR_MODE = 0o755
FILE_MODE = 0o644
WAIT_TIMEOUT = 180.0
POLL_INTERVAL = 3.0
# Jellyfin 12.2 accepted the path-scoped notice (POST /Library/Media/Updated) but did not act on it within
# 60 s in the live test; the full scan (POST /Library/Refresh) did. Both are sent, the full scan after this.
NARROW_GRACE = 10.0
SETTLE_TIMEOUT = 45.0     # seconds to keep correcting after the lock (the online fetch ran ~10 s after listing)
SCAN_TIMEOUT = 120.0


@dataclass
class ImportResult:
    status: str                      # imported | imported_unconfirmed | duplicate | dry_run | failed
    album: str = ""
    album_artist: str = ""
    dest: str = ""                   # host folder
    container_path: str = ""         # the same folder as Jellyfin sees it
    files: list[str] = field(default_factory=list)       # file names written (or to be written)
    existing: list[str] = field(default_factory=list)    # duplicate: what the folder already holds
    replaced: bool = False
    created_artist_dir: bool = False
    item_id: str = ""
    jellyfin_track_count: int | None = None
    track_count: int | None = None   # same as jellyfin_track_count (alias the web page reads)
    jellyfin_album_artist: str = ""
    web_url: str = ""
    refresh: str = ""                # how the scan was triggered
    reason: str = ""                 # English detail
    reason_code: str = ""            # jellyfin_unreachable | not_listed_in_time | tracks_incomplete |
                                     # jellyfin_not_configured | duplicate | ""
    timings: dict = field(default_factory=dict)
    # Traditional -> Simplified corrections made in Jellyfin: {item, type, name, field, from, to}
    normalized: list[dict] = field(default_factory=list)
    normalize_locked: list[str] = field(default_factory=list)    # item ids given LockData=true
    normalize_verified: bool | None = None   # still Simplified after a full library scan (None: nothing to do)
    leftover_artists: list[str] = field(default_factory=list)    # Traditional artist entries still present
    normalize_error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class JellyfinError(RuntimeError):
    pass


class Jellyfin:
    """Tiny client: auth header ``Authorization: MediaBrowser Token="..."`` (12.x rejects X-Emby-Token)."""

    def __init__(self, url: str, api_key: str, *, http: Http | None = None, timeout: float = 20.0):
        self.url = url.rstrip("/")
        self._key = api_key
        self.http = http or Http(None, retries=1, timeout=timeout)

    def _h(self) -> dict:
        return {"Authorization": f'MediaBrowser Token="{self._key}"', "Content-Type": "application/json"}

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        try:
            raw = self.http.get_bytes(self.url + path, params=params, method=method, cache=False,
                                      headers=self._h(),
                                      data=json.dumps(body).encode() if body is not None else None)
        except HttpError as e:
            msg = str(e).replace(self._key, "***") if self._key else str(e)
            raise JellyfinError(msg) from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def server_id(self) -> str:
        return (self._call("GET", "/System/Info") or {}).get("Id", "")

    def notify_created(self, container_path: str) -> None:
        """Path-scoped change notice (``POST /Library/Media/Updated``)."""
        self._call("POST", "/Library/Media/Updated",
                   body={"Updates": [{"Path": container_path, "UpdateType": "Created"}]})

    def refresh_library(self) -> None:
        self._call("POST", "/Library/Refresh")

    def album_by_path(self, container_path: str) -> dict | None:
        j = self._call("GET", "/Items", {"IncludeItemTypes": "MusicAlbum", "Recursive": "true",
                                         "Fields": "Path", "Limit": "10000"}) or {}
        want = container_path.rstrip("/")
        return next((i for i in j.get("Items") or [] if (i.get("Path") or "").rstrip("/") == want), None)

    def album_count(self) -> int:
        j = self._call("GET", "/Items", {"IncludeItemTypes": "MusicAlbum", "Recursive": "true", "Limit": "0"}) or {}
        return int(j.get("TotalRecordCount") or 0)

    def track_count(self, album_id: str) -> int:
        j = self._call("GET", "/Items", {"ParentId": album_id, "IncludeItemTypes": "Audio", "Recursive": "true",
                                         "Limit": "0"}) or {}
        return int(j.get("TotalRecordCount") or 0)

    # -- item metadata (reading needs a user id with an API key; any administrator will do) --------

    @functools.cached_property
    def admin_user_id(self) -> str:
        users = self._call("GET", "/Users") or []
        uid = next((u["Id"] for u in users if (u.get("Policy") or {}).get("IsAdministrator")), "")
        if not uid:
            raise JellyfinError("no administrator user to read item metadata with")
        return uid

    def get_item(self, item_id: str) -> dict:
        return self._call("GET", f"/Items/{item_id}", {"userId": self.admin_user_id}) or {}

    def item_exists(self, item_id: str) -> bool:
        try:
            return bool(self.get_item(item_id).get("Id"))
        except JellyfinError as e:
            if "404" in str(e):
                return False
            raise

    def update_item(self, item_id: str, dto: dict) -> None:
        """``POST /Items/{id}`` takes the whole item: send back what get_item returned, modified."""
        self._call("POST", f"/Items/{item_id}", body=dto)

    def delete_item(self, item_id: str) -> None:
        """Remove an item record (used only for the tool's own album after its files are gone)."""
        try:
            self._call("DELETE", f"/Items/{item_id}")
        except JellyfinError as e:
            if "404" not in str(e):
                raise

    def album_tracks(self, album_id: str) -> list[dict]:
        j = self._call("GET", "/Items", {"ParentId": album_id, "IncludeItemTypes": "Audio", "Recursive": "true",
                                         "Fields": "Path"}) or {}
        return j.get("Items") or []

    def library_scan_running(self) -> bool:
        tasks = self._call("GET", "/ScheduledTasks") or []
        t = next((x for x in tasks if x.get("Key") == "RefreshLibrary"), {})
        return t.get("State") not in (None, "Idle")


# ----------------------------------------------------------------------------- source

@dataclass
class Staged:
    album: str
    album_artist: str
    files: list[tuple[Path, str]]        # (source path, destination file name)
    tmpdir: Path | None = None           # apply output we made and should delete


def _album_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
                  and p.suffix.lower() in AUDIO_EXT)


def _cover_in(folder: Path) -> Path | None:
    return next((folder / f"cover.{k}" for k in ("jpg", "png") if (folder / f"cover.{k}").is_file()), None)


def stage(source: Path, *, staging_root: Path | None = None, cover: Path | None = None) -> Staged:
    """Work out what to copy: an album folder from ``apply``, a single ``.m4a``, or a ``plan.json``
    (cut and tagged first into a temporary folder)."""
    source = Path(source)
    if source.suffix == ".json":
        plan = json.loads(source.read_text(encoding="utf-8"))
        root = Path(tempfile.mkdtemp(prefix="import-", dir=staging_root or source.parent))
        try:
            res = apply_plan(source, root)
        except PlanError:
            shutil.rmtree(root, ignore_errors=True)
            raise
        if res.warnings:
            shutil.rmtree(root, ignore_errors=True)
            raise PlanError([f"apply warning: {w}" for w in res.warnings])
        if res.folder is None:
            st = _single(res.files[0])
            cov = Path(plan["album"].get("cover_path") or "")
            if cov.is_file() and image_kind(cov) in ("jpg", "png"):
                st.files.append((cov, f"cover.{image_kind(cov)}"))
        else:
            st = _folder(res.folder)
        st.tmpdir = root
        return st
    if source.is_dir():
        return _folder(source)
    if source.is_file() and source.suffix.lower() == ".m4a":
        st = _single(source)
        if cover and Path(cover).is_file() and image_kind(Path(cover)) in ("jpg", "png"):
            st.files.append((Path(cover), f"cover.{image_kind(Path(cover))}"))
        return st
    raise FileNotFoundError(f"not an album folder, .m4a file or plan.json: {source}")


def _folder(folder: Path) -> Staged:
    audio = _album_files(folder)
    if not audio:
        raise FileNotFoundError(f"no audio files in {folder}")
    tags = read_tags(audio[0])
    files = [(p, p.name) for p in audio]
    cov = _cover_in(folder)
    if cov:
        files.append((cov, cov.name))
    return Staged(tags["album"] or folder.name, tags["album_artist"] or tags["artist"], files)


def _single(path: Path) -> Staged:
    tags = read_tags(path)
    title = tags["title"] or path.stem
    return Staged(tags["album"] or title, tags["album_artist"] or tags["artist"],
                  [(path, f"01-{sanitize(title)}.m4a")])


# ----------------------------------------------------------------------------- copy

def _copy_into_place(st: Staged, dest: Path, replace: bool) -> tuple[bool, bool]:
    """Returns (created_artist_dir, replaced)."""
    artist_dir = dest.parent
    created_artist = not artist_dir.exists()
    if created_artist:
        artist_dir.mkdir(parents=True)
        os.chmod(artist_dir, DIR_MODE)
    tmp = artist_dir / f".bili-album-{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        for src, name in st.files:
            target = tmp / name
            shutil.copyfile(src, target)
            os.chmod(target, FILE_MODE)
        os.chmod(tmp, DIR_MODE)
        # Verify before anything becomes visible.
        got = sorted(p.name for p in tmp.iterdir())
        want = sorted(name for _, name in st.files)
        if got != want:
            raise OSError(f"copy verification failed: expected {want}, found {got}")
        for src, name in st.files:
            if (tmp / name).stat().st_size != src.stat().st_size:
                raise OSError(f"copy verification failed: size of {name} differs")
        replaced = False
        if dest.exists():
            if not replace:
                raise FileExistsError(str(dest))
            old = artist_dir / f".bili-album-old-{uuid.uuid4().hex[:8]}"
            dest.rename(old)
            tmp.rename(dest)
            shutil.rmtree(old)
            replaced = True
        else:
            tmp.rename(dest)
        return created_artist, replaced
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if created_artist and artist_dir.exists() and not any(artist_dir.iterdir()):
            artist_dir.rmdir()
        raise


def destination(cfg: Config, album_artist: str, album: str) -> tuple[Path, str]:
    lib = cfg.library_dir
    if lib is None:
        raise JellyfinError("BILI_ALBUM_LIBRARY_DIR is not configured")
    rel = Path(sanitize(first_artist(album_artist) or "Unknown Artist")) / sanitize(album or "Unknown Album")
    return Path(lib) / rel, f"{cfg.jellyfin_library_path.rstrip('/')}/{rel.as_posix()}"


def web_link(cfg: Config, item_id: str, server_id: str) -> str:
    base = cfg.jellyfin_public_url or cfg.jellyfin_url
    return f"{base}/web/#/details?id={item_id}&serverId={server_id}" if base and item_id else ""


# ----------------------------------------------------------------------------- main

def import_album(source: Path | str, *, config: Config | None = None, replace: bool = False,
                 dry_run: bool = False, wait_timeout: float = WAIT_TIMEOUT, poll_interval: float = POLL_INTERVAL,
                 jellyfin: Jellyfin | None = None, on_progress: Callable[[str], None] | None = None,
                 staging_root: Path | None = None, cover: Path | None = None,
                 settle_timeout: float = SETTLE_TIMEOUT, dest_dir: str | None = None) -> ImportResult:
    """Copy an album (folder, single file or plan.json) into the library and wait for Jellyfin.

    Never raises for Jellyfin trouble: files stay in place and the status is
    ``imported_unconfirmed`` with the reason. Raises :class:`PlanError` /
    ``FileNotFoundError`` for unusable input."""
    cfg = config or load_config()
    t0 = time.monotonic()
    say = on_progress or (lambda m: None)
    st = stage(Path(source), staging_root=staging_root, cover=cover)
    # The tags the tool wrote, per destination file name: what Jellyfin should show.
    tags = {name: read_tags(src) for src, name in st.files if Path(name).suffix.lower() == ".m4a"}
    try:
        dest, cpath = destination(cfg, st.album_artist, st.album)
        if dest_dir:   # tests / throwaway copies: "<artist folder>/<album folder>" under the library
            rel = Path(*[sanitize(x) for x in Path(dest_dir).parts])
            if len(rel.parts) != 2:
                raise ValueError("dest_dir must be '<artist folder>/<album folder>'")
            dest = Path(cfg.library_dir) / rel
            cpath = f"{cfg.jellyfin_library_path.rstrip('/')}/{rel.as_posix()}"
        res = ImportResult("dry_run" if dry_run else "imported", album=st.album, album_artist=st.album_artist,
                           dest=str(dest), container_path=cpath, files=[n for _, n in st.files])
        if dest.exists() and any(dest.iterdir()) and not replace:
            res.status, res.reason_code = "duplicate", "duplicate"
            res.existing = sorted(p.name for p in dest.iterdir() if not p.name.startswith("."))
            res.reason = f"{dest} already holds {len(res.existing)} file(s); pass replace=True to replace this album"
            return res
        if dry_run:
            res.reason = "dry run: nothing copied"
            res.replaced = dest.exists()
            return res
        if dest.exists() and not any(dest.iterdir()):
            dest.rmdir()
        say(f"copying {len(st.files)} files to {dest}")
        res.created_artist_dir, res.replaced = _copy_into_place(st, dest, replace)
        res.timings["copy_s"] = round(time.monotonic() - t0, 2)
    finally:
        if st.tmpdir:
            shutil.rmtree(st.tmpdir, ignore_errors=True)
    n_audio = sum(1 for _, n in st.files if Path(n).suffix.lower() in AUDIO_EXT)
    jf = _confirm(res, cfg, jellyfin, n_audio, wait_timeout, poll_interval, say)
    if res.status == "imported" and res.item_id and jf is not None:
        say("checking Jellyfin's names against the tags")
        t1 = time.monotonic()
        normalize_script(res, jf, tags, settle_timeout=settle_timeout, poll=poll_interval,
                         scan_timeout=SCAN_TIMEOUT)
        res.timings["normalize_s"] = round(time.monotonic() - t1, 2)
    res.timings["total_s"] = round(time.monotonic() - t0, 2)
    return res


def _confirm(res: ImportResult, cfg: Config, jf: Jellyfin | None, n_audio: int, wait_timeout: float,
             poll: float, say: Callable[[str], None]) -> Jellyfin | None:
    if jf is None:
        if not cfg.jellyfin_ready:
            res.status, res.reason = "imported_unconfirmed", "Jellyfin is not configured"
            res.reason_code = "jellyfin_not_configured"
            return None
        jf = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key)
    t0 = time.monotonic()
    try:
        server_id = jf.server_id()
        jf.notify_created(res.container_path)
        res.refresh = "media_updated"
    except JellyfinError as e:
        res.status, res.reason = "imported_unconfirmed", f"Jellyfin unreachable: {e}"
        res.reason_code = "jellyfin_unreachable"
        return None
    say("waiting for Jellyfin to list the album")
    full_scan = False
    item = None
    last_err = ""
    while time.monotonic() - t0 < wait_timeout:
        try:
            item = jf.album_by_path(res.container_path) or item
            if item:
                res.item_id = item["Id"]
                res.jellyfin_album_artist = item.get("AlbumArtist", "")
                res.jellyfin_track_count = res.track_count = jf.track_count(item["Id"])
                if res.jellyfin_track_count >= n_audio and res.jellyfin_album_artist:
                    break
            if not full_scan and time.monotonic() - t0 > NARROW_GRACE:
                jf.refresh_library()
                full_scan = True
                res.refresh = "media_updated+library_refresh"
        except JellyfinError as e:
            last_err = str(e)
        time.sleep(poll)
    res.timings["jellyfin_s"] = round(time.monotonic() - t0, 2)
    if res.item_id:
        res.web_url = web_link(cfg, res.item_id, server_id)
    if not item:
        res.status = "imported_unconfirmed"
        res.reason = f"Jellyfin did not list the album within {wait_timeout:.0f}s" + (f" ({last_err})" if last_err else "")
        res.reason_code = "jellyfin_unreachable" if last_err else "not_listed_in_time"
    elif (res.jellyfin_track_count or 0) < n_audio:
        res.status = "imported_unconfirmed"
        res.reason = f"Jellyfin lists {res.jellyfin_track_count} of {n_audio} tracks after {wait_timeout:.0f}s"
        res.reason_code = "tracks_incomplete"
    return jf


# ----------------------------------------------------------------------------- Simplified Chinese in Jellyfin

@functools.lru_cache(maxsize=1)
def _opencc_t2s():
    from opencc import OpenCC
    return OpenCC("t2s")


def t2s(text: str) -> str:
    return _opencc_t2s().convert(text or "")


_ARTIST_SPLIT = re.compile(r"\s*(?:&|/|;|、|,|，|\bfeat\.?|\bft\.?)\s*", re.I)


def tag_artists(*values: str) -> list[str]:
    """Names a tag stands for: the whole value and its parts ("A & B" -> "A & B", "A", "B")."""
    out: list[str] = []
    for v in values:
        for x in [v, *_ARTIST_SPLIT.split(v or "")]:
            x = (x or "").strip()
            if x and x not in out:
                out.append(x)
    return out


def fix_name(current: str, tagged: str) -> str | None:
    """The tagged value when Jellyfin shows the same text in Traditional script, else None."""
    if current and tagged and current != tagged and t2s(current) == tagged:
        return tagged
    return None


def fix_artists(current: list[str], tagged: list[str]) -> tuple[list[str], list[str]] | None:
    """(new list, removed names) when an entry is a Traditional-script variant of a tagged artist:
    replaced by the tagged spelling, or dropped when that spelling is already there. Names that are
    not variants of a tagged artist (other scripts, other people) are kept as they are."""
    allowed = set(tagged)
    out: list[str] = []
    removed: list[str] = []
    for name in current:
        target = name if name in allowed else (t2s(name) if t2s(name) in allowed else None)
        if target is None:
            out.append(name)
        elif target in out:
            removed.append(name)
        else:
            out.append(target)
            if target != name:
                removed.append(name)
    return (out, removed) if removed else None


def _names(pairs) -> list[str]:
    return [p.get("Name", "") for p in pairs or [] if p.get("Name")]


def _plan_fixes(dto: dict, want: dict) -> list[tuple[str, object, object, list[str]]]:
    """[(field, old, new, removed artist names)] for one item. ``want`` keys: name, artists, album_artists."""
    fixes = []
    new = fix_name(dto.get("Name", ""), want.get("name", ""))
    if new:
        fixes.append(("Name", dto.get("Name"), new, []))
    for fld, key in (("ArtistItems", "artists"), ("AlbumArtists", "album_artists")):
        if want.get(key):
            cur = _names(dto.get(fld))
            got = fix_artists(cur, want[key])
            if got:
                fixes.append((fld, cur, got[0], got[1]))
    return fixes


def _apply_fixes(jf: Jellyfin, dto: dict, fixes: list) -> None:
    for fld, _old, new, _rm in fixes:
        if fld == "Name":
            dto["Name"] = new
        elif fld == "ArtistItems":
            dto["ArtistItems"] = [{"Name": n} for n in new]
            dto["Artists"] = list(new)
        elif fld == "AlbumArtists":
            dto["AlbumArtists"] = [{"Name": n} for n in new]
            dto["AlbumArtist"] = new[0] if new else ""
    dto["LockData"] = True
    jf.update_item(dto["Id"], dto)


def _wants(tags: dict[str, dict], tracks: list[dict]) -> tuple[dict, dict[str, dict]]:
    vals = list(tags.values())
    album = {"name": next((t["album"] for t in vals if t.get("album")), ""),
             "album_artists": tag_artists(*{t.get("album_artist", "") for t in vals}),
             "artists": tag_artists(*{t.get("artist", "") for t in vals})}
    per: dict[str, dict] = {}
    for tr in tracks:
        t = tags.get(Path(tr.get("Path") or "").name)
        if t:
            per[tr["Id"]] = {"name": t.get("title", ""), "artists": tag_artists(t.get("artist", "")),
                             "album_artists": tag_artists(t.get("album_artist", ""))}
    return album, per


def _scan_fixes(jf: Jellyfin, album_id: str, tags: dict[str, dict]) -> list[tuple[dict, list]]:
    tracks = jf.album_tracks(album_id)
    want_album, want_tracks = _wants(tags, tracks)
    out = []
    dto = jf.get_item(album_id)
    fx = _plan_fixes(dto, want_album)
    if fx:
        out.append((dto, fx))
    for tid, want in want_tracks.items():
        tdto = jf.get_item(tid)
        fx = _plan_fixes(tdto, want)
        if fx:
            out.append((tdto, fx))
    return out


_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")


def is_chinese_album(tags: dict[str, dict]) -> bool:
    """Tags carry Chinese text: Han characters and no kana anywhere (kana means Japanese)."""
    vals = [str(t.get(k) or "") for t in tags.values() for k in ("title", "artist", "album", "album_artist")]
    return any(_HAN.search(v) for v in vals) and not any(_KANA.search(v) for v in vals)


def lock_item(jf: Jellyfin, item_id: str) -> bool:
    """Set ``LockData`` (Jellyfin passes it on to an album's tracks). False when already locked."""
    dto = jf.get_item(item_id)
    if dto.get("LockData"):
        return False
    dto["LockData"] = True
    jf.update_item(item_id, dto)
    return True


def normalize_script(res: ImportResult, jf: Jellyfin, tags: dict[str, dict], *, settle_timeout: float = SETTLE_TIMEOUT,
                     poll: float = POLL_INTERVAL, scan_timeout: float = SCAN_TIMEOUT, scan: bool = True,
                     apply: bool = True) -> list[tuple[dict, list]]:
    """Keep the album just imported (``res.item_id``) and its tracks in the tool's Simplified Chinese.

    Only for albums whose tags contain Chinese. Jellyfin's online fetch (MusicBrainz) runs about
    10 s after the scan lists a new album and, inside one refresh, sets the provider ids before it
    merges extra album artists, so a single check can come too early (the 2026-11-27 web import).
    So: lock the album first (``LockData``; later refreshes skip the online providers), keep
    correcting for ``settle_timeout`` seconds in case a fetch was already running, then run a full
    library scan and check once more. ``apply=False`` only reports what would change. Never raises:
    problems become ``normalize_error`` and a warning; the import stays imported."""
    if not tags or not is_chinese_album(tags):
        return []
    planned: list[tuple[dict, list]] = []
    try:
        if not apply:
            dto = jf.get_item(res.item_id)
            planned = _scan_fixes(jf, res.item_id, tags)
            if not dto.get("LockData"):
                res.normalize_locked.append(res.item_id)       # would lock
            return planned
        if lock_item(jf, res.item_id):
            res.normalize_locked.append(res.item_id)
        artist_ids: dict[str, str] = {}
        t0 = time.monotonic()
        while True:
            _fix_round(res, jf, tags, artist_ids)
            if time.monotonic() - t0 >= settle_timeout:
                break
            time.sleep(poll)
        res.timings["settle_s"] = round(time.monotonic() - t0, 2)
        if scan:
            # Make sure it sticks: a full library scan, then compare again (and fix again if needed).
            jf.refresh_library()
            res.refresh = (res.refresh + "+" if res.refresh else "") + "normalize_scan"
            t1 = time.monotonic()
            time.sleep(min(3.0, poll))
            while time.monotonic() - t1 < scan_timeout and jf.library_scan_running():
                time.sleep(poll)
            again = _fix_round(res, jf, tags, artist_ids)
            res.normalize_verified = not again
            if again:
                res.warnings.append("Jellyfin showed Traditional-script names again after a library scan "
                                    "(corrected again): " + "; ".join(f"{d.get('Name')}: {f[0]}" for d, fx in again
                                                                      for f in fx))
            # The scan deletes artist entries nothing points at; report any that stayed (never deleted here).
            res.leftover_artists = [n for n, i in artist_ids.items() if jf.item_exists(i)]
    except Exception as e:  # the import itself succeeded; this is a cosmetic correction
        res.normalize_error = f"{type(e).__name__}: {e}"[:300]
        res.warnings.append(f"could not correct Traditional-script names in Jellyfin: {res.normalize_error}")
        log.warning("normalize: %s", res.normalize_error)
    return planned


def _fix_round(res: ImportResult, jf: Jellyfin, tags: dict[str, dict], artist_ids: dict[str, str]) -> list:
    fixes = _scan_fixes(jf, res.item_id, tags)
    for dto, fx in fixes:
        for fld, _old, _new, removed in fx:
            if fld in ("ArtistItems", "AlbumArtists"):
                for pr in dto.get(fld) or []:
                    if pr.get("Name") in removed and pr.get("Id"):
                        artist_ids[pr["Name"]] = pr["Id"]
        _apply_fixes(jf, dto, fx)
        if dto["Id"] not in res.normalize_locked:
            res.normalize_locked.append(dto["Id"])
        for fld, old, new, _rm in fx:
            res.normalized.append({"item": dto["Id"], "type": dto.get("Type", ""), "name": dto.get("Name", ""),
                                   "field": fld, "from": old, "to": new})
    return fixes


def remove_album(dest: Path | str, *, library_dir: Path | str, remove_empty_artist: bool = True,
                 jellyfin: Jellyfin | None = None, config: Config | None = None) -> list[str]:
    """Delete one album folder under the library (and its artist folder if that is then empty).
    Refuses anything that is not exactly ``library_dir/<artist>/<album>``.

    With ``jellyfin`` and ``config`` it also deletes the album's record in Jellyfin and asks for a
    library scan. Jellyfin 12.2 keeps the album record (an orphan whose artist entry then stays
    alive) when the album's artist folder disappears, with or without the tool's lock (checked live);
    ``DELETE /Items/{id}`` removes it, and the scan then drops artist entries nothing points at."""
    dest = Path(dest).resolve()
    lib = Path(library_dir).resolve()
    if dest.parent.parent != lib or not dest.is_dir():
        raise ValueError(f"{dest} is not an album folder directly under {lib}/<artist>/")
    album_id = ""
    if jellyfin is not None and config is not None:
        rel = dest.relative_to(lib).as_posix()
        item = jellyfin.album_by_path(f"{config.jellyfin_library_path.rstrip('/')}/{rel}")
        album_id = item["Id"] if item else ""
    removed = [str(dest)]
    shutil.rmtree(dest)
    if remove_empty_artist and not any(dest.parent.iterdir()):
        dest.parent.rmdir()
        removed.append(str(dest.parent))
    if jellyfin is not None:
        if album_id:
            jellyfin.delete_item(album_id)
            removed.append(f"jellyfin:{album_id}")
        jellyfin.refresh_library()
    return removed



# ----------------------------------------------------------------------------- repair albums already in the library

TOOL_COMMENT = re.compile(r"bilibili\.com/video/|b23\.tv/", re.I)


@dataclass
class FixResult:
    folder: str
    status: str = "unchanged"        # fixed | unchanged | would_fix | skipped | error
    reason: str = ""
    move: dict | None = None         # {"from": host folder, "to": host folder}
    item_id_before: str = ""
    item_id: str = ""
    album: str = ""
    album_artist: str = ""
    jellyfin_track_count: int | None = None
    normalized: list[dict] = field(default_factory=list)          # applied (or, dry run, planned) corrections
    normalize_locked: list[str] = field(default_factory=list)
    normalize_verified: bool | None = None
    leftover_artists: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def album_tags(folder: Path) -> dict[str, dict]:
    return {p.name: read_tags(p) for p in _album_files(folder) if p.suffix.lower() == ".m4a"}


def is_tool_album(tags: dict[str, dict]) -> bool:
    """Files carry the tool's source comment (the Bilibili link apply writes into ``©cmt``)."""
    return bool(tags) and all(TOOL_COMMENT.search(t.get("comment") or "") for t in tags.values())


def library_album_dirs(library_dir: Path) -> list[Path]:
    lib = Path(library_dir)
    return sorted(a for artist in lib.iterdir() if artist.is_dir() and not artist.name.startswith(".")
                  for a in artist.iterdir() if a.is_dir() and not a.name.startswith("."))


def fix_library(folders: list[Path | str] | None = None, *, all_albums: bool = False, dry_run: bool = False,
                config: Config | None = None, jellyfin: Jellyfin | None = None,
                wait_timeout: float = WAIT_TIMEOUT, poll_interval: float = POLL_INTERVAL,
                settle_timeout: float = SETTLE_TIMEOUT, on_progress: Callable[[str], None] | None = None
                ) -> list[FixResult]:
    """Repair albums already in the library: put each folder where its tags say
    (``<Album Artist>/<Album>/``) and run the tag-driven Simplified-Chinese correction (and lock).

    ``folders``: album folders (absolute, or relative to the library). ``all_albums``: every album
    whose files all carry the tool's source comment; albums added by hand are never selected.
    ``dry_run`` reports the planned move and corrections and changes nothing."""
    cfg = config or load_config()
    if cfg.library_dir is None:
        raise JellyfinError("BILI_ALBUM_LIBRARY_DIR is not configured")
    lib = Path(cfg.library_dir).resolve()
    say = on_progress or (lambda m: None)
    jf = jellyfin or (Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key) if cfg.jellyfin_ready else None)
    targets: list[Path] = []
    for f in folders or []:
        p = Path(f)
        targets.append((p if p.is_absolute() else lib / p).resolve())
    if all_albums:
        targets += [d.resolve() for d in library_album_dirs(lib) if is_tool_album(album_tags(d))]
    out: list[FixResult] = []
    for folder in dict.fromkeys(targets):
        r = FixResult(str(folder))
        out.append(r)
        try:
            _fix_one(r, folder, lib, cfg, jf, dry_run, wait_timeout, poll_interval, settle_timeout, say)
        except Exception as e:
            r.status, r.reason = "error", f"{type(e).__name__}: {e}"[:300]
    return out


def _cpath(cfg: Config, lib: Path, folder: Path) -> str:
    return f"{cfg.jellyfin_library_path.rstrip('/')}/{folder.relative_to(lib).as_posix()}"


def _fix_one(r: FixResult, folder: Path, lib: Path, cfg: Config, jf: Jellyfin | None, dry_run: bool,
             wait_timeout: float, poll: float, settle: float, say) -> None:
    if folder.parent.parent != lib or not folder.is_dir():
        r.status, r.reason = "skipped", "not an album folder directly under <library>/<artist>/"
        return
    tags = album_tags(folder)
    if not tags:
        r.status, r.reason = "skipped", "no .m4a files"
        return
    vals = list(tags.values())
    r.album = next((t["album"] for t in vals if t.get("album")), "") or folder.name
    r.album_artist = next((t.get("album_artist") or t.get("artist") for t in vals
                           if t.get("album_artist") or t.get("artist")), "") or folder.parent.name
    want, want_cpath = destination(cfg, r.album_artist, r.album)
    old_cpath = _cpath(cfg, lib, folder)
    move = want.resolve() != folder
    if move:
        r.move = {"from": str(folder), "to": str(want)}
        if want.exists() and any(want.iterdir()):
            r.status, r.reason = "error", f"cannot move: {want} already exists and is not empty"
            return
    item = jf.album_by_path(old_cpath) if jf else None
    r.item_id_before = item["Id"] if item else ""
    if dry_run:
        r.status = "would_fix" if move else "unchanged"
        if item and not move:
            probe = ImportResult("imported", item_id=item["Id"])
            planned = normalize_script(probe, jf, tags, apply=False)
            r.normalized = [{"item": d["Id"], "type": d.get("Type", ""), "name": d.get("Name", ""), "field": f[0],
                             "from": f[1], "to": f[2]} for d, fx in planned for f in fx]
            r.normalize_locked = probe.normalize_locked
            if r.normalized or r.normalize_locked:
                r.status = "would_fix"
        elif move and is_chinese_album(tags):
            r.reason = "after the move: lock the album and correct Traditional-script names"
        return
    if jf is None:
        r.status, r.reason = "error", "Jellyfin is not configured"
        return
    res = ImportResult("imported", album=r.album, album_artist=r.album_artist, dest=str(folder),
                       container_path=old_cpath, files=sorted(tags))
    if move:
        say(f"moving {folder} -> {want}")
        artist_dir = want.parent
        if not artist_dir.exists():
            artist_dir.mkdir(parents=True)
            os.chmod(artist_dir, DIR_MODE)
        if want.exists():
            want.rmdir()                     # empty (checked above)
        os.rename(folder, want)              # same filesystem: atomic
        os.chmod(want, DIR_MODE)
        if not any(folder.parent.iterdir()):
            folder.parent.rmdir()
        # The moved album gets a new path, hence a new Jellyfin item; the old record would stay as
        # an orphan (as after a folder delete). Delete it now that its path no longer exists.
        if r.item_id_before and not folder.exists():
            jf.delete_item(r.item_id_before)
        res.dest, res.container_path = str(want), want_cpath
        n_audio = len(_album_files(want))
        _confirm(res, cfg, jf, n_audio, wait_timeout, poll, say)
        if res.status != "imported" or not res.item_id:
            r.status, r.reason = "error", f"moved, but Jellyfin has not listed the album yet: {res.reason}"
            r.warnings += res.warnings
            return
    else:
        if not item:
            r.status, r.reason = "error", "Jellyfin does not list this album"
            return
        res.item_id = item["Id"]
    normalize_script(res, jf, tags, settle_timeout=settle if move else 0.0, poll=poll)
    r.item_id = res.item_id
    r.jellyfin_track_count = res.jellyfin_track_count if move else jf.track_count(res.item_id)
    r.normalized, r.normalize_locked = res.normalized, res.normalize_locked
    r.normalize_verified, r.leftover_artists = res.normalize_verified, res.leftover_artists
    r.warnings += res.warnings
    r.status = "fixed" if (move or r.normalized or r.normalize_locked) else "unchanged"
