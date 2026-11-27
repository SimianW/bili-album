#!/usr/bin/env python3
"""Review tier and PR-body check for bili-album.

Runs in CI on every pull request (.github/workflows/pr-body.yml).

Three modes:

    python3 scripts/check-pr-body.py tier [base]                # print `Review tier: N` for HEAD's changes
    python3 scripts/check-pr-body.py diff-hash [base] [rev]     # print the 12-char diff hash of `<merge-base>..<rev>`
    PR_BODY=... python3 scripts/check-pr-body.py [base]         # check a PR body (CI)

`base` defaults to DEFAULT_BASE and `rev` to HEAD. The diff hash is the first 12 hex digits of
the SHA-256 of `<merge-base>^{tree}..<rev>^{tree}` (the two full tree ids and a newline), so no
local `git diff` or `core.*` setting can change it.

The PR body records the review as a Markdown list, by convention under a `## Review` heading at
the end of the body (layout in the `pr` skill):

    ## Review

    - **Tier:** 1
    - **Verdict:** approved at `fd81ae1` (diff `a1b2c3d4e5f6`)
    - **Reviewers:** gpt-6.2-sol ×2, claude-opus-5-5 ×2

The check enforces:

1. The first Tier item gives N in 0..MAX_TIER, and N is at least the tier computed from the changed
   files against base. Agent steering files (`AGENTS.md` or `CLAUDE.md` in any folder,
   `CODING_STANDARDS.md`, `docs/bugfix.md`, `docs/agents/*`) are at least tier 1, not tier 0
   (still tier 2 if matched by a high-risk path).
2. When the computed tier is at least REVIEW_FROM_TIER, the last Verdict item reads
   `approved at <sha> (diff <12-hex>)` or `overridden by <user> at <sha> (diff <12-hex>)`
   (7 to 40 hex digits for sha, 12 hex digits for diff hash, backticks optional;
   the override form records a person shipping a stuck review-loop), the sha exists in this checkout
   and is an ancestor of HEAD, its `<merge-base>..<sha>` diff hash matches `<12-hex>`, and nothing
   changed after it reaches REVIEW_FROM_TIER. A rewrite
   that drops the approved commit from HEAD's history (a rebase, or an amend or force-push of that
   commit) fails the ancestor rule: review-loop's After approval updates a PR only by merging the
   base branch in. The approved commit is replayed onto the current base with `git merge-tree`
   (git 2.40+), so a later merge of the base branch does not count as a change. When the replay
   conflicts, the paths it conflicts on are classified too, so every resolution of a conflicted
   file counts as a change after the approval, even one that keeps the replayed tree's own copy
   (for a modify/delete or binary conflict, one side's version with no conflict markers). Only
   conflicted file paths count: a directory-level conflict (a directory rename split) committed
   as git proposed it changes no file and passes.
3. At any tier, the PR adds no leftover conflict markers: the lines `git diff --check` reports as
   "leftover conflict marker" between the merge base and the working tree fail, each named by
   path and line; a Markdown heading underline of exactly seven `=` counts as one. Its whitespace
   findings do not count.
4. The Reviewers item is for people; the check does not read it.

Items inside HTML comments (`<!-- ... -->`) and fenced code blocks do not count: they never render.

Exits 0 when the body passes; 1 with one `FAIL:` line per failure otherwise; 2 on bad usage.
"""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath


# --------------------------------------------------------------------------------------------
# Repo settings. Paths are repo-relative with forward slashes.
# --------------------------------------------------------------------------------------------

DEFAULT_BASE = "origin/main"

MAX_TIER = 2  # Tiers run 0..MAX_TIER. 0: checks only; 1: ordinary code; 2: high risk.
REVIEW_FROM_TIER = 1  # Changes at this tier or above need a review-loop Verdict.

# Tier 2: paths where a mistake is costly (secrets, auth, live library mutations, deploy, CI, CLIs).
HIGH_RISK_PREFIXES = (
    ".github/workflows/",
    "deploy/",
)
HIGH_RISK_FILES = (
    "scripts/check-pr-body.py",  # This check gates itself: weakening it needs a tier-2 review.
    "src/bili_album/config.py",
    "src/bili_album/library.py",
    "src/bili_album/cli.py",
    "src/bili_album/web/auth.py",
    "src/bili_album/web/cleanup.py",
    "src/bili_album/web/cli.py",
    "src/bili_album/web/server.py",
)

# Tier 1: agent steering files are executable instructions for agents, never tier 0.
# AGENTS.md and CLAUDE.md count in any folder (tools read nested copies too).
AGENT_STEERING_BASENAMES = (
    "AGENTS.md",
    "CLAUDE.md",
)
AGENT_STEERING_PREFIXES = (
    "docs/agents/",
)
AGENT_STEERING_FILES = (
    "CODING_STANDARDS.md",
    "docs/bugfix.md",
)

# Tier 0: changes that need only the CI checks, no review.
CHECKS_ONLY_PREFIXES = (
    "docs/",
    "notes/",
    "mockups/",
    "eval/results/",
)
CHECKS_ONLY_SUFFIXES = (
    ".md",
    ".png",
    ".svg",
    ".jpg",
    ".jpeg",
)


def classify_special(path, base_rev, root, conflicted):
    """Return (tier, reason) for a path that needs a content heuristic, or None."""
    return None


# --------------------------------------------------------------------------------------------
# The rest is generic. Change it only when the PR-body format in the `pr` skill changes.
# --------------------------------------------------------------------------------------------

FENCE_OPENER = re.compile(r"(`{3,}|~{3,})(.*)")
TIER_ITEM = re.compile(r"[-*] \*\*Tier:\*\* ([0-9]+)")
CONFLICT_MARKER = re.compile(r"(.+):([0-9]+): leftover conflict marker")
VERDICT_ITEM = re.compile(
    r"[-*] \*\*Verdict:\*\* (approved|overridden by [^`@\s]+) at (`?)([0-9a-fA-F]{7,40})\2"
    r" \(diff (`?)([0-9a-fA-F]{12})\4\)"
)


def rendered_lines(body):
    """Return the body's stripped lines without the text that Markdown does not render.

    Drops HTML comments (`<!-- ... -->`, on one line or across several; the text around them on
    a line stays) and fenced code blocks (``` or ~~~, with or without an info string; all fence
    lines go). A fence closes on a line of only its own character, at least as long as the
    opener; a backtick opener's info string has no backtick, so ```x``` is inline code. An unclosed
    fence, or an unclosed comment that opens at the start of its line, hides the rest of the
    body. A `<!--` after other text that does not close on its own line is kept as text, although
    Markdown can carry it over the paragraph's next lines. Fence markers inside a comment, and
    `<!--` inside a fence, are literal. Indented code blocks are not recognised. Fences that open
    on a list-marker line, and closers indented 4 or more spaces, are not handled as CommonMark
    does.
    """
    lines = []
    fence = None  # (character, length) of the open fence
    in_comment = False
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        if fence is not None:
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= fence[1]:
                fence = None
            continue
        if not in_comment:
            opener = FENCE_OPENER.match(stripped)
            if opener and not (opener.group(1)[0] == "`" and "`" in opener.group(2)):
                fence = (opener.group(1)[0], len(opener.group(1)))
                continue
        kept, i = [], 0
        while i < len(raw):
            if in_comment:
                end = raw.find("-->", i)
                if end < 0:
                    break
                in_comment, i = False, end + 3
            else:
                start = raw.find("<!--", i)
                if start < 0:
                    kept.append(raw[i:])
                    break
                end = raw.find("-->", start + 2)
                if end < 0 and raw[:start].strip():
                    kept.append(raw[i:])
                    break
                kept.append(raw[i:start])
                if end < 0:
                    in_comment = True
                    break
                i = end + 3
        lines.append("".join(kept).strip())
    return lines


class GitError(Exception):
    pass


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], capture_output=True, cwd=cwd)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {message}")
    return result.stdout.decode("utf-8")


def git_status(*args, ok, cwd=None):
    """Return (status, stdout) for a git command whose exit status is an answer; a status
    outside ok is an error."""
    result = subprocess.run(["git", *args], capture_output=True, cwd=cwd)
    if result.returncode not in ok:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {message}")
    return result.returncode, result.stdout.decode("utf-8", "replace")


def changed_since(rev, root):
    """Paths that differ between rev and the working tree, untracked files included, sorted."""
    tracked = git("diff", "--name-only", "--no-renames", "-z", rev, "--", cwd=root)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", cwd=root)
    return sorted({path for path in (tracked + untracked).split("\0") if path})


def diff_hash(base_rev, head_rev, root):
    """Return 12-char lowercase SHA-256 hex digest of `<base_tree>..<head_tree>`.

    Uses Git's own full tree object IDs from `git rev-parse --verify <rev>^{tree}`, so the
    digest uniquely identifies the committed tree change and is immune to local
    diff-rendering git configuration.
    """
    base_tree = git("rev-parse", "--verify", f"{base_rev}^{{tree}}", cwd=root).strip()
    head_tree = git("rev-parse", "--verify", f"{head_rev}^{{tree}}", cwd=root).strip()
    return hashlib.sha256(f"{base_tree}..{head_tree}\n".encode("ascii")).hexdigest()[:12]


def classify_path(path, base_rev, root, conflicted=False):
    """Return (tier, reason) for one path. The first matching rule wins."""
    special = classify_special(path, base_rev, root, conflicted)
    if special is not None:
        return special
    if path.startswith(HIGH_RISK_PREFIXES) or path in HIGH_RISK_FILES:
        return MAX_TIER, f"{path} (high risk)"
    if (
        PurePosixPath(path).name in AGENT_STEERING_BASENAMES
        or path.startswith(AGENT_STEERING_PREFIXES)
        or path in AGENT_STEERING_FILES
    ):
        return 1, f"{path} (agent instructions)"
    if path.startswith(CHECKS_ONLY_PREFIXES) or PurePosixPath(path).suffix in CHECKS_ONLY_SUFFIXES:
        return 0, f"{path} (docs or assets)"
    return 1, path


def classify(paths, base_rev, root, conflicted=frozenset()):
    """Return (tier, reasons): the highest tier any path reaches, and the paths that reach it.
    conflicted holds the paths that conflict when the approved commit is replayed onto base_rev."""
    results = [classify_path(path, base_rev, root, path in conflicted) for path in sorted(set(paths))]
    tier = max(result_tier for result_tier, _ in results)
    return tier, [reason for result_tier, reason in results if result_tier == tier]


def check_conflict_markers(merge_base, root):
    """Return (failures, passes): leftover conflict markers fail the PR at any tier. `git diff
    --check` exits 2 on any finding, whitespace too, so only its marker lines count."""
    _, out = git_status("diff", "--check", merge_base, "--", ok=(0, 1, 2), cwd=root)
    found = map(CONFLICT_MARKER.fullmatch, out.splitlines())
    markers = [f"{m.group(1)}:{m.group(2)}" for m in found if m]
    if markers:
        return [
            f"leftover conflict markers at {', '.join(markers)}; resolve the conflict and commit "
            "(a heading underline of exactly seven `=` counts too; lengthen it)"
        ], []
    return [], ["no leftover conflict markers"]


def check_body(body, tier, reasons, review_gate, head, head_diff_hash="000000000000"):
    """Return (failures, passes) for a PR body against the computed tier."""
    lines = rendered_lines(body)
    failures, passes = [], []

    claims = [int(m.group(1)) for m in map(TIER_ITEM.fullmatch, lines) if m]
    claimed = claims[0] if claims else None
    if claimed is None or claimed > MAX_TIER:
        failures.append(f"no `- **Tier:** N` item with N in 0..{MAX_TIER}; add `- **Tier:** {tier}`")
    elif claimed < tier:
        failures.append(f"body claims Tier {claimed}, but the changes reach tier {tier}: {'; '.join(reasons)}")
    else:
        passes.append(f"Tier {claimed} is at least the computed tier {tier}")

    if tier < REVIEW_FROM_TIER:
        passes.append(f"tier {tier} needs no Verdict item")
        return failures, passes

    verdicts = [m for m in map(VERDICT_ITEM.fullmatch, lines) if m]
    if not verdicts:
        failures.append(
            f"tier {tier} needs a Verdict item with diff hash; add "
            f"`- **Verdict:** approved at `{head}` (diff `{head_diff_hash}`)` after review-loop approves"
        )
        return failures, passes
    verdict, sha, expected_hash = verdicts[-1].group(1), verdicts[-1].group(3), verdicts[-1].group(5).lower()
    passes.append(f"Verdict: {verdict} at {sha} (diff {expected_hash})")
    gate_failures, gate_passes = review_gate(sha, expected_hash)
    return failures + gate_failures, passes + gate_passes


def replayed_tree(sha, base, root):
    """Return (tree, conflicted): the tree of the approved sha's PR changes replayed onto the
    current base, and the set of paths that conflict in that replay (empty when it is clean). A
    conflicted path's entry in the tree may hold conflict markers, or one side's version with none
    (modify/delete, binary), so callers treat every conflicted path as changed."""
    old_base = git("merge-base", sha, base, cwd=root).strip()
    new_base = git("merge-base", "HEAD", base, cwd=root).strip()
    args = (
        "merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
        f"--merge-base={old_base}", sha, new_base,
    )
    status, out = git_status(*args, ok=(0, 1), cwd=root)  # 1: the replay conflicts
    # Output: the tree id, then each conflicted path, every field NUL-terminated.
    tree, *paths = out.split("\0")
    return tree.strip(), {path for path in paths if path} if status == 1 else set()


def check_after_review(sha, expected_hash, base, root):
    """Return (failures, passes) for the diff hash and changes made after the approved commit."""
    try:
        full = git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", cwd=root).strip()
    except GitError:
        return [
            f"approved commit {sha} is not in this checkout: the approved history was likely rewritten (rebase, amend "
            "or force-push); follow review-loop's After approval: merge the base branch in, never rebase"
        ], []
    status, _ = git_status("merge-base", "--is-ancestor", full, "HEAD", ok=(0, 1), cwd=root)
    if status == 1:
        return [
            f"approved commit {sha} is not an ancestor of HEAD: the approved history was rewritten (rebase, amend or "
            "force-push); follow review-loop's After approval and merge the base branch in instead of rebasing"
        ], []
    old_base = git("merge-base", full, base, cwd=root).strip()
    actual_hash = diff_hash(old_base, full, root)
    if actual_hash != expected_hash:
        return [
            f"Verdict diff hash {expected_hash} does not match commit {sha}'s diff against {base} ({actual_hash}); "
            f"the Verdict must carry the hash the approving seats reviewed (`diff-hash {base} {sha}`). "
            "If this is a stacked PR whose parent merged or whose base branch changed, its merge base moved "
            "and the PR needs a new review-loop round against the current base"
        ], []
    hash_pass = f"diff hash {actual_hash} matches commit {sha}"
    reviewed, conflicted = replayed_tree(full, base, root)
    changed = sorted(set(changed_since(reviewed, root)) | conflicted)
    if not changed:
        return [], [hash_pass, f"nothing changed after the approved commit {sha}"]
    tier, reasons = classify(changed, reviewed, root, conflicted)
    if tier >= REVIEW_FROM_TIER:
        return [
            f"changes after the approved commit {sha} reach tier {tier}: {'; '.join(reasons)}; follow review-loop's "
            "After approval: if every flagged change comes from resolving a base-branch merge conflict, an approved "
            "Verdict gets one merge round on that resolution and an override goes back to the user; otherwise run a "
            "new review-loop round"
        ], []
    return [], [hash_pass, f"changes after the approved commit {sha} are tier 0"]


def collect(base):
    """Return (root, merge_base, paths) for HEAD's work against base."""
    root = git("rev-parse", "--show-toplevel").strip()
    merge_base = git("merge-base", "HEAD", base, cwd=root).strip()
    return root, merge_base, changed_since(merge_base, root)


def main(argv):
    mode = argv[0] if argv and argv[0] in ("tier", "diff-hash") else "check"
    args = argv[1:] if mode != "check" else argv
    max_args = 2 if mode == "diff-hash" else 1
    if len(args) > max_args or any(a.startswith("-") for a in args):
        print(
            "usage: check-pr-body.py tier [base] | check-pr-body.py diff-hash [base] [rev] "
            "| PR_BODY=... check-pr-body.py [base]",
            file=sys.stderr,
        )
        return 2
    base = args[0] if args else DEFAULT_BASE
    try:
        root = git("rev-parse", "--show-toplevel").strip()
        if mode == "diff-hash":
            rev = args[1] if len(args) == 2 else "HEAD"
            mb = git("merge-base", rev, base, cwd=root).strip()
            print(diff_hash(mb, rev, root))
            return 0
        root, merge_base, paths = collect(base)
        if not paths:
            print(f"check-pr-body: no changes against {base} (merge base {merge_base[:7]})", file=sys.stderr)
            return 1
        tier, reasons = classify(paths, merge_base, root)
        if mode == "tier":
            print(f"Review tier: {tier}")
            print("\n".join(reasons))
            return 0
        head = git("rev-parse", "--short", "HEAD", cwd=root).strip()
        head_hash = diff_hash(merge_base, "HEAD", root)
        failures, passes = check_body(
            os.environ.get("PR_BODY", ""),
            tier,
            reasons,
            lambda sha, expected_hash: check_after_review(sha, expected_hash, base, root),
            head,
            head_hash,
        )
        marker_failures, marker_passes = check_conflict_markers(merge_base, root)
        failures, passes = failures + marker_failures, passes + marker_passes
    except (GitError, OSError) as error:
        print(f"check-pr-body: {error}", file=sys.stderr)
        return 1
    for message in failures:
        print(f"FAIL: {message}")
    if failures:
        return 1
    for message in passes:
        print(f"PASS: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
