"""Command line: plan / apply / run / inspect."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import unicodedata
from pathlib import Path

from .apply import PlanError, apply_plan, inspect_paths
from .hints import fmt_ts
from .plan import PlanOptions, make_plan

REPO_ROOT = Path(__file__).resolve().parents[2]


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def table(rows: list[dict], cols: list[str], max_w: int = 40) -> str:
    def cell(v) -> str:
        s = str(v if v is not None else "")
        s = s.replace("\n", " ")
        out, w = "", 0
        for ch in s:
            cw = _width(ch)
            if w + cw > max_w:
                return out + "…"
            out += ch
            w += cw
        return out
    data = [[cell(r.get(c, "")) for c in cols] for r in rows]
    widths = [max([_width(c)] + [_width(d[i]) for d in data]) for i, c in enumerate(cols)]

    def line(vals):
        return "  ".join(v + " " * (widths[i] - _width(v)) for i, v in enumerate(vals)).rstrip()
    return "\n".join([line(cols), line(["-" * w for w in widths])] + [line(d) for d in data])


def _add_plan_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workdir", type=Path, help="work directory (default: .work/<bvid>/ in the repo)")
    p.add_argument("--no-text-hints", action="store_true",
                   help="ignore uploader-written text (chapters, description, comments, part names, tags)")
    p.add_argument("--cookies", type=Path, help="Netscape cookie file passed to yt-dlp (optional)")
    p.add_argument("--album-hint", help='override the album guess: "ARTIST - ALBUM"')
    p.add_argument("--tracklist", type=Path,
                   help="file with one 'title', 'mm:ss title' or 'title - artist' per line")
    p.add_argument("--audio", type=Path, help="offline mode: use this audio file instead of Bilibili")
    p.add_argument("--title", help="offline mode: the video title")
    p.add_argument("--description", default="", help="offline mode: the video description")
    p.add_argument("--no-recognize", action="store_true", help="do not call the recognition service")


def _plan_opts(a) -> PlanOptions:
    return PlanOptions(workdir=a.workdir, text_hints=not a.no_text_hints, cookies=a.cookies,
                       album_hint=a.album_hint, tracklist=a.tracklist, audio=a.audio, title=a.title,
                       description=a.description, recognize=not a.no_recognize, repo_root=REPO_ROOT)


def _print_plan_summary(path: Path) -> None:
    plan = json.loads(path.read_text(encoding="utf-8"))
    al = plan["album"]
    print(f"kind: {plan['kind']} ({plan['kind_reason']})", file=sys.stderr)
    print(f"album: {al['artist']} - {al['title']} [{al['year']}] {al['catalog'].get('provider', '')}",
          file=sys.stderr)
    rows = [{"#": t["index"], "start": fmt_ts(t["start"]), "end": fmt_ts(t["end"]), "title": t["title"],
             "artist": t["artist"], "conf": t["confidence"]} for t in plan["tracks"]]
    print(table(rows, ["#", "start", "end", "title", "artist", "conf"]), file=sys.stderr)
    if plan["needs_review"]:
        print("needs_review:", file=sys.stderr)
        for r in plan["review_reasons"]:
            print(f"  - {r}", file=sys.stderr)
    print("timings: " + ", ".join(f"{k} {v}s" for k, v in plan["timings"].items()), file=sys.stderr)


def _print_apply(res) -> None:
    rows = [{"#": r["index"], "title": r["title"], "start": fmt_ts(r["start"]), "end": fmt_ts(r["end"]),
             "planned": f"{r['planned']:.2f}", "actual": f"{r['actual']:.2f}", "file": Path(r["file"]).name}
            for r in res.rows]
    print(table(rows, ["#", "title", "start", "end", "planned", "actual", "file"]))
    if res.folder:
        print(f"album folder: {res.folder}")
    if res.zip_path:
        print(f"zip: {res.zip_path}")
    if res.empty_optional:
        print("optional tags left empty: " + "; ".join(
            f"{k} (tracks {', '.join(map(str, v))})" for k, v in res.empty_optional.items()))
    for w in res.warnings:
        print(f"WARNING: {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bili-album", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="analyse a link (or --audio file) and write plan.json")
    p.add_argument("url", nargs="?")
    _add_plan_flags(p)

    p = sub.add_parser("apply", help="cut and tag from a plan.json")
    p.add_argument("plan", type=Path)
    p.add_argument("-o", "--outdir", type=Path, required=True)
    p.add_argument("--force", action="store_true", help="overwrite existing output files")
    p.add_argument("--zip", action="store_true", help="also write <Album>.zip next to the album folder")

    p = sub.add_parser("run", help="plan + apply")
    p.add_argument("url", nargs="?")
    p.add_argument("-o", "--outdir", type=Path, required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--zip", action="store_true")
    _add_plan_flags(p)

    p = sub.add_parser("inspect", help="print tags and durations of .m4a files")
    p.add_argument("target", type=Path)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serve", help="web page: paste a link, the album lands in Jellyfin")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1; never 0.0.0.0)")
    p.add_argument("--port", type=int, default=5191, help="port (default 5191)")
    p.add_argument("--data-dir", type=Path, help="job folders (default: .work/web/ in the repo)")
    p.add_argument("--fake", action="store_true",
                   help="replay scripted jobs instead of running the pipeline (no network, no import)")
    p.add_argument("--fake-speed", type=float, default=1.0, help="--fake: delay divisor (2 = twice as fast)")
    p.add_argument("--fake-hold", action="append", default=[], metavar="STAGE=SECONDS",
                   help="--fake: stay SECONDS longer in STAGE (plan, review, cut, import); repeatable")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    if a.cmd in ("plan", "run"):
        if a.audio and not a.title:
            ap.error("--audio needs --title")
        if not a.audio and not a.url:
            ap.error("give a URL or --audio FILE --title TEXT")
        path = make_plan(a.url, _plan_opts(a))
        _print_plan_summary(path)
        print(path)
        if a.cmd == "plan":
            return 0
        a.plan = path
    if a.cmd in ("apply", "run"):
        try:
            res = apply_plan(a.plan, a.outdir, force=a.force, make_zip=a.zip)
        except PlanError as e:
            print("input error:", file=sys.stderr)
            for pr in e.problems:
                print(f"  - {pr}", file=sys.stderr)
            return 2
        _print_apply(res)
        return 1 if res.warnings else 0
    if a.cmd == "inspect":
        rows = inspect_paths(a.target)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                r["trk"] = f"{r['track']}/{r['track_total']}"
                r["dsk"] = f"{r['disc']}/{r['disc_total']}"
                r["dur"] = f"{r['duration']:.2f}"
            print(table(rows, ["file", "dur", "trk", "dsk", "title", "artist", "album", "album_artist", "year",
                               "genre", "cover", "composer", "lyrics"], max_w=28))
            total = sum(r["duration"] for r in rows)
            print(f"{len(rows)} file(s), total {total:.2f}s")
        return 0
    if a.cmd == "serve":
        return _cmd_serve(a, ap)
    return 1


def _cmd_serve(a, ap) -> int:
    from .web.server import serve

    hold = {}
    for item in a.fake_hold:
        stage, _, secs = item.partition("=")
        try:
            hold[stage] = float(secs)
        except ValueError:
            ap.error(f"--fake-hold expects STAGE=SECONDS, got {item!r}")
    if a.host in ("0.0.0.0", "::", ""):
        ap.error("--host must be a specific address (127.0.0.1 or the Tailscale address), not every interface")
    if not 0 < a.port < 65536:
        ap.error("--port must be between 1 and 65535")
    data_dir = a.data_dir or REPO_ROOT / ".work" / "web"
    return serve(a.host, a.port, data_dir, fake=a.fake, fake_speed=a.fake_speed, fake_hold=hold)


if __name__ == "__main__":
    sys.exit(main())
