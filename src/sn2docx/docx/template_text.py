"""Everything the output copies from the Word template, read from ``template.docx``.

The template is a converted sample manuscript, so its paragraphs are exemplars:
how the house style spells caption labels ("Figure 1: ..."), the abstract and
keyword headings and the corresponding-author line, and which paragraph, run and
table properties each construct carries (equation tab stops, image width,
compact table cells, bibliography indents ...). The post-processor clones these
exemplars instead of writing properties of its own.
"""

from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field

from docx.shared import Emu, Length, Pt, Twips
from lxml import etree
from PIL import Image

from . import ooxml as ox
from .ooxml import q
from .package import Package

WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"


@dataclass
class TemplateText:
    captions: dict[str, tuple[str, str]] = field(default_factory=dict)  # SEQ id -> (label before number, separator)
    equation: tuple[str, str] = ("", "")  # text around an equation number
    abstract: str = ""
    keywords: str = ""
    corresponding: str = ""  # label before the corresponding author's e-mail
    corresponding_mark: str = ""  # superscript tying the author to that line
    author_sep: str = ""  # between two authors
    line_lead: str = ""  # text opening each further line of the author block
    label_sep: str = ""  # between a label ("Corresponding author:", "Keywords:") and what follows
    heading_lead: str = ""  # text between a heading's number and its title
    references: str = ""
    bullets: list[tuple[str, dict[str, str]]] = field(default_factory=list)  # per level: (glyph, rFonts attributes)
    # exemplar properties, cloned by the post-processor
    pprs: dict[str, etree._Element] = field(default_factory=dict)  # role -> w:pPr
    rprs: dict[str, etree._Element | None] = field(default_factory=dict)  # role -> w:rPr
    tbl_pr: etree._Element | None = None
    header_tr_pr: etree._Element | None = None
    body_tr_pr: etree._Element | None = None
    drawing: etree._Element | None = None  # an inline picture run
    image_width: Length | None = None  # the width every figure has
    image_dpi: float | None = None  # resolution of the template's own images
    text_width: Length | None = None
    text_height: Length | None = None
    body_size: Length | None = None  # body font size (Normal/BodyText)
    rule_size: int | None = None  # w:sz of the table's top rule (eighths of a point)
    language: str | None = None
    header_date: str | None = None  # the sample date in the page header
    list_ind: dict[str, etree._Element] = field(default_factory=dict)  # numFmt -> w:ind of its first level

    def ppr(self, role: str, fallback: str | None = None) -> etree._Element | None:
        """A copy of the exemplar paragraph properties for ``role``."""
        found = self.pprs.get(role) if role in self.pprs else self.pprs.get(fallback or "")
        return copy.deepcopy(found) if found is not None else None

    def rpr(self, role: str) -> etree._Element | None:
        found = self.rprs.get(role)
        return copy.deepcopy(found) if found is not None else None


def _pieces(p: etree._Element) -> list[tuple[str, str, etree._Element | None]]:
    """Paragraph content in order: ("text", t, run), ("field", instruction, run), ("br"/"tab", "", None)."""
    out: list[tuple[str, str, etree._Element | None]] = []
    in_result = False
    for e in p.iter(q("w:t"), q("w:instrText"), q("w:fldChar"), q("w:fldSimple"), q("w:br"), q("w:tab")):
        tag = etree.QName(e).localname
        if tag == "fldChar":
            kind = e.get(q("w:fldCharType"))
            in_result = kind == "separate"
        elif tag == "instrText":
            out.append(("field", (e.text or "").strip(), e.getparent()))
        elif tag == "fldSimple":
            out.append(("field", (e.get(q("w:instr")) or "").strip(), None))
        elif tag in ("br", "tab"):
            if e.getparent().tag == q("w:r"):
                out.append((tag, "", None))
        elif not in_result and not any(a.tag == q("w:fldSimple") for a in e.iterancestors()):
            out.append(("text", e.text or "", e.getparent()))
    return out


def _rpr(run: etree._Element | None) -> etree._Element | None:
    return run.find(q("w:rPr")) if run is not None else None


def _superscript(run: etree._Element | None) -> bool:
    va = run.find(f"{q('w:rPr')}/{q('w:vertAlign')}") if run is not None else None
    return va is not None and va.get(q("w:val")) == "superscript"


def _unnumbered(p: etree._Element) -> bool:
    num = p.find(f"{q('w:pPr')}/{q('w:numPr')}/{q('w:numId')}")
    return num is not None and num.get(q("w:val")) == "0"


def _keep(tt: TemplateText, role: str, p: etree._Element) -> None:
    if role not in tt.pprs:
        ppr = p.find(q("w:pPr"))
        tt.pprs[role] = copy.deepcopy(ppr) if ppr is not None else ox.el("w:pPr")


def read_template_text(template: Package) -> TemplateText:
    tt = TemplateText()
    doc = template.xml("word/document.xml")
    body = doc.find(q("w:body"))
    paragraphs = list(body.iter(q("w:p")))
    for i, p in enumerate(paragraphs):
        style = ox.para_style(p) or ""
        pieces = _pieces(p)
        text = "".join(v for k, v, _ in pieces if k == "text")
        in_table = any(a.tag == q("w:tbl") for a in p.iterancestors())
        has_math = p.find(q("m:oMath")) is not None or p.find(q("m:oMathPara")) is not None
        drawing = p.find(f".//{q('w:drawing')}")
        for j, (kind, value, run) in enumerate(pieces):
            if kind != "field":
                continue
            m = re.match(r"(SEQ|REF)\s+(\S+)", value)
            if not m:
                continue
            if m.group(1) == "REF":
                tt.rprs.setdefault("ref", copy.deepcopy(_rpr(run)))
                continue
            if m.group(2) != "equation":
                tt.rprs.setdefault("caption_label", copy.deepcopy(_rpr(run)))
            before = "".join(v for k, v, _ in pieces[:j] if k == "text")
            after = "".join(v for k, v, _ in pieces[j + 1 :] if k == "text")
            if m.group(2) == "equation":
                tt.equation = (before[-1:] if before and not before[-1].isalnum() else "",
                               re.match(r"\W*", after).group(0))
                _keep(tt, "equation_numbered", p)
            elif m.group(2) not in tt.captions:
                tt.captions[m.group(2)] = (before, re.match(r"\W*", after).group(0))
                _keep(tt, f"{m.group(2)}_caption", p)
        if has_math and not in_table and "equation_numbered" in tt.pprs and not any(k == "field" for k, _, _ in pieces):
            _keep(tt, "equation_plain", p)
        for h in p.iter(q("w:hyperlink")):
            if h.get(q("w:anchor"), "").startswith("bibref"):
                tt.rprs.setdefault("cite", copy.deepcopy(_rpr(h.find(q("w:r")))))
        if in_table:
            _keep(tt, "cell", p)
        elif drawing is not None:
            if tt.drawing is None:
                tt.drawing = copy.deepcopy(drawing.getparent())
            _keep(tt, f"image_{style}", p)
        elif style == "Compact" and text.strip():
            _keep(tt, "subcaption", p)
        elif style in ("Title", "Author", "AbstractTitle", "Abstract"):
            _keep(tt, style, p)
        if style == "AbstractTitle" and not tt.abstract:
            tt.abstract = text.strip()
            # the paragraph after the abstract text opens with the keywords label
            k = i + 1
            while k < len(paragraphs) and ox.para_style(paragraphs[k]) == "Abstract":
                k += 1
            if k < len(paragraphs):
                first = next(((v, r) for kd, v, r in _pieces(paragraphs[k]) if kd == "text" and v.strip()), None)
                if first is not None:
                    kw = _pieces(paragraphs[k])
                    idx = next(n for n, (kd, v, r) in enumerate(kw) if kd == "text" and v.strip())
                    tt.label_sep = tt.label_sep or _gap(kw[idx + 1 :])
                    tt.keywords = first[0].strip()
                    tt.rprs["keywords_label"] = copy.deepcopy(_rpr(first[1]))
                    _keep(tt, "keywords", paragraphs[k])
        if style == "Author":
            _author_block(tt, pieces)
        if style.startswith("Heading") and not _unnumbered(p) and not tt.heading_lead:
            tt.heading_lead = text[: len(text) - len(text.lstrip())]
        if style.startswith("Heading") and _unnumbered(p):
            tt.references = text.strip()
            # the reference list follows its heading
            for k, entry in enumerate(paragraphs[i + 1 : i + 3]):
                _keep(tt, "bib_first" if k == 0 else "bib", entry)
        if style == "BodyText" and not has_math and drawing is None and not in_table and text.strip():
            _keep(tt, "body", p)
    _geometry(tt, template, body)
    tt.bullets, tt.list_ind = _lists(template)
    return tt


def _author_block(tt: TemplateText, pieces) -> None:
    texts = [(v, r) for k, v, r in pieces if k in ("text", "br")]
    # between the first author's marks and the next name: the author separator
    for n, (v, r) in enumerate(texts):
        if _superscript(r):
            tt.rprs.setdefault("sup", copy.deepcopy(_rpr(r)))
            sep = ""
            for v2, r2 in texts[n + 1 :]:
                if _superscript(r2) or re.search(r"\w", v2):
                    break
                sep += v2
            tt.author_sep = sep
            break
    for n, (kind, _, _) in enumerate(pieces):
        if kind == "br":
            tt.line_lead = _gap(pieces[n + 1 :])
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
            if len(line) >= 3:
                tt.rprs["email"] = copy.deepcopy(_rpr(line[2][1]))


def _geometry(tt: TemplateText, template: Package, body: etree._Element) -> None:
    sect = body.find(q("w:sectPr"))
    sz, mar = sect.find(q("w:pgSz")), sect.find(q("w:pgMar"))
    tt.text_width = Twips(int(sz.get(q("w:w"))) - int(mar.get(q("w:left"))) - int(mar.get(q("w:right"))))
    tt.text_height = Twips(int(sz.get(q("w:h"))) - int(mar.get(q("w:top"))) - int(mar.get(q("w:bottom"))))
    extent = body.find(f".//{WP}extent")
    if extent is not None:
        tt.image_width = Emu(int(extent.get("cx")))
    # the resolution the template's pictures were rendered at (recorded in the image files)
    dpis = []
    for name, data in template.parts.items():
        if name.startswith("word/media/"):
            dpi = Image.open(io.BytesIO(data)).info.get("dpi")
            if dpi:
                dpis.append(round(float(dpi[0])))
    tt.image_dpi = max(dpis) if dpis else None
    table = body.find(f".//{q('w:tbl')}")
    if table is not None:
        tt.tbl_pr = copy.deepcopy(table.find(q("w:tblPr")))
        rows = table.findall(q("w:tr"))
        head = [r for r in rows if r.find(f"{q('w:trPr')}/{q('w:tblHeader')}") is not None]
        rest = [r for r in rows if r not in head]
        tt.header_tr_pr = copy.deepcopy(head[0].find(q("w:trPr"))) if head else None
        tt.body_tr_pr = copy.deepcopy(rest[0].find(q("w:trPr"))) if rest else None
        top = tt.tbl_pr.find(f"{q('w:tblBorders')}/{q('w:top')}")
        tt.rule_size = int(top.get(q("w:sz"))) if top is not None and top.get(q("w:sz")) else None
    styles = template.xml("word/styles.xml")
    lang = styles.find(f"{q('w:docDefaults')}//{q('w:lang')}")
    tt.language = lang.get(q("w:val")) if lang is not None else None
    by_id = {st.get(q("w:styleId")): st for st in styles.findall(q("w:style"))}
    for sid in ("BodyText", "Normal"):
        size = by_id[sid].find(f"{q('w:rPr')}/{q('w:sz')}") if sid in by_id else None
        if size is not None:
            tt.body_size = Pt(int(size.get(q("w:val"))) / 2)  # w:sz is in half-points
            break
    for name in template.parts:
        if re.fullmatch(r"word/header\d*\.xml", name):
            tt.header_date = "".join(t.text or "" for t in template.xml(name).iter(q("w:t")))


def _lists(template: Package) -> tuple[list[tuple[str, dict[str, str]]], dict[str, etree._Element]]:
    num = template.xml("word/numbering.xml")
    bullets: list[tuple[str, dict[str, str]]] = []
    indents: dict[str, etree._Element] = {}
    for a in num.findall(q("w:abstractNum")):
        levels = []
        for lvl in a.findall(q("w:lvl")):
            fmt = lvl.find(q("w:numFmt"))
            text = lvl.find(q("w:lvlText"))
            ind = lvl.find(f"{q('w:pPr')}/{q('w:ind')}")
            if fmt is not None and lvl.get(q("w:ilvl")) == "0" and ind is not None and lvl.find(q("w:pStyle")) is None:
                indents.setdefault(fmt.get(q("w:val")), copy.deepcopy(ind))
            if fmt is None or fmt.get(q("w:val")) != "bullet" or text is None:
                break
            fonts = lvl.find(f"{q('w:rPr')}/{q('w:rFonts')}")
            levels.append((text.get(q("w:val")), dict(fonts.attrib) if fonts is not None else {}))
        if levels and not bullets:
            bullets = levels
    return bullets, indents


def _gap(pieces) -> str:
    """The whitespace-only text at the start of ``pieces``."""
    out = ""
    for kind, v, _ in pieces:
        if kind != "text" or v.strip():
            break
        out += v
    return out
