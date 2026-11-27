"""Library import with a fake Jellyfin (needs ffmpeg for the tiny test files, no network)."""

from __future__ import annotations

import json
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


# ----------------------------------------------------------------------------- Simplified Chinese in Jellyfin

class StoreJellyfin(FakeJellyfin):
    """Fake with item metadata: the album gets a Traditional duplicate artist (as MusicBrainz did live)."""

    def __init__(self, album_artists=("周杰伦", "周杰倫"), track_names=None, relapse=False, fail_update=False):
        super().__init__(appear_after=1, tracks=2)
        self.relapse = relapse
        self.fail_update = fail_update
        self.updates: list[dict] = []
        self.items = {"item1": {"Id": "item1", "Type": "MusicAlbum", "Name": "范特西",
                                "ProviderIds": {"MusicBrainzAlbum": "x"},
                                "AlbumArtists": [{"Name": n, "Id": f"a-{n}"} for n in album_artists],
                                "ArtistItems": [{"Name": "周杰伦", "Id": "a-周杰伦"}]}}
        names = track_names or ["爱在西元前", "爸，我回来了"]
        for i, n in enumerate(names, 1):
            self.items[f"t{i}"] = {"Id": f"t{i}", "Type": "Audio", "Name": n,
                                   "Path": f"/media/music/周杰伦/范特西/{i:02d}-{['爱在西元前', '爸，我回来了'][i - 1]}.m4a",
                                   "ArtistItems": [{"Name": "周杰伦", "Id": "a-周杰伦"}],
                                   "AlbumArtists": [{"Name": "周杰伦", "Id": "a-周杰伦"}]}
        self.alive = {"a-周杰倫"}

    def get_item(self, item_id):
        return json.loads(json.dumps(self.items[item_id]))

    def item_exists(self, item_id):
        return item_id in self.alive

    def update_item(self, item_id, dto):
        if self.fail_update:
            raise JellyfinError("HTTP 500 from jf")
        self.updates.append(dto)
        self.items[item_id] = dto

    def album_tracks(self, album_id):
        return [self.items[k] for k in self.items if k.startswith("t")]

    def library_scan_running(self):
        return False

    def refresh_library(self):
        super().refresh_library()
        self.alive.discard("a-周杰倫")          # the scan deletes artists nothing points at
        if self.relapse:
            self.items["item1"]["AlbumArtists"].append({"Name": "周杰倫", "Id": "a-周杰倫"})


def test_normalizes_traditional_duplicate_and_locks(tmp_path, album_dir):
    c = cfg(tmp_path)
    jf = StoreJellyfin()
    res = import_album(album_dir, config=c, jellyfin=jf, poll_interval=0.01)
    assert res.status == "imported"
    assert [a["Name"] for a in jf.items["item1"]["AlbumArtists"]] == ["周杰伦"]
    assert jf.items["item1"]["LockData"] is True and jf.items["item1"]["AlbumArtist"] == "周杰伦"
    assert res.normalized == [{"item": "item1", "type": "MusicAlbum", "name": "范特西", "field": "AlbumArtists",
                               "from": ["周杰伦", "周杰倫"], "to": ["周杰伦"]}]
    assert res.normalize_locked == ["item1"] and res.normalize_verified is True
    assert res.leftover_artists == [] and not res.warnings
    assert len(jf.updates) == 1                       # tracks were already right: untouched, not locked


def test_normalizes_traditional_track_name_but_not_other_scripts(tmp_path, album_dir):
    c = cfg(tmp_path)
    jf = StoreJellyfin(album_artists=("周杰伦",), track_names=["愛在西元前", "爸，我回来了"])
    jf.items["item1"]["AlbumArtists"].append({"Name": "宇多田ヒカル", "Id": "a-u"})   # not a variant: kept
    res = import_album(album_dir, config=c, jellyfin=jf, poll_interval=0.01)
    assert jf.items["t1"]["Name"] == "爱在西元前" and jf.items["t1"]["LockData"] is True
    assert [a["Name"] for a in jf.items["item1"]["AlbumArtists"]] == ["周杰伦", "宇多田ヒカル"]
    assert "LockData" not in jf.items["item1"]
    assert [n["field"] for n in res.normalized] == ["Name"]


def test_nothing_to_fix_means_no_update(tmp_path, album_dir):
    jf = StoreJellyfin(album_artists=("周杰伦",))
    res = import_album(album_dir, config=cfg(tmp_path), jellyfin=jf, poll_interval=0.01)
    assert res.normalized == [] and res.normalize_verified is None and jf.updates == []
    assert "refresh" not in jf.calls                  # no extra scan when nothing was corrected


def test_relapse_after_scan_and_failed_update_are_warnings(tmp_path, album_dir):
    c = cfg(tmp_path)
    res = import_album(album_dir, config=c, jellyfin=StoreJellyfin(relapse=True), poll_interval=0.01)
    assert res.status == "imported" and res.normalize_verified is False and res.warnings
    shutil.rmtree(c.library_dir / "周杰伦")
    res = import_album(album_dir, config=c, jellyfin=StoreJellyfin(fail_update=True), poll_interval=0.01)
    assert res.status == "imported" and "HTTP 500" in res.normalize_error and res.warnings


def test_t2s_helpers():
    from bili_album.library import fix_artists, fix_name, tag_artists
    assert fix_name("範特西", "范特西") == "范特西" and fix_name("范特西", "范特西") is None
    assert fix_name("Yellow", "范特西") is None
    assert fix_artists(["周杰倫"], ["周杰伦"]) == (["周杰伦"], ["周杰倫"])
    assert fix_artists(["周杰伦", "費玉清"], tag_artists("周杰伦 & 费玉清")) == (["周杰伦", "费玉清"], ["費玉清"])
    assert fix_artists(["宇多田ヒカル"], ["宇多田光"]) is None


def test_remove_album_deletes_its_jellyfin_record(tmp_path, album_dir):
    c = cfg(tmp_path)
    jf = StoreJellyfin()
    res = import_album(album_dir, config=c, jellyfin=jf, poll_interval=0.01)
    deleted = []
    jf.delete_item = deleted.append
    removed = remove_album(res.dest, library_dir=c.library_dir, jellyfin=jf, config=c)
    assert deleted == ["item1"] and removed[-1] == "jellyfin:item1" and jf.calls[-1] == "refresh"
    assert not (c.library_dir / "周杰伦").exists()
