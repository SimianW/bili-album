#!/usr/bin/env python3
"""Evaluation harness for bili-album: run every case in three modes, score, write results.json.

    uv run python eval/run_eval.py                      # all cases, all modes, resumable
    uv run python eval/run_eval.py --case ID --mode MODE [--force]
    uv run python eval/run_eval.py --score-only         # rescore from eval/results/{runs,plans}
    uv run python eval/run_eval.py --score-only --results-dir eval/results/baseline   # rescore the archived baseline

Modes (each with its own cold work directory ``.work/eval/<mode>/<case-id>/``):

hints          plan <url>, apply, inspect --json (the real workflow)
blind          plan --no-text-hints (multipart: parts concatenated by the harness, passed with --audio/--title)
blind_norecog  blind + --no-recognize

Raw records of each run (commands, exit codes, wall times, stderr tails, probes, inspect output) are kept in
``eval/results/runs/<mode>/<id>.json`` and the plan (lyrics blanked) in ``eval/results/plans/<mode>/<id>.json``,
so ``--score-only`` needs nothing from ``.work/``. Scoring lives in ``eval/scoring.py`` (pure).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import scoring  # noqa: E402

CASES_FILE = HERE / "cases.yaml"
RESULTS = HERE / "results"
WORK = REPO / ".work" / "eval"
MODES = ("hints", "blind", "blind_norecog")
SEEN_IN_DEV = {"user-520am-derivative", "jaychou-fantasy", "weeknd-starboy-gapless", "wego6-sixhearts",
               "zhangjie-yaode"}
TIMEOUT = 45 * 60
RETRY_412 = 3
PAUSE_412 = 25
PAUSE_BETWEEN = 5
CONCAT_TOL = 0.2
TRUTH_ALIGN_TOL = 1.5


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cases() -> list[dict]:
    return yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def rel(p: Path | str) -> str:
    p = Path(p)
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


# ----------------------------------------------------------------------------- subprocess

def invoke(cmd: list[str], timeout: int = TIMEOUT) -> dict:
    """Run one command from the repo root in its own process group; a timeout kills the whole group."""
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
    wall = time.monotonic() - t0
    return {"cmd": [rel(c) if str(c).startswith(str(REPO)) else str(c) for c in cmd],
            "exit_code": None if timed_out else proc.returncode, "timed_out": timed_out,
            "wall_seconds": round(wall, 2), "stdout": out or "", "stderr_tail": (err or "")[-3000:]}


def invoke_plan(cmd: list[str]) -> list[dict]:
    """Plan with a 25 s pause and retry when Bilibili answers HTTP 412."""
    attempts = []
    for i in range(RETRY_412 + 1):
        r = invoke(cmd)
        attempts.append(r)
        if r["exit_code"] == 0 or r["timed_out"] or "412" not in r["stderr_tail"] or i == RETRY_412:
            break
        log(f"  HTTP 412 from Bilibili; pausing {PAUSE_412}s before retry {i + 1}")
        time.sleep(PAUSE_412)
    return attempts


def probe(path: Path | str) -> dict:
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                                   "-show_streams", str(path)], check=True, capture_output=True, text=True).stdout)
    st = next(s for s in j["streams"] if s.get("codec_type") == "audio")
    fmt = j.get("format", {})
    dur = float(st.get("duration") or fmt.get("duration") or 0)
    br = st.get("bit_rate") or fmt.get("bit_rate")
    return {"codec": st.get("codec_name"), "profile": st.get("profile"), "sample_rate": int(st.get("sample_rate") or 0),
            "channels": int(st.get("channels") or 0), "bitrate_kbps": round(int(br) / 1000, 1) if br else None,
            "duration": round(dur, 3)}


# ----------------------------------------------------------------------------- synthetic multipart source

def _retry_412(fn, what: str):
    for i in range(RETRY_412 + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if "412" not in str(e) or i == RETRY_412:
                raise
            log(f"  HTTP 412 during {what}; pausing {PAUSE_412}s")
            time.sleep(PAUSE_412)


def prepare_concat(case: dict) -> dict:
    """Download (or reuse) the parts of a multipart case and stream-copy them into one .m4a."""
    d = WORK / "sources" / case["id"]
    meta_path = d / "meta.json"
    meta = read_json(meta_path)
    if meta and meta.get("ok") and Path(meta["concat"]).exists():
        return meta
    d.mkdir(parents=True, exist_ok=True)
    from bili_album.bilibili import Bilibili  # the tool's own downloader: same audio streams as the tool gets
    from bili_album.net import Http

    t0 = time.monotonic()
    bili = Bilibili(Http(d / "cache"))
    vi = _retry_412(lambda: bili.info(case["url"], with_comments=False), "page fetch")
    hints_dir = WORK / "hints" / case["id"]
    parts = []
    for p in vi.parts:
        dst = d / f"part{p.page:03d}.m4a"
        src = hints_dir / f"part{p.page:03d}.m4a"
        if not dst.exists() and src.exists() and src.stat().st_size > 0:
            shutil.copyfile(src, dst)
        if not dst.exists():
            _retry_412(lambda p=p: bili.download_audio(vi, d, page=p.page), f"download p{p.page}")
            time.sleep(1)
        parts.append(dst)
    listing = d / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    concat = d / "concat.m4a"
    tmp = d / "concat.tmp.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1", "-movflags", "+faststart", str(tmp)],
                   check=True, cwd=d)
    tmp.replace(concat)
    part_probes = [probe(p) for p in parts]
    cprobe = probe(concat)
    total = sum(pp["duration"] for pp in part_probes)
    cum, offs = 0.0, []
    for pp in part_probes:
        offs.append(round(cum, 3))
        cum += pp["duration"]
    truth_starts = [float(t["start"]) for t in case["tracks"]]
    align = [round(o - t, 3) for o, t in zip(offs, truth_starts)]
    same_codec = all(pp["codec"] == cprobe["codec"] and pp["sample_rate"] == cprobe["sample_rate"]
                     and pp["channels"] == cprobe["channels"] for pp in part_probes)
    checks = {
        "parts": len(parts), "truth_tracks": len(case["tracks"]),
        "sum_part_durations": round(total, 3), "concat_duration": cprobe["duration"],
        "duration_diff": round(cprobe["duration"] - total, 3),
        "duration_ok": abs(cprobe["duration"] - total) <= CONCAT_TOL,
        "codec_ok": same_codec,
        "part_offsets": offs, "truth_minus_offset": [round(-a, 3) for a in align],
        "truth_alignment_max_abs": round(max(abs(a) for a in align), 3) if align else None,
        "truth_alignment_ok": len(parts) == len(case["tracks"]) and all(abs(a) <= TRUTH_ALIGN_TOL for a in align),
    }
    meta = {"ok": checks["duration_ok"] and checks["codec_ok"], "title": vi.title, "bvid": vi.bvid,
            "concat": str(concat), "part_probes": part_probes, "concat_probe": cprobe, "checks": checks,
            "page_durations": [p.duration for p in vi.parts], "seconds": round(time.monotonic() - t0, 1)}
    write_json(meta_path, meta)
    return meta


# ----------------------------------------------------------------------------- one run

def run_one(case: dict, mode: str, force: bool = False) -> bool:
    cid = case["id"]
    raw_path = RESULTS / "runs" / mode / f"{cid}.json"
    if raw_path.exists() and not force:
        log(f"skip {cid} / {mode} (result exists)")
        return False
    workdir = WORK / mode / cid
    outdir = WORK / "out" / cid
    if force and workdir.exists():
        shutil.rmtree(workdir)
    (RESULTS / "plans" / mode / f"{cid}.json").unlink(missing_ok=True)
    cache = "resumed" if workdir.exists() and any(workdir.iterdir()) else "cold"
    workdir.mkdir(parents=True, exist_ok=True)
    rec: dict = {"case_id": cid, "mode": mode, "started_at": now_iso(), "head_commit": git("rev-parse", "HEAD"),
                 "cache": cache, "ok": False, "error": None, "exit_code": None, "wall_seconds": 0.0,
                 "plan_wall_seconds": None, "invocations": [], "source_prep": None, "part_offsets": None,
                 "source_probe": None, "output_files": None}
    log(f"run {cid} / {mode} ({cache})")
    try:
        _run_one(case, mode, workdir, outdir, rec)
    except Exception as e:  # noqa: BLE001 - a harness-side failure is a result too
        rec["ok"] = False
        rec["error"] = rec["error"] or f"harness: {type(e).__name__}: {e}"
    rec["wall_seconds"] = round(sum(i["wall_seconds"] for i in rec["invocations"]), 2)
    rec["finished_at"] = now_iso()
    for inv in rec["invocations"]:
        inv.pop("stdout_full", None)
    write_json(raw_path, rec)
    log(f"  -> ok={rec['ok']} wall={rec['wall_seconds']}s error={rec['error']}")
    return True


def _record(rec: dict, step: str, inv: dict) -> dict:
    inv = dict(inv, step=step)
    inv["stdout_tail"] = inv.pop("stdout")[-1500:]
    rec["invocations"].append(inv)
    return inv


def _run_one(case: dict, mode: str, workdir: Path, outdir: Path, rec: dict) -> None:
    cid = case["id"]
    flags = ["--workdir", rel(workdir)]
    if mode != "hints":
        flags.append("--no-text-hints")
    if mode == "blind_norecog":
        flags.append("--no-recognize")
    if case["kind"] == "multipart" and mode != "hints":
        meta = prepare_concat(case)
        rec["source_prep"] = {"title": meta["title"], "concat": rel(meta["concat"]), "checks": meta["checks"],
                              "seconds": meta["seconds"]}
        if not meta["ok"]:
            rec["error"] = f"synthetic source failed checks: {meta['checks']}"
            return
        cmd = ["uv", "run", "bili-album", "plan", "--audio", rel(meta["concat"]), "--title", meta["title"], *flags]
    else:
        cmd = ["uv", "run", "bili-album", "plan", case["url"], *flags]
    attempts = invoke_plan(cmd)
    for i, a in enumerate(attempts):
        _record(rec, "plan" if i == len(attempts) - 1 else f"plan (412 attempt {i + 1})", a)
    last = attempts[-1]
    rec["exit_code"] = last["exit_code"]
    rec["plan_wall_seconds"] = last["wall_seconds"]
    plan_path = workdir / "plan.json"
    if last["timed_out"]:
        rec["error"] = f"plan timed out after {TIMEOUT}s"
        return
    if last["exit_code"] != 0 or not plan_path.exists():
        rec["error"] = f"plan exited {last['exit_code']}: {last['stderr_tail'].strip().splitlines()[-1:]}"
        return
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan["source"].get("part_paths"):
        offs, cum = {}, 0.0
        for p in plan["source"]["part_paths"]:
            offs[p] = round(cum, 3)
            cum += probe(p)["duration"]
        rec["part_offsets"] = offs
    store_plan(plan, mode, cid)
    if mode != "hints":
        rec["ok"] = True
        return
    # hints: apply + inspect
    if outdir.exists():
        shutil.rmtree(outdir)
    inv = _record(rec, "apply", invoke(["uv", "run", "bili-album", "apply", rel(plan_path), "-o", rel(outdir)]))
    if inv["timed_out"] or inv["exit_code"] not in (0, 1):
        rec["error"] = f"apply exited {inv['exit_code']}: {inv['stderr_tail'].strip()[-500:]}"
        rec["exit_code"] = inv["exit_code"]
        return
    if not outdir.exists():
        rec["error"] = "apply produced no output directory"
        return
    inv = invoke(["uv", "run", "bili-album", "inspect", rel(outdir), "--json"])
    rows = json.loads(inv["stdout"]) if inv["exit_code"] == 0 else []
    _record(rec, "inspect", inv)
    if inv["exit_code"] != 0:
        rec["error"] = f"inspect exited {inv['exit_code']}"
        return
    files = sorted(rows, key=lambda r: r["file"])
    tracks = plan["tracks"]
    src_cache: dict[str, dict] = {}

    def src_probe(path: str) -> dict:
        if path not in src_cache:
            src_cache[path] = probe(path)
        return src_cache[path]
    for f, t in zip(files, tracks):
        f["probe"] = probe(outdir / f["file"])
        f["source_probe"] = src_probe(t.get("audio_path") or plan["source"]["audio_path"])
    for f in files[len(tracks):]:
        f["probe"] = probe(outdir / f["file"])
    rec["output_files"] = files
    rec["source_probe"] = src_probe(tracks[0].get("audio_path") or plan["source"]["audio_path"]) if tracks else None
    rec["ok"] = True


def store_plan(plan: dict, mode: str, cid: str) -> None:
    p = json.loads(json.dumps(plan))
    for t in p.get("tracks") or []:
        if "lyrics" in t:
            t["lyrics"] = ""
    write_json(RESULTS / "plans" / mode / f"{cid}.json", p)


# ----------------------------------------------------------------------------- results

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).stdout.strip()


def measured_offsets(case: dict) -> list[float] | None:
    """Measured start of each part on the concatenated time line (multipart only), from stored run records."""
    if case["kind"] != "multipart":
        return None
    for mode in ("blind", "blind_norecog"):
        rec = read_json(RESULTS / "runs" / mode / f"{case['id']}.json") or {}
        offs = ((rec.get("source_prep") or {}).get("checks") or {}).get("part_offsets")
        if offs:
            return [float(o) for o in offs]
    rec = read_json(RESULTS / "runs" / "hints" / f"{case['id']}.json") or {}
    if rec.get("part_offsets"):
        return sorted(float(o) for o in rec["part_offsets"].values())
    return None


def build_results(cases: list[dict]) -> dict:
    out_cases = []
    per_mode: dict[str, list] = {m: [] for m in MODES}
    held: dict[str, list] = {m: [] for m in MODES}
    seen: dict[str, list] = {m: [] for m in MODES}
    for c in cases:
        runs = {}
        ce = scoring.with_measured_starts(c, measured_offsets(c))
        for mode in MODES:
            rec = read_json(RESULTS / "runs" / mode / f"{c['id']}.json")
            plan = read_json(RESULTS / "plans" / mode / f"{c['id']}.json")
            if rec is None:
                rec = {"ok": False, "error": "not run", "exit_code": None, "wall_seconds": 0.0, "cache": None,
                       "invocations": []}
                plan = None
            alt = [float(t["start_cases_yaml"]) for t in ce["tracks"]] \
                if (ce is not c and scoring.scores_boundaries(c["kind"], mode)) else None
            sc = scoring.score_run(ce, mode, plan, part_offsets=rec.get("part_offsets"),
                                   output_files=rec.get("output_files"), source_probe=rec.get("source_probe"),
                                   alt_starts=alt)
            run = {"ok": bool(rec.get("ok")), "error": rec.get("error"), "exit_code": rec.get("exit_code"),
                   "wall_seconds": rec.get("wall_seconds"), "cache": rec.get("cache")}
            for k in ("expected_kind", "kind_pred", "kind_ok", "kind_reason", "needs_review", "review_reasons",
                      "had_problem", "timings", "album", "tracks_pred", "boundaries", "extra_boundaries", "trim",
                      "titles", "metrics", "output"):
                run[k] = sc[k]
            run["problems"] = sc["problems"]
            run["boundaries_alt_truth"] = sc["boundaries_alt_truth"]
            dur = float(c["duration"])
            run["speed"] = {"audio_seconds": dur, "plan_wall_seconds": rec.get("plan_wall_seconds"),
                            "realtime_factor": round(dur / rec["wall_seconds"], 2) if rec.get("wall_seconds") else None}
            run["source_prep"] = rec.get("source_prep")
            run["started_at"] = rec.get("started_at")
            run["plan_file"] = f"{rel(RESULTS)}/plans/{mode}/{c['id']}.json" if plan is not None else None
            run["invocations"] = [{k: i.get(k) for k in ("step", "cmd", "exit_code", "timed_out", "wall_seconds",
                                                         "stderr_tail")} for i in rec.get("invocations", [])]
            runs[mode] = run
            if run["error"] != "not run":       # never-run pairs are reported in "not_run", not as failures
                per_mode[mode].append((c, run))
                (seen if c["id"] in SEEN_IN_DEV else held)[mode].append((c, run))
        out_cases.append({
            "id": c["id"], "kind": c["kind"], "category": c["category"], "url": c["url"],
            "duration": float(c["duration"]), "truth_source": c["truth_source"],
            "truth_quality": c["truth_quality"], "seen_in_dev": c["id"] in SEEN_IN_DEV,
            "truth": {"album": c.get("album"), "album_artist": c.get("album_artist"), "year": c.get("year"),
                      "end": c.get("end"),
                      "start_basis": ce.get("start_basis", "cases.yaml"),
                      "tracks": [dict({"n": t["n"], "start": float(t["start"]), "title": t["title"],
                                       "artist": t["artist"]},
                                      **({"start_cases_yaml": float(t["start_cases_yaml"])}
                                         if "start_cases_yaml" in t else {}))
                                 for t in ce["tracks"]]},
            "notes": c.get("notes"),
            "runs": runs,
        })
    src_commit = git("log", "-1", "--format=%H", "--", "src")
    not_run = [f"{c['id']}/{m}" for c in out_cases for m, r in c["runs"].items() if r["error"] == "not run"]
    return {
        "generated_at": now_iso(),
        "partial": bool(not_run),
        "not_run": not_run,
        "tool_commit": src_commit,
        "head_commit": git("rev-parse", "HEAD"),
        "src_dirty": bool(git("status", "--porcelain", "--", "src")),
        "modes": list(MODES),
        "scoring": {"match_window_s": scoring.MATCH_WINDOW, "hit_tolerances_s": list(scoring.HIT_TOLERANCES),
                    "f_tolerance_s": scoring.F_TOL, "problem_tolerance_s": scoring.PROBLEM_TOL,
                    "title_ratio": scoring.TITLE_RATIO,
                    "notes": "Rates are pooled over boundaries / tracks. Failed runs count fully (all boundaries "
                             "missed, all titles wrong); case+mode pairs that were never run (see not_run) are left "
                             "out of the summaries. Multipart truth starts are the measured part offsets (see "
                             "truth.start_basis); stream_copy_ok follows the 5 % bitrate rule, which VBR sources can "
                             "fail on short cuts, so stream_copy_format_ok (codec, rate, channels) is given too. Multipart cases are boundary-scored only in blind modes "
                             "(synthetic concatenation). wall_seconds sums every tool invocation of a run."},
        "cases": out_cases,
        "summary": {m: scoring.summarize(per_mode[m]) for m in MODES},
        "summary_held_out": {m: scoring.summarize(held[m]) for m in MODES},
        "summary_seen_in_dev": {m: scoring.summarize(seen[m]) for m in MODES},
    }


def write_results(cases: list[dict]) -> Path:
    path = RESULTS / "results.json"
    write_json(path, build_results(cases))
    return path


def main() -> int:
    global RESULTS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", action="append", help="case id (repeatable); default all")
    ap.add_argument("--mode", action="append", choices=MODES, help="mode (repeatable); default all")
    ap.add_argument("--force", action="store_true", help="re-run even if a result exists (wipes the work dir)")
    ap.add_argument("--score-only", action="store_true", help="only rescore stored runs and rewrite results.json")
    ap.add_argument("--results-dir", type=Path, default=RESULTS,
                    help="where runs/, plans/ and results.json live (default eval/results; e.g. eval/results/baseline)")
    a = ap.parse_args()
    RESULTS = a.results_dir.resolve()
    cases = load_cases()
    if a.case:
        unknown = set(a.case) - {c["id"] for c in cases}
        if unknown:
            ap.error(f"unknown case id(s): {', '.join(sorted(unknown))}")
    if not a.score_only:
        sel = [c for c in cases if not a.case or c["id"] in a.case]
        modes = [m for m in MODES if not a.mode or m in a.mode]
        t0 = time.monotonic()
        for c in sel:
            for m in modes:
                ran = run_one(c, m, force=a.force)
                if ran:
                    try:
                        write_results(cases)
                    except Exception as e:  # noqa: BLE001 - never stop the run over a scoring bug
                        log(f"  scoring failed (run kept; rescore with --score-only): {type(e).__name__}: {e}")
                    time.sleep(PAUSE_BETWEEN)
        log(f"all requested runs done in {time.monotonic() - t0:.0f}s")
    path = write_results(cases)
    log(f"wrote {rel(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
