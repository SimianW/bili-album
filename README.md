# bili-album

Turn a Bilibili music upload into lossless, tagged `.m4a` files and import them into Jellyfin.

Supports:
- **Single song** videos → one `.m4a` file
- **Album compilations** (full album in one video) → split per track where songs actually change
- **Multi-part (分P)** videos → one `.m4a` file per part

Audio is cut with `ffmpeg -c copy` and never re-encoded.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on `PATH`

## Quick Start

```sh
git clone https://github.com/SimianW/bili-album.git
cd bili-album
uv sync
```

Export locally:

```sh
uv run bili-album run https://www.bilibili.com/video/BV14PVVzNE2R -o ~/Music/incoming
uv run bili-album inspect ~/Music/incoming/范特西
```

Or run the full pipeline (plan → LLM review → cut → Jellyfin import):

```sh
uv run bili-album ingest https://www.bilibili.com/video/BV14PVVzNE2R
```

## How It Works

1. **Plan** — Fetches metadata from Bilibili and audio via `yt-dlp`, reads uploader hints (chapters, description, pinned comment, part names), matches NetEase Cloud Music / iTunes catalogs, aligns track boundaries using audio analysis (silence, level dips, timbral novelty), verifies with Shazam, and refines each boundary to the millisecond. Writes `plan.json` and sets `needs_review` if anything is uncertain.
2. **Review** *(optional)* — Sends a compact brief to an OpenAI-compatible LLM to validate or fix uncertain plans. Every proposed edit is verified against catalog, recognition, or uploader data before being applied.
3. **Apply** — Losslessly cuts `.m4a` files and writes tags + cover art (`OUTDIR/<Album>/NN-<Title>.m4a` + `cover.jpg`, or `OUTDIR/<Artist> - <Title>.m4a` for singles).
4. **Import** — Copies files into `LIBRARY_DIR/<Album Artist>/<Album>/`, triggers a Jellyfin scan, and keeps Simplified Chinese metadata consistent on imported albums.

## CLI Commands

| Command | Description |
| --- | --- |
| `bili-album plan <url>` | Analyze a Bilibili URL (or `--audio FILE --title TEXT`) and write `plan.json`. Useful flags: `--album-hint "ARTIST - ALBUM"`, `--tracklist FILE`, `--cookies FILE`, `--no-text-hints`, `--no-recognize`, `--no-refine`. |
| `bili-album apply <plan.json> -o DIR` | Cut and tag `.m4a` files from a plan (`--force`, `--zip`). |
| `bili-album run <url> -o DIR` | Run `plan` + `apply` in one step. |
| `bili-album inspect <path>` | Show tags and durations of `.m4a` files (`--json`). |
| `bili-album review <plan.json>` | Review a plan with the configured LLM (`--dry-run`, `--no-replan`, `--out FILE`). |
| `bili-album import <path>` | Import an album folder, single `.m4a`, or `plan.json` into Jellyfin (`--replace`, `--dry-run`, `--timeout S`). |
| `bili-album ingest <url>` | End-to-end `plan` → `review` → `apply` → `import` (`--on-unsure hold\|import`, `--no-review`, `--no-import`, `--replace`). |
| `bili-album fix-library [DIR ...]` | Repair folder layout and Simplified Chinese tags for albums imported by this tool (`--all`, `--dry-run`). |
| `bili-album serve` | Start the web UI. |

## Web UI

A lightweight, single-page web UI (standard library only, no external assets): paste a Bilibili link (`bilibili.com/video/BV…`, `b23.tv/…`, or a bare `BV…`), monitor progress, edit/confirm uncertain tracks, and import directly into Jellyfin.

```sh
uv run bili-album serve                # http://127.0.0.1:5191/
uv run bili-album serve --fake         # demo mode, no network/import (login: any username + "demo")
```

- **Auth:** Uses Jellyfin accounts by default when `BILI_ALBUM_JELLYFIN_URL` is configured (`--auth none` to disable). Passwords are verified once against Jellyfin and never stored.
- **Networking:** Pass `--host` multiple times to bind specific addresses (`0.0.0.0` and `::` are refused). Behind a reverse proxy, forward `Host`, `X-Forwarded-For`, and `X-Forwarded-Proto` (configure `BILI_ALBUM_TRUSTED_PROXIES` if the proxy is not on loopback).
- **Deployment:** See [`deploy/bili-album-web.service`](deploy/bili-album-web.service) for a systemd user unit.

## Configuration

Only required for `review`, `import`, `ingest`, `fix-library`, and `serve`.

Store `KEY=VALUE` pairs in `~/.config/bili-album/env` (`chmod 600`), or set them as environment variables (env vars take precedence; set `BILI_ALBUM_CONFIG` to use a different path):

| Variable | Description |
| --- | --- |
| `BILI_ALBUM_LLM_BASE_URL` | OpenAI-compatible `/chat/completions` base URL for `review` |
| `BILI_ALBUM_LLM_MODEL` | Model name for `review` |
| `BILI_ALBUM_LLM_API_KEY` | API key for `review` |
| `BILI_ALBUM_JELLYFIN_URL` | Jellyfin server URL |
| `BILI_ALBUM_JELLYFIN_API_KEY` | Jellyfin API key |
| `BILI_ALBUM_JELLYFIN_PUBLIC_URL` | Public Jellyfin URL for links shown after import and in the web UI |
| `BILI_ALBUM_LIBRARY_DIR` | Host path to the Jellyfin music library |
| `BILI_ALBUM_JELLYFIN_LIBRARY_PATH` | Library path inside Jellyfin (default: `/media/music-videos/music`) |
| `BILI_ALBUM_TRUSTED_PROXIES` | Optional comma-separated reverse proxy IPs/CIDRs for `serve` |

## Development

```sh
uv run pytest -q                       # offline unit tests
uv run pytest -m network               # live integration tests
uv run python eval/run_eval.py         # run evaluation suite (eval/cases.yaml)
```

## License

[MIT](LICENSE)
