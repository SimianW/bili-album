# AGENTS.md

You turn a Bilibili music link into tagged `.m4a` files for the owner's Jellyfin Music library.
Run everything with `uv run` from the repo root. Never commit downloaded audio, `.work/`, output, or cookie files.

## Run on a link

1. Plan: `uv run bili-album plan <url>` (writes `.work/<bvid>/plan.json` and prints its path; a summary table goes to stderr).
2. Read the plan (see below). If `needs_review` is `false` and the table looks right, go to step 3. Otherwise fix it first (see "When needs_review is true").
3. Cut and tag: `uv run bili-album apply .work/<bvid>/plan.json -o <OUTDIR>`. Add `--zip` only if the owner asks for a zip.
4. Confirm: `uv run bili-album inspect <OUTDIR>/<Album>` (or the single file). Report the table.

`uv run bili-album run <url> -o <OUTDIR>` does plan + apply in one go. Use it only when you will not review the plan.

`uv run bili-album review <plan.json>` lets the configured model review the plan first (in place; the original
is kept as `plan.unreviewed.json`). Read `review` in the result: `verdict`, `edits_applied`, `edits_rejected`,
`remaining_concerns`. If `needs_review` is still true, handle it as below. `uv run bili-album import <album-dir>`
puts a finished album into the owner's Jellyfin library; `uv run bili-album ingest <url>` does everything.
Import only what the owner asked for; never pass `--replace` unless they say so.

Output layout: album / multi-part → `OUTDIR/<Album>/NN-<Title>.m4a` plus `cover.jpg|png`; single → `OUTDIR/<Artist> - <Title>.m4a`.
`apply` never overwrites. If it reports existing files, stop and tell the owner; pass `--force` only if they say so.

## Read plan.json

- `kind` + `kind_reason`: `single`, `compilation` (one video, many songs) or `multipart` (分P, one song per part). Check the reason makes sense.
- `album`: title, artist, year, genre, catalog provider and `match_reason` (which catalog album was chosen and how well its track count and total length fit).
- `tracks[]`: `start`/`end` in seconds on the source audio (multipart tracks have their own `audio_path`), tags, `confidence` (0–1) and `evidence` (every signal used: `chapter`, `description`, `comment`, `part`, `catalog_duration`, `silence`, `dip`, `novelty`, `recognition`).
- `source.trim_head` / `source.trim_tail`: seconds before track 1 / after the last track that will not be exported (intro, outro, ads).
- `source.derivative` (+ `derivative_cues`, `derivative_info`): the title says the upload is a cover, fan edit, AI remake, rendition or remix; the tags then name the ORIGINAL song (see below) and the plan is always flagged.
- `recognition`: how many tracks Shazam checked and why (`policy`: `single`, `unsure`, `sampled`, `full`, `none`; `requests`; `reason`).
- `review_reasons`: one concrete problem per line, naming the tracks.

## How the plan decides (rules worth knowing when you check it)

- **Cuts at gaps.** A low-level run between songs is often a fade-out, a short true gap and a soft intro. The cut goes at the end of the run's deepest part (frames within 6 dB of its minimum or at digital-silence level, -70 dBFS), not at the end of the run, so a quiet intro stays with its song. When recognition gives the next recording's start, the cut may never land later than that start minus 0.3 s, unless everything in between is digital silence (then nothing audible is lost). Each track's `evidence` says which case applied (`digital silence`, `quiet gap … floor -58 dB`, `cut held 0.3s before the recognised start`).
- **Recognition budget (blind runs).** A clean catalog alignment (every catalog track placed, lengths within tolerance, total length fits) is verified with 3 tracks from the first, middle and last third, plus any track whose start is weak; every track is checked only when a sample disagrees or the alignment is not clean. With text hints only tracks below confidence 0.8 are checked.
- **Order from recognition.** When recognition consistently names other tracks of the same album than the catalog order predicts (vinyl or regional track order, missing bonus track), the tracks are re-labelled in heard order, catalog tracks never heard are dropped, the album is re-aligned and track numbers follow the video.
- **Release for a single.** The artist's own earliest album with the recording wins over a same-named single, which wins over a compilation (various artists, best-of, soundtrack, playlist). A compilation is used only when nothing else exists, and then flagged.
- **Derivatives.** Title cues (翻唱, cover, 改编, remix, AI, 演奏, 「X版」 …) mark a derivative. The original is the earliest same-titled, unmarked release that other artists' versions followed (two or more within a year); an explicit `原唱：X` / `翻自 X` credit in the catalog wins; otherwise the earliest. The video's length is never evidence for the original.
- **Original script.** Titles keep the original script: a bracketed Latin translation after a Korean / Japanese / Chinese title is dropped (`괴물(Monster)` → `괴물`), Latin-only storefront titles are replaced by the other provider's original-script title for the same track, and a recognition that returns a translated title of the same recording counts as agreement.
- **Editions.** An album edition is also scored without its instrumental / accompaniment tracks, repeated titles, or a trailing bonus block, so a 20-track "songs + instrumentals" edition matches a 10-song video.

## When needs_review is true

Work through `review_reasons` in order. Typical fixes:

- **Wrong or missing album** (`album: no catalog candidate`, `close second candidate`, wrong `match_reason`): re-plan with `--album-hint "ARTIST - ALBUM"`.
- **Wrong track split or titles** (`Shazam heard X, plan says Y`, `length … disagrees with catalog`, `not found in the audio`, `segment … not in the catalog album`): find the real tracklist (official page, the video's comments) and re-plan with `--tracklist FILE`, one line per song: `title`, `mm:ss title`, or `title - artist`. Times make the split exact; titles alone make the tool align catalog lengths.
- **Single matched on title and length only**: the upload may be a cover, fan edit or AI remake. Ask the owner, or keep the guess and say so.
- **Derivative version** (`the video looks like a derivative version (cue: …)`): check the named original artist (the reason says which rule picked it); if it is wrong, edit `tracks[].artist`, `album`, `album_artist` and `year`. Never tag a fan version with the uploader as artist.
- **Track order follows recognition** / **compilation** (`… is a compilation; the artist's own album … was not found`): usually right; confirm the heard order or look for the original album and re-plan with `--album-hint`.
- **Weak start** (`start … rests only on …`): no silence or recognition supports that boundary; listen around it and hand-edit `start`.
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
- Machine config and API keys live in `~/.config/bili-album/env` (see README). Never print, log, commit or copy key
  values (not into tests, fixtures or error messages); `Config` hides them from `repr` and `Config.scrub` masks them.
- `review.py`: the model only proposes; every edit must be confirmed by data (`Editor.op_*`). Keep it that way when
  adding edit kinds. Offline tests use a local fake endpoint (`tests/test_review.py`); never record real replies.
- `library.py` writes into the live Jellyfin library: test with a temporary `library_dir` and a fake Jellyfin.

## Web page (`src/bili_album/web/`)

- `uv run bili-album serve --fake --port <free port>` for any UI work: scripted jobs, no network, nothing imported. The real mode (no `--fake`) imports into the owner's Jellyfin library; do not start it for testing.
- Bind to `127.0.0.1` in development; never `0.0.0.0` (the CLI refuses it). Stop the server when done.
- `runner.py` is the only code that knows `bili_album.pipeline`'s names (`PipelineRunner`, `map_event`, `map_result`, `read_plan`); change the mapping there when the pipeline API changes. `fake.py` runs the real pipeline with scripted `Steps`; `tests/test_web_fixtures.py` checks its plans against real reviewed plans in `tests/fixtures/web/` (refresh those from a live run when the plan or review format changes; strip config values and home paths first).
- Every string sent to the browser is scrubbed of config values (only the public Jellyfin address goes out, as `library_url`) and inserted with `textContent`, never as HTML. No CDN, fonts or other external requests in `static/index.html`.
- Tests: `uv run pytest -q tests/test_web_*.py`. The approved design is `mockups/web-import.html`.
