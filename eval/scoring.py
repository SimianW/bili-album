"""Pure scoring for the bili-album evaluation (no I/O).

The harness (``run_eval.py``) gathers plans, inspect output and probes; this
module turns one case's truth plus one run's plan into scores, and pools runs
into summaries. Text normalisation is written independently of the tool's own
``textnorm`` so that the judge does not share the tool's blind spots.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from difflib import SequenceMatcher

try:  # opencc-python-reimplemented (a dependency of the project)
    import opencc

    _T2S = opencc.OpenCC("t2s")
except Exception:  # pragma: no cover - fallback keeps the module importable
    _T2S = None

MATCH_WINDOW = 15.0
HIT_TOLERANCES = (1.0, 3.0, 5.0)
F_TOL = 3.0
PROBLEM_TOL = 3.0
TITLE_RATIO = 0.85
TAGS = ("title", "artist", "album", "album_artist", "track", "disc", "year", "genre", "cover", "composer",
        "lyrics")
STAGES = ("fetch", "download", "analyze", "catalog", "recognize", "total")

# ----------------------------------------------------------------------------- text

_VERSION_WORDS = (r"feat|ft\.?|featuring|with|prod|remaster|remastered|version|ver\.?|live|demo|edit|mix|remix|"
                  r"radio|explicit|clean|instrumental|acoustic|mono|stereo|bonus|single|伴奏|版|现场|纯音乐|"
                  r"伴唱|翻唱|cover")
_BRACKETS = r"\(\[（【《<〈「『"
_CLOSE = r"\)\]）】》>〉」』"
_BRACKETED = re.compile(rf"[{_BRACKETS}][^{_CLOSE}]*(?:{_VERSION_WORDS})[^{_CLOSE}]*[{_CLOSE}]", re.IGNORECASE)
_DASH_SUFFIX = re.compile(rf"\s+[-–—]\s+[^-–—]*(?:{_VERSION_WORDS})[^-–—]*$", re.IGNORECASE)
_FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|，|、|/|&|＆|;|；|×|\s+x\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+|"
                           r"\s+with\s+|\s+and\s+)\s*", re.IGNORECASE)


def t2s(s: str) -> str:
    return _T2S.convert(s) if _T2S is not None else s


def normalize(s: str | None) -> str:
    """NFKC, lower-case, Traditional -> Simplified, drop punctuation, symbols and whitespace."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = t2s(s)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] not in "PSZC")


def strip_suffixes(s: str | None) -> str:
    """Remove bracketed version / feature suffixes such as ``(feat. X)``, ``[Remastered]``, `` - Live``."""
    s = unicodedata.normalize("NFKC", s or "")
    prev = None
    while prev != s:
        prev = s
        s = _BRACKETED.sub("", s)
        s = _DASH_SUFFIX.sub("", s)
        s = _FEAT_TAIL.sub("", s)
    return s.strip()


def title_ok(truth: str | None, pred: str | None) -> bool:
    nt, np_ = normalize(truth), normalize(pred)
    if not nt or not np_:
        return False
    if nt == np_:
        return True
    st, sp = normalize(strip_suffixes(truth)), normalize(strip_suffixes(pred))
    if st and sp and (st in sp or sp in st):
        return True
    return SequenceMatcher(None, nt, np_).ratio() >= TITLE_RATIO


def primary_artist(artist: str | None) -> str:
    parts = [p for p in _ARTIST_SPLIT.split(artist or "") if p.strip()]
    return parts[0].strip() if parts else (artist or "").strip()


def artist_ok(truth: str | None, pred: str | None) -> bool:
    prim = normalize(primary_artist(truth))
    return bool(prim) and prim in normalize(pred)


def year_ok(truth: int | None, pred) -> bool:
    m = re.match(r"\s*(\d{4})", str(pred or ""))
    return bool(m) and int(m.group(1)) == int(truth)


# ----------------------------------------------------------------------------- boundaries

def match_boundaries(truth: list[float], pred: list[float], window: float = MATCH_WINDOW
                     ) -> tuple[list[int | None], list[int]]:
    """One-to-one, order-preserving matching of sorted ``truth`` to sorted ``pred`` times.

    Pairs only within ``window`` seconds. Maximises the number of pairs first and
    then minimises the total absolute error among maximal matchings.
    Returns (pred index per truth item or None, unmatched pred indices).
    """
    n, m = len(truth), len(pred)
    inf = (math.inf, math.inf)
    # cost[i][j]: best (-(pairs), error) for truth[i:], pred[j:]
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n and j == m:
                cost[i][j] = (0, 0.0)
                continue
            best, mv = inf, 0
            if i < n:  # truth i missed
                c = cost[i + 1][j]
                if c < best:
                    best, mv = c, 1
            if j < m:  # pred j extra
                c = cost[i][j + 1]
                if c < best:
                    best, mv = c, 2
            if i < n and j < m and abs(pred[j] - truth[i]) <= window:
                c0 = cost[i + 1][j + 1]
                c = (c0[0] - 1, c0[1] + abs(pred[j] - truth[i]))
                if c < best:
                    best, mv = c, 3
            cost[i][j], move[i][j] = best, mv
    assign: list[int | None] = [None] * n
    used = set()
    i = j = 0
    while i < n or j < m:
        mv = move[i][j]
        if mv == 1:
            i += 1
        elif mv == 2:
            j += 1
        else:
            assign[i] = j
            used.add(j)
            i += 1
            j += 1
    return assign, [k for k in range(m) if k not in used]


def percentile(xs: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100); None for an empty list."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


def score_boundaries(truth_tracks: list[dict], pred_tracks: list[dict]) -> dict:
    """Score starts of truth tracks 2..N against predicted starts of tracks 2..M (global seconds)."""
    t_starts = [float(t["start"]) for t in truth_tracks[1:]]
    p_starts = sorted(float(t["start"]) for t in pred_tracks[1:])
    assign, extra = match_boundaries(t_starts, p_starts)
    rows = []
    for k, (t, a) in enumerate(zip(t_starts, assign)):
        p = p_starts[a] if a is not None else None
        rows.append({"truth_n": truth_tracks[k + 1]["n"], "truth_start": _r(t),
                     "pred_start": _r(p), "error": _r(p - t) if p is not None else None})
    return {"boundaries": rows, "extra_boundaries": [_r(p_starts[k]) for k in extra]}


def boundary_counts(rows: list[dict], n_extra: int) -> dict:
    errs = [abs(r["error"]) for r in rows if r["error"] is not None]
    n_truth = len(rows)
    n_pred = len(errs) + n_extra
    hits = {tol: sum(e <= tol for e in errs) for tol in HIT_TOLERANCES}
    return {"n_truth_b": n_truth, "n_pred_b": n_pred, "hits": hits, "tp_3s": hits[F_TOL], "abs_errs": errs}


def rates_from_counts(n_truth: int, n_pred: int, hits: dict, abs_errs: list[float]) -> dict:
    def div(a, b):
        return round(a / b, 4) if b else None
    tp = hits[F_TOL]
    prec, rec = div(tp, n_pred), div(tp, n_truth)
    f1 = round(2 * prec * rec / (prec + rec), 4) if prec and rec else (0.0 if n_truth or n_pred else None)
    return {"hit_1s": div(hits[1.0], n_truth), "hit_3s": div(hits[3.0], n_truth), "hit_5s": div(hits[5.0], n_truth),
            "precision_3s": prec if n_pred else (0.0 if n_truth else None), "recall_3s": rec, "f1_3s": f1,
            "median_abs_err": _r(statistics.median(abs_errs)) if abs_errs else None,
            "p90_abs_err": _r(percentile(abs_errs, 90))}


# ----------------------------------------------------------------------------- titles

def truth_ranges(truth_tracks: list[dict], end: float) -> list[tuple[float, float]]:
    starts = [float(t["start"]) for t in truth_tracks]
    return [(s, starts[i + 1] if i + 1 < len(starts) else end) for i, s in enumerate(starts)]


def score_titles(truth_tracks: list[dict], pred_tracks: list[dict], end: float) -> list[dict]:
    """Per true track: the predicted track overlapping it most, and whether title / artist agree."""
    out = []
    for t, (a, b) in zip(truth_tracks, truth_ranges(truth_tracks, end)):
        best, best_ov = None, 0.0
        for p in pred_tracks:
            ov = max(0.0, min(b, float(p["end"])) - max(a, float(p["start"])))
            if ov > best_ov:
                best, best_ov = p, ov
        length = max(b - a, 1e-9)
        out.append({
            "truth_n": t["n"], "truth_title": t["title"], "pred_title": best["title"] if best else None,
            "truth_artist": t["artist"], "pred_artist": best["artist"] if best else None,
            "title_ok": bool(best) and title_ok(t["title"], best["title"]),
            "artist_ok": bool(best) and artist_ok(t["artist"], best["artist"]),
            "overlap": round(best_ov / length, 4),
            "confidence": best.get("confidence") if best else None,
            "pred_index": best.get("index") if best else None,
        })
    return out


# ----------------------------------------------------------------------------- plan -> global tracks

def global_tracks(plan: dict, part_offsets: dict[str, float] | None = None) -> list[dict]:
    """Predicted tracks with start/end on one time line.

    Multipart plans cut each track from its own part file; ``part_offsets`` maps a
    part's ``audio_path`` to its offset on the concatenated time line.
    """
    out = []
    for t in plan.get("tracks") or []:
        off = 0.0
        if t.get("audio_path") and part_offsets is not None:
            off = float(part_offsets.get(t["audio_path"], 0.0))
        ev_kinds = []
        for e in t.get("evidence") or []:
            if e.get("kind") and e["kind"] not in ev_kinds:
                ev_kinds.append(e["kind"])
        out.append({"index": t.get("index"), "start": _r(float(t["start"]) + off), "end": _r(float(t["end"]) + off),
                    "title": t.get("title", ""), "artist": t.get("artist", ""),
                    "confidence": t.get("confidence"), "evidence_kinds": ev_kinds})
    return out


# ----------------------------------------------------------------------------- output files

def score_output(n_planned: int, planned_durations: list[float], files: list[dict], source: dict | None) -> dict:
    """``files``: one dict per output file in track order with the inspect tags plus
    ``probe`` = {codec, sample_rate, channels, bitrate_kbps, duration}. ``source`` is a probe of the source audio
    (for multipart, the probe of each track's part, given per file as ``source_probe``)."""
    n = len(files)
    comp = {}
    for tag in TAGS:
        if n == 0:
            comp[tag] = 0.0
            continue
        have = 0
        for f in files:
            v = f.get(tag)
            have += bool(v) and v != 0 and v != "0"
        comp[tag] = round(have / n, 4)
    devs = []
    for f, planned in zip(files, planned_durations):
        dur = (f.get("probe") or {}).get("duration")
        if dur is None:
            dur = f.get("duration")
        if dur is not None:
            devs.append(abs(float(dur) - planned))
    copy_ok = n > 0
    copy_detail = []
    for f in files:
        src = f.get("source_probe") or source or {}
        pr = f.get("probe") or {}
        ok = (bool(src) and pr.get("codec") == src.get("codec") and pr.get("sample_rate") == src.get("sample_rate")
              and pr.get("channels") == src.get("channels"))
        sb, pb = src.get("bitrate_kbps") or 0, pr.get("bitrate_kbps") or 0
        br_ok = bool(sb) and abs(pb - sb) <= 0.05 * sb
        if not (ok and br_ok):
            copy_detail.append({"file": f.get("file"), "codec": pr.get("codec"), "sample_rate": pr.get("sample_rate"),
                                "channels": pr.get("channels"), "bitrate_kbps": pb,
                                "source_bitrate_kbps": sb, "format_ok": ok, "bitrate_ok": br_ok})
        copy_ok = copy_ok and ok and br_ok
    return {"files": n, "planned_tracks": n_planned, "files_match_plan": n == n_planned, "tag_completeness": comp,
            "max_duration_dev": _r(max(devs)) if devs else None, "stream_copy_ok": copy_ok,
            "stream_copy_mismatches": copy_detail}


# ----------------------------------------------------------------------------- one run

def expected_kind(case_kind: str, mode: str) -> str:
    return "compilation" if (case_kind == "multipart" and mode != "hints") else case_kind


def scores_boundaries(case_kind: str, mode: str) -> bool:
    return case_kind == "compilation" or (case_kind == "multipart" and mode != "hints")


def score_run(case: dict, mode: str, plan: dict | None, *, part_offsets: dict[str, float] | None = None,
              output_files: list[dict] | None = None, source_probe: dict | None = None) -> dict:
    """Scores for one case+mode. ``plan`` None means the tool produced no plan (crash / timeout)."""
    truth = case["tracks"]
    end = float(case.get("end") or case["duration"])
    exp = expected_kind(case["kind"], mode)
    do_b = scores_boundaries(case["kind"], mode)
    res: dict = {"expected_kind": exp}
    if plan is None:
        rows = [{"truth_n": t["n"], "truth_start": _r(t["start"]), "pred_start": None, "error": None}
                for t in truth[1:]] if do_b else []
        titles = [{"truth_n": t["n"], "truth_title": t["title"], "pred_title": None, "truth_artist": t["artist"],
                   "pred_artist": None, "title_ok": False, "artist_ok": False, "overlap": 0.0, "confidence": None,
                   "pred_index": None} for t in truth]
        res.update({"kind_pred": None, "kind_ok": False, "kind_reason": None, "needs_review": None,
                    "review_reasons": [], "timings": None,
                    "album": _album_scores(case, None), "tracks_pred": [], "boundaries": rows,
                    "extra_boundaries": [], "trim": None, "titles": titles})
        res["metrics"] = _metrics(case, res, do_b, n_pred=0)
        res["had_problem"] = True
        res["problems"] = ["no plan (tool failed)"]
        res["output"] = None
        return res

    tracks_pred = global_tracks(plan, part_offsets)
    kind_pred = plan.get("kind")
    res.update({"kind_pred": kind_pred, "kind_ok": kind_pred == exp, "kind_reason": plan.get("kind_reason"),
                "needs_review": bool(plan.get("needs_review")), "review_reasons": list(plan.get("review_reasons") or []),
                "timings": plan.get("timings"), "album": _album_scores(case, plan), "tracks_pred": tracks_pred})
    if do_b:
        res.update(score_boundaries(truth, tracks_pred))
    else:
        res.update({"boundaries": [], "extra_boundaries": []})
    if tracks_pred and not (kind_pred == "multipart"):
        src_dur = (plan.get("source") or {}).get("duration")
        res["trim"] = {"first_start_error": _r(tracks_pred[0]["start"] - float(truth[0]["start"])),
                       "last_end_error": _r(tracks_pred[-1]["end"] - end),
                       "last_end_error_vs_audio": _r(tracks_pred[-1]["end"] - float(src_dur)) if src_dur else None}
    else:
        res["trim"] = None
    res["titles"] = score_titles(truth, tracks_pred, end)
    res["metrics"] = _metrics(case, res, do_b, n_pred=len(tracks_pred))
    res["problems"] = _problems(res, do_b)
    res["had_problem"] = bool(res["problems"])
    if mode == "hints" and output_files is not None:
        planned = [float(t["end"]) - float(t["start"]) for t in plan.get("tracks") or []]
        res["output"] = score_output(len(planned), planned, output_files, source_probe)
    else:
        res["output"] = None
    return res


def _album_scores(case: dict, plan: dict | None) -> dict:
    al = (plan or {}).get("album") or {}
    cat = al.get("catalog") or {}
    out = {"title": al.get("title"), "artist": al.get("artist"), "year": al.get("year"),
           "provider": cat.get("provider"), "match_reason": al.get("match_reason")}
    out["title_ok"] = None if case.get("album") is None else title_ok(case["album"], al.get("title"))
    out["artist_ok"] = None if case.get("album_artist") is None else artist_ok(case["album_artist"], al.get("artist"))
    out["year_ok"] = None if case.get("year") is None else year_ok(case["year"], al.get("year"))
    return out


def _metrics(case: dict, res: dict, do_b: bool, n_pred: int) -> dict:
    m: dict = {"n_truth": len(case["tracks"]), "n_pred": n_pred, "count_ok": n_pred == len(case["tracks"])}
    if do_b:
        c = boundary_counts(res["boundaries"], len(res["extra_boundaries"]))
        m.update(rates_from_counts(c["n_truth_b"], c["n_pred_b"], c["hits"], c["abs_errs"]))
        m["n_truth_boundaries"], m["n_pred_boundaries"] = c["n_truth_b"], c["n_pred_b"]
    else:
        m.update({k: None for k in ("hit_1s", "hit_3s", "hit_5s", "precision_3s", "recall_3s", "f1_3s",
                                    "median_abs_err", "p90_abs_err")})
        m["n_truth_boundaries"], m["n_pred_boundaries"] = 0, 0
    titles = res["titles"]
    m["title_acc"] = round(sum(t["title_ok"] for t in titles) / len(titles), 4) if titles else None
    m["artist_acc"] = round(sum(t["artist_ok"] for t in titles) / len(titles), 4) if titles else None
    return m


def _problems(res: dict, do_b: bool) -> list[str]:
    p = []
    if not res["kind_ok"]:
        p.append(f"kind {res['kind_pred']} != {res['expected_kind']}")
    if not res["metrics"]["count_ok"]:
        p.append(f"{res['metrics']['n_pred']} tracks vs {res['metrics']['n_truth']} true")
    if do_b:
        bad = [b["truth_n"] for b in res["boundaries"] if b["error"] is None or abs(b["error"]) > PROBLEM_TOL]
        if bad:
            p.append(f"boundaries missed or off > {PROBLEM_TOL:g}s: tracks {bad}")
        if res["extra_boundaries"]:
            p.append(f"{len(res['extra_boundaries'])} extra boundaries")
    wt = [t["truth_n"] for t in res["titles"] if not t["title_ok"]]
    if wt:
        p.append(f"wrong title: tracks {wt}")
    wa = [t["truth_n"] for t in res["titles"] if not t["artist_ok"]]
    if wa:
        p.append(f"wrong artist: tracks {wa}")
    for k in ("title", "artist", "year"):
        if res["album"][f"{k}_ok"] is False:
            p.append(f"album {k} wrong")
    return p


# ----------------------------------------------------------------------------- summary

def summarize(runs: list[tuple[dict, dict]]) -> dict:
    """Pool (case, run) pairs of one mode. Rates are micro-averaged over boundaries / tracks.

    Failed runs count fully: their truth boundaries are missed and their titles wrong.
    """
    n_tb = n_pb = 0
    hits = {tol: 0 for tol in HIT_TOLERANCES}
    errs: list[float] = []
    n_titles = t_ok = a_ok = 0
    album = {"title": [0, 0], "artist": [0, 0], "year": [0, 0]}
    kind_ok = count_ok = ok = 0
    audio = wall = 0.0
    timings = {k: 0.0 for k in STAGES}
    conf = {"review_problem": 0, "review_no_problem": 0, "no_review_problem": 0, "no_review_no_problem": 0,
            "failed": 0}
    trim_first, trim_last = [], []
    speed_audio = speed_wall = 0.0
    for case, run in runs:
        ok += bool(run.get("ok"))
        kind_ok += bool(run.get("kind_ok"))
        count_ok += bool((run.get("metrics") or {}).get("count_ok"))
        if run.get("boundaries") is not None and (run.get("metrics") or {}).get("n_truth_boundaries"):
            c = boundary_counts(run["boundaries"], len(run.get("extra_boundaries") or []))
            n_tb += c["n_truth_b"]
            n_pb += c["n_pred_b"]
            for tol in HIT_TOLERANCES:
                hits[tol] += c["hits"][tol]
            errs += c["abs_errs"]
        for t in run.get("titles") or []:
            n_titles += 1
            t_ok += bool(t["title_ok"])
            a_ok += bool(t["artist_ok"])
        for k in album:
            v = (run.get("album") or {}).get(f"{k}_ok")
            if v is not None:
                album[k][0] += 1
                album[k][1] += bool(v)
        audio += float(case["duration"])
        wall += float(run.get("wall_seconds") or 0.0)
        if run.get("ok"):
            speed_audio += float(case["duration"])
            speed_wall += float(run.get("wall_seconds") or 0.0)
        for k in STAGES:
            timings[k] += float((run.get("timings") or {}).get(k) or 0.0)
        if run.get("needs_review") is None:
            conf["failed"] += 1
        else:
            key = ("review" if run["needs_review"] else "no_review") + ("_problem" if run["had_problem"] else
                                                                       "_no_problem")
            conf[key] += 1
        tr = run.get("trim")
        if tr:
            trim_first.append(abs(tr["first_start_error"]))
            trim_last.append(abs(tr["last_end_error"]))
    n = len(runs)

    def div(a, b):
        return round(a / b, 4) if b else None
    out = {"cases": n, "ok": ok, "kind_acc": div(kind_ok, n), "count_acc": div(count_ok, n), "boundaries": n_tb,
           "pred_boundaries": n_pb}
    out.update(rates_from_counts(n_tb, n_pb, hits, errs))
    out.update({"titles": n_titles, "title_acc": div(t_ok, n_titles), "artist_acc": div(a_ok, n_titles),
                "album_title_acc": div(album["title"][1], album["title"][0]),
                "album_artist_acc": div(album["artist"][1], album["artist"][0]),
                "year_acc": div(album["year"][1], album["year"][0]),
                "audio_seconds": round(audio, 1), "wall_seconds": round(wall, 1),
                "realtime_factor": div(speed_audio, speed_wall),
                "timings": {k: round(v, 1) for k, v in timings.items()},
                "review_confusion": conf,
                "trim_first_median_abs": _r(statistics.median(trim_first)) if trim_first else None,
                "trim_last_median_abs": _r(statistics.median(trim_last)) if trim_last else None})
    return out
