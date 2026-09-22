"""Low-level helpers for scanning LaTeX source.

These work on plain strings and understand just enough TeX syntax for the
preprocessor: balanced groups, optional arguments, environments, comments and
verbatim regions. They never try to *execute* TeX.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Environments whose bodies must be passed through untouched.
VERBATIM_ENVS = ("verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "comment")

_ENV_BEGIN_RE = re.compile(r"\\begin\s*\{([^}]*)\}")


def is_escaped(s: str, i: int) -> bool:
    """True when ``s[i]`` is preceded by an odd number of backslashes."""
    n = 0
    j = i - 1
    while j >= 0 and s[j] == "\\":
        n += 1
        j -= 1
    return n % 2 == 1


def skip_ws(s: str, i: int, newlines: bool = True) -> int:
    """Skip spaces (and single newlines when ``newlines``) starting at ``i``."""
    while i < len(s):
        c = s[i]
        if c in " \t" or (newlines and c in "\r\n"):
            i += 1
        else:
            break
    return i


def read_group(s: str, i: int, open_: str = "{", close: str = "}") -> tuple[str, int] | None:
    """Read a balanced group starting at ``s[i] == open_``.

    Returns ``(content, index_after_group)`` or ``None`` if ``s[i]`` does not
    open a group. Braces nested inside square-bracket groups are respected.
    """
    if i >= len(s) or s[i] != open_:
        return None
    depth = 0
    brace = 0  # brace depth, used when reading [...] groups
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if open_ != "{":
            if c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
            elif brace == 0 and c == open_:
                depth += 1
            elif brace == 0 and c == close:
                depth -= 1
                if depth == 0:
                    return s[i + 1 : j], j + 1
        else:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[i + 1 : j], j + 1
        j += 1
    return None


def read_arg(s: str, i: int) -> tuple[str, int] | None:
    """Read a mandatory argument: a braced group or a single token."""
    i = skip_ws(s, i)
    if i >= len(s):
        return None
    if s[i] == "{":
        return read_group(s, i)
    if s[i] == "\\":
        m = re.match(r"\\([A-Za-z@]+|.)", s[i:])
        if m:
            return m.group(0), i + len(m.group(0))
    return s[i], i + 1


def read_optional(s: str, i: int) -> tuple[str | None, int]:
    """Read an optional ``[...]`` argument if present (after whitespace)."""
    j = skip_ws(s, i)
    if j < len(s) and s[j] == "[":
        g = read_group(s, j, "[", "]")
        if g:
            return g
    return None, i


def parse_command_args(s: str, i: int, spec: str) -> tuple[list[str | None], int]:
    """Parse arguments following a command according to ``spec``.

    ``spec`` is a string of ``o`` (optional) and ``m`` (mandatory) letters, and
    ``s`` for an optional star. Returns the argument list and the end index.
    """
    args: list[str | None] = []
    for kind in spec:
        if kind == "s":
            j = skip_ws(s, i, newlines=False)
            if j < len(s) and s[j] == "*":
                args.append("*")
                i = j + 1
            else:
                args.append(None)
        elif kind == "o":
            val, i = read_optional(s, i)
            args.append(val)
        else:
            g = read_arg(s, i)
            if g is None:
                args.append("")
            else:
                args.append(g[0])
                i = g[1]
    return args, i


def command_re(name: str) -> re.Pattern[str]:
    """Regex matching ``\\name`` not followed by another letter."""
    return re.compile(r"\\" + re.escape(name) + r"(?![A-Za-z@])")


@dataclass
class Env:
    name: str
    start: int  # index of "\begin"
    body_start: int  # index just after "\begin{name}"
    body_end: int  # index of "\end{name}"
    end: int  # index after "\end{name}"

    def body(self, s: str) -> str:
        return s[self.body_start : self.body_end]


def find_env_end(s: str, name: str, body_start: int) -> tuple[int, int] | None:
    """Find the matching ``\\end{name}`` for an environment whose body starts at ``body_start``."""
    if name in VERBATIM_ENVS:
        m = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}").search(s, body_start)
        return (m.start(), m.end()) if m else None
    pat = re.compile(r"\\(begin|end)\s*\{" + re.escape(name) + r"\}")
    depth = 1
    pos = body_start
    while True:
        m = pat.search(s, pos)
        if not m:
            return None
        if is_escaped(s, m.start()):
            pos = m.end()
            continue
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.start(), m.end()
        pos = m.end()


def find_env(s: str, names: str | tuple[str, ...], start: int = 0) -> Env | None:
    """Find the first environment named in ``names`` at or after ``start``."""
    if isinstance(names, str):
        names = (names,)
    pat = re.compile(r"\\begin\s*\{(" + "|".join(re.escape(n) for n in names) + r")\}")
    pos = start
    while True:
        m = pat.search(s, pos)
        if not m:
            return None
        if is_escaped(s, m.start()):
            pos = m.end()
            continue
        name = m.group(1)
        end = find_env_end(s, name, m.end())
        if end is None:
            return None
        return Env(name, m.start(), m.end(), end[0], end[1])


def iter_envs(s: str, names: str | tuple[str, ...]):
    pos = 0
    while True:
        env = find_env(s, names, pos)
        if env is None:
            return
        yield env
        pos = env.end


def strip_comments(s: str) -> str:
    """Remove TeX comments, leaving verbatim-like regions and ``\\verb`` intact.

    A comment swallows the end of line and the leading whitespace of the next
    line, as TeX does, unless the comment occupies a whole line (then the line
    disappears without joining its neighbours).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    verb_env = re.compile(r"\\begin\s*\{(" + "|".join(re.escape(e) for e in VERBATIM_ENVS) + r")\}")
    while i < n:
        c = s[i]
        if c == "\\":
            m = verb_env.match(s, i)
            if m:
                end = find_env_end(s, m.group(1), m.end())
                stop = end[1] if end else n
                out.append(s[i:stop])
                i = stop
                continue
            m = re.match(r"\\verb\*?(.)", s[i:])
            if m and not m.group(1).isalpha():
                delim = m.group(1)
                j = s.find(delim, i + len(m.group(0)))
                stop = n if j < 0 else j + 1
                out.append(s[i:stop])
                i = stop
                continue
            out.append(s[i : i + 2])
            i += 2
            continue
        if c == "%":
            j = s.find("\n", i)
            if j < 0:
                break
            line_start = s.rfind("\n", 0, i) + 1
            if s[line_start:i].strip() == "":
                # whole-line comment: drop the line entirely
                while out and out[-1] and out[-1][-1] in " \t":
                    out[-1] = out[-1].rstrip(" \t")
                i = j + 1
                continue
            # trailing comment: join with the next line like TeX does
            i = skip_ws(s, j + 1, newlines=False)
            continue
        out.append(c)
        i += 1
    return "".join(out)


def split_top_level(s: str, sep: str = r"\\") -> list[str]:
    """Split on ``sep`` (default the row separator) at brace/environment depth 0.

    ``\\\\[2pt]`` and ``\\\\*`` are treated as plain separators.
    """
    parts: list[str] = []
    depth = 0
    env_depth = 0
    last = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if s.startswith("\\begin", i) and not s[i + 6 : i + 7].isalpha():
                env_depth += 1
                i += 6
                continue
            if s.startswith("\\end", i) and not s[i + 4 : i + 5].isalpha():
                env_depth -= 1
                i += 4
                continue
            if depth == 0 and env_depth == 0 and s.startswith(sep, i):
                parts.append(s[last:i])
                j = i + len(sep)
                if j < n and s[j] == "*":
                    j += 1
                k = skip_ws(s, j)
                if k < n and s[k] == "[":
                    g = read_group(s, k, "[", "]")
                    if g:
                        j = g[1]
                last = j
                i = j
                continue
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    parts.append(s[last:])
    return parts


def strip_top_level(s: str, ch: str = "&") -> str:
    """Remove every ``ch`` that sits at brace/environment depth 0."""
    out: list[str] = []
    depth = 0
    env_depth = 0
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if s.startswith("\\begin", i) and not s[i + 6 : i + 7].isalpha():
                env_depth += 1
            elif s.startswith("\\end", i) and not s[i + 4 : i + 5].isalpha():
                env_depth -= 1
            out.append(s[i : i + 2])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == ch and depth == 0 and env_depth == 0:
            out.append(" ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def remove_command(s: str, name: str, spec: str = "m", keep: int | None = None) -> str:
    """Remove every ``\\name`` with arguments per ``spec``.

    With ``keep`` set to an argument index, the command is replaced by that
    argument's content instead of vanishing.
    """
    pat = command_re(name)
    out: list[str] = []
    pos = 0
    while True:
        m = pat.search(s, pos)
        if not m:
            break
        if is_escaped(s, m.start()):
            out.append(s[pos : m.end()])
            pos = m.end()
            continue
        args, end = parse_command_args(s, m.end(), spec)
        out.append(s[pos : m.start()])
        if keep is not None:
            out.append(args[keep] or "")
        pos = end
    out.append(s[pos:])
    return "".join(out)


def find_commands(s: str, name: str, spec: str = "m"):
    """Yield ``(start, end, args)`` for each occurrence of ``\\name``."""
    pat = command_re(name)
    pos = 0
    while True:
        m = pat.search(s, pos)
        if not m:
            return
        if is_escaped(s, m.start()):
            pos = m.end()
            continue
        args, end = parse_command_args(s, m.end(), spec)
        yield m.start(), end, args
        pos = end


_SYMBOLS = {
    "star": "\u22c6", "ast": "\u2217", "dagger": "\u2020", "ddagger": "\u2021", "prime": "\u2032",
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4", "S": "\u00a7", "P": "\u00b6",
    "dag": "\u2020", "ddag": "\u2021", "TeX": "TeX", "LaTeX": "LaTeX", "circ": "\u2218", "bullet": "\u2022",
    "dots": "\u2026", "ldots": "\u2026", "pm": "\u00b1", "times": "\u00d7", "infty": "\u221e",
}


def latex_to_plain(s: str) -> str:
    """Crude LaTeX-to-text for metadata fields, equation tags and alt text."""
    s = re.sub(r"\\([A-Za-z]+)(?![A-Za-z])\s*", lambda m: _SYMBOLS.get(m.group(1), m.group(0)), s)
    s = re.sub(r"\\(?:textbf|textit|emph|textrm|textsf|texttt|mathrm|mbox|text|textsc|fnm|sur|orgdiv|orgname|orgaddress|street|city|postcode|state|country|url)\s*", "", s)
    s = re.sub(r"\\[A-Za-z@]+\*?", "", s)
    s = s.replace("~", " ").replace("\\&", "&").replace("\\%", "%").replace("\\_", "_")
    s = s.replace("{", "").replace("}", "").replace("$", "")
    s = s.replace("---", "\u2014").replace("--", "\u2013").replace("``", "\u201c").replace("''", "\u201d")
    return re.sub(r"\s+", " ", s).strip()
