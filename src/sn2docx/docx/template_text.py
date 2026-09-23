"""Words and punctuation the Word template itself uses, read from ``template.docx``.

The template is a converted sample manuscript, so its paragraphs show how the
house style spells caption labels ("Figure 1: ..."), equation numbers, the
abstract and keyword headings, the corresponding-author line and the
references heading. Reading them here keeps such text out of the code; what the
template does not show falls back to the LaTeX class and package names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from . import ooxml as ox
from .ooxml import q
from .package import Package


@dataclass
class TemplateText:
    captions: dict[str, tuple[str, str]] = field(default_factory=dict)  # SEQ id -> (label before number, separator)
    equation: tuple[str, str] = ("", "")  # text around an equation number
    abstract: str = ""
    keywords: str = ""
    keywords_bold: bool = False
    corresponding: str = ""  # label before the corresponding author's e-mail
    corresponding_mark: str = ""  # superscript tying the author to that line
    author_sep: str = ""  # between two authors
    references: str = ""
    bullets: list[tuple[str, dict[str, str]]] = field(default_factory=list)  # per level: (glyph, rFonts attributes)


def _pieces(p: etree._Element) -> list[tuple[str, str, etree._Element | None]]:
    """Paragraph content in order: ("text", t, run), ("field", instruction, None), ("br", "", None)."""
    out: list[tuple[str, str, etree._Element | None]] = []
    in_result = False
    for e in p.iter(q("w:t"), q("w:instrText"), q("w:fldChar"), q("w:fldSimple"), q("w:br"), q("w:tab")):
        tag = etree.QName(e).localname
        if tag == "fldChar":
            kind = e.get(q("w:fldCharType"))
            in_result = kind == "separate"
            if kind == "end":
                in_result = False
        elif tag == "instrText":
            out.append(("field", (e.text or "").strip(), None))
        elif tag == "fldSimple":
            out.append(("field", (e.get(q("w:instr")) or "").strip(), None))
        elif tag == "br":
            out.append(("br", "", None))
        elif tag == "tab":
            out.append(("tab", "", None))
        elif not in_result and not _in_fld_simple(e):
            out.append(("text", e.text or "", e.getparent()))
    return out


def _in_fld_simple(e: etree._Element) -> bool:
    return any(a.tag == q("w:fldSimple") for a in e.iterancestors())


def _superscript(run: etree._Element | None) -> bool:
    va = run.find(f"{q('w:rPr')}/{q('w:vertAlign')}") if run is not None else None
    return va is not None and va.get(q("w:val")) == "superscript"


def _bold(run: etree._Element | None) -> bool:
    b = run.find(f"{q('w:rPr')}/{q('w:b')}") if run is not None else None
    return b is not None and b.get(q("w:val"), "true") not in ("0", "false")


def _unnumbered(p: etree._Element) -> bool:
    num = p.find(f"{q('w:pPr')}/{q('w:numPr')}/{q('w:numId')}")
    return num is not None and num.get(q("w:val")) == "0"


def read_template_text(template: Package) -> TemplateText:
    tt = TemplateText()
    doc = template.xml("word/document.xml")
    paragraphs = list(doc.iter(q("w:p")))
    for i, p in enumerate(paragraphs):
        style = ox.para_style(p) or ""
        pieces = _pieces(p)
        for j, (kind, value, _) in enumerate(pieces):
            if kind != "field":
                continue
            m = re.match(r"SEQ\s+(\S+)", value)
            if not m:
                continue
            before = "".join(v for k, v, _ in pieces[:j] if k == "text")
            after = "".join(v for k, v, _ in pieces[j + 1 :] if k == "text")
            if m.group(1) == "equation":
                tt.equation = (before[-1:] if before and not before[-1].isalnum() else "",
                               re.match(r"\W*", after).group(0))
            elif m.group(1) not in tt.captions:
                tt.captions[m.group(1)] = (before, re.match(r"\W*", after).group(0))
        if style == "AbstractTitle" and not tt.abstract:
            tt.abstract = "".join(v for k, v, _ in pieces if k == "text").strip()
            # the paragraph after the abstract text opens with the keywords label
            k = i + 1
            while k < len(paragraphs) and ox.para_style(paragraphs[k]) == "Abstract":
                k += 1
            if k < len(paragraphs):
                first = next(((v, r) for kd, v, r in _pieces(paragraphs[k]) if kd == "text" and v.strip()), None)
                if first is not None:
                    tt.keywords, tt.keywords_bold = first[0].strip(), _bold(first[1])
        if style == "Author":
            _author_block(tt, pieces)
        if style.startswith("Heading") and _unnumbered(p):
            tt.references = "".join(v for k, v, _ in pieces if k == "text").strip()
    tt.bullets = _bullets(template)
    return tt


def _author_block(tt: TemplateText, pieces) -> None:
    texts = [(v, r) for k, v, r in pieces if k in ("text", "br")]
    # between the first author's marks and the next name: the author separator
    for n, (v, r) in enumerate(texts):
        if _superscript(r):
            sep = ""
            for v2, r2 in texts[n + 1 :]:
                if _superscript(r2) or re.search(r"\w", v2):
                    break
                sep += v2
            tt.author_sep = sep
            break
    # the last line: a superscript mark that is not an affiliation number, then the label
    lines: list[list[tuple[str, etree._Element | None]]] = [[]]
    for kind, v, r in pieces:
        if kind == "br":
            lines.append([])
        elif kind == "text" and v.strip():
            lines[-1].append((v, r))
    for line in lines:
        if len(line) >= 2 and _superscript(line[0][1]) and not line[0][0].strip().isdigit():
            tt.corresponding_mark = line[0][0].strip()
            tt.corresponding = line[1][0].strip()


def _bullets(template: Package) -> list[tuple[str, dict[str, str]]]:
    num = template.xml("word/numbering.xml")
    for a in num.findall(q("w:abstractNum")):
        levels = []
        for lvl in a.findall(q("w:lvl")):
            fmt = lvl.find(q("w:numFmt"))
            text = lvl.find(q("w:lvlText"))
            if fmt is None or fmt.get(q("w:val")) != "bullet" or text is None:
                break
            fonts = lvl.find(f"{q('w:rPr')}/{q('w:rFonts')}")
            levels.append((text.get(q("w:val")), dict(fonts.attrib) if fonts is not None else {}))
        if levels:
            return levels
    return []
