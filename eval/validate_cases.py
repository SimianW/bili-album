#!/usr/bin/env python3
"""Validate eval/cases.yaml. Offline by default; --live also checks Bilibili.

Run: uv run --with pyyaml python eval/validate_cases.py [--live]
"""
import json
import sys
import time
import urllib.request
from collections import Counter
from http.cookiejar import CookieJar
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.yaml"
KINDS = {"single", "multipart", "compilation"}
SOURCES = {"parts", "chapters", "description", "comment", "single", "manual"}
QUALITIES = {"exact", "good", "approximate"}
MAX_HOURS = 8.5
N_RANGE = (15, 17)
MIX = {"single": (4, 4), "multipart": (3, 3), "compilation": (8, 10)}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

errors: list[str] = []


def err(cid, msg):
    errors.append(f"[{cid}] {msg}")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def mmss(sec):
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}"


def check_schema(c, seen):
    cid = c.get("id", "<no id>")
    req = {"id": str, "url": str, "bvid": str, "kind": str, "category": str, "truth_source": str,
           "truth_quality": str, "reference": str, "notes": str, "tracks": list}
    for k, t in req.items():
        if k not in c:
            err(cid, f"missing key '{k}'")
        elif not isinstance(c[k], t):
            err(cid, f"'{k}' must be {t.__name__}")
    for k in ("album", "album_artist"):
        if k not in c:
            err(cid, f"missing key '{k}'")
        elif c[k] is not None and not isinstance(c[k], str):
            err(cid, f"'{k}' must be string or null")
    if "year" not in c:
        err(cid, "missing key 'year'")
    elif c["year"] is not None and not (isinstance(c["year"], int) and not isinstance(c["year"], bool)):
        err(cid, "'year' must be int or null")
    if not is_num(c.get("duration")):
        err(cid, "'duration' must be a number")
    if "end" in c and not is_num(c["end"]):
        err(cid, "'end' must be a number")
    if isinstance(c.get("id"), str):
        import re
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", c["id"]):
            err(cid, "id must be lowercase ascii and hyphens")
        if c["id"] in seen:
            err(cid, "duplicate id")
        seen.add(c["id"])
    if c.get("kind") not in KINDS:
        err(cid, f"kind must be one of {sorted(KINDS)}")
    if c.get("truth_source") not in SOURCES:
        err(cid, f"truth_source must be one of {sorted(SOURCES)}")
    if c.get("truth_quality") not in QUALITIES:
        err(cid, f"truth_quality must be one of {sorted(QUALITIES)}")
    if isinstance(c.get("bvid"), str) and isinstance(c.get("url"), str) and not c["url"].endswith(c["bvid"]):
        err(cid, "url must end with bvid")
    for i, t in enumerate(c.get("tracks") or [], 1):
        if not isinstance(t, dict):
            err(cid, f"track {i} is not a mapping")
            continue
        for k, ty in (("n", int), ("title", str), ("artist", str)):
            if not isinstance(t.get(k), ty) or isinstance(t.get(k), bool):
                err(cid, f"track {i}: '{k}' must be {ty.__name__}")
        if not is_num(t.get("start")):
            err(cid, f"track {i}: 'start' must be a number")
        if "duration" not in t or (t["duration"] is not None and not is_num(t["duration"])):
            err(cid, f"track {i}: 'duration' must be a number or null")


def check_consistency(c):
    cid = c.get("id", "<no id>")
    tr = c.get("tracks")
    if not isinstance(tr, list) or not tr or not all(isinstance(t, dict) and is_num(t.get("start")) for t in tr):
        return
    kind, dur = c.get("kind"), c.get("duration")
    if [t.get("n") for t in tr] != list(range(1, len(tr) + 1)):
        err(cid, "track numbers must run 1..N")
    starts = [t["start"] for t in tr]
    if any(b <= a for a, b in zip(starts, starts[1:])):
        err(cid, "start values must strictly increase")
    if starts[0] < 0:
        err(cid, "first start is negative")
    if kind in ("single", "multipart") and starts[0] != 0.0:
        err(cid, f"first start must be 0.0 for {kind}")
    if is_num(dur) and starts[-1] >= dur:
        err(cid, "last start must be below duration")
    if "end" in c and is_num(c["end"]) and is_num(dur):
        if not (c["end"] > starts[-1] and c["end"] <= dur):
            err(cid, "end must be above the last start and at most duration")
    if kind == "single" and len(tr) != 1:
        err(cid, "single must have exactly one track")
    if kind in ("multipart", "compilation") and len(tr) < 3:
        err(cid, f"{kind} needs at least 3 tracks")
    if kind == "multipart" and not (5 <= len(tr) <= 14):
        err(cid, "multipart needs 5 to 14 tracks")
    if kind == "single" and is_num(dur) and dur >= 8 * 60:
        err(cid, "single must be under 8 minutes")


def check_mix(cases):
    counts = Counter(c.get("kind") for c in cases)
    if not (N_RANGE[0] <= len(cases) <= N_RANGE[1]):
        errors.append(f"[mix] need {N_RANGE[0]}-{N_RANGE[1]} cases, found {len(cases)}")
    for k, (lo, hi) in MIX.items():
        if not (lo <= counts.get(k, 0) <= hi):
            errors.append(f"[mix] need {lo}-{hi} '{k}' cases, found {counts.get(k, 0)}")
    hours = sum(c["duration"] for c in cases if is_num(c.get("duration"))) / 3600
    if hours > MAX_HOURS:
        errors.append(f"[mix] total {hours:.2f} h exceeds {MAX_HOURS} h")
    long_ok = any(c.get("kind") == "compilation" and is_num(c.get("duration")) and 3600 <= c["duration"] <= 4800
                  for c in cases)
    if not long_ok:
        errors.append("[mix] need a compilation of 60-80 minutes")
    return hours


class Bili:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.op.addheaders = [("User-Agent", UA), ("Referer", "https://www.bilibili.com/")]
        self.op.open("https://www.bilibili.com/", timeout=30).read()

    def view(self, bvid):
        time.sleep(1)
        with self.op.open(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", timeout=30) as r:
            return json.load(r)


def check_live(cases):
    try:
        b = Bili()
    except Exception as e:  # noqa: BLE001
        errors.append(f"[live] cannot reach Bilibili: {e}")
        return
    for c in cases:
        cid, bvid = c.get("id"), c.get("bvid")
        try:
            j = b.view(bvid)
        except Exception as e:  # noqa: BLE001
            err(cid, f"live fetch failed: {e}")
            continue
        d = j.get("data")
        if j.get("code") != 0 or not d:
            err(cid, f"video missing or unavailable (code {j.get('code')})")
            continue
        pages = d.get("pages") or []
        total = sum(p["duration"] for p in pages)
        if abs(total - c["duration"]) > 2:
            err(cid, f"duration {c['duration']} differs from live {total}")
        if c["kind"] in ("single", "compilation") and len(pages) != 1:
            err(cid, f"expected 1 part, live has {len(pages)}")
        if c["kind"] == "multipart":
            if len(pages) != len(c["tracks"]):
                err(cid, f"live has {len(pages)} parts but {len(c['tracks'])} tracks")
            else:
                run = 0
                for t, p in zip(c["tracks"], pages):
                    if abs(t["start"] - run) > 1:
                        err(cid, f"track {t['n']} start {t['start']} vs running part sum {run}")
                    run += p["duration"]


def main():
    live = "--live" in sys.argv[1:]
    data = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("cases"), list):
        sys.exit("FAIL: top level must be {version: 1, cases: [...]}")
    cases = data["cases"]
    seen: set = set()
    for c in cases:
        if not isinstance(c, dict):
            errors.append("a case is not a mapping")
            continue
        check_schema(c, seen)
        check_consistency(c)
    hours = check_mix([c for c in cases if isinstance(c, dict)])
    if live and not errors:
        check_live(cases)
    if errors:
        print("FAIL:")
        for e in errors:
            print("  " + e)
        sys.exit(1)
    for c in cases:
        print(f"{c['id']:28s} {c['kind']:12s} {len(c['tracks']):2d} tracks  {mmss(c['duration'])}")
    print(f"OK: {len(cases)} cases, {hours:.2f} hours")


if __name__ == "__main__":
    main()
