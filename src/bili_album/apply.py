"""Turn a ``plan.json`` into tagged .m4a files, then verify them.

Works from the plan alone (plus the files it points to), so a reviewer can
hand-edit titles, artists or boundaries and re-apply.
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .hints import fmt_ts
from .media import audio_info, image_kind
from .packetcut import Cut, PacketCutError, RangeError, cut_tracks
from .tag import OPTIONAL, REQUIRED, read_tags, write_tags

DUR_TOL = 0.1
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class PlanError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


class CutError(PlanError):
    """The source or ffmpeg failed while cutting (``cut failed: …``); editing the plan does not help."""


def sanitize(name: str, max_len: int = 120) -> str:
    """Safe file/folder name; keeps CJK and other letters, replaces path-hostile characters."""
    s = unicodedata.normalize("NFC", name or "").strip()
    s = _ILLEGAL.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "_"
    if s.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", s):
        s = "_" + s
    enc = s.encode("utf-8")
    if len(enc) > max_len * 3:
        s = enc[: max_len * 3].decode("utf-8", "ignore").rstrip()
    return s


def output_paths(plan: dict, outdir: Path) -> tuple[Path | None, list[Path]]:
    tracks = plan["tracks"]
    if plan["kind"] == "single":
        t = tracks[0]
        return None, [outdir / f"{sanitize(t['artist'] or 'Unknown Artist')} - {sanitize(t['title'])}.m4a"]
    album = plan["album"]["title"] or (tracks[0]["album"] if tracks else "") or "Unknown Album"
    folder = outdir / sanitize(album)
    width = max(2, len(str(len(tracks))))
    multi_disc = (plan["album"].get("disc_total") or 1) > 1
    out = []
    for i, t in enumerate(tracks, 1):
        prefix = f"{t.get('disc_number', 1)}-" if multi_disc else ""
        out.append(folder / f"{prefix}{i:0{width}d}-{sanitize(t['title'])}.m4a")
    return folder, out


def track_source(plan: dict, t: dict) -> Path:
    return Path(t.get("audio_path") or plan["source"]["audio_path"])


def validate(plan: dict) -> list[str]:
    problems: list[str] = []
    if plan.get("version") != 1:
        problems.append(f"unsupported plan version {plan.get('version')!r}")
    tracks = plan.get("tracks") or []
    if not tracks:
        problems.append("plan has no tracks")
        return problems
    durations: dict[str, float] = {}
    for t in tracks:
        src = track_source(plan, t)
        if not src.exists():
            problems.append(f"track {t['index']}: source audio {src} not found")
            continue
        if str(src) not in durations:
            durations[str(src)] = audio_info(src)["duration"]
        D = durations[str(src)]
        if not (t["end"] > t["start"]):
            problems.append(f"track {t['index']} {t['title']!r}: duration is not positive "
                            f"({t['start']} -> {t['end']})")
        if t["start"] < 0 or t["end"] > D + 0.05:
            problems.append(f"track {t['index']} {t['title']!r}: range {t['start']}-{t['end']} outside the "
                            f"source (0-{D:.3f})")
    shared = [t for t in tracks if not t.get("audio_path")]
    for a, b in zip(shared, shared[1:]):
        if b["start"] <= a["start"]:
            problems.append(f"track {b['index']}: start {b['start']} does not increase after track {a['index']}")
        if b["start"] < a["end"] - 0.001:
            problems.append(f"tracks {a['index']} and {b['index']} overlap ({a['end']} > {b['start']})")
    missing: dict[str, list[str]] = {}
    for t in tracks:
        for key in ("title", "artist", "album", "album_artist"):
            if not (t.get(key) or "").strip():
                missing.setdefault(key, []).append(str(t["index"]))
    for key, idx in missing.items():
        problems.append(f"missing {key} for track(s) {', '.join(idx)}")
    return problems


@dataclass
class ApplyResult:
    folder: Path | None
    files: list[Path]
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    empty_optional: dict[str, list[int]] = field(default_factory=dict)
    zip_path: Path | None = None


def apply_plan(plan_path: Path, outdir: Path, *, force: bool = False, make_zip: bool = False) -> ApplyResult:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    problems = validate(plan)
    if problems:
        raise PlanError(problems)
    folder, files = output_paths(plan, outdir)
    if len(set(files)) != len(files):
        raise PlanError(["two tracks map to the same file name: " +
                         ", ".join(str(f.name) for f in files if files.count(f) > 1)])
    existing = [f for f in files if f.exists()]
    if existing and not force:
        raise PlanError([f"output exists (use --force to overwrite): {f}" for f in existing])
    zip_path = None
    if make_zip:
        if folder is None:
            raise PlanError(["--zip packages album folders; a single has no folder"])
        zip_path = folder.parent / f"{folder.name}.zip"
        if zip_path.exists():
            raise PlanError([f"zip exists, not overwriting: {zip_path}"])
    res = ApplyResult(folder, files, zip_path=zip_path)
    tracks = plan["tracks"]
    alb = plan["album"]
    track_total = alb.get("track_total") or len(tracks)
    disc_total = alb.get("disc_total") or 1
    url = plan["source"].get("url") or ""
    # One packet-aligned pass per source file (see packetcut); tracks keep their plan order.
    by_source: dict[Path, list[int]] = {}
    for i, t in enumerate(tracks):
        by_source.setdefault(track_source(plan, t), []).append(i)
    # Every source is cut into hidden staging files first; only when all succeed do they replace
    # the outputs, so a failure leaves no new file and (with --force) keeps the old ones.
    staged = [f.with_name("." + f.stem + ".cut.m4a") for f in files]
    cuts: dict[int, Cut] = {}
    try:
        for src, idx in by_source.items():
            done = cut_tracks(src, [(float(tracks[i]["start"]), float(tracks[i]["end"])) for i in idx],
                              [staged[i] for i in idx], overwrite=True)
            cuts.update(zip(idx, done))
    except BaseException as e:
        for s in staged:
            s.unlink(missing_ok=True)
        if isinstance(e, RangeError):
            raise PlanError([str(e)]) from e
        if isinstance(e, PacketCutError):
            raise CutError([f"cut failed: {e}"]) from e
        raise
    for s, f in zip(staged, files):
        s.replace(f)
    for t, dst in zip(tracks, files):
        span = f"{fmt_ts(t['start'])}-{fmt_ts(t['end'])}"
        comment = f"{url} {span}".strip() if url else f"{plan['source'].get('title', '')} {span}".strip()
        if t.get("audio_path") and plan["source"].get("parts", 1) > 1:
            comment = f"{url}?p={t['index']}" if url else comment
        cover = Path(t.get("cover_path") or alb.get("cover_path") or "")
        write_tags(dst, {
            "title": t["title"], "artist": t["artist"], "album": t["album"] or alb["title"],
            "album_artist": t["album_artist"] or alb["artist"], "year": t.get("year") or alb.get("year"),
            "genre": t.get("genre") or alb.get("genre"), "composer": t.get("composer"),
            "lyrics": t.get("lyrics"), "comment": comment,
            "track": t.get("track_number") or t["index"], "track_total": track_total,
            "disc": t.get("disc_number") or 1, "disc_total": disc_total,
        }, cover if cover and cover.is_file() else None)
    # Folder cover next to the files (Jellyfin reads cover.jpg / cover.png).
    if folder is not None:
        cov = Path(alb.get("cover_path") or "")
        if cov.is_file():
            kind = image_kind(cov) or "jpg"
            if not any((folder / f"cover.{k}").exists() for k in ("jpg", "png")):
                shutil.copyfile(cov, folder / f"cover.{kind}")
    # Verify.
    produced = [f for f in files if f.exists()]
    if len(produced) != len(tracks):
        res.warnings.append(f"{len(tracks)} planned tracks but {len(produced)} files")
    for i, (t, f) in enumerate(zip(tracks, files)):
        tags = read_tags(f)
        info = audio_info(f)
        planned = float(t["end"]) - float(t["start"])
        c = cuts[i]
        row = {"index": t["index"], "title": t["title"], "start": t["start"], "end": t["end"],
               "planned": round(planned, 3), "actual": round(info["duration"], 3), "file": str(f),
               "cut_start": round(c.start, 3), "cut_end": round(c.end, 3), "cut": c.mode}
        res.rows.append(row)
        # Packet cuts move each boundary by up to a packet (about 93 ms for ALAC): compare with what was cut.
        if abs(info["duration"] - (c.end - c.start)) > DUR_TOL:
            res.warnings.append(f"track {t['index']}: duration {info['duration']:.3f}s vs cut "
                                f"{c.end - c.start:.3f}s (planned {planned:.3f}s)")
        if info["start_time"] < 0:
            res.warnings.append(f"track {t['index']}: negative start time {info['start_time']}")
        for key in REQUIRED:
            if not tags.get(key):
                res.warnings.append(f"track {t['index']}: tag {key} missing after writing")
        for key in OPTIONAL:
            if not tags.get(key):
                res.empty_optional.setdefault(key, []).append(t["index"])
    if zip_path is not None and folder is not None:
        with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_STORED) as z:
            for p in sorted(folder.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    z.write(p, f"{folder.name}/{p.name}")
    return res


def inspect_paths(target: Path) -> list[dict]:
    files = sorted(target.rglob("*.m4a")) if target.is_dir() else [target]
    rows = []
    for f in files:
        if f.name.startswith("."):
            continue
        tags = read_tags(f)
        info = audio_info(f)
        tags["file"] = str(f.relative_to(target) if target.is_dir() else f.name)
        tags["start_time"] = info["start_time"]
        tags["lyrics"] = f"{len(tags['lyrics'])} chars" if tags["lyrics"] else ""
        rows.append(tags)
    return rows
