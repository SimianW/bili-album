"""Packet-aligned lossless cutting on small generated files (needs ffmpeg, no network)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from bili_album import packetcut
from bili_album.media import audio_info
from bili_album.packetcut import PacketCutError, audio_stream, cut_tracks, timeline

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
RATE = 48000
PACKET = 1024 / RATE


def gen(path: Path, codec: list[str]) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration=10:sample_rate={RATE}", "-ac", "2", *codec, str(path)],
                   check=True)
    return path


@pytest.fixture(scope="module")
def aac(tmp_path_factory) -> Path:
    return gen(tmp_path_factory.mktemp("pc") / "src.m4a", ["-c:a", "aac", "-b:a", "128k"])


def hashes(path: Path) -> list[str]:
    return timeline(path, audio_stream(path))[1]


def test_contiguous_tracks_share_no_packet_and_drop_none(aac, tmp_path):
    ranges = [(0.0, 3.3), (3.3, 6.71), (6.71, 10.0)]
    dsts = [tmp_path / f"{i}.m4a" for i in range(3)]
    cuts = cut_tracks(aac, ranges, dsts)
    assert [c.mode for c in cuts] == ["packets"] * 3
    # Boundaries move forward to the next packet start, never back, by less than one packet.
    for (s, _), c in zip(ranges, cuts):
        assert s <= c.start + 1e-9 < s + PACKET
    assert cuts[0].end == cuts[1].start and cuts[1].end == cuts[2].start
    # Every source packet lands in exactly one file, in order.
    assert sum((hashes(d) for d in dsts), []) == hashes(aac)
    for c, d in zip(cuts, dsts):
        info = audio_info(d)
        assert info["codec"] == "aac" and info["start_time"] >= 0
        assert info["duration"] == pytest.approx(c.end - c.start, abs=0.002)


def test_gaps_are_left_out(aac, tmp_path):
    cuts = cut_tracks(aac, [(1.0, 2.0), (4.0, 5.0)], [tmp_path / "a.m4a", tmp_path / "b.m4a"])
    assert cuts[0].end < cuts[1].start
    assert audio_info(tmp_path / "a.m4a")["duration"] == pytest.approx(1.0, abs=PACKET + 0.002)


def test_refuses_bad_ranges_and_existing_files(aac, tmp_path):
    with pytest.raises(PacketCutError, match="overlap"):
        cut_tracks(aac, [(0.0, 5.0), (4.99, 8.0)], [tmp_path / "a.m4a", tmp_path / "b.m4a"])
    with pytest.raises(PacketCutError, match="does not fit"):
        cut_tracks(aac, [(9.0, 12.0)], [tmp_path / "c.m4a"])
    with pytest.raises(PacketCutError, match="no whole audio packet"):
        cut_tracks(aac, [(5.0, 5.005)], [tmp_path / "d.m4a"])
    d = tmp_path / "e.m4a"
    d.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        cut_tracks(aac, [(0.0, 1.0)], [d])
    cut_tracks(aac, [(0.0, 1.0)], [d], overwrite=True)
    assert audio_info(d)["codec"] == "aac"
    assert not list(tmp_path.glob(".*part*")) and not list(tmp_path.glob(".packetcut-*"))


def test_failed_verification_leaves_no_file(aac, tmp_path, monkeypatch):
    def bad(*a, **k):
        raise PacketCutError("audio packets differ from the source range")
    monkeypatch.setattr(packetcut, "_verify", bad)
    d = tmp_path / "x.m4a"
    with pytest.raises(PacketCutError):
        cut_tracks(aac, [(0.0, 2.0)], [d])
    assert not d.exists() and not list(tmp_path.glob(".*part*"))


def test_other_codecs_are_encoded_as_alac(tmp_path):
    flac = gen(tmp_path / "src.flac", ["-c:a", "flac"])
    cuts = cut_tracks(flac, [(0.0, 4.0), (4.0, 10.0)], [tmp_path / "a.m4a", tmp_path / "b.m4a"])
    assert [c.mode for c in cuts] == ["encode", "encode"]
    assert audio_info(tmp_path / "a.m4a")["codec"] == "alac"


# ---------------------------------------------------------------- review regressions

def test_percent_in_the_folder_name(aac, tmp_path):
    for name in ("100% Love", "a%d b"):
        d = tmp_path / name
        cuts = cut_tracks(aac, [(0.0, 5.0), (5.0, 10.0)], [d / "1.m4a", d / "2.m4a"])
        assert [c.mode for c in cuts] == ["packets"] * 2 and (d / "2.m4a").exists()


def test_end_slightly_past_the_audio_is_the_end(aac, tmp_path):
    # apply.validate accepts an end up to 0.05 s past the source; the cut must accept it too.
    [c] = cut_tracks(aac, [(8.0, 10.03)], [tmp_path / "x.m4a"])
    assert c.end == pytest.approx(10.0, abs=PACKET)
    with pytest.raises(PacketCutError, match="does not fit"):
        cut_tracks(aac, [(8.0, 10.2)], [tmp_path / "y.m4a"])


def test_boundary_never_moves_before_the_planned_time():
    from fractions import Fraction
    from bili_album.packetcut import packet_ranges
    pos = [Fraction(1024 * i) for i in range(10)]
    # 1024.1 samples rounds to 1024, but the packet starting at 1024 lies before the plan.
    [(first, _)] = packet_ranges([(1024.1 / 44100, 9000 / 44100)], 44100, 10240, pos)
    assert first == 2


def test_overlap_within_a_millisecond_is_one_boundary():
    from fractions import Fraction
    from bili_album.packetcut import packet_ranges
    pos = [Fraction(480 * i) for i in range(200)]   # 10 ms packets at 48 kHz
    got = packet_ranges([(0.0, 1.0006), (1.0, 1.5)], 48000, 96000, pos)
    assert got[0][1] == got[1][0]


def test_failure_on_a_later_track_leaves_no_file_of_that_source(aac, tmp_path, monkeypatch):
    real, calls = packetcut._verify, []

    def second_fails(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise PacketCutError("audio packets differ from the source range")
        return real(*a, **k)
    monkeypatch.setattr(packetcut, "_verify", second_fails)
    dsts = [tmp_path / "1.m4a", tmp_path / "2.m4a", tmp_path / "3.m4a"]
    with pytest.raises(PacketCutError):
        cut_tracks(aac, [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)], dsts)
    assert not any(d.exists() for d in dsts) and not list(tmp_path.glob(".*"))


def test_apply_removes_earlier_sources_and_reports_a_plan_error(aac, tmp_path, monkeypatch):
    import json
    from bili_album.apply import PlanError, apply_plan

    part2 = tmp_path / "p2.m4a"
    shutil.copy(aac, part2)
    tracks = [{"index": i, "start": 0.0, "end": 10.0, "title": t, "artist": "x", "album": "al",
               "album_artist": "x", "audio_path": str(src)} for i, t, src in ((1, "a", aac), (2, "b", part2))]
    plan = {"version": 1, "kind": "multipart", "tracks": tracks,
            "source": {"url": "", "title": "t", "audio_path": str(aac), "parts": 2},
            "album": {"title": "al", "artist": "x", "cover_path": ""}}
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan, ensure_ascii=False))
    real = packetcut.cut_tracks

    def fail_second_source(src, *a, **k):
        if Path(src) == part2:
            raise PacketCutError("audio packets differ from the source range")
        return real(src, *a, **k)
    monkeypatch.setattr("bili_album.apply.cut_tracks", fail_second_source)
    with pytest.raises(PlanError, match="cut failed"):
        apply_plan(p, tmp_path / "out")
    assert not list((tmp_path / "out").rglob("*.m4a"))


def _apply_plan_file(tmp_path, sources_and_ranges, kind="multipart"):
    import json
    tracks = [{"index": i, "start": s, "end": e, "title": f"t{i}", "artist": "x", "album": "al",
               "album_artist": "x", "audio_path": str(src)}
              for i, (src, s, e) in enumerate(sources_and_ranges, 1)]
    plan = {"version": 1, "kind": kind, "tracks": tracks,
            "source": {"url": "", "title": "t", "audio_path": str(sources_and_ranges[0][0]),
                       "parts": len(tracks)},
            "album": {"title": "al", "artist": "x", "cover_path": ""}}
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return p


def test_failed_force_run_keeps_the_old_files(aac, tmp_path, monkeypatch):
    from bili_album.apply import PlanError, apply_plan
    part2 = tmp_path / "p2.m4a"
    shutil.copy(aac, part2)
    p = _apply_plan_file(tmp_path, [(aac, 0.0, 10.0), (part2, 0.0, 10.0)])
    res = apply_plan(p, tmp_path / "out")
    before = [f.read_bytes() for f in res.files]
    real = packetcut.cut_tracks

    def fail_second_source(src, *a, **k):
        if Path(src) == part2:
            raise PacketCutError("audio packets differ from the source range")
        return real(src, *a, **k)
    monkeypatch.setattr("bili_album.apply.cut_tracks", fail_second_source)
    with pytest.raises(PlanError):
        apply_plan(p, tmp_path / "out", force=True)
    assert [f.read_bytes() for f in res.files] == before
    assert not list((tmp_path / "out").rglob(".*"))


def test_encode_path_failure_leaves_no_file(tmp_path, monkeypatch):
    flac = gen(tmp_path / "src.flac", ["-c:a", "flac"])
    real, calls = packetcut.cut, []

    def second_fails(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, "ffmpeg")
        return real(*a, **k)
    monkeypatch.setattr(packetcut, "cut", second_fails)
    dsts = [tmp_path / "a.m4a", tmp_path / "b.m4a"]
    with pytest.raises(PacketCutError):
        cut_tracks(flac, [(0.0, 4.0), (4.0, 10.0)], dsts)
    assert not any(d.exists() for d in dsts)


def test_encode_failure_after_the_part_file_is_written(tmp_path, monkeypatch):
    # ffmpeg dies after creating media.cut's hidden part file: no file is left, and apply reports a
    # cut failure (not an unexpected error).
    from bili_album import media
    from bili_album.apply import CutError, apply_plan
    from bili_album.messages import explain
    flac = gen(tmp_path / "src.flac", ["-c:a", "flac"])

    real = subprocess.run

    def dies(args, *a, **k):
        if args[0] != "ffmpeg":
            return real(args, *a, **k)
        Path(args[-1]).write_bytes(b"half")
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(media.subprocess, "run", dies)
    p = _apply_plan_file(tmp_path, [(flac, 0.0, 4.0), (flac, 4.0, 10.0)], kind="compilation")
    with pytest.raises(CutError) as e:
        apply_plan(p, tmp_path / "out")
    assert explain(e.value, "cut").code == "cut_failed"
    assert not [x for x in (tmp_path / "out").rglob("*") if x.is_file()]


def test_cut_failures_and_range_problems_read_differently():
    from bili_album.apply import CutError, PlanError
    from bili_album.messages import explain
    assert explain(CutError(["cut failed: audio packets differ from the source range"]), "cut").code == "cut_failed"
    assert explain(PlanError(["range 9.000-12.000s does not fit the audio (10.000s)"]), "cut").code == "plan_problems"


def test_long_alac_packets_do_not_trip_the_duration_check(tmp_path):
    from bili_album.apply import apply_plan
    alac = tmp_path / "src.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=10:sample_rate=22050", "-ac", "2", "-c:a", "alac", str(alac)],
                   check=True)
    # 4096-sample packets are 186 ms at 22.05 kHz: each boundary can move by most of one.
    p = _apply_plan_file(tmp_path, [(alac, 0.0, 3.01), (alac, 3.01, 10.0)], kind="compilation")
    res = apply_plan(p, tmp_path / "out")
    assert [r["cut"] for r in res.rows] == ["packets", "packets"]
    assert not res.warnings, res.warnings
