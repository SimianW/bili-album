"""Library import with a fake Jellyfin (needs ffmpeg for the tiny test files, no network)."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from bili_album.config import Config
from bili_album.library import JellyfinError, import_album, remove_album
from bili_album.tag import write_tags

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def tone(path: Path, tags: dict, seconds: float = 1.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-b:a", "64k", str(path)], check=True)
    write_tags(path, tags)
    return path


@pytest.fixture
def album_dir(tmp_path: Path) -> Path:
    d = tmp_path / "out" / "范特西"
    d.mkdir(parents=True)
    for i, title in enumerate(["爱在西元前", "爸，我回来了"], 1):
        tone(d / f"{i:02d}-{title}.m4a", {"title": title, "artist": "周杰伦", "album": "范特西",
                                          "album_artist": "周杰伦", "track": i, "track_total": 2})
    from PIL import Image
    Image.new("RGB", (16, 16)).save(d / "cover.jpg", "JPEG")
    return d


class FakeJellyfin:
    def __init__(self, appear_after: int = 1, tracks: int = 2, down: bool = False):
        self.calls: list[str] = []
        self.polls = 0
        self.appear_after = appear_after
        self.tracks = tracks
        self.down = down

    def server_id(self):
        if self.down:
            raise JellyfinError("connection refused")
        return "srv1"

    def notify_created(self, path):
        self.calls.append(f"notify {path}")

    def refresh_library(self):
        self.calls.append("refresh")

    def album_by_path(self, path):
        self.polls += 1
        if self.appear_after is None or self.polls < self.appear_after:
            return None
        return {"Id": "item1", "AlbumArtist": "周杰伦", "Path": path}

    def track_count(self, item_id):
        return self.tracks


def cfg(tmp_path: Path) -> Config:
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    return Config(jellyfin_url="http://jf", jellyfin_public_url="https://music.example", jellyfin_api_key="k",
                  library_dir=lib, jellyfin_library_path="/media/music")


def test_import_album_and_confirm(tmp_path, album_dir):
    c = cfg(tmp_path)
    jf = FakeJellyfin(appear_after=2)
    res = import_album(album_dir, config=c, jellyfin=jf, poll_interval=0.01)
    dest = c.library_dir / "周杰伦" / "范特西"
    assert res.status == "imported", res.reason
    assert res.dest == str(dest) and res.container_path == "/media/music/周杰伦/范特西"
    assert sorted(p.name for p in dest.iterdir()) == ["01-爱在西元前.m4a", "02-爸，我回来了.m4a", "cover.jpg"]
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755 and stat.S_IMODE(dest.parent.stat().st_mode) == 0o755
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o644 for p in dest.iterdir())
    assert res.item_id == "item1" and res.jellyfin_track_count == 2 and res.created_artist_dir
    assert res.web_url == "https://music.example/web/#/details?id=item1&serverId=srv1"
    assert jf.calls[0] == "notify /media/music/周杰伦/范特西"
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]   # no temp folders left


def test_duplicate_then_replace(tmp_path, album_dir):
    c = cfg(tmp_path)
    import_album(album_dir, config=c, jellyfin=FakeJellyfin(), poll_interval=0.01)
    dest = c.library_dir / "周杰伦" / "范特西"
    (dest / "extra.m4a").write_bytes(b"x")
    dup = import_album(album_dir, config=c, jellyfin=FakeJellyfin(), poll_interval=0.01)
    assert dup.status == "duplicate" and "extra.m4a" in dup.existing
    assert (dest / "extra.m4a").exists()
    rep = import_album(album_dir, config=c, jellyfin=FakeJellyfin(), poll_interval=0.01, replace=True)
    assert rep.status == "imported" and rep.replaced
    assert not (dest / "extra.m4a").exists() and len(list(dest.iterdir())) == 3


def test_dry_run_copies_nothing(tmp_path, album_dir):
    c = cfg(tmp_path)
    res = import_album(album_dir, config=c, dry_run=True, jellyfin=FakeJellyfin())
    assert res.status == "dry_run" and not any(c.library_dir.iterdir())
    assert res.files == ["01-爱在西元前.m4a", "02-爸，我回来了.m4a", "cover.jpg"]


def test_unconfirmed_when_jellyfin_down_or_slow(tmp_path, album_dir):
    c = cfg(tmp_path)
    down = import_album(album_dir, config=c, jellyfin=FakeJellyfin(down=True))
    assert down.status == "imported_unconfirmed" and "unreachable" in down.reason
    assert (c.library_dir / "周杰伦" / "范特西" / "cover.jpg").exists()      # files stay
    shutil.rmtree(c.library_dir / "周杰伦")
    slow = import_album(album_dir, config=c, jellyfin=FakeJellyfin(appear_after=None), wait_timeout=0.3,
                        poll_interval=0.05)
    assert slow.status == "imported_unconfirmed" and "did not list" in slow.reason
    shutil.rmtree(c.library_dir / "周杰伦")
    partial = import_album(album_dir, config=c, jellyfin=FakeJellyfin(tracks=1), wait_timeout=0.3,
                           poll_interval=0.05)
    assert partial.status == "imported_unconfirmed" and "1 of 2" in partial.reason


def test_single_goes_to_artist_album_folder(tmp_path):
    c = cfg(tmp_path)
    f = tone(tmp_path / "Coldplay - Yellow.m4a", {"title": "Yellow", "artist": "Coldplay", "album": "Parachutes",
                                                  "album_artist": "Coldplay", "track": 5})
    from PIL import Image
    cover = tmp_path / "c.png"
    Image.new("RGB", (16, 16)).save(cover, "PNG")
    res = import_album(f, config=c, jellyfin=FakeJellyfin(tracks=1), poll_interval=0.01, cover=cover)
    assert res.status == "imported"
    assert sorted(p.name for p in (c.library_dir / "Coldplay" / "Parachutes").iterdir()) == \
        ["01-Yellow.m4a", "cover.png"]


def test_remove_album_guard(tmp_path, album_dir):
    c = cfg(tmp_path)
    res = import_album(album_dir, config=c, jellyfin=FakeJellyfin(), poll_interval=0.01)
    with pytest.raises(ValueError):
        remove_album(c.library_dir / "周杰伦", library_dir=c.library_dir)
    removed = remove_album(res.dest, library_dir=c.library_dir)
    assert removed == [res.dest, str(c.library_dir / "周杰伦")] and not any(c.library_dir.iterdir())
