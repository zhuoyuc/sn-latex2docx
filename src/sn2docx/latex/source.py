"""Loading a manuscript: file inclusion, preamble/body split and user macros."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .scan import (
    command_re,
    find_env,
    is_escaped,
    parse_command_args,
    read_group,
    skip_ws,
    strip_comments,
)

log = logging.getLogger(__name__)

_INPUT_RE = re.compile(r"\\(input|include|subfile)\s*\{([^}]*)\}")


def read_tex(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def resolve_inputs(text: str, base_dir: Path, depth: int = 0) -> str:
    """Inline ``\\input``/``\\include`` files recursively (comments already stripped)."""
    if depth > 20:
        raise RuntimeError("\\input nesting too deep (cycle?)")

    def repl(m: re.Match[str]) -> str:
        if is_escaped(text, m.start()):
            return m.group(0)
        name = m.group(2).strip()
        candidates = [base_dir / name, base_dir / f"{name}.tex"]
        for c in candidates:
            if c.is_file():
                sub = strip_comments(read_tex(c))
                sub = resolve_inputs(sub, base_dir, depth + 1)
                if m.group(1) == "include":
                    return "\n\\clearpage\n" + sub + "\n\\clearpage\n"
                return sub
        # \input of a .bbl or unknown file: leave it for later stages / drop
        log.warning("could not find \\%s{%s}", m.group(1), name)
        return ""

    return _INPUT_RE.sub(repl, text)


@dataclass
class Macro:
    name: str
    nargs: int
    default: str | None
    body: str


@dataclass
class EnvMacro:
    name: str
    nargs: int
    default: str | None
    begin: str
    end: str


@dataclass
class MacroTable:
    commands: dict[str, Macro] = field(default_factory=dict)
    environments: dict[str, EnvMacro] = field(default_factory=dict)


# Commands that pandoc understands natively and which must not be expanded by
# us even when a document (re)defines them.
_KEEP_NATIVE = {"ref", "eqref", "label", "cite", "citep", "citet", "caption", "section", "subsection"}


def collect_macros(preamble: str, table: MacroTable | None = None) -> tuple[str, MacroTable]:
    """Extract ``\\newcommand``-style definitions, returning the stripped preamble."""
    table = table or MacroTable()
    out: list[str] = []
    pos = 0
    pat = re.compile(
        r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|def|gdef|edef|"
        r"newenvironment|renewenvironment)(?![A-Za-z@])"
    )
    while True:
        m = pat.search(preamble, pos)
        if not m:
            break
        if is_escaped(preamble, m.start()):
            out.append(preamble[pos : m.end()])
            pos = m.end()
            continue
        out.append(preamble[pos : m.start()])
        kind = m.group(1)
        i = m.end()
        try:
            if kind in ("def", "gdef", "edef"):
                i = skip_ws(preamble, i)
                nm = re.match(r"\\([A-Za-z@]+|.)", preamble[i:])
                if not nm:
                    pos = i
                    continue
                name = nm.group(1)
                i += len(nm.group(0))
                params_start = i
                while i < len(preamble) and preamble[i] != "{":
                    i += 1
                params = preamble[params_start:i]
                g = read_group(preamble, i)
                if g is None:
                    pos = i
                    continue
                body, i = g
                if re.fullmatch(r"(#\d)*", params.strip()):
                    nargs = len(re.findall(r"#\d", params))
                    if name not in _KEEP_NATIVE:
                        table.commands[name] = Macro(name, nargs, None, body)
                pos = i
                continue
            if kind in ("newenvironment", "renewenvironment"):
                args, i = parse_command_args(preamble, i, "smoomm")
                name = (args[1] or "").strip()
                nargs = int(args[2]) if args[2] and args[2].strip().isdigit() else 0
                table.environments[name] = EnvMacro(name, nargs, args[3], args[4] or "", args[5] or "")
                pos = i
                continue
            args, i = parse_command_args(preamble, i, "smoom")
            name = (args[1] or "").strip().lstrip("\\")
            nargs = int(args[2]) if args[2] and args[2].strip().isdigit() else 0
            if kind == "providecommand" and name in table.commands:
                pos = i
                continue
            if name and name not in _KEEP_NATIVE:
                table.commands[name] = Macro(name, nargs, args[3], args[4] or "")
            pos = i
        except Exception:  # pragma: no cover - malformed definitions are skipped
            log.warning("could not parse macro definition near: %s", preamble[m.start() : m.start() + 60])
            pos = m.end()
    out.append(preamble[pos:])
    return "".join(out), table


def _substitute(body: str, args: list[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        k = int(m.group(1))
        return args[k - 1] if 0 < k <= len(args) else ""

    return re.sub(r"#(\d)", repl, body)


def expand_macros(text: str, table: MacroTable, max_passes: int = 10) -> str:
    """Expand user-defined commands and environments (verbatim regions untouched)."""
    if not table.commands and not table.environments:
        return text
    # Longest names first so \foo does not shadow \foobar (the regex also guards).
    names = sorted(table.commands, key=len, reverse=True)
    letter_names = [n for n in names if re.fullmatch(r"[A-Za-z@]+", n)]
    pat = re.compile(r"\\(" + "|".join(re.escape(n) for n in letter_names) + r")(?![A-Za-z@])") if letter_names else None

    for _ in range(max_passes):
        changed = False
        if pat is not None:
            out: list[str] = []
            pos = 0
            for seg_start, seg_end, verbatim in _segments(text):
                if verbatim:
                    continue
                # process [seg_start, seg_end)
                i = seg_start
                while True:
                    m = pat.search(text, i, seg_end)
                    if not m:
                        break
                    if is_escaped(text, m.start()):
                        i = m.end()
                        continue
                    macro = table.commands[m.group(1)]
                    spec = ("o" if macro.default is not None else "m") + "m" * max(0, macro.nargs - 1) if macro.nargs else ""
                    args, end = parse_command_args(text, m.end(), spec)
                    if macro.nargs and macro.default is not None and args[0] is None:
                        args[0] = macro.default
                    if not macro.nargs:
                        # swallow the space after a control word, as TeX does
                        end = m.end()
                    expansion = _substitute(macro.body, [a or "" for a in args])
                    out.append(text[pos : m.start()])
                    # keep a separating space if the macro ended a control word
                    if not macro.nargs and end < len(text) and text[end : end + 1].isalpha() and expansion[-1:].isalpha():
                        expansion += " "
                    out.append(expansion)
                    pos = end
                    i = end
                    changed = True
            out.append(text[pos:])
            text = "".join(out)
        for name, env in table.environments.items():
            e = find_env(text, name)
            guard = 0
            while e is not None and guard < 1000:
                guard += 1
                spec = ("o" if env.default is not None else "m") + "m" * max(0, env.nargs - 1) if env.nargs else ""
                args, body_start = parse_command_args(text, e.body_start, spec)
                if env.nargs and env.default is not None and args[0] is None:
                    args[0] = env.default
                vals = [a or "" for a in args]
                repl = _substitute(env.begin, vals) + text[body_start : e.body_end] + _substitute(env.end, vals)
                text = text[: e.start] + repl + text[e.end :]
                changed = True
                e = find_env(text, name, e.start)
        if not changed:
            break
    return text


def _segments(text: str):
    """Split text into (start, end, is_verbatim) segments."""
    from .scan import VERBATIM_ENVS, find_env_end

    pat = re.compile(r"\\begin\s*\{(" + "|".join(re.escape(e) for e in VERBATIM_ENVS) + r")\}|\\verb\*?([^A-Za-z\s])")
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            yield pos, len(text), False
            return
        yield pos, m.start(), False
        if m.group(1):
            end = find_env_end(text, m.group(1), m.end())
            stop = end[1] if end else len(text)
        else:
            j = text.find(m.group(2), m.end())
            stop = len(text) if j < 0 else j + 1
        yield m.start(), stop, True
        pos = stop


@dataclass
class Manuscript:
    path: Path
    documentclass_options: list[str]
    documentclass: str
    preamble: str
    body: str


def load_manuscript(path: Path) -> Manuscript:
    raw = strip_comments(read_tex(path))
    raw = resolve_inputs(raw, path.parent)
    m = re.search(r"\\begin\s*\{document\}", raw)
    if not m:
        raise ValueError(f"{path}: no \\begin{{document}} found")
    preamble = raw[: m.start()]
    end = re.search(r"\\end\s*\{document\}", raw[m.end() :])
    body = raw[m.end() : m.end() + end.start()] if end else raw[m.end() :]
    opts: list[str] = []
    cls = ""
    dc = command_re("documentclass").search(preamble)
    if dc:
        args, _ = parse_command_args(preamble, dc.end(), "om")
        opts = [o.strip() for o in (args[0] or "").split(",") if o.strip()]
        cls = (args[1] or "").strip()
    return Manuscript(path, opts, cls, preamble, body)
