"""Build the self-contained performance report page from the eval harness output.

    uv run python report/build_report.py [--results PATH] [--out PATH]

Reads ``results.json`` (written by the eval harness), inlines it as JSON into
``report/template.html`` and writes one HTML file with no external requests.
Every aggregate is computed in the page's own JavaScript from the per-case data,
so the scope and mode filters can recompute them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATE = HERE / "template.html"
PLACEHOLDER = "/*__RESULTS_JSON__*/"


def embed_json(data: object) -> str:
    """Serialise ``data`` so it is safe inside a ``<script type="application/json">`` block.

    ``<``, ``>`` and ``&`` are escaped as JSON unicode escapes, so no value can close
    the script element or open a comment; ``JSON.parse`` restores them exactly.
    """
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def clean(value: object) -> object:
    """Replace non-finite floats (NaN, inf) with None so the JSON stays valid."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def build(results_path: Path, out_path: Path) -> None:
    raw = results_path.read_text(encoding="utf-8")
    # The harness may write NaN/Infinity (Python's json default); accept and null them.
    data = clean(json.loads(raw))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError(f"{results_path}: expected an object with a 'cases' list")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"{TEMPLATE}: expected exactly one {PLACEHOLDER} placeholder")
    html = template.replace(PLACEHOLDER, embed_json(data))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the bili-album performance report page.")
    parser.add_argument("--results", type=Path, default=REPO / "eval/results/results.json",
                        help="harness output (default: eval/results/results.json)")
    parser.add_argument("--out", type=Path, default=REPO / "eval/results/report.html",
                        help="page to write (default: eval/results/report.html)")
    args = parser.parse_args(argv)
    try:
        build(args.results, args.out)
    except (OSError, ValueError) as exc:
        print(f"build_report: {exc}", file=sys.stderr)
        return 1
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
