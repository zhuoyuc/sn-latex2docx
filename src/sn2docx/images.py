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



@cache
def search_extensions() -> tuple[str, ...]:
    """The extensions graphicx tries for ``\\includegraphics{name}`` (``\\Gin@extensions`` of its driver)."""
    from .latex.texdefs import tex_source

    src = tex_source("pdftex.def")
    exts = []
    for m in re.finditer(r"\\Gin@extensions\{([^}]*)\}", src):
        exts += re.findall(r"\.[A-Za-z0-9]+", m.group(1))
    return ("", *dict.fromkeys(exts))


@dataclass
class RasterImage:
    data: bytes
    ext: str  # png | jpeg
    width_px: int
    height_px: int


def resolve(source: str, search_dirs: list[Path]) -> Path | None:
    source = source.strip().strip('"')
    for d in search_dirs:
        for ext in search_extensions():
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


def _render(src: Path | bytes, dpi: int, page: int = 0, filetype: str = "pdf") -> RasterImage:
    """Rasterise a vector page (PDF, or SVG) with PyMuPDF."""
    import pymupdf

    doc = pymupdf.open(stream=src, filetype=filetype) if isinstance(src, bytes) else pymupdf.open(src, filetype=filetype)
    try:
        pg = doc[min(page, len(doc) - 1)]
        pix = pg.get_pixmap(dpi=dpi, alpha=False)
        return RasterImage(pix.tobytes("png"), "png", pix.width, pix.height)
    finally:
        doc.close()


def _eps_to_png(path: Path, dpi: int) -> RasterImage:
    gs = _ghostscript()
    if gs:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.png"
            cmd = [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dEPSCrop", "-sDEVICE=png16m",
                   f"-r{dpi}", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4", f"-sOutputFile={out}", str(path)]
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
                return _render(out.read_bytes(), dpi)
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


def load_image(path: Path, options: str | None, dpi: int) -> RasterImage:
    """PNG or JPEG for Word; vector formats are rendered at ``dpi`` (the template's resolution)."""
    ext = path.suffix.lower()
    page = 0
    if options:
        m = re.search(r"page\s*=\s*(\d+)", options)
        if m:
            page = int(m.group(1)) - 1
    if ext in (".pdf", ".svg"):
        return _render(path, dpi, page, ext[1:])
    if ext in (".eps", ".ps"):
        return _eps_to_png(path, dpi)
    return _raster_from_bytes(path.read_bytes(), keep_jpeg=True)
