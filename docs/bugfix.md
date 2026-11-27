# Fixing a bug

The steps for any bug the user reports or a review or test run finds. Each step ends on its completion criterion; finish it before starting the next.

## 1. Control

Reproduce the bug with the CLI (`uv run bili-album plan`/`apply`/`review`/`inspect`, using `--audio`, `--workdir`, or `--no-recognize` to isolate offline) or the fake web server (`uv run bili-album serve --fake --port <port>`). Because `bili-album` runs on top of `yt-dlp`, `ffmpeg`/`ffprobe`, `shazamio`, catalog APIs, an LLM proxy, and Jellyfin, reproduce it with `bili-album` and directly against the upstream tool or service when relevant: if it also happens without `bili-album`, it is the upstream system's bug—stop and report that to the user. If you cannot reproduce it, the user's capture (a log, a trace, a screenshot) stands in for the run, and the PR says so.

Done when: you have seen the bug yourself, or the user's capture shows this repo's own code going wrong.

## 2. Red

Build the feedback loop with the `diagnosing-bugs` skill (Phase 1), at the first seam in [docs/agents/seams.md](agents/seams.md) that reaches the bug.

If no automated seam reaches the bug (manual-only seam), write exact step-by-step reproduction commands and run them on the base commit to confirm the failing behavior, as `CODING_STANDARDS.md` allows.

Done when: one automated command goes **red** on this bug and you have seen it fail—or, for a manual-only seam, the written steps reproduce the bug on the base commit.

## 3. Green

Fix it with the `tdd` skill: the smallest change that turns the step 2 command **green** (or makes the manual repro steps pass on this branch).

Done when: the step 2 check passes, and every local test, lint, and file-length check in `.github/workflows/tests.yml` and `.github/workflows/file-length.yml` passes locally (`pr-body` runs in step 4 after `review-loop`).

## 4. Review and ship

Follow [docs/agents/workflow.md](agents/workflow.md) to review, run the PR-body check, and ship. The PR body carries the step 1 result and the step 2 command (or manual repro steps).

Done when: the PR is open with `- **Tier:** N` in its `## Review` section, passes the local PR-body check, and—for tiers 1 and 2—records `review-loop` `APPROVE` as ``- **Verdict:** approved at `<sha>` (diff `<12-hex>`)``.
