"""Small builders for WordprocessingML elements used by the post-processor."""

from __future__ import annotations

import copy
import re

from lxml import etree

from .package import NS, q

W = NS["w"]
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Temporary marker elements live in their own namespace and are removed before saving.
MK_NS = "urn:sn2docx:marker"
MK = "{%s}mk" % MK_NS


def el(tag: str, attrib: dict[str, str] | None = None, *children) -> etree._Element:
    e = etree.Element(q(tag))
    for k, v in (attrib or {}).items():
        e.set(q(k) if ":" in k else k, v)
    for c in children:
        if c is not None:
            e.append(c)
    return e


def rpr(style: str | None = None, bold=False, italic=False, sup=False, size: int | None = None,
        color: str | None = None, base: etree._Element | None = None) -> etree._Element | None:
    r = copy.deepcopy(base) if base is not None else el("w:rPr")
    if style:
        for old in r.findall(q("w:rStyle")):
            r.remove(old)
        r.insert(0, el("w:rStyle", {"w:val": style}))
    if bold:
        r.append(el("w:b"))
        r.append(el("w:bCs"))
    if italic:
        r.append(el("w:i"))
        r.append(el("w:iCs"))
    if color:
        r.append(el("w:color", {"w:val": color}))
    if size:
        r.append(el("w:sz", {"w:val": str(size)}))
        r.append(el("w:szCs", {"w:val": str(size)}))
    if sup:
        r.append(el("w:vertAlign", {"w:val": "superscript"}))
    _sort_children(r, _RPR_ORDER)
    return r if len(r) else None


# Schema order of the rPr children we emit (subset of CT_RPr).
_RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "noProof",
              "vanish", "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
              "vertAlign", "lang"]


def _sort_children(parent: etree._Element, order: list[str]) -> None:
    """Reorder children into schema order (unknown elements go last, stable)."""
    rank = {name: i for i, name in enumerate(order)}
    parent[:] = sorted(parent, key=lambda c: rank.get(etree.QName(c).localname, len(order)))


def run(text: str = "", props: etree._Element | None = None, **kw) -> etree._Element:
    r = el("w:r")
    p = props if props is not None else rpr(**kw) if kw else None
    if p is not None:
        r.append(copy.deepcopy(p))
    if text:
        t = el("w:t")
        t.text = text
        t.set(XML_SPACE, "preserve")
        r.append(t)
    return r


def special_run(tag: str | etree._Element, props: etree._Element | None = None) -> etree._Element:
    """A run holding one non-text child (``w:tab``, ``w:br``, a ``w:fldChar`` …)."""
    r = el("w:r")
    if props is not None:
        r.append(copy.deepcopy(props))
    r.append(el(tag) if isinstance(tag, str) else tag)
    return r


def field(instr: str, result: str, props: etree._Element | None = None) -> list[etree._Element]:
    """Complex field runs: begin, instruction, separate, cached result, end."""
    def fld(kind: str) -> etree._Element:
        return special_run(el("w:fldChar", {"w:fldCharType": kind}), props)

    it = el("w:instrText")
    it.text = f" {instr} "
    it.set(XML_SPACE, "preserve")
    return [fld("begin"), special_run(it, props), fld("separate"), run(result, props), fld("end")]


class Bookmarks:
    """Allocates unique bookmark ids."""

    def __init__(self, start: int = 1):
        self.next = start

    def pair(self, name: str) -> tuple[etree._Element, etree._Element]:
        i = str(self.next)
        self.next += 1
        return el("w:bookmarkStart", {"w:id": i, "w:name": name}), el("w:bookmarkEnd", {"w:id": i})


def num_pr(num_id: int, ilvl: int) -> etree._Element:
    """``w:numPr``; ``num_id`` 0 switches numbering off."""
    return el("w:numPr", None, el("w:ilvl", {"w:val": str(ilvl)}), el("w:numId", {"w:val": str(num_id)}))


def ppr(style: str | None = None, keep_next=False, jc: str | None = None, spacing: dict | None = None,
        ind: dict | None = None, tabs: list[tuple[str, int]] | None = None, num: tuple[int, int] | None = None,
        borders: dict | None = None) -> etree._Element:
    """Paragraph properties in schema order."""
    p = el("w:pPr")
    if style:
        p.append(el("w:pStyle", {"w:val": style}))
    if keep_next:
        p.append(el("w:keepNext"))
    if num is not None:
        p.append(num_pr(*num))
    if borders:
        b = el("w:pBdr")
        for side in ("top", "left", "bottom", "right"):
            if side in borders:
                b.append(el(f"w:{side}", {"w:val": "single", "w:sz": str(borders[side]), "w:space": "1", "w:color": "000000"}))
        p.append(b)
    if tabs:
        t = el("w:tabs")
        for kind, pos in tabs:
            t.append(el("w:tab", {"w:val": kind, "w:pos": str(pos)}))
        p.append(t)
    if spacing:
        p.append(el("w:spacing", {f"w:{k}": str(v) for k, v in spacing.items()}))
    if ind:
        p.append(el("w:ind", {f"w:{k}": str(v) for k, v in ind.items()}))
    if jc:
        p.append(el("w:jc", {"w:val": jc}))
    return p


def paragraph(props: etree._Element | None, content: list[etree._Element]) -> etree._Element:
    p = el("w:p")
    if props is not None:
        p.append(props)
    for c in content:
        p.append(c)
    return p


_PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
              "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
              "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
              "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
              "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
              "sectPr", "pPrChange"]


def set_ppr(p: etree._Element, **changes) -> etree._Element:
    """Set/replace children of a paragraph's pPr, keeping schema order.

    ``changes`` maps local names to elements (or None to delete).
    """
    pp = p.find(q("w:pPr"))
    if pp is None:
        pp = el("w:pPr")
        p.insert(0, pp)
    for local, new in changes.items():
        for old in pp.findall(q(f"w:{local}")):
            pp.remove(old)
        if new is not None:
            pp.append(new)
    _sort_children(pp, _PPR_ORDER)
    return pp


def para_style(p: etree._Element) -> str | None:
    s = p.find(q("w:pPr") + "/" + q("w:pStyle"))
    return s.get(q("w:val")) if s is not None else None


def content_children(p: etree._Element) -> list[etree._Element]:
    """Children of a paragraph other than its pPr."""
    return [c for c in p if c.tag != q("w:pPr")]


def strip_leading_space(items: list[etree._Element]) -> list[etree._Element]:
    """Drop leading whitespace-only runs and left-strip the first text run.

    Bookmarks and inline markers are zero-width, so they are kept but looked past.
    """
    out = list(items)
    i = 0
    while i < len(out):
        first = out[i]
        if first.tag in (q("w:bookmarkStart"), q("w:bookmarkEnd"), MK):
            i += 1
            continue
        if first.tag == q("w:r"):
            t = first.find(q("w:t"))
            other = [c for c in first if c.tag not in (q("w:rPr"), q("w:t"))]
            if t is not None and not other:
                stripped = (t.text or "").lstrip()
                if not stripped:
                    out.pop(i)
                    continue
                t.text = stripped
        break
    return out


def plain_text(items) -> str:
    parts = []
    for it in items:
        for t in it.iter(q("w:t"), q("m:t")):
            parts.append(t.text or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def drawing(rid: str, cx: int, cy: int, doc_id: int, name: str, descr: str) -> etree._Element:
    """Inline picture, mirroring the structure used by the Word template."""
    wp = NS["wp"]
    a = NS["a"]
    pic = NS["pic"]
    r_ns = NS["r"]
    d = el("w:drawing")
    inline = etree.SubElement(d, "{%s}inline" % wp)
    etree.SubElement(inline, "{%s}extent" % wp, cx=str(cx), cy=str(cy))
    etree.SubElement(inline, "{%s}effectExtent" % wp, l="0", t="0", r="0", b="0")
    etree.SubElement(inline, "{%s}docPr" % wp, id=str(doc_id), name=f"Picture {doc_id}", descr=descr)
    gfp = etree.SubElement(inline, "{%s}cNvGraphicFramePr" % wp)
    etree.SubElement(gfp, "{%s}graphicFrameLocks" % a, noChangeAspect="1")
    graphic = etree.SubElement(inline, "{%s}graphic" % a)
    gd = etree.SubElement(graphic, "{%s}graphicData" % a, uri="http://schemas.openxmlformats.org/drawingml/2006/picture")
    p = etree.SubElement(gd, "{%s}pic" % pic)
    nv = etree.SubElement(p, "{%s}nvPicPr" % pic)
    etree.SubElement(nv, "{%s}cNvPr" % pic, id=str(doc_id), name=name, descr=descr)
    cnv = etree.SubElement(nv, "{%s}cNvPicPr" % pic)
    etree.SubElement(cnv, "{%s}picLocks" % a, noChangeAspect="1", noChangeArrowheads="1")
    bf = etree.SubElement(p, "{%s}blipFill" % pic)
    blip = etree.SubElement(bf, "{%s}blip" % a)
    blip.set("{%s}embed" % r_ns, rid)
    st = etree.SubElement(bf, "{%s}stretch" % a)
    etree.SubElement(st, "{%s}fillRect" % a)
    sp = etree.SubElement(p, "{%s}spPr" % pic, bwMode="auto")
    xf = etree.SubElement(sp, "{%s}xfrm" % a)
    etree.SubElement(xf, "{%s}off" % a, x="0", y="0")
    etree.SubElement(xf, "{%s}ext" % a, cx=str(cx), cy=str(cy))
    geom = etree.SubElement(sp, "{%s}prstGeom" % a, prst="rect")
    etree.SubElement(geom, "{%s}avLst" % a)
    etree.SubElement(sp, "{%s}noFill" % a)
    r = el("w:r")
    r.append(d)
    return r
