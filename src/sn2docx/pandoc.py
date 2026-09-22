"""Running pandoc on the preprocessed LaTeX."""

from __future__ import annotations

import logging
import shutil
import subprocess
from functools import cache
from importlib import resources
from pathlib import Path

from .model import Conversion

log = logging.getLogger(__name__)

MIN_PANDOC = (3, 0)


def pandoc_executable() -> str:
    exe = shutil.which("pandoc")
    if not exe:
        raise RuntimeError("pandoc not found on PATH; install pandoc >= 3.0 (https://pandoc.org/installing.html)")
    return exe


@cache
def pandoc_version(exe: str | None = None) -> tuple[int, ...]:
    out = subprocess.run([exe or pandoc_executable(), "--version"], capture_output=True, text=True, check=True).stdout
    first = out.splitlines()[0].split()[-1]
    return tuple(int(x) for x in first.split(".") if x.isdigit())


def resource_path(name: str) -> Path:
    return Path(str(resources.files("sn2docx") / "resources" / name))


def run_pandoc(tex: str, conv: Conversion, reference_doc: Path, out: Path, workdir: Path, csl: Path | None = None) -> None:
    exe = pandoc_executable()
    if pandoc_version(exe) < MIN_PANDOC:
        raise RuntimeError("pandoc >= 3.0 is required")
    workdir = workdir.resolve()
    out = out.resolve()
    src = workdir / "pandoc-input.tex"
    src.write_text(tex, encoding="utf-8")
    cmd = [
        exe, str(src), "-f", "latex", "-t", "docx", "-o", str(out),
        "--reference-doc", str(reference_doc.resolve()),
        "--resource-path", str(conv.source_dir.resolve()),
    ]
    if conv.bibliography and not conv.manual_bibliography:
        style = csl or resource_path(f"csl/{conv.citation_mode}.csl")
        cmd += ["--citeproc", "--csl", str(Path(style).resolve()), "-M", "link-citations=true", "-M", "reference-section-title=References"]
        for b in conv.bibliography:
            cmd += ["--bibliography", str(b.resolve())]
    log.debug("running %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=workdir)
    for line in (res.stderr or "").splitlines():
        if line.strip():
            log.warning("pandoc: %s", line.strip())
    if res.returncode != 0:
        raise RuntimeError(f"pandoc failed with exit code {res.returncode}")
