"""Structural checks on a generated .docx (no Word needed).

Used by the test-suite and by ``sn2docx --check``. Each problem is returned as a
human-readable string; an empty list means the document passed.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .package import NS, q


@dataclass
class Summary:
    problems: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)  # field instructions in document order
    bookmarks: list[str] = field(default_factory=list)
    headings: list[tuple[str, str]] = field(default_factory=list)  # (style, text)
    captions: list[str] = field(default_factory=list)
    equations: int = 0
    images: int = 0
    tables: int = 0


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(q("w:t")))


def _field_instructions(root) -> list[str]:
    out = []
    buf = None
    for el in root.iter(q("w:fldChar"), q("w:instrText")):
        if el.tag == q("w:fldChar"):
            kind = el.get(q("w:fldCharType"))
            if kind == "begin":
                buf = ""
            elif kind in ("separate", "end") and buf is not None:
                out.append(re.sub(r"\s+", " ", buf).strip())
                buf = None
        elif buf is not None:
            buf += el.text or ""
    for fs in root.iter(q("w:fldSimple")):
        out.append(fs.get(q("w:instr"), "").strip())
    return out


def inspect(path: Path) -> Summary:
    s = Summary()
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for required in ("word/document.xml", "word/styles.xml", "word/numbering.xml", "[Content_Types].xml"):
            if required not in names:
                s.problems.append(f"missing part {required}")
        parts = {n: z.read(n) for n in names if n.endswith(".xml") or n.endswith(".rels")}
        media = {n for n in names if n.startswith("word/media/")}
    roots = {}
    for n, data in parts.items():
        try:
            roots[n] = etree.fromstring(data)
        except etree.XMLSyntaxError as exc:
            s.problems.append(f"{n}: invalid XML ({exc})")
    doc = roots.get("word/document.xml")
    if doc is None:
        return s

    # leftover markers / temporary elements
    for n in ("word/document.xml", "word/footnotes.xml"):
        root = roots.get(n)
        if root is None:
            continue
        for t in root.iter(q("w:t"), q("m:t")):
            if t.text and re.search(r"@@[A-Z]+[0-9:]*@@", t.text):
                s.problems.append(f"{n}: leftover marker in text {t.text!r}")
        if b"urn:sn2docx:marker" in parts[n]:
            s.problems.append(f"{n}: temporary marker namespace left in XML")

    # bookmarks: unique names and ids, starts matched by ends
    ids: dict[str, str] = {}
    all_names: set[str] = set()
    for n in ("word/document.xml", "word/footnotes.xml"):
        root = roots.get(n)
        if root is None:
            continue
        ends = {e.get(q("w:id")) for e in root.iter(q("w:bookmarkEnd"))}
        for b in root.iter(q("w:bookmarkStart")):
            name, i = b.get(q("w:name")), b.get(q("w:id"))
            if name in all_names:
                s.problems.append(f"duplicate bookmark name {name}")
            if i in ids:
                s.problems.append(f"duplicate bookmark id {i} ({ids[i]}, {name})")
            if i not in ends:
                s.problems.append(f"bookmark {name} has no end")
            ids[i] = name
            all_names.add(name)
            s.bookmarks.append(name)

    # fields and hyperlinks must point at existing bookmarks
    for n in ("word/document.xml", "word/footnotes.xml"):
        root = roots.get(n)
        if root is None:
            continue
        instrs = _field_instructions(root)
        if n == "word/document.xml":
            s.fields = instrs
        for ins in instrs:
            m = re.match(r"(REF|PAGEREF|NOTEREF)\s+(\S+)", ins)
            if m and m.group(2) not in all_names:
                s.problems.append(f"{m.group(1)} field points to missing bookmark {m.group(2)}")
        for h in root.iter(q("w:hyperlink")):
            a = h.get(q("w:anchor"))
            if a and a not in all_names:
                s.problems.append(f"hyperlink to missing bookmark {a}")

    # relationships: every r:embed / r:id used in the body exists
    rels = roots.get("word/_rels/document.xml.rels")
    rel_ids = {r.get("Id"): r for r in rels} if rels is not None else {}
    for el in doc.iter():
        for attr in ("{%s}embed" % NS["r"], "{%s}id" % NS["r"]):
            rid = el.get(attr)
            if rid and rid not in rel_ids:
                s.problems.append(f"dangling relationship {rid}")
    for rid, r in rel_ids.items():
        if r.get("TargetMode") != "External" and r.get("Type", "").endswith("/image"):
            if "word/" + r.get("Target") not in media:
                s.problems.append(f"image relationship {rid} targets missing part {r.get('Target')}")

    # numbering: every numId used exists
    num = roots.get("word/numbering.xml")
    num_ids = {n.get(q("w:numId")) for n in num.findall(q("w:num"))} if num is not None else set()
    styles = roots.get("word/styles.xml")
    for root in (doc, styles):
        for nid in root.iter(q("w:numId")):
            v = nid.get(q("w:val"))
            if v not in num_ids and v != "0":
                s.problems.append(f"numId {v} is not defined in numbering.xml")

    # summary
    body = doc.find(q("w:body"))
    for p in body.iter(q("w:p")):
        st = p.find(f"{q('w:pPr')}/{q('w:pStyle')}")
        style = st.get(q("w:val")) if st is not None else ""
        if style.startswith("Heading"):
            s.headings.append((style, _text(p).strip()))
        elif style in ("ImageCaption", "TableCaption"):
            s.captions.append(_text(p).strip())
    s.equations = sum(1 for _ in body.iter(q("m:oMath")))
    s.images = sum(1 for _ in body.iter("{%s}inline" % NS["wp"]))
    s.tables = sum(1 for _ in body.iter(q("w:tbl")))
    return s
