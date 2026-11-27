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

**The tool writes Jellyfin metadata, for its own albums only.** Chinese is always Simplified. Jellyfin's
MusicBrainz fetcher adds a Traditional-script duplicate about 10 s after a new album is listed (周杰倫 next to
周杰伦). For an album whose tags contain Chinese (Han characters, no kana) the import therefore locks the album item
(`LockData` through `POST /Items/{id}`; Jellyfin passes the lock on to the tracks) as soon as Jellyfin lists it, keeps
comparing for 45 s, then runs a full library scan and compares once more. Wherever Jellyfin shows a tagged value in
Traditional script (OpenCC `t2s` of Jellyfin's value equals the tag), or adds a Traditional duplicate of a tagged
artist, the item is set back to the tagged value. Other scripts and other names are never touched; albums without
Chinese tags are left alone. Jellyfin 12.2 has no lockable artist field, and without the lock a "replace all
metadata" refresh brought the Traditional artist back. The scan drops the Traditional artist entry once nothing
points at it. Results are in the import result (`normalized`, `normalize_locked`, `normalize_verified`,
`leftover_artists`, `warnings`); a failed correction is a warning, the import still counts.

What the lock costs: Jellyfin skips its internet metadata providers for the locked album and its tracks, so they get
no MusicBrainz ids or links and no online album overview, genres or ratings; online artwork is probably not
fetched either (not verified; the tool's `cover.jpg` and embedded cover still show). The artist pages (biography, photo) are separate items and are not
locked. To let Jellyfin enrich an album again, untick "Lock this item" in its metadata editor (the Traditional name
may then come back).

`bili-album fix-library [ALBUM_DIR ...] [--all] [--dry-run]` repairs albums already in the library: the folders given
(relative to the library or absolute), or with `--all` every album whose files carry the tool's Bilibili source
comment (albums added by hand are never selected). It moves a folder whose tags no longer match it to
`<Album Artist>/<Album>/` (never onto a non-empty folder; an emptied artist folder is removed; the old Jellyfin record
is deleted once its path is gone, so no orphan stays), then locks and corrects as above. `--dry-run` lists the moves
and corrections.

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
uv run bili-album serve [--host ADDR ...] [--port 5191] [--data-dir DIR] [--auth jellyfin|none]   # real pipeline
uv run bili-album serve --fake                    # scripted jobs, no network, no import; sign in with any name + "demo"
```

- **Sign-in:** with a Jellyfin URL configured, the page asks for a Jellyfin user name and password (`--auth none` turns this off; without a Jellyfin URL the server refuses to start unless `--auth none` is given). The password is checked once against Jellyfin (`/Users/AuthenticateByName`; the Jellyfin session this creates is ended at once) and never stored. The page's own session is a random cookie (`HttpOnly`, `SameSite=Lax`, `Secure` behind HTTPS), kept on the server only as a SHA-256 hash in `<data-dir>/sessions.json`; it lasts 30 days from last use, and the account is re-checked against Jellyfin at most hourly (deleted or disabled: signed out). Everyone who can sign in has the same rights. Five failed sign-ins in five minutes for one user name or one client address block it for five minutes. Every job records who submitted it and who confirmed, replaced, kept or retried it; deleted jobs leave a line in `<data-dir>/audit.log`. At most 20 jobs wait in the queue.
- **Clean-up:** when a job is imported (or ended with 「保留原来的」) its big files are deleted (`work/source.m4a`, `work/part*.m4a`, `work/features.npz`, `work/out/`, `work/cache/`); the record, plans and cover stay. Finished jobs already on disk are cleaned on start. Failed and held jobs keep their files for 「再试一次」 / 「确认并导入」 / 「替换原有专辑」.
- **Listening:** `--host` may be repeated; every address gets a socket on the same port, served by one process. `0.0.0.0` and `::` are refused; an address that cannot be bound fails the start. The unit in `deploy/` listens on `127.0.0.1`, `::1` and the Tailscale address.
- **Behind a reverse proxy:** point it at `http://localhost:5191` (or `127.0.0.1` / `[::1]`) and pass `Host` unchanged, `X-Forwarded-For` (the client) and `X-Forwarded-Proto` (`https`). Requests from loopback, from the server's own addresses and from `BILI_ALBUM_TRUSTED_PROXIES` (optional, addresses or networks, in the env file) are trusted: the attempt limiter then uses the right-most untrusted `X-Forwarded-For` entry and the cookie gets `Secure` on `https`. Other peers' forwarding headers are ignored.

- Jobs live in `.work/web/<job-id>/` (`job.json` plus the pipeline's `work/` directory). Finished and held jobs survive a restart; a job that was running becomes failed and can be retried with the same work directory.
- Standard library only (`http.server`); the page is `src/bili_album/web/static/index.html` (inline CSS and JS, no external requests).
- `deploy/bili-album-web.service` is a systemd user unit (install steps in the file).
- `--fake` runs the real pipeline (`bili_album.pipeline`) with scripted stages: the plan, review, cut files and import result are made up in the shapes the real stages write, chosen by BV id: `BV14PVVzNE2R` clean album, `BV1sT421k7Ps` album with one Sol edit, `BV1Hx4y1z7Qm` derivative single held for confirmation, `BV1Wb411m7Eq` album with one flagged track, `BV1Kq4y1M7Vd` held album whose confirm is refused (hard problem in the plan), `BV1GJ411x7h7` failure in the plan stage, `BV1zt411p7Cn` already in the library, `BV1Ns411D7pE` import Jellyfin did not confirm (any other id behaves like the first). Nothing is downloaded, cut or imported. `--fake-speed N` runs N times faster; `--fake-hold review=600` parks a job in that stage for screenshots.
- The 「打开音乐库」 link is `BILI_ALBUM_JELLYFIN_PUBLIC_URL`, sent to the page as `library_url`; it is the only configuration value the browser receives. Every other configured value (keys, model address and name, internal Jellyfin address, library paths) is scrubbed from everything the page is sent.

API (JSON; mutating requests need `Content-Type: application/json` and a same-origin or absent `Origin`; with sign-in on, every `/api/*` route except `POST /api/login` answers 401 without a session): `POST /api/login {username, password}`, `POST /api/logout`, `GET /api/jobs` (`{now, jobs, me, auth, library_url?}`), `POST /api/jobs {url}`, `POST /api/jobs/<id>/confirm {tracks:[{index,title,artist}]}`, `POST /api/jobs/<id>/{discard,retry,replace,keep}`, `DELETE /api/jobs/<id>` (queued or failed), `GET /api/jobs/<id>/cover`.
