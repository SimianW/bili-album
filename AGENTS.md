# AGENTS.md

You turn a Bilibili music link into tagged `.m4a` files for the owner's Jellyfin Music library.
Run everything with `uv run` from the repo root. Never commit downloaded audio, `.work/`, output, or cookie files.

## Run on a link

1. Plan: `uv run bili-album plan <url>` (writes `.work/<bvid>/plan.json` and prints its path; a summary table goes to stderr).
2. Read the plan (see below). If `needs_review` is `false` and the table looks right, go to step 3. Otherwise fix it first (see "When needs_review is true").
3. Cut and tag: `uv run bili-album apply .work/<bvid>/plan.json -o <OUTDIR>`. Add `--zip` only if the owner asks for a zip.
4. Confirm: `uv run bili-album inspect <OUTDIR>/<Album>` (or the single file). Report the table.

`uv run bili-album run <url> -o <OUTDIR>` does plan + apply in one go. Use it only when you will not review the plan.

Output layout: album / multi-part → `OUTDIR/<Album>/NN-<Title>.m4a` plus `cover.jpg|png`; single → `OUTDIR/<Artist> - <Title>.m4a`.
`apply` never overwrites. If it reports existing files, stop and tell the owner; pass `--force` only if they say so.

## Read plan.json

- `kind` + `kind_reason`: `single`, `compilation` (one video, many songs) or `multipart` (分P, one song per part). Check the reason makes sense.
- `album`: title, artist, year, genre, catalog provider and `match_reason` (which catalog album was chosen and how well its track count and total length fit).
- `tracks[]`: `start`/`end` in seconds on the source audio (multipart tracks have their own `audio_path`), tags, `confidence` (0–1) and `evidence` (every signal used: `chapter`, `description`, `comment`, `part`, `catalog_duration`, `silence`, `dip`, `novelty`, `recognition`).
- `source.trim_head` / `source.trim_tail`: seconds before track 1 / after the last track that will not be exported (intro, outro, ads).
- `review_reasons`: one concrete problem per line, naming the tracks.

## When needs_review is true

Work through `review_reasons` in order. Typical fixes:

- **Wrong or missing album** (`album: no catalog candidate`, `close second candidate`, wrong `match_reason`): re-plan with `--album-hint "ARTIST - ALBUM"`.
- **Wrong track split or titles** (`Shazam heard X, plan says Y`, `length … disagrees with catalog`, `not found in the audio`, `segment … not in the catalog album`): find the real tracklist (official page, the video's comments) and re-plan with `--tracklist FILE`, one line per song: `title`, `mm:ss title`, or `title - artist`. Times make the split exact; titles alone make the tool align catalog lengths.
- **Single matched on title and length only**: the upload may be a cover, fan edit or AI remake. Ask the owner, or keep the guess and say so.
- **Artist copied from album artist**: confirm each listed track's performer; edit `tracks[].artist` if it differs.
- **Small boundary errors** (a second or two, a long intro/outro): hand-edit `start`/`end` in `plan.json`. Keep tracks in order and non-overlapping; `end` of track N is normally `start` of track N+1.
- **Intro/outro trimmed** (`intro of … is not exported`): check it is not a song. If it is, add a track or move `start` of track 1 to 0.

After edits run `apply` on the edited plan. `apply` validates first (increasing starts, positive durations, inside the source, title/artist/album/album_artist present) and prints `input error:` with every problem; fix them and re-run. Do not invent metadata: leave a field empty and tell the owner rather than guessing.

## Confirm the result

`apply` prints index, title, start, end, planned vs actual duration and the file name, lists optional tags left empty, and warns if a file's duration is more than 0.1 s off its plan or a required tag is missing. Then run `uv run bili-album inspect <dir>` (add `--json` for machine output) and check: one file per planned track, titles and track numbers in order, artist/album/album artist/year/cover present. Tell the owner to listen to the first and last seconds of a few tracks.

## Flags worth knowing

- `--no-text-hints`: ignore uploader text (chapters, description, comments, part names, tags); only the title, audio, catalogs and recognition are used. For evaluation.
- `--audio FILE --title TEXT [--description TEXT]`: offline source instead of a link (AAC/ALAC copied; lossless sources encoded as ALAC; lossy non-AAC refused).
- `--cookies FILE`: Netscape cookie file for yt-dlp (higher quality). Never copy it into the repo.
- `--no-recognize`: skip Shazam (faster; less verification).
- `--workdir DIR`: where the plan, audio, cover and HTTP cache live (default `.work/<bvid>/`). Re-planning reuses the cache.

## Development

- Tests: `uv run pytest -q` (offline). Live checks: `uv run pytest -m network`.
- `eval/cases.yaml` and `eval/validate_cases.py` belong to another agent. Do not edit them.
- Never send personal data (names, emails, hostnames, home paths) in requests. The User-Agent is `bili-album/<version>`.

## Web page (`src/bili_album/web/`)

- `uv run bili-album serve --fake --port <free port>` for any UI work: scripted jobs, no network, nothing imported. The real mode (no `--fake`) imports into the owner's Jellyfin library; do not start it for testing.
- Bind to `127.0.0.1` in development; never `0.0.0.0` (the CLI refuses it). Stop the server when done.
- `runner.py` is the only code that knows `bili_album.pipeline`'s names (`PipelineRunner`, `map_event`, `map_result`, `read_plan`); change the mapping there when the pipeline API changes. `FakeRunner` scripts every page state; keep it in step with new states.
- Every string sent to the browser is scrubbed of config secrets and inserted with `textContent`, never as HTML. No CDN, fonts or other external requests in `static/index.html`.
- Tests: `uv run pytest -q tests/test_web_*.py`. The approved design is `mockups/web-import.html`.
