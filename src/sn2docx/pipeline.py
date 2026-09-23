"""End-to-end conversion: LaTeX manuscript -> house-style .docx."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .docx.package import Package, make_reference_doc
from .docx.postprocess import postprocess
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


def convert(source: Path, output: Path | None = None) -> Result:
    """Convert ``source`` (an sn-jnl .tex file in its complete template folder) to ``output``.

    The output defaults to ``source`` with a .docx suffix. Everything else comes from the
    manuscript, its folder, the TeX installation and the bundled Word template.
    """
    source = Path(source).resolve()
    output = Path(output) if output else source.with_suffix(".docx")
    warnings: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record):  # pandoc and preprocessor warnings end up in the result
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    handler = _Collector()
    root = logging.getLogger("sn2docx")
    root.addHandler(handler)
    try:
        pre = preprocess(source)
        with tempfile.TemporaryDirectory(prefix="sn2docx-") as tmp:
            work = Path(tmp)
            tpl = Package.open(default_template())
            ref = make_reference_doc(tpl, work / "reference.docx")
            raw = work / "pandoc.docx"
            run_pandoc(pre.pandoc_tex, pre.conversion, ref, raw, work)
            postprocess(raw, output, pre.conversion, tpl)
    finally:
        root.removeHandler(handler)
    return Result(output, list(dict.fromkeys(warnings)), pre.conversion.bibstyle)
