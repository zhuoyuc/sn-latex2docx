"""Rewrite the manuscript body into pandoc-friendly LaTeX plus structural markers.

The :class:`Transformer` walks the body once, in document order, so that it
can number headings, equations, floats, algorithms and theorems exactly as
LaTeX would, and bind every ``\\label`` to the object it names. Anything it
does not recognise is passed through for pandoc to handle.
"""

from __future__ import annotations

import logging
import re
import string

from ..model import (
    Algorithm,
    Equation,
    Figure,
    Heading,
    Image,
    Label,
    Panel,
    Registry,
    Table,
    marker,
)
from .scan import (
    VERBATIM_ENVS,
    find_env,
    latex_to_plain,
    find_env_end,
    is_escaped,
    parse_command_args,
    read_group,
    read_optional,
    remove_command,
    skip_ws,
    split_top_level,
    strip_top_level,
)

log = logging.getLogger(__name__)

SECTION_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}
EQ_ENVS = {
    "equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
    "eqnarray", "eqnarray*", "flalign", "flalign*", "alignat", "alignat*", "displaymath",
}
MULTIROW_ENVS = {"align", "gather", "eqnarray", "flalign", "alignat"}
FIG_ENVS = {"figure", "figure*", "sidewaysfigure", "sidewaysfigure*", "wrapfigure", "SCfigure"}
TAB_ENVS = {"table", "table*", "sidewaystable", "sidewaystable*", "wraptable", "longtable"}
TABULAR_ENVS = ("tabular", "tabular*", "tabularx", "tabulary")
ALG_ENVS = {"algorithm", "algorithm*"}

_WALK_RE = re.compile(
    r"\\(section|subsection|subsubsection|bmhead|appendix|label|begin|end\s*\{appendices\}|verb\*?|\[)(?![A-Za-z@])"
    r"|(?<!\\)\$\$"
)

_ENUM_LABELS = {r"\roman*": "i", r"\Roman*": "I", r"\alph*": "a", r"\Alph*": "A", r"\arabic*": "1"}


def _letters(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (appendix numbering)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


class Transformer:
    def __init__(self, registry: Registry, theorems: dict[str, tuple[str, str | None]] | None = None):
        self.reg = registry
        # theorem env name -> (counter name, display name); counter None => unnumbered
        self.theorems = theorems or {}
        self.theorem_counts: dict[str, int] = {}
        self.sec = [0, 0, 0]
        self.appendix = False
        self.appendix_count = 0
        self.eq = 0
        self.fig = 0
        self.tab = 0
        self.alg = 0
        self.listing = 0
        # current \label target: callable(label_name) -> replacement text
        self._target = None

    # ------------------------------------------------------------------ labels
    def _bind(self, name: str, kind: str, text: str, bookmark: str | None = None, field: bool = False) -> Label:
        if name in self.reg.labels:
            log.warning("duplicate label %r", name)
        lab = Label(name, kind, text, bookmark or self.reg.bookmark_for(name), field)
        self.reg.labels[name] = lab
        return lab

    def _anchor_label(self, name: str, kind: str, text: str) -> str:
        lab = self._bind(name, kind, text)
        return marker("ANCHOR", self.reg.add_anchor(lab.bookmark))

    # ------------------------------------------------------------------ walker
    def walk(self, s: str) -> str:
        out: list[str] = []
        pos = 0
        while True:
            m = _WALK_RE.search(s, pos)
            if not m:
                break
            if m.group(0).startswith("\\") and is_escaped(s, m.start()):
                out.append(s[pos : m.end()])
                pos = m.end()
                continue
            out.append(s[pos : m.start()])
            cmd = m.group(1)
            if cmd is None:  # $$ ... $$
                close = s.find("$$", m.end())
                if close < 0:
                    out.append(s[m.start() :])
                    pos = len(s)
                    break
                out.append(self._display([(s[m.end() : close], None, False, None)]))
                pos = close + 2
            elif cmd == "[":
                close = _find_display_close(s, m.end())
                if close < 0:
                    out.append(s[m.start() : m.end()])
                    pos = m.end()
                    continue
                out.append(self._display([(s[m.end() : close], None, False, None)]))
                pos = close + 2
            elif cmd in SECTION_LEVELS or cmd == "bmhead":
                args, end = parse_command_args(s, m.end(), "som" if cmd != "bmhead" else "m")
                if cmd == "bmhead":
                    level, starred, title = 1, True, args[0] or ""
                else:
                    level, starred, title = SECTION_LEVELS[cmd], args[0] == "*", args[2] or ""
                out.append(self._heading(level, not starred, title))
                pos = end
            elif cmd.startswith("verb"):
                delim = s[m.end() : m.end() + 1]
                close = s.find(delim, m.end() + 1) if delim else -1
                stop = len(s) if close < 0 else close + 1
                out.append(s[m.start() : stop])
                pos = stop
            elif cmd == "appendix":
                self._start_appendix()
                pos = m.end()
            elif cmd.startswith("end"):
                pos = m.end()  # \end{appendices}
            elif cmd == "label":
                args, end = parse_command_args(s, m.end(), "m")
                out.append(self._label(args[0] or ""))
                pos = end
            elif cmd == "begin":
                g = read_group(s, skip_ws(s, m.end()))
                if g is None:
                    out.append(s[m.start() : m.end()])
                    pos = m.end()
                    continue
                name, body_start = g[0].strip(), g[1]
                end = find_env_end(s, name, body_start)
                if end is None:
                    log.warning("unterminated environment %s", name)
                    out.append(s[m.start() : body_start])
                    pos = body_start
                    continue
                body = s[body_start : end[0]]
                full = s[m.start() : end[1]]
                text, pos = self._environment(name, body, full, s, m.start(), body_start, end)
                out.append(text)
        out.append(s[pos:])
        return "".join(out)

    def _environment(self, name, body, full, s, start, body_start, end) -> tuple[str, int]:
        """Handle ``\\begin{name}``; returns the replacement and where walking resumes."""
        resume = end[1]
        if name in VERBATIM_ENVS:
            if name == "lstlisting":
                return self._listing(body), resume
            return full, resume
        if name in EQ_ENVS:
            return self._equation_env(name, body), resume
        if name in FIG_ENVS:
            return self._figure(body), resume
        if name in TAB_ENVS:
            return self._table(body, longtable=name == "longtable"), resume
        if name in ALG_ENVS:
            return self._algorithm(body), resume
        if name == "algorithmic":
            return self._algorithm("\\begin{algorithmic}" + body + "\\end{algorithmic}"), resume
        if name in TABULAR_ENVS:
            return "\n\n" + tabular_with_header_marker(full) + "\n\n", resume
        if name == "appendices":
            self._start_appendix()
            resume = body_start
            return "", resume
        if name in self.theorems:
            return self._theorem(name, s, body_start, end), resume
        if name in ("enumerate", "itemize", "description"):
            opt, after = read_optional(s, body_start)
            head = "\\begin{" + name + "}"
            if opt is not None:
                opt = _enum_option(opt) if name == "enumerate" else None
                if opt:
                    head += "[" + opt + "]"
            resume = after
            return head, resume
        # Unknown/other environments: keep the \begin and continue inside.
        resume = body_start
        return s[start:body_start], resume

    # ---------------------------------------------------------------- headings
    def _start_appendix(self):
        if not self.appendix:
            self.appendix = True
            self.sec = [0, 0, 0]

    def _heading(self, level: int, numbered: bool, title: str) -> str:
        number = ""
        if numbered:
            self.sec[level - 1] += 1
            for i in range(level, 3):
                self.sec[i] = 0
            if self.appendix:
                parts = [_letters(self.sec[0])] + [str(n) for n in self.sec[1:level]]
            else:
                parts = [str(n) for n in self.sec[:level]]
            number = ".".join(parts)
        h = Heading(level, numbered, self.appendix, number)
        self.reg.headings.append(h)
        j = len(self.reg.headings) - 1

        def target(label: str) -> str:
            lab = self._bind(label, "appendix" if self.appendix and level == 1 else "section", number, field=numbered)
            if h.bookmark is None:
                h.bookmark = lab.bookmark
            else:  # second label on the same heading: alias via anchor
                return marker("ANCHOR", self.reg.add_anchor(lab.bookmark))
            return ""

        self._target = target
        cmd = {1: "section", 2: "subsection", 3: "subsubsection"}[level]
        # Pull a label that sits inside the title into the heading binding.
        labels = []
        title = _pop_labels(title, labels)
        for lab in labels:
            target(lab)
        return f"\n\n\\{cmd}{{{marker('HEAD', j)}{title.strip()}}}\n\n"

    def _label(self, name: str) -> str:
        name = name.strip()
        if self._target is None:
            lab = self._bind(name, "other", "")
            return marker("ANCHOR", self.reg.add_anchor(lab.bookmark))
        return self._target(name)

    # --------------------------------------------------------------- equations
    def _equation_env(self, name: str, body: str) -> str:
        base = name.rstrip("*")
        starred = name.endswith("*") or name == "displaymath"
        if base == "alignat":
            _, i = parse_command_args(body, 0, "m")
            body = body[i:]
        rows = [r for r in split_top_level(body)]
        # drop a trailing empty row produced by a final "\\"
        while len(rows) > 1 and not rows[-1].strip():
            rows.pop()
        parsed = [_parse_row(r) for r in rows]
        items: list[tuple[str, str | None, bool, str | None]] = []
        if base in MULTIROW_ENVS and not starred and len(rows) > 1:
            for content, label, nonumber, tag in parsed:
                items.append((strip_top_level(content), label, not nonumber or tag is not None, tag))
        elif base in MULTIROW_ENVS or base == "multline" or len(rows) > 1:
            env = {"gather": "gathered", "multline": "gathered"}.get(base, "aligned")
            label = next((p[1] for p in parsed if p[1]), None)
            tag = next((p[3] for p in parsed if p[3]), None)
            nonumber = all(p[2] for p in parsed) if base in MULTIROW_ENVS else any(p[2] for p in parsed)
            content = " \\\\ ".join(p[0] for p in parsed)
            if len(rows) > 1:
                content = f"\\begin{{{env}}}{content}\\end{{{env}}}"
            items.append((content, label, (not starred and not nonumber) or tag is not None, tag))
        else:
            content, label, nonumber, tag = parsed[0]
            items.append((content, label, (not starred and not nonumber) or tag is not None, tag))
        return self._display(items)

    def _display(self, items) -> str:
        """Emit display equations. ``items``: (content, label, numbered, tag)."""
        out = []
        for content, label, numbered, tag in items:
            number = None
            if tag is not None:
                number = latex_to_plain(tag)
            elif numbered:
                self.eq += 1
                number = str(self.eq)
            eq = Equation(number, tag=tag is not None)
            content, inner_labels = _pop_all_labels(content)
            if label is None and inner_labels:
                label = inner_labels[0]
            if label:
                lab = self._bind(label, "equation", number or "", field=number is not None and tag is None)
                eq.bookmark = lab.bookmark
            self.reg.equations.append(eq)
            k = len(self.reg.equations) - 1
            content = replace_refs(content, self.reg, math=True)
            content = content.strip()
            if not content:
                continue
            out.append(f"\n\n{marker('EQ', k)}\n\n\\[{content}\\]\n\n")
        return "".join(out)

    # ----------------------------------------------------------------- figures
    def _figure(self, body: str) -> str:
        panels: list[Panel] = []
        caption: str | None = None
        main_label: str | None = None
        subcaps: list[tuple[int, str]] = []
        pos = 0
        pat = re.compile(r"\\(begin\s*\{subfigure\}|subfloat|subfigure|subcaptionbox|includegraphics|caption|label)(?![A-Za-z@])")
        sub_labels: list[tuple[int, str]] = []
        while True:
            m = pat.search(body, pos)
            if not m:
                break
            if is_escaped(body, m.start()):
                pos = m.end()
                continue
            kind = m.group(1)
            if kind.startswith("begin"):
                end = find_env_end(body, "subfigure", m.end())
                if end is None:
                    pos = m.end()
                    continue
                inner = body[m.end() : end[0]]
                _, i = parse_command_args(inner, 0, "om")
                inner = inner[i:]
                imgs = _images(inner)
                cap = _first_command(inner, "caption", "om", 1)
                labs: list[str] = []
                _pop_labels(inner, labs)
                if cap is not None:
                    cap = _pop_labels(cap, [])
                panels.append(Panel(imgs, caption=cap is not None))
                if cap is not None:
                    subcaps.append((len(panels) - 1, cap))
                for lab in labs:
                    sub_labels.append((len(panels) - 1, lab))
                pos = end[1]
            elif kind in ("subfloat", "subfigure"):
                args, end = parse_command_args(body, m.end(), "oom")
                inner = args[2] or ""
                cap = args[1] if args[1] is not None else args[0]
                labs = []
                if cap is not None:
                    cap = _pop_labels(cap, labs)
                _pop_labels(inner, labs)
                panels.append(Panel(_images(inner), caption=bool(cap and cap.strip())))
                if cap and cap.strip():
                    subcaps.append((len(panels) - 1, cap))
                for lab in labs:
                    sub_labels.append((len(panels) - 1, lab))
                pos = end
            elif kind == "subcaptionbox":
                args, end = parse_command_args(body, m.end(), "somom")
                cap = args[1] or ""
                labs = []
                cap = _pop_labels(cap, labs)
                inner = args[4] or ""
                _pop_labels(inner, labs)
                panels.append(Panel(_images(inner), caption=bool(cap.strip())))
                if cap.strip():
                    subcaps.append((len(panels) - 1, cap))
                for lab in labs:
                    sub_labels.append((len(panels) - 1, lab))
                pos = end
            elif kind == "includegraphics":
                args, end = parse_command_args(body, m.end(), "som")
                panels.append(Panel([Image((args[2] or "").strip(), args[1])]))
                pos = end
            elif kind == "caption":
                args, end = parse_command_args(body, m.end(), "som")
                labs = []
                caption = _pop_labels(args[2] or "", labs)
                if labs:
                    main_label = labs[0]
                pos = end
            else:  # label
                args, end = parse_command_args(body, m.end(), "m")
                if main_label is None:
                    main_label = (args[0] or "").strip()
                pos = end
        number = None
        if caption is not None:
            self.fig += 1
            number = str(self.fig)
        fig = Figure(number, panels, caption is not None)
        if main_label:
            fig.bookmark = self._bind(main_label, "figure", number or "", field=number is not None).bookmark
        k = len(self.reg.figures)
        self.reg.figures.append(fig)
        letter = 0
        for idx, _ in subcaps:
            letter += 1
            panels[idx].letter = string.ascii_lowercase[letter - 1]
        for idx, lab in sub_labels:
            text = f"{number or ''}{panels[idx].letter}"
            panels[idx].bookmark = self._bind(lab, "subfigure", text).bookmark
        parts = [f"\n\n{marker('FIG', k)}\n\n"]
        for idx, cap in subcaps:
            parts.append(f"{marker('SUBCAP', k, idx)} {self.walk(cap).strip()}\n\n")
        if caption is not None:
            parts.append(f"{marker('FIGCAP', k)} {self.walk(caption).strip()}\n\n")
        parts.append(f"{marker('FIGEND', k)}\n\n")
        return "".join(parts)

    # ------------------------------------------------------------------ tables
    def _table(self, body: str, longtable: bool = False) -> str:
        caption = None
        label = None
        if longtable:
            cap_m = re.search(r"\\caption(?![A-Za-z@])", body)
            if cap_m:
                args, end = parse_command_args(body, cap_m.end(), "som")
                caption = args[2]
                # drop the caption row terminator
                rest = body[end:]
                rest = re.sub(r"^\s*(\\label\{[^}]*\}\s*)?\\\\", lambda mm: mm.group(1) or "", rest)
                body = body[: cap_m.start()] + rest
            body = _longtable_heads(body)
            spec, i = parse_command_args(body, 0, "om")
            body = "\\begin{tabular}{" + (spec[1] or "") + "}" + body[i:] + "\\end{tabular}"
        else:
            for start, end, args in _commands(body, "caption", "som"):
                caption = args[2]
                break
        labs: list[str] = []
        if caption is not None:
            caption = _pop_labels(caption, labs)
        body_wo_caption = remove_command(body, "caption", "som")
        _pop_labels(body_wo_caption, labs)
        label = labs[0] if labs else None

        notes: list[tuple[str | None, str]] = []
        for _, _, args in _commands(body_wo_caption, "footnotetext", "om"):
            notes.append((args[0], args[1] or ""))
        tn = find_env(body_wo_caption, "tablenotes")
        if tn is not None:
            for item in re.split(r"\\item(?![A-Za-z@])", tn.body(body_wo_caption))[1:]:
                opt, i = read_optional(item, 0)
                notes.append((opt, item[i:].strip()))

        tabulars = []
        pos = 0
        while True:
            env = find_env(body_wo_caption, TABULAR_ENVS, pos)
            if env is None:
                break
            tabulars.append(sanitize_tabular(body_wo_caption[env.start : env.end]))  # walk() adds TBLHDR
            pos = env.end
        images = _images(body_wo_caption) if not tabulars else []

        number = None
        if caption is not None:
            self.tab += 1
            number = str(self.tab)
        tab = Table(number, caption is not None, len(notes), panels=[Panel([img]) for img in images])
        if label:
            tab.bookmark = self._bind(label, "table", number or "", field=number is not None).bookmark
        k = len(self.reg.tables)
        self.reg.tables.append(tab)
        parts = [f"\n\n{marker('TAB', k)}\n\n"]
        if caption is not None:
            parts.append(f"{marker('TABCAP', k)} {self.walk(caption).strip()}\n\n")
        for t in tabulars:
            parts.append(self.walk(t) + "\n\n")
        for j, (mark, text) in enumerate(notes):
            sup = f"\\textsuperscript{{{mark.strip()}}}" if mark else ""
            parts.append(f"{marker('TABNOTE', k, j)} {sup}{self.walk(text).strip()}\n\n")
        parts.append(f"{marker('TABEND', k)}\n\n")
        return "".join(parts)

    # -------------------------------------------------------------- algorithms
    def _algorithm(self, body: str) -> str:
        caption = None
        labs: list[str] = []
        for _, _, args in _commands(body, "caption", "som"):
            caption = _pop_labels(args[2] or "", labs)
            break
        rest = remove_command(body, "caption", "som")
        env = find_env(rest, "algorithmic")
        lines_src = ""
        numbered_every = 0
        outside = rest
        if env is not None:
            inner = env.body(rest)
            opt, i = read_optional(inner, 0)
            if opt and opt.strip().isdigit():
                numbered_every = int(opt.strip())
            lines_src = inner[i:]
            outside = rest[: env.start] + rest[env.end :]
        _pop_labels(outside, labs)
        number = None
        if caption is not None:
            self.alg += 1
            number = str(self.alg)
        alg = Algorithm(number, caption is not None)
        if labs:
            alg.bookmark = self._bind(labs[0], "algorithm", number or "", field=number is not None).bookmark
        k = len(self.reg.algorithms)
        self.reg.algorithms.append(alg)
        parts = [f"\n\n{marker('ALG', k)}\n\n"]
        if caption is not None:
            parts.append(f"{marker('ALGCAP', k)} {self.walk(caption).strip()}\n\n")
        for indent, lineno, text, line_labels in parse_algorithmic(lines_src, numbered_every):
            anchors = "".join(self._anchor_label(lab, "line", str(lineno or "")) for lab in line_labels)
            parts.append(f"{marker('ALGLINE', k, indent, lineno)} {anchors}{self.walk(text).strip()}\n\n")
        parts.append(f"{marker('ALGEND', k)}\n\n")
        return "".join(parts)

    # ---------------------------------------------------------------- theorems
    def _theorem(self, name: str, s: str, body_start: int, end: tuple[int, int]) -> str:
        counter, _title = self.theorems[name]
        opt, after = read_optional(s, body_start)
        number = ""
        if counter is not None:
            self.theorem_counts[counter] = self.theorem_counts.get(counter, 0) + 1
            number = str(self.theorem_counts[counter])
        saved = self._target

        def target(label: str) -> str:
            return self._anchor_label(label, "theorem", number)

        self._target = target
        inner = self.walk(s[after : end[0]])
        self._target = saved if saved is not None else None
        head = f"\\begin{{{name}}}" + (f"[{opt}]" if opt is not None else "")
        return f"{head}{inner}\\end{{{name}}}"

    # ---------------------------------------------------------------- listings
    def _listing(self, body: str) -> str:
        opt, i = read_optional(body, 0)
        code = body[i:]
        code = re.sub(r"\(\*\s*(?:\\text(?:rm|it|bf|sf)\s*)?\{?(.*?)\}?\s*\*\)", r"\1", code)
        head = ""
        keep_opts = []
        if opt:
            opts = _keyvals(opt)
            if "language" in opts:
                keep_opts.append("language=" + opts["language"])
            if "caption" in opts:
                self.listing += 1
                cap = opts["caption"].strip("{}")
                anchor = ""
                if "label" in opts:
                    anchor = self._anchor_label(opts["label"].strip("{}"), "listing", str(self.listing))
                head = f"\n\n\\textbf{{Listing {self.listing}:}} {anchor}{cap}\n\n"
        o = "[" + ",".join(keep_opts) + "]" if keep_opts else ""
        return f"{head}\\begin{{lstlisting}}{o}{code}\\end{{lstlisting}}"


# --------------------------------------------------------------------- helpers
def _find_display_close(s: str, i: int) -> int:
    m = re.compile(r"\\\]").search(s, i)
    while m and is_escaped(s, m.start()):
        m = re.compile(r"\\\]").search(s, m.end())
    return m.start() if m else -1


def _parse_row(row: str) -> tuple[str, str | None, bool, str | None]:
    labels: list[str] = []
    row = _pop_labels(row, labels)
    nonumber = bool(re.search(r"\\(nonumber|notag)(?![A-Za-z])", row))
    row = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", row)
    tag = None
    for _, _, args in _commands(row, "tag", "sm"):
        tag = args[1]
    row = remove_command(row, "tag", "sm")
    return row.strip(), (labels[0] if labels else None), nonumber, tag


def _pop_labels(text: str, sink: list[str]) -> str:
    out = []
    pos = 0
    for start, end, args in _commands(text, "label", "m"):
        sink.append((args[0] or "").strip())
        out.append(text[pos:start])
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _pop_all_labels(text: str) -> tuple[str, list[str]]:
    labels: list[str] = []
    return _pop_labels(text, labels), labels


def _commands(text: str, name: str, spec: str):
    from .scan import find_commands

    yield from find_commands(text, name, spec)


def _first_command(text: str, name: str, spec: str, index: int) -> str | None:
    for _, _, args in _commands(text, name, spec):
        return args[index] or ""
    return None


def _images(text: str) -> list[Image]:
    return [Image((a[2] or "").strip(), a[1]) for _, _, a in _commands(text, "includegraphics", "som")]


def _keyvals(opt: str) -> dict[str, str]:
    out: dict[str, str] = {}
    depth = 0
    cur = ""
    parts = []
    for c in opt:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    parts.append(cur)
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v.strip()
        elif p.strip():
            out[p.strip()] = ""
    return out


def _enum_option(opt: str) -> str | None:
    """Translate an enumerate optional argument into the enumerate-package form pandoc knows."""
    kv = _keyvals(opt)
    if "label" in kv:
        lab = kv["label"].strip("{}")
        for k, v in _ENUM_LABELS.items():
            lab = lab.replace(k, v)
        return lab if re.fullmatch(r"[\(\[]?[iIaA1][\)\]\.:]?", lab) else None
    if "=" in opt:
        return None
    return opt if re.fullmatch(r"\s*[\(\[]?[iIaA1][\)\]\.:]?\s*", opt) else None


def _longtable_heads(body: str) -> str:
    """Keep the first header of a longtable, drop continuation heads/feet."""
    if "\\endfirsthead" in body:
        first, rest = body.split("\\endfirsthead", 1)
        if "\\endhead" in rest:
            rest = rest.split("\\endhead", 1)[1]
        body = first + rest
    else:
        body = body.replace("\\endhead", "")
    for marker_ in ("\\endfoot", "\\endlastfoot"):
        if marker_ in body:
            before, after = body.split(marker_, 1)
            # the foot rows sit between the previous \end... marker and here; drop them
            body = before[: before.rfind("\\\\") + 2] if "\\\\" in before else before
            body += after
    return body


# ------------------------------------------------------------------ tabulars
def clean_colspec(spec: str) -> str:
    out = []
    i = 0
    n = len(spec)
    while i < n:
        c = spec[i]
        if c in "@!<>":
            g = read_group(spec, skip_ws(spec, i + 1))
            i = g[1] if g else i + 1
            continue
        if c == "*":
            args, j = parse_command_args(spec, i + 1, "mm")
            try:
                out.append(clean_colspec(args[1] or "") * int((args[0] or "1").strip()))
            except ValueError:
                pass
            i = j
            continue
        if c in "pmbw":
            g = read_group(spec, skip_ws(spec, i + 1))
            if g:
                out.append("p{" + g[0] + "}")
                i = g[1]
                continue
        if c == "S":
            if i + 1 < n and spec[i + 1] == "[":
                g = read_group(spec, i + 1, "[", "]")
                i = g[1] if g else i + 1
            else:
                i += 1
            out.append("c")
            continue
        if c in "lcr":
            out.append(c)
        elif c in "LCR":
            out.append(c.lower())
        elif c in "XJY":
            out.append("l")
        i += 1
    return "".join(out) or "l"


def header_rows(tabular: str) -> int:
    """Rows above the first ``\\midrule`` (or the first ``\\hline`` after a row) of a sanitized tabular."""
    m = re.match(r"\\begin\{tabular\}\{[^}]*\}", tabular)
    body = tabular[m.end() :] if m else tabular
    body = re.sub(r"\\end\{tabular\}\s*$", "", body)
    body = re.sub(r"^\s*\\(toprule|hline)", "", body)
    rule = re.search(r"\\(midrule|hline)(?![A-Za-z])", body)
    if not rule:
        return 0
    rows = [r for r in split_top_level(body[: rule.start()]) if r.strip()]
    return len(rows)


def tabular_with_header_marker(src: str) -> str:
    """Sanitized tabular preceded by a marker paragraph telling how many header rows it has."""
    tab = sanitize_tabular(src)
    return f"{marker('TBLHDR', header_rows(tab))}\n\n{tab}"


def sanitize_tabular(src: str) -> str:
    """Rewrite any tabular-like environment into a plain ``tabular`` pandoc can parse."""
    m = re.match(r"\\begin\s*\{(tabular\*?|tabularx|tabulary)\}", src)
    if not m:
        return src
    name = m.group(1)
    i = m.end()
    if name in ("tabular*", "tabularx", "tabulary"):
        _, i = parse_command_args(src, i, "m")
    _, i = read_optional(src, i)
    args, i = parse_command_args(src, i, "m")
    spec = clean_colspec(args[0] or "")
    end = re.search(r"\\end\s*\{" + re.escape(name) + r"\}\s*$", src)
    body = src[i : end.start()] if end else src[i:]
    body = _clean_table_body(body)
    return "\\begin{tabular}{" + spec + "}" + body + "\\end{tabular}"


def _clean_table_body(body: str) -> str:
    body = re.sub(r"\\botrule(?![A-Za-z])", r"\\bottomrule", body)
    body = re.sub(r"\\(toprule|midrule|bottomrule)\s*\[[^\]]*\]", r"\\\1", body)
    body = re.sub(r"\\specialrule\s*\{[^}]*\}\s*\{[^}]*\}\s*\{[^}]*\}", r"\\midrule", body)
    # partial rules: the Word table gets borders under spanning header cells instead
    body = re.sub(r"\\cmidrule\s*(\([^)]*\))?\s*(\[[^\]]*\])?\s*\{[^}]*\}", "", body)
    body = re.sub(r"\\cline\s*\{[^}]*\}", "", body)
    for name, spec in (("rowcolor", "om"), ("cellcolor", "om"), ("arrayrulecolor", "om"), ("addlinespace", "o"),
                       ("noalign", "m"), ("rule", "omm"), ("morecmidrules", ""), ("centering", ""),
                       ("raggedright", ""), ("raggedleft", "")):
        body = remove_command(body, name, spec)
    # \footnotemark[1] / \tnote{a} inside cells become superscripts
    body = re.sub(r"\\footnotemark\s*\[([^\]]*)\]", r"\\textsuperscript{\1}", body)
    body = re.sub(r"\\tnote\s*\{([^}]*)\}", r"\\textsuperscript{\1}", body)
    # \multicolumn column specs
    out = []
    pos = 0
    for start, end, args in _commands(body, "multicolumn", "mmm"):
        out.append(body[pos:start])
        out.append("\\multicolumn{" + (args[0] or "1") + "}{" + clean_colspec(args[1] or "c") + "}{" + (args[2] or "") + "}")
        pos = end
    out.append(body[pos:])
    body = "".join(out)
    # \makecell{a\\b} -> a b
    out = []
    pos = 0
    for start, end, args in _commands(body, "makecell", "om"):
        out.append(body[pos:start])
        out.append(" ".join(split_top_level(args[1] or "")))
        pos = end
    out.append(body[pos:])
    return "".join(out)


# ---------------------------------------------------------------- algorithms
_ALG_START = {
    "State": ("", 0, 0, True), "STATE": ("", 0, 0, True),
    "Statex": ("", 0, 0, False),
    "If": ("if", 0, 1, True), "IF": ("if", 0, 1, True),
    "ElsIf": ("else if", -1, 1, True), "ELSIF": ("else if", -1, 1, True),
    "Else": ("else", -1, 1, True), "ELSE": ("else", -1, 1, True),
    "EndIf": ("end if", -1, 0, True), "ENDIF": ("end if", -1, 0, True),
    "For": ("for", 0, 1, True), "FOR": ("for", 0, 1, True),
    "ForAll": ("for all", 0, 1, True), "FORALL": ("for all", 0, 1, True),
    "EndFor": ("end for", -1, 0, True), "ENDFOR": ("end for", -1, 0, True),
    "While": ("while", 0, 1, True), "WHILE": ("while", 0, 1, True),
    "EndWhile": ("end while", -1, 0, True), "ENDWHILE": ("end while", -1, 0, True),
    "Repeat": ("repeat", 0, 1, True), "REPEAT": ("repeat", 0, 1, True),
    "Until": ("until", -1, 0, True), "UNTIL": ("until", -1, 0, True),
    "Loop": ("loop", 0, 1, True), "LOOP": ("loop", 0, 1, True),
    "EndLoop": ("end loop", -1, 0, True), "ENDLOOP": ("end loop", -1, 0, True),
    "Function": ("function", 0, 1, True), "EndFunction": ("end function", -1, 0, True),
    "Procedure": ("procedure", 0, 1, True), "EndProcedure": ("end procedure", -1, 0, True),
    "Require": ("Require:", 0, 0, False), "REQUIRE": ("Require:", 0, 0, False),
    "Ensure": ("Ensure:", 0, 0, False), "ENSURE": ("Ensure:", 0, 0, False),
    "Input": ("Input:", 0, 0, False), "Output": ("Output:", 0, 0, False),
    "RETURN": ("return", 0, 0, True), "PRINT": ("print", 0, 0, True),
}
_ALG_RE = re.compile(r"\\(" + "|".join(sorted(_ALG_START, key=len, reverse=True)) + r")(?![A-Za-z])")


def _alg_inline(text: str) -> str:
    text = re.sub(r"\\(Return|RETURN)(?![A-Za-z])", r"\\textbf{return}", text)
    text = re.sub(r"\\(And|AND)(?![A-Za-z])", r"\\textbf{and}", text)
    text = re.sub(r"\\(Or|OR)(?![A-Za-z])", r"\\textbf{or}", text)
    text = re.sub(r"\\(Not|NOT)(?![A-Za-z])", r"\\textbf{not}", text)
    text = re.sub(r"\\(True|TRUE)(?![A-Za-z])", r"\\textsc{true}", text)
    text = re.sub(r"\\(False|FALSE)(?![A-Za-z])", r"\\textsc{false}", text)
    out, pos = [], 0
    for start, end, args in _commands(text, "Call", "mm"):
        out.append(text[pos:start] + "\\textsc{" + (args[0] or "") + "}(" + (args[1] or "") + ")")
        pos = end
    out.append(text[pos:])
    text = "".join(out)
    out, pos = [], 0
    for start, end, args in _commands(text, "Comment", "m"):
        out.append(text[pos:start] + "\u2003\u25b7 " + (args[0] or ""))
        pos = end
    out.append(text[pos:])
    text = "".join(out)
    text = re.sub(r"\\COMMENT\s*\{([^}]*)\}", "\u2003\u25b7 \\1", text)
    return text


def parse_algorithmic(src: str, numbered_every: int) -> list[tuple[int, int, str, list[str]]]:
    """Return (indent, line number or 0, LaTeX text, labels) for each algorithm line."""
    lines: list[tuple[int, int, str, list[str]]] = []
    indent = 0
    lineno = 0
    matches = [m for m in _ALG_RE.finditer(src) if not is_escaped(src, m.start())]
    for idx, m in enumerate(matches):
        name = m.group(1)
        word, before, after, counts = _ALG_START[name]
        seg_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(src)
        rest = src[m.end() : seg_end]
        text = ""
        if name in ("Function", "Procedure"):
            args, i = parse_command_args(rest, 0, "mm")
            text = f"\\textbf{{{word}}} \\textsc{{{args[0]}}}({args[1]})"
            rest = rest[i:]
        elif name in ("If", "IF", "ElsIf", "ELSIF", "While", "WHILE", "For", "FOR", "ForAll", "FORALL", "Until", "UNTIL"):
            args, i = parse_command_args(rest, 0, "m")
            cond = args[0] or ""
            tail = {"if": "then", "else if": "then", "while": "do", "for": "do", "for all": "do"}.get(word)
            text = f"\\textbf{{{word}}} {cond}" + (f" \\textbf{{{tail}}}" if tail else "")
            rest = rest[i:]
        elif name in ("Else", "ELSE"):
            opt, i = read_optional(rest, 0)
            text = "\\textbf{else}" + (f"\u2003\u25b7 {opt}" if opt else "")
            rest = rest[i:]
        elif word:
            text = f"\\textbf{{{word}}}"
        labels: list[str] = []
        rest = _pop_labels(rest, labels)
        text = (text + " " + rest.strip()).strip()
        text = _alg_inline(text)
        indent = max(0, indent + before)
        number = 0
        if counts and numbered_every:
            lineno += 1
            number = lineno if lineno % numbered_every == 0 else 0
        lines.append((indent, number, text, labels))
        indent = max(0, indent + after)  # else/elsif are dedented for their own line only
    return lines


# ------------------------------------------------------------------ references
_REF_RE = re.compile(
    r"\\(ref|eqref|autoref|cref|Cref|nameref|pageref|subref|cpageref|labelcref|crefrange|Crefrange)\*?(?![A-Za-z@])"
)


def replace_refs(text: str, reg: Registry, math: bool = False) -> str:
    """Replace reference commands by REF markers (``\\text``-wrapped inside math).

    Marker variants: ``ref`` bare number, ``eq`` parenthesised, ``cref``/``Cref``
    with a type name (``+`` suffix: plural name), ``bare`` formatted like its
    type but without a name (later items of a list or range).
    """
    out: list[str] = []
    pos = 0
    while True:
        m = _REF_RE.search(text, pos)
        if not m:
            break
        if is_escaped(text, m.start()):
            out.append(text[pos : m.end()])
            pos = m.end()
            continue
        cmd = m.group(1)
        if cmd in ("crefrange", "Crefrange"):
            args, end = parse_command_args(text, m.end(), "mm")
            a = marker("REF", reg.add_ref((args[0] or "").strip(), cmd[:4] + "+"))
            b = marker("REF", reg.add_ref((args[1] or "").strip(), "bare"))
            joined = f"{a}\u2013{b}"
            out.append(text[pos : m.start()] + (f"\\text{{{joined}}}" if math else joined))
            pos = end
            continue
        args, end = parse_command_args(text, m.end(), "m")
        out.append(text[pos : m.start()])
        if cmd in ("pageref", "cpageref"):
            pos = end
            continue
        keys = [k.strip() for k in (args[0] or "").split(",") if k.strip()]
        base = {"eqref": "eq", "autoref": "Cref", "cref": "cref", "Cref": "Cref"}.get(cmd, "ref")
        pieces = []
        for n, key in enumerate(keys):
            if base in ("cref", "Cref"):
                variant = (base + "+" if len(keys) > 1 else base) if n == 0 else "bare"
            else:
                variant = base
            pieces.append(marker("REF", reg.add_ref(key, variant)))
        if len(pieces) > 2:
            joined = ", ".join(pieces[:-1]) + " and " + pieces[-1]
        else:
            joined = " and ".join(pieces)
        out.append(f"\\text{{{joined}}}" if math else joined)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def replace_refs_outside_math(text: str, reg: Registry) -> str:
    """Apply :func:`replace_refs` to text, using ``\\text`` wrappers inside inline math."""
    out: list[str] = []
    i = 0
    n = len(text)
    buf_start = 0
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "(":
                close = text.find("\\)", i + 2)
                if close > 0:
                    out.append(replace_refs(text[buf_start:i], reg))
                    out.append("\\(" + replace_refs(text[i + 2 : close], reg, math=True) + "\\)")
                    i = buf_start = close + 2
                    continue
            i += 2
            continue
        if c == "$":
            close = i + 1
            while close < n:
                if text[close] == "$" and not is_escaped(text, close):
                    break
                close += 1
            if close < n:
                out.append(replace_refs(text[buf_start:i], reg))
                out.append("$" + replace_refs(text[i + 1 : close], reg, math=True) + "$")
                i = buf_start = close + 1
                continue
        i += 1
    out.append(replace_refs(text[buf_start:], reg))
    return "".join(out)
