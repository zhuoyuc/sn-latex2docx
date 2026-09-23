"""Definitions read from the LaTeX packages themselves.

Words and symbols that a LaTeX package prints (cleveref's "Figure", algpseudocode's
"end while", siunitx's "to", natbib's brackets and separators, sn-jnl's
equal-contribution mark) are taken from that package's source, never written into
this converter. Files are looked up in the manuscript's directory and in the
installed TeX distribution (``kpsewhich``); a TeX distribution is required, as for
compiling the manuscript itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from .scan import find_commands, parse_command_args, read_group, skip_ws, strip_comments


# ----------------------------------------------------------------- locating files
@cache
def _kpsewhich(name: str) -> Path | None:
    exe = shutil.which("kpsewhich")
    if not exe:
        return None
    res = subprocess.run([exe, name], capture_output=True, text=True)
    path = res.stdout.strip()
    return Path(path) if res.returncode == 0 and path and Path(path).is_file() else None


def tex_path(name: str, extra_dirs: tuple[Path, ...] = ()) -> Path | None:
    """A TeX input file: ``extra_dirs`` (the manuscript's folder) first, then the TeX installation."""
    for path in (*(d / name for d in extra_dirs), _kpsewhich(name)):
        if path is not None and path.is_file():
            return path
    return None


@cache
def _read(path: Path) -> str:
    return strip_comments(path.read_text(encoding="utf-8", errors="replace"))


def tex_source(name: str, extra_dirs: tuple[Path, ...] = ()) -> str:
    """Comment-free source of a TeX file (see :func:`tex_path`)."""
    path = tex_path(name, extra_dirs)
    if path is None:
        raise FileNotFoundError(f"{name} not found; a TeX distribution (TeX Live, MiKTeX) is required")
    return _read(path)


# ---------------------------------------------------------------------- cleveref
@dataclass
class Cleveref:
    """cleveref's names per reference type, and its conjunctions (all LaTeX source)."""

    names: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)  # type -> {cref|Cref: (sg, pl)}
    pair: str = ""
    middle: str = ""
    last: str = ""
    range: str = ""
    parens: set[str] = field(default_factory=set)  # types whose label format wraps the number in ()

    def name(self, ref_type: str, style: str, plural: bool) -> str | None:
        entry = self.names.get(ref_type, {}).get(style)
        return None if entry is None else entry[1 if plural else 0]

    def declare(self, ref_type: str, style: str, singular: str, plural: str) -> None:
        self.names.setdefault(ref_type, {})[style] = (singular, plural)


def _run_conditionals(body: str, flags: dict[str, bool], commands: str = "crefname@preamble|Crefname@preamble|def|creflabelformat"):
    """Yield command matches (``commands``) that sit in active branches of TeX conditionals.

    ``\\if<flag>`` switches take their value from ``flags``; any other conditional
    counts as false. ``\\newif`` declarations and ``\\ifthenelse`` are not conditionals.
    """
    token = re.compile(r"(\\newif\s*)?\\(if(?!thenelse)[A-Za-z@]*|else|fi|" + commands + r")(?![A-Za-z@])")
    # a conditional that is only named (\let\if@x\iftrue, \newif\if@x) does not open a block
    named = re.compile(r"\\let\s*(?:\\[A-Za-z@]+\s*=?\s*)?$")
    active = [True]
    for m in token.finditer(body):
        if m.group(1) or (m.group(2).startswith("if") and named.search(body, max(0, m.start() - 80), m.start())):
            continue
        cmd = m.group(2)
        if cmd.startswith("if"):
            active.append(active[-1] and flags.get(cmd[2:], False))
        elif cmd == "else":
            parent = active[-2] if len(active) > 1 else True
            active[-1] = parent and not active[-1]
        elif cmd == "fi":
            if len(active) > 1:
                active.pop()
        elif active[-1]:
            yield m


@cache
def cleveref(language: str = "english", capitalise: bool = False, abbrev: bool = True) -> Cleveref:
    """cleveref's definitions for ``language`` under the given package options."""
    src = tex_source("cleveref.sty")
    out = Cleveref()
    start = re.search(r"\\DeclareOption\{" + re.escape(language) + r"\}", src)
    if start is None:
        return out
    body, _ = read_group(src, skip_ws(src, start.end())) or ("", 0)
    flags = {"@cref@capitalise": capitalise, "@cref@abbrev": abbrev}
    for m in _run_conditionals(body, flags):
        cmd = m.group(2)
        if cmd in ("crefname@preamble", "Crefname@preamble"):
            args, _ = parse_command_args(body, m.end(), "mmm")
            out.declare(args[0] or "", "cref" if cmd[0] == "c" else "Cref", args[1] or "", args[2] or "")
        elif cmd == "def":
            name = re.match(r"\s*\\cref(pair|middle|last|range)conjunction@preamble", body[m.end():])
            if name:
                g = read_group(body, skip_ws(body, m.end() + name.end()))
                setattr(out, name.group(1), (g[0] if g else "").replace("\\nobreakspace", "~"))
    # label formats are declared outside the language options, e.g. (#2#1#3) for equations
    for _, _, args in find_commands(src, "creflabelformat", spec="mm"):
        if re.search(r"\(\s*#2\s*#1\s*#3\s*\)", args[1] or ""):
            out.parens.add((args[0] or "").strip())
    return out


# ---------------------------------------------------------------- algorithmicx
@dataclass
class AlgCommand:
    role: str  # "start" | "end" | "continue" | "line" (numbered) | "item" (unnumbered, labelled)
    nargs: int = 0
    default: str | None = None  # default of an optional first argument
    template: str = ""  # LaTeX with #1..#9


@dataclass
class AlgGrammar:
    commands: dict[str, AlgCommand] = field(default_factory=dict)
    inline: dict[str, tuple[int, str]] = field(default_factory=dict)  # macro -> (nargs, LaTeX body)
    linenumber: str = "#1"  # \alglinenumber: how a line number is printed


def _read_def(src: str, i: int) -> tuple[str, int, str | None, str, int]:
    """Parse ``\\name[n][default]{body}`` starting at ``i``: (name, nargs, default, body, end)."""
    name_args, i = parse_command_args(src, i, "m")
    n, i = _optional_int(src, i)
    default, i = _optional(src, i)
    body, i = parse_command_args(src, i, "m")
    return (name_args[0] or "").lstrip("\\"), n, default, body[0] or "", i


def _optional(src: str, i: int) -> tuple[str | None, int]:
    j = skip_ws(src, i)
    if j < len(src) and src[j] == "[":
        g = read_group(src, j, "[", "]")
        if g:
            return g[0], g[1]
    return None, i


def _optional_int(src: str, i: int) -> tuple[int, int]:
    val, j = _optional(src, i)
    return (int(val), j) if val is not None and val.strip().isdigit() else (0, i)


@cache
def algorithmic() -> AlgGrammar:
    """algpseudocode's commands (and algcompatible's upper-case ones) with their LaTeX output."""
    g = AlgGrammar()
    core = tex_source("algorithmicx.sty")
    m = re.search(r"\\algnewcommand\\algorithmiccomment\[1\]\{", core)
    if m:
        body = read_group(core, m.end() - 1)[0].replace("##", "#")
        g.inline["algorithmiccomment"] = (1, body)
    for m in re.finditer(r"\\def\\([A-Za-z]+)\{(\\[A-Za-z@]+)\}", core):
        g.inline.setdefault(m.group(1), (0, m.group(2)))  # \def\Comment{\algorithmiccomment}
    for name in ("algpseudocode.sty", "algcompatible.sty"):
        src = tex_source(name)
        for m in re.finditer(r"\\(algnewcommand|newcommand)(?![A-Za-z@])", src):
            cmd, n, default, body, _ = _read_def(src, m.end())
            item = re.fullmatch(r"\s*\\item\[(.*)\]\s*", body, re.S)
            if item:
                g.commands[cmd] = AlgCommand("item", 0, None, item.group(1))
            elif body.strip() in ("\\State", "\\Statex", "\\Comment"):
                g.inline.setdefault(cmd, (0, body.strip()))  # upper-case aliases (\STATE -> \State)
            else:
                g.inline[cmd] = (n, body)
        for m in re.finditer(r"\\algdef(?![A-Za-z@])", src):
            flags, i = parse_command_args(src, m.end(), "m")
            _, i = _optional(src, i)  # [BLOCK]
            flags = flags[0] or ""
            names_needed = 1 + ("E" in flags) + ("C" in flags) + ("e" in flags)
            names = []
            for _ in range(names_needed):
                a, i = parse_command_args(src, i, "m")
                names.append(a[0] or "")
            n, i = _optional_int(src, i)
            default, i = _optional(src, i)
            text, i = parse_command_args(src, i, "m")
            if "C" in flags:
                g.commands[names[1]] = AlgCommand("continue", n, default, text[0] or "")
                continue
            g.commands[names[0]] = AlgCommand("start", n, default, text[0] or "")
            if "E" in flags:
                n2, i = _optional_int(src, i)
                d2, i = _optional(src, i)
                end_text, i = parse_command_args(src, i, "m")
                g.commands[names[1]] = AlgCommand("end", n2, d2, end_text[0] or "")
    m = re.search(r"\\algnewcommand\\alglinenumber\[1\]\{", core)
    if m:
        g.linenumber = read_group(core, m.end() - 1)[0].replace("##", "#")
    # the two line commands of algorithmicx itself
    g.commands.setdefault("State", AlgCommand("line"))
    g.commands.setdefault("Statex", AlgCommand("item"))
    return g


# ---------------------------------------------------------------------- siunitx
@dataclass
class Siunitx:
    """siunitx defaults (LaTeX source) and its prefix/power declarations."""

    options: dict[str, str] = field(default_factory=dict)
    prefixes: set[str] = field(default_factory=set)
    powers: dict[str, tuple[str, int]] = field(default_factory=dict)  # \square -> ("pre", 2), \squared -> ("post", 2)

    def phrase(self, key: str, math: bool = False) -> str:
        """An option value as LaTeX for text (or math) mode: ``\\TextOrMath`` resolved."""
        value = self.options.get(key, "")
        out, pos = [], 0
        for m in re.finditer(r"\\TextOrMath(?![A-Za-z@])", value):
            args, end = parse_command_args(value, m.end(), "mm")
            out.append(value[pos : m.start()] + ((args[1] if math else args[0]) or ""))
            pos = end
        out.append(value[pos:])
        text = "".join(out).replace("\\space", " ")
        if not math:
            text = re.sub(r"\\text\s*\{([^}]*)\}", r"\1", text)
        return re.sub(r"\s+", " ", text)


@cache
def siunitx() -> Siunitx:
    src = tex_source("siunitx.sty")
    out = Siunitx()
    for key in ("list-separator", "list-final-separator", "list-pair-separator", "range-phrase",
                "product-symbol", "group-separator", "group-minimum-digits", "number-unit-separator"):
        m = re.search(r"(?<![\w-])" + re.escape(key) + r"\s*=\s*", src)
        if not m:
            continue
        i = m.end()
        if src[i] == "{":
            out.options[key] = read_group(src, i)[0].strip()
        else:
            tok = re.match(r"\\[A-Za-z]+|\\.|[^,\s]+", src[i:])
            out.options[key] = tok.group(0) if tok else ""
    for m in re.finditer(r"^\\siunitx_declare_prefix:Nnn\s*\\([A-Za-z]+)", src, re.M):
        out.prefixes.add(m.group(1))
    for m in re.finditer(r"^\\siunitx_declare_power:NNn\s*\\([A-Za-z]+)\s*\\([A-Za-z]+)\s*\{\s*(-?\d+)\s*\}", src, re.M):
        out.powers[m.group(1)] = ("pre", int(m.group(3)))
        out.powers[m.group(2)] = ("post", int(m.group(3)))
    return out


# ------------------------------------------------------------------------ natbib
@dataclass
class Natbib:
    """natbib's citation punctuation after package options and ``\\setcitestyle``."""

    macros: dict[str, str] = field(default_factory=dict)  # NAT@open, NAT@close, NAT@sep, NAT@cmt, NAT@aysep, ...
    flags: dict[str, bool] = field(default_factory=dict)  # NAT@numbers, NAT@super
    sort: bool = False
    compress: bool = False
    range_dash: str = ""  # LaTeX between the ends of a compressed range
    biblabel: str = ""  # LaTeX with #1: label of a numbered bibliography entry

    def get(self, name: str) -> str:
        return self.macros.get(name, "")

    @property
    def numbers(self) -> bool:
        return self.flags.get("NAT@numbers", False)

    @property
    def superscript(self) -> bool:
        return self.flags.get("NAT@super", False)


def _option_bodies(src: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"\\DeclareOption\{([^}]*)\}", src):
        g = read_group(src, skip_ws(src, m.end()))
        if g:
            out[m.group(1)] = g[0]
    return out


def _apply_natbib(body: str, nb: Natbib, options: dict[str, str]) -> None:
    for m in re.finditer(r"\\(?:renewcommand|newcommand|def|gdef|xdef)\s*\\(NAT@[A-Za-z@]+)\s*\{", body):
        g = read_group(body, m.end() - 1)
        if g:
            nb.macros[m.group(1)] = g[0]
    for m in re.finditer(r"\\(NAT@[A-Za-z]+)(true|false)(?![A-Za-z@])", body):
        nb.flags[m.group(1)] = m.group(2) == "true"
    for m in re.finditer(r"\\ExecuteOptions\{([^}]*)\}", body):
        for opt in m.group(1).split(","):
            if opt.strip() in options:
                _apply_natbib(options[opt.strip()], nb, options)


@cache
def natbib(options: tuple[str, ...] = (), citestyle: tuple[str, ...] = ()) -> Natbib:
    """natbib's settings for the given package options and ``\\setcitestyle`` arguments."""
    src = tex_source("natbib.sty")
    nb = Natbib()
    opt_bodies = _option_bodies(src)
    # package defaults: the top-level \newcommand\NAT@... definitions
    for m in re.finditer(r"\\newcommand\\(NAT@[A-Za-z@]+)\{", src):
        nb.macros[m.group(1)] = read_group(src, m.end() - 1)[0]
    for opt in options:
        if opt in opt_bodies:
            _apply_natbib(opt_bodies[opt], nb, opt_bodies)
    nb.sort = nb.compress = False
    for opt in options:
        body = opt_bodies.get(opt, "")
        nb.sort |= bool(re.search(r"\\def\\NAT@sort\{\\@ne\}", body))
        nb.compress |= bool(re.search(r"\\def\\NAT@cmprs\{\\@ne\}", body))
    # \setcitestyle{key=value}: natbib maps each key onto one of its macros
    keymap = {k: v for k, v in re.findall(r"\\def\\@tempb\{(\w+)\}\\ifx\\@tempa\\@tempb\s*\\[gx]?def\\([A-Za-z@]+)", src)}
    for item in citestyle:
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1]
        if key in keymap and _:
            nb.macros[keymap[key]] = value
        elif key in opt_bodies:
            _apply_natbib(opt_bodies[key], nb, opt_bodies)
    dash = re.search(r"\\def@NAT@last@yr\{(.*?)\\NAT@penalty\}", src)
    nb.range_dash = dash.group(1) if dash else ""
    # numbered entries fall back to the kernel's \@biblabel
    kernel = tex_source("latex.ltx")
    label = re.search(r"\\def\\@biblabel#1\{", kernel)
    nb.biblabel = read_group(kernel, label.end() - 1)[0] if label else "#1"
    return nb


# ------------------------------------------------------------ class references
@dataclass
class ClassReferences:
    bibstyle: str | None = None
    natbib_options: list[str] = field(default_factory=list)
    citestyle: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------- sn-jnl
@dataclass
class ThmStyle:
    """Head/body formatting of an amsthm theorem style."""

    head_bold: bool = False
    head_italic: bool = False
    body_italic: bool = False
    punct: str = ""  # LaTeX after the head
    note_open: str = ""  # LaTeX before an optional note
    note_close: str = ""  # LaTeX after it


def _fonts(spec: str) -> tuple[bool, bool]:
    return bool(re.search(r"\\(bfseries|textbf|bf)(?![A-Za-z])", spec)), bool(re.search(r"\\(itshape|textit|it|slshape)(?![A-Za-z])", spec))


def _note_parts(spec: str) -> tuple[str, str]:
    """``\thmnote{ {\the\thm@notefont(#3)}}`` -> ("(", ")") around the note (#3)."""
    m = re.search(r"\\thmnote\s*\{", spec)
    note = read_group(spec, m.end() - 1)[0] if m else ""
    note = re.sub(r"\\the\\thm@notefont|[{}]", "", note)
    before, _, after = note.partition("#3")
    return before.strip(), after.strip()


@cache
def theorem_style(name: str, class_source: str = "") -> ThmStyle:
    """Formatting of theorem style ``name``: the class's ``\newtheoremstyle`` or amsthm's ``\th@name``."""
    for src in (class_source, tex_source("amsthm.sty")):
        for m in re.finditer(r"\\newtheoremstyle\s*\{" + re.escape(name) + r"\}", src):
            args, _ = parse_command_args(src, m.end(), "mmmmmmmm")
            _, _, body_font, _, head_font, punct, _, head_spec = args
            head_bold, head_italic = _fonts(head_font or "")
            opening, closing = _note_parts(head_spec or "")
            if not (head_spec or "").strip():  # amsthm's default head spec
                opening, closing = _note_parts(_amsthm_default_head())
            return ThmStyle(head_bold, head_italic, _fonts(body_font or "")[1], punct or "", opening, closing)
    amsthm = tex_source("amsthm.sty")
    m = re.search(r"\\def\\th@" + re.escape(name) + r"\{", amsthm)
    if m is None:
        return theorem_style("plain", class_source) if name != "plain" else ThmStyle()
    body = read_group(amsthm, m.end() - 1)[0]
    head = re.search(r"\\thm@headfont\{([^}]*)\}", body)
    # amsthm's own defaults (\thm@headfont{\bfseries}, \thm@headpunct{.}) apply unless the style changes them
    default_head = re.search(r"\\thm@headfont\{([^}]*)\}", amsthm[: amsthm.find("\\def\\th@plain")])
    head_bold, head_italic = _fonts(head.group(1) if head else (default_head.group(1) if default_head else ""))
    punct = re.search(r"\\thm@headpunct\{([^}]*)\}", amsthm)
    opening, closing = _note_parts(_amsthm_default_head())
    body_font = re.sub(r"\\thm@headfont\{[^}]*\}", "", body)  # what remains sets the body font
    return ThmStyle(head_bold, head_italic, _fonts(body_font)[1], punct.group(1) if punct else "", opening, closing)


def _amsthm_default_head() -> str:
    amsthm = tex_source("amsthm.sty")
    m = re.search(r"\\thmnote\s*\{[^\n]*", amsthm)
    return m.group(0) if m else ""


def package_name(package: str, macro: str) -> str | None:
    """Default of a name macro a package defines (``\\ALG@name`` in algorithm.sty, ``\\lstlistingname`` ...)."""
    try:
        src = tex_source(package)
    except FileNotFoundError:
        return None
    m = re.search(r"\\(?:newcommand|renewcommand|def|lst@UserCommand)\s*\{?\\" + re.escape(macro) + r"\}?\s*\{", src)
    return read_group(src, m.end() - 1)[0] if m else None


@cache
def subcaption_label() -> str:
    """How subcaption labels a sub-figure (LaTeX, ``#2`` = the letter), separator included.

    subcaption.sty picks a caption3 label format and separator (``labelformat=parens,
    labelsep=space``); both are looked up in caption3.sty.
    """
    try:
        sub, cap = tex_source("subcaption.sty"), tex_source("caption3.sty")
    except FileNotFoundError:
        return "#2 "
    fmt = re.search(r"labelformat=(\w+)", sub)
    sep = re.search(r"labelsep=(\w+)", sub)
    label = "#2"
    if fmt:
        m = re.search(r"\\DeclareCaptionLabelFormat\{" + fmt.group(1) + r"\}\{", cap)
        if m:
            body = read_group(cap, m.end() - 1)[0]
            label = re.sub(r"\\bothIfFirst\{#1\}\{[^}]*\}", "", body)  # the name part (#1) is empty for panels
    separator = " "
    if sep:
        m = re.search(r"\\DeclareCaptionLabelSeparator\{" + sep.group(1) + r"\}\{", cap)
        if m:
            separator = read_group(cap, m.end() - 1)[0]
    return label + separator


@cache
def caption_separator(class_source: str = "") -> str:
    """Text between a caption's label and its text in ``\\@makecaption`` (class, else article.cls)."""
    for src in (class_source, tex_source("article.cls")):
        m = re.search(r"\\sbox\\@tempboxa\{#1(.*?)#2\}", src)
        if m:
            return m.group(1)
    return " "


# ------------------------------------------------------------------ lengths
# TeX's unit keywords and the names pint gives the same units (the sizes come from pint)
TEX_UNITS = {"in": "inch", "cm": "centimeter", "mm": "millimeter", "pt": "tex_point", "bp": "big_point",
             "pc": "tex_pica", "dd": "didot", "cc": "cicero", "sp": "scaled_point"}


@cache
def _units():
    import pint

    return pint.UnitRegistry()


def points_to(value: float, unit: str) -> float:
    """Convert TeX points to a pint unit ("inch", "big_point" ...)."""
    return _units().Quantity(value, "tex_point").to(unit).magnitude

_UNIT_RE = re.compile(r"([-+]?[0-9]*\.?[0-9]+)\s*(pt|bp|mm|cm|in|pc|dd|cc|sp|em|ex|\\p@)")


def tex_length(text: str, em: float = 0.0) -> float | None:
    """A TeX dimension in points (``.8pt``, ``.4\\p@``, ``1.5em`` with ``em`` points, ``\\z@``)."""
    text = text.strip()
    if re.fullmatch(r"\\z@", text):
        return 0.0
    m = _UNIT_RE.match(text)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    if unit == "\\p@":
        unit = "pt"
    if unit == "em":
        return value * em
    if unit == "ex":
        return None
    return _units().Quantity(value, TEX_UNITS[unit]).to("tex_point").magnitude


def _assigned(src: str, name: str) -> str | None:
    """The last value assigned to a length: ``\\setlength\\x{v}``, ``\\x=v``, ``\\x v``."""
    found = None
    for m in re.finditer(r"\\setlength\s*\{?\\" + re.escape(name) + r"\}?\s*\{([^}]*)\}|"
                         r"\\" + re.escape(name) + r"(?![A-Za-z@])\s*=?\s*([-+]?[0-9]*\.?[0-9]+\s*(?:\\p@|[a-z]{2}))", src):
        found = m.group(1) or m.group(2)
    return found


@dataclass
class FloatStyle:
    """float.sty's ``ruled`` style (algorithm.sty's default): caption font and rules."""

    caption_bold: bool = False
    caption_sep: str = " "  # between "Algorithm 1" and the caption
    top_rule: float | None = None  # pt; above the caption
    rule: float | None = None  # pt; below the caption and at the end (\hrule's default)


@cache
def float_style(class_source: str = "") -> FloatStyle:
    src = tex_source("float.sty")
    name = re.search(r"\\newcommand\{\\ALG@floatstyle\}\{(\w+)\}", tex_source("algorithm.sty"))
    style = name.group(1) if name else "ruled"
    fs = FloatStyle()
    m = re.search(r"\\newcommand\\fs@" + style + r"\{", src)
    if not m:
        return fs
    body = read_group(src, m.end() - 1)[0]
    fs.caption_bold = bool(re.search(r"\\def\\@fs@cfont\{[^}]*\\bfseries", body))
    capt = re.search(r"\\newcommand\\floatc@" + style + r"\[2\]\{\{\\@fs@cfont #1\}(.*?)#2", src)
    if capt:
        fs.caption_sep = capt.group(1)
    pre = re.search(r"\\def\\@fs@pre\{\\hrule height\s*([^ ]+)", body)
    fs.top_rule = tex_length(pre.group(1)) if pre else None
    # a bare \hrule is as thick as LaTeX's standard rules
    rule = _assigned(class_source, "arrayrulewidth") or _assigned(tex_source("article.cls"), "arrayrulewidth")
    fs.rule = tex_length(rule) if rule else None
    return fs


@dataclass
class AlgLayout:
    """algorithmicx's list geometry (TeX lengths, may use em)."""

    indent: str = ""  # \algorithmicindent per block level
    labelwidth_numbered: str = ""
    labelwidth_plain: str = ""
    labelsep: str = ""


@cache
def alg_layout() -> AlgLayout:
    src = tex_source("algorithmicx.sty")
    lay = AlgLayout()
    m = re.search(r"\\algnewcommand\\algorithmicindent\{([^}]*)\}", src)
    lay.indent = m.group(1) if m else ""
    m = re.search(r"\\ifthenelse\{\\equal\{#1\}\{0\}\}%?\s*\{\\labelwidth\s*([^}]*)\}%?\s*\{\\labelwidth\s*([^}]*)\}", src)
    if m:
        lay.labelwidth_plain, lay.labelwidth_numbered = m.group(1).strip(), m.group(2).strip()
    m = re.search(r"\\labelsep\s*([0-9.]+\s*[a-z]{2})", src)
    lay.labelsep = m.group(1) if m else ""
    return lay


@cache
def booktabs_rules() -> dict[str, str]:
    """booktabs' rule widths: heavyrulewidth, lightrulewidth, cmidrulewidth (TeX lengths)."""
    src = tex_source("booktabs.sty")
    return {name: m.group(1) for name in ("heavyrulewidth", "lightrulewidth", "cmidrulewidth")
            if (m := re.search(r"\\" + name + r"\s*=\s*([0-9.]+\s*[a-z]{2})", src))}


@cache
def svg_extension() -> str:
    """The file extension ``\\includesvg`` adds (svg.sty's ``\\svg@file@ext``)."""
    m = re.search(r"\\newcommand\*?\\svg@file@ext\{([^}]*)\}", tex_source("svg.sty"))
    return m.group(1) if m else ""


@cache
def undefined_ref() -> tuple[str, bool]:
    """What LaTeX prints for an undefined reference (``\\@setref``), and whether it is bold."""
    src = tex_source("latex.ltx")
    m = re.search(r"\\def\\@setref#1#2#3\{.*?\\nfss@text\{(.*?)\}", src, re.S)
    if not m:
        return "", False
    text = m.group(1)
    bold = "\\bfseries" in text
    return re.sub(r"\\[A-Za-z@]+", "", text).strip(), bold


@dataclass
class DocumentClass:
    source: str
    base: "DocumentClass | None" = None  # the class loaded with \LoadClass

    def definition(self, macro: str) -> str | None:
        """Body of ``\\def\\macro`` or ``\\newcommand\\macro`` here or in the base class."""
        m = None
        for m in re.finditer(r"\\(?:renewcommand|newcommand|def|gdef)\s*\{?\\" + re.escape(macro) + r"\}?\s*\{", self.source):
            pass
        if m is not None:
            return read_group(self.source, m.end() - 1)[0]
        return self.base.definition(macro) if self.base else None

    def today(self) -> tuple[list[str], str]:
        """Month names and the layout of ``\\today`` ("{month} {day}, {year}")."""
        body = self.definition("today") or ""
        ifcase = re.search(r"\\ifcase\\month\\or(.*?)\\fi", body, re.S)
        if not ifcase:
            return [], ""
        months = [x.strip() for x in ifcase.group(1).split("\\or")]
        layout = body[: ifcase.start()] + "{month}" + body[ifcase.end():]
        layout = layout.replace("\\space", " ").replace("\\number\\day", "{day}").replace("\\number\\year", "{year}")
        return months, re.sub(r"\s+", " ", layout).strip()

    def font_size(self, command: str) -> float | None:
        """Point size a size command sets (``\\@setfontsize\\footnotesize{7}{8}``, ``\\@ixpt``)."""
        for src in (self.source, *((self.base.source,) if self.base else ())):
            m = None
            for m in re.finditer(r"\\@setfontsize\s*\\" + re.escape(command) + r"\s*(\{[^}]*\}|\\@[a-z]+pt)", src):
                pass  # the last definition wins
            if not m:
                continue
            size = m.group(1).strip("{}")
            if size.startswith("\\@"):
                d = re.search(r"\\def\\" + re.escape(size[1:]) + r"\{([0-9.]+)\}", tex_source("latex.ltx"))
                size = d.group(1) if d else ""
            return tex_length(size if re.search(r"[a-z]", size) else size + "pt")
        return None

    def length(self, name: str) -> str | None:
        return _assigned(self.source, name) or (self.base.length(name) if self.base else None)

    def name(self, macro: str) -> str | None:
        """LaTeX text of a name macro the class sets (``\\refname``, ``\\figurename``, ``\\keywordname`` ...)."""
        m = None
        for m in re.finditer(r"\\(?:renewcommand|newcommand|def|gdef)\s*\{?\\" + re.escape(macro) + r"\}?\s*\{", self.source):
            pass  # the last definition wins
        return read_group(self.source, m.end() - 1)[0] if m else None

    def contributing_label(self) -> str | None:
        """Text sn-jnl prints before the list of other authors' e-mail addresses."""
        m = re.search(r"\\ifx\\authemail\\@empty\\else\s*(.*?)\\\s*\\authemail", self.source, re.S)
        return m.group(1).strip() if m else None

    def email_separator(self) -> str | None:
        """What follows each address in the class's e-mail lists (sn-jnl: ``;\\ ``)."""
        m = re.search(r"\\g@addto@macro\\corrauthemail\{", self.source)
        body = read_group(self.source, m.end() - 1)[0] if m else ""
        _, found, after = body.partition("{#1}")
        return after if found else None

    def references(self, options: list[str]) -> ClassReferences:
        """The bibliography style and natbib setup the class selects for ``options``.

        Each class option's ``\\DeclareOption`` body switches flags (``\\@X@refstyletrue``);
        the class's conditionals then decide which ``\\bibliographystyle``,
        ``\\usepackage[...]{natbib}`` and ``\\setcitestyle`` are in force.
        """
        bodies = _option_bodies(self.source)
        flags: dict[str, bool] = {}
        for opt in options:
            for m in re.finditer(r"\\(@?[A-Za-z@]+?)(true|false)(?![A-Za-z@])", bodies.get(opt, "")):
                flags[m.group(1)] = m.group(2) == "true"
        out = ClassReferences()
        for m in _run_conditionals(self.source, flags, "bibliographystyle|usepackage|setcitestyle"):
            cmd = m.group(2)
            if cmd == "bibliographystyle":
                args, _ = parse_command_args(self.source, m.end(), "m")
                out.bibstyle = (args[0] or "").strip()
            elif cmd == "usepackage":
                args, _ = parse_command_args(self.source, m.end(), "om")
                if (args[1] or "").strip() == "natbib":
                    out.natbib_options = [o.strip() for o in (args[0] or "").split(",") if o.strip()]
            else:
                args, _ = parse_command_args(self.source, m.end(), "m")
                out.citestyle += [a.strip() for a in _split_keyvals(args[0] or "")]
        return out

    def equalcont_mark(self) -> str | None:
        """LaTeX that ``\\equalcont`` appends to the author list (sn-jnl: ``$^{\\dagger}$``)."""
        for _, _, args in find_commands(self.source, "newcommand", spec="mom"):
            if (args[0] or "").strip() == "\\equalcont":
                m = re.search(r"\\g@addto@macro\\artauthors\{", args[2] or "")
                if m:
                    return read_group(args[2], m.end() - 1)[0]
        return None


def document_class(name: str, search_dirs: tuple[Path, ...] = ()) -> DocumentClass | None:
    """The document class source (``search_dirs`` first, then the TeX installation)."""
    try:
        src = tex_source(f"{name}.cls", tuple(search_dirs))
    except FileNotFoundError:
        return None
    base = re.search(r"\\LoadClass(?:\[[^]]*\])?\{([^}]+)\}", src)
    return DocumentClass(src, document_class(base.group(1), search_dirs) if base else None)


def _split_keyvals(text: str) -> list[str]:
    """Split ``a=b, c={d,e}`` at top-level commas."""
    parts, depth, cur = [], 0, ""
    for c in text:
        depth += (c == "{") - (c == "}")
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    return [p for p in (*parts, cur) if p.strip()]
