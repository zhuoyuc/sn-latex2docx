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


@cache
def pandoc_executable() -> str:
    """pandoc on PATH, else the binary bundled with ``pypandoc_binary`` (``uv sync --extra pandoc``)."""
    exe = shutil.which("pandoc")
    if exe:
        return exe
    try:
        import pypandoc

        return pypandoc.get_pandoc_path()
    except (ImportError, OSError):
        raise RuntimeError(
            "pandoc not found: install pandoc >= 3.0 (https://pandoc.org/installing.html) "
            "or the bundled build with `uv sync --extra pandoc`"
        ) from None


@cache
def pandoc_version(exe: str | None = None) -> tuple[int, ...]:
    out = subprocess.run([exe or pandoc_executable(), "--version"], capture_output=True, text=True, check=True).stdout
    first = out.splitlines()[0].split()[-1]
    return tuple(int(x) for x in first.split(".") if x.isdigit())


def resource_path(name: str) -> Path:
    return Path(str(resources.files("sn2docx") / "resources" / name))


def run_pandoc(tex: str, conv: Conversion, reference_doc: Path, out: Path, workdir: Path) -> None:
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
    log.debug("running %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=workdir)
    for line in (res.stderr or "").splitlines():
        if line.strip():
            log.warning("pandoc: %s", line.strip())
    if res.returncode != 0:
        raise RuntimeError(f"pandoc failed with exit code {res.returncode}")
