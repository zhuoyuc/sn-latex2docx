"""Top-level LaTeX stage: turn an sn-jnl manuscript into pandoc input plus a registry."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..model import Conversion, Heading, Registry, TheoremSpec, marker
from . import texdefs
from .bibliography import CITE_COMMANDS, cited_keys, parse_thebibliography, run_bibtex
from .extras import map_math_aware, rewrite_packages
from .frontmatter import extract_frontmatter
from .scan import (
    find_commands,
    find_env,
    is_escaped,
    latex_to_plain,
    map_nonverbatim,
    parse_command_args,
    replace_commands,
)
from .source import MacroTable, collect_macros, expand_macros, load_manuscript
from .texdefs import _split_keyvals
from .transform import SECTION_LEVELS, Transformer, replace_refs

log = logging.getLogger(__name__)

# Body commands rewritten in one pass before pandoc sees the text: layout commands
# are dropped; \footnotemark/\footnotetext pairs outside tables keep the text as a footnote.
_BODY_COMMANDS = {
    **{name: (spec, lambda a: "") for name, spec in (
        ("maketitle", ""), ("backmatter", ""), ("frontmatter", ""), ("mainmatter", ""), ("raggedbottom", ""),
        ("bigskip", ""), ("medskip", ""), ("smallskip", ""), ("noindent", ""), ("clearpage", ""),
        ("cleardoublepage", ""), ("newpage", ""), ("pagebreak", "o"), ("nopagebreak", "o"), ("FloatBarrier", ""),
        ("unskip", ""), ("centering", ""), ("lstset", "m"), ("vspace", "sm"), ("hspace", "sm"), ("linenumbers", ""),
        ("nolinenumbers", ""), ("bibliographystyle", "m"), ("printbibliography", "o"), ("tableofcontents", ""),
        ("hypersetup", "m"), ("graphicspath", "m"), ("setcounter", "mm"), ("addtocounter", "mm"),
        ("footnotemark", "o"), ("nocite", "m"), ("setcitestyle", "m"),
    )},
    "footnotetext": ("om", lambda a: "\\footnote{" + (a[1] or "") + "}"),
}

# \vskip/\hskip/\kern take a TeX dimension (with optional stretch), not a braced argument
_SKIP_RE = re.compile(
    r"\\(?:vskip|hskip|kern)(?![A-Za-z@])\s*[-+]?\s*(?:[0-9.]+\s*(?:[a-z]{2}|\\[A-Za-z@]+)|\\[A-Za-z@]+)"
    r"(?:\s*(?:plus|minus)\s*[-+]?\s*[0-9.]+\s*(?:fill+|[a-z]{2}|\\[A-Za-z@]+))*"
)


@dataclass
class PreprocessResult:
    pandoc_tex: str
    conversion: Conversion


def parse_theorems(preamble: str) -> dict[str, TheoremSpec]:
    """Theorem-like environments declared with ``\\newtheorem`` (amsthm syntax).

    The style is kept by name; its fonts and punctuation are read from the class or
    amsthm when the heads are written. pandoc knows amsthm's ``proof`` natively.
    """
    envs: dict[str, TheoremSpec] = {}
    style = "plain"
    pat = re.compile(r"\\(newtheorem\*?|theoremstyle)(?![A-Za-z@])")
    for m in pat.finditer(preamble):
        if is_escaped(preamble, m.start()):
            continue
        cmd = m.group(1)
        if cmd == "theoremstyle":
            args, _ = parse_command_args(preamble, m.end(), "m")
            style = (args[0] or "plain").strip()
        elif cmd == "newtheorem*":
            args, _ = parse_command_args(preamble, m.end(), "mm")
            envs[(args[0] or "").strip()] = TheoremSpec(args[1] or "", None, style)
        else:
            args, _ = parse_command_args(preamble, m.end(), "momo")
            name = (args[0] or "").strip()
            shared = envs.get((args[1] or "").strip())
            if shared is not None:  # \newtheorem{lemma}[theorem]{Lemma}: same counter and numbering
                envs[name] = TheoremSpec(args[2] or "", shared.counter, style, shared.within)
            else:
                within = SECTION_LEVELS.get((args[3] or "").strip(), 0)
                envs[name] = TheoremSpec(args[2] or "", name, style, within)
    return envs


def parse_crefnames(preamble: str) -> dict[str, dict[str, tuple[str, str]]]:
    """``\\crefname{type}{singular}{plural}`` and ``\\Crefname`` declarations (cleveref)."""
    names: dict[str, dict[str, tuple[str, str]]] = {}
    for cmd, style in (("crefname", "cref"), ("Crefname", "Cref")):
        for _, _, args in find_commands(preamble, cmd, spec="mmm"):
            names.setdefault((args[0] or "").strip(), {})[style] = (args[1] or "", args[2] or "")
    return names


def _package_options(preamble: str, package: str) -> list[str]:
    options: list[str] = []
    for _, _, args in find_commands(preamble, "usepackage", spec="om"):
        if package in [p.strip() for p in (args[1] or "").split(",")]:
            options += [o.strip() for o in (args[0] or "").split(",") if o.strip()]
    return options


def cleveref_setup(preamble: str) -> texdefs.Cleveref:
    """cleveref's definitions under the manuscript's package options, plus its ``\\crefname``s.

    Package options that are not language names (``capitalise``, ``noabbrev`` ...) are
    recognised by being declared as options in cleveref.sty with their own meaning.
    """
    options = _package_options(preamble, "cleveref")
    capitalise = bool({"capitalise", "capitalize"} & set(options))
    languages = [o for o in options if texdefs.cleveref(o).names]
    base = texdefs.cleveref(languages[-1] if languages else "english", capitalise=capitalise,
                            abbrev="noabbrev" not in options)
    cref = texdefs.Cleveref({t: dict(v) for t, v in base.names.items()}, base.pair, base.middle, base.last,
                            base.range, set(base.parens))
    for ref_type, styles in parse_crefnames(preamble).items():
        for style, (singular, plural) in styles.items():
            cref.declare(ref_type, style, singular, plural)
    # names are inserted as plain text in Word
    cref.names = {t: {st: (latex_to_plain(a), latex_to_plain(b)) for st, (a, b) in v.items()} for t, v in cref.names.items()}
    return cref


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


def _citations(text: str, reg: Registry) -> str:
    """natbib citations become CITE markers (one optional argument: postnote; two: pre and post)."""

    def handler(name: str):
        def fn(args: list[str | None]) -> str:
            keys = [k.strip() for k in (args[3] or "").split(",") if k.strip()]
            pre, post = (args[1], args[2]) if args[2] is not None else (None, args[1])
            reg.cites.append((name.lower(), keys, pre, post))
            return marker("CITE", len(reg.cites) - 1)

        return "soom", fn

    return replace_commands(text, {name: handler(name) for name in CITE_COMMANDS})


def _replace_refs(text: str, reg: Registry) -> str:
    return map_math_aware(text, lambda t: replace_refs(t, reg), lambda m: replace_refs(m, reg, math=True))


def preprocess(path: Path) -> PreprocessResult:
    """The class and ``.bst`` files are looked up next to the manuscript, then in the TeX installation."""
    ms = load_manuscript(path)
    search = (ms.path.parent,)
    preamble, macros = collect_macros(ms.preamble)
    # definitions made inside the body (rare) are honoured too
    body, macros = collect_macros(ms.body, macros)
    preamble = expand_macros(preamble, macros)
    body = expand_macros(body, macros)

    doc_class = texdefs.document_class(ms.documentclass, search) if ms.documentclass else None
    if ms.documentclass and doc_class is None:
        log.warning("%s.cls is neither next to the manuscript nor installed: class names, reference "
                    "style and theorem styles are unavailable", ms.documentclass)
    class_macros = collect_macros(doc_class.source)[1] if doc_class else MacroTable()
    front, preamble, body = extract_frontmatter(preamble, body, doc_class)

    graphics_paths = [ms.path.parent]
    for _, _, args in find_commands(preamble + body, "graphicspath"):
        for g in re.findall(r"\{([^}]*)\}", args[0] or ""):
            graphics_paths.append((ms.path.parent / g).resolve())

    # ---- references: the manuscript's (or its class's) .bst via BibTeX, natbib punctuation
    class_refs = doc_class.references(ms.documentclass_options) if doc_class else texdefs.ClassReferences()
    bibstyle = class_refs.bibstyle
    for _, _, args in find_commands(preamble + body, "bibliographystyle"):
        bibstyle = (args[0] or "").strip()
    citestyle = list(class_refs.citestyle)
    for _, _, args in find_commands(preamble + body, "setcitestyle"):
        citestyle += _split_keyvals(args[0] or "")
    natbib = texdefs.natbib(tuple(class_refs.natbib_options + _package_options(preamble, "natbib")), tuple(citestyle))
    bib_files, body = _bib_files(body, ms.path.parent)
    more, preamble = _bib_files(preamble, ms.path.parent)
    bib_files += more

    reg = Registry()
    bib = None
    typed = find_env(body, "thebibliography")
    if typed is not None:
        bib = parse_thebibliography(body[typed.start : typed.end], class_macros)
        body = body[: typed.start] + body[typed.end :]
    elif bib_files and bibstyle:
        bbl = run_bibtex(cited_keys(front.abstract + body), bibstyle, bib_files, search)
        bib = parse_thebibliography(bbl, class_macros) if bbl else None
    elif bib_files:
        log.warning("no bibliography style: add \\bibliographystyle or use a class that sets one")
    if bib is not None:
        reg.bibitems = [k for k, _ in bib.items]
        reg.bib_labels = bib.labels

    # \includesvg{name} (svg package) is \includegraphics of name.<svg extension>
    def includesvg(a: list[str | None]) -> str:
        name = (a[1] or "").strip()
        ext = texdefs.svg_extension()
        name = name if Path(name).suffix or not ext else f"{name}.{ext}"
        return "\\includegraphics" + (f"[{a[0]}]" if a[0] is not None else "") + "{" + name + "}"

    body = replace_commands(body, {"includesvg": ("om", includesvg)})

    # packages pandoc handles poorly: siunitx, mhchem
    body = rewrite_packages(body)
    front.abstract = rewrite_packages(front.abstract)
    front.title = rewrite_packages(front.title)

    cref = cleveref_setup(preamble)
    reg.conjunctions = {"pair": cref.pair, "middle": cref.middle, "last": cref.last, "range": cref.range}
    listing_name = texdefs.package_name("listings.sty", "lstlistingname") or ""
    body = Transformer(reg, parse_theorems(preamble), doc_class.source if doc_class else "", listing_name).walk(body)

    body = _replace_refs(body, reg)
    body = map_nonverbatim(body, lambda s: _citations(s, reg))
    front.abstract = _citations(front.abstract, reg)
    body = map_nonverbatim(body, lambda s: _SKIP_RE.sub("", replace_commands(s, _BODY_COMMANDS)))

    if bib is not None and bib.items:
        reg.headings.append(Heading(1, False, False, "", role="references"))
        body += f"\n\n\\section{{{marker('HEAD', len(reg.headings) - 1)}}}\n\n"
        body += "".join(f"{marker('BIB', n)} {text}\n\n" for n, (_, text) in enumerate(bib.items))

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
        fm_parts.append(f"{marker('FMKEYWORDS')} {front.keywords}\n\n")
    fm_parts.append(f"{marker('FMEND')}\n\n")

    math_ops = [preamble[s:e] for s, e, _ in find_commands(preamble, "DeclareMathOperator", spec="smm")]
    pandoc_preamble = "\n".join(["\\documentclass{article}", *math_ops])
    pandoc_tex = pandoc_preamble + "\n\\begin{document}\n" + "".join(fm_parts) + body + "\n\\end{document}\n"

    def class_name(macro: str) -> str:
        text = doc_class.name(macro) if doc_class else None
        return latex_to_plain(text) if text else ""

    names = {macro: class_name(macro) for macro in ("refname", "figurename", "tablename", "abstractname", "keywordname")}
    names["algorithmname"] = latex_to_plain(texdefs.package_name("algorithm.sty", "ALG@name") or "")
    names["contributing"] = latex_to_plain(doc_class.contributing_label() or "") if doc_class else ""
    names["emailsep"] = latex_to_plain(doc_class.email_separator() or "", strip=False) if doc_class else ""

    conv = Conversion(
        front=front,
        registry=reg,
        source_dir=ms.path.parent,
        graphics_paths=graphics_paths,
        natbib=natbib,
        bibstyle=bibstyle if bib_files and typed is None else None,
        equal_notes=notes,
        cref_names=cref.names,
        cref_parens=cref.parens,
        equal_mark=doc_class.equalcont_mark() if doc_class else None,
        names=names,
        doc_class=doc_class,
    )
    return PreprocessResult(pandoc_tex, conv)
