"""Extraction of the sn-jnl title block (title, authors, affiliations, abstract, keywords)."""

from __future__ import annotations

import re

from ..model import Affiliation, Author, FrontMatter
from .scan import find_env, replace_commands

# Wrappers used inside \author and \affil that simply print their argument.
_UNWRAP = (
    "fnm", "sur", "spfx", "pfx", "sfx", "tanm", "dgr", "orgdiv", "orgname",
    "orgaddress", "street", "city", "postcode", "state", "country", "address",
)
_UNWRAP_RE = re.compile(r"\\(?:" + "|".join(_UNWRAP) + r")\s*\{")


def unwrap(text: str) -> str:
    text = _UNWRAP_RE.sub("{", text)
    text = re.sub(r"\s+", " ", text).strip()
    # tidy separators left behind by empty fields
    text = re.sub(r"(,\s*)+,", ",", text)
    return text.strip(" ,")


def extract_frontmatter(preamble: str, body: str) -> tuple[FrontMatter, str, str]:
    """Pull title-block commands out of preamble and body.

    Returns the front matter and the preamble/body with those commands removed.
    ``\\email`` and ``\\equalcont`` belong to the most recent ``\\author``.
    """
    fm = FrontMatter()

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
        fm.keywords = [k.strip() for k in re.split(r"[,;]", a[0] or "") if k.strip()]

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
