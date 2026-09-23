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
from pathlib import Path

from docx.shared import Pt
from lxml import etree

from .. import images
from ..latex import texdefs
from ..latex.scan import latex_to_plain
from ..model import MARKER_RE, Conversion, Label
from ..pandoc import default_style
from . import ooxml as ox
from .template_text import read_template_text
from .ooxml import MK, MK_NS, el, q
from .package import NS, Package

log = logging.getLogger(__name__)


class PostProcessor:
    def __init__(self, pkg: Package, conv: Conversion, template: Package):
        self.pkg = pkg
        self.conv = conv
        self.reg = conv.registry
        self.template = template
        self.doc = pkg.xml("word/document.xml")
        self.body = self.doc.find(q("w:body"))
        self.roots = [self.doc] + ([pkg.xml("word/footnotes.xml")] if pkg.has("word/footnotes.xml") else [])
        self.search_dirs = list(dict.fromkeys([conv.source_dir, *conv.graphics_paths]))
        self._media: dict[tuple, tuple[images.RasterImage, str]] = {}  # (path, options) -> (raster, rId)
        self._bibnum = {k: n for n, k in enumerate(self.reg.bibitems, 1)}
        # drawing ids continue after pandoc's own
        self.doc_pr_id = max([int(d.get("id", "0")) for d in self.doc.iter(f"{_WP}docPr")] or [0])
        self.our_bookmarks: set[str] = set()
        self.appendix_num = 0
        self.tt = read_template_text(template)  # the template's labels, punctuation and exemplar properties
        self.names = conv.names  # the LaTeX class's and packages' names (plain text)
        self.cls = conv.doc_class  # texdefs.DocumentClass: font sizes, \today

    @staticmethod
    def warn(msg: str) -> None:
        log.warning(msg)

    # ================================================================ driver
    def run(self) -> None:
        roots = self.roots
        max_id = 0
        for root in roots:
            normalise_markers(root)
            _remove_bookmarks(root, lambda name: False)
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
        self._pass_bibliography()
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

    def _caption_parts(self, kind: str) -> tuple[str, str]:
        """(label before the number, separator after it): the template's, else the class's name."""
        if kind in self.tt.captions:
            return self.tt.captions[kind]
        seps = [sep for _, sep in self.tt.captions.values()]
        return (self.names.get(kind + "name", "") + " ").lstrip(), seps[0] if seps else " "

    def _seq(self, kind: str, number: str | None, bookmark: str | None) -> list[etree._Element]:
        """``Figure 1`` style caption lead: label text + bookmarked SEQ field."""
        hl = self.tt.rpr("caption_label")
        if number is None:
            return []
        runs = [ox.run(self._caption_parts(kind)[0], hl)] if self._caption_parts(kind)[0] else []
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

        def fresh(used: set[str]) -> str:
            i = str(max([int(u) for u in used if u.isdigit()] or [0]) + 1)
            used.add(i)
            return i

        def copy_num(tnum_id: str) -> str:
            tn = tnum.find(f"{q('w:num')}[@{q('w:numId')}='{tnum_id}']")
            abs_id = tn.find(q("w:abstractNumId")).get(q("w:val"))
            ta = tnum.find(f"{q('w:abstractNum')}[@{q('w:abstractNumId')}='{abs_id}']")
            new_abs = copy.deepcopy(ta)
            new_abs_id = fresh(used_abs)
            new_abs.set(q("w:abstractNumId"), new_abs_id)
            for nsid in new_abs.findall(q("w:nsid")):  # optional; Word assigns one
                new_abs.remove(nsid)
            first_num = num.find(q("w:num"))
            if first_num is not None:
                first_num.addprevious(new_abs)
            else:
                num.append(new_abs)
            new_num = el("w:num", {"w:numId": fresh(used_num)}, el("w:abstractNumId", {"w:val": new_abs_id}))
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
        bullets = self.tt.bullets
        for a in num.findall(q("w:abstractNum")):
            for lvl in a.findall(q("w:lvl")):
                fmt = lvl.find(q("w:numFmt"))
                if fmt is None:
                    continue
                if fmt.get(q("w:val")) != "bullet":
                    # first-level indents as the template's list of the same kind ("(iii)" needs room)
                    ind = lvl.find(f"{q('w:pPr')}/{q('w:ind')}")
                    tind = self.tt.list_ind.get(fmt.get(q("w:val")))
                    heading = lvl.find(q("w:pStyle")) is not None  # heading numbering keeps its own
                    if lvl.get(q("w:ilvl")) == "0" and ind is not None and tind is not None and not heading:
                        ind.getparent().replace(ind, copy.deepcopy(tind))
                    continue
                if not bullets:
                    continue
                glyph, fonts = bullets[int(lvl.get(q("w:ilvl"))) % len(bullets)]
                lvl.find(q("w:lvlText")).set(q("w:val"), glyph)
                old = lvl.find(q("w:rPr"))
                if old is not None:
                    lvl.remove(old)
                rfonts = etree.Element(q("w:rFonts"))
                for k, v in fonts.items():
                    rfonts.set(k, v)
                lvl.append(el("w:rPr", None, rfonts))

    def _style_code_blocks(self) -> None:
        """Code blocks: pandoc's monospace font, set as compactly as the template's table cells.

        The template's *Verbatim Char* stays as it is (it also formats e-mail addresses);
        code runs get their own *Source Code Char* style carrying pandoc's default
        *Verbatim Char* font, and pandoc's token styles are re-based on it.
        """
        styles = self.pkg.xml("word/styles.xml")
        by_id = {st.get(q("w:styleId")): st for st in styles.findall(q("w:style"))}
        code = by_id.get("SourceCode")
        pandoc_char = default_style("VerbatimChar")
        if code is None or pandoc_char is None:
            return
        cell = self.tt.ppr("cell")
        spacing = cell.find(q("w:spacing")) if cell is not None else None
        ppr = code.find(q("w:pPr"))
        if ppr is None:
            ppr = el("w:pPr")
            code.append(ppr)
        if spacing is not None:
            for old in ppr.findall(q("w:spacing")):
                ppr.remove(old)
            ppr.append(copy.deepcopy(spacing))
            ox.sort_ppr(ppr)
        char = copy.deepcopy(pandoc_char)
        char.set(q("w:styleId"), "SourceCodeChar")
        char.find(q("w:name")).set(q("w:val"), "Source Code Char")
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
            new.append(ox.paragraph(self.tt.ppr("Title"), runs_of(("FMTITLE", ""))))

        if fm.authors:
            content: list[etree._Element] = []
            affil_num = {a.key: a.number for a in fm.affiliations}
            notes = self.conv.equal_notes
            corr_mark = self.tt.corresponding_mark
            equal_mark = _superscript_mark(self.conv.equal_mark)
            email_sep = self.names.get("emailsep", "")
            author_sep = self.tt.author_sep or email_sep
            sup, lead, gap = self.tt.rpr("sup"), self.tt.line_lead, self.tt.label_sep
            for i, a in enumerate(fm.authors):
                content += runs_of(("FMAUTHOR", str(i)))
                marks = [str(affil_num.get(k, k)) for k in a.affils]
                if a.corresponding and corr_mark:
                    marks.append(corr_mark)
                if a.equal and equal_mark:
                    marks.append(equal_mark)
                if marks:
                    content.append(ox.run(author_sep.strip().join(marks), sup))
                if i < len(fm.authors) - 1:
                    content.append(ox.run(author_sep))
            for i, aff in sorted(enumerate(fm.affiliations), key=lambda t: t[1].number):
                content += [ox.special_run("w:br"), ox.run(lead), ox.run(str(aff.number), sup)]
                content += runs_of(("FMAFFIL", str(i)))
            corr = [e for a in fm.authors if a.corresponding for e in a.emails]
            others = [e for a in fm.authors if not a.corresponding for e in a.emails]
            for mark, label, emails in ((corr_mark, self.tt.corresponding, corr),
                                        ("", self.names.get("contributing", ""), others)):
                if not emails:
                    continue
                content += [ox.special_run("w:br"), ox.run(lead)]
                if mark:
                    content.append(ox.run(mark, sup))
                if label:
                    content += [ox.run(label), ox.run(gap)]
                for j, e in enumerate(emails):
                    if j:
                        content.append(ox.run(email_sep))
                    content.append(ox.run(e, self.tt.rpr("email")))
            for i, _ in enumerate(notes):  # sn-jnl marks every equal contribution alike
                content += [ox.special_run("w:br"), ox.run(lead)]
                if equal_mark:
                    content.append(ox.run(equal_mark, sup))
                content += runs_of(("FMNOTE", str(i)))
            new.append(ox.paragraph(self.tt.ppr("Author"), content))

        abs_p = found.get(("FMABSTRACT", ""))
        if abs_p is not None:
            new.append(ox.paragraph(self.tt.ppr("AbstractTitle"), [ox.run(self.tt.abstract or self.names.get("abstractname", ""))]))
            cur = abs_p.getnext()
            stop = {found.get(("FMKEYWORDS", "")), end}
            while cur is not None and cur not in stop:
                nxt = cur.getnext()
                if cur.tag == q("w:p"):
                    cur.replace(cur.find(q("w:pPr")), self.tt.ppr("Abstract")) if cur.find(q("w:pPr")) is not None \
                        else cur.insert(0, self.tt.ppr("Abstract"))
                    new.append(cur)
                cur = nxt
        kw = runs_of(("FMKEYWORDS", ""))
        if kw:
            label = self.tt.keywords or self.names.get("keywordname", "")
            new.append(ox.paragraph(self.tt.ppr("keywords"), [ox.run(label, self.tt.rpr("keywords_label")),
                                                                ox.run(self.tt.label_sep), *kw]))

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
            if h.role == "references":
                items = [ox.run(self.tt.references or self.names.get("refname", ""))]
            for c in ox.content_children(p):
                p.remove(c)
            if not h.numbered:
                ox.set_ppr(p, numPr=ox.num_pr(0, 0))
            elif h.appendix and self.appendix_num:
                ox.set_ppr(p, numPr=ox.num_pr(self.appendix_num, h.level - 1))
            content = [ox.run(self.tt.heading_lead)] + items if h.numbered and self.tt.heading_lead else items
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
            props = self.tt.ppr("equation_numbered" if eq.number is not None else "equation_plain")
            content: list[etree._Element] = list(maths)
            content.append(ox.special_run("w:tab"))
            if eq.number is not None:
                opening, closing = self.tt.equation
                if opening:
                    content.append(ox.run(opening))
                if eq.tag:
                    num_runs = [ox.run(eq.number)]
                else:
                    num_runs = ox.field("SEQ equation \\* ARABIC", eq.number)
                content += self._bookmark_wrap(eq.bookmark, num_runs)
                if closing:
                    content.append(ox.run(closing))
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
            raster = images.load_image(path, options, self.tt.image_dpi)
            self._media[key] = (raster, self.pkg.add_media(raster.data, raster.ext))
        return self._media[key]

    def _image_paragraphs(self, panels, role: str) -> list[etree._Element]:
        """One paragraph per image, laid out like the template's pictures (``role``: its paragraph kind)."""
        out = []
        for panel in panels:
            for img in panel.images:
                path = images.resolve(img.source, self.search_dirs)
                raster = None
                if path is None:
                    self.warn(f"image not found: {img.source}")
                else:
                    try:
                        raster, rid = self._raster(path, img.options)
                    except Exception as exc:  # conversion failure: keep going with the file name
                        self.warn(f"cannot convert image {path}: {exc}")
                if raster is None:  # like graphicx's draft mode: the file name stands in for the picture
                    out.append(ox.paragraph(self.tt.ppr(role), [ox.run(img.source)]))
                    continue
                # every figure as wide as the template's, at most the text block
                cx = min(self.tt.image_width, self.tt.text_width)
                cy = int(cx * raster.height_px / max(1, raster.width_px))
                if cy > self.tt.text_height:
                    cx, cy = int(cx * self.tt.text_height / cy), int(self.tt.text_height)
                self.doc_pr_id += 1
                run = _picture(self.tt.drawing, rid, int(cx), cy, self.doc_pr_id, path.name, img.source)
                out.append(ox.paragraph(self.tt.ppr(role), [run]))
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
                paras = self._image_paragraphs([panel], "image_Compact" if multi else "image_CaptionedFigure")
                if idx not in subcaps and panel.bookmark and paras:
                    content = ox.content_children(paras[0])
                    for c in self._bookmark_wrap(panel.bookmark, content):
                        paras[0].append(c)
                new += paras
                if idx in subcaps:
                    label = latex_to_plain(texdefs.subcaption_label().replace("#2", panel.letter), strip=False)
                    runs = [ox.run(label)] + self._content_without_marker(subcaps[idx])
                    new.append(ox.paragraph(self.tt.ppr("subcaption"), self._bookmark_wrap(panel.bookmark, runs)))
            if caption is not None:
                runs = self._seq("figure", fig.number, fig.bookmark)
                runs.append(ox.run(self._caption_parts("figure")[1]))
                runs += self._content_without_marker(caption)
                new.append(ox.paragraph(self.tt.ppr("figure_caption"), runs))
            _replace_block(block, new)

    # ================================================================ tables
    def _style_table(self, tbl: etree._Element, caption_text: str | None = None) -> None:
        """Table, row and cell properties as in the template's tables; cell alignment stays pandoc's."""
        rows = tbl.findall(q("w:tr"))
        header_rows = [r for r in rows if r.find(f"{q('w:trPr')}/{q('w:tblHeader')}") is not None]
        new = copy.deepcopy(self.tt.tbl_pr)
        for old in new.findall(q("w:tblCaption")):
            new.remove(old)
        if caption_text:
            new.append(el("w:tblCaption", {"w:val": caption_text}))
        old = tbl.find(q("w:tblPr"))
        if old is not None:
            tbl.replace(old, new)
        else:
            tbl.insert(0, new)
        for r in rows:
            head = r in header_rows
            trpr = copy.deepcopy(self.tt.header_tr_pr if head else self.tt.body_tr_pr)
            old = r.find(q("w:trPr"))
            if old is not None:
                r.remove(old)
            if trpr is not None:
                r.insert(0, trpr)
        if len(header_rows) > 1:  # the table style only rules off the first row: booktabs' \midrule
            for tc in header_rows[-1].findall(q("w:tc")):
                self._cell_rule(tc, "lightrulewidth")
        for p in tbl.iter(q("w:p")):
            jc = p.find(f"{q('w:pPr')}/{q('w:jc')}")
            props = self.tt.ppr("cell")
            for old in props.findall(q("w:jc")):
                props.remove(old)
            if jc is not None:
                props.append(copy.deepcopy(jc))
            ox.sort_ppr(props)
            old = p.find(q("w:pPr"))
            if old is not None:
                p.replace(old, props)
            else:
                p.insert(0, props)

    def _rule(self, side: str, sz: int) -> etree._Element:
        """A border like the template table's rules, ``sz`` eighths of a point thick."""
        proto = self.tt.tbl_pr.find(f"{q('w:tblBorders')}/{q('w:top')}")
        b = copy.deepcopy(proto) if proto is not None else el("w:top")
        b.tag = q(f"w:{side}")
        b.set(q("w:sz"), str(max(1, round(sz))))
        return b

    def _booktabs_sz(self, rule: str) -> int:
        """Word thickness of a booktabs rule, scaled from the template's top rule (\\heavyrulewidth)."""
        widths = texdefs.booktabs_rules()
        heavy = texdefs.tex_length(widths["heavyrulewidth"], 1.0)
        return self.tt.rule_size * texdefs.tex_length(widths[rule], 1.0) / heavy

    def _cell_rule(self, tc: etree._Element, rule: str) -> None:
        tcpr = tc.find(q("w:tcPr"))
        if tcpr is None:
            tcpr = el("w:tcPr")
            tc.insert(0, tcpr)
        b = tcpr.find(q("w:tcBorders"))
        if b is not None and b.find(q("w:bottom")) is not None:
            return
        if b is None:
            b = el("w:tcBorders")
            # tcBorders comes after tcW/gridSpan/vMerge in CT_TcPr
            after = [c for c in tcpr if etree.QName(c).localname in ("tcW", "gridSpan", "hMerge", "vMerge")]
            if after:
                after[-1].addnext(b)
            else:
                tcpr.insert(0, b)
        b.append(self._rule("bottom", self._booktabs_sz(rule)))

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
                            self._cell_rule(tc, "cmidrulewidth")
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
                    trpr.append(copy.deepcopy(self.tt.header_tr_pr.find(q("w:tblHeader"))))

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
            label, sep = self._caption_parts("table")
            cap_text = f"{label}{tab.number}{sep}{ox.plain_text(cap_runs)}" if tab.number else None
            new: list[etree._Element] = []
            for t in tables:
                self._style_table(t, cap_text)
                handled.add(t)
                new.append(t)
            if tab.panels:
                new += self._image_paragraphs(tab.panels, "image_CaptionedFigure")
            # table notes: compact like the cells, in the class's \\footnotesize relative to the body
            note_size = self._scaled_size("footnotesize")
            for n in notes:
                new.append(ox.paragraph(self.tt.ppr("cell"),
                                        [_resize(r, note_size) for r in self._content_without_marker(n)]))
            if caption is not None:
                runs = self._seq("table", tab.number, tab.bookmark)
                runs.append(ox.run(sep))
                runs += cap_runs
                new.append(ox.paragraph(self.tt.ppr("table_caption"), runs))
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
            fs = texdefs.float_style(self.cls.source if self.cls else "")
            lay = texdefs.alg_layout()
            em = self.tt.body_size.pt
            if caption is not None:
                bold = ox.rpr(bold=fs.caption_bold)
                runs = [ox.run(self.names.get("algorithmname", "") + " ", bold)]
                runs += self._bookmark_wrap(alg.bookmark, ox.field("SEQ algorithm \\* ARABIC", alg.number or "", bold))
                runs.append(ox.run(fs.caption_sep))
                runs += self._content_without_marker(caption)
                # float.sty's ruled style: a heavier rule above the caption, a normal one below it
                new.append(ox.paragraph(self._alg_ppr(borders={"top": fs.top_rule, "bottom": fs.rule}), runs))
            numbered = any(a[2] for a, _ in lines)
            # algorithmicx's list: label box and separation, then \algorithmicindent per block level
            label = texdefs.tex_length(lay.labelwidth_numbered if numbered else lay.labelwidth_plain, em) or 0
            gutter = Pt(label + (texdefs.tex_length(lay.labelsep, em) or 0)).twips
            step = Pt(texdefs.tex_length(lay.indent, em) or 0).twips
            size = self._scaled_size(re.search(r"\\([a-z]+size|small|large)", texdefs.algorithmic().linenumber))
            for n, (args, b) in enumerate(lines):
                _, indent, lineno = args
                left = gutter + step * indent
                runs = []
                if numbered:
                    runs += [ox.run(self._alg_lineno(lineno), size=size), ox.special_run("w:tab")]
                runs += self._content_without_marker(b)
                last = n == len(lines) - 1
                # the number stays in the margin column; the text starts at the block's indent
                ind = {"left": left, "hanging": left} if numbered else {"left": left}
                new.append(ox.paragraph(self._alg_ppr(borders={"bottom": fs.rule} if last else None, ind=ind,
                                                      keep_next=not last), runs))
            _replace_block(block, new)

    def _alg_ppr(self, borders: dict[str, float | None] | None = None, ind: dict | None = None,
                 keep_next: bool = True) -> etree._Element:
        """Algorithm lines: compact like the template's table cells, rules as float.sty draws them."""
        props = self.tt.ppr("cell")
        for tag in ("jc", "ind", "pBdr", "keepNext"):
            for old in props.findall(q(f"w:{tag}")):
                props.remove(old)
        if keep_next:
            props.append(el("w:keepNext"))
        if borders:
            pbdr = el("w:pBdr")
            for side, pt in borders.items():
                if pt:
                    pbdr.append(self._rule(side, pt * 8))  # w:sz is in eighths of a point
            props.append(pbdr)
        if ind:
            props.append(el("w:ind", {f"w:{k}": str(int(v)) for k, v in ind.items()}))
        ox.sort_ppr(props)
        return props

    def _scaled_size(self, command) -> int | None:
        """Half-points for a LaTeX size command, relative to the body size as in the class."""
        name = command.group(1) if hasattr(command, "group") else command
        if not name or self.cls is None:
            return None
        size, normal = self.cls.font_size(name), self.cls.font_size("normalsize")
        if not size or not normal:
            return None
        return round(self.tt.body_size.pt * size / normal * 2)

    # ============================================================== theorems            _replace_block(block, new)

    # ============================================================== theorems
    def _pass_theorems(self) -> None:
        for mk, p in self._marker_paragraphs("THM"):
            k = self._args(mk)[0]
            thm = self.reg.theorems[k]
            block = self._block(p, "THMEND", k)
            note = next((b for name, _, b in self._block_markers(block) if name == "THMNOTE"), None)
            body = [b for b in block[1:-1] if b is not note]
            style = thm.style  # texdefs.ThmStyle, from the class or amsthm
            head_font = ox.rpr(bold=style.head_bold, italic=style.head_italic)
            head = [ox.run(thm.head, head_font)]
            if note is not None:
                head += [ox.run(" " + latex_to_plain(style.note_open, strip=False).lstrip())]
                head += self._content_without_marker(note) + [ox.run(latex_to_plain(style.note_close, strip=False))]
            punct = latex_to_plain(style.punct)
            if punct:
                head.append(ox.run(punct, head_font))
            head.append(ox.run(" "))
            if style.body_italic:
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
                new = [ox.paragraph(self.tt.ppr("body"), head), *body]
            _replace_block(block, new)

    # ========================================================== bibliography
    def _pass_bibliography(self) -> None:
        nb = self.conv.natbib
        for mk, p in self._marker_paragraphs("BIB"):
            n = self._args(mk)[0]
            runs = self._content_without_marker(p)
            label = []
            if nb is not None and nb.numbers and nb.biblabel:
                label = [ox.run(latex_to_plain(nb.biblabel.replace("#1", str(n + 1))) + " ")]
            props = self.tt.ppr("bib_first" if n == 0 else "bib", "bib_first")
            new = ox.paragraph(props, self._bookmark_wrap(f"bibref_{n + 1}", label + runs))
            p.addprevious(new)
            p.getparent().remove(p)

    def _alg_lineno(self, lineno: int) -> str:
        """algorithmicx prints line numbers with ``\\alglinenumber``."""
        fmt = texdefs.algorithmic().linenumber
        return latex_to_plain(fmt.replace("#1", str(lineno))) if lineno else ""

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
            return texdefs.undefined_ref()[0]
        prefix, parens = format_ref(lab, variant, self.conv.cref_names, self.conv.cref_parens)
        opening, closing = self.tt.equation if parens else ("", "")
        number = f"{opening}{lab.text}{closing}"
        return f"{prefix} {number}" if prefix else number

    def _ref_runs(self, i: int, base) -> list[etree._Element]:
        label, variant = self.reg.refs[i]
        lab: Label | None = self.reg.labels.get(label)
        if lab is None:
            self.warn(f"undefined reference: {label}")
            text, bold = texdefs.undefined_ref()
            return [ox.run(text, ox.rpr(bold=bold, base=base))]
        hl = _merge_rpr(self.tt.rpr("ref"), _strip_color(base))
        runs: list[etree._Element] = []
        prefix, parens = format_ref(lab, variant, self.conv.cref_names, self.conv.cref_parens)
        opening, closing = self.tt.equation if parens else ("", "")
        if prefix:
            runs.append(ox.run(f"{prefix} ", hl))
        if opening:
            runs.append(ox.run(opening, hl))
        if lab.field and lab.bookmark:
            switches = "\\w \\h" if lab.kind in ("section", "appendix") else "\\h"
            runs += ox.field(f"REF {lab.bookmark} {switches} \\* MERGEFORMAT", lab.text or texdefs.undefined_ref()[0], hl)
        elif lab.bookmark:
            h = el("w:hyperlink", {"w:anchor": lab.bookmark})
            h.append(ox.run(lab.text or texdefs.undefined_ref()[0], hl))
            runs.append(h)
        else:
            runs.append(ox.run(lab.text or texdefs.undefined_ref()[0], hl))
        if closing:
            runs.append(ox.run(closing, hl))
        return runs

    def _cite_runs(self, i: int, base) -> list[etree._Element]:
        """A natbib citation, punctuated as natbib's options and ``\\setcitestyle`` make it."""
        command, keys, pre, post = self.reg.cites[i]
        nb = self.conv.natbib
        hl = _merge_rpr(self.tt.rpr("cite"), _strip_color(base))
        plain = copy.deepcopy(base) if base is not None else None
        nums = []
        for k in keys:
            if k in self._bibnum:
                nums.append(self._bibnum[k])
            else:
                self.warn(f"undefined citation: {k}")
        pre = latex_to_plain(pre) if pre else ""
        post = latex_to_plain(post) if post else ""

        def punct(name: str) -> str:
            return latex_to_plain(nb.get(name), strip=False) if nb is not None else ""

        opening, closing, sep, cmt = punct("NAT@open"), punct("NAT@close"), punct("NAT@sep").strip(), punct("NAT@cmt")
        aysep = punct("NAT@aysep").strip()
        numbers = nb is not None and nb.numbers
        superscript = nb is not None and nb.superscript
        between = sep + ("" if superscript else " ")

        def link(n: int, text: str) -> etree._Element:
            h = el("w:hyperlink", {"w:anchor": f"bibref_{n}"})
            h.append(ox.run(text, hl))
            return h

        def who_year(n: int) -> tuple[str, str]:
            key = self.reg.bibitems[n - 1]
            who, year = self.reg.bib_labels.get(key, (key, ""))
            return latex_to_plain(who), latex_to_plain(year)

        def wrap(inner: list[etree._Element], paren: bool = True) -> list[etree._Element]:
            if not paren:
                return inner
            head = opening + (f"{pre} " if pre else "")
            tail = (f"{cmt}{post}" if post else "") + closing
            return ([ox.run(head, plain)] if head else []) + inner + ([ox.run(tail, plain)] if tail else [])

        if command in ("citeauthor", "citeyear", "citeyearpar"):
            parts = [link(n, who_year(n)[0 if command == "citeauthor" else 1]) for n in nums]
            return wrap(_joined(parts, between, plain), command == "citeyearpar")

        textual = command in ("citet", "citealt", "textcite")
        paren = command not in ("citealp", "citealt", "citenum")
        if numbers:
            order = sorted(set(nums)) if nb.sort else list(dict.fromkeys(nums))
            spans = _ranges(order) if nb.compress else [(n, n) for n in order]
            dash = latex_to_plain(nb.range_dash)
            if textual:  # Knuth [1]
                parts: list[etree._Element] = []
                for n in nums:
                    parts += [link(n, who_year(n)[0]), ox.run(" ", plain), *wrap([link(n, str(n))], paren)]
                    parts.append(ox.run(between, plain))
                return parts[:-1]
            items = [link(a, str(a) if a == b else f"{a}{dash}{b}") for a, b in spans]
            runs = _joined(items, between, plain)
            if superscript and command not in ("citenum",):
                return [_superscript_run(r) for r in runs]
            return wrap(runs, paren)
        if textual:  # Knuth (1984, p. 5)
            parts = []
            for n in nums:
                who, year = who_year(n)
                parts += [link(n, who), ox.run(" ", plain), *wrap([link(n, year)], paren)]
                parts.append(ox.run(between, plain))
            return parts[:-1]
        items = []
        for n in nums:
            who, year = who_year(n)
            items.append(link(n, f"{who}{aysep} {year}"))
        return wrap(_joined(items, between, plain), paren)

    # ============================================================ properties
    def _header_and_properties(self) -> None:
        fm = self.conv.front
        # dates as the class's \\today writes them; \\date{...} wins
        months, layout = self.cls.today() if self.cls else ([], "")
        today = dt.date.today()
        date = latex_to_plain(fm.date) if fm.date and "\\today" not in fm.date else \
            layout.format(month=months[today.month - 1], day=today.day, year=today.year) if months else ""
        date_re = _date_re(months, layout)
        for name in list(self.pkg.parts):
            if date and date_re and re.fullmatch(r"word/header\d*\.xml", name):
                for t in self.pkg.xml(name).iter(q("w:t")):
                    # the template's header carries a sample date; keep its case and surrounding text
                    t.text = date_re.sub(lambda m: date.upper() if m.group(0).isupper() else date, t.text or "")
        title = latex_to_plain(fm.title)
        authors = [latex_to_plain(a.name) for a in fm.authors]
        core = self.pkg.xml("docProps/core.xml") if self.pkg.has("docProps/core.xml") else None
        if core is not None:
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _set_child(core, "dc:title", title)
            _set_child(core, "dc:creator", "; ".join(authors))
            _set_child(core, "cp:lastModifiedBy", authors[0] if authors else "")
            _set_child(core, "cp:keywords", latex_to_plain(fm.keywords))
            if self.tt.language:
                _set_child(core, "dc:language", self.tt.language)
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
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"


def _date_re(months: list[str], layout: str) -> re.Pattern[str] | None:
    """A date written in the ``\\today`` layout, e.g. "August 23, 2026", in any letter case."""
    if not months or not layout:
        return None
    pattern = re.escape(layout).replace(re.escape("{month}"), "(?:" + "|".join(map(re.escape, months)) + ")")
    pattern = pattern.replace(re.escape("{day}"), r"\d{1,2}").replace(re.escape("{year}"), r"\d{4}")
    return re.compile(pattern, re.IGNORECASE)


def _picture(proto: etree._Element, rid: str, cx: int, cy: int, doc_id: int, name: str, descr: str) -> etree._Element:
    """A copy of the template's picture run showing another image."""
    run = copy.deepcopy(proto)
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    pic = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
    for e in run.iter(f"{_WP}extent", f"{a}ext"):
        e.set("cx", str(cx))
        e.set("cy", str(cy))
    for e in run.iter(f"{_WP}docPr", f"{pic}cNvPr"):
        e.set("id", str(doc_id))
        e.set("name", name)
        e.set("descr", descr)
    for e in run.iter(f"{a}blip"):
        e.set(q("r:embed"), rid)
    return run


def _merge_rpr(proto: etree._Element | None, base: etree._Element | None) -> etree._Element | None:
    """The surrounding run's formatting with the template's link formatting on top."""
    if proto is None:
        return copy.deepcopy(base) if base is not None else None
    merged = copy.deepcopy(base) if base is not None else el("w:rPr")
    for child in proto:
        for old in merged.findall(child.tag):
            merged.remove(old)
        merged.append(copy.deepcopy(child))
    ox.sort_rpr(merged)
    return merged
_BLOCK_MARKERS = {
    "FMTITLE", "FMAUTHOR", "FMAFFIL", "FMNOTE", "FMABSTRACT", "FMKEYWORDS", "FMEND", "HEAD", "EQ", "FIG", "FIGCAP",
    "SUBCAP", "FIGEND", "TAB", "TABCAP", "TABNOTE", "TABEND", "ALG", "ALGCAP", "ALGLINE", "ALGEND", "BIB", "TBLHDR",
    "THM", "THMNOTE", "THMEND",
}


def normalise_markers(root: etree._Element) -> None:
    """Split ``@@NAME...@@`` text inside runs into standalone marker elements."""
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


def format_ref(lab: Label, variant: str, names: dict[str, dict[str, tuple[str, str]]],
               parens_types: set[str]) -> tuple[str, bool]:
    """Type-name prefix (may be empty) and whether the number is parenthesised.

    ``variant`` comes from :func:`sn2docx.latex.transform.replace_refs`: ``ref``,
    ``eq``, ``cref``/``Cref`` (``+`` suffix: plural) or ``bare``. ``names`` are
    cleveref's (after the manuscript's ``\\crefname``s); theorem-like environments
    cleveref does not know use their own title.
    """
    prefix = ""
    style = variant.rstrip("+")
    types = [t for t in (lab.ref_type, lab.kind) if t]
    if style in ("cref", "Cref"):
        plural = variant.endswith("+")
        entry = next((names[t][style] for t in types if style in names.get(t, {})), None)
        if entry is not None:
            prefix = entry[1] if plural else entry[0]
        elif lab.type_name:
            prefix = lab.type_name
    parens = variant == "eq" or (variant != "ref" and any(t in parens_types for t in types))
    return prefix, parens


def _superscript_mark(latex: str | None) -> str:
    """Plain text of a mark the class sets as a superscript (``$^{\\dagger}$``)."""
    if not latex:
        return ""
    m = re.fullmatch(r"\s*\$\s*\^\s*\{?(.*?)\}?\s*\$\s*", latex)
    return latex_to_plain(f"${m.group(1)}$" if m else latex)


def _superscript_run(r: etree._Element) -> etree._Element:
    runs = [r] if r.tag == q("w:r") else list(r.iter(q("w:r")))
    for run in runs:
        _set_rpr(run, ox.rpr(sup=True, base=run.find(q("w:rPr"))))
    return r


def _set_rpr(r: etree._Element, new: etree._Element | None) -> etree._Element:
    """Replace (or insert) a run's properties."""
    old = r.find(q("w:rPr"))
    if old is not None:
        r.remove(old)
    if new is not None:
        r.insert(0, new)
    return r


def _resize(r: etree._Element, size: int | None) -> etree._Element:
    if not size or r.tag != q("w:r"):
        return r
    return _set_rpr(r, ox.rpr(size=size, base=r.find(q("w:rPr"))))


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


def postprocess(pandoc_docx: Path, out: Path, conv: Conversion, template: Package) -> None:
    """Rewrite pandoc's docx into the template layout; problems are reported through logging."""
    pkg = Package.open(pandoc_docx)
    PostProcessor(pkg, conv, template).run()
    pkg.save(out)
