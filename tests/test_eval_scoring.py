"""Unit tests for eval/scoring.py (the evaluation judge)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import scoring as sc  # noqa: E402


# ---------------------------------------------------------------- normalisation and text rules

def test_normalize_folds_width_case_script_punct():
    assert sc.normalize("Ｈｉ-Ｒｅｓ！") == "hires"
    assert sc.normalize("萬能青年旅店") == "万能青年旅店"
    assert sc.normalize("爸，我回来了") == sc.normalize("爸,我回来了")
    assert sc.normalize("5:20AM") == sc.normalize("520am")
    assert sc.normalize("𝐇𝐢-𝐑𝐞𝐬") == "hires"


@pytest.mark.parametrize("truth,pred,ok", [
    ("Yellow", "yellow", True),
    ("Starboy", "Starboy (feat. Daft Punk)", True),
    ("Party Monster", "Party Monster [Remastered 2016]", True),
    ("上海一九四三", "上海一九四三", True),
    ("簡單愛", "简单爱", True),
    ("Paranoid Android", "Paranoid Andriod", True),      # ratio >= 0.85
    ("Karma Police", "Lucky", False),
    ("Yellow", "", False),
    ("Yellow", None, False),
    ("Airbag", "Airbag - Remastered", True),
])
def test_title_ok(truth, pred, ok):
    assert sc.title_ok(truth, pred) is ok


def test_primary_artist_and_artist_ok():
    assert sc.primary_artist("The Weeknd, Daft Punk") == "The Weeknd"
    assert sc.primary_artist("A2O & A2O SOUL") == "A2O"
    assert sc.primary_artist("周杰伦、费玉清") == "周杰伦"
    assert sc.artist_ok("The Weeknd feat. Daft Punk", "The Weeknd")
    assert sc.artist_ok("張杰", "张杰")
    assert sc.artist_ok("KEY.L刘聪", "KEY.L 刘聪")
    assert not sc.artist_ok("Coldplay", "Radiohead")
    assert not sc.artist_ok("Coldplay", "")


def test_year_ok():
    assert sc.year_ok(2001, "2001-09-14")
    assert sc.year_ok(2001, 2001)
    assert not sc.year_ok(2001, "2002")
    assert not sc.year_ok(2001, "")


# ---------------------------------------------------------------- boundary matching

def test_match_is_one_to_one_and_order_preserving():
    assign, extra = sc.match_boundaries([100.0, 200.0], [101.0, 102.0, 199.0])
    assert assign == [0, 2]
    assert extra == [1]


def test_match_prefers_more_pairs_over_smaller_error():
    # Pairing 10 with 18 (err 8) and 20 with 21 beats only 20<->18 (err 2).
    assign, extra = sc.match_boundaries([10.0, 20.0], [18.0, 21.0])
    assert assign == [0, 1] and extra == []


def test_match_minimises_total_error_among_maximal():
    assign, _ = sc.match_boundaries([100.0], [95.0, 99.0, 108.0])
    assert assign == [1]


def test_match_window():
    assign, extra = sc.match_boundaries([100.0], [116.0])
    assert assign == [None] and extra == [0]
    assign, _ = sc.match_boundaries([100.0], [115.0])
    assert assign == [0]


def test_match_empty():
    assert sc.match_boundaries([], []) == ([], [])
    assert sc.match_boundaries([1.0], []) == ([None], [])
    assert sc.match_boundaries([], [1.0]) == ([], [0])


def _truth(starts, titles=None):
    titles = titles or [f"t{i}" for i in range(1, len(starts) + 1)]
    return [{"n": i, "start": s, "title": ti, "artist": "A"} for i, (s, ti) in enumerate(zip(starts, titles), 1)]


def _pred(starts, end, titles=None):
    titles = titles or [f"t{i}" for i in range(1, len(starts) + 1)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end
        out.append({"index": i + 1, "start": s, "end": e, "title": titles[i], "artist": "A", "confidence": 0.9})
    return out


def test_score_boundaries_rows_and_metrics():
    truth = _truth([0.0, 100.0, 200.0, 300.0])
    pred = _pred([0.0, 100.5, 196.0, 250.0], 400.0)
    b = sc.score_boundaries(truth, pred)
    assert [r["error"] for r in b["boundaries"]] == [0.5, -4.0, None]
    assert b["boundaries"][2]["pred_start"] is None
    assert b["extra_boundaries"] == [250.0]
    c = sc.boundary_counts(b["boundaries"], len(b["extra_boundaries"]))
    r = sc.rates_from_counts(c["n_truth_b"], c["n_pred_b"], c["hits"], c["abs_errs"])
    assert r["hit_1s"] == round(1 / 3, 4) and r["hit_3s"] == round(1 / 3, 4) and r["hit_5s"] == round(2 / 3, 4)
    assert r["precision_3s"] == round(1 / 3, 4) and r["recall_3s"] == round(1 / 3, 4)
    assert r["f1_3s"] == round(1 / 3, 4)
    assert r["median_abs_err"] == 2.25
    assert r["p90_abs_err"] == pytest.approx(3.65)


def test_percentile():
    assert sc.percentile([], 90) is None
    assert sc.percentile([5.0], 90) == 5.0
    assert sc.percentile([0.0, 10.0], 50) == 5.0
    assert sc.percentile(list(range(11)), 90) == 9.0


# ---------------------------------------------------------------- titles by overlap

def test_titles_pick_largest_overlap():
    truth = _truth([0.0, 100.0, 200.0], ["Alpha", "Beta", "Gamma"])
    pred = _pred([0.0, 140.0, 200.0], 300.0, ["Alpha", "Beta", "Gamma"])
    rows = sc.score_titles(truth, pred, 300.0)
    # truth track 2 (100-200): pred 1 overlaps 40 s, pred 2 overlaps 60 s -> pred 2
    assert rows[1]["pred_title"] == "Beta" and rows[1]["overlap"] == 0.6
    assert all(r["title_ok"] for r in rows)


def test_titles_no_prediction():
    rows = sc.score_titles(_truth([0.0]), [], 100.0)
    assert rows[0]["pred_title"] is None and not rows[0]["title_ok"] and rows[0]["overlap"] == 0.0


def test_global_tracks_offsets_multipart():
    plan = {"tracks": [
        {"index": 1, "start": 0.0, "end": 10.0, "title": "a", "artist": "x", "audio_path": "/p1",
         "evidence": [{"kind": "part"}, {"kind": "catalog_duration"}, {"kind": "part"}]},
        {"index": 2, "start": 0.0, "end": 20.0, "title": "b", "artist": "x", "audio_path": "/p2", "evidence": []},
    ]}
    g = sc.global_tracks(plan, {"/p1": 0.0, "/p2": 10.0})
    assert [(t["start"], t["end"]) for t in g] == [(0.0, 10.0), (10.0, 30.0)]
    assert g[0]["evidence_kinds"] == ["part", "catalog_duration"]


# ---------------------------------------------------------------- whole run

CASE = {"id": "c", "kind": "compilation", "duration": 400.0, "album": "Album", "album_artist": "A", "year": 2001,
        "tracks": _truth([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"])}


def _plan(starts, titles, kind="compilation", review=False, year="2001-01-01"):
    return {"kind": kind, "kind_reason": "r", "needs_review": review, "review_reasons": [],
            "source": {"duration": 400.5},
            "album": {"title": "Album", "artist": "A", "year": year, "catalog": {"provider": "netease"}},
            "timings": {"fetch": 1, "download": 2, "analyze": 3, "catalog": 4, "recognize": 5, "total": 15},
            "tracks": _pred(starts, 400.0, titles)}


def test_score_run_clean():
    r = sc.score_run(CASE, "hints", _plan([0.0, 100.2, 199.5, 300.0], ["One", "Two", "Three", "Four"]))
    assert r["kind_ok"] and not r["had_problem"] and r["problems"] == []
    assert r["metrics"]["hit_1s"] == 1.0 and r["metrics"]["f1_3s"] == 1.0
    assert r["trim"] == {"first_start_error": 0.0, "last_end_error": 0.0, "last_end_error_vs_audio": -0.5}
    assert r["album"]["title_ok"] and r["album"]["artist_ok"] and r["album"]["year_ok"]
    assert r["output"] is None   # no output files given


def test_score_run_problems():
    r = sc.score_run(CASE, "blind", _plan([0.0, 100.0, 210.0], ["One", "Two", "Nope"], review=False, year="1999"))
    assert r["had_problem"]
    txt = " | ".join(r["problems"])
    assert "3 tracks vs 4" in txt and "boundaries" in txt and "wrong title" in txt and "album year" in txt


def test_score_run_failed():
    r = sc.score_run(CASE, "blind", None)
    assert r["had_problem"] and r["needs_review"] is None and r["kind_ok"] is False
    assert len(r["boundaries"]) == 3 and all(b["error"] is None for b in r["boundaries"])
    assert r["metrics"]["recall_3s"] == 0.0 and r["metrics"]["title_acc"] == 0.0


def test_expected_kind_and_boundary_scope():
    assert sc.expected_kind("multipart", "hints") == "multipart"
    assert sc.expected_kind("multipart", "blind") == "compilation"
    assert sc.expected_kind("single", "blind_norecog") == "single"
    assert not sc.scores_boundaries("multipart", "hints")
    assert sc.scores_boundaries("multipart", "blind")
    assert not sc.scores_boundaries("single", "blind")


def test_null_album_truth_is_skipped():
    case = dict(CASE, album=None, year=None)
    r = sc.score_run(case, "hints", _plan([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"]))
    assert r["album"]["title_ok"] is None and r["album"]["year_ok"] is None


# ---------------------------------------------------------------- output files

def _file(dur, **over):
    f = {"file": "x.m4a", "title": "t", "artist": "a", "album": "b", "album_artist": "a", "track": 1, "disc": 1,
         "year": "2001", "genre": "g", "cover": "100B", "composer": "", "lyrics": "",
         "probe": {"codec": "aac", "sample_rate": 48000, "channels": 2, "bitrate_kbps": 200, "duration": dur}}
    f.update(over)
    return f


def test_score_output():
    src = {"codec": "aac", "sample_rate": 48000, "channels": 2, "bitrate_kbps": 205}
    files = [_file(100.02), _file(49.9, composer="c")]
    o = sc.score_output(2, [100.0, 50.0], files, src)
    assert o["files"] == 2 and o["files_match_plan"]
    assert o["tag_completeness"]["title"] == 1.0 and o["tag_completeness"]["composer"] == 0.5
    assert o["tag_completeness"]["lyrics"] == 0.0
    assert o["max_duration_dev"] == 0.1
    assert o["stream_copy_ok"]
    o2 = sc.score_output(2, [100.0, 50.0], [_file(100.0)], dict(src, bitrate_kbps=320))
    assert not o2["files_match_plan"] and not o2["bitrate_within_5pct"]
    assert o2["stream_copy_ok"] and o2["stream_copy_format_ok"]   # only the bitrate differs (VBR source)
    o3 = sc.score_output(1, [100.0], [_file(100.0)], dict(src, sample_rate=44100))
    assert not o3["stream_copy_ok"] and not o3["stream_copy_format_ok"]


# ---------------------------------------------------------------- summary pooling

def test_summary_is_micro_averaged_and_counts_failures():
    good = sc.score_run(CASE, "hints", _plan([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"]))
    good.update(ok=True, wall_seconds=10.0)
    single = {"id": "s", "kind": "single", "duration": 100.0, "album": None, "album_artist": None, "year": None,
              "tracks": _truth([0.0], ["Solo"])}
    bad = sc.score_run(single, "hints", None)
    bad.update(ok=False, wall_seconds=5.0)
    s = sc.summarize([(CASE, good), (single, bad)])
    assert s["cases"] == 2 and s["ok"] == 1 and s["kind_acc"] == 0.5
    assert s["boundaries"] == 3 and s["hit_1s"] == 1.0
    assert s["titles"] == 5 and s["title_acc"] == 0.8
    assert s["album_title_acc"] == 1.0          # null truth skipped
    assert s["review_confusion"]["failed"] == 1 and s["review_confusion"]["no_review_no_problem"] == 1
    assert s["audio_seconds"] == 500.0 and s["wall_seconds"] == 15.0
    assert s["realtime_factor"] == 40.0
    assert s["timings"]["recognize"] == 5.0


def test_summary_review_confusion_all_cells():
    clean = _plan([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"])
    wrong = _plan([0.0, 150.0], ["One", "X"])
    runs = []
    for plan, review in ((clean, True), (clean, False), (wrong, True), (wrong, False)):
        r = sc.score_run(CASE, "blind", dict(plan, needs_review=review))
        r.update(ok=True, wall_seconds=1.0)
        runs.append((CASE, r))
    conf = sc.summarize(runs)["review_confusion"]
    assert conf == {"review_problem": 1, "review_no_problem": 1, "no_review_problem": 1,
                    "no_review_no_problem": 1, "failed": 0}


def test_alt_truth_boundaries_for_synthetic_multipart():
    case = {"id": "m", "kind": "multipart", "duration": 400.0, "album": None, "album_artist": None, "year": None,
            "tracks": _truth([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"])}
    plan = _plan([0.0, 99.5, 198.5, 297.0], ["One", "Two", "Three", "Four"])
    r = sc.score_run(case, "blind", plan, alt_starts=[0.0, 99.6, 198.8, 297.2])
    assert [b["error"] for b in r["boundaries"]] == [-0.5, -1.5, -3.0]
    assert [b["error"] for b in r["boundaries_alt_truth"]["boundaries"]] == [-0.1, -0.3, -0.2]
    assert r["boundaries_alt_truth"]["metrics"]["hit_1s"] == 1.0
    r.update(ok=True, wall_seconds=1.0)
    s = sc.summarize([(case, r)])
    assert s["hit_1s"] == round(1 / 3, 4) and s["alt_truth"]["hit_1s"] == 1.0
    assert sc.score_run(case, "hints", plan, alt_starts=[0.0, 99.6, 198.8, 297.2])["boundaries_alt_truth"] is None


# ---------------------------------------------------------------- Title (Alias) pairs

@pytest.mark.parametrize("truth,pred,ok", [
    ("Very First Love (첫사랑)", "첫사랑", True),            # inside the brackets
    ("Very First Love (첫사랑)", "Very First Love", True),   # outside the brackets
    ("괴물", "괴물(Monster)", True),                           # alias pair on the predicted side
    ("처음이자 마지막으로", "First and Last", False),          # a translation alone is not the original title
    ("연애의 조건", "연애의 조건 (LOVE OPTIONS)", True),
    ("甲乙丙丁Strangers", "甲乙丙丁 (你我怎么两清)", True),     # outside part contained after normalisation
    ("Very First Love (첫사랑)", "Moonlight Shadow", False),
    ("Starboy (feat. Daft Punk)", "Starboy", True),           # version brackets still handled by stripping
])
def test_title_alias_pairs(truth, pred, ok):
    assert sc.title_ok(truth, pred) is ok


def test_title_variants():
    assert sc.title_variants("Very First Love (첫사랑)") == ["Very First Love (첫사랑)", "Very First Love", "첫사랑"]
    assert sc.title_variants("Yellow") == ["Yellow"]
    assert sc.title_variants("Hong Kong (feat. 王菲季候风)") == ["Hong Kong (feat. 王菲季候风)"]


# ---------------------------------------------------------------- measured truth for synthetic multipart

MP = {"id": "m", "kind": "multipart", "duration": 700.0, "album": None, "album_artist": None, "year": None,
      "tracks": _truth([0.0, 234.0, 464.0], ["A", "B", "C"])}


def test_with_measured_starts_replaces_and_keeps_yaml():
    ce = sc.with_measured_starts(MP, [0.0, 233.685, 463.4234])
    assert [t["start"] for t in ce["tracks"]] == [0.0, 233.685, 463.423]
    assert [t["start_cases_yaml"] for t in ce["tracks"]] == [0.0, 234.0, 464.0]
    assert ce["start_basis"] == sc.MEASURED_BASIS
    assert [t["start"] for t in MP["tracks"]] == [0.0, 234.0, 464.0]        # input not mutated


def test_with_measured_starts_ignores_bad_input():
    assert sc.with_measured_starts(MP, None) is MP
    assert sc.with_measured_starts(MP, [0.0, 233.7]) is MP                # wrong count
    assert sc.with_measured_starts(CASE, [0.0, 1.0, 2.0, 3.0]) is CASE    # not multipart


def test_measured_truth_is_primary_and_yaml_is_alt():
    ce = sc.with_measured_starts(MP, [0.0, 233.685, 463.423])
    plan = _plan([0.0, 233.7, 466.0], ["A", "B", "C"])
    yaml_starts = [t["start_cases_yaml"] for t in ce["tracks"]]
    r = sc.score_run(ce, "blind", plan, alt_starts=yaml_starts)
    assert [b["truth_start"] for b in r["boundaries"]] == [233.685, 463.423]
    assert [b["error"] for b in r["boundaries"]] == [0.015, 2.577]
    assert r["boundaries_alt_truth"]["basis"] == sc.YAML_BASIS
    assert [b["error"] for b in r["boundaries_alt_truth"]["boundaries"]] == [-0.3, 2.0]


# ---------------------------------------------------------------- stricter artist rule

@pytest.mark.parametrize("truth,pred,ok", [
    ("黄霄雲", "黄霄云 原创", False),                       # a stray word is not a second artist
    ("黄霄雲", "黄霄云", True),
    ("Billie Eilish", "Billie Eilish & Vince Staples", True),
    ("FLY COAST feat. Ai Ninomiya", "FLY COAST feat.Ai Ninomiya", True),
    ("周杰伦", "周杰倫 (Jay Chou)", True),
    ("The Weeknd", "The Weeknd, Daft Punk", True),
    ("A2O", "A2O SOUL", False),
    ("soldier", "苏也莉", False),
    ("Coldplay", "Various Artists", False),
])
def test_artist_rule_names_not_substrings(truth, pred, ok):
    assert sc.artist_ok(truth, pred) is ok


def test_credited_names():
    assert sc.credited_names("周杰倫 (Jay Chou)、费玉清") == ["周杰倫 (Jay Chou)", "周杰倫", "Jay Chou", "费玉清"]


# ---------------------------------------------------------------- output: stream copy, bitrate, script

def test_stream_copy_is_format_check_and_bitrate_reported_apart():
    src = {"codec": "aac", "sample_rate": 48000, "channels": 2, "bitrate_kbps": 150.0}
    o = sc.score_output(2, [100.0, 50.0], [_file(100.0), _file(50.0)], src)   # files at 200 kbps (VBR drift)
    assert o["stream_copy_ok"] and not o["bitrate_within_5pct"]
    assert o["bitrate_ratio"] == {"min": 1.333, "max": 1.333}


def test_traditional_strings():
    files = [_file(10.0, title="擱淺", artist="周杰伦", genre="華語流行樂", lyrics="擱淺 lyrics are skipped"),
             _file(10.0, title="さくら（独唱）", album="風の詩"),        # kana in the string: Japanese, left alone
             _file(10.0, album_artist="張學友")]
    hits = sc.score_output(3, [10.0, 10.0, 10.0], files, None)["traditional_strings"]
    assert [(h["tag"], h["value"], h["simplified"]) for h in hits] == [
        ("title", "擱淺", "搁浅"), ("genre", "華語流行樂", "华语流行乐"), ("album_artist", "張學友", "张学友")]
    assert sc.score_output(1, [10.0], [_file(10.0)], None)["traditional_strings"] == []


# ---------------------------------------------------------------- reviewed plan and owner counts

REVIEW = {"verdict": "edited", "confidence": 0.82, "edits_applied": [{"op": "relabel_track"}, {"op": "x"}],
          "edits_rejected": [{"edit": {"op": "move_boundary"}, "why": "w"}], "flagged_tracks": [3],
          "replan": {"request": {"album_hint": "A - B"}}, "timing": {"total_s": 41.5}, "error": None}


def test_review_info_counts_and_flags():
    info = sc.review_info(REVIEW, 44.0)
    assert info == {"verdict": "edited", "confidence": 0.82, "edits_applied": 2, "edits_rejected": 1,
                    "flagged_tracks": [3], "replanned": True, "seconds": 44.0, "error": None}
    assert sc.review_info(dict(REVIEW, replan=None))["seconds"] == 41.5
    assert sc.review_info(None)["verdict"] is None


def test_reviewed_run_scores_the_reviewed_plan():
    raw = _plan([0.0, 100.0, 210.0, 300.0], ["One", "Two", "Nope", "Four"], review=True)
    fixed = dict(_plan([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"]), review=REVIEW)
    s_raw = sc.score_run(CASE, "blind", raw)
    s_rev = sc.score_run(CASE, "blind", fixed)
    rev = sc.reviewed_run(s_rev, fixed["review"], ok=True, seconds=44.0)
    assert s_raw["had_problem"] and not rev["had_problem"]
    assert set(sc.REVIEWED_FIELDS) <= set(rev) and rev["review"]["verdict"] == "edited"
    assert rev["metrics"]["title_acc"] == 1.0 and rev["needs_review"] is False


def test_summary_owner_counts():
    clean = _plan([0.0, 100.0, 200.0, 300.0], ["One", "Two", "Three", "Four"])
    wrong = _plan([0.0, 150.0], ["One", "X"])
    runs = []
    for plan, review in ((clean, False), (clean, False), (wrong, False), (wrong, True), (clean, True)):
        r = sc.score_run(CASE, "blind", dict(plan, needs_review=review))
        r.update(ok=True, wall_seconds=1.0)
        runs.append((CASE, r))
    failed = sc.score_run(CASE, "blind", None)
    failed.update(ok=False, wall_seconds=0.0)
    runs.append((CASE, failed))
    s = sc.summarize(runs)
    assert (s["auto_ok"], s["auto_wrong"], s["held_wrong"], s["held_ok"]) == (2, 1, 1, 1)
    assert s["review_confusion"]["failed"] == 1
    assert "review" in s["timings"]
