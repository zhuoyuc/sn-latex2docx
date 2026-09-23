"""End-to-end conversion: LaTeX manuscript -> house-style .docx."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .docx.package import Package, make_reference_doc
from .docx.postprocess import Options, postprocess
from .latex.preprocess import preprocess
from .pandoc import resource_path, run_pandoc

log = logging.getLogger(__name__)


@dataclass
class Result:
    output: Path
    warnings: list[str] = field(default_factory=list)
    bibstyle: str | None = None  # BibTeX style the references were formatted with


def default_template() -> Path:
    return resource_path("template.docx")


def convert(
    source: Path,
    output: Path | None = None,
    template: Path | None = None,
    figure_width: float | None = 3.25,
    date: str | None = None,
    keep_intermediate: Path | None = None,
    tex_dirs: tuple[Path, ...] = (),
) -> Result:
    """Convert ``source`` (an sn-jnl .tex file) and return the output path plus warnings."""
    source = Path(source).resolve()
    output = Path(output) if output else source.with_suffix(".docx")
    template = Path(template) if template else default_template()
    warnings: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record):  # pandoc and preprocessor warnings end up in the result
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    handler = _Collector()
    root = logging.getLogger("sn2docx")
    root.addHandler(handler)
    try:
        pre = preprocess(source, tuple(tex_dirs))
        with tempfile.TemporaryDirectory(prefix="sn2docx-") as tmp:
            work = Path(tmp)
            tpl = Package.open(template)
            ref = make_reference_doc(tpl, work / "reference.docx")
            raw = work / "pandoc.docx"
            run_pandoc(pre.pandoc_tex, pre.conversion, ref, raw, work)
            if keep_intermediate:
                keep_intermediate.mkdir(parents=True, exist_ok=True)
                shutil.copy(work / "pandoc-input.tex", keep_intermediate / "pandoc-input.tex")
                shutil.copy(raw, keep_intermediate / "pandoc.docx")
            opts = Options(figure_width=figure_width, date=date)
            postprocess(raw, output, pre.conversion, tpl, opts)
    finally:
        root.removeHandler(handler)
    return Result(output, list(dict.fromkeys(warnings)), pre.conversion.bibstyle)
