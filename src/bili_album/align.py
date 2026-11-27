"""Align expected track durations to audio boundary candidates (dynamic programming),
and snap rough timestamps to nearby candidates.

The DP places ``n`` consecutive tracks on a set of points (detected candidates
plus a weak 1 s grid so a boundary can still land where nothing was detected).
It allows a constant offset (the first track may start later than 0 and the
last may end before the source does), per-track drift (a soft duration cost),
catalog tracks missing from the video (skip at a fixed penalty) and unknown
extra material between tracks (an "extra" segment at a fixed penalty).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .analyze import Candidate

W_STRENGTH = 3.0       # reward for ending a track on a candidate of strength 1
W_GAP = 3.0            # extra reward for a real silent gap (full at >= 2 s)
P_MISS = 9.0           # catalog track absent from the video
P_EXTRA = 12.0         # unexplained material between two tracks
HEAD_COST = 0.04       # per second of skipped intro
TAIL_COST = 0.04       # per second of skipped outro
GRID = 1.0


@dataclass
class Point:
    time: float
    strength: float
    kind: str
    gap: float = 0.0


@dataclass
class Segment:
    start: float
    end: float
    expected_index: int | None       # index into expected durations, None = extra material
    start_point: Point
    end_point: Point
    expected: float | None = None

    @property
    def deviation(self) -> float | None:
        return None if self.expected is None else (self.end - self.start) - self.expected


@dataclass
class Alignment:
    segments: list[Segment]
    missing: list[int] = field(default_factory=list)
    head: float = 0.0
    tail: float = 0.0
    cost: float = 0.0

    @property
    def mean_abs_dev(self) -> float:
        devs = [abs(s.deviation) for s in self.segments if s.deviation is not None]
        return float(np.mean(devs)) if devs else float("inf")


def sigma_for(d: float, loose: bool = False) -> float:
    return (4.0 + 0.04 * d) if loose else (2.5 + 0.01 * d)


def dur_cost(x: np.ndarray | float, d: float, loose: bool = False) -> np.ndarray | float:
    z = np.abs(np.asarray(x, dtype=float) - d) / sigma_for(d, loose)
    return np.where(z < 1.0, z * z, 2.0 * z - 1.0)


def build_points(cands: list[Candidate], duration: float, grid: float = GRID) -> list[Point]:
    pts = [Point(0.0, 1.0, "start"), Point(duration, 1.0, "end")]
    pts += [Point(c.time, c.strength, c.kind, (c.gap_end - c.gap_start) if c.kind == "silence" else 0.0)
            for c in cands if 0.0 < c.time < duration]
    taken = np.array(sorted(p.time for p in pts))
    for t in np.arange(grid, duration, grid):
        i = np.searchsorted(taken, t)
        near = min(abs(t - taken[i - 1]) if i > 0 else 1e9, abs(t - taken[i]) if i < len(taken) else 1e9)
        if near > grid / 2:
            pts.append(Point(float(t), 0.0, "grid"))
    pts.sort(key=lambda p: p.time)
    return pts


def align(expected: list[float], cands: list[Candidate], duration: float, *, loose: bool = False,
          allow_extra: bool = True, max_head: float | None = None, max_tail: float | None = None
          ) -> Alignment:
    n = len(expected)
    pts = build_points(cands, duration)
    T = np.array([p.time for p in pts])
    S = np.array([p.strength for p in pts])
    m = len(pts)
    G = np.array([p.gap for p in pts])
    reward = W_STRENGTH * S + W_GAP * np.minimum(G, 2.0) / 2.0
    max_head = duration * 0.25 if max_head is None else max_head
    max_tail = duration * 0.25 if max_tail is None else max_tail
    INF = 1e18
    # cost[i][j]: tracks 0..i-1 placed, last placed boundary at point j.
    cost = np.full((n + 1, m), INF)
    back: list[list[tuple | None]] = [[None] * m for _ in range(n + 1)]
    head_ok = T <= max_head
    cost[0, head_ok] = HEAD_COST * T[head_ok] - np.where(T[head_ok] > 0, reward[head_ok] * 0.5, 0)
    strong_idx = np.nonzero((S > 0) | (T == 0))[0]
    for i in range(n + 1):
        if i > 0:
            d = expected[i - 1]
            lo_x, hi_x = max(1.0, 0.4 * d), 1.6 * d + 30
            for j in range(m):
                lo = np.searchsorted(T, T[j] - hi_x)
                hi = np.searchsorted(T, T[j] - lo_x, side="right")
                if hi > lo:
                    prev = cost[i - 1, lo:hi]
                    c = prev + dur_cost(T[j] - T[lo:hi], d, loose) - reward[j]
                    k = int(np.argmin(c))
                    if c[k] < cost[i, j]:
                        cost[i, j] = c[k]
                        back[i][j] = ("track", i - 1, lo + k)
                # Missing track: stay on the same point.
                if cost[i - 1, j] + P_MISS < cost[i, j]:
                    cost[i, j] = cost[i - 1, j] + P_MISS
                    back[i][j] = ("miss", i - 1, j)
        if allow_extra and 0 < i < n:
            # Extra material after track i-1, only between strong points.
            for j in strong_idx:
                lo = np.searchsorted(strong_idx, np.searchsorted(T, T[j] - 480))
                hi = np.searchsorted(strong_idx, np.searchsorted(T, T[j] - 30, side="right"))
                ks = strong_idx[lo:hi]
                if len(ks) == 0:
                    continue
                c = cost[i, ks] + P_EXTRA - reward[j]
                k = int(np.argmin(c))
                if c[k] < cost[i, j]:
                    cost[i, j] = c[k]
                    back[i][j] = ("extra", i, int(ks[k]))
    tail_ok = (duration - T) <= max_tail
    final = cost[n] + np.where(tail_ok, TAIL_COST * (duration - T), INF)
    j = int(np.argmin(final))
    total = float(final[j])
    segs: list[Segment] = []
    missing: list[int] = []
    i = n
    guard = 0
    while i > 0 or back[i][j] is not None:
        b = back[i][j]
        if b is None:
            break
        kind, idx, k = b
        if kind == "track":
            segs.append(Segment(float(T[k]), float(T[j]), idx, pts[k], pts[j], expected[idx]))
            i, j = i - 1, k
        elif kind == "miss":
            missing.append(idx)
            i = i - 1
        else:
            segs.append(Segment(float(T[k]), float(T[j]), None, pts[k], pts[j]))
            j = k
        guard += 1
        if guard > 10 * (n + m):
            raise RuntimeError("alignment backtrack loop")
    segs.reverse()
    missing.reverse()
    head = segs[0].start if segs else 0.0
    tail = duration - segs[-1].end if segs else 0.0
    return Alignment(segs, missing, head, tail, total)


def snap(t: float, cands: list[Candidate], window: float = 3.0, *, prefer_before: bool = False
         ) -> tuple[float, Candidate | None]:
    """Move a rough time (e.g. a whole-second uploader timestamp) to the best nearby candidate."""
    best, best_score = None, -1e9
    for c in cands:
        dt = c.time - t
        if abs(dt) > window:
            continue
        # A silence gap that contains t is a perfect hit.
        inside = c.kind == "silence" and c.gap_start - 0.3 <= t <= c.gap_end + 0.3
        score = c.strength - 0.5 * abs(dt) / window + (0.5 if inside else 0.0)
        if prefer_before and dt > 0:
            score -= 0.1
        if score > best_score:
            best, best_score = c, score
    if best is None or (best.kind == "novelty" and best.strength < 0.35):
        return t, None
    return best.time, best
