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
