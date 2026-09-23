"""Command-line interface: ``sn2docx manuscript.tex [-o out.docx]``."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from . import __version__
from .images import TEX_UNITS, length_in
from .pipeline import convert


def _width(value: str) -> float | None:
    """Figure width in inches from "3.25in", "8cm", "90mm", "3.25" or "source"."""
    v = value.lower().strip()
    if v in ("source", "latex", "auto"):
        return None
    m = re.fullmatch(r"([0-9.]+)\s*(" + "|".join(TEX_UNITS) + ")?", v)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid width: {value}")
    return length_in(float(m.group(1)), m.group(2) or "in")


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
    ap.add_argument("--tex-dir", type=Path, action="append", default=[], metavar="DIR",
                    help="folder with the document class and .bst files when they are neither next to the "
                    "manuscript nor installed in the TeX distribution (repeatable)")
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
        res = convert(args.source, args.output, args.template, args.figure_width, args.date,
                      args.keep_intermediate, tuple(args.tex_dir))
    except Exception as exc:  # report cleanly on the command line
        logging.getLogger("sn2docx").debug("conversion failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        style = f"{res.bibstyle}.bst, " if res.bibstyle else ""
        print(f"wrote {res.output} ({style}{len(res.warnings)} warning(s))")
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
