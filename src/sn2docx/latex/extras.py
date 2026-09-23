"""Package emulation that pandoc gets wrong or does not do: siunitx syntax, mhchem.

Everything here rewrites LaTeX into other LaTeX that pandoc converts faithfully;
no symbol or word is written here. siunitx phrases, prefixes and powers come from
``siunitx.sty`` (:mod:`.texdefs`).
"""

from __future__ import annotations

import logging
import re

from . import texdefs
from .scan import (
    ENV_BEGIN_RE,
    command_re,
    find_env_end,
    is_escaped,
    map_nonverbatim,
    read_control_sequence,
    read_group,
    replace_commands,
)

log = logging.getLogger(__name__)

MATH_ENVS = (
    "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "eqnarray",
    "eqnarray*", "flalign", "flalign*", "alignat", "alignat*", "displaymath", "math",
)


# ------------------------------------------------------------ math segmentation
def _map_math(s: str, text_fn, math_fn) -> str:
    """Split verbatim-free text into text and math runs (``$``, ``$$``, ``\\(``, ``\\[``, math envs)."""
    out: list[str] = []
    pos = buf = 0
    n = len(s)
    while pos < n:
        c = s[pos]
        if c == "\\":
            m = ENV_BEGIN_RE.match(s, pos)
            if m and m.group(1) in MATH_ENVS:
                end = find_env_end(s, m.group(1), m.end())
                if end is not None:
                    out.append(text_fn(s[buf:pos]))
                    out.append(s[pos : m.end()] + math_fn(s[m.end() : end[0]]) + s[end[0] : end[1]])
                    pos = buf = end[1]
                    continue
            close = {"(": "\\)", "[": "\\]"}.get(s[pos + 1 : pos + 2])
            j = s.find(close, pos + 2) if close and not is_escaped(s, pos) else -1
            if j > 0:
                out.append(text_fn(s[buf:pos]))
                out.append(s[pos : pos + 2] + math_fn(s[pos + 2 : j]) + close)
                pos = buf = j + 2
            else:
                pos += 2
            continue
        if c == "$":
            delim = "$$" if s.startswith("$$", pos) else "$"
            j = pos + len(delim)
            while j < n and not (s.startswith(delim, j) and not is_escaped(s, j)):
                j += 1
            if j < n:
                out.append(text_fn(s[buf:pos]))
                out.append(delim + math_fn(s[pos + len(delim) : j]) + delim)
                pos = buf = j + len(delim)
                continue
        pos += 1
    out.append(text_fn(s[buf:]))
    return "".join(out)


def map_math_aware(s: str, text_fn, math_fn) -> str:
    """Apply ``text_fn`` to text runs and ``math_fn`` to math content, skipping verbatim."""
    return map_nonverbatim(s, lambda seg: _map_math(seg, text_fn, math_fn))


# ---------------------------------------------------------------------- siunitx
# pandoc renders \qty, \unit, \num, \ang itself. Rewritten here, using siunitx's own
# definitions: \per (pandoc drops a leading one), list/range/product commands (pandoc
# prints "&" or nothing) and digit grouping (pandoc does not group).
def rewrite_unit(spec: str) -> str:
    """Express ``\\per`` and pre-powers as ``\\tothe`` exponents on the following unit."""
    if "\\per" not in spec and not any(f"\\{p}" in spec for p, (kind, _) in texdefs.siunitx().powers.items() if kind == "pre"):
        return spec
    si = texdefs.siunitx()
    tokens = re.findall(r"\\[A-Za-z]+(?:\s*\{[^}]*\})?|[^\\\s]+", spec)
    out: list[str] = []
    prefix, sign, power = "", 1, 1
    for tok in tokens:
        name = tok[1:].split("{")[0].strip() if tok.startswith("\\") else ""
        if name == "per":
            sign = -1
        elif name in si.powers and si.powers[name][0] == "pre":
            power = si.powers[name][1]
        elif name in si.prefixes:
            prefix += tok
        elif name in si.powers or name in ("tothe", "raiseto", "of", "highlight"):
            out.append(tok)  # post powers and qualifiers stay attached to the previous unit
        else:
            exp = sign * power
            out.append(prefix + tok + (f"\\tothe{{{exp}}}" if exp != 1 else ""))
            prefix, sign, power = "", 1, 1
    return "".join(out)


def group_digits(number: str) -> str | None:
    """siunitx digit grouping for a plain decimal number, or None when not needed."""
    si = texdefs.siunitx()
    m = re.fullmatch(r"\s*([-+]?)(\d+)(?:\.(\d+))?\s*", number)
    minimum = int(si.options.get("group-minimum-digits", "0") or 0)
    if not m or not minimum:
        return None
    sep = si.options.get("group-separator", "")
    whole, frac = m.group(2), m.group(3) or ""
    if len(whole) < minimum and len(frac) < minimum:
        return None

    def chunks(digits: str, from_left: bool) -> str:
        if len(digits) < minimum:
            return digits
        if from_left:
            return sep.join(digits[i : i + 3] for i in range(0, len(digits), 3))
        head = len(digits) % 3 or 3
        return sep.join([digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)])

    return m.group(1) + chunks(whole, False) + ("." + chunks(frac, True) if frac else "")


def _num(x: str) -> str:
    return group_digits(x) or f"\\num{{{x.strip()}}}"


def _qty(x: str, unit: str) -> str:
    unit = rewrite_unit(unit)
    grouped = group_digits(x)
    if grouped is None:
        return f"\\qty{{{x.strip()}}}{{{unit}}}"
    return grouped + texdefs.siunitx().options.get("number-unit-separator", "") + f"\\unit{{{unit}}}"


def siunitx_table(math: bool):
    si = texdefs.siunitx()
    symbol = si.options.get("product-symbol", "")
    product = f" {symbol} " if math else f" ${symbol}$ "

    def listed(items: list[str]) -> str:
        if len(items) == 2:
            return items[0] + si.phrase("list-pair-separator", math) + items[1]
        head = si.phrase("list-separator", math).join(items[:-1])
        return head + (si.phrase("list-final-separator", math) + items[-1] if len(items) > 1 else items[-1])

    def values(a, i):
        return [x for x in (a[i] or "").split(";")]

    return {
        "SI": ("omm", lambda a: _qty(a[1] or "", a[2] or "")),
        "qty": ("omm", lambda a: _qty(a[1] or "", a[2] or "")),
        "si": ("om", lambda a: f"\\unit{{{rewrite_unit(a[1] or '')}}}"),
        "unit": ("om", lambda a: f"\\unit{{{rewrite_unit(a[1] or '')}}}"),
        "num": ("om", lambda a: _num(a[1] or "")),
        "SIlist": ("omm", lambda a: listed([_qty(x, a[2] or "") for x in values(a, 1)])),
        "qtylist": ("omm", lambda a: listed([_qty(x, a[2] or "") for x in values(a, 1)])),
        "numlist": ("om", lambda a: listed([_num(x) for x in values(a, 1)])),
        "SIrange": ("ommm", lambda a: _qty(a[1] or "", a[3] or "") + si.phrase("range-phrase", math) + _qty(a[2] or "", a[3] or "")),
        "qtyrange": ("ommm", lambda a: _qty(a[1] or "", a[3] or "") + si.phrase("range-phrase", math) + _qty(a[2] or "", a[3] or "")),
        "numrange": ("omm", lambda a: _num(a[1] or "") + si.phrase("range-phrase", math) + _num(a[2] or "")),
        "qtyproduct": ("omm", lambda a: product.join(_qty(x, a[2] or "") for x in re.split(r"\s*x\s*", (a[1] or "").strip()))),
        "numproduct": ("om", lambda a: product.join(_num(x) for x in re.split(r"\s*x\s*", (a[1] or "").strip()))),
        "sisetup": ("m", lambda a: ""),
    }


# ----------------------------------------------------------------------- mhchem
_ARROWS = [("<=>", "\\rightleftharpoons"), ("<->", "\\leftrightarrow"), ("->", "\\rightarrow"), ("<-", "\\leftarrow")]


_SCRIPT_TOKEN_RE = re.compile(r"[0-9]+[+-]?|[+-]|.")


def _charge_ends(s: str, i: int) -> bool:
    """``s[i]`` is a charge sign: + or - closing a species (end, space, bracket or arrow follows)."""
    return i < len(s) and s[i] in "+-" and (i + 1 == len(s) or s[i + 1] in " )]}" or s.startswith("->", i))


def _charge_sign(c: str) -> str:
    return "{-}" if c == "-" else "+"


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
            flush()
            if prev_is_species and _charge_ends(s, j):  # Ca2+ -> Ca^{2+}
                out.append(f"^{{{num}{_charge_sign(s[j])}}}")
                j += 1
            elif prev_is_species:
                out.append(f"_{{{num}}}")
            else:
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
                m = _SCRIPT_TOKEN_RE.match(s, i + 1)
                body = m.group(0) if m else ""
                i += 1 + len(body)
            body = body.replace("-", "{-}") if c == "^" else body
            out.append(f"{c}{{{body}}}")
            prev_is_species = True
            continue
        if c in "+-" and prev_is_species and _charge_ends(s, i):  # Na+ / I- : a charge, not an operator
            flush()
            out.append(f"^{{{_charge_sign(c)}}}")
            i += 1
            continue
        if c == "+" and (i + 1 >= len(s) or s[i + 1] == " "):
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
            cs = read_control_sequence(s, i) or "\\"
            out.append(cs + " ")
            i += len(cs)
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
    if not command_re(*text_table).search(body):
        return body
    math_table = {**siunitx_table(True), **mhchem_table(True)}
    return map_math_aware(body, lambda t: replace_commands(t, text_table), lambda m: replace_commands(m, math_table))
