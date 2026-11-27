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
3. **Catalog:** album/artist guessed from the title (`《》`, `|`, years, noise words such as 无损 / Hi-Res / 全专 / 原创 and descriptive phrases such as 在录音棚听 stripped), looked up on NetEase Cloud Music and iTunes (one failing or throttled provider falls back to the other). The album whose track count and total length fit the video best wins; an edition may match through its vocal subset (instrumental or bonus blocks ignored). A single is filed under the artist's own earliest album, not a compilation. Derivative uploads (covers, fan edits, AI remakes) are tagged with the original song and flagged. Titles keep the original script.
4. **Audio:** one low-rate decode gives silence gaps, level dips and a timbral-novelty curve as boundary candidates. A gap is cut at the end of its deepest (digitally silent) part, so soft intros stay with their song. Catalog track lengths are aligned to the candidates with dynamic programming, allowing an intro/outro offset, drift, and missing or extra tracks.
5. **Recognition:** Shazam (via `shazamio`) checks titles, pins track starts through its match offset (never cutting into a recognised intro), or scans mixed playlists that no album explains. A clean alignment is verified by sampling 3 tracks; when recognition contradicts the catalog order, the album is re-labelled in heard order and re-aligned.
6. Everything lands in `plan.json` with per-track confidence and evidence. When the sources disagree, `needs_review` is set with concrete reasons. Edit the plan, or re-plan with `--album-hint` / `--tracklist`, then `apply`.

## Commands

```
bili-album plan <url> [--workdir DIR] [--no-text-hints] [--cookies FILE] [--album-hint "ARTIST - ALBUM"] [--tracklist FILE]
bili-album plan --audio FILE --title TEXT [--description TEXT] [...]
bili-album apply <plan.json> -o OUTDIR [--force] [--zip]
bili-album run <url> -o OUTDIR [plan flags] [--force] [--zip]
bili-album inspect <file-or-dir> [--json]
bili-album review <plan.json> [--out FILE] [--dry-run] [--show-brief] [--no-replan]
bili-album import <album-dir | single.m4a | plan.json> [--replace] [--dry-run] [--timeout S]
bili-album ingest <url> [--workdir DIR] [--on-unsure hold|import] [--no-review] [--no-import] [--replace] [--json]
```

Requirements: Python 3.12, `uv`, `ffmpeg`/`ffprobe` on PATH. See `AGENTS.md` for the review workflow and `docs/reference/` for the owner's original manual procedure.

## Review by a model, import into Jellyfin

`review` sends a compact brief (plan, catalog tracklist and album candidates, recognition results, measured
boundary candidates; the uploader's text only when the plan used text hints) to an OpenAI-compatible
`/chat/completions` endpoint. The model answers with a verdict and edits from a closed set (choose / propose an
album, relabel, reorder, drop or split tracks, move a boundary to a measured candidate, mark a derivative version,
ask for one re-plan). Every edit is checked against the catalog, recognition or uploader text before it is applied;
anything unconfirmed is rejected and keeps `needs_review: true`. The outcome lands in `plan.review` (reasons and
concerns in Simplified Chinese) and the original plan is kept as `plan.unreviewed.json`. If the model is down or
answers nonsense, the plan stays as it was with `review.error` set. Derivative versions always stay flagged.

`import` copies an album into `LIBRARY_DIR/<Album Artist>/<Album>/` (a single into `<Artist>/<Album>/01-<Title>.m4a`)
through a hidden temporary folder renamed into place, never overwrites without `--replace`, then asks Jellyfin to
scan and waits until the album is listed with all tracks; it prints the Jellyfin item id and the album's web link.

**The tool writes Jellyfin metadata, for its own imports only.** Chinese is always Simplified. Jellyfin's
MusicBrainz fetcher can add a Traditional-script duplicate during the scan (周杰倫 next to 周杰伦). After an import
the tool compares Jellyfin's album name, album artists, track names and track artists with the tags it wrote; where
Jellyfin shows the same text in Traditional script (OpenCC `t2s` of Jellyfin's value equals the tag) or adds a
Traditional duplicate of a tagged artist, it updates that item through the API (`POST /Items/{id}`) to the tagged
value and sets `LockData` on it (Jellyfin passes the lock on to the album's tracks). Other scripts and other names
are never touched. Jellyfin 12.2 has no lockable artist field, and without the lock a metadata refresh with "replace
all" brought the Traditional artist back. The correction is checked again after a full library scan; the scan also
drops the Traditional artist entry once nothing points at it. Results are in the import result (`normalized`,
`normalize_locked`, `normalize_verified`, `leftover_artists`, `warnings`); a failed correction is a warning, the import
still counts. Because of the lock, Jellyfin no longer refreshes those items from the internet; to change one later,
untick "Lock this item" in Jellyfin's metadata editor.

Deleting an album folder by hand leaves its record behind in Jellyfin 12.2 (an orphan album that also keeps its
artist entry alive; seen with and without the lock). Delete albums from Jellyfin's own UI, or with
`bili_album.library.remove_album(dest, library_dir=..., jellyfin=..., config=...)`, which removes the folder, deletes
the album's record (`DELETE /Items/{id}`) and starts a scan.

Messages shown to the owner (`message`, `error` in the job state, stage events, `reason_zh` of rejected edits in the
job state) are plain Simplified Chinese; the technical text is kept in `error_detail`, with a stable `error_code`
(see `bili_album/messages.py`).

`ingest` runs plan → review → cut → import; with `--on-unsure hold` (default) it stops before cutting when the
reviewed plan still needs review. The Python API for a web front end is `bili_album.pipeline`
(`ingest`, `confirm`, `discard`, `load_job`; state in `<workdir>/job.json`).

Configuration lives outside the repo in `~/.config/bili-album/env` (`KEY=VALUE`, mode 600; environment variables
of the same name win; `BILI_ALBUM_CONFIG` points elsewhere):

| Key | Meaning |
| --- | --- |
| `BILI_ALBUM_LLM_BASE_URL`, `BILI_ALBUM_LLM_MODEL`, `BILI_ALBUM_LLM_API_KEY` | chat-completions endpoint for `review` |
| `BILI_ALBUM_JELLYFIN_URL`, `BILI_ALBUM_JELLYFIN_API_KEY` | Jellyfin API (auth header `Authorization: MediaBrowser Token="…"`) |
| `BILI_ALBUM_JELLYFIN_PUBLIC_URL` | base of the web links reported after import |
| `BILI_ALBUM_LIBRARY_DIR` | host folder of the Jellyfin music library |
| `BILI_ALBUM_JELLYFIN_LIBRARY_PATH` | the same folder inside the Jellyfin container (default `/media/music-videos/music`) |

Request parameters were chosen against the owner's proxy (2026-11-26): `response_format: json_object`,
`temperature: 0`, `reasoning_effort: medium`, `max_completion_tokens: 6000`, 120 s timeout. A review takes 4–13 s
of model time (about 3 k prompt tokens for a single, 5.5 k for a 10-track album).

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

## Web page

`uv run bili-album serve` serves a one-page front end on `http://127.0.0.1:5191/`: paste a Bilibili link (`bilibili.com/video/BV…`, `b23.tv/…` or a bare `BV…` id) and the job is planned, reviewed by the model, cut and imported into Jellyfin. It stops for the owner only when the review is still unsure (edit titles / artists, then 确认并导入 or 放弃) or when the library already has the album (替换原有专辑 / 保留原来的). One job runs at a time; the rest queue.

```sh
uv run bili-album serve [--host 127.0.0.1] [--port 5191] [--data-dir DIR]   # real pipeline
uv run bili-album serve --fake                                                # scripted jobs, no network, no import
```

- Jobs live in `.work/web/<job-id>/` (`job.json` plus the pipeline's `work/` directory). Finished and held jobs survive a restart; a job that was running becomes failed and can be retried with the same work directory.
- Standard library only (`http.server`); the page is `src/bili_album/web/static/index.html` (inline CSS and JS, no external requests).
- `--host` must be a specific address; `0.0.0.0` is refused. `deploy/bili-album-web.service` is a systemd user unit that binds to the Tailscale address (install steps in the file).
- `--fake` runs the real pipeline (`bili_album.pipeline`) with scripted stages: the plan, review, cut files and import result are made up in the shapes the real stages write, chosen by BV id: `BV14PVVzNE2R` clean album, `BV1sT421k7Ps` album with one Sol edit, `BV1Hx4y1z7Qm` derivative single held for confirmation, `BV1Wb411m7Eq` album with one flagged track, `BV1Kq4y1M7Vd` held album whose confirm is refused (hard problem in the plan), `BV1GJ411x7h7` failure in the plan stage, `BV1zt411p7Cn` already in the library, `BV1Ns411D7pE` import Jellyfin did not confirm (any other id behaves like the first). Nothing is downloaded, cut or imported. `--fake-speed N` runs N times faster; `--fake-hold review=600` parks a job in that stage for screenshots.
- The 「打开音乐库」 link is `BILI_ALBUM_JELLYFIN_PUBLIC_URL`, sent to the page as `library_url`; it is the only configuration value the browser receives. Every other configured value (keys, model address and name, internal Jellyfin address, library paths) is scrubbed from everything the page is sent.

API (JSON; mutating requests need `Content-Type: application/json` and a same-origin or absent `Origin`): `GET /api/jobs` (`{now, jobs, library_url?}`), `POST /api/jobs {url}`, `POST /api/jobs/<id>/confirm {tracks:[{index,title,artist}]}`, `POST /api/jobs/<id>/{discard,retry,replace,keep}`, `DELETE /api/jobs/<id>` (queued or failed), `GET /api/jobs/<id>/cover`.
