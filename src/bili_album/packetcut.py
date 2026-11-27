"""Lossless cutting of several tracks from one source by whole audio packets, verified packet by packet.

``media.cut`` cuts each track on its own with ``-ss``/``-t`` and relies on an MP4 edit list to
hide the samples before the cut inside the first packet; two neighbouring files can then hold
the same packet. Here one source is split in a single ffmpeg ``segment`` pass at packet
boundaries, so consecutive files never share or drop a packet, and every exported file is
checked against the source: same codec, channels and sample rate, the planned length within
1 ms, and the SHA-256 of every packet equal to the source packets it came from.

A cut moves to the first packet that starts at or after the planned time (an AAC-LC packet is
1024 samples: 21-23 ms at 48 / 44.1 kHz; an ALAC packet 4096: about 93 ms at 44.1 kHz). An end
up to ``END_SLACK`` past the audio (the tolerance ``apply.validate`` allows) is the end of the
audio, and an overlap of at most ``OVERLAP`` (also allowed there) is a shared boundary. Sources that are not AAC or ALAC are left to
``media.cut``, which encodes them as ALAC. Entry point: :func:`cut_tracks`.
"""

from __future__ import annotations

import bisect
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .media import COPY_CODECS, cut


END_SLACK = 0.05   # seconds an end may run past the audio (apply.validate allows this much)
OVERLAP = 0.001    # seconds two ranges may overlap and still share one boundary (apply.validate)


class PacketCutError(ValueError):
    """The source or ffmpeg could not give a verified copy (not something a plan edit fixes)."""


class RangeError(PacketCutError):
    """A planned range does not fit the source's packets (overlap, past the end, too short)."""


@dataclass(frozen=True)
class Cut:
    """One exported file: where it really starts and ends on the source, and how it was made."""
    start: float
    end: float
    mode: str          # "packets" (stream copy at packet boundaries) or "encode" (media.cut)


def _ffprobe(path: Path, *options: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", *options, "-of", "json", str(path)],
                       capture_output=True, text=True, check=False)
    if r.returncode or r.stderr.strip():
        raise PacketCutError(f"ffprobe {path.name}: {r.stderr.strip() or 'probe failed'}")
    return json.loads(r.stdout)


def _ffmpeg(*args) -> None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y", *map(str, args)],
                       capture_output=True, text=True, check=False)
    if r.returncode or r.stderr.strip():
        raise PacketCutError(f"ffmpeg: {r.stderr.strip() or 'export failed'}")


def audio_stream(path: Path) -> dict:
    """The first audio stream: index, codec_name, sample_rate, channels, time_base."""
    j = _ffprobe(path, "-select_streams", "a:0",
                 "-show_entries", "stream=index,codec_name,sample_rate,channels,time_base")
    streams = j.get("streams") or []
    if not streams or int(streams[0].get("sample_rate") or 0) <= 0:
        raise PacketCutError(f"{path.name}: no audio stream with a sample rate")
    return streams[0]


def timeline(path: Path, stream: dict) -> tuple[int, list[str], list[Fraction]]:
    """(decoded sample count, packet hashes, packet start positions in samples from the first frame)."""
    j = _ffprobe(path, "-select_streams", str(stream["index"]), "-show_data_hash", "sha256",
                 "-show_entries", "frame=nb_samples,best_effort_timestamp:packet=pts,data_hash")
    items = j.get("packets_and_frames") or []
    frames = [i for i in items if i.get("type") == "frame"]
    packets = [i for i in items if i.get("type") == "packet"]
    if not frames or not packets:
        raise PacketCutError(f"{path.name}: the audio stream has no packets to copy")
    origin = frames[0].get("best_effort_timestamp")
    if origin is None or any("pts" not in p or "data_hash" not in p for p in packets):
        raise PacketCutError(f"{path.name}: audio packets lack a usable copy timeline")
    per_tick = Fraction(stream["time_base"]) * int(stream["sample_rate"])
    decoded = 0
    for f in frames:
        pts = f.get("best_effort_timestamp")
        if pts is None or abs((pts - origin) * per_tick - decoded) > max(1, per_tick):
            raise PacketCutError(f"{path.name}: packet timestamps diverge from the decoded samples")
        decoded += int(f["nb_samples"])
    positions = [(p["pts"] - origin) * per_tick for p in packets]
    if any(a >= b for a, b in zip(positions, positions[1:])):
        raise PacketCutError(f"{path.name}: packet timestamps are not strictly increasing")
    return decoded, [p["data_hash"] for p in packets], positions


def packet_ranges(ranges: list[tuple[float, float]], rate: int, samples: int,
                  positions: list[Fraction]) -> list[tuple[int, int]]:
    """Packet index ranges [first, last) for second ranges: each side goes to the first packet
    starting at or after its exact (unrounded) sample position, so both sides of a shared
    boundary get the same packet index. See the module text for END_SLACK and OVERLAP."""
    out = []
    slack = Fraction(END_SLACK) * rate + (rate + 999) // 1000
    for start, end in ranges:
        s, e = Fraction(start) * rate, Fraction(end) * rate
        if samples < e <= samples + slack:
            e = Fraction(samples)
        if not 0 <= s < e <= samples:
            raise RangeError(f"range {start:.3f}-{end:.3f}s does not fit the audio ({samples / rate:.3f}s)")
        first = 0 if s == 0 else bisect.bisect_left(positions, s)
        last = len(positions) if e == samples else bisect.bisect_left(positions, e)
        if first >= last:
            raise RangeError(f"range {start:.3f}-{end:.3f}s holds no whole audio packet")
        out.append((first, last))
    for i in range(len(out) - 1):
        (a0, a1), (b0, b1) = out[i], out[i + 1]
        if b0 < a1:
            if ranges[i][1] - ranges[i + 1][0] > OVERLAP or b0 <= a0:
                raise RangeError("ranges overlap once moved to packet boundaries")
            out[i] = (a0, b0)
    return out


def _verify(path: Path, stream: dict, samples: Fraction, hashes: list[str]) -> None:
    j = _ffprobe(path, "-show_data_hash", "sha256", "-show_entries",
                 "stream=codec_type,codec_name,sample_rate,channels,time_base,duration_ts:packet=data_hash")
    outs = j.get("streams") or []
    if len(outs) != 1 or outs[0].get("codec_type") != "audio" or outs[0].get("codec_name") != stream["codec_name"]:
        raise PacketCutError(f"{path.name}: expected one {stream['codec_name']} audio stream")
    a, rate = outs[0], int(stream["sample_rate"])
    if a.get("channels") != stream["channels"] or int(a["sample_rate"]) != rate:
        raise PacketCutError(f"{path.name}: channel count or sample rate changed")
    actual = Fraction(a.get("duration_ts", 0)) * Fraction(a["time_base"]) * rate
    if actual <= 0 or abs(actual - samples) > Fraction(rate, 1000):
        raise PacketCutError(f"{path.name}: audio length differs from the packet range")
    if [p.get("data_hash") for p in j.get("packets") or []] != hashes:
        raise PacketCutError(f"{path.name}: audio packets differ from the source range")


def cut_tracks(src: Path, ranges: list[tuple[float, float]], dsts: list[Path], *,
               overwrite: bool = False) -> list[Cut]:
    """Cut ``ranges`` (seconds, increasing, non-overlapping) of ``src`` into ``dsts``.

    AAC / ALAC: one segment pass at packet boundaries, each file verified packet by packet,
    written to a hidden ``.part`` file and moved into place. Other codecs: ``media.cut``."""
    if len(ranges) != len(dsts) or not ranges:
        raise ValueError("one destination per range is needed")
    for d in dsts:
        if d.exists() and not overwrite:
            raise FileExistsError(d)
    stream = audio_stream(src)
    if stream.get("codec_name") not in COPY_CODECS:
        written: list[Path] = []
        try:
            for (s, e), d in zip(ranges, dsts):
                cut(src, d, s, e, overwrite=overwrite)
                written.append(d)
        except BaseException:
            for d in written:
                d.unlink(missing_ok=True)
            raise
        return [Cut(s, e, "encode") for s, e in ranges]
    rate = int(stream["sample_rate"])
    samples, hashes, positions = timeline(src, stream)
    idx = packet_ranges(ranges, rate, samples, positions)
    bounds = sorted({0, len(positions), *(i for pair in idx for i in pair)})
    seg_of = {b: n for n, b in enumerate(bounds)}
    dsts[0].parent.mkdir(parents=True, exist_ok=True)
    out: list[Cut] = []
    with tempfile.TemporaryDirectory(prefix=".packetcut-", dir=dsts[0].parent) as tmp:
        work = Path(tmp)
        # The segment muxer reads the whole output path as a template: a "%" in a folder name
        # (an album called "100% Love") must be doubled.
        pattern = str(work).replace("%", "%%") + "/segment-%d.m4a"
        _ffmpeg("-i", src, "-map", f"0:{stream['index']}", "-map_metadata", "-1", "-map_chapters", "-1",
                "-c:a", "copy", "-f", "segment", "-segment_format", "ipod",
                "-segment_format_options", f"movie_timescale={rate}", "-reset_timestamps", "1",
                "-break_non_keyframes", "1", "-segment_frames", ",".join(map(str, bounds[1:])),
                pattern)
        if len(list(work.glob("segment-*.m4a"))) != len(bounds) - 1:
            raise PacketCutError("ffmpeg made an unexpected number of packet segments")
        # Every file is written and verified as a hidden .part first; only when all of them pass
        # are they moved into place, so a failure leaves none of this source's files behind.
        parts: list[Path] = []
        try:
            for (first, last), d in zip(idx, dsts):
                seg = work / f"segment-{seg_of[first]}.m4a"
                opts = ["-map_metadata", "-1", "-map_chapters", "-1", "-c:a", "copy",
                        "-movie_timescale", str(rate), "-movflags", "+faststart"]
                priming = max(0, -round(positions[0])) if first == 0 else 0
                if priming:
                    # Resetting segment timestamps makes AAC priming audible unless the edit is restored.
                    opts += ["-bsf:a", f"setts=pts=PTS-{priming}:dts=DTS-{priming}", "-use_editlist", "1"]
                part = d.with_name("." + d.stem + ".part.m4a")
                part.unlink(missing_ok=True)
                parts.append(part)
                _ffmpeg("-i", seg, "-map", "0:a:0", *opts, part)
                start = Fraction(0) if first == 0 else positions[first]
                end = Fraction(samples) if last == len(positions) else positions[last]
                _verify(part, stream, end - start, hashes[first:last])
                out.append(Cut(float(start / rate), float(end / rate), "packets"))
        except BaseException:
            for part in parts:
                part.unlink(missing_ok=True)
            raise
        for part, d in zip(parts, dsts):
            if d.exists() and overwrite:
                d.unlink()
            shutil.move(part, d)
    return out
