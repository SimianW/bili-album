"""Import cut-and-tagged files into the Jellyfin music library, then confirm Jellyfin sees them.

Layout: ``LIBRARY_DIR/<Album Artist>/<Album>/NN-<Title>.m4a`` plus ``cover.jpg|png``
(a single goes to ``<Artist>/<Album or Title>/01-<Title>.m4a``). Files are copied
into a hidden temporary sibling folder on the same filesystem and renamed into
place, so Jellyfin's real-time monitor never sees half-written files. Nothing
is overwritten silently: an existing album folder gives a ``duplicate`` result
unless ``replace=True``.

Entry point: :func:`import_album`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .apply import PlanError, apply_plan, sanitize
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
    jellyfin_album_artist: str = ""
    web_url: str = ""
    refresh: str = ""                # how the scan was triggered
    reason: str = ""
    timings: dict = field(default_factory=dict)

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
    rel = Path(sanitize(album_artist or "Unknown Artist")) / sanitize(album or "Unknown Album")
    return Path(lib) / rel, f"{cfg.jellyfin_library_path.rstrip('/')}/{rel.as_posix()}"


def web_link(cfg: Config, item_id: str, server_id: str) -> str:
    base = cfg.jellyfin_public_url or cfg.jellyfin_url
    return f"{base}/web/#/details?id={item_id}&serverId={server_id}" if base and item_id else ""


# ----------------------------------------------------------------------------- main

def import_album(source: Path | str, *, config: Config | None = None, replace: bool = False,
                 dry_run: bool = False, wait_timeout: float = WAIT_TIMEOUT, poll_interval: float = POLL_INTERVAL,
                 jellyfin: Jellyfin | None = None, on_progress: Callable[[str], None] | None = None,
                 staging_root: Path | None = None, cover: Path | None = None) -> ImportResult:
    """Copy an album (folder, single file or plan.json) into the library and wait for Jellyfin.

    Never raises for Jellyfin trouble: files stay in place and the status is
    ``imported_unconfirmed`` with the reason. Raises :class:`PlanError` /
    ``FileNotFoundError`` for unusable input."""
    cfg = config or load_config()
    t0 = time.monotonic()
    say = on_progress or (lambda m: None)
    st = stage(Path(source), staging_root=staging_root, cover=cover)
    try:
        dest, cpath = destination(cfg, st.album_artist, st.album)
        res = ImportResult("dry_run" if dry_run else "imported", album=st.album, album_artist=st.album_artist,
                           dest=str(dest), container_path=cpath, files=[n for _, n in st.files])
        if dest.exists() and any(dest.iterdir()) and not replace:
            res.status = "duplicate"
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
    _confirm(res, cfg, jellyfin, n_audio, wait_timeout, poll_interval, say)
    res.timings["total_s"] = round(time.monotonic() - t0, 2)
    return res


def _confirm(res: ImportResult, cfg: Config, jf: Jellyfin | None, n_audio: int, wait_timeout: float,
             poll: float, say: Callable[[str], None]) -> None:
    if jf is None:
        if not cfg.jellyfin_ready:
            res.status, res.reason = "imported_unconfirmed", "Jellyfin is not configured"
            return
        jf = Jellyfin(cfg.jellyfin_url, cfg.jellyfin_api_key)
    t0 = time.monotonic()
    try:
        server_id = jf.server_id()
        jf.notify_created(res.container_path)
        res.refresh = "media_updated"
    except JellyfinError as e:
        res.status, res.reason = "imported_unconfirmed", f"Jellyfin unreachable: {e}"
        return
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
                res.jellyfin_track_count = jf.track_count(item["Id"])
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
    elif (res.jellyfin_track_count or 0) < n_audio:
        res.status = "imported_unconfirmed"
        res.reason = f"Jellyfin lists {res.jellyfin_track_count} of {n_audio} tracks after {wait_timeout:.0f}s"


def remove_album(dest: Path | str, *, library_dir: Path | str, remove_empty_artist: bool = True) -> list[str]:
    """Delete one album folder under the library (and its artist folder if that is then empty).
    Refuses anything that is not exactly ``library_dir/<artist>/<album>``."""
    dest = Path(dest).resolve()
    lib = Path(library_dir).resolve()
    if dest.parent.parent != lib or not dest.is_dir():
        raise ValueError(f"{dest} is not an album folder directly under {lib}/<artist>/")
    removed = [str(dest)]
    shutil.rmtree(dest)
    if remove_empty_artist and not any(dest.parent.iterdir()):
        dest.parent.rmdir()
        removed.append(str(dest.parent))
    return removed

