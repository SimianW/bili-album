import random

import pytest

from bili_album.align import align, snap
from bili_album.analyze import Candidate


def sil(t, strength=0.9, gap=1.0):
    return Candidate(t, strength, "silence", t - gap + 0.2, t + 0.2)


def nov(t, strength=0.4):
    return Candidate(t, strength, "novelty", t, t)


def boundaries(expected, offset=0.0, drift=None):
    out, t = [], offset
    for i, d in enumerate(expected):
        t += d + (drift[i] if drift else 0.0)
        out.append(t)
    return out


EXP = [210.0, 185.5, 240.2, 199.9, 260.0, 175.3]


def test_exact_with_intro_offset_and_noise():
    rng = random.Random(1)
    offset = 12.0
    bs = boundaries(EXP, offset)
    duration = bs[-1] + 6.0  # outro
    cands = [sil(offset)] + [sil(b) for b in bs[:-1]] + [sil(bs[-1])]
    cands += [nov(rng.uniform(5, duration - 5), rng.uniform(0.2, 0.6)) for _ in range(40)]
    a = align(EXP, sorted(cands, key=lambda c: c.time), duration)
    assert [s.expected_index for s in a.segments] == list(range(len(EXP)))
    assert a.segments[0].start == pytest.approx(offset, abs=0.01)
    for s, b in zip(a.segments, bs):
        assert s.end == pytest.approx(b, abs=0.01)
    assert a.missing == []


def test_missing_track_is_skipped():
    present = [0, 1, 3, 4, 5]  # track 2 not in the video
    durs = [EXP[i] for i in present]
    bs = boundaries(durs)
    cands = [sil(b) for b in bs[:-1]]
    a = align(EXP, cands, bs[-1])
    assert a.missing == [2]
    assert [s.expected_index for s in a.segments] == present
    for s, b in zip(a.segments, bs):
        assert s.end == pytest.approx(b, abs=0.01)


def test_drift_prefers_real_gaps_over_grid():
    drift = [3.0, -2.5, 4.0, -3.5, 2.0, 0.0]
    bs = boundaries(EXP, 0.0, drift)
    cands = [sil(b, gap=1.5) for b in bs[:-1]] + [nov(b - 20) for b in bs[:-1]]
    a = align(EXP, sorted(cands, key=lambda c: c.time), bs[-1])
    for s, b in zip(a.segments, bs):
        assert s.end == pytest.approx(b, abs=0.01)


def test_gapless_without_candidates_falls_back_to_grid():
    bs = boundaries(EXP)
    a = align(EXP, [], bs[-1])
    assert len(a.segments) == len(EXP)
    for s, b in zip(a.segments, bs):
        assert abs(s.end - b) <= 1.0


def test_extra_material_between_tracks():
    durs = EXP[:3] + [95.0] + EXP[3:]  # an unlisted skit after track 3
    bs = boundaries(durs)
    cands = [sil(b, gap=2.0) for b in bs[:-1]]
    a = align(EXP, cands, bs[-1])
    kinds = [s.expected_index for s in a.segments]
    assert kinds == [0, 1, 2, None, 3, 4, 5]


def test_snap_prefers_silence_containing_the_rough_time():
    cands = [nov(100.4, 0.5), Candidate(101.7, 0.9, "silence", 99.5, 101.9)]
    t, c = snap(100.0, cands, 3.0)
    assert c.kind == "silence" and t == 101.7


def test_snap_keeps_time_when_nothing_close():
    t, c = snap(50.0, [sil(60.0)], 3.0)
    assert t == 50.0 and c is None


def test_snap_ignores_weak_novelty():
    t, c = snap(50.0, [nov(50.5, 0.25)], 3.0)
    assert t == 50.0 and c is None
