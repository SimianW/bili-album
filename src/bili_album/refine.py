"""Millisecond refinement of track starts that are already roughly right.

The planner places boundaries on a 10 Hz feature track (:mod:`analyze`) and snaps or aligns
tracks to them. This module looks again at a small window around each placed start, on the
full-rate signal (16 kHz, 32 ms frames every 5 ms), and moves the cut to the precise edge:

1. a quiet run (first at ``silence_db``, then at ``deep_silence_db``) whose onset lies in the
   window, nearest to the guess; two runs whose onsets are within ``tie_ms`` of the same
   distance count as ambiguous and are skipped;
2. from that onset, back to the last frame still near the run's noise floor (its
   ``floor_percentile`` level + ``floor_margin_db``), minus ``pre_roll_ms``, never leaving the
   run or the window, so a faint fade-in intro and the next track's attack stay with it;
3. otherwise the strongest spectral change in the window, if it clearly beats its rivals;
4. otherwise the guess is kept (``fallback``).

The audio is decoded once, in order, and only the PCM the next window still needs is kept.
Every tuning value is a keyword argument of :func:`refine_starts` with its default here.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLES_PER_MS = 16
SAMPLE_RATE = 1000 * SAMPLES_PER_MS
PCM_DTYPE = np.dtype("<f4")
METHODS = ("silence", "spectral", "fallback")


@dataclass(frozen=True)
class RefineParams:
    search_ms: int = 1500             # window radius around each guess
    silence_db: float = -42.0         # first-pass quiet threshold (dBFS)
    deep_silence_db: float = -65.0    # second-pass threshold, <= silence_db
    min_silence_ms: int = 450         # shortest quiet run
    recovery_ms: int = 25             # the level must stay above the threshold this long after the onset
    tie_ms: int = 100                 # two runs this close in distance to the guess are ambiguous
    floor_percentile: float = 20.0    # noise floor of a run: this percentile of its frame levels
    floor_margin_db: float = 10.0     # frames within floor + this still count as the gap; 0 disables backtracking
    pre_roll_ms: int = 50             # extra lead before the backtracked cut (never leaves the gap)
    spectral_context_ms: int = 400    # audio compared on each side of a spectral candidate
    spectral_min_score: float = 0.22  # lowest accepted spectral difference (0-1)
    spectral_min_margin: float = 0.04  # the best point must beat rivals farther than spectral_rival_ms by this
    spectral_rival_ms: int = 200
    spectral_tie_score: float = 0.01  # scores this close count as equal; the one nearest the guess wins
    hop_ms: int = 5
    frame_ms: int = 32

    def validate(self) -> None:
        if self.search_ms < 0 or self.min_silence_ms <= 0 or self.recovery_ms < 0 or self.tie_ms < 0:
            raise ValueError("search_ms, recovery_ms and tie_ms must be nonnegative; min_silence_ms positive")
        if not math.isfinite(self.deep_silence_db) or not self.deep_silence_db <= self.silence_db <= 0:
            raise ValueError("silence thresholds must satisfy deep_silence_db <= silence_db <= 0")
        if self.hop_ms <= 0 or self.frame_ms < self.hop_ms or self.spectral_context_ms < self.hop_ms:
            raise ValueError("hop_ms must be positive and no larger than frame_ms and spectral_context_ms")
        if not 0 <= self.floor_percentile <= 100 or self.floor_margin_db < 0 or self.pre_roll_ms < 0:
            raise ValueError("floor_percentile must be 0-100; floor_margin_db and pre_roll_ms nonnegative")

    def hops(self, ms: int) -> int:
        return max(1, round(ms / self.hop_ms))


@dataclass
class Window:
    index: int          # position in the caller's list
    guess_ms: int
    lower_ms: int
    upper_ms: int
    clip_start_ms: int
    clip_end_ms: int
    pcm: bytearray = field(default_factory=bytearray)


@dataclass(frozen=True)
class Gap:
    """A quiet run [first, end) in frame indices; ``end`` is the first frame back above the threshold."""

    first: int
    end: int


def refine_starts(audio: Path | str, guesses: list[float], *, channels: int = 2,
                  bounds: list[tuple[float | None, float | None]] | None = None,
                  search_ms: int = 1500, silence_db: float = -42.0, deep_silence_db: float = -65.0,
                  min_silence_ms: int = 450, recovery_ms: int = 25, tie_ms: int = 100,
                  floor_percentile: float = 20.0, floor_margin_db: float = 10.0, pre_roll_ms: int = 50,
                  spectral_context_ms: int = 400, spectral_min_score: float = 0.22,
                  spectral_min_margin: float = 0.04, spectral_rival_ms: int = 200,
                  spectral_tie_score: float = 0.01, hop_ms: int = 5, frame_ms: int = 32
                  ) -> list[tuple[float, str]]:
    """Refine increasing start guesses (seconds) on ``audio``; return ``[(start_s, method)]``.

    Each window is ``guess ± search_ms``, kept between the midpoints to the neighbouring guesses
    and inside the caller's ``bounds[i] = (lo_s, hi_s)`` (either may be None). ``channels`` is the
    channel count of the first audio stream (the decoder keeps it). A guess whose window is empty
    comes back unchanged as ``fallback``."""
    p = RefineParams(search_ms, silence_db, deep_silence_db, min_silence_ms, recovery_ms, tie_ms,
                     floor_percentile, floor_margin_db, pre_roll_ms, spectral_context_ms, spectral_min_score,
                     spectral_min_margin, spectral_rival_ms, spectral_tie_score, hop_ms, frame_ms)
    p.validate()
    if not guesses:
        return []
    ms = [round(g * 1000) for g in guesses]
    if any(b <= a for a, b in zip(ms, ms[1:])):
        raise ValueError("start guesses must increase")
    context = max(p.spectral_context_ms, p.min_silence_ms) + 2 * p.frame_ms
    windows: list[Window] = []
    for i, g in enumerate(ms):
        lo, hi = g - p.search_ms, g + p.search_ms
        if i > 0:
            lo = max(lo, (ms[i - 1] + g) // 2 + 1)
        if i + 1 < len(ms):
            hi = min(hi, (g + ms[i + 1]) // 2)
        if bounds and bounds[i][0] is not None:
            lo = max(lo, math.ceil(bounds[i][0] * 1000))
        if bounds and bounds[i][1] is not None:
            hi = min(hi, math.floor(bounds[i][1] * 1000))
        lo = max(lo, 0)
        windows.append(Window(i, g, lo, hi, max(0, lo - context), hi + context))
    out: list[tuple[float, str] | None] = [None] * len(ms)
    for w, start, method in _decode_and_refine(Path(audio), windows, channels, p):
        out[w.index] = (start / 1000.0, method)
    return [o if o is not None else (g / 1000.0, "fallback") for o, g in zip(out, ms)]


def _decode_and_refine(audio: Path, windows: list[Window], channels: int, p: RefineParams):
    """Decode once (16 kHz float, all channels) and yield (window, start_ms, method) in order."""
    if channels <= 0:
        raise ValueError("channels must be positive")
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(audio), "-map", "0:a:0",
               "-vn", "-sn", "-dn", "-ar", str(SAMPLE_RATE), "-ac", str(channels), "-f", "f32le", "pipe:1"]
    bytes_per_sample = PCM_DTYPE.itemsize * channels
    bytes_per_ms = bytes_per_sample * SAMPLES_PER_MS
    buffer, buffer_start, offset = bytearray(), 0, 0
    pending = list(windows)

    def take(w: Window) -> Window:
        first = w.clip_start_ms * bytes_per_ms
        w.pcm = bytearray(buffer[first - buffer_start:min(w.clip_end_ms * bytes_per_ms, offset) - buffer_start])
        return w

    def finish(w: Window):
        if w.lower_ms > w.upper_ms:
            return w, w.guess_ms, "fallback"
        start, method = refine_start(take(w), channels, p)
        w.pcm = bytearray()
        return w, start, method

    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors) as decoder:
            assert decoder.stdout is not None
            while chunk := decoder.stdout.read(16384 * bytes_per_sample):
                buffer.extend(chunk)
                offset += len(chunk)
                while pending and pending[0].clip_end_ms * bytes_per_ms <= offset:
                    yield finish(pending.pop(0))
                keep = offset if not pending else min(offset, pending[0].clip_start_ms * bytes_per_ms)
                if keep > buffer_start:
                    del buffer[:keep - buffer_start]
                    buffer_start = keep
            if decoder.wait():
                errors.seek(0)
                message = errors.read().decode("utf-8", errors="replace").strip()
                raise ValueError(f"ffmpeg: {message or 'decoding failed'}")
    end = (offset + bytes_per_ms // 2) // bytes_per_ms
    for w in pending:
        w.upper_ms = min(w.upper_ms, end - 1)
        yield finish(w)


def silence_gap(db, times, eligible, threshold: float, p: RefineParams, guess: int) -> Gap | None:
    """The unambiguous quiet run whose onset (end) lies in the window, nearest to the guess."""
    changes = np.flatnonzero(np.diff(db < threshold, prepend=False, append=False))
    found = []
    recovery = p.hops(p.recovery_ms)
    for first, end in zip(changes[::2], changes[1::2]):
        if (end - first) * p.hop_ms < p.min_silence_ms or end + recovery > len(db):
            continue
        if eligible[end] and np.all(db[end:end + recovery] >= threshold):
            found.append(Gap(int(first), int(end)))
    found.sort(key=lambda gap: abs(times[gap.end] - guess))
    if len(found) > 1:
        nearest, runner_up = (abs(times[gap.end] - guess) for gap in found[:2])
        if runner_up - nearest < p.tie_ms:
            return None
    return found[0] if found else None


def backtrack(gap: Gap, db, times, lower_ms: int, p: RefineParams) -> int:
    """Move the cut from the onset back to where the level last sat near the gap's noise floor.

    Keeps faint fade-in intros and the attack in the next track; the cut never leaves the gap
    or the search window."""
    onset = int(times[gap.end])
    if p.floor_margin_db <= 0:
        return onset
    quiet = db[gap.first:gap.end]
    floor = float(np.percentile(quiet, p.floor_percentile))
    near_floor = np.flatnonzero(quiet <= floor + p.floor_margin_db)
    last_quiet = gap.first + int(near_floor[-1]) if near_floor.size else gap.end - 1
    # Frame i covers times[i] ± frame_ms/2 and is still near the floor, so audio up to its end is quiet.
    cut = int(times[last_quiet]) + p.frame_ms // 2 - p.pre_roll_ms
    earliest = max(int(times[gap.first]), lower_ms)
    return min(onset, max(cut, earliest))


def spectral_boundary(frames, db, times, eligible, guess: int, p: RefineParams) -> int | None:
    context = p.hops(p.spectral_context_ms)
    margin = math.ceil(p.frame_ms / (2 * p.hop_ms))
    indices = np.flatnonzero(eligible)
    indices = indices[(indices >= context + margin - 1) & (indices + margin + context <= len(frames))]
    if not indices.size:
        return None
    taper = np.hanning(frames.shape[-1] + 1)[:-1]
    power = np.mean(np.abs(np.fft.rfft(frames * taper, axis=-1)) ** 2, axis=1)
    totals = power.sum(axis=1, keepdims=True)
    power /= np.maximum(totals, np.finfo(power.dtype).tiny)
    active = db >= p.deep_silence_db
    power[~active] = 0
    sums = np.vstack((np.zeros(power.shape[1]), np.cumsum(power, axis=0)))
    counts = np.concatenate(([0], np.cumsum(active)))
    left_end = indices - margin + 1
    left_start = left_end - context
    right_start = indices + margin
    right_end = right_start + context
    left_count = counts[left_end] - counts[left_start]
    right_count = counts[right_end] - counts[right_start]
    supported = (left_count >= context // 2) & (right_count >= context // 2)
    if not supported.any():
        return None
    left = (sums[left_end[supported]] - sums[left_start[supported]]) / left_count[supported, None]
    right = (sums[right_end[supported]] - sums[right_start[supported]]) / right_count[supported, None]
    scores = np.abs(left - right).sum(axis=1) / 2
    positions = times[indices[supported]]
    distances = np.abs(positions - guess)
    # Treat small differences as ties and prefer cuts nearer the guess.
    almost_best = np.flatnonzero(scores >= scores.max() - p.spectral_tie_score)
    best = almost_best[np.argmin(distances[almost_best])]
    rivals = scores[np.abs(positions - positions[best]) > p.spectral_rival_ms]
    if scores[best] < p.spectral_min_score or (rivals.size and scores[best] - rivals.max() < p.spectral_min_margin):
        return None
    return int(positions[best])


def refine_start(w: Window, channels: int, p: RefineParams) -> tuple[int, str]:
    """(start_ms, method) for one window; method is silence / spectral / fallback."""
    frame_length = p.frame_ms * SAMPLES_PER_MS
    samples = np.frombuffer(bytes(w.pcm), dtype=PCM_DTYPE)
    samples = samples[:len(samples) - len(samples) % channels].reshape(-1, channels)
    if len(samples) < frame_length:
        return w.guess_ms, "fallback"
    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_length, axis=0)[::p.hop_ms * SAMPLES_PER_MS]
    times = w.clip_start_ms + p.frame_ms // 2 + p.hop_ms * np.arange(len(frames))
    eligible = (times >= w.lower_ms) & (times <= w.upper_ms)
    rms_power = np.einsum("ijk,ijk->i", frames, frames) / (frame_length * channels)
    db = 10 * np.log10(np.maximum(rms_power, np.finfo(np.float32).tiny))
    for threshold in dict.fromkeys((p.silence_db, p.deep_silence_db)):
        gap = silence_gap(db, times, eligible, threshold, p, w.guess_ms)
        if gap is not None:
            return backtrack(gap, db, times, w.lower_ms, p), "silence"
    boundary = spectral_boundary(frames, db, times, eligible, w.guess_ms, p)
    if boundary is not None:
        return boundary, "spectral"
    return w.guess_ms, "fallback"
