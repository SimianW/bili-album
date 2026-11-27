"""Several artists in one field: splitting, the "/" join, multi-value tags, and the library folder."""

import shutil
import subprocess
from pathlib import Path

import pytest

from bili_album.artists import first_artist, join_artists, split_artists


@pytest.mark.parametrize("value, names", [
    ("周杰伦/费玉清", ["周杰伦", "费玉清"]),
    ("周杰伦 / 费玉清", ["周杰伦", "费玉清"]),
    ("周杰伦／费玉清", ["周杰伦", "费玉清"]),
    ("周杰伦、费玉清", ["周杰伦", "费玉清"]),
    ("周杰伦 & 费玉清", ["周杰伦", "费玉清"]),          # storefront join, every part CJK
    ("周杰伦，费玉清", ["周杰伦", "费玉清"]),
    ("a|b;c\\d", ["a", "b", "c", "d"]),                # Jellyfin's default delimiters
    ("Lady Gaga feat. Bruno Mars", ["Lady Gaga", "Bruno Mars"]),
    ("周杰伦 (feat. 杨瑞代)", ["周杰伦", "杨瑞代"]),
    ("Simon & Garfunkel", ["Simon & Garfunkel"]),      # "&" and "," inside Latin names stay
    ("Earth, Wind & Fire", ["Earth, Wind & Fire"]),
    ("周杰伦 & Lara", ["周杰伦 & Lara"]),
    ("AC/DC", ["AC/DC"]),                               # a name that holds a delimiter
    ("Soft Cell", ["Soft Cell"]),
    ("周杰伦/周杰伦", ["周杰伦"]),
    ("", []),
])
def test_split_artists(value, names):
    assert split_artists(value) == names


def test_join_and_first():
    assert join_artists(["周杰伦 & 费玉清", "费玉清", "林俊杰"]) == "周杰伦/费玉清/林俊杰"
    assert first_artist("周杰伦 & 费玉清") == "周杰伦" and first_artist("AC/DC") == "AC/DC"
    assert first_artist("") == ""


@pytest.mark.parametrize("title, artists", [
    ("周杰伦/费玉清《千里之外》", ["周杰伦/费玉清", "周杰伦", "费玉清"]),
    ("林俊杰 / 金莎《被风吹过的夏天》", ["林俊杰/金莎", "林俊杰", "金莎"]),
    ("周杰伦 & 费玉清《千里之外》", ["周杰伦/费玉清", "周杰伦", "费玉清"]),
    ("【无损】周杰伦、费玉清 - 千里之外", ["周杰伦/费玉清", "周杰伦", "费玉清"]),
    ("HiRes无损 / 周杰伦《范特西》", ["周杰伦"]),
    ("Simon & Garfunkel《Bookends》", ["Simon & Garfunkel"]),
])
def test_title_guess_keeps_every_artist(title, artists):
    from bili_album.hints import guess_from_title
    g = guess_from_title(title)
    assert g.artists == artists
    assert g.queries()[0].startswith(artists[0])


def test_netease_lists_are_joined_with_slash():
    from bili_album.catalog import NetEase
    al = NetEase._summary({"id": 1, "name": "x", "artists": [{"name": "周杰伦"}, {"name": "费玉清"}]})
    assert al.artist == "周杰伦/费玉清"


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@needs_ffmpeg
def test_tags_hold_one_value_per_artist(tmp_path):
    from mutagen.mp4 import MP4
    from bili_album.tag import read_tags, write_tags

    f = tmp_path / "t.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=duration=1",
                    "-c:a", "aac", str(f)], check=True)
    write_tags(f, {"title": "千里之外", "artist": "周杰伦 & 费玉清", "album": "依然范特西",
                   "album_artist": "周杰伦", "track": 1})
    t = MP4(str(f)).tags
    assert t["\xa9ART"] == ["周杰伦", "费玉清"]
    assert [bytes(x).decode() for x in t["----:com.apple.iTunes:ARTISTS"]] == ["周杰伦", "费玉清"]
    assert t["aART"] == ["周杰伦"]
    got = read_tags(f)
    assert got["artist"] == "周杰伦/费玉清" and got["album_artist"] == "周杰伦"
    # Writing a single name again drops the second value and keeps the freeform tag in step.
    write_tags(f, {"title": "x", "artist": "周杰伦", "album": "y", "album_artist": "周杰伦", "track": 1})
    t = MP4(str(f)).tags
    assert t["\xa9ART"] == ["周杰伦"] and len(t["----:com.apple.iTunes:ARTISTS"]) == 1


def test_library_files_an_album_under_its_first_artist(tmp_path):
    from bili_album.config import Config
    from bili_album.library import destination
    cfg = Config(library_dir=tmp_path, jellyfin_library_path="/media/music")
    dest, cpath = destination(cfg, "周杰伦/费玉清", "千里之外")
    assert dest == tmp_path / "周杰伦" / "千里之外" and cpath == "/media/music/周杰伦/千里之外"
    assert destination(cfg, "AC/DC", "Back in Black")[0] == tmp_path / "AC_DC" / "Back in Black"


def test_single_file_name_lists_every_artist(tmp_path):
    from bili_album.apply import output_paths
    plan = {"kind": "single", "album": {"title": ""}, "tracks": [{"title": "千里之外", "artist": "周杰伦/费玉清"}]}
    _, [f] = output_paths(plan, tmp_path)
    assert f.name == "周杰伦、费玉清 - 千里之外.m4a"
