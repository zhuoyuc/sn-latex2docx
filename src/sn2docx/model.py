"""Data shared between the LaTeX preprocessing stage and the docx post-processor.

The preprocessor rewrites the manuscript into LaTeX that pandoc handles well,
leaving *markers* of the form ``@@NAME<args>@@`` wherever the Word output needs
structure pandoc cannot express (numbered equations, captions with live SEQ
fields, cross-reference fields, the author block ...). The marker arguments
are indices into the lists held by :class:`Registry`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MARKER_RE = re.compile(r"@@([A-Z]+)([0-9:]*)@@")


def marker(name: str, *args: int) -> str:
    return "@@" + name + ":".join(str(a) for a in args) + "@@"


@dataclass
class Author:
    name: str  # LaTeX
    affils: list[str]
    corresponding: bool = False
    emails: list[str] = field(default_factory=list)
    equal: str | None = None  # text of \equalcont


@dataclass
class Affiliation:
    key: str
    text: str  # LaTeX
    number: int = 0


@dataclass
class FrontMatter:
    title: str = ""
    short_title: str = ""
    authors: list[Author] = field(default_factory=list)
    affiliations: list[Affiliation] = field(default_factory=list)
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    date: str | None = None


@dataclass
class Label:
    name: str  # LaTeX label
    kind: str  # section, appendix, equation, figure, subfigure, table, algorithm, line, theorem, other
    text: str  # number as printed by \ref
    bookmark: str = ""
    field: bool = False  # True when the target carries a SEQ field / numbered heading


@dataclass
class Heading:
    level: int
    numbered: bool
    appendix: bool
    number: str
    bookmark: str | None = None


@dataclass
class Equation:
    number: str | None  # None = unnumbered
    tag: bool = False  # number comes from \tag (static text)
    bookmark: str | None = None


@dataclass
class Panel:
    images: list["Image"]
    caption: bool = False  # has a sub-caption paragraph
    letter: str = ""
    bookmark: str | None = None


@dataclass
class Image:
    source: str  # as written in \includegraphics
    options: str | None = None


@dataclass
class Figure:
    number: str | None
    panels: list[Panel]
    has_caption: bool
    bookmark: str | None = None


@dataclass
class Table:
    number: str | None
    has_caption: bool
    notes: int = 0
    bookmark: str | None = None
    panels: list[Panel] = field(default_factory=list)  # images standing in for a tabular


@dataclass
class Algorithm:
    number: str | None
    has_caption: bool
    bookmark: str | None = None


@dataclass
class TheoremSpec:
    """A ``\\newtheorem`` declaration."""

    title: str  # e.g. "Theorem"
    counter: str | None  # counter name (shared counters point to the same name); None = unnumbered
    style: str = "plain"  # plain | definition | remark | roman-head (sn-jnl thmstyletwo)
    within: int = 0  # 1 = numbered within section ("2.1"), 2 = within subsection, 0 = global


@dataclass
class Theorem:
    """One theorem-like environment instance; the head is written by the post-processor."""

    head: str  # "Theorem 2.1"
    style: str
    has_note: bool = False  # optional argument, e.g. [Pythagoras]


@dataclass
class Registry:
    headings: list[Heading] = field(default_factory=list)
    equations: list[Equation] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    algorithms: list[Algorithm] = field(default_factory=list)
    theorems: list[Theorem] = field(default_factory=list)
    labels: dict[str, Label] = field(default_factory=dict)
    # \ref-style references: index -> (label name, variant)
    refs: list[tuple[str, str]] = field(default_factory=list)
    # point anchors (theorems, algorithm lines, ...): index -> bookmark
    anchors: list[str] = field(default_factory=list)
    # manual bibliography (thebibliography): citation index -> (command, keys, prenote, postnote)
    cites: list[tuple[str, list[str], str | None, str | None]] = field(default_factory=list)
    bibitems: list[str] = field(default_factory=list)  # keys in order
    bib_labels: dict[str, str] = field(default_factory=dict)  # key -> \bibitem optional label
    bookmarks: set[str] = field(default_factory=set)  # lower-cased names in use

    def bookmark_for(self, label: str) -> str:
        """Word-safe, unique bookmark name derived from a LaTeX label."""
        base = re.sub(r"[^A-Za-z0-9_]", "_", label)
        if not base or not base[0].isalpha():
            base = "x" + base
        base = base[:36]
        name = base
        k = 2
        # Word compares bookmark names case-insensitively
        while name.lower() in self.bookmarks:
            name = f"{base[:33]}_{k}"
            k += 1
        self.bookmarks.add(name.lower())
        return name

    def add_ref(self, label: str, variant: str) -> int:
        self.refs.append((label, variant))
        return len(self.refs) - 1

    def add_anchor(self, bookmark: str) -> int:
        self.anchors.append(bookmark)
        return len(self.anchors) - 1


@dataclass
class Conversion:
    """Everything the docx stage needs besides the pandoc output."""

    front: FrontMatter
    registry: Registry
    source_dir: Path
    graphics_paths: list[Path]
    bibliography: list[Path]
    citation_mode: str  # "numeric" | "author-year"
    manual_bibliography: bool = False
    equal_notes: list[str] = field(default_factory=list)  # distinct \equalcont texts
