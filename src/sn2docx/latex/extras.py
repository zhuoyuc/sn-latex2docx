"""Package emulation that pandoc gets wrong or does not do: siunitx, mhchem, natbib textual citations.

Everything here rewrites LaTeX into LaTeX that pandoc converts faithfully. Each
rewrite knows whether it sits in text or in math, because the output differs
(``\\textsuperscript`` versus ``^``).
"""

from __future__ import annotations

import re
from pathlib import Path

from .scan import VERBATIM_ENVS, find_env_end, is_escaped, parse_command_args, read_group, skip_ws

NNBSP = "\u202f"  # narrow no-break space: number-unit and unit-unit separator
MINUS = "\u2212"
MATH_ENVS = (
    "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray",
    "eqnarray*", "flalign", "flalign*", "alignat", "alignat*", "displaymath", "math",
)


# ------------------------------------------------------------ math segmentation
def map_math_aware(s: str, text_fn, math_fn) -> str:
    """Apply ``text_fn`` to text runs and ``math_fn`` to math content, skipping verbatim."""
    out: list[str] = []
    pos = 0
    buf = 0
    n = len(s)
    begin_re = re.compile(r"\\begin\s*\{([^}]*)\}")
    while pos < n:
        c = s[pos]
        if c == "\\":
            m = begin_re.match(s, pos)
            if m and (m.group(1) in VERBATIM_ENVS or m.group(1) in MATH_ENVS):
                end = find_env_end(s, m.group(1), m.end())
                if end is None:
                    pos = m.end()
                    continue
                out.append(text_fn(s[buf:pos]))
                if m.group(1) in VERBATIM_ENVS:
                    out.append(s[pos : end[1]])
                else:
                    out.append(s[pos : m.end()] + math_fn(s[m.end() : end[0]]) + s[end[0] : end[1]])
                pos = buf = end[1]
                continue
            mv = re.match(r"\\verb\*?([^A-Za-z\s])", s[pos:])
            if mv:
                j = s.find(mv.group(1), pos + len(mv.group(0)))
                stop = n if j < 0 else j + 1
                out.append(text_fn(s[buf:pos]))
                out.append(s[pos:stop])
                pos = buf = stop
                continue
            for open_, close in (("\\(", "\\)"), ("\\[", "\\]")):
                if s.startswith(open_, pos) and not is_escaped(s, pos):
                    j = s.find(close, pos + 2)
                    if j > 0:
                        out.append(text_fn(s[buf:pos]))
                        out.append(open_ + math_fn(s[pos + 2 : j]) + close)
                        pos = buf = j + 2
                        break
            else:
                pos += 2
            continue
        if c == "$":
            dbl = s.startswith("$$", pos)
            delim = "$$" if dbl else "$"
            j = pos + len(delim)
            while j < n:
                if s.startswith(delim, j) and not is_escaped(s, j):
                    break
                j += 1
            if j < n:
                out.append(text_fn(s[buf:pos]))
                out.append(delim + math_fn(s[pos + len(delim) : j]) + delim)
                pos = buf = j + len(delim)
                continue
        pos += 1
    out.append(text_fn(s[buf:]))
    return "".join(out)


def _replace_commands(s: str, table: dict[str, tuple[str, callable]]) -> str:
    """Replace ``\\name`` (with args per spec) by ``fn(args)`` for each entry of ``table``."""
    if not table:
        return s
    pat = re.compile(r"\\(" + "|".join(sorted(map(re.escape, table), key=len, reverse=True)) + r")(?![A-Za-z@])")
    out = []
    pos = 0
    while True:
        m = pat.search(s, pos)
        if not m:
            break
        if is_escaped(s, m.start()):
            out.append(s[pos : m.end()])
            pos = m.end()
            continue
        spec, fn = table[m.group(1)]
        args, end = parse_command_args(s, m.end(), spec)
        out.append(s[pos : m.start()])
        out.append(fn(args))
        pos = end
    out.append(s[pos:])
    return "".join(out)


# ---------------------------------------------------------------------- siunitx
PREFIXES = {
    "quecto": "q", "ronto": "r", "yocto": "y", "zepto": "z", "atto": "a", "femto": "f", "pico": "p", "nano": "n",
    "micro": "\u00b5", "milli": "m", "centi": "c", "deci": "d", "deca": "da", "deka": "da", "hecto": "h",
    "kilo": "k", "mega": "M", "giga": "G", "tera": "T", "peta": "P", "exa": "E", "zetta": "Z", "yotta": "Y",
}
UNITS = {
    "metre": "m", "meter": "m", "second": "s", "gram": "g", "kilogram": "kg", "ampere": "A", "kelvin": "K",
    "mole": "mol", "candela": "cd", "hertz": "Hz", "newton": "N", "pascal": "Pa", "joule": "J", "watt": "W",
    "coulomb": "C", "volt": "V", "ohm": "\u03a9", "farad": "F", "siemens": "S", "tesla": "T", "henry": "H",
    "weber": "Wb", "celsius": "\u00b0C", "degreeCelsius": "\u00b0C", "liter": "L", "litre": "L", "minute": "min",
    "hour": "h", "day": "d", "degree": "\u00b0", "arcminute": "\u2032", "arcsecond": "\u2033", "percent": "%",
    "electronvolt": "eV", "angstrom": "\u00c5", "bar": "bar", "molar": "M", "dalton": "Da", "gray": "Gy",
    "becquerel": "Bq", "lumen": "lm", "lux": "lx", "radian": "rad", "steradian": "sr", "sievert": "Sv",
    "katal": "kat", "atomicmassunit": "u", "bel": "B", "decibel": "dB", "neper": "Np", "hectare": "ha",
    "tonne": "t", "mmHg": "mmHg", "astronomicalunit": "au", "barn": "b", "knot": "kn", "nauticalmile": "M",
    "clight": "c", "elementarycharge": "e", "planckbar": "\u210f", "electronmass": "m\u2091", "bohr": "a\u2080",
    "hartree": "E\u2095", "micron": "\u00b5m", "torr": "Torr", "calorie": "cal", "gauss": "G", "erg": "erg",
    "psi": "psi", "inch": "in", "foot": "ft", "atmosphere": "atm", "rpm": "rpm", "cm": "cm", "mm": "mm",
    "km": "km", "kg": "kg", "mg": "mg", "ms": "ms", "us": "\u00b5s", "ns": "ns", "ps": "ps", "kHz": "kHz",
    "MHz": "MHz", "GHz": "GHz", "mA": "mA", "mV": "mV", "kV": "kV", "kW": "kW", "mW": "mW", "nm": "nm",
    "um": "\u00b5m", "pm": "pm", "mL": "mL", "ml": "mL", "uL": "\u00b5L", "mol": "mol", "mmol": "mmol",
    "umol": "\u00b5mol", "kJ": "kJ", "MPa": "MPa", "GPa": "GPa", "kPa": "kPa", "eV": "eV", "keV": "keV",
    "MeV": "MeV", "GeV": "GeV",
}
_UNIT_TOKEN = re.compile(r"\\([A-Za-z]+)|([A-Za-z\u00b5\u00b0\u03a9%]+)|\^\s*\{?(-?[0-9.]+)\}?|([./~ ])")


def _unit_parts(unit: str) -> list[tuple[str, str]]:
    """Parse a siunitx unit specification into (symbol, exponent) pairs."""
    unit = unit.strip()
    parts: list[tuple[str, str]] = []
    prefix = ""
    pending_power: str | None = None
    per = False
    i = 0
    while i < len(unit):
        if unit[i].isspace():
            i += 1
            continue
        m = re.match(r"\\([A-Za-z]+)", unit[i:])
        if m:
            name = m.group(1)
            i += len(m.group(0))
            if name in PREFIXES:
                prefix += PREFIXES[name]
            elif name == "per":
                per = True
            elif name in ("square",):
                pending_power = "2"
            elif name == "cubic":
                pending_power = "3"
            elif name in ("squared", "cubed") and parts:
                s, e = parts[-1]
                p = "2" if name == "squared" else "3"
                parts[-1] = (s, _mul_power(e, p))
            elif name == "tothe" and parts:
                args, j = parse_command_args(unit, i, "m")
                i = j
                s, e = parts[-1]
                parts[-1] = (s, _mul_power(e, args[0] or "1"))
            elif name == "raiseto":
                args, j = parse_command_args(unit, i, "m")
                i = j
                pending_power = args[0]
            elif name in ("of", "highlight"):
                args, j = parse_command_args(unit, i, "m")
                i = j
            else:
                sym = UNITS.get(name, name)
                exp = pending_power or "1"
                if per:
                    exp = _mul_power(exp, "-1")
                parts.append((prefix + sym, exp))
                prefix, pending_power, per = "", None, False
            continue
        m = re.match(r"\^\s*(\{[^}]*\}|-?[0-9.]+)", unit[i:])
        if m and parts:
            p = m.group(1).strip("{}")
            s, e = parts[-1]
            parts[-1] = (s, _mul_power(e, p))
            i += len(m.group(0))
            continue
        m = re.match(r"[A-Za-z\u00b5\u00b0\u03a9%\u00c5]+", unit[i:])
        if m:
            parts.append((prefix + m.group(0), "-1" if per else "1"))
            prefix, per = "", False
            i += len(m.group(0))
            continue
        if unit[i] == "/":
            per = True
        i += 1
    return parts


def _mul_power(a: str, b: str) -> str:
    try:
        v = float(a) * float(b)
        return str(int(v)) if v == int(v) else str(v)
    except ValueError:
        return b


def format_unit(unit: str, math: bool) -> str:
    parts = _unit_parts(unit)
    pieces = []
    for sym, exp in parts:
        if math:
            s = f"\\mathrm{{{sym}}}"
            if exp != "1":
                s += f"^{{{exp}}}"
        else:
            s = sym
            if exp != "1":
                s += "\\textsuperscript{" + exp.replace("-", MINUS) + "}"
        pieces.append(s)
    sep = "\\," if math else NNBSP
    return sep.join(pieces)


def _group(digits: str) -> str:
    if len(digits) < 5:
        return digits
    out = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return NNBSP.join(out)


def format_number(num: str, math: bool) -> str:
    num = num.strip().replace("{", "").replace("}", "")
    num = re.sub(r"\\pm|\+-", " \u00b1 ", num)
    tokens = re.split(r"(\s*\u00b1\s*)", num)
    out = []
    for t in tokens:
        if "\u00b1" in t:
            out.append(" \\pm " if math else " \u00b1 ")
            continue
        m = re.fullmatch(r"\s*([-+]?)(\d*)(?:[.,](\d+))?\s*(?:[eEdD]\s*([-+]?\d+))?\s*", t)
        if not m:
            out.append(t.strip())
            continue
        sign, integer, frac, exp = m.groups()
        s = ""
        if sign == "-":
            s += "-" if math else MINUS
        elif sign == "+":
            s += "+"
        s += _group(integer or ("0" if frac else ""))
        if frac:
            s += "." + frac
        if exp is not None:
            e = exp.lstrip("+")
            if s:
                s += " \\times " if math else " \u00d7 "
            if math:
                s += f"10^{{{e}}}"
            else:
                s += "10\\textsuperscript{" + e.replace("-", MINUS) + "}"
        out.append(s)
    return "".join(out)


def _qty(num: str, unit: str, math: bool) -> str:
    u = format_unit(unit, math)
    n = format_number(num, math)
    if not u:
        return n
    if u.startswith("\u00b0") and not u.startswith("\u00b0C") or u in ("%",):
        return n + u  # angles and percent attach directly
    return n + ("\\," if math else NNBSP) + u


def _list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + " and " + items[-1]


def siunitx_table(math: bool):
    def wrap(s: str) -> str:
        return s

    def qty(a):
        return wrap(_qty(a[1] or "", a[2] or "", math))

    def qlist(a):
        return wrap(_list([_qty(x, a[2] or "", math) for x in (a[1] or "").split(";")]))

    def nlist(a):
        return wrap(_list([format_number(x, math) for x in (a[1] or "").split(";")]))

    def qrange(a):
        return wrap(f"{_qty(a[1] or '', a[3] or '', math)} to {_qty(a[2] or '', a[3] or '', math)}")

    def nrange(a):
        return wrap(f"{format_number(a[1] or '', math)} to {format_number(a[2] or '', math)}")

    def qproduct(a):
        times = " \\times " if math else " \u00d7 "
        return wrap(times.join(_qty(x, a[2] or "", math) for x in re.split(r"\s*x\s*", (a[1] or "").strip())))

    def nproduct(a):
        times = " \\times " if math else " \u00d7 "
        return wrap(times.join(format_number(x, math) for x in re.split(r"\s*x\s*", (a[1] or "").strip())))

    def unit(a):
        return wrap(format_unit(a[1] or "", math))

    def num(a):
        return wrap(format_number(a[1] or "", math))

    def ang(a):
        parts = (a[1] or "").split(";")
        marks = ["\u00b0", "\u2032", "\u2033"]
        return wrap("".join(format_number(p, math) + m for p, m in zip(parts, marks) if p.strip()))

    return {
        "SI": ("omm", qty), "qty": ("omm", qty), "si": ("om", unit), "unit": ("om", unit), "num": ("om", num),
        "SIlist": ("omm", qlist), "qtylist": ("omm", qlist), "numlist": ("om", nlist),
        "SIrange": ("ommm", qrange), "qtyrange": ("ommm", qrange), "numrange": ("omm", nrange),
        "qtyproduct": ("omm", qproduct), "numproduct": ("om", nproduct), "ang": ("om", ang),
        "sisetup": ("m", lambda a: ""),
    }


# ----------------------------------------------------------------------- mhchem
_ARROWS = [("<=>", "\\rightleftharpoons"), ("<->", "\\leftrightarrow"), ("->", "\\rightarrow"), ("<-", "\\leftarrow")]


def mhchem_to_math(formula: str) -> str:
    """Convert a (simple) mhchem formula to math: upright element symbols, real subscripts."""
    out: list[str] = []
    i = 0
    s = formula.strip()
    letters = ""
    prev_is_species = False  # a digit after an element or bracket is a subscript

    def flush():
        nonlocal letters
        if letters:
            out.append(f"\\mathrm{{{letters}}}")
            letters = ""

    while i < len(s):
        c = s[i]
        arrow = next((a for a in _ARROWS if s.startswith(a[0], i)), None)
        if arrow:
            flush()
            out.append(f" {arrow[1]} ")
            i += len(arrow[0])
            prev_is_species = False
            continue
        if c.isalpha():
            letters += c
            prev_is_species = True
            i += 1
            continue
        if c.isdigit() or (c == "." and letters == "" and out and prev_is_species):
            j = i
            while j < len(s) and (s[j].isdigit() or s[j] == "."):
                j += 1
            num = s[i:j]
            if prev_is_species:
                flush()
                out.append(f"_{{{num}}}")
            else:
                flush()
                out.append(num + "\\,")
            i = j
            continue
        if c in "_^":
            flush()
            if i + 1 < len(s) and s[i + 1] == "{":
                g = read_group(s, i + 1)
                body = g[0] if g else ""
                i = g[1] if g else i + 2
            else:
                m = re.match(r"[0-9]*[+-]?|.", s[i + 1 :])
                body = m.group(0) if m else ""
                i += 1 + len(body)
            body = body.replace("-", "{-}") if c == "^" else body
            out.append(f"{c}{{{body}}}")
            prev_is_species = True
            continue
        if c in "+" and (i + 1 >= len(s) or s[i + 1] == " "):
            flush()
            out.append(" + ")
            prev_is_species = False
            i += 1
            continue
        if c in "()[]":
            flush()
            out.append(c)
            prev_is_species = c in ")]"
            i += 1
            continue
        if c == " ":
            flush()
            out.append("\\ ")
            prev_is_species = False
            i += 1
            continue
        if c == "\\":
            flush()
            m = re.match(r"\\[A-Za-z]+|\\.", s[i:])
            out.append(m.group(0) + " ")
            i += len(m.group(0))
            continue
        flush()
        out.append(c)
        i += 1
    flush()
    return "".join(out).strip()


def mhchem_table(math: bool):
    def ce(a):
        body = mhchem_to_math(a[0] or "")
        return body if math else f"${body}$"

    return {"ce": ("m", ce), "cf": ("m", ce)}


# ------------------------------------------------------------ package rewriting
def rewrite_packages(body: str) -> str:
    """Rewrite siunitx and mhchem commands everywhere, math-aware."""
    text_table = {**siunitx_table(False), **mhchem_table(False)}
    math_table = {**siunitx_table(True), **mhchem_table(True)}
    return map_math_aware(body, lambda t: _replace_commands(t, text_table), lambda m: _replace_commands(m, math_table))


# ------------------------------------------------------- textual numeric cites
def read_bib_authors(files: list[Path]) -> dict[str, tuple[list[str], str]]:
    """Map citation key -> (author/editor family names, year) using a light BibTeX reader."""
    out: dict[str, tuple[list[str], str]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"@\s*(\w+)\s*\{\s*([^,\s]+)\s*,", text):
            if m.group(1).lower() in ("string", "comment", "preamble"):
                continue
            start = m.end()
            depth = 1
            j = m.end() - 1
            while j + 1 < len(text) and depth > 0:
                j += 1
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
            entry = text[start:j]
            fields = _bib_fields(entry)
            names = fields.get("author") or fields.get("editor") or ""
            year = re.sub(r"[{}]", "", fields.get("year", ""))
            out[m.group(2)] = ([_family(n) for n in re.split(r"\s+and\s+", names) if n.strip()], year)
    return out


def _bib_fields(entry: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in re.finditer(r"(\w+)\s*=\s*", entry):
        key = m.group(1).lower()
        i = skip_ws(entry, m.end())
        if i < len(entry) and entry[i] == "{":
            g = read_group(entry, i)
            if g:
                fields[key] = g[0]
        elif i < len(entry) and entry[i] == '"':
            j = entry.find('"', i + 1)
            if j > 0:
                fields[key] = entry[i + 1 : j]
        else:
            mv = re.match(r"[^,\n]+", entry[i:])
            if mv:
                fields[key] = mv.group(0).strip()
    return fields


def _family(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip())
    if name.startswith("{") and name.endswith("}"):
        return name[1:-1]
    if "," in name:
        return name.split(",")[0].strip()
    # "First von Last": the last token, keeping braced groups together
    tokens = re.findall(r"\{[^}]*\}|\S+", name)
    return tokens[-1] if tokens else name


def author_text(names: list[str]) -> str:
    if not names:
        return "?"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


_TEXTUAL_RE = re.compile(r"\\(citet|Citet|citeauthor|Citeauthor|citeyear|citeyearpar|citealt)\*?(?![A-Za-z@])")


def rewrite_textual_citations(body: str, bib: dict[str, tuple[list[str], str]]) -> str:
    """Numeric mode: ``\\citet{k}`` -> ``Name et al.~\\cite{k}`` (pandoc prints only the number)."""
    out: list[str] = []
    pos = 0
    while True:
        m = _TEXTUAL_RE.search(body, pos)
        if not m:
            break
        if is_escaped(body, m.start()):
            out.append(body[pos : m.end()])
            pos = m.end()
            continue
        args, end = parse_command_args(body, m.end(), "oom")
        keys = [k.strip() for k in (args[2] or "").split(",") if k.strip()]
        cmd = m.group(1).lower()
        pieces = []
        for k in keys:
            names, year = bib.get(k, ([], ""))
            who = author_text(names) if names else k
            if cmd == "citeauthor":
                pieces.append(who)
            elif cmd == "citeyear":
                pieces.append(year)
            elif cmd == "citeyearpar":
                pieces.append(f"({year})")
            else:
                note = f"[{args[1]}]" if args[1] else ""
                pieces.append(f"{who}~\\cite{note}{{{k}}}")
        out.append(body[pos : m.start()] + ", ".join(pieces))
        pos = end
    out.append(body[pos:])
    return "".join(out)
