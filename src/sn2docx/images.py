"""Locating ``\\includegraphics`` targets and converting them to formats Word embeds."""

from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

SEARCH_EXTS = ("", ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps", ".tif", ".tiff", ".gif", ".bmp")
DPI = 300


@dataclass
class RasterImage:
    data: bytes
    ext: str  # png | jpeg
    width_px: int
    height_px: int


def resolve(source: str, search_dirs: list[Path]) -> Path | None:
    source = source.strip().strip('"')
    for d in search_dirs:
        for ext in SEARCH_EXTS:
            p = (d / (source + ext)) if ext else (d / source)
            if p.is_file():
                return p
    return None


@cache
def _ghostscript() -> str | None:
    for name in ("gswin64c", "gswin32c", "gs", "mgs"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def _pdf_to_png(pdf: Path | bytes, page: int = 0) -> RasterImage:
    import pymupdf

    doc = pymupdf.open(stream=pdf, filetype="pdf") if isinstance(pdf, bytes) else pymupdf.open(pdf)
    try:
        pg = doc[min(page, len(doc) - 1)]
        pix = pg.get_pixmap(dpi=DPI, alpha=False)
        return RasterImage(pix.tobytes("png"), "png", pix.width, pix.height)
    finally:
        doc.close()


def _eps_to_png(path: Path) -> RasterImage:
    gs = _ghostscript()
    if gs:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            cmd = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop", "-sDEVICE=png16m",
                   f"-r{DPI}", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4", f"-sOutputFile={out}", str(path)]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0 and out.is_file():
                return _raster_from_bytes(out.read_bytes())
            log.warning("ghostscript failed on %s: %s", path, res.stderr.decode(errors="replace")[:200])
    epstopdf = _epstopdf()
    if epstopdf:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.pdf"
            res = subprocess.run([epstopdf, str(path), f"--outfile={out}"], capture_output=True)
            if res.returncode == 0 and out.is_file():
                return _pdf_to_png(out.read_bytes())
    raise RuntimeError(f"cannot convert {path}: no Ghostscript (gs/gswin64c/mgs) or epstopdf available")


@cache
def _epstopdf() -> str | None:
    return shutil.which("epstopdf")


def _raster_from_bytes(data: bytes, keep_jpeg: bool = False) -> RasterImage:
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    w, h = im.size
    if keep_jpeg and im.format == "JPEG":
        return RasterImage(data, "jpeg", w, h)
    if im.format == "PNG":
        return RasterImage(data, "png", w, h)
    buf = io.BytesIO()
    if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        im = im.convert("RGBA" if "A" in im.mode else "RGB")
    im.save(buf, "PNG")
    return RasterImage(buf.getvalue(), "png", w, h)


def load_image(path: Path, options: str | None = None) -> RasterImage:
    ext = path.suffix.lower()
    page = 0
    if options:
        m = re.search(r"page\s*=\s*(\d+)", options)
        if m:
            page = int(m.group(1)) - 1
    if ext == ".pdf":
        return _pdf_to_png(path, page)
    if ext in (".eps", ".ps"):
        return _eps_to_png(path)
    return _raster_from_bytes(path.read_bytes(), keep_jpeg=True)


UNIT_IN = {"in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4, "pt": 1 / 72.27, "bp": 1 / 72.0, "pc": 12 / 72.27}


def requested_width(options: str | None, text_width_in: float) -> float | None:
    """Width asked for in ``\\includegraphics[width=...]``, in inches (None if not given)."""
    if not options:
        return None
    m = re.search(r"(?<![a-z])width\s*=\s*([0-9.]*)\s*\\(textwidth|linewidth|columnwidth|hsize)", options)
    if m:
        f = float(m.group(1)) if m.group(1) else 1.0
        return f * text_width_in
    m = re.search(r"(?<![a-z])width\s*=\s*([0-9.]+)\s*(in|cm|mm|pt|bp|pc)", options)
    if m:
        return float(m.group(1)) * UNIT_IN[m.group(2)]
    return None  # scale= and height= keep the default width
