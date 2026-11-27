# bili-album

Turn a Bilibili music upload into tagged `.m4a` files, ready for a Jellyfin Music library:

- a **single song** video → one file;
- a **compilation** (a whole album in one video) → one file per song, split where the songs really change;
- a **multi-part (分P)** video → one file per part.

Audio is never re-encoded: Bilibili serves AAC, and the tool cuts it with `ffmpeg -c copy`.

```sh
uv run bili-album run https://www.bilibili.com/video/BV14PVVzNE2R -o ~/Music/incoming
uv run bili-album inspect ~/Music/incoming/范特西
```

## How it decides

1. **Source:** page metadata from Bilibili's public API, audio via yt-dlp (anonymous; `--cookies` optional).
2. **Text hints:** chapters, timestamps in the description or comments (uploader's pinned comment first), part names.
3. **Catalog:** album/artist guessed from the title (`《》`, `|`, years, noise words such as 无损 / Hi-Res / 全专 stripped), looked up on NetEase Cloud Music and iTunes. The album whose track count and total length fit the video best wins.
4. **Audio:** one low-rate decode gives silence gaps, level dips and a timbral-novelty curve as boundary candidates. Catalog track lengths are aligned to them with dynamic programming, allowing an intro/outro offset, drift, and missing or extra tracks.
5. **Recognition:** Shazam (via `shazamio`) checks titles, pins track starts through its match offset, or scans mixed playlists that no album explains.
6. Everything lands in `plan.json` with per-track confidence and evidence. When the sources disagree, `needs_review` is set with concrete reasons. Edit the plan, or re-plan with `--album-hint` / `--tracklist`, then `apply`.

## Commands

```
bili-album plan <url> [--workdir DIR] [--no-text-hints] [--cookies FILE] [--album-hint "ARTIST - ALBUM"] [--tracklist FILE]
bili-album plan --audio FILE --title TEXT [--description TEXT] [...]
bili-album apply <plan.json> -o OUTDIR [--force] [--zip]
bili-album run <url> -o OUTDIR [plan flags] [--force] [--zip]
bili-album inspect <file-or-dir> [--json]
```

Requirements: Python 3.12, `uv`, `ffmpeg`/`ffprobe` on PATH. See `AGENTS.md` for the review workflow and `docs/reference/` for the owner's original manual procedure.

## Evaluation

`eval/cases.yaml` holds 15 ground-truth cases (single, multi-part and compilation videos with true track starts, titles and artists). `eval/run_eval.py` runs the tool on each case in three modes, one at a time and each in a cold work directory under `.work/eval/`:

- `hints`: the real workflow (`plan`, `apply`, `inspect --json`);
- `blind`: `plan --no-text-hints` (multi-part videos are concatenated losslessly first and passed with `--audio`/`--title`, so the part structure does not give the split away);
- `blind_norecog`: `blind` plus `--no-recognize`.

```sh
uv run python eval/run_eval.py                       # all cases and modes; skips runs that already have a result
uv run python eval/run_eval.py --case ID --mode MODE --force
uv run python eval/run_eval.py --score-only          # rescore stored runs, rewrite eval/results/results.json
uv run --with pyyaml python eval/validate_cases.py [--live]
```

Scoring (`eval/scoring.py`, tested in `tests/test_eval_scoring.py`) compares kind, track count, boundaries (one-to-one matching within 15 s; hit rate at 1/3/5 s, precision/recall/F1 at 3 s), titles and artists, album fields, output tags and stream copy, and whether `needs_review` was raised on runs that had a problem. A full run takes a long time because Shazam is rate-limited; results land in `eval/results/` (`results.json`, plus raw run records and plans per mode).
