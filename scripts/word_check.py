"""Open a .docx in Microsoft Word (Windows, via COM) to verify and render it.

* checks that Word opens the file without repair,
* compares every field's cached result with the value Word computes after
  updating all fields (SEQ numbering, REF cross-references),
* exports a PDF and, optionally, PNG page images for visual review.

Usage:  uv run --extra word python scripts/word_check.py output/x.docx [--png DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def check(docx: Path, pdf: Path | None, png_dir: Path | None, dpi: int = 80) -> dict:
    import pythoncom
    import win32com.client as win32

    pythoncom.CoInitialize()
    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    report: dict = {"file": str(docx), "fields": 0, "mismatches": [], "errors": []}
    try:
        # work on a copy so updating fields never touches the original
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / docx.name
            copy.write_bytes(docx.read_bytes())
            doc = word.Documents.Open(str(copy), ConfirmConversions=False, ReadOnly=False, AddToRecentFiles=False,
                                      OpenAndRepair=False, NoEncodingDialog=True)
            before = []
            for f in doc.Fields:
                before.append((f.Code.Text.strip(), f.Result.Text))
            report["fields"] = len(before)
            doc.Fields.Update()
            for (code, old), f in zip(before, doc.Fields):
                new = f.Result.Text
                if old != new:
                    report["mismatches"].append({"code": code, "cached": old, "word": new})
                if "Error!" in new:
                    report["errors"].append({"code": code, "word": new})
            report["pages"] = doc.ComputeStatistics(2)  # wdStatisticPages
            report["paragraphs"] = doc.Paragraphs.Count
            report["tables"] = doc.Tables.Count
            report["inline_shapes"] = doc.InlineShapes.Count
            report["equations"] = doc.OMaths.Count
            if pdf:
                doc.ExportAsFixedFormat(str(pdf.resolve()), 17)  # wdExportFormatPDF
            doc.Close(SaveChanges=False)
    finally:
        word.Quit()
    if pdf and png_dir:
        import pymupdf

        png_dir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf) as d:
            for i, page in enumerate(d, 1):
                page.get_pixmap(dpi=dpi).save(png_dir / f"page-{i:02d}.png")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("docx", type=Path)
    ap.add_argument("--pdf", type=Path, help="PDF output (default: next to the docx)")
    ap.add_argument("--png", type=Path, help="directory for PNG page renders")
    ap.add_argument("--dpi", type=int, default=80)
    args = ap.parse_args()
    pdf = args.pdf or args.docx.with_suffix(".pdf")
    rep = check(args.docx.resolve(), pdf, args.png, args.dpi)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    return 1 if rep["mismatches"] or rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
