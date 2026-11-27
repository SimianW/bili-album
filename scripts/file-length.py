#!/usr/bin/env python3
"""Length limit for every code file, with a cap for each file already over it.

Usage:
    python3 scripts/file-length.py          # check every code file; exit 1 on any problem
    PR_BODY=... python3 scripts/file-length.py <base>   # also check caps added or raised vs base
    python3 scripts/file-length.py --caps   # print a CAPS block for the files over MAX_LINES

Reads the working tree: tracked files, and untracked files that git does not ignore. Only code
counts, by the same rule as the size label: `is_code` is loaded from scripts/pr-size.py, so its
SKIP_* settings decide here too. Binary files are left out.

Lines are counted as ESLint's `max-lines` counts them: \\r\\n, \\r, \\n, U+2028 and U+2029 end a line,
blank lines and comments count, and the empty piece after a final line break does not. An empty
file is 1 line.

A file over MAX_LINES fails unless CAPS holds its path. A capped file fails when it grows past its
cap, and also when it shrinks below it: the cap must come down to the new length, so caps only
ratchet down. A cap whose file is back within MAX_LINES, or gone, must be dropped.

With a base (a PR's base branch, such as origin/main), the check also compares CAPS with CAPS in
this script at the merge base of HEAD and base, read with `git show` and `ast.literal_eval`. When
the script is missing there, the PR is the one that adds it, and its starting caps need no items.
Otherwise every cap that is new, or higher than there, fails unless the PR_BODY environment
variable holds its item, one line per cap:

    - **Cap raised:** `src/big.py` 640 → 700: <why the file cannot split now>

The item gives the path in backticks, the cap at the merge base (`new` for an added cap), `→` (or
`->`), the cap now, and a reason after the colon. A cap that drops needs no item. Without a base
(a push to the default branch) only the working tree is checked. Items inside HTML comments
(`<!-- ... -->`) and fenced code blocks do not count: they never render.

CI runs this on every PR, with its base and body, and on every push to the default branch:
.github/workflows/file-length.yml. Exits 0 when every file passes, or after --caps; 1 on a failing
file or cap, or when git fails or CAPS at the merge base cannot be read; 2 on bad usage.
"""
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# --------------------------------------------------------------------------------------------
# Repo settings. Paths are repo-relative with forward slashes.
# --------------------------------------------------------------------------------------------

MAX_LINES = 500

# Each code file over MAX_LINES, capped at its length. Raising a cap needs the reason in the PR
# body; prefer splitting the file along a seam.
CAPS = {
    "eval/run_eval.py": 604,
    "eval/scoring.py": 653,
    "report/template.html": 1943,
    "src/bili_album/catalog.py": 629,
    "src/bili_album/library.py": 819,
    "src/bili_album/plan.py": 1488,
    "src/bili_album/review.py": 1482,
    "src/bili_album/web/static/index.html": 817,
    "tests/test_review.py": 541,
    "tests/test_rules.py": 624,
    "tests/test_web_auth.py": 563,
}


# --------------------------------------------------------------------------------------------
# The rest is generic.
# --------------------------------------------------------------------------------------------

LINE_BREAK = re.compile("\r\n|[\r\n\u2028\u2029]")
FENCE_OPENER = re.compile(r"(`{3,}|~{3,})(.*)")
CAP_ITEM = re.compile(r"[-*] \*\*Cap raised:\*\* `([^`]+)` (new|[0-9]+) (?:→|->) ([0-9]+): \S.*")


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
    return result.stdout


def load_is_code():
    """Return `is_code` from pr-size.py beside this script (its name has a hyphen, so no import)."""
    path = Path(__file__).resolve().with_name("pr-size.py")
    spec = importlib.util.spec_from_file_location("pr_size", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_code


def count_lines(data):
    lines = LINE_BREAK.split(data.decode("utf-8", "replace"))
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return len(lines)


def code_file_lines(root, is_code):
    """Return {path: lines} for every code file in the working tree."""
    listed = git("ls-files", "-z", cwd=root) + b"\0"
    listed += git("ls-files", "--others", "--exclude-standard", "-z", cwd=root)
    result = {}
    for raw in set(listed.split(b"\0")):
        path = raw.decode("utf-8", "replace")
        if not path or not is_code(path):
            continue
        try:
            data = Path(root, path).read_bytes()
        except OSError:  # Deleted, a nested repo's folder or a dangling symlink: nothing to count.
            continue
        if b"\0" not in data:
            result[path] = count_lines(data)
    return result


def problems(files):
    found = []
    for path, lines in files.items():
        if lines > MAX_LINES and path not in CAPS:
            found.append(f"{path}: {lines} lines, over the limit of {MAX_LINES}: split it along a seam, "
                         f"or cap it at {lines} in CAPS with the reason in the PR body")
    for path, cap in CAPS.items():
        lines = files.get(path)
        if lines is None:
            found.append(f"{path}: capped, but not a code file in the tree: drop its cap")
        elif lines <= MAX_LINES:
            found.append(f"{path}: {lines} lines, within the limit of {MAX_LINES}: drop its cap")
        elif lines > cap:
            found.append(f"{path}: {lines} lines, over its cap of {cap}: split it along a seam, "
                         f"or raise its cap with the reason in the PR body")
        elif lines < cap:
            found.append(f"{path}: {lines} lines, under its cap of {cap}: lower its cap to {lines}")
    return sorted(found)


def base_caps(base, root):
    """Return CAPS from this script at the merge base of HEAD and base; None when it is missing there."""
    merge_base = git("merge-base", "HEAD", base, cwd=root).decode("utf-8").strip()
    script = Path(__file__).resolve().relative_to(Path(root).resolve()).as_posix()
    try:
        source = git("show", f"{merge_base}:{script}", cwd=root).decode("utf-8")
    except GitError:  # The PR adds this script, so its starting caps are not raises.
        return None
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "CAPS" for t in node.targets):
            caps = ast.literal_eval(node.value)
            if isinstance(caps, dict):
                return caps
    raise ValueError(f"no literal CAPS dict in {script} at the merge base {merge_base[:7]}")


def raised_caps(body, before, after):
    """Return one problem for each cap added or raised from before to after without its item."""
    items = set()
    for line in rendered_lines(body):
        match = CAP_ITEM.fullmatch(line)
        if match:
            items.add((match.group(1), match.group(2), int(match.group(3))))
    found = []
    for path, cap in sorted(after.items()):
        old = before.get(path)
        if old is not None and cap <= old:
            continue
        if (path, "new" if old is None else str(old), cap) in items:
            continue
        change = f"adds a cap of {cap}" if old is None else f"raises its cap from {old} to {cap}"
        item = f"- **Cap raised:** `{path}` {'new' if old is None else old} → {cap}: <reason>"
        found.append(f"{path}: the PR {change}: split the file along a seam, or add `{item}` "
                     f"to the PR body")
    return found


def caps_block(files):
    over = sorted((path, lines) for path, lines in files.items() if lines > MAX_LINES)
    if not over:
        return "CAPS = {\n}"
    rows = "".join(f"    {json.dumps(path, ensure_ascii=False)}: {lines},\n" for path, lines in over)
    return "CAPS = {\n" + rows + "}"


def main(argv):
    if len(argv) > 1 or (argv and argv != ["--caps"] and argv[0].startswith("-")):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    base = argv[0] if argv and argv != ["--caps"] else None
    try:
        is_code = load_is_code()
        root = git("rev-parse", "--show-toplevel").decode("utf-8").strip()
        files = code_file_lines(root, is_code)
        before = base_caps(base, root) if base else None
    except (GitError, OSError, SyntaxError, ValueError) as error:
        print(f"file-length: {error}", file=sys.stderr)
        return 1
    if argv == ["--caps"]:
        print(caps_block(files))
        return 0
    found = problems(files)
    if before is not None:
        found += raised_caps(os.environ.get("PR_BODY", ""), before, CAPS)
    for line in found:
        print(line)
    if found:
        print(f"file-length: {len(found)} problem{'s' * (len(found) != 1)} in {len(files)} files checked")
        return 1
    print(f"file-length: ok, {len(files)} files checked, {len(CAPS)} capped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
