"""Cut + tag round trip on a tiny generated AAC file (needs ffmpeg, no network)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bili_album.apply import apply_plan, output_paths, sanitize, validate, PlanError
from bili_album.media import audio_info, cut
from bili_album.tag import read_tags, write_tags

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def aac(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("aac")
    p = d / "src.m4a"
    # 3 tones separated by silence, 12 s total.
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3.5",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=0.5",
                    "-f", "lavfi", "-i", "sine=frequency=660:duration=3.5",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=0.5",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
                    "-filter_complex", "[0]aformat=sample_rates=48000:channel_layouts=stereo[a];"
                    "[2]aformat=sample_rates=48000:channel_layouts=stereo[b];"
                    "[4]aformat=sample_rates=48000:channel_layouts=stereo[c];"
                    "[a][1][b][3][c]concat=n=5:v=0:a=1",
                    "-c:a", "aac", "-b:a", "96k", str(p)], check=True)
    return p


def png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (32, 32), (200, 30, 30)).save(path, "PNG")
    return path


def test_tag_round_trip(aac, tmp_path):
    f = tmp_path / "t.m4a"
    shutil.copy(aac, f)
    cover = png(tmp_path / "c.png")
    tags = {"title": "上海一九四三", "artist": "周杰伦", "album": "范特西", "album_artist": "周杰伦",
            "year": "2001-09-14", "genre": "Pop", "composer": "周杰伦", "lyrics": "line1\nline2",
            "comment": "https://www.bilibili.com/video/BV1 0:00-3:00", "track": 6, "track_total": 10,
            "disc": 1, "disc_total": 1}
    write_tags(f, tags, cover)
    got = read_tags(f)
    for k in ("title", "artist", "album", "album_artist", "year", "genre", "composer", "lyrics", "comment"):
        assert got[k] == tags[k]
    assert (got["track"], got["track_total"], got["disc"], got["disc_total"]) == (6, 10, 1, 1)
    assert got["cover"].endswith("B")


def test_cut_is_stream_copy_and_exact(aac, tmp_path):
    a = tmp_path / "a.m4a"
    cut(aac, a, 3.7, 7.7)
    info = audio_info(a)
    assert info["codec"] == "aac"
    assert info["start_time"] >= 0
    assert info["duration"] == pytest.approx(4.0, abs=0.1)
    with pytest.raises(FileExistsError):
        cut(aac, a, 0.0, 1.0)  # never overwrite without force


def test_sanitize_keeps_cjk_and_strips_path_chars():
    assert sanitize("爸，我回来了") == "爸，我回来了"
    assert sanitize('AC/DC: "Back" <in> Black?') == "AC_DC_ _Back_ _in_ Black_"
    assert sanitize("  ..  ") == "_"
    assert sanitize("CON") == "_CON"
    assert len(sanitize("长" * 300).encode()) <= 360


def _plan(aac, tracks, kind="compilation", album="测试专辑"):
    return {
        "version": 1,
        "source": {"url": "https://www.bilibili.com/video/BVTEST", "bvid": "BVTEST", "title": "t",
                   "uploader": "", "duration": 12.0, "audio_path": str(aac),
                   "audio_format": {"codec": "aac", "bitrate_kbps": 96, "sample_rate": 48000},
                   "cover_path": "", "parts": 1, "trim_head": 0.0, "trim_tail": 0.0},
        "kind": kind, "kind_reason": "", "album": {"title": album, "artist": "Tester", "year": "2024",
                                                    "genre": "", "track_total": len(tracks), "disc_total": 1,
                                                    "cover_path": "", "catalog": {}},
        "tracks": tracks, "needs_review": False, "review_reasons": [], "timings": {},
    }


def _track(i, s, e, title):
    return {"index": i, "start": s, "end": e, "title": title, "artist": "Tester", "album": "测试专辑",
            "album_artist": "Tester", "track_number": i, "disc_number": 1, "year": "2024", "genre": "",
            "composer": "", "lyrics": "", "cover_path": "", "catalog": {}, "confidence": 1.0, "evidence": []}


def test_apply_end_to_end(aac, tmp_path):
    tracks = [_track(1, 0.0, 3.8, "One/1"), _track(2, 3.8, 7.8, "Two"), _track(3, 7.8, 12.0, "三")]
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(_plan(aac, tracks), ensure_ascii=False))
    res = apply_plan(p, tmp_path / "out", make_zip=True)
    names = [f.name for f in res.files]
    assert names == ["01-One_1.m4a", "02-Two.m4a", "03-三.m4a"]
    assert res.folder.name == "测试专辑"
    assert not res.warnings, res.warnings
    total = sum(r["actual"] for r in res.rows)
    assert total == pytest.approx(audio_info(aac)["duration"], abs=0.5)
    assert res.zip_path.exists()
    import zipfile
    with zipfile.ZipFile(res.zip_path) as z:
        assert sorted(z.namelist()) == ["测试专辑/01-One_1.m4a", "测试专辑/02-Two.m4a", "测试专辑/03-三.m4a"]
    # Re-applying without --force refuses to overwrite.
    with pytest.raises(PlanError):
        apply_plan(p, tmp_path / "out")
    res2 = apply_plan(p, tmp_path / "out", force=True)
    assert len(res2.files) == 3


def test_validate_catches_bad_plans(aac):
    bad = _plan(aac, [_track(1, 0.0, 5.0, "a"), _track(2, 4.0, 3.0, "b"), _track(3, 6.0, 20.0, "")])
    problems = "\n".join(validate(bad))
    assert "overlap" in problems
    assert "not positive" in problems
    assert "outside the source" in problems
    assert "missing title for track(s) 3" in problems


def test_single_output_path(aac, tmp_path):
    plan = _plan(aac, [_track(1, 0.0, 12.0, "Song")], kind="single")
    folder, files = output_paths(plan, tmp_path)
    assert folder is None and files[0].name == "Tester - Song.m4a"
