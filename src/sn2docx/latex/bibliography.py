"""Reference lists produced by BibTeX with the manuscript's own ``.bst``.

The converter does not format references. It writes an ``.aux`` file with the cited
keys (in citation order, as LaTeX would), runs ``bibtex`` with the style the
manuscript or its document class selects, and reads the resulting ``.bbl`` like a
``thebibliography`` environment typed into the manuscript. The markup macros the
``.bbl`` uses are defined in the ``.bbl`` itself or in the document class.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from . import texdefs
from .scan import find_env, map_nonverbatim, parse_command_args, read_optional, replace_commands, strip_comments
from .source import MacroTable, collect_macros, expand_macros

log = logging.getLogger(__name__)

CITE_COMMANDS = ("cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "citeyearpar", "Cite",
                 "Citep", "Citet", "Citealp", "Citealt", "Citeauthor", "parencite", "textcite", "autocite", "citenum")


@dataclass
class Bibliography:
    items: list[tuple[str, str]] = field(default_factory=list)  # (key, entry LaTeX, macros expanded)
    labels: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (author LaTeX, year) from \bibitem[...]


def cited_keys(body: str) -> list[str]:
    """Citation keys in order of first citation (``\\nocite`` included)."""
    keys: list[str] = []

    def note(args) -> str:
        for k in (args[-1] or "").split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
        return ""

    table = {name: ("soom", note) for name in CITE_COMMANDS}
    table["nocite"] = ("m", note)
    map_nonverbatim(body, lambda s: replace_commands(s, table))
    return keys


def run_bibtex(keys: list[str], style: str, bib_files: list[Path], search_dirs: tuple[Path, ...]) -> str | None:
    """``.bbl`` text for ``keys`` formatted by ``style`` (a ``.bst`` name), or None on failure."""
    exe = shutil.which("bibtex")
    if exe is None:
        log.warning("bibtex not found: the reference list needs a TeX distribution")
        return None
    bst = texdefs.tex_path(f"{style}.bst", tuple(search_dirs))
    if bst is None:
        log.warning("bibliography style %s.bst not found", style)
        return None
    with tempfile.TemporaryDirectory(prefix="sn2docx-bib-") as tmp:
        aux = [f"\\citation{{{k}}}" for k in keys]
        aux.append(f"\\bibstyle{{{bst.resolve().with_suffix('').as_posix()}}}")
        aux.append("\\bibdata{" + ",".join(b.resolve().with_suffix("").as_posix() for b in bib_files) + "}")
        (Path(tmp) / "refs.aux").write_text("\n".join(aux) + "\n", encoding="utf-8")
        res = subprocess.run([exe, "refs"], cwd=tmp, capture_output=True, text=True, errors="replace")
        for line in res.stdout.splitlines():
            if line.startswith(("Warning--", "I couldn't", "I found no", "Repeated entry")):
                log.warning("bibtex: %s", line.strip())
        bbl = Path(tmp) / "refs.bbl"
        return bbl.read_text(encoding="utf-8", errors="replace") if bbl.is_file() else None


@cache
def _known_to_pandoc(names: tuple[str, ...]) -> set[str]:
    """The commands among ``names`` that pandoc's LaTeX reader already interprets."""
    from ..pandoc import pandoc_executable

    if not names:
        return set()
    probe = "\n\n".join(f"\\{n}{{sntwodocxprobe}}" for n in names)
    res = subprocess.run([pandoc_executable(), "-f", "latex", "-t", "plain"], input=probe,
                         capture_output=True, text=True, encoding="utf-8")
    paragraphs = res.stdout.strip().split("\n\n") if res.returncode == 0 else []
    return {n for n, out in zip(names, paragraphs) if "sntwodocxprobe" in out}


def _label(label: str) -> tuple[str, str]:
    """natbib label -> (author, year): ``\\citeauthoryear{A}{Y}`` or ``A(Y)Long``."""
    m = re.search(r"\\citeauthoryear(?![A-Za-z@])", label)
    if m:
        args, _ = parse_command_args(label, m.end(), "mmo")
        return (args[0] or "").strip(), (args[1] or "").strip()
    m = re.match(r"\s*(.*?)\((.*?)\)(.*)$", label, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


def parse_thebibliography(text: str, extra: MacroTable | None = None) -> Bibliography | None:
    """Entries of a ``thebibliography`` environment (typed in the manuscript or from a ``.bbl``)."""
    text = strip_comments(text)
    env = find_env(text, "thebibliography")
    if env is None:
        return None
    inner = env.body(text)
    _, i = parse_command_args(inner, 0, "m")
    inner = inner[i:]
    first = re.search(r"\\bibitem(?![A-Za-z@])", inner)
    header = inner[: first.start()] if first else inner
    _, macros = collect_macros(header, MacroTable(dict((extra or MacroTable()).commands), dict((extra or MacroTable()).environments)))
    # \providecommand only defines what is not defined yet: where pandoc already knows a
    # command (e.g. \url from hyperref), its own handling stays in force
    provided = re.findall(r"\\providecommand\s*\{?\\([A-Za-z@]+)", header)
    for name in _known_to_pandoc(tuple(provided)):
        macros.commands.pop(name, None)
    wrapper_envs = [n for n in macros.commands]  # \begin{barticle} with \barticle defined: a plain wrapper
    out = Bibliography()
    for chunk in re.split(r"\\bibitem(?![A-Za-z@])", inner)[1:]:
        opt, j = read_optional(chunk, 0)
        args, j = parse_command_args(chunk, j, "m")
        key = (args[0] or "").strip()
        entry = chunk[j:]
        entry = re.sub(r"\\(begin|end)\s*\{(" + "|".join(map(re.escape, wrapper_envs)) + r")\}", "", entry) if wrapper_envs else entry
        entry = expand_macros(entry, macros)
        out.items.append((key, re.sub(r"\s+", " ", entry).strip()))
        out.labels[key] = _label(opt or "")
    return out
