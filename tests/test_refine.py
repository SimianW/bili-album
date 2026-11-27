"""Millisecond boundary refinement on small generated AAC files (needs ffmpeg, no network),
and how the planner applies it."""

import shutil
import wave
import subprocess
from pathlib import Path

import numpy as np
import pytest

from bili_album import cuts as cuts_mod
from bili_album.plan import Planner, PlanOptions, Seg
from bili_album.recognize import Recognition
from bili_album.refine import RefineParams, refine_starts

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_audio(path: Path, graph: str, duration: float) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-filter_complex", graph, "-t", str(duration),
                    "-ac", "2", "-ar", "44100", "-c:a", "aac", "-b:a", "192k", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def gap_audio(tmp_path_factory):
    """Tone 0-3 s, digital silence 3-4 s, tone 4-7 s: the second track starts at 4.0 s."""
    p = tmp_path_factory.mktemp("refine") / "gap.m4a"
    return make_audio(p, "sine=f=440:d=3,apad=pad_dur=1[a];sine=f=660:d=3[b];[a][b]concat=n=2:v=0:a=1", 7)


@pytest.fixture(scope="module")
def fade_audio(tmp_path_factory):
    """Tone 0-3 s, silence 3-4 s, then a tone fading in over 1.5 s from 4.0 s (a soft intro)."""
    p = tmp_path_factory.mktemp("refine") / "fade.m4a"
    return make_audio(p, "sine=f=440:d=3,apad=pad_dur=1[a];sine=f=660:d=3,afade=t=in:d=1.5[b];"
                         "[a][b]concat=n=2:v=0:a=1", 7)


@pytest.fixture(scope="module")
def gapless_audio(tmp_path_factory):
    """440 Hz tone 0-3 s then white-ish noise 3-6 s, no gap: only a spectral change at 3.0 s."""
    p = tmp_path_factory.mktemp("refine") / "gapless.m4a"
    return make_audio(p, "sine=f=440:d=3,volume=0.5[a];anoisesrc=d=3:a=0.3:c=pink[b];[a][b]concat=n=2:v=0:a=1", 6)


@needs_ffmpeg
@pytest.mark.parametrize("guess", [3.4, 4.0, 4.9])
def test_silence_onset_found_from_a_rough_guess(gap_audio, guess):
    [(start, method)] = refine_starts(gap_audio, [guess])
    assert method == "silence"
    # Backtracked from the onset at 4.0 s to the end of the quiet frames, minus the 50 ms pre-roll.
    assert 3.90 <= start <= 4.0


@needs_ffmpeg
def test_soft_intro_stays_with_its_track(fade_audio):
    [(start, method)] = refine_starts(fade_audio, [4.6])
    assert method == "silence" and start <= 4.0


@needs_ffmpeg
def test_spectral_change_without_silence(gapless_audio):
    [(start, method)] = refine_starts(gapless_audio, [3.3])
    assert method == "spectral" and abs(start - 3.0) <= 0.05


@needs_ffmpeg
def test_no_edge_keeps_the_guess(tmp_path):
    p = make_audio(tmp_path / "tone.m4a", "sine=f=440:d=6", 6)
    assert refine_starts(p, [3.0]) == [(3.0, "fallback")]


@needs_ffmpeg
def test_bounds_and_neighbours_limit_the_window(gap_audio):
    # An upper bound before the gap's onset: the onset is outside the window, nothing to find there.
    [(start, method)] = refine_starts(gap_audio, [3.5], bounds=[(None, 3.6)], search_ms=400)
    assert start <= 3.6
    # A window that cannot reach the onset falls back to the guess.
    assert refine_starts(gap_audio, [2.0], search_ms=300) == [(2.0, "fallback")]
    # An empty window (lower bound above upper bound) falls back without decoding problems.
    assert refine_starts(gap_audio, [4.0], bounds=[(4.5, 4.2)]) == [(4.0, "fallback")]


@needs_ffmpeg
def test_keyword_parameters_reach_the_search(gap_audio):
    # Backtracking off: the cut lands on the onset itself.
    [(start, _)] = refine_starts(gap_audio, [4.3], floor_margin_db=0)
    assert abs(start - 4.0) <= 0.02
    with pytest.raises(ValueError):
        refine_starts(gap_audio, [4.0], silence_db=-70, deep_silence_db=-60)
    with pytest.raises(ValueError):
        refine_starts(gap_audio, [4.0, 3.0])


def test_defaults_are_the_documented_ones():
    p = RefineParams()
    assert (p.search_ms, p.silence_db, p.deep_silence_db, p.pre_roll_ms) == (1500, -42.0, -65.0, 50)
    p.validate()


# ---------------------------------------------------------------- planner


def planner(tmp_path, **kw) -> Planner:
    p = Planner(PlanOptions(workdir=tmp_path, **kw))
    return p


def segs4():
    return [Seg(0.0, 100.0, "a"), Seg(100.0, 200.0, "b"), Seg(205.0, 300.0, "c"), Seg(300.0, 400.0, "d")]


def fake_refine(results, calls):
    def run(audio, guesses, **kw):
        calls.append((guesses, kw))
        return results
    return run


def test_planner_refines_only_shared_boundaries(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cuts_mod, "refine_starts", fake_refine([(99.95, "silence"), (300.0, "fallback")], calls))
    monkeypatch.setattr(cuts_mod, "audio_info", lambda a: {"channels": 2})
    p = planner(tmp_path, refine_options={"search_ms": 800})
    s = segs4()
    p._refine_cuts(Path("x.m4a"), s)
    guesses, kw = calls[0]
    assert guesses == [100.0, 300.0]               # track 1 and the start after a gap (205 s) are left alone
    assert kw["search_ms"] == 800 and kw["channels"] == 2
    assert kw["bounds"] == [(1.0, 199.0), (206.0, 399.0)]     # 1 s kept on both sides of each cut
    assert s[1].start == 99.95 and s[0].end == 99.95
    assert s[3].start == 300.0 and s[2].end == 300.0
    assert any(e["kind"] == "refine" and "-50 ms" in e["detail"] for e in s[1].evidence)
    assert any(e["kind"] == "refine" and "kept" in e["detail"] for e in s[3].evidence)
    assert p.refine_info["moved"] == 1 and p.refine_info["methods"] == {"silence": 1, "fallback": 1}


def test_recognised_start_caps_the_window(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cuts_mod, "refine_starts", fake_refine([(99.0, "silence")], calls))
    monkeypatch.setattr(cuts_mod, "audio_info", lambda a: {"channels": 2})
    s = [Seg(0.0, 100.0, "a"), Seg(100.0, 200.0, "b")]
    s[1].pinned = True
    s[1].recog = Recognition(clip_start=110.0, clip_len=12.0, title="b", artist="x", offset=9.8)  # starts at 100.2
    planner(tmp_path)._refine_cuts(Path("x.m4a"), s)
    lo, hi = calls[0][1]["bounds"][0]
    assert hi == pytest.approx(100.0)              # max(current start 100.0, 100.2 - 0.3)


def test_refinement_off_or_failing_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cuts_mod, "audio_info", lambda a: {"channels": 2})
    s = segs4()
    p = planner(tmp_path, refine=False)
    p._refine_cuts(Path("x.m4a"), s)
    assert [x.start for x in s] == [0.0, 100.0, 205.0, 300.0] and p.refine_info["reason"] == "refinement off"

    def boom(*a, **k):
        raise ValueError("ffmpeg: broken")
    monkeypatch.setattr(cuts_mod, "refine_starts", boom)
    p = planner(tmp_path)
    p._refine_cuts(Path("x.m4a"), s)
    assert [x.start for x in s] == [0.0, 100.0, 205.0, 300.0] and "failed" in p.refine_info["reason"]


def test_unknown_refine_option_keeps_the_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(cuts_mod, "audio_info", lambda a: {"channels": 2})
    s = segs4()
    p = planner(tmp_path, refine_options={"no_such_option": 1})
    p._refine_cuts(Path("x.m4a"), s)
    assert [x.start for x in s] == [0.0, 100.0, 205.0, 300.0] and "failed" in p.refine_info["reason"]


@needs_ffmpeg
def test_a_short_track_keeps_its_start_before_its_end(tmp_path):
    # Review finding: B (10-11 s) is followed by unexported material, so its end is not a refined
    # boundary; the tone change at 11.3 s used to pull B's start past its end.
    audio = make_audio(tmp_path / "change.m4a",
                       "sine=f=440:d=11.3[a];sine=f=520:d=20[b];[a][b]concat=n=2:v=0:a=1", 31.3)
    s = [Seg(0.0, 10.0, "a"), Seg(10.0, 11.0, "b"), Seg(20.0, 30.0, "c")]
    cuts_mod.refine_cuts(audio, s)
    assert s[1].start <= s[1].end - 1.0 and s[0].end == s[1].start


def write_wav(path: Path, samples) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path


@needs_ffmpeg
def test_cap_limits_the_cut_not_the_edge_it_comes_from(tmp_path):
    # Review finding: a 2 s fade-in from 14.000 s over a -60 dB floor; recognition caps the cut at 14.04 s.
    # The quiet run's onset (the fade crosses -42 dB at about 14.075 s) lies past the cap, but its backtracked cut
    # does not, so the cut must still go before the intro instead of to a later spectral point.
    rng = np.random.default_rng(1)
    t = np.arange(18 * 16000) / 16000
    x = rng.normal(0, 0.001, t.size)
    x[t < 10] += 0.3 * np.sin(2 * np.pi * 440 * t[t < 10])
    fade = t >= 14
    x[fade] += np.minimum(1.0, (t[fade] - 14) / 2) * 0.3 * np.sin(2 * np.pi * 660 * t[fade])
    audio = write_wav(tmp_path / "fade.wav", x)
    [(start, method)] = refine_starts(audio, [14.04], channels=1, bounds=[(None, 14.04)])
    assert method == "silence" and start <= 14.0


def test_moved_cuts_redo_the_catalog_length_check(tmp_path, monkeypatch):
    # Review finding: a length checked before refinement kept its old verdict and confidence.
    from bili_album.catalog import CatTrack
    monkeypatch.setattr(cuts_mod, "refine_starts", fake_refine([(101.5, "silence")], []))
    monkeypatch.setattr(cuts_mod, "audio_info", lambda a: {"channels": 2})
    p = planner(tmp_path)
    s = [Seg(0.0, 100.0, "a"), Seg(100.0, 200.0, "b")]
    s[0].cat = CatTrack(title="a", artist="x", duration=107.0, number=1)
    s[1].cat = CatTrack(title="b", artist="x", duration=100.0, number=2)
    p._check_durations(s)
    assert "disagrees" in next(e["detail"] for e in s[0].evidence if e["kind"] == "catalog_duration")
    p._refine_cuts(Path("x.m4a"), s)
    lens = [next(e["detail"] for e in x.evidence if e["kind"] == "catalog_duration") for x in s]
    assert lens[0].startswith("length 101.5s vs catalog 107.0s") and lens[1].startswith("length 98.5s vs")


@needs_ffmpeg
def test_a_run_whose_cut_is_past_the_cap_does_not_make_a_tie(tmp_path):
    # Review finding: quiet runs end at 19.5 s and 20.5 s, equally far from the guess (20.0 s, also the
    # recognition cap). The second run's cut lies past the cap, so it must not count as a rival.
    rng = np.random.default_rng(2)
    t = np.arange(24 * 16000) / 16000
    x = 0.3 * np.sin(2 * np.pi * 440 * t)
    x[((t >= 18.88) & (t < 19.5)) | ((t >= 19.9) & (t < 20.5))] = 0
    x += rng.normal(0, 0.001, t.size)
    audio = write_wav(tmp_path / "two-gaps.wav", x)
    [(start, method)] = refine_starts(audio, [20.0], channels=1, bounds=[(None, 20.0)])
    assert method == "silence" and 19.3 <= start <= 19.5


def test_moved_cuts_rescore_catalog_aligned_lengths(tmp_path, monkeypatch):
    # Review finding: a catalog-aligned track (no timestamps) kept its pre-refinement length verdict.
    from bili_album.catalog import CatTrack
    monkeypatch.setattr(cuts_mod, "refine_starts", fake_refine([(51.117, "spectral")], []))
    monkeypatch.setattr(cuts_mod, "audio_info", lambda a: {"channels": 2})
    p = planner(tmp_path)
    s = [Seg(0.0, 51.7, "a", confidence=0.55), Seg(51.7, 102.0, "b", confidence=0.55)]
    s[0].cat = CatTrack(title="a", artist="x", duration=51.0, number=1)
    s[1].cat = CatTrack(title="b", artist="x", duration=56.5, number=2)
    for x in s:
        p._score_aligned_length(x)
    assert s[1].confidence == pytest.approx(0.40) and s[1].length_mismatch is not None
    p._refine_cuts(Path("x.m4a"), s)
    assert s[1].confidence == pytest.approx(0.70) and s[1].length_mismatch is None
    assert [e["detail"] for e in s[1].evidence if e["kind"] == "catalog_duration"] == [
        "aligned catalog length 56.5s to 50.9s (-5.6s)"]
