"""Extraction of the sn-jnl title block (title, authors, affiliations, abstract, keywords)."""

from __future__ import annotations

import re

from ..model import Affiliation, Author, FrontMatter
from .scan import find_env, replace_commands
from .source import MacroTable, collect_macros, expand_macros
from .texdefs import DocumentClass


def _expander(doc_class: DocumentClass | None):
    """Expand the class's own markup macros (sn-jnl: ``\\fnm``, ``\\sur``, ``\\orgdiv`` ...)."""
    table = collect_macros(doc_class.source)[1] if doc_class is not None else MacroTable()

    def expand(text: str) -> str:
        text = expand_macros(text, table)
        text = re.sub(r"\s*\\unskip(?![A-Za-z@])", "", text)  # TeX: remove the preceding space
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"(,\s*)+,", ",", text)  # separators left behind by empty fields
        return text.strip(" ,")

    return expand


def extract_frontmatter(preamble: str, body: str, doc_class: DocumentClass | None = None) -> tuple[FrontMatter, str, str]:
    """Pull title-block commands out of preamble and body.

    Returns the front matter and the preamble/body with those commands removed.
    ``\\email`` and ``\\equalcont`` belong to the most recent ``\\author``.
    """
    fm = FrontMatter()
    unwrap = _expander(doc_class)

    def title(a):
        fm.title, fm.short_title = (a[1] or "").strip(), (a[0] or "").strip()

    def author(a):
        keys = [k.strip() for k in (a[1] or "").split(",") if k.strip()]
        fm.authors.append(Author(unwrap(a[2] or ""), keys, corresponding=a[0] == "*"))

    def email(a):
        if fm.authors:
            fm.authors[-1].emails.append((a[0] or "").strip())

    def equalcont(a):
        if fm.authors:
            fm.authors[-1].equal = (a[0] or "").strip()

    def affil(a):
        key = (a[1] or str(len(fm.affiliations) + 1)).strip()
        fm.affiliations.append(Affiliation(key, unwrap(a[2] or "")))

    def abstract(a):
        fm.abstract = (a[0] or "").strip()

    def keywords(a):
        fm.keywords = (a[0] or "").strip()

    def date(a):
        fm.date = (a[0] or "").strip() or None

    def consume(fn):
        return lambda a: fn(a) or ""

    table = {
        "title": ("om", consume(title)), "author": ("som", consume(author)), "email": ("m", consume(email)),
        "equalcont": ("m", consume(equalcont)), "affil": ("som", consume(affil)),
        "abstract": ("m", consume(abstract)), "keywords": ("m", consume(keywords)), "date": ("m", consume(date)),
        "pacs": ("m", lambda a: ""), "thanks": ("m", lambda a: ""), "maketitle": ("", lambda a: ""),
    }
    preamble = replace_commands(preamble, table)
    body = replace_commands(body, table)

    env = find_env(body, "abstract")
    if env is not None and not fm.abstract:
        fm.abstract = env.body(body).strip()
        body = body[: env.start] + body[env.end :]

    # Number affiliations: numeric keys keep their value, others count up.
    for i, aff in enumerate(fm.affiliations, 1):
        aff.number = int(aff.key) if aff.key.isdigit() else i
    return fm, preamble, body
