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
    Theorem,
    TheoremSpec,
    marker,
)
from .extras import join_list
from .scan import (
    VERBATIM_ENVS,
    find_commands,
    find_env,
    find_env_end,
    first_arg,
    is_escaped,
    latex_to_plain,
    parse_command_args,
    read_group,
    read_optional,
    remove_command,
    replace_commands,
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

_FIGURE_ITEM_RE = re.compile(
    r"\\(begin\s*\{subfigure\}|subfloat|subfigure|subcaptionbox|includegraphics|caption|label)(?![A-Za-z@])"
)
_DISPLAY_CLOSE_RE = re.compile(r"\\\]")

_ENUM_LABELS = {r"\roman*": "i", r"\Roman*": "I", r"\alph*": "a", r"\Alph*": "A", r"\arabic*": "1"}


def _letters(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (appendix numbering)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


class Transformer:
    def __init__(self, registry: Registry, theorems: dict[str, TheoremSpec] | None = None):
        self.reg = registry
        self.theorems = theorems or {}  # environment name -> declaration
        self.theorem_counts: dict[str, int] = {}
        self.sec = [0, 0, 0]
        self.appendix = False
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
            return "\n\n" + self._tabular(full) + "\n\n", resume
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
            # counters numbered within this level (or a deeper one) restart
            for spec in self.theorems.values():
                if spec.counter and spec.within >= level:
                    self.theorem_counts.pop(spec.counter, None)
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
        rows = split_top_level(body)
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
            else:  # a single row needs no alignment, and "&" outside an aligned block breaks pandoc
                content = strip_top_level(content)
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
            inner_labels: list[str] = []
            content = _pop_labels(content, inner_labels)
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
        sub_labels: list[tuple[int, str]] = []

        def add_panel(inner: str, cap: str | None) -> None:
            labs: list[str] = []
            _pop_labels(inner, labs)
            cap = _pop_labels(cap, labs) if cap is not None else None
            has_caption = bool(cap and cap.strip())
            panels.append(Panel(_images(inner), caption=has_caption))
            if has_caption:
                subcaps.append((len(panels) - 1, cap))
            sub_labels.extend((len(panels) - 1, lab) for lab in labs)

        pos = 0
        while True:
            m = _FIGURE_ITEM_RE.search(body, pos)
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
                add_panel(inner, first_arg(inner, "caption", "om", 1))
                pos = end[1]
            elif kind in ("subfloat", "subfigure"):
                args, pos = parse_command_args(body, m.end(), "oom")
                add_panel(args[2] or "", args[1] if args[1] is not None else args[0])
            elif kind == "subcaptionbox":
                args, pos = parse_command_args(body, m.end(), "somom")
                add_panel(args[4] or "", args[1] or "")
            elif kind == "includegraphics":
                args, pos = parse_command_args(body, m.end(), "som")
                panels.append(Panel([Image((args[2] or "").strip(), args[1])]))
            elif kind == "caption":
                args, pos = parse_command_args(body, m.end(), "som")
                labs: list[str] = []
                caption = _pop_labels(args[2] or "", labs)
                if labs:
                    main_label = labs[0]
            else:  # label
                args, pos = parse_command_args(body, m.end(), "m")
                if main_label is None:
                    main_label = (args[0] or "").strip()
        number = None
        if caption is not None:
            self.fig += 1
            number = str(self.fig)
        fig = Figure(number, panels, caption is not None)
        if main_label:
            fig.bookmark = self._bind(main_label, "figure", number or "", field=number is not None).bookmark
        k = len(self.reg.figures)
        self.reg.figures.append(fig)
        for n, (idx, _) in enumerate(subcaps):
            panels[idx].letter = string.ascii_lowercase[n % 26]
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
    def _tabular(self, src: str) -> str:
        """Sanitised tabular preceded by a TBLHDR marker; cell contents are walked.

        Marker arguments: header row count, then ``row, first, last`` column triples for
        every partial rule (``\\cmidrule``/``\\cline``) drawn under a row.
        """
        parts = _split_tabular(src)
        if parts is None:
            return src
        _, spec, body = parts
        rules = [n for triple in partial_rules(body) for n in triple]
        tab = "\\begin{tabular}{" + clean_colspec(spec) + "}" + _clean_table_body(body) + "\\end{tabular}"
        body_start = _tabular_body_start(tab)
        cells = self.walk(tab[body_start : len(tab) - len("\\end{tabular}")])
        return f"{marker('TBLHDR', header_rows(tab), *rules)}\n\n{tab[:body_start]}{cells}\\end{{tabular}}"

    def _table(self, body: str, longtable: bool = False) -> str:
        caption = None
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
            caption = first_arg(body, "caption", "som", 2)
        labs: list[str] = []
        if caption is not None:
            caption = _pop_labels(caption, labs)
        body_wo_caption = remove_command(body, "caption", "som")
        body_wo_caption = _pop_labels(body_wo_caption, labs)
        label = labs[0] if labs else None

        notes: list[tuple[str | None, str]] = []
        for _, _, args in find_commands(body_wo_caption, "footnotetext", spec="om"):
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
            tabulars.append(self._tabular(body_wo_caption[env.start : env.end]))
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
            parts.append(t + "\n\n")
        for j, (mark, text) in enumerate(notes):
            sup = f"\\textsuperscript{{{mark.strip()}}}" if mark else ""
            parts.append(f"{marker('TABNOTE', k, j)} {sup}{self.walk(text).strip()}\n\n")
        parts.append(f"{marker('TABEND', k)}\n\n")
        return "".join(parts)

    # -------------------------------------------------------------- algorithms
    def _algorithm(self, body: str) -> str:
        labs: list[str] = []
        caption = first_arg(body, "caption", "som", 2)
        if caption is not None:
            caption = _pop_labels(caption, labs)
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
        spec = self.theorems[name]
        opt, after = read_optional(s, body_start)
        number = ""
        if spec.counter is not None:
            self.theorem_counts[spec.counter] = self.theorem_counts.get(spec.counter, 0) + 1
            number = str(self.theorem_counts[spec.counter])
            if spec.within:
                number = ".".join([*self._section_number(spec.within), number])
        thm = Theorem(f"{spec.title} {number}".strip(), spec.style, has_note=bool(opt and opt.strip()))
        self.reg.theorems.append(thm)
        k = len(self.reg.theorems) - 1
        saved = self._target

        def target(label: str) -> str:
            return self._anchor_label(label, "theorem", number)

        self._target = target
        inner = self.walk(s[after : end[0]])
        self._target = saved
        note = f"{marker('THMNOTE', k)} {self.walk(opt).strip()}\n\n" if thm.has_note else ""
        return f"\n\n{marker('THM', k)}\n\n{note}{inner.strip()}\n\n{marker('THMEND', k)}\n\n"

    def _section_number(self, depth: int) -> list[str]:
        first = _letters(self.sec[0]) if self.appendix else str(self.sec[0])
        return [first, *(str(n) for n in self.sec[1:depth])]

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
    m = _DISPLAY_CLOSE_RE.search(s, i)
    while m and is_escaped(s, m.start()):
        m = _DISPLAY_CLOSE_RE.search(s, m.end())
    return m.start() if m else -1


def _parse_row(row: str) -> tuple[str, str | None, bool, str | None]:
    labels: list[str] = []
    row = _pop_labels(row, labels)
    nonumber = bool(re.search(r"\\(nonumber|notag)(?![A-Za-z])", row))
    row = re.sub(r"\\(nonumber|notag)(?![A-Za-z])", "", row)
    tag = None
    for _, _, args in find_commands(row, "tag", spec="sm"):
        tag = args[1]
    row = remove_command(row, "tag", "sm")
    return row.strip(), (labels[0] if labels else None), nonumber, tag


def _pop_labels(text: str, sink: list[str]) -> str:
    """Remove ``\\label`` commands from ``text``, appending their keys to ``sink``."""
    return replace_commands(text, {"label": ("m", lambda a: sink.append((a[0] or "").strip()) or "")})


def _images(text: str) -> list[Image]:
    return [Image((a[2] or "").strip(), a[1]) for _, _, a in find_commands(text, "includegraphics", spec="som")]


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
    """Keep the first header and the body of a longtable; drop continuation heads and feet.

    Each ``\\endfirsthead``/``\\endhead``/``\\endfoot``/``\\endlastfoot`` ends the section
    of rows written before it; the rows after the last such marker are the body.
    """
    pieces = re.split(r"\\(endfirsthead|endhead|endfoot|endlastfoot)(?![A-Za-z])", body)
    sections = dict(zip(pieces[1::2], pieces[0::2]))  # marker -> the rows it closes
    head = sections.get("endfirsthead", sections.get("endhead", ""))
    return head + pieces[-1]


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


def _tabular_body_start(tab: str) -> int | None:
    """Index just after ``\\begin{tabular}{spec}`` of a sanitised tabular."""
    if not tab.startswith("\\begin{tabular}"):
        return None
    g = read_group(tab, len("\\begin{tabular}"))
    return g[1] if g else None


def header_rows(tabular: str) -> int:
    """Rows above the first ``\\midrule`` (or the first ``\\hline`` after a row) of a sanitised tabular."""
    start = _tabular_body_start(tabular)
    body = tabular[start:] if start is not None else tabular
    body = re.sub(r"\\end\{tabular\}\s*$", "", body)
    body = re.sub(r"^\s*\\(toprule|hline)", "", body)
    rule = re.search(r"\\(midrule|hline)(?![A-Za-z])", body)
    if not rule:
        return 0
    return sum(1 for r in split_top_level(body[: rule.start()]) if r.strip())


def _split_tabular(src: str) -> tuple[str, str, str] | None:
    """``(environment, column spec, body)`` of a tabular-like environment."""
    m = re.match(r"\\begin\s*\{(tabular\*?|tabularx|tabulary)\}", src)
    if not m:
        return None
    name = m.group(1)
    i = m.end()
    if name in ("tabular*", "tabularx", "tabulary"):
        _, i = parse_command_args(src, i, "m")
    _, i = read_optional(src, i)
    args, i = parse_command_args(src, i, "m")
    end = re.search(r"\\end\s*\{" + re.escape(name) + r"\}\s*$", src)
    return name, args[0] or "", src[i : end.start()] if end else src[i:]


def sanitize_tabular(src: str) -> str:
    """Rewrite any tabular-like environment into a plain ``tabular`` pandoc can parse."""
    parts = _split_tabular(src)
    if parts is None:
        return src
    _, spec, body = parts
    return "\\begin{tabular}{" + clean_colspec(spec) + "}" + _clean_table_body(body) + "\\end{tabular}"


_PARTIAL_RULE_RE = re.compile(r"\\(?:cmidrule\s*(?:\([^)]*\))?\s*(?:\[[^\]]*\])?|cline)\s*\{\s*(\d+)\s*-\s*(\d+)\s*\}")
_RULE_RE = re.compile(r"\\(?:toprule|midrule|bottomrule|botrule|hline|specialrule\s*\{[^}]*\}\s*\{[^}]*\}\s*\{[^}]*\})(?:\s*\[[^\]]*\])?")


def partial_rules(body: str) -> list[tuple[int, int, int]]:
    """``(row, first column, last column)`` for each partial rule, 0-based row, 1-based columns.

    A rule written after row ``r``'s ``\\\\`` is drawn under row ``r``.
    """
    out: list[tuple[int, int, int]] = []
    row = -1
    for segment in split_top_level(body):
        if row >= 0:
            out += [(row, int(a), int(b)) for a, b in _PARTIAL_RULE_RE.findall(segment)]
        if _RULE_RE.sub("", _PARTIAL_RULE_RE.sub("", segment)).strip():
            row += 1
    return out


def _clean_table_body(body: str) -> str:
    body = re.sub(r"\\botrule(?![A-Za-z])", r"\\bottomrule", body)
    body = re.sub(r"\\(toprule|midrule|bottomrule)\s*\[[^\]]*\]", r"\\\1", body)
    body = re.sub(r"\\specialrule\s*\{[^}]*\}\s*\{[^}]*\}\s*\{[^}]*\}", r"\\midrule", body)
    # partial rules: the Word table gets borders under spanning header cells instead
    body = re.sub(r"\\cmidrule\s*(\([^)]*\))?\s*(\[[^\]]*\])?\s*\{[^}]*\}", "", body)
    body = re.sub(r"\\cline\s*\{[^}]*\}", "", body)
    # \footnotemark[1] / \tnote{a} inside cells become superscripts
    body = re.sub(r"\\footnotemark\s*\[([^\]]*)\]", r"\\textsuperscript{\1}", body)
    body = re.sub(r"\\tnote\s*\{([^}]*)\}", r"\\textsuperscript{\1}", body)
    return replace_commands(body, _TABLE_BODY_COMMANDS)


def _drop(spec: str):
    return spec, lambda a: ""


_TABLE_BODY_COMMANDS = {
    **{name: _drop(spec) for name, spec in (
        ("rowcolor", "om"), ("cellcolor", "om"), ("arrayrulecolor", "om"), ("addlinespace", "o"), ("noalign", "m"),
        ("rule", "omm"), ("morecmidrules", ""), ("centering", ""), ("raggedright", ""), ("raggedleft", ""))},
    # \multicolumn column specs must be sanitised too
    "multicolumn": ("mmm", lambda a: "\\multicolumn{%s}{%s}{%s}" % (a[0] or "1", clean_colspec(a[1] or "c"), a[2] or "")),
    # \makecell{a\\b} -> a b
    "makecell": ("om", lambda a: " ".join(split_top_level(a[1] or ""))),
}


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


def _const(text: str):
    return "", lambda a: text


_COMMENT = ("m", lambda a: "\u2003\u25b7 " + (a[0] or ""))
_ALG_INLINE = {
    **{name: _const(r"\textbf{%s}" % name.lower()) for name in ("Return", "RETURN", "And", "AND", "Or", "OR", "Not", "NOT")},
    **{name: _const(r"\textsc{%s}" % name.lower()) for name in ("True", "TRUE", "False", "FALSE")},
    "Call": ("mm", lambda a: "\\textsc{%s}(%s)" % (a[0] or "", a[1] or "")),
    "Comment": _COMMENT,
    "COMMENT": _COMMENT,
}


def _alg_inline(text: str) -> str:
    return replace_commands(text, _ALG_INLINE)


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
        joined = join_list(pieces)
        out.append(f"\\text{{{joined}}}" if math else joined)
        pos = end
    out.append(text[pos:])
    return "".join(out)
