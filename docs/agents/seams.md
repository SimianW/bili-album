# Test seams

The agreed seams for this repo. The `tdd`, `to-spec`, `implement` and `diagnosing-bugs` skills test at these without asking the user; propose a new seam to the user only when none of these reaches the behaviour.

In order of preference:

1. **Targeted module tests**: `tests/test_<module>.py` (e.g., `tests/test_web_*.py`), run with `uv run pytest -q tests/test_<module>.py`. Fast feedback for a single module (alignment, hints, media, fake-LLM review, fake-Jellyfin library, web auth/jobs/server/fixtures); misses cross-module regressions and live external services.
2. **Full offline test suite**: `tests/`, run with `uv run pytest -q` (skips `@pytest.mark.network` by default). Catches regressions across planning, tagging, fake-LLM review, fake-Jellyfin import, eval scoring, and the web server; misses live external APIs and real browser rendering.
3. **Fake web server**: `src/bili_album/web/`, run with `uv run bili-album serve --fake --port <free port>` (bind `127.0.0.1`, login password `demo`). Catches real browser rendering, interactions, and scripted pipeline states with no network or Jellyfin writes; misses real Jellyfin auth/import and live downloads.
4. **Live network tests (opt-in)**: `tests/test_network.py`, run with `uv run pytest -m network`. Catches external API contract drift (Bilibili, catalogs, Shazam); requires network access and is slower/non-deterministic.
5. **Eval runner (opt-in)**: `eval/run_eval.py`, run with `uv run python eval/run_eval.py`. Catches end-to-end planning and alignment accuracy regressions across benchmark cases; slow and requires cached audio or network.

Before a PR, run every check in `.github/workflows/tests.yml` and `.github/workflows/file-length.yml` locally.
