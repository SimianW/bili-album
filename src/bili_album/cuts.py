"""Where the cut between two tracks goes once the planner has placed them.

place_cut    a cut before a recording that recognition says starts at a given time: held
             ``CUT_MARGIN`` before it unless only digital silence lies in between
refine_cuts  millisecond refinement of every start that is also the previous track's end, on
             the full-rate signal (:func:`refine.refine_starts`); writes ``refine`` evidence
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .analyze import DIGITAL_DB, Candidate, level_between
from .media import audio_info
from .refine import refine_starts

log = logging.getLogger(__name__)

CUT_MARGIN = 0.3            # a cut stays this far before a recognised recording start


def place_cut(imp: float, cands: list[Candidate], feats, lo: float = 0.0) -> tuple[float, str]:
    """Where to cut before a recording that recognition says starts at ``imp``.

    Rule: a cut may not land after ``imp - CUT_MARGIN``, unless everything between that point
    and the cut is digital silence (then nothing audible is lost and the gap stays with the
    previous track). If ``imp`` falls in a low-level run, the run's own cut (the end of its
    deepest block, see :mod:`analyze`) is proposed first; otherwise ``imp - CUT_MARGIN``.
    Returns (time, explanation for the evidence)."""
    gap = next((c for c in cands if c.kind == "silence" and c.gap_start - 0.3 <= imp <= c.gap_end + 0.3), None)
    limit = imp - CUT_MARGIN
    if gap:
        new = gap.time
        what = ("digital silence" if gap.digital else f"quiet gap (floor {gap.floor_db:.0f} dB, not silence)")
        note = f"recording starts at {imp:.2f}s inside a {what} {gap.gap_start:.2f}-{gap.gap_end:.2f}s"
    else:
        new = limit
        note = f"recording starts at {imp:.2f}s, no silence there"
    if new > limit:
        lvl = level_between(feats, limit, new) if feats is not None else 0.0
        if lvl <= DIGITAL_DB:
            note += f"; cut {new - imp:+.2f}s from it stays in digital silence"
        else:
            new = limit
            note += (f"; cut held {CUT_MARGIN}s before the recognised start (sound up to {lvl:.0f} dB in between, "
                     "so cutting later would clip the intro)")
    else:
        note += f"; cut {new - imp:+.2f}s from it"
    return max(lo, new), note


def refine_cuts(audio: Path, segs: list, options: dict | None = None) -> dict:
    """Refine every start that is also the previous track's end; return the plan's ``refine`` summary.

    ``segs`` are the planner's tracks (``start``, ``end``, ``evidence``, ``recog``, optional
    ``pinned``); the moved starts and ends are written back. Track 1 and starts after unexported
    material keep their place. A start pinned by recognition may still move earlier, but never
    later than the recognised start minus ``CUT_MARGIN`` (or its current place, if that is already
    later: digital silence). ``options`` are keyword arguments for :func:`refine.refine_starts`.
    Any failure (bad audio, a bad option) leaves every boundary unchanged."""
    idx = [i for i in range(1, len(segs)) if abs(segs[i - 1].end - segs[i].start) < 1e-6]
    if not idx:
        return {"boundaries": 0, "moved": 0, "methods": {}, "reason": "no boundary between adjacent tracks"}
    bounds = []
    for i in idx:
        s, hi = segs[i], None
        imp = s.recog.implied_start if getattr(s, "pinned", False) and s.recog is not None else None
        if imp is not None:
            hi = max(s.start, imp - CUT_MARGIN)
        bounds.append((segs[i - 1].start + 1.0, hi))
    try:
        res = refine_starts(audio, [segs[i].start for i in idx], channels=audio_info(audio)["channels"] or 2,
                            bounds=bounds, **(options or {}))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as e:
        log.warning("refinement skipped: %s", e)
        return {"boundaries": len(idx), "moved": 0, "methods": {}, "reason": f"refinement failed: {e}"}
    methods: dict[str, int] = {}
    moved = 0
    for i, (new, method) in zip(idx, res):
        s, prev = segs[i], segs[i - 1]
        methods[method] = methods.get(method, 0) + 1
        delta = new - s.start
        if method == "fallback":
            detail = f"start {s.start:.3f}s kept: no clear edge within the search window"
        elif abs(delta) < 0.0005:
            detail = f"start {s.start:.3f}s confirmed ({method})"
        else:
            detail = f"start {s.start:.3f}s -> {new:.3f}s ({method}, {delta * 1000:+.0f} ms)"
            prev.end = s.start = new
            moved += 1
        s.evidence.append({"kind": "refine", "detail": detail})
    return {"boundaries": len(idx), "moved": moved, "methods": methods,
            "reason": f"{moved} of {len(idx)} boundaries moved"}
