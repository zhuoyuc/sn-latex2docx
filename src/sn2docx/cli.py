"""Command-line interface: ``sn2docx manuscript.tex [out.docx]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .docx.verify import inspect
from .pipeline import convert


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sn2docx",
        description="Convert a Springer Nature (sn-jnl) LaTeX manuscript into a Word document "
        "that follows the house Word template. The manuscript's folder holds the complete "
        "sn-jnl kit (class, .bst, figures, .bib), as in Springer Nature's template zip.",
    )
    ap.add_argument("source", type=Path, help="main .tex file of the manuscript")
    ap.add_argument("output", type=Path, nargs="?", help="output .docx (default: next to the source)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    if not args.source.is_file():
        print(f"error: {args.source} does not exist", file=sys.stderr)
        return 2
    try:
        res = convert(args.source, args.output)
    except Exception as exc:  # report cleanly on the command line
        logging.getLogger("sn2docx").debug("conversion failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    style = f"{res.bibstyle}.bst, " if res.bibstyle else ""
    print(f"wrote {res.output} ({style}{len(res.warnings)} warning(s))")
    # the structural check runs on every conversion
    summary = inspect(res.output)
    for p in summary.problems:
        print(f"check: {p}", file=sys.stderr)
    print(f"check: {len(summary.fields)} fields, {len(summary.bookmarks)} bookmarks, "
          f"{summary.equations} equations, {summary.images} images, {summary.tables} tables, "
          f"{len(summary.problems)} problem(s)")
    return 1 if summary.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
