"""Extraction of the sn-jnl title block (title, authors, affiliations, abstract, keywords)."""

from __future__ import annotations

import re

from ..model import Affiliation, Author, FrontMatter
from .scan import find_env, is_escaped, parse_command_args

# Wrappers used inside \author and \affil that simply print their argument.
_UNWRAP = (
    "fnm", "sur", "spfx", "pfx", "sfx", "tanm", "dgr", "orgdiv", "orgname",
    "orgaddress", "street", "city", "postcode", "state", "country", "address",
)

_FM_COMMANDS = {
    "title": "om",
    "author": "som",
    "email": "m",
    "equalcont": "m",
    "affil": "som",
    "abstract": "m",
    "keywords": "m",
    "pacs": "m",
    "date": "m",
    "maketitle": "",
    "thanks": "m",
}

_FM_RE = re.compile(r"\\(" + "|".join(_FM_COMMANDS) + r")(?![A-Za-z@])")


def unwrap(text: str) -> str:
    for name in _UNWRAP:
        text = re.sub(r"\\" + name + r"\s*\{", "{", text)
    text = re.sub(r"\s+", " ", text).strip()
    # tidy separators left behind by empty fields
    text = re.sub(r"(,\s*)+,", ",", text)
    return text.strip(" ,")


def extract_frontmatter(preamble: str, body: str) -> tuple[FrontMatter, str, str]:
    """Pull title-block commands out of preamble and body.

    Returns the front matter and the preamble/body with those commands removed.
    """
    fm = FrontMatter()
    affil_keys: dict[str, Affiliation] = {}
    current: Author | None = None
    corresponding_affils: list[str] = []

    def process(text: str) -> str:
        nonlocal current
        out: list[str] = []
        pos = 0
        while True:
            m = _FM_RE.search(text, pos)
            if not m:
                break
            if is_escaped(text, m.start()):
                out.append(text[pos : m.end()])
                pos = m.end()
                continue
            name = m.group(1)
            args, end = parse_command_args(text, m.end(), _FM_COMMANDS[name])
            out.append(text[pos : m.start()])
            pos = end
            if name == "title":
                fm.title = (args[1] or "").strip()
                fm.short_title = (args[0] or "").strip()
            elif name == "author":
                keys = [k.strip() for k in (args[1] or "").split(",") if k.strip()]
                current = Author(unwrap(args[2] or ""), keys, corresponding=args[0] == "*")
                fm.authors.append(current)
            elif name == "email":
                if current is not None:
                    current.emails.append((args[0] or "").strip())
            elif name == "equalcont":
                if current is not None:
                    current.equal = (args[0] or "").strip()
            elif name == "affil":
                key = (args[1] or str(len(fm.affiliations) + 1)).strip()
                aff = Affiliation(key, unwrap(args[2] or ""))
                affil_keys[key] = aff
                fm.affiliations.append(aff)
                if args[0] == "*":
                    corresponding_affils.append(key)
            elif name == "abstract":
                fm.abstract = (args[0] or "").strip()
            elif name == "keywords":
                fm.keywords = [k.strip() for k in re.split(r"[,;]", args[0] or "") if k.strip()]
            elif name == "date":
                fm.date = (args[0] or "").strip() or None
            elif name == "thanks":
                pass
        out.append(text[pos:])
        return "".join(out)

    preamble = process(preamble)
    body = process(body)

    env = find_env(body, "abstract")
    if env is not None and not fm.abstract:
        fm.abstract = env.body(body).strip()
        body = body[: env.start] + body[env.end :]

    # Number affiliations: numeric keys keep their value, others count up.
    used = set()
    for i, aff in enumerate(fm.affiliations, 1):
        aff.number = int(aff.key) if aff.key.isdigit() else i
        used.add(aff.number)
    return fm, preamble, body
