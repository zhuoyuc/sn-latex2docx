"""Top-level LaTeX stage: turn an sn-jnl manuscript into pandoc input plus a registry."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..model import Conversion, Heading, Registry, marker
from .extras import map_math_aware, read_bib_authors, rewrite_packages, rewrite_textual_citations
from .frontmatter import extract_frontmatter
from .scan import (
    find_commands,
    find_env,
    is_escaped,
    map_nonverbatim,
    parse_command_args,
    read_optional,
    replace_commands,
)
from .source import collect_macros, expand_macros, load_manuscript
from .transform import Transformer, replace_refs

log = logging.getLogger(__name__)

# natbib/sn-jnl reference-style class options that produce author-year citations
_AUTHOR_YEAR_OPTIONS = {"sn-mathphys-ay", "sn-vancouver-ay", "sn-apa", "sn-basic", "sn-chicago"}
_NUMERIC_OPTIONS = {"sn-mathphys-num", "sn-vancouver-num", "sn-aps", "sn-nature", "Numbered"}

_THEOREM_STYLES = {"thmstyleone": "plain", "thmstyletwo": "plain", "thmstylethree": "definition"}

# Body commands rewritten in one pass before pandoc sees the text: layout commands
# are dropped; \footnotemark/\footnotetext pairs outside tables keep the text as a footnote.
_BODY_COMMANDS = {
    **{name: (spec, lambda a: "") for name, spec in (
        ("maketitle", ""), ("backmatter", ""), ("frontmatter", ""), ("mainmatter", ""), ("raggedbottom", ""),
        ("bigskip", ""), ("medskip", ""), ("smallskip", ""), ("noindent", ""), ("clearpage", ""),
        ("cleardoublepage", ""), ("newpage", ""), ("pagebreak", "o"), ("nopagebreak", "o"), ("FloatBarrier", ""),
        ("unskip", ""), ("centering", ""), ("lstset", "m"), ("vspace", "sm"), ("vskip", ""), ("linenumbers", ""),
        ("nolinenumbers", ""), ("bibliographystyle", "m"), ("printbibliography", "o"), ("tableofcontents", ""),
        ("hypersetup", "m"), ("graphicspath", "m"), ("setcounter", "mm"), ("addtocounter", "mm"),
        ("footnotemark", "o"),
    )},
    "footnotetext": ("om", lambda a: "\\footnote{" + (a[1] or "") + "}"),
}


@dataclass
class PreprocessResult:
    pandoc_tex: str
    conversion: Conversion


def parse_theorems(preamble: str) -> tuple[dict[str, tuple[str | None, str]], list[str]]:
    """Return env -> (counter, title) and the pandoc preamble lines for theorems.

    pandoc knows amsthm's ``proof`` environment natively, so it needs no entry.
    """
    envs: dict[str, tuple[str | None, str]] = {}
    lines: list[str] = []
    pat = re.compile(r"\\(newtheorem\*?|theoremstyle|declaretheorem)(?![A-Za-z@])")
    for m in pat.finditer(preamble):
        if is_escaped(preamble, m.start()):
            continue
        cmd = m.group(1)
        if cmd == "theoremstyle":
            args, _ = parse_command_args(preamble, m.end(), "m")
            style = _THEOREM_STYLES.get((args[0] or "").strip(), (args[0] or "plain").strip())
            if style not in ("plain", "definition", "remark"):
                style = "plain"
            lines.append(f"\\theoremstyle{{{style}}}")
        elif cmd == "newtheorem*":
            args, _ = parse_command_args(preamble, m.end(), "mm")
            envs[(args[0] or "").strip()] = (None, args[1] or "")
            lines.append(f"\\newtheorem*{{{args[0]}}}{{{args[1]}}}")
        elif cmd == "newtheorem":
            args, _ = parse_command_args(preamble, m.end(), "momo")
            name = (args[0] or "").strip()
            shared = (args[1] or "").strip() or None
            counter = envs[shared][0] if shared and shared in envs else (shared or name)
            envs[name] = (counter, args[2] or "")
            lines.append(f"\\newtheorem{{{name}}}" + (f"[{shared}]" if shared else "") + f"{{{args[2]}}}")
    return envs, lines


def citation_mode(options: list[str], style: str | None) -> str:
    opts = set(options)
    if style:
        s = style.lower()
        if s.endswith("-ay") or "apa" in s or "chicago" in s or "natbib" in s or "harv" in s:
            return "author-year"
        if s in ("sn-basic",):
            return "numeric" if "Numbered" in opts else "author-year"
        return "numeric"
    if "Numbered" in opts or opts & _NUMERIC_OPTIONS:
        return "numeric"
    if opts & _AUTHOR_YEAR_OPTIONS:
        return "author-year"
    return "numeric"


def _bib_files(text: str, base: Path) -> tuple[list[Path], str]:
    files: list[Path] = []
    for _, _, args in find_commands(text, "bibliography", "addbibresource", spec="om"):
        for name in (args[1] or "").split(","):
            name = name.strip()
            if not name:
                continue
            p = base / name
            if p.suffix.lower() != ".bib":
                p = p.with_name(p.name + ".bib")
            if p.is_file():
                files.append(p)
            else:
                log.warning("bibliography file not found: %s", p)
    drop = ("om", lambda a: "")
    return files, replace_commands(text, {"bibliography": drop, "addbibresource": drop})


_BBL_WRAPPERS = re.compile(
    r"\\(bibinfo|bibfield)\s*\{[^{}]*\}|\\(?:bibnamefont|bibfnamefont|citenamefont|textbibinfo|bibsnm|binits|bsnm|bfnm|"
    r"bauthor|beditor|bparticle|btitle|bjtitle|bvolume|bissue|bfpage|blpage|byear|bdoi|bpublisher|blocation|bbtitle|"
    r"bedition|bcomment|bptitle|bptok|bpages|bnumber|bseries|burl|binstitution|bschool|bnote|borganization|"
    r"bmonth|bday|bsuffix|bcollab|betal|bchapter|bconfname|bconflocation|bconfdate|beprint|bisbn|bissn|bparticle|"
    r"bprefix|bsertitle|bvolumetitle|bstitle|bstnum|bpublishername)(?![A-Za-z@])"
)


def _clean_bibitem(text: str) -> str:
    text = re.sub(r"\\begin\{b[a-z]*\}|\\end\{b[a-z]*\}", "", text)
    text = re.sub(r"\\(newblock|BibitemOpen|bibAnnoteFile\s*\{[^}]*\}|bibAnnote\s*\{[^}]*\}\{[^}]*\})", " ", text)
    text = re.sub(r"\\BibitemShut\s*\{[^}]*\}", "", text)
    text = re.sub(r"\\natexlab\s*\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\doi\s*\{([^}]*)\}", r"doi: \\url{https://doi.org/\1}", text)
    text = _BBL_WRAPPERS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _manual_bibliography(body: str, reg: Registry) -> tuple[str, list[tuple[str, str]]]:
    env = find_env(body, "thebibliography")
    if env is None:
        return body, []
    inner = env.body(body)
    _, i = parse_command_args(inner, 0, "m")
    inner = inner[i:]
    items: list[tuple[str, str]] = []
    for chunk in re.split(r"\\bibitem(?![A-Za-z@])", inner)[1:]:
        opt, j = read_optional(chunk, 0)
        args, j = parse_command_args(chunk, j, "m")
        key = (args[0] or "").strip()
        items.append((key, _clean_bibitem(chunk[j:])))
        reg.bib_labels[key] = opt or ""
        reg.bibitems.append(key)
    return body[: env.start] + body[env.end :], items


_CITE_COMMANDS = ("cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "citeyearpar", "Cite",
                  "Citep", "Citet", "parencite", "textcite", "autocite", "citenum")


def _manual_citations(text: str, reg: Registry) -> str:
    """Citations against a ``thebibliography`` list become CITE markers."""

    def handler(name: str):
        def fn(args: list[str | None]) -> str:
            keys = [k.strip() for k in (args[3] or "").split(",") if k.strip()]
            reg.cites.append((name.lower(), keys))
            return marker("CITE", len(reg.cites) - 1)

        return "soom", fn

    return replace_commands(text, {name: handler(name) for name in _CITE_COMMANDS})


def _replace_refs(text: str, reg: Registry) -> str:
    return map_math_aware(text, lambda t: replace_refs(t, reg), lambda m: replace_refs(m, reg, math=True))


def preprocess(path: Path) -> PreprocessResult:
    ms = load_manuscript(path)
    preamble, macros = collect_macros(ms.preamble)
    # definitions made inside the body (rare) are honoured too
    body, macros = collect_macros(ms.body, macros)
    preamble = expand_macros(preamble, macros)
    body = expand_macros(body, macros)

    front, preamble, body = extract_frontmatter(preamble, body)

    graphics_paths = [ms.path.parent]
    for _, _, args in find_commands(preamble + body, "graphicspath"):
        for g in re.findall(r"\{([^}]*)\}", args[0] or ""):
            graphics_paths.append((ms.path.parent / g).resolve())

    style = None
    for _, _, args in find_commands(preamble + body, "bibliographystyle"):
        style = (args[0] or "").strip()
    bib_files, body = _bib_files(body, ms.path.parent)
    more, preamble = _bib_files(preamble, ms.path.parent)
    bib_files += more
    mode = citation_mode(ms.documentclass_options, style)

    reg = Registry()
    body, bib_items = _manual_bibliography(body, reg)
    manual = bool(bib_items) and not bib_files

    # packages pandoc handles poorly: siunitx, mhchem, natbib textual cites in numeric mode
    body = rewrite_packages(body)
    front.abstract = rewrite_packages(front.abstract)
    front.title = rewrite_packages(front.title)
    if mode == "numeric" and bib_files and not manual:
        authors = read_bib_authors(bib_files)
        body = map_nonverbatim(body, lambda s: rewrite_textual_citations(s, authors))
        front.abstract = rewrite_textual_citations(front.abstract, authors)

    theorems, theorem_lines = parse_theorems(preamble)
    body = Transformer(reg, theorems).walk(body)

    body = _replace_refs(body, reg)
    if manual:
        body = map_nonverbatim(body, lambda s: _manual_citations(s, reg))
        front.abstract = _manual_citations(front.abstract, reg)
    body = map_nonverbatim(body, lambda s: replace_commands(s, _BODY_COMMANDS))

    if manual:
        reg.headings.append(Heading(1, False, False, ""))
        parts = [f"\n\n\\section{{{marker('HEAD', len(reg.headings) - 1)}References}}\n\n"]
        for n, (_, text) in enumerate(bib_items):
            parts.append(f"{marker('BIB', n)} {text}\n\n")
        body += "".join(parts)

    fm_parts = []
    if front.title:
        fm_parts.append(f"{marker('FMTITLE')} {front.title}\n\n")
    for i, a in enumerate(front.authors):
        fm_parts.append(f"{marker('FMAUTHOR', i)} {a.name}\n\n")
    for i, a in enumerate(front.affiliations):
        fm_parts.append(f"{marker('FMAFFIL', i)} {a.text}\n\n")
    notes = list(dict.fromkeys(a.equal for a in front.authors if a.equal))
    for i, n in enumerate(notes):
        fm_parts.append(f"{marker('FMNOTE', i)} {n}\n\n")
    if front.abstract:
        fm_parts.append(f"{marker('FMABSTRACT')}\n\n{_replace_refs(front.abstract, reg)}\n\n")
    if front.keywords:
        fm_parts.append(f"{marker('FMKEYWORDS')} " + "; ".join(front.keywords) + "\n\n")
    fm_parts.append(f"{marker('FMEND')}\n\n")

    math_ops = [preamble[s:e] for s, e, _ in find_commands(preamble, "DeclareMathOperator", spec="smm")]
    pandoc_preamble = "\n".join(["\\documentclass{article}", *math_ops, *theorem_lines])
    pandoc_tex = pandoc_preamble + "\n\\begin{document}\n" + "".join(fm_parts) + body + "\n\\end{document}\n"

    conv = Conversion(
        front=front,
        registry=reg,
        source_dir=ms.path.parent,
        graphics_paths=graphics_paths,
        bibliography=bib_files,
        citation_mode=mode,
        manual_bibliography=manual,
        equal_notes=notes,
    )
    return PreprocessResult(pandoc_tex, conv)
