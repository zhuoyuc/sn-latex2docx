"""Turn pandoc's docx plus the preprocessor's markers into the house Word layout.

Each ``_pass_*`` method handles one kind of marker. The conventions (styles,
field codes, bookmark placement, numbering) follow ``resources/template.docx``.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .. import images
from ..latex.scan import latex_to_plain
from ..model import MARKER_RE, Conversion, Label
from . import ooxml as ox
from .ooxml import MK, MK_NS, el, q
from .package import NS, Package

log = logging.getLogger(__name__)

EMU_PER_IN = 914400
# Cross-reference type names for \cref / \Cref / \autoref.
CREF_NAMES = {
    "section": ("section", "Section"),
    "appendix": ("appendix", "Appendix"),
    "equation": ("Eq.", "Eq."),
    "figure": ("Figure", "Figure"),
    "subfigure": ("Figure", "Figure"),
    "table": ("Table", "Table"),
    "algorithm": ("Algorithm", "Algorithm"),
    "line": ("line", "Line"),
    "theorem": ("Theorem", "Theorem"),
    "listing": ("Listing", "Listing"),
}
CREF_PLURALS = {"Eq.": "Eqs.", "section": "sections", "Section": "Sections", "appendix": "appendices",
                "Appendix": "Appendices", "line": "lines", "Line": "Lines"}
DAGGERS = ["†", "‡", "§", "¶", "‖"]


@dataclass
class Options:
    figure_width: float | None = 3.25  # inches; None = honour \includegraphics width
    date: str | None = None


class PostProcessor:
    def __init__(self, pkg: Package, conv: Conversion, template: Package, opts: Options):
        self.pkg = pkg
        self.conv = conv
        self.reg = conv.registry
        self.template = template
        self.opts = opts
        self.doc = pkg.xml("word/document.xml")
        self.body = self.doc.find(q("w:body"))
        self.roots = [self.doc] + ([pkg.xml("word/footnotes.xml")] if pkg.has("word/footnotes.xml") else [])
        self.search_dirs = list(dict.fromkeys([conv.source_dir, *conv.graphics_paths]))
        self._media: dict[tuple, tuple[images.RasterImage, str]] = {}  # (path, options) -> (raster, rId)
        self._bibnum = {k: n for n, k in enumerate(self.reg.bibitems, 1)}
        self.doc_pr_id = 1000
        self.our_bookmarks: set[str] = set()
        sect = self.body.find(q("w:sectPr"))
        self.text_width = 9360
        if sect is not None:
            sz = sect.find(q("w:pgSz"))
            mar = sect.find(q("w:pgMar"))
            if sz is not None and mar is not None:
                self.text_width = int(sz.get(q("w:w"))) - int(mar.get(q("w:left"))) - int(mar.get(q("w:right")))
        self.appendix_num = 0

    @staticmethod
    def warn(msg: str) -> None:
        log.warning(msg)

    # ================================================================ driver
    def run(self) -> None:
        roots = self.roots
        max_id = 0
        for root in roots:
            normalise_markers(root)
            kept = _remove_bookmarks(root, lambda name: name.startswith("ref-"))  # citeproc entries, renamed later
            max_id = max([max_id, *kept])
        self.bm = ox.Bookmarks(max_id + 1)
        # block markers never move to another paragraph before their pass runs, so index them once
        self._markers: dict[str, list] = defaultdict(list)
        for mk in self.body.iter(MK):
            self._markers[mk.get("name")].append((mk, _ancestor(mk, "w:p")))
        self._setup_numbering()
        self._pass_frontmatter()
        self._pass_headings()
        self._pass_equations()
        self._pass_figures()
        self._pass_tables()
        self._pass_algorithms()
        self._pass_theorems()
        self._pass_manual_bibliography()
        self._pass_citeproc_bibliography()
        for root in roots:
            self._pass_inline(root)
        self._style_lists()
        self._style_code_blocks()
        for root in roots:
            _remove_bookmarks(root, lambda name: name in self.our_bookmarks)
        self._header_and_properties()
        self.pkg.drop_orphan_media()
        for root in roots:
            leftover = root.findall(".//" + MK)
            for mk in leftover:
                self.warn(f"unhandled marker {mk.get('name')}{mk.get('args')}")
                mk.getparent().remove(mk)
            etree.cleanup_namespaces(root)

    # ============================================================ utilities
    def _marker_paragraphs(self, name: str) -> list[tuple[etree._Element, etree._Element]]:
        return [(mk, p) for mk, p in self._markers.get(name, []) if p is not None]

    def _block_markers(self, block: list[etree._Element]):
        """Yield ``(name, args, paragraph)`` for marker paragraphs inside a block."""
        for b in block:
            m = b.find(MK) if b.tag == q("w:p") else None
            if m is not None:
                yield m.get("name"), self._args(m), b

    @staticmethod
    def _args(mk) -> list[int]:
        a = mk.get("args") or ""
        return [int(x) for x in a.split(":") if x != ""]

    def _content_without_marker(self, p: etree._Element) -> list[etree._Element]:
        items = []
        for c in ox.content_children(p):
            if c.tag == MK and c.get("name") in _BLOCK_MARKERS:
                continue
            items.append(c)
        return ox.strip_leading_space(items)

    def _block(self, start_p: etree._Element, end_name: str, k: int) -> list[etree._Element]:
        """Sibling elements from the start paragraph up to (and including) the end marker paragraph."""
        out = [start_p]
        cur = start_p.getnext()
        while cur is not None:
            out.append(cur)
            mk = cur.find(MK) if cur.tag == q("w:p") else None
            if mk is not None and mk.get("name") == end_name and self._args(mk)[:1] == [k]:
                return out
            cur = cur.getnext()
        self.warn(f"no {end_name} marker for block {k}")
        return out

    def _bookmark_wrap(self, name: str | None, content: list[etree._Element]) -> list[etree._Element]:
        if not name:
            return content
        s, e = self.bm.pair(name)
        self.our_bookmarks.add(name)
        return [s, *content, e]

    def _seq(self, kind: str, number: str | None, bookmark: str | None, label: str) -> list[etree._Element]:
        """``Figure 1`` style caption lead: label text + bookmarked SEQ field."""
        hl = ox.rpr(style="Hyperlink")
        if number is None:
            return []
        runs = [ox.run(f"{label} ", hl)]
        runs += self._bookmark_wrap(bookmark, ox.field(f"SEQ {kind} \\* ARABIC", number, hl))
        return runs

    # ============================================================ numbering
    def _setup_numbering(self) -> None:
        """Copy the template's heading numbering (arabic and appendix) into the output."""
        num = self.pkg.xml("word/numbering.xml")
        tnum = self.template.xml("word/numbering.xml")
        tstyles = self.template.xml("word/styles.xml")
        # which template num does Heading1 use?
        h1 = tstyles.find(f".//{q('w:style')}[@{q('w:styleId')}='Heading1']")
        h_numid = h1.find(f".//{q('w:numId')}").get(q("w:val")) if h1 is not None and h1.find(f".//{q('w:numId')}") is not None else None
        used_abs = {a.get(q("w:abstractNumId")) for a in num.findall(q("w:abstractNum"))}
        used_num = {n.get(q("w:numId")) for n in num.findall(q("w:num"))}

        def fresh(used: set[str], start: int) -> str:
            i = start
            while str(i) in used:
                i += 1
            used.add(str(i))
            return str(i)

        def copy_num(tnum_id: str) -> str:
            tn = tnum.find(f"{q('w:num')}[@{q('w:numId')}='{tnum_id}']")
            abs_id = tn.find(q("w:abstractNumId")).get(q("w:val"))
            ta = tnum.find(f"{q('w:abstractNum')}[@{q('w:abstractNumId')}='{abs_id}']")
            new_abs = copy.deepcopy(ta)
            new_abs_id = fresh(used_abs, 8000)
            new_abs.set(q("w:abstractNumId"), new_abs_id)
            nsid = new_abs.find(q("w:nsid"))
            if nsid is not None:
                nsid.set(q("w:val"), f"{int(new_abs_id) + 0x5E000000:08X}")
            first_num = num.find(q("w:num"))
            if first_num is not None:
                first_num.addprevious(new_abs)
            else:
                num.append(new_abs)
            new_num = el("w:num", {"w:numId": fresh(used_num, 9000)}, el("w:abstractNumId", {"w:val": new_abs_id}))
            num.append(new_num)
            return new_num.get(q("w:numId"))

        if h_numid is None:
            self.warn("template Heading1 style has no numbering; headings will be unnumbered")
            return
        heading_num = copy_num(h_numid)
        # the appendix list is the template's other heading-style list (upper letters)
        app_id = None
        for tn in tnum.findall(q("w:num")):
            abs_id = tn.find(q("w:abstractNumId")).get(q("w:val"))
            ta = tnum.find(f"{q('w:abstractNum')}[@{q('w:abstractNumId')}='{abs_id}']")
            lvl0 = ta.find(q("w:lvl")) if ta is not None else None
            if lvl0 is not None and lvl0.find(q("w:pStyle")) is not None and lvl0.find(q("w:numFmt")).get(q("w:val")) == "upperLetter":
                app_id = tn.get(q("w:numId"))
                break
        if app_id:
            self.appendix_num = int(copy_num(app_id))
        elif any(h.appendix for h in self.reg.headings):
            self.warn("template has no lettered heading list; appendix headings use arabic numbers")
        # point the heading styles at the copied list
        styles = self.pkg.xml("word/styles.xml")
        for st in styles.findall(q("w:style")):
            nid = st.find(f"{q('w:pPr')}/{q('w:numPr')}/{q('w:numId')}")
            if nid is not None and nid.get(q("w:val")) == h_numid:
                nid.set(q("w:val"), heading_num)

    def _style_lists(self) -> None:
        """Give pandoc's bullet lists the template's bullet glyphs and font."""
        num = self.pkg.xml("word/numbering.xml")
        glyphs = ["•", "◦", "▪"]
        for a in num.findall(q("w:abstractNum")):
            for lvl in a.findall(q("w:lvl")):
                fmt = lvl.find(q("w:numFmt"))
                if fmt is None:
                    continue
                if fmt.get(q("w:val")) != "bullet":
                    # "(iii)" is wider than the default 360-twip hanging indent: widen it like the template
                    text = lvl.find(q("w:lvlText"))
                    wide = fmt.get(q("w:val")) in ("lowerRoman", "upperRoman") or "(" in (text.get(q("w:val")) if text is not None else "")
                    ind = lvl.find(f"{q('w:pPr')}/{q('w:ind')}")
                    if wide and ind is not None and int(ind.get(q("w:hanging"), "0")) < 720:
                        left = int(ind.get(q("w:left"), "720"))
                        ind.set(q("w:left"), str(left + 360))
                        ind.set(q("w:hanging"), "720")
                    continue
                ilvl = int(lvl.get(q("w:ilvl")))
                lvl.find(q("w:lvlText")).set(q("w:val"), glyphs[ilvl % 3])
                old = lvl.find(q("w:rPr"))
                if old is not None:
                    lvl.remove(old)
                lvl.append(el("w:rPr", None, el("w:rFonts", {"w:ascii": "Times New Roman", "w:hAnsi": "Times New Roman",
                                                              "w:cs": "Times New Roman", "w:eastAsia": "Times New Roman"})))

    def _style_code_blocks(self) -> None:
        """Code blocks: single-spaced monospace via styles, not the body text they inherit.

        The template's *Verbatim Char* stays as it is (it also formats e-mail addresses);
        code runs get their own *Source Code Char* style, which pandoc's token styles
        (``KeywordTok`` …) are re-based on, so users can restyle all code in one place.
        """
        mono = {"w:ascii": "Courier New", "w:hAnsi": "Courier New", "w:cs": "Courier New", "w:eastAsia": "Courier New"}
        size = (el("w:sz", {"w:val": "20"}), el("w:szCs", {"w:val": "20"}))
        styles = self.pkg.xml("word/styles.xml")
        by_id = {st.get(q("w:styleId")): st for st in styles.findall(q("w:style"))}
        code = by_id.get("SourceCode")
        if code is None:
            return
        for tag in ("w:pPr", "w:rPr"):
            for old in code.findall(q(tag)):
                code.remove(old)
        code.append(el("w:pPr", None, el("w:wordWrap", {"w:val": "off"}),
                       el("w:spacing", {"w:before": "0", "w:after": "120", "w:line": "240", "w:lineRule": "auto"}),
                       el("w:contextualSpacing"), el("w:jc", {"w:val": "left"})))
        code.append(el("w:rPr", None, el("w:rFonts", mono), *copy.deepcopy(size)))
        char = el("w:style", {"w:type": "character", "w:customStyle": "1", "w:styleId": "SourceCodeChar"},
                  el("w:name", {"w:val": "Source Code Char"}), el("w:basedOn", {"w:val": "DefaultParagraphFont"}),
                  el("w:rPr", None, el("w:rFonts", mono), *copy.deepcopy(size)))
        styles.append(char)
        for sid, st in by_id.items():
            based = st.find(q("w:basedOn"))
            if sid.endswith("Tok") and based is not None and based.get(q("w:val")) == "VerbatimChar":
                based.set(q("w:val"), "SourceCodeChar")
        for p in self.body.iter(q("w:p")):
            if ox.para_style(p) == "SourceCode":
                for rs in p.iter(q("w:rStyle")):
                    if rs.get(q("w:val")) == "VerbatimChar":
                        rs.set(q("w:val"), "SourceCodeChar")

    # ========================================================== front matter
    def _pass_frontmatter(self) -> None:
        fm = self.conv.front
        found = {}
        for mk in list(self.body.iter(MK)):
            if mk.get("name", "").startswith("FM"):
                p = _ancestor(mk, "w:p")
                found.setdefault((mk.get("name"), mk.get("args")), p)
        if not found:
            return
        # lxml elements are falsy when childless, so test "is not None" explicitly
        start = next((found[k] for k in (("FMTITLE", ""), ("FMAUTHOR", "0")) if found.get(k) is not None),
                     next(iter(found.values())))
        end = found.get(("FMEND", ""))
        new: list[etree._Element] = []

        def runs_of(key):
            p = found.get(key)
            return self._content_without_marker(p) if p is not None else []

        if fm.title:
            new.append(ox.paragraph(ox.ppr("Title"), runs_of(("FMTITLE", ""))))

        if fm.authors:
            content: list[etree._Element] = []
            affil_num = {a.key: a.number for a in fm.affiliations}
            notes = self.conv.equal_notes
            for i, a in enumerate(fm.authors):
                content += runs_of(("FMAUTHOR", str(i)))
                marks = [str(affil_num.get(k, k)) for k in a.affils]
                if a.corresponding:
                    marks.append("*")
                if a.equal:
                    marks.append(DAGGERS[notes.index(a.equal) % len(DAGGERS)])
                if marks:
                    content.append(ox.run(",".join(marks), sup=True))
                if i < len(fm.authors) - 1:
                    content += [ox.run(","), ox.run(" ")]
            for i, aff in sorted(enumerate(fm.affiliations), key=lambda t: t[1].number):
                content += [ox.special_run("w:br"), ox.run(" "), ox.run(str(aff.number), sup=True)]
                content += runs_of(("FMAFFIL", str(i)))
            corr = [e for a in fm.authors if a.corresponding for e in a.emails]
            others = [e for a in fm.authors if not a.corresponding for e in a.emails]
            if corr:
                content += [ox.special_run("w:br"), ox.run(" "), ox.run("*", sup=True), ox.run("Corresponding author:"), ox.run(" ")]
                for j, e in enumerate(corr):
                    if j:
                        content.append(ox.run("; "))
                    content.append(ox.run(e, style="VerbatimChar"))
            if others:
                content += [ox.special_run("w:br"), ox.run(" "), ox.run("Contributing authors:"), ox.run(" ")]
                for j, e in enumerate(others):
                    if j:
                        content.append(ox.run("; "))
                    content.append(ox.run(e, style="VerbatimChar"))
            for i, _ in enumerate(notes):
                content += [ox.special_run("w:br"), ox.run(" "), ox.run(DAGGERS[i % len(DAGGERS)], sup=True)]
                content += runs_of(("FMNOTE", str(i)))
            new.append(ox.paragraph(ox.ppr("Author"), content))

        abs_p = found.get(("FMABSTRACT", ""))
        if abs_p is not None:
            new.append(ox.paragraph(ox.ppr("AbstractTitle"), [ox.run("Abstract")]))
            cur = abs_p.getnext()
            stop = {found.get(("FMKEYWORDS", "")), end}
            while cur is not None and cur not in stop:
                nxt = cur.getnext()
                if cur.tag == q("w:p"):
                    ox.set_ppr(cur, pStyle=el("w:pStyle", {"w:val": "Abstract"}))
                    new.append(cur)
                cur = nxt
        kw = runs_of(("FMKEYWORDS", ""))
        if kw:
            new.append(ox.paragraph(ox.ppr("FirstParagraph"), [ox.run("Keywords:", bold=True), ox.run(" "), *kw]))

        # replace the original block (start marker .. end marker) by the new paragraphs
        i0 = self.body.index(start)
        i1 = self.body.index(end) if end is not None else i0
        _replace_block(self.body[i0 : i1 + 1], new)

    # ============================================================== headings
    def _pass_headings(self) -> None:
        for mk, p in self._marker_paragraphs("HEAD"):
            j = self._args(mk)[0]
            h = self.reg.headings[j]
            mk.getparent().remove(mk)
            items = ox.strip_leading_space(ox.content_children(p))
            for c in ox.content_children(p):
                p.remove(c)
            if not h.numbered:
                ox.set_ppr(p, numPr=ox.num_pr(0, 0))
            elif h.appendix and self.appendix_num:
                ox.set_ppr(p, numPr=ox.num_pr(self.appendix_num, h.level - 1))
            content = [ox.run(" ")] + items if h.numbered else items
            for c in self._bookmark_wrap(h.bookmark, content):
                p.append(c)

    # ============================================================= equations
    def _pass_equations(self) -> None:
        for mk, p in self._marker_paragraphs("EQ"):
            k = self._args(mk)[0]
            eq = self.reg.equations[k]
            nxt = p.getnext()
            omp = nxt.find(".//" + q("m:oMathPara")) if nxt is not None and nxt.tag == q("w:p") else None
            if omp is None:
                self.warn(f"equation {k}: pandoc produced no display math")
                p.getparent().remove(p)
                continue
            maths = omp.findall(q("m:oMath"))
            if eq.number is not None:
                props = ox.ppr("BodyText", tabs=[("right", self.text_width)])
            else:
                props = ox.ppr("BodyText", jc="left")
            content: list[etree._Element] = list(maths)
            content.append(ox.special_run("w:tab"))
            if eq.number is not None:
                content.append(ox.run("("))
                if eq.tag:
                    num_runs = [ox.run(eq.number)]
                else:
                    num_runs = ox.field("SEQ equation \\* ARABIC", eq.number)
                content += self._bookmark_wrap(eq.bookmark, num_runs)
                content.append(ox.run(")"))
            new = ox.paragraph(props, content)
            # any text pandoc left in the math paragraph (unlikely) goes after the equation
            nxt.addprevious(new)
            nxt.getparent().remove(nxt)
            p.getparent().remove(p)

    # =============================================================== figures
    def _raster(self, path: Path, options: str | None) -> tuple[images.RasterImage, str]:
        """Convert and embed an image once, however often it is included."""
        key = (path, options)
        if key not in self._media:
            raster = images.load_image(path, options)
            self._media[key] = (raster, self.pkg.add_media(raster.data, raster.ext))
        return self._media[key]

    def _image_paragraphs(self, panels, style: str) -> list[etree._Element]:
        out = []
        for panel in panels:
            for img in panel.images:
                path = images.resolve(img.source, self.search_dirs)
                if path is None:
                    self.warn(f"image not found: {img.source}")
                    out.append(ox.paragraph(ox.ppr(style, keep_next=True, jc="center"),
                                            [ox.run(f"[missing figure: {img.source}]", bold=True)]))
                    continue
                try:
                    raster, rid = self._raster(path, img.options)
                except Exception as exc:  # conversion failure: keep going with a placeholder
                    self.warn(f"cannot convert image {path}: {exc}")
                    out.append(ox.paragraph(ox.ppr(style, keep_next=True, jc="center"),
                                            [ox.run(f"[unconvertible figure: {img.source}]", bold=True)]))
                    continue
                width_in = self.opts.figure_width
                if width_in is None:
                    width_in = images.requested_width(img.options, self.text_width / 1440) or min(
                        self.text_width / 1440, raster.width_px / images.DPI)
                width_in = min(width_in, self.text_width / 1440)
                cx = int(width_in * EMU_PER_IN)
                cy = int(cx * raster.height_px / max(1, raster.width_px))
                max_h = int(8.0 * EMU_PER_IN)
                if cy > max_h:
                    cx, cy = int(cx * max_h / cy), max_h
                self.doc_pr_id += 1
                run = ox.drawing(rid, cx, cy, self.doc_pr_id, path.name, img.source)
                out.append(ox.paragraph(ox.ppr(style, keep_next=True, jc="center"), [run]))
        return out

    def _pass_figures(self) -> None:
        for mk, p in self._marker_paragraphs("FIG"):
            k = self._args(mk)[0]
            fig = self.reg.figures[k]
            block = self._block(p, "FIGEND", k)
            subcaps: dict[int, etree._Element] = {}
            caption = None
            for name, args, b in self._block_markers(block):
                if name == "SUBCAP":
                    subcaps[args[1]] = b
                elif name == "FIGCAP":
                    caption = b
            multi = len(fig.panels) > 1 or bool(subcaps)
            new: list[etree._Element] = []
            for idx, panel in enumerate(fig.panels):
                paras = self._image_paragraphs([panel], "Compact" if multi else "CaptionedFigure")
                if idx not in subcaps and panel.bookmark and paras:
                    content = ox.content_children(paras[0])
                    for c in self._bookmark_wrap(panel.bookmark, content):
                        paras[0].append(c)
                new += paras
                if idx in subcaps:
                    runs = [ox.run(f"({panel.letter}) ")] + self._content_without_marker(subcaps[idx])
                    new.append(ox.paragraph(ox.ppr("Compact", keep_next=True, jc="center"),
                                            self._bookmark_wrap(panel.bookmark, runs)))
            if caption is not None:
                runs = self._seq("figure", fig.number, fig.bookmark, "Figure")
                runs.append(ox.run(": "))
                runs += self._content_without_marker(caption)
                new.append(ox.paragraph(ox.ppr("ImageCaption"), runs))
            elif new:
                ox.set_ppr(new[-1], keepNext=None)
            _replace_block(block, new)

    # ================================================================ tables
    def _style_table(self, tbl: etree._Element, caption_text: str | None = None) -> None:
        old = tbl.find(q("w:tblPr"))
        rows = tbl.findall(q("w:tr"))
        header_rows = [r for r in rows if r.find(f"{q('w:trPr')}/{q('w:tblHeader')}") is not None]
        new = el("w:tblPr", None,
                 el("w:tblStyle", {"w:val": "Table"}),
                 el("w:tblW", {"w:type": "auto", "w:w": "0"}),
                 el("w:jc", {"w:val": "center"}),
                 el("w:tblBorders", None,
                    el("w:top", {"w:val": "single", "w:sz": "8", "w:space": "0", "w:color": "000000"}),
                    el("w:left", {"w:val": "none"}),
                    el("w:bottom", {"w:val": "single", "w:sz": "8", "w:space": "0", "w:color": "000000"}),
                    el("w:right", {"w:val": "none"}),
                    el("w:insideH", {"w:val": "none"}),
                    el("w:insideV", {"w:val": "none"})))
        first_row = "1" if len(header_rows) == 1 else "0"
        new.append(el("w:tblLook", {"w:firstRow": first_row, "w:lastRow": "0", "w:firstColumn": "0", "w:lastColumn": "0",
                                    "w:noHBand": "0", "w:noVBand": "0", "w:val": "0020" if first_row == "1" else "0000"}))
        if caption_text:
            new.append(el("w:tblCaption", {"w:val": caption_text[:250]}))
        if old is not None:
            tbl.replace(old, new)
        else:
            tbl.insert(0, new)
        for r in rows:
            trpr = r.find(q("w:trPr"))
            if trpr is None:
                trpr = el("w:trPr")
                r.insert(0, trpr)
            if trpr.find(q("w:cantSplit")) is None:
                trpr.append(el("w:cantSplit"))
            # schema order: cantSplit before tblHeader
            hdr = trpr.find(q("w:tblHeader"))
            if hdr is not None:
                trpr.remove(hdr)
                trpr.append(hdr)
        if len(header_rows) > 1:  # the table style only rules off the first row
            for tc in header_rows[-1].findall(q("w:tc")):
                _cell_border(tc, "bottom", 4)
        for p in tbl.iter(q("w:p")):
            jc = p.find(f"{q('w:pPr')}/{q('w:jc')}")
            ox.set_ppr(p, pStyle=el("w:pStyle", {"w:val": "Compact"}), keepNext=el("w:keepNext"),
                       spacing=el("w:spacing", {"w:before": "0", "w:after": "0", "w:line": "240", "w:lineRule": "auto"}),
                       jc=el("w:jc", {"w:val": jc.get(q("w:val")) if jc is not None else "left"}))

    def _apply_header_rows(self) -> None:
        """Apply TBLHDR markers: header rows (pandoc drops multi-row heads) and partial rules.

        Arguments: header row count, then ``row, first column, last column`` per
        ``\\cmidrule``/``\\cline``; the rule becomes a bottom border on the cells it spans.
        """
        for mk, p in self._marker_paragraphs("TBLHDR"):
            n, *rules = self._args(mk)
            tbl = p.getnext()
            p.getparent().remove(p)
            if tbl is None or tbl.tag != q("w:tbl"):
                continue
            rows = tbl.findall(q("w:tr"))
            for r, first, last in zip(rules[0::3], rules[1::3], rules[2::3]):
                if r < len(rows):
                    col = 1
                    for tc in rows[r].findall(q("w:tc")):
                        span = tc.find(f"{q('w:tcPr')}/{q('w:gridSpan')}")
                        width = int(span.get(q("w:val"), "1")) if span is not None else 1
                        if col <= last and col + width - 1 >= first:
                            _cell_border(tc, "bottom", 4)
                        col += width
            for i, tr in enumerate(rows):
                trpr = tr.find(q("w:trPr"))
                if trpr is not None:
                    for h in trpr.findall(q("w:tblHeader")):
                        trpr.remove(h)
                if i < n:
                    if trpr is None:
                        trpr = el("w:trPr")
                        tr.insert(0, trpr)
                    trpr.append(el("w:tblHeader", {"w:val": "on"}))

    def _pass_tables(self) -> None:
        self._apply_header_rows()
        handled = set()
        for mk, p in self._marker_paragraphs("TAB"):
            k = self._args(mk)[0]
            tab = self.reg.tables[k]
            block = self._block(p, "TABEND", k)
            tables = [b for b in block if b.tag == q("w:tbl")]
            caption = None
            notes = []
            for name, _, b in self._block_markers(block):
                if name == "TABCAP":
                    caption = b
                elif name == "TABNOTE":
                    notes.append(b)
            cap_runs = self._content_without_marker(caption) if caption is not None else []
            cap_text = f"Table {tab.number}: {ox.plain_text(cap_runs)}" if tab.number else None
            new: list[etree._Element] = []
            for t in tables:
                self._style_table(t, cap_text)
                handled.add(t)
                new.append(t)
            if tab.panels:
                new += self._image_paragraphs(tab.panels, "CaptionedFigure")
            for n in notes:
                new.append(ox.paragraph(
                    ox.ppr("Compact", keep_next=caption is not None, jc="left",
                           spacing={"before": "0", "after": "0", "line": "240", "lineRule": "auto"}),
                    [_resize(r, 20) for r in self._content_without_marker(n)]))
            if caption is not None:
                runs = self._seq("table", tab.number, tab.bookmark, "Table")
                runs.append(ox.run(": "))
                runs += cap_runs
                new.append(ox.paragraph(ox.ppr("TableCaption"), runs))
            if not tables and not tab.panels:
                self.warn(f"table {tab.number or k}: no tabular content was converted")
            _replace_block(block, new)
        for t in self.body.iter(q("w:tbl")):
            if t not in handled:
                self._style_table(t)

    # ============================================================ algorithms
    def _pass_algorithms(self) -> None:
        for mk, p in self._marker_paragraphs("ALG"):
            k = self._args(mk)[0]
            alg = self.reg.algorithms[k]
            block = self._block(p, "ALGEND", k)
            caption = None
            lines = []
            for name, args, b in self._block_markers(block):
                if name == "ALGCAP":
                    caption = b
                elif name == "ALGLINE":
                    lines.append((args, b))
            new: list[etree._Element] = []
            if caption is not None:
                bold = ox.rpr(bold=True)
                runs = [ox.run("Algorithm ", bold)]
                runs += self._bookmark_wrap(alg.bookmark, ox.field("SEQ algorithm \\* ARABIC", alg.number or "", bold))
                runs.append(ox.run(" "))
                runs += self._content_without_marker(caption)
                new.append(ox.paragraph(ox.ppr("Compact", keep_next=True, jc="left", borders={"top": 8, "bottom": 4},
                                               spacing={"before": "120", "after": "60", "line": "240", "lineRule": "auto"}),
                                        runs))
            numbered = any(a[2] for a, _ in lines)
            gutter = 400 if numbered else 0
            for n, (args, b) in enumerate(lines):
                _, indent, lineno = args
                left = gutter + 360 * indent
                runs = []
                if numbered:
                    runs += [ox.run(f"{lineno}:" if lineno else "", size=18), ox.special_run("w:tab")]
                runs += self._content_without_marker(b)
                last = n == len(lines) - 1
                props = ox.ppr("Compact", keep_next=not last, jc="left",
                               borders={"bottom": 8} if last else None,
                               spacing={"before": "0", "after": "0", "line": "276", "lineRule": "auto"},
                               ind=({"left": left, "hanging": left} if numbered else {"left": left}) if left else None)
                new.append(ox.paragraph(props, runs))
            _replace_block(block, new)

    # ============================================================== theorems
    _THEOREM_FONTS = {  # style -> (head bold, head italic, body italic), as in amsthm / sn-jnl
        "plain": (True, False, True),
        "definition": (True, False, False),
        "remark": (False, True, False),
        "roman-head": (False, False, True),
    }

    def _pass_theorems(self) -> None:
        for mk, p in self._marker_paragraphs("THM"):
            k = self._args(mk)[0]
            thm = self.reg.theorems[k]
            block = self._block(p, "THMEND", k)
            note = next((b for name, _, b in self._block_markers(block) if name == "THMNOTE"), None)
            body = [b for b in block[1:-1] if b is not note]
            head_bold, head_italic, body_italic = self._THEOREM_FONTS.get(thm.style, self._THEOREM_FONTS["plain"])
            head_font = ox.rpr(bold=head_bold, italic=head_italic)
            head = [ox.run(thm.head, head_font)]
            if note is not None:
                head += [ox.run(" (")] + self._content_without_marker(note) + [ox.run(")")]
            head.append(ox.run(".", head_font))
            head.append(ox.run(" "))
            if body_italic:
                for b in body:
                    if _is_equation(b):  # equation numbers stay upright, as in LaTeX
                        continue
                    for r in b.iter(q("w:r")):
                        rp = r.find(q("w:rPr"))
                        if rp is None or rp.find(q("w:i")) is None:
                            _set_rpr(r, ox.rpr(italic=True, base=rp))
            first = body[0] if body else None
            content = ox.content_children(first) if first is not None and first.tag == q("w:p") else []
            # the head runs into an ordinary first paragraph, but not into a display equation or list item
            inline = (bool(content) and not _is_equation(first)
                      and first.find(f"{q('w:pPr')}/{q('w:numPr')}") is None)
            if inline:
                for c in content:
                    first.remove(c)
                for c in head + ox.strip_leading_space(content):
                    first.append(c)
                new = body
            else:
                new = [ox.paragraph(ox.ppr("BodyText"), head), *body]
            _replace_block(block, new)

    # ========================================================== bibliography
    def _pass_manual_bibliography(self) -> None:
        for mk, p in self._marker_paragraphs("BIB"):
            n = self._args(mk)[0]
            runs = self._content_without_marker(p)
            label = [] if self.conv.citation_mode == "author-year" else [ox.run(f"[{n + 1}] ")]
            new = ox.paragraph(_bib_ppr(), self._bookmark_wrap(f"bibref_{n + 1}", label + runs))
            p.addprevious(new)
            p.getparent().remove(p)

    def _pass_citeproc_bibliography(self) -> None:
        bib_paras = [p for p in self.body.iter(q("w:p")) if ox.para_style(p) == "Bibliography"]
        if not bib_paras:
            return
        # map pandoc's ref-KEY bookmarks to bibref_N in bibliography order
        mapping: dict[str, str] = {}
        for n, p in enumerate(bib_paras, 1):
            start = p.getprevious()
            key = None
            while start is not None and start.tag == q("w:bookmarkStart"):
                name = start.get(q("w:name"), "")
                if name.startswith("ref-"):
                    key = name
                    break
                start = start.getprevious()
            new_name = f"bibref_{n}"
            if key:
                mapping[key] = new_name
            content = ox.content_children(p)
            for c in content:
                p.remove(c)
            for c in self._bookmark_wrap(new_name, content):
                p.append(c)
            p.replace(p.find(q("w:pPr")), _bib_ppr())
        for root in self.roots:
            for h in root.iter(q("w:hyperlink")):
                anchor = h.get(q("w:anchor"))
                if anchor in mapping:
                    h.set(q("w:anchor"), mapping[anchor])
                    for r in h.findall(q("w:r")):
                        _set_rpr(r, ox.rpr(style="Hyperlink", color="000000", base=_strip_color(r.find(q("w:rPr")))))

    # ============================================================ inline marks
    def _pass_inline(self, root) -> None:
        for mk in list(root.iter(MK)):
            name = mk.get("name")
            args = self._args(mk)
            if name == "REF":
                repl = self._ref_runs(args[0], _prev_rpr(mk))
            elif name == "ANCHOR":
                bm = self.reg.anchors[args[0]]
                repl = self._bookmark_wrap(bm, [])
            elif name == "CITE":
                repl = self._cite_runs(args[0], _prev_rpr(mk))
            else:
                continue
            parent = mk.getparent()
            for r in repl:
                mk.addprevious(r)
            parent.remove(mk)
        # markers that ended up inside math: substitute plain text
        for t in root.iter(q("m:t"), q("w:t")):
            if t.text and "@@" in t.text:
                t.text = MARKER_RE.sub(lambda m: self._marker_text(m.group(1), m.group(2)), t.text)

    def _marker_text(self, name: str, args: str) -> str:
        """Plain-text rendering of a REF marker (used where fields cannot go, e.g. inside math)."""
        if name != "REF" or not args:
            return ""
        label, variant = self.reg.refs[int(args)]
        lab = self.reg.labels.get(label)
        if lab is None:
            return "??"
        prefix, parens = format_ref(lab, variant, self.conv.cref_names)
        number = f"({lab.text})" if parens else lab.text
        return f"{prefix} {number}" if prefix else number

    def _ref_runs(self, i: int, base) -> list[etree._Element]:
        label, variant = self.reg.refs[i]
        lab: Label | None = self.reg.labels.get(label)
        if lab is None:
            self.warn(f"undefined reference: {label}")
            return [ox.run("??", base) if base is not None else ox.run("??", bold=True)]
        hl = ox.rpr(style="Hyperlink", base=_strip_color(base))
        runs: list[etree._Element] = []
        prefix, parens = format_ref(lab, variant, self.conv.cref_names)
        if prefix:
            runs.append(ox.run(f"{prefix} ", hl))
        if parens:
            runs.append(ox.run("(", hl))
        if lab.field and lab.bookmark:
            switches = "\\w \\h" if lab.kind in ("section", "appendix") else "\\h"
            runs += ox.field(f"REF {lab.bookmark} {switches} \\* MERGEFORMAT", lab.text or "??", hl)
        elif lab.bookmark:
            h = el("w:hyperlink", {"w:anchor": lab.bookmark})
            h.append(ox.run(lab.text or "??", hl))
            runs.append(h)
        else:
            runs.append(ox.run(lab.text or "??", hl))
        if parens:
            runs.append(ox.run(")", hl))
        return runs

    def _cite_runs(self, i: int, base) -> list[etree._Element]:
        """Citation against a ``thebibliography`` list (natbib semantics, numeric or author-year)."""
        command, keys, pre, post = self.reg.cites[i]
        hl = ox.rpr(style="Hyperlink", color="000000", base=_strip_color(base))
        plain = copy.deepcopy(base) if base is not None else None
        nums = []
        for k in keys:
            if k in self._bibnum:
                nums.append(self._bibnum[k])
            else:
                self.warn(f"undefined citation: {k}")
        pre = latex_to_plain(pre) if pre else ""
        post = latex_to_plain(post) if post else ""

        def link(n: int, text: str) -> etree._Element:
            h = el("w:hyperlink", {"w:anchor": f"bibref_{n}"})
            h.append(ox.run(text, hl))
            return h

        def who_year(n: int) -> tuple[str, str]:
            key = self.reg.bibitems[n - 1]
            label = self.reg.bib_labels.get(key, "")
            m = re.match(r"\{?(.*?)\((\d{4}[a-z]?)\)(.*)\}?$", label)
            return (latex_to_plain(m.group(1)), m.group(2)) if m else (latex_to_plain(label) or key, "")

        if command in ("citeauthor", "citeyear", "citeyearpar"):
            parts = []
            for n in nums:
                who, year = who_year(n)
                parts.append(link(n, who if command == "citeauthor" else year))
            runs = _joined(parts, ", ", plain)
            return [ox.run("(", plain), *runs, ox.run(")", plain)] if command == "citeyearpar" else runs

        textual = command in ("citet", "textcite")
        if self.conv.citation_mode == "author-year":
            if textual:  # Knuth (1984, p. 5)
                parts = []
                for n in nums:
                    who, year = who_year(n)
                    parts += [link(n, who), ox.run(f" ({year}", plain), ox.run(f", {post})" if post else ")", plain)]
                    parts.append(ox.run(", ", plain))
                return parts[:-1]
            items = [link(n, "%s, %s" % who_year(n)) for n in nums]
            runs = [ox.run("(" + (f"{pre} " if pre else ""), plain), *_joined(items, "; ", plain)]
            return runs + [ox.run((f", {post}" if post else "") + ")", plain)]
        items = [link(a, str(a) if a == b else f"{a}–{b}") for a, b in _ranges(sorted(set(nums)))]
        runs = [ox.run("[" + (f"{pre} " if pre else ""), plain), *_joined(items, ", ", plain)]
        return runs + [ox.run((f", {post}" if post else "") + "]", plain)]

    # ============================================================ properties
    def _header_and_properties(self) -> None:
        fm = self.conv.front
        date = self.opts.date or (latex_to_plain(fm.date) if fm.date and "\\today" not in fm.date else None)
        if not date:
            today = dt.date.today()
            date = f"{today:%B} {today.day}, {today.year}"
        for name in list(self.pkg.parts):
            if re.fullmatch(r"word/header\d*\.xml", name):
                for t in self.pkg.xml(name).iter(q("w:t")):
                    # the template's header carries a sample date; keep its case and surrounding text
                    t.text = _DATE_RE.sub(lambda m: date.upper() if m.group(0).isupper() else date, t.text or "")
        title = latex_to_plain(fm.title)
        authors = [latex_to_plain(a.name) for a in fm.authors]
        core = self.pkg.xml("docProps/core.xml") if self.pkg.has("docProps/core.xml") else None
        if core is not None:
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _set_child(core, "dc:title", title)
            _set_child(core, "dc:creator", "; ".join(authors))
            _set_child(core, "cp:lastModifiedBy", authors[0] if authors else "")
            _set_child(core, "cp:keywords", "; ".join(latex_to_plain(k) for k in fm.keywords))
            _set_child(core, "dc:language", "en-US")
            for tag in ("dcterms:created", "dcterms:modified"):
                e = _set_child(core, tag, now)
                e.set("{%s}type" % NS["xsi"], "dcterms:W3CDTF")
        if self.pkg.has("docProps/app.xml"):
            app = self.pkg.xml("docProps/app.xml")
            for tag in ("Words", "Characters", "CharactersWithSpaces", "Paragraphs", "Lines", "Pages", "Company", "Manager"):
                for e in app.findall(q(f"ep:{tag}")):
                    app.remove(e)
            corr = next((a for a in fm.authors if a.corresponding), fm.authors[0] if fm.authors else None)
            if corr is not None:
                aff = next((x for x in fm.affiliations if corr.affils and x.key == corr.affils[0]), None)
                if aff is not None:
                    _set_child(app, "ep:Company", latex_to_plain(aff.text))
                email = f" <{corr.emails[0]}>" if corr.emails else ""
                _set_child(app, "ep:Manager", latex_to_plain(corr.name) + email)


# ------------------------------------------------------------------ helpers
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE_RE = re.compile(r"\b(?:%s) \d{1,2}, \d{4}\b" % _MONTHS, re.IGNORECASE)
_BLOCK_MARKERS = {
    "FMTITLE", "FMAUTHOR", "FMAFFIL", "FMNOTE", "FMABSTRACT", "FMKEYWORDS", "FMEND", "HEAD", "EQ", "FIG", "FIGCAP",
    "SUBCAP", "FIGEND", "TAB", "TABCAP", "TABNOTE", "TABEND", "ALG", "ALGCAP", "ALGLINE", "ALGEND", "BIB", "TBLHDR",
    "THM", "THMNOTE", "THMEND",
}


def normalise_markers(root: etree._Element) -> None:
    """Split ``@@NAME…@@`` text inside runs into standalone marker elements."""
    for t in list(root.iter(q("w:t"))):
        text = t.text or ""
        if "@@" not in text or not MARKER_RE.search(text):
            continue
        r = t.getparent()
        if r is None or r.tag != q("w:r"):
            continue
        rp = r.find(q("w:rPr"))
        pieces: list[etree._Element] = []
        pos = 0
        for m in MARKER_RE.finditer(text):
            if m.start() > pos:
                pieces.append(ox.run(text[pos : m.start()], rp))
            mk = etree.Element(MK, nsmap={"sn": MK_NS})
            mk.set("name", m.group(1))
            mk.set("args", m.group(2))
            pieces.append(mk)
            pos = m.end()
        if pos < len(text):
            pieces.append(ox.run(text[pos:], rp))
        others = [c for c in r if c.tag not in (q("w:rPr"), q("w:t"))]
        if others:  # keep tabs/breaks in a run of their own
            keep = ox.run("", rp)
            for o in others:
                keep.append(o)
            pieces.append(keep)
        for piece in pieces:
            r.addprevious(piece)
        r.getparent().remove(r)


def _ancestor(e: etree._Element, tag: str) -> etree._Element | None:
    t = q(tag)
    while e is not None and e.tag != t:
        e = e.getparent()
    return e


def _replace_block(block: list[etree._Element], new: list[etree._Element]) -> None:
    if not block:
        return
    anchor = block[0]
    parent = anchor.getparent()
    for n in new:
        anchor.addprevious(n)
    for b in block:
        if b.getparent() is parent and b not in new:
            parent.remove(b)


def _cell_border(tc: etree._Element, side: str, sz: int) -> None:
    tcpr = tc.find(q("w:tcPr"))
    if tcpr is None:
        tcpr = el("w:tcPr")
        tc.insert(0, tcpr)
    b = tcpr.find(q("w:tcBorders"))
    if b is not None and b.find(q(f"w:{side}")) is not None:
        return
    if b is None:
        b = el("w:tcBorders")
        # tcBorders comes after tcW/gridSpan/vMerge in CT_TcPr
        after = [c for c in tcpr if etree.QName(c).localname in ("tcW", "gridSpan", "hMerge", "vMerge")]
        if after:
            after[-1].addnext(b)
        else:
            tcpr.insert(0, b)
    b.append(el(f"w:{side}", {"w:val": "single", "w:sz": str(sz), "w:space": "0", "w:color": "000000"}))


def format_ref(lab: Label, variant: str, overrides: dict[str, dict[str, tuple[str, str]]] | None = None) -> tuple[str, bool]:
    """Type-name prefix (may be empty) and whether the number is parenthesised.

    ``variant`` comes from :func:`sn2docx.latex.transform.replace_refs`: ``ref``,
    ``eq``, ``cref``/``Cref`` (``+`` suffix: plural) or ``bare``. Names declared with
    ``\\crefname``/``\\Crefname`` (``overrides``) win over the built-in ones.
    """
    prefix = ""
    style = variant.rstrip("+")
    if style in ("cref", "Cref"):
        plural = variant.endswith("+")
        declared = next((overrides[t][style] for t in (lab.ref_type, lab.kind)
                         if t and overrides and style in overrides.get(t, {})), None)
        if declared is not None:
            prefix = declared[1] if plural else declared[0]
        else:
            if lab.type_name:  # theorem-like environments use their own title ("Lemma")
                prefix = lab.type_name
            else:
                names = CREF_NAMES.get(lab.kind, (lab.kind, lab.kind.capitalize()))
                prefix = names[0] if style == "cref" else names[1]
            if plural:
                prefix = CREF_PLURALS.get(prefix, prefix + "s")
    parens = variant == "eq" or (variant != "ref" and lab.kind == "equation")
    return prefix, parens


def _set_rpr(r: etree._Element, new: etree._Element | None) -> etree._Element:
    """Replace (or insert) a run's properties."""
    old = r.find(q("w:rPr"))
    if old is not None:
        r.remove(old)
    if new is not None:
        r.insert(0, new)
    return r


def _resize(r: etree._Element, size: int) -> etree._Element:
    return _set_rpr(r, ox.rpr(size=size, base=r.find(q("w:rPr")))) if r.tag == q("w:r") else r


def _bib_ppr() -> etree._Element:
    return ox.ppr("BodyText", jc="left", ind={"left": 567, "hanging": 567})


def _prev_rpr(mk: etree._Element):
    """Formatting of the run before a marker, minus character style and super/subscript."""
    prev = next(mk.itersiblings(q("w:r"), preceding=True), None)
    rp = prev.find(q("w:rPr")) if prev is not None else None
    if rp is None:
        return None
    rp = copy.deepcopy(rp)
    for tag in ("w:rStyle", "w:vertAlign"):
        for e in rp.findall(q(tag)):
            rp.remove(e)
    return rp


def _strip_color(rp):
    if rp is None:
        return None
    rp = copy.deepcopy(rp)
    for e in rp.findall(q("w:color")):
        rp.remove(e)
    return rp


def _is_equation(p: etree._Element) -> bool:
    """A display-equation paragraph (its first content is the equation itself)."""
    if p.tag != q("w:p"):
        return False
    content = [c for c in ox.content_children(p) if c.tag not in (q("w:bookmarkStart"), q("w:bookmarkEnd"))]
    return bool(content) and content[0].tag == q("m:oMath")


def _joined(items: list[etree._Element], sep: str, props) -> list[etree._Element]:
    out: list[etree._Element] = []
    for j, item in enumerate(items):
        if j:
            out.append(ox.run(sep, props))
        out.append(item)
    return out


def _ranges(nums: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    # a two-number run prints as "1, 2", matching natbib's compress
    res = []
    for a, b in out:
        if b == a + 1:
            res += [(a, a), (b, b)]
        else:
            res.append((a, b))
    return res


def _remove_bookmarks(root: etree._Element, keep) -> list[int]:
    """Remove bookmarks whose name fails ``keep(name)``; return the ids of those kept."""
    removed = set()
    kept = []
    for s in list(root.iter(q("w:bookmarkStart"))):
        if keep(s.get(q("w:name")) or ""):
            kept.append(int(s.get(q("w:id"), "0")))
        else:
            removed.add(s.get(q("w:id")))
            s.getparent().remove(s)
    for e in list(root.iter(q("w:bookmarkEnd"))):
        if e.get(q("w:id")) in removed:
            e.getparent().remove(e)
    return kept


def _set_child(parent: etree._Element, tag: str, text: str) -> etree._Element:
    e = parent.find(q(tag))
    if e is None:
        e = etree.SubElement(parent, q(tag))
    e.text = text
    return e


def postprocess(pandoc_docx: Path, out: Path, conv: Conversion, template: Package, opts: Options) -> None:
    """Rewrite pandoc's docx into the template layout; problems are reported through logging."""
    pkg = Package.open(pandoc_docx)
    PostProcessor(pkg, conv, template, opts).run()
    pkg.save(out)
