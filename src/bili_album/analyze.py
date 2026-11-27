"""Audio boundary candidates from a low-rate mono decode.

One ffmpeg decode (8 kHz mono s16) is streamed into numpy and reduced to a
10 Hz feature track: RMS level in dBFS and 32 log-spaced band energies.
From that we derive boundary *candidates*:

- ``silence``: runs below an adaptive threshold. A run is often not one gap but
  a fade-out, a short true gap and a soft intro. The cut goes at the end of the
  run's deepest block (frames within ``CORE_DB`` of the run's minimum, or at digital-silence
  level), just before
  the level rises out of it, so a quiet intro stays with its own song. For a run
  that is uniformly quiet (a plain gap) that block is the whole run and the cut
  lands just before the next onset, as before. ``floor_db`` records the block's
  level: at or below ``DIGITAL_DB`` it is digital silence.
- ``dip``: deep local minima of the smoothed level (fades / crossfades).
- ``novelty``: peaks of a timbral change curve (two-sided window distance), for
  gapless albums where nothing goes quiet.

Each candidate carries a strength in 0..1. Features are cached as ``.npz``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

SR = 8000
HOP = 800  # 0.1 s
WIN = 1600
FPS = SR / HOP
N_BANDS = 32
DIGITAL_DB = -70.0   # a block this quiet is digital silence: nothing audible can be cut
CORE_DB = 6.0        # frames within this of a run's minimum form its deepest block


@dataclass
class Candidate:
    time: float          # proposed cut point (s)
    strength: float      # 0..1
    kind: str            # silence | dip | novelty
    gap_start: float = 0.0
    gap_end: float = 0.0
    floor_db: float = 0.0    # silence: median level of the deepest block (dBFS)
    core_start: float = 0.0  # silence: the deepest block inside [gap_start, gap_end]
    core_end: float = 0.0

    @property
    def digital(self) -> bool:
        return self.kind == "silence" and self.floor_db <= DIGITAL_DB

    def to_dict(self) -> dict:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}


@dataclass
class Features:
    rms_db: np.ndarray   # (N,)
    bands: np.ndarray    # (N, N_BANDS) log energies
    duration: float

    @property
    def times(self) -> np.ndarray:
        return np.arange(len(self.rms_db)) / FPS


def _band_matrix(n_fft: int) -> np.ndarray:
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    edges = np.geomspace(60, SR / 2 * 0.95, N_BANDS + 1)
    m = np.zeros((N_BANDS, len(freqs)), dtype=np.float32)
    for i in range(N_BANDS):
        sel = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if not sel.any():
            sel[np.argmin(np.abs(freqs - (edges[i] + edges[i + 1]) / 2))] = True
        m[i, sel] = 1.0
    return m


def decode_features(audio: Path | str, cache: Path | None = None) -> Features:
    if cache and cache.exists():
        z = np.load(cache)
        return Features(z["rms_db"], z["bands"], float(z["duration"]))
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(audio), "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    assert proc.stdout
    window = np.hanning(WIN).astype(np.float32)
    bm = _band_matrix(WIN)
    rms_parts, band_parts = [], []
    carry = np.zeros(0, dtype=np.float32)
    total = 0
    chunk_bytes = HOP * 2 * 600  # 60 s
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        x = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
        total += len(x)
        x = np.concatenate([carry, x])
        n = (len(x) - WIN) // HOP + 1 if len(x) >= WIN else 0
        if n > 0:
            idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
            frames = x[idx]
            rms = np.sqrt(np.mean(frames[:, (WIN - HOP) // 2:(WIN + HOP) // 2] ** 2, axis=1) + 1e-12)
            rms_parts.append(20 * np.log10(rms + 1e-9))
            spec = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
            band_parts.append(np.log10(spec @ bm.T + 1e-10).astype(np.float32))
            carry = x[n * HOP:]
        else:
            carry = x
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed for {audio}")
    rms_db = np.concatenate(rms_parts) if rms_parts else np.zeros(0, np.float32)
    bands = np.concatenate(band_parts) if band_parts else np.zeros((0, N_BANDS), np.float32)
    feats = Features(rms_db.astype(np.float32), bands, total / SR)
    if cache:
        np.savez_compressed(cache, rms_db=feats.rms_db, bands=feats.bands, duration=feats.duration)
    return feats


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) == 0:
        return x
    k = np.ones(n) / n
    pad = n // 2
    xp = np.pad(x, (pad, n - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid")


def silence_runs(f: Features, min_len: float = 0.35) -> tuple[list[tuple[float, float, float]], float]:
    """Return ([(start, end, depth_db)], threshold_db)."""
    r = f.rms_db
    if len(r) == 0:
        return [], -60.0
    loud = float(np.percentile(r, 90))
    thr = max(-65.0, min(-40.0, loud - 32.0))
    quiet = r < thr
    runs = []
    i = 0
    n = len(r)
    while i < n:
        if quiet[i]:
            j = i
            while j < n and quiet[j]:
                j += 1
            if (j - i) / FPS >= min_len:
                runs.append((i / FPS, j / FPS, float(loud - np.median(r[i:j]))))
            i = j
        else:
            i += 1
    return runs, thr


def novelty_curve(f: Features, half_window: float = 6.0) -> np.ndarray:
    """Two-sided timbral distance at every frame, normalised to roughly 0..1."""
    b = f.bands
    n = len(b)
    if n == 0:
        return np.zeros(0)
    # Downsample to 2 Hz for speed; features are smooth anyway.
    step = 5
    m = n // step
    if m < 4:
        return np.zeros(n)
    bd = b[: m * step].reshape(m, step, -1).mean(axis=1)
    lvl = _smooth(f.rms_db, 5)[: m * step].reshape(m, step).mean(axis=1)
    feats = np.concatenate([bd, (lvl / 10.0)[:, None]], axis=1)
    feats = (feats - feats.mean(axis=0)) / (feats.std(axis=0) + 1e-6)
    w = max(2, int(half_window * FPS / step))
    cs = np.cumsum(np.pad(feats, ((1, 0), (0, 0))), axis=0)
    cs2 = np.cumsum(np.pad(feats ** 2, ((1, 0), (0, 0))), axis=0)
    out = np.zeros(m)
    for t in range(w, m - w):
        left = (cs[t] - cs[t - w]) / w
        right = (cs[t + w] - cs[t]) / w
        lv = (cs2[t] - cs2[t - w]) / w - left ** 2
        rv = (cs2[t + w] - cs2[t]) / w - right ** 2
        out[t] = np.sum((left - right) ** 2 / (lv + rv + 0.25))
    out = out / (np.percentile(out[w:m - w], 99) + 1e-9) if m > 2 * w else out
    return np.repeat(np.clip(out, 0, 1.5), step)[:n] if len(out) else out


def _peaks(x: np.ndarray, min_dist: int, min_height: float) -> list[int]:
    idx = []
    n = len(x)
    order = np.argsort(-x)
    taken = np.zeros(n, dtype=bool)
    for i in order:
        if x[i] < min_height:
            break
        lo, hi = max(0, i - min_dist), min(n, i + min_dist + 1)
        if taken[lo:hi].any():
            continue
        taken[i] = True
        idx.append(int(i))
    return sorted(idx)


def quiet_core(f: Features, s: float, e: float) -> tuple[float, float, float]:
    """The deepest block of the low-level run [s, e]: the longest stretch of frames within
    ``CORE_DB`` of the run's minimum or at digital-silence level (the later one on a tie).
    Returns (start, end, median dB)."""
    i0, i1 = int(round(s * FPS)), max(int(round(s * FPS)) + 1, int(round(e * FPS)))
    seg = f.rms_db[i0:i1]
    if len(seg) == 0:
        return s, e, 0.0
    # Anything at digital-silence level is as good as the minimum (faint noise after a zero block).
    low = (seg <= float(seg.min()) + CORE_DB) | (seg <= DIGITAL_DB)
    best = (0, 0)
    i = 0
    while i < len(seg):
        if low[i]:
            j = i
            while j < len(seg) and low[j]:
                j += 1
            if j - i >= best[1] - best[0]:
                best = (i, j)
            i = j
        else:
            i += 1
    a, b = best
    return (i0 + a) / FPS, (i0 + b) / FPS, float(np.median(seg[a:b]))


def level_between(f: Features, t0: float, t1: float) -> float:
    """Loudest 0.1 s frame between t0 and t1 (dBFS); -inf when the span is empty."""
    i0, i1 = int(np.floor(min(t0, t1) * FPS)), int(np.ceil(max(t0, t1) * FPS))
    seg = f.rms_db[max(0, i0):max(0, i1)]
    return float(seg.max()) if len(seg) else float("-inf")


def candidates(f: Features) -> list[Candidate]:
    out: list[Candidate] = []
    runs, thr = silence_runs(f)
    for s, e, depth in runs:
        if s <= 0.05 or e >= f.duration - 0.05:
            continue
        gap = e - s
        cs, ce, floor = quiet_core(f, s, e)
        cut = max(cs, ce - min(0.2, (ce - cs) / 2))
        strength = min(1.0, 0.55 + 0.25 * min(gap, 2.0) / 2.0 + 0.2 * min(depth, 40) / 40)
        out.append(Candidate(cut, strength, "silence", s, e, floor, cs, ce))
    # Level dips that do not reach silence (fades, crossfades).
    sm = _smooth(f.rms_db, 10)
    ctx = _smooth(f.rms_db, 150)
    depth = ctx - sm
    for i in _peaks(depth, int(4 * FPS), 9.0):
        t = i / FPS
        if any(abs(t - c.time) < 2.0 for c in out):
            continue
        out.append(Candidate(t, float(min(0.75, 0.25 + depth[i] / 40)), "dip", t, t))
    nov = novelty_curve(f)
    for i in _peaks(nov, int(5 * FPS), 0.25):
        t = i / FPS
        near = [c for c in out if abs(t - c.time) < 2.5]
        if near:
            continue
        out.append(Candidate(t, float(min(0.6, 0.2 + 0.4 * nov[i])), "novelty", t, t))
    out.sort(key=lambda c: c.time)
    return out


def onset_after(f: Features, t: float, max_look: float = 3.0) -> float | None:
    """First frame after t where level rises 20 dB above the local floor."""
    i0 = int(t * FPS)
    i1 = min(len(f.rms_db), int((t + max_look) * FPS))
    if i1 <= i0:
        return None
    seg = f.rms_db[i0:i1]
    floor = float(seg.min())
    above = np.nonzero(seg > floor + 20)[0]
    return (i0 + int(above[0])) / FPS if len(above) else None


def analyze(audio: Path, workdir: Path) -> tuple[Features, list[Candidate]]:
    cache = workdir / "features.npz"
    feats = decode_features(audio, cache)
    cands = candidates(feats)
    (workdir / "candidates.json").write_text(
        json.dumps([c.to_dict() for c in cands], ensure_ascii=False, indent=0), encoding="utf-8")
    return feats, cands
