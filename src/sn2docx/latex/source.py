"""Loading a manuscript: file inclusion, preamble/body split and user macros."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .scan import (
    find_commands,
    find_env,
    is_escaped,
    map_nonverbatim,
    parse_command_args,
    read_control_sequence,
    read_group,
    replace_commands,
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
                cs = read_control_sequence(preamble, i)
                if not cs:
                    pos = i
                    continue
                name = cs[1:]
                i += len(cs)
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


def _arg_spec(nargs: int, default: str | None) -> str:
    if not nargs:
        return ""
    return ("o" if default is not None else "m") + "m" * (nargs - 1)


def expand_macros(text: str, table: MacroTable, max_passes: int = 10) -> str:
    """Expand user-defined commands and environments (verbatim regions untouched)."""
    if not table.commands and not table.environments:
        return text

    def expander(macro: Macro):
        def fn(args: list[str | None]) -> str:
            if macro.nargs and macro.default is not None and args[0] is None:
                args[0] = macro.default
            expansion = _substitute(macro.body, [a or "" for a in args])
            # a control word ending in a letter must not merge with the text after it
            return expansion + "{}" if not macro.nargs and expansion[-1:].isalpha() else expansion

        return _arg_spec(macro.nargs, macro.default), fn

    handlers = {n: expander(m) for n, m in table.commands.items() if re.fullmatch(r"[A-Za-z@]+", n)}
    for _ in range(max_passes):
        before = text
        text = map_nonverbatim(text, lambda s: replace_commands(s, handlers))
        for name, env in table.environments.items():
            e = find_env(text, name)
            guard = 0
            while e is not None and guard < 1000:
                guard += 1
                args, body_start = parse_command_args(text, e.body_start, _arg_spec(env.nargs, env.default))
                if env.nargs and env.default is not None and args[0] is None:
                    args[0] = env.default
                vals = [a or "" for a in args]
                repl = _substitute(env.begin, vals) + text[body_start : e.body_end] + _substitute(env.end, vals)
                text = text[: e.start] + repl + text[e.end :]
                e = find_env(text, name, e.start)
        if text == before:
            break
    return text


@dataclass
class Manuscript:
    path: Path
    documentclass_options: list[str]
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
    for _, _, args in find_commands(preamble, "documentclass", spec="om"):
        opts = [o.strip() for o in (args[0] or "").split(",") if o.strip()]
        break
    return Manuscript(path, opts, preamble, body)
