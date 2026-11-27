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

## Without the owner's skills or T3 Code

This file and `AGENTS.md` name skills (`review-loop`, `pr`, `code-review`, `tdd`, `diagnosing-bugs`, `mockup-first`, `to-spec`, `to-tickets`, `implement`, `implement-spec`) that live on the owner's machine, not in this repo. When your agent does not have one, do its job by hand as below. CI checks the PR body, tests, size and file length the same way either way; it cannot tell whether a review happened.

### Review (in place of `review-loop`)

Run `python3 scripts/check-pr-body.py tier <base>` first. Tier 0 needs no review; agent steering files (`AGENTS.md`, `CLAUDE.md`, `CODING_STANDARDS.md`, `docs/bugfix.md`, `docs/agents/*`) are at least tier 1. For tiers 1 and 2, two reviewers from two model families read the committed diff without seeing each other's findings. With the `claude` and `codex` CLIs installed and signed in, run both in the background from the repo root:

```bash
BASE_REF=origin/main                    # a stacked PR: origin/<parent PR's branch>
git fetch origin
BASE=$(git merge-base HEAD "$BASE_REF")
HEAD_SHA=$(git rev-parse HEAD)          # commit first: only committed work is reviewed
OUT=$(mktemp -d)
# The quoted 'BRIEF' keeps the shell from expanding backticks and $ in what you type here.
cat > "$OUT/brief.md" <<'BRIEF'
Review only. Do not edit, commit, push, or comment on the PR.
Intent: <one or two sentences: what the change is for, and what is out of scope>
Test changes: <the PR body's "Existing tests modified/removed" entry, or None>
Apply every rule in CODING_STANDARDS.md. Read the surrounding code. Find bugs, broken
callers and mismatch with the intent; skip formatting.
blocker = the intended case is wrong. should = a real bug on a path the repo's own code or
a user reaches. nit = the rest. A blocker or should needs evidence: a file:line trace, a
repro, or a failing test.
Start your reply with: ## Verdict: APPROVE | CHANGES (diff <the 12-char hash>)
Then list findings, one per line: severity, file:line, claim, evidence, fix.
BRIEF
printf 'See: git diff %s %s\nFor the hash, run: python3 scripts/check-pr-body.py diff-hash %s %s\n' \
  "$BASE" "$HEAD_SHA" "$BASE" "$HEAD_SHA" >> "$OUT/brief.md"

claude -p "$(cat "$OUT/brief.md")" \
  --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(python3 scripts/check-pr-body.py:*)" \
  > "$OUT/claude.md" 2> "$OUT/claude.err" &
codex exec --sandbox read-only -o "$OUT/codex.md" "$(cat "$OUT/brief.md")" \
  > "$OUT/codex.log" 2>&1 < /dev/null &
wait
head -n 1 "$OUT/claude.md" "$OUT/codex.md"
python3 scripts/check-pr-body.py diff-hash "$BASE_REF" "$HEAD_SHA"
```

Then:

1. Check that both Verdict headers carry the hash the last line printed. A reviewer with another hash read other content: run it again.
2. A finding both reviewers raise is confirmed. For a blocker or should only one raised, read the code yourself, or ask the other reviewer to confirm or refute it with evidence.
3. Fix every confirmed blocker and should in one commit, with a test where `CODING_STANDARDS.md` asks for one. If that commit changes anything above tier 0, run both reviewers again on the new head: up to 2 rounds at tier 1, 3 at tier 2.
4. The review is approved only when both reviewers have read the head you will record, and no blocker or should is left open. Then write the two items under `## Review` in the PR body, naming the reviewers that ran. `<short sha>` is the last `$HEAD_SHA` both reviewers read and `<12-hex>` is its hash from step 1; a tier 0 commit after it keeps them unchanged.

   ```markdown
   - **Verdict:** approved at `<short sha>` (diff `<12-hex>`)
   - **Reviewers:** <model> ×<calls>, <model> ×<calls>
   ```

If the rounds run out with a blocker or should open, or with a fix above tier 0 that no reviewer has read, stop: a person chooses another round, or ships anyway and writes ``- **Verdict:** overridden by <login> at `<short sha>` (diff `<12-hex>`)``. Never write `approved at` for a commit the reviewers did not read. With only one CLI, a person is the second reviewer and is named in `Reviewers`.

After approval, update the branch only by merging the base branch in: a rebase, amend or force-push drops the approved commit and fails the `pr-body` check. Leave the Verdict as it is and run the local body check (step 3 of "Pull requests and review gate", with `$BASE_REF` as its argument). If it fails on files the merge conflicted on, both reviewers read the resolution (`git show --remerge-diff <merge>`), and on approval the Verdict moves to the merge commit and its hash. An overridden PR that fails this way goes back to the person who overrode it.

### PR body (in place of `pr`)

```markdown
## Summary

<a short tree, diff sketch or call tree of what changes>

## Evidence

- **Before:** <output, failing test or screenshot>
- **After:** <output, passing test or screenshot>
- **Existing tests modified/removed:** None (or each changed assertion and why)
- **Not verified here:** <what your checks did not exercise>

## Merge Danger

| Door | Blast radius |
|---|---|
| <one-way or two-way> | <one word> |

## Review

- **Tier:** <N>
- **Cap raised:** `<path>` <old cap, or new> → <cap>: <why the file cannot be split now>
```

CI reads only the `Tier`, `Verdict` and `Cap raised` items, so keep those in exactly this shape. Drop the `Cap raised` item when the PR adds or raises no cap. A PR that changes visible UI embeds a screenshot under Evidence.

### The other skills

- `code-review`: compare the diff with the spec issue and with `CODING_STANDARDS.md`, and report the two lists separately.
- `tdd`: write one failing test at a seam from `docs/agents/seams.md`, make it pass with the smallest change, then tidy.
- `diagnosing-bugs`: get one command that fails on the bug before changing code ([docs/bugfix.md](../bugfix.md) steps 1 and 2).
- `mockup-first`: show the owner a self-contained HTML mockup and wait for approval before changing visible UI.
- `to-spec`, `to-tickets`, `implement`, `implement-spec`: write the spec as a GitHub issue labelled `ready-for-agent`, split it into sub-issues when it is large, and build each one test-first.
