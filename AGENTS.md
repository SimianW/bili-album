# AGENTS.md

You turn a Bilibili music link into tagged `.m4a` files for the owner's Jellyfin Music library.
Run everything with `uv run` from the repo root. Never commit downloaded audio, `.work/`, output, or cookie files.

- **Processing a Bilibili link:** before running `plan`, `run`, `review`, `apply`, `inspect`, `import`, or `ingest` on a link, read [docs/agents/link-workflow.md](docs/agents/link-workflow.md).

## Development

- `eval/cases.yaml` and `eval/validate_cases.py` belong to another agent. Do not edit them.
- Never send personal data (names, emails, hostnames, home paths) in requests. The User-Agent is `bili-album/<version>`.
- Machine config and API keys live in `~/.config/bili-album/env` (see README). Never print, log, commit or copy key
  values (not into tests, fixtures or error messages); `Config` hides them from `repr` and `Config.scrub` masks them.
- `review.py`: the model only proposes; every edit must be confirmed by data (`Editor.op_*`). Keep it that way when
  adding edit kinds. Offline tests use a local fake endpoint (`tests/test_review.py`); never record real replies.
- `library.py` writes into the live Jellyfin library and, after an import, writes item metadata with the API key
  (`LockData` first for albums with Chinese tags, then Traditional -> Simplified corrections, for the album just
  imported and its tracks only; see README). `fix-library` does the same, and moves folders to match their tags, for
  the tool's own albums (Bilibili source comment) or folders the owner names. Never extend that to other items.
  Test with a temporary `library_dir` and a fake Jellyfin; live experiments go under `zz-bili-album-test/` with
  `import_album(..., dest_dir=...)` and are removed with `remove_album(..., jellyfin=, config=)`.
- Never call `GET /Artists/{name}` to check for an artist: Jellyfin creates the artist when it does not exist. Look
  items up by id (`GET /Items/{id}?userId=<admin>`).
- To remove an imported album use `remove_album(..., jellyfin=, config=)` (deletes the Jellyfin record too); a bare
  folder delete leaves an orphan album record in Jellyfin 12.2.
- Owner-facing text goes through `messages.py`: plain Simplified Chinese in `message` / `error`, technical text in
  `error_detail`.

## Web page (`src/bili_album/web/`)

- `uv run bili-album serve --fake --port <free port>` for any UI work: scripted jobs, no network, nothing imported. The real mode (no `--fake`) imports into the owner's Jellyfin library; do not start it for testing.
- Bind to `127.0.0.1` in development; never `0.0.0.0` (the CLI refuses it). Stop the server when done. Never point a test server at the live data directory `.work/web/`: on start it cleans finished jobs there and recovers interrupted ones.
- Sign-in: `--fake` accepts any user name with the password `demo`. Real sign-in (`auth.py`) talks to Jellyfin's `/Users/AuthenticateByName` and `/Sessions/Logout`; tests use the fake Jellyfin in `tests/test_web_auth.py`. Never log or store a password, a Jellyfin token or a session token (sessions keep only a SHA-256).
- The web package does not import `library.py`; `serve`'s arguments live in `web/cli.py`.
- `runner.py` is the only code that knows `bili_album.pipeline`'s names (`PipelineRunner`, `map_event`, `map_result`, `read_plan`); change the mapping there when the pipeline API changes. `fake.py` runs the real pipeline with scripted `Steps`; `tests/test_web_fixtures.py` checks its plans against real reviewed plans in `tests/fixtures/web/` (refresh those from a live run when the plan or review format changes; strip config values and home paths first).
- Every string sent to the browser is scrubbed of config values (only the public Jellyfin address goes out, as `library_url`) and inserted with `textContent`, never as HTML. No CDN, fonts or other external requests in `static/index.html`.
- The approved designs are `mockups/web-import.html` and `mockups/web-login.html`.

## Agent skills

### Issue tracker

GitHub Issues on `SimianW/bili-album` via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical triage roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`GLOSSARY.md` + `docs/adr/` at repo root). See `docs/agents/domain.md`.

## Workflow

- PRs & size: every change reaches `main` through a PR. Before opening, updating, or merging a PR, read [docs/agents/workflow.md](docs/agents/workflow.md).
- File length: keep code files at or under 500 lines (split along a seam before raising a cap); see [docs/agents/workflow.md](docs/agents/workflow.md) for cap-ratchet rules and `python3 scripts/file-length.py`.
- Reviews: every gate review before merge (and any branch/PR review request) runs `review-loop` using `python3 scripts/check-pr-body.py tier`; only `implement` / `implement-spec` runs `code-review` before it. Without that skill, follow "Without the owner's skills or T3 Code" in [docs/agents/workflow.md](docs/agents/workflow.md).
- Naming: use [GLOSSARY.md](GLOSSARY.md) terms in modules, tests, and docs; add new terms there.
- Testing & bugs: test at the seams in [docs/agents/seams.md](docs/agents/seams.md); fix bugs via [docs/bugfix.md](docs/bugfix.md).
- Dev docs: put each module/test role in its header comment; keep setup, commands, and gotchas in [README.md](README.md).
- Frontend: run `mockup-first` before any visible UI change (`mockups/`; default UI language Simplified Chinese (`zh-CN`); tokens in `src/bili_album/web/static/index.html`).
- New features: for multi-step specs/tickets (`to-spec`, `to-tickets`, `implement`, `implement-spec`), read [docs/agents/workflow.md](docs/agents/workflow.md).
- Changelog: user-visible PRs add a bullet under `## [Unreleased]` in `CHANGELOG.md`; read [docs/agents/changelog.md](docs/agents/changelog.md) first.
