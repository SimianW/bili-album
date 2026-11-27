"""Delete the big files of a finished job, keep its record.

Removed from ``<job>/work/``: ``source.m4a``, ``part*.m4a``, ``features.npz``, ``out/``, ``cache/``.
Kept: ``job.json`` files, plans, covers and other small files. Nothing outside the job's own
folder is touched, and symlinks are never followed (a symlink in the list is removed as a link).
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)

BIG_FILES = ("source.m4a", "features.npz")
BIG_GLOBS = ("part*.m4a",)
BIG_DIRS = ("out", "cache")


def _inside(path: Path, root: Path) -> bool:
    """``path`` (not resolved itself: it may be a symlink) sits directly or deeper inside ``root``."""
    try:
        Path(os.path.realpath(path.parent)).relative_to(root)
        return True
    except ValueError:
        return False


def _tree_size(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for n in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            try:
                st = os.lstat(os.path.join(dirpath, n))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def _remove_tree(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        for n in filenames:
            os.unlink(os.path.join(dirpath, n))
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                os.unlink(full)
            else:
                os.rmdir(full)
    os.rmdir(path)


def targets(job_dir: Path) -> list[Path]:
    """What :func:`clean_job` would remove (only paths that exist)."""
    root = Path(os.path.realpath(job_dir))
    work = Path(job_dir) / "work"
    if work.is_symlink() or not work.is_dir() or not _inside(work, root):
        return []
    out = [work / n for n in BIG_FILES] + [p for g in BIG_GLOBS for p in sorted(work.glob(g))] + \
          [work / d for d in BIG_DIRS]
    return [p for p in out if os.path.lexists(p) and _inside(p, root)]


def clean_job(job_dir: Path) -> int:
    """Remove the big files of one job; returns the bytes freed."""
    freed = 0
    for p in targets(job_dir):
        try:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                os.unlink(p)
            elif stat.S_ISDIR(st.st_mode):
                size = _tree_size(p)
                _remove_tree(p)
                freed += size
            elif stat.S_ISREG(st.st_mode):
                os.unlink(p)
                freed += st.st_size
        except OSError as e:
            log.warning("clean-up of %s stopped: %s", p.name, type(e).__name__)
    return freed
