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
    # review round 1
    ("IU (아이유)", ["IU (아이유)"]),                    # brackets that belong to the name stay
    ("邓紫棋 (G.E.M.)", ["邓紫棋 (G.E.M.)"]),
    ("(G)I-DLE", ["(G)I-DLE"]),
    ("A＼B", ["A", "B"]),                               # full-width backslash
    ("K/DA", ["K/DA"]),
    ("AC/DC Tribute", ["AC/DC Tribute"]),
    ("A featuring B", ["A", "B"]),
    ("周杰伦 [feat. 杨瑞代]", ["周杰伦", "杨瑞代"]),
    # review round 2
    ("Jack/David", ["Jack", "David"]),                  # "k/Da" inside other names is not K/DA
    ("Frank/Daniel", ["Frank", "Daniel"]),
    ("ac/dc", ["ac/dc"]),                               # protected names keep their spelling
    ("周杰伦feat.杨瑞代", ["周杰伦", "杨瑞代"]),
    ("(周杰伦/费玉清)", ["周杰伦", "费玉清"]),
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
    ("国语/周杰伦《范特西》", ["周杰伦"]),                 # a label before "/" is not an artist
    ("华语经典/周杰伦《范特西》", ["周杰伦"]),
    ("Jay Chou/周杰伦《晴天》", ["周杰伦"]),               # an alias in another script: one artist
    ("民谣/赵雷《成都》", ["赵雷"]),                       # genre and role words are labels
    ("流行/周杰伦《范特西》", ["周杰伦"]),
])
def test_title_guess_keeps_every_artist(title, artists):
    from bili_album.hints import guess_from_title
    g = guess_from_title(title)
    assert g.artists == artists
    assert g.queries()[0].startswith(artists[0])


def test_split_names_are_not_spent_on_extra_searches():
    from bili_album.hints import guess_from_title
    g = guess_from_title("周杰伦/费玉清《千里之外》")
    assert "周杰伦" not in g.extra and "费玉清" not in g.extra


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


def test_import_result_names_the_real_folder():
    from types import SimpleNamespace
    from bili_album.web.runner import map_result
    res = SimpleNamespace(status="imported", message="", error="",
                          import_result={"album_artist": "周杰伦/费玉清", "album": "千里之外",
                                         "dest": "/music/周杰伦/千里之外", "files": ["a"], "status": "imported"})
    assert map_result(res).result["folder"] == "周杰伦 / 千里之外"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_preview_uses_the_same_first_artist_rule():
    import json
    import re
    html = (Path(__file__).parents[1] / "src/bili_album/web/static/index.html").read_text(encoding="utf-8")
    fn = re.search(r"safeFolder\((\(s => \{.*?\|\| s; \}\))\(artist\)\)", html).group(1)
    cases = ["周杰伦/费玉清", "周杰伦 & 费玉清", "周杰伦、费玉清", "周杰伦，费玉清", "Simon & Garfunkel", "AC/DC",
             "AC/DC Tribute", "K/DA", "A featuring B", "Lady Gaga feat. Bruno Mars", "周杰伦 [feat. 杨瑞代]",
             "周杰伦【feat. 杨瑞代】", "A＼B", "a|b", "IU (아이유)", "周杰伦 & Lara", "周杰伦",
             "Jack/David", "ac/dc", "周杰伦feat.杨瑞代", "(周杰伦/费玉清)"]
    out = subprocess.run(["node", "-e", f"const f = {fn}; console.log(JSON.stringify({json.dumps(cases)}.map(f)))"],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [first_artist(c) for c in cases]
