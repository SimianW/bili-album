# Repo workflow

Disclosed procedure for pull requests, size/length checks, and multi-step feature delivery. Always-on invariants live in `AGENTS.md`.

## Pull requests and review gate

Every change reaches `main` through a PR.

1. Run `python3 scripts/check-pr-body.py tier` and include `- **Tier:** N` under `## Review` in the PR body (use the `pr` skill). Pushing to open a **draft PR** before review is allowed so CI runs early (`pr-body` CI runs only on ready PRs).
2. For **Tiers 1 and 2**, run `review-loop` on the final diff (within the tier's round budget). On `APPROVE`, `review-loop` adds ``- **Verdict:** approved at `<sha>` (diff `<12-hex>`)`` and `- **Reviewers:**` to the PR body. It gets the diff hash from the `diff-hash` mode of this repo's configured PR-body checker (`scripts/check-pr-body.py`, the body-check command below). Write the ``(diff `<12-hex>`)`` suffix whenever that checker has a `diff-hash` mode (or the repo has no checker); only while a legacy checker lacks one, write the plain `` - **Verdict:** approved at `<sha>` `` until `/simianize` upgrades it. (**Tier 0** skips this step.)
3. Run the local body check: `PR_BODY="$(cat body.md)" python3 scripts/check-pr-body.py`.
4. Mark the draft PR **ready** only after `review-loop` approves (for Tiers 1–2) and the local body check passes; merge only after CI is green.

## PR size and splitting

CI labels each PR from `size/XS` to `size/XXL` by lines of code changed (excluding prose, lock files, and binaries). Run `python3 scripts/pr-size.py` to check locally; CI owns setting the label.

When a task mixes a **structural refactor** (such as splitting a capped file along a seam) with **behavior changes**, or is projected to exceed `size/L`, ship the behavior-preserving refactor as its own small PR first, then stack the focused bugfix or feature PR on top.

For a stacked PR, open it against the parent PR's branch and pass that branch as `<base>` to the tier, `diff-hash`, `review-loop`, and local body-check commands:

- **Merge-commit repos:** after the parent PR merges and GitHub retargets the child PR to `main`, the child's `merge-base` stays on the parent's tip, so its `Verdict` diff hash remains valid.
- **Squash- or rebase-merge repos:** after the parent merges, the child's `merge-base` against `main` no longer matches the parent branch tip, so a pre-merge `Verdict` diff hash will fail CI. Keep stacked child PRs in draft until the parent merges (then update the branch and run `review-loop` against `main`), or re-run `review-loop` after retargeting.

## File length ratchet

Every code file stays at or under `500` lines.

- Split a growing file along a seam before raising a cap.
- Existing files over the limit are capped in `scripts/file-length.py`; caps only ratchet down. When a change shortens a capped file, lower its cap (or drop it once within `500`) in the same PR.
- Adding or raising a cap requires a `- **Cap raised:**` item in the PR body (exact format in the `pr` skill), or CI fails.
- Run `python3 scripts/file-length.py` locally before opening or updating a PR.

## New features

1. `to-spec` publishes the spec to the issue tracker labelled `ready-for-agent`.
2. Build directly with `implement <spec>`, or split via `to-tickets` and build with `implement-spec <spec>`.
3. Run `code-review` against the spec issue. Sort findings into **fix now / follow-up issue / drop** (one-line reason each) and stop for the user to choose. File each follow-up as a `ready-for-agent` issue.
4. After fixes, run `review-loop` on the final diff at its tier (listing approved refactors in its Intent).
5. Open a PR whose body includes `Closes #<issue>` and the `## Review` section's `Tier` item.

`to-spec`, `to-tickets`, `implement`, and `implement-spec` are user-invoked: at each handoff, tell the user the exact command to type (e.g., `/to-spec`).

## Delegation

Role models, `target` values, brief rules, and the `Verify` checklist live in `~/.agents/delegation.md` (and are activated for full sessions via `/simian-mode`). Any brief for delegated `implement` work must name the `tdd` skill and `docs/agents/seams.md`.
