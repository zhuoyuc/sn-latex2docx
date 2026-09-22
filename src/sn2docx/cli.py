"""Command-line interface: ``sn2docx manuscript.tex [-o out.docx]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .pipeline import convert


def _width(value: str) -> float | None:
    if value.lower() in ("source", "latex", "auto"):
        return None
    v = value.lower().strip()
    factor = 1.0
    if v.endswith("cm"):
        factor, v = 1 / 2.54, v[:-2]
    elif v.endswith("in"):
        v = v[:-2]
    return float(v) * factor


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sn2docx",
        description="Convert a Springer Nature (sn-jnl) LaTeX manuscript into a Word document "
        "that follows the house Word template.",
    )
    ap.add_argument("source", type=Path, help="main .tex file of the manuscript")
    ap.add_argument("-o", "--output", type=Path, help="output .docx (default: next to the source)")
    ap.add_argument("--template", type=Path, help="Word template to take styles/header/footer from "
                    "(default: the bundled house template)")
    ap.add_argument("--figure-width", type=_width, default=3.25, metavar="W",
                    help="uniform figure width, e.g. 3.25in or 8cm; 'source' keeps the LaTeX widths (default: 3.25in)")
    ap.add_argument("--date", help="date shown in the page header (default: \\date or today)")
    ap.add_argument("--csl", type=Path, help="custom CSL style for the bibliography")
    ap.add_argument("--keep-intermediate", type=Path, metavar="DIR",
                    help="write the pandoc input and raw pandoc output to DIR for debugging")
    ap.add_argument("--check", action="store_true",
                    help="validate the generated document (fields, bookmarks, relationships, numbering)")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.ERROR if args.quiet else logging.WARNING, format="%(levelname)s: %(message)s")
    if not args.source.is_file():
        print(f"error: {args.source} does not exist", file=sys.stderr)
        return 2
    try:
        res = convert(args.source, args.output, args.template, args.figure_width, args.date, args.csl,
                      args.keep_intermediate)
    except Exception as exc:  # report cleanly on the command line
        logging.getLogger("sn2docx").debug("conversion failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"wrote {res.output} ({res.citation_mode} citations, {len(res.warnings)} warning(s))")
    if args.check:
        from .docx.verify import inspect

        summary = inspect(res.output)
        for p in summary.problems:
            print(f"check: {p}", file=sys.stderr)
        if not args.quiet:
            print(f"check: {len(summary.fields)} fields, {len(summary.bookmarks)} bookmarks, "
                  f"{summary.equations} equations, {summary.images} images, {summary.tables} tables, "
                  f"{len(summary.problems)} problem(s)")
        return 1 if summary.problems else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
