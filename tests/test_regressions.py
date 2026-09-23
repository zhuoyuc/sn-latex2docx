"""Regression tests for bugs found in code review (one test per bug)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from sn2docx.docx.package import q, text_of
from sn2docx.latex.extras import mhchem_to_math
from sn2docx.latex.preprocess import _SKIP_RE, _citations
from sn2docx.latex.scan import strip_comments
from sn2docx.latex.transform import Transformer, _longtable_heads, partial_rules
from sn2docx.model import Registry
from sn2docx.pipeline import convert

ROOT = Path(__file__).resolve().parents[1]


def test_one_row_align_is_valid_display_math():
    reg = Registry()
    out = Transformer(reg).walk(r"\begin{align} a &= b \label{e}\end{align}")
    assert r"\[a  = b\]" in out and reg.equations[0].number == "1"


def test_comment_before_blank_line_keeps_paragraph_break():
    assert strip_comments("foo % c\n\nbar").count("\n\n") == 1


def test_longtable_drops_continuation_head_and_feet():
    body = r"H1\\ \endfirsthead H2\\ \endhead F\\ \endfoot LF\\ \endlastfoot a\\ b\\"
    assert _longtable_heads(body) == r"H1\\  a\\ b\\"


def test_mhchem_letter_subscripts_and_charges():
    assert mhchem_to_math("Cs_xFA_{1-x}PbI3") == r"\mathrm{Cs}_{x}\mathrm{FA}_{1-x}\mathrm{PbI}_{3}"
    assert mhchem_to_math("Na+") == r"\mathrm{Na}^{+}"
    assert mhchem_to_math("I-") == r"\mathrm{I}^{{-}}"
    assert mhchem_to_math("Ca2+") == r"\mathrm{Ca}^{2+}"


def test_subfloat_label_without_caption_gets_a_bookmark():
    reg = Registry()
    Transformer(reg).walk(r"\begin{figure}\subfloat{\includegraphics{a}\label{f:a}}\caption{C}\end{figure}")
    assert reg.figures[0].panels[0].bookmark == "f_a"


def test_vskip_dimension_is_removed():
    assert _SKIP_RE.sub("", r"a\vskip 6pt plus 2pt b \hskip-1em c \kern\parindent d") == "a b  c  d"


def test_citations_keep_notes_and_command():
    reg = Registry()
    _citations(r"\citep[see][p.~5]{k} \citeauthor{k} \citep[p.~9]{k}", reg)
    assert reg.cites == [("citep", ["k"], "see", "p.~5"), ("citeauthor", ["k"], None, None),
                         ("citep", ["k"], None, "p.~9")]


def test_partial_rules_map_to_rows_and_columns():
    body = r"\toprule & \multicolumn{2}{c}{G} \\ \cmidrule(lr){2-3} N & A & B \\ \midrule x & 1 & 2 \\ \bottomrule"
    assert partial_rules(body) == [(0, 2, 3)]


# ---------------------------------------------------------------- end to end
@pytest.fixture(scope="module")
def edge(tmp_path_factory):
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    from tests.test_convert import edge_kit

    tmp = tmp_path_factory.mktemp("edge")
    return convert(edge_kit(tmp / "kit"), tmp / "edge.docx")


def _paragraphs(path: Path) -> list[etree._Element]:
    with zipfile.ZipFile(path) as z:
        return list(etree.fromstring(z.read("word/document.xml")).iter(q("w:p")))


def test_theorem_heads_and_section_numbering(edge):
    texts = [text_of(p).replace("\u00a0", " ") for p in _paragraphs(edge.output)]
    # sn-jnl's thmstylethree sets no punctuation after the head; amsthm's plain style a period
    assert any(t.startswith("Definition 3.1 (Charge) Ions such as") for t in texts)
    assert "See Definition 3.1." in texts
    assert any(t.startswith("Lemma 1. A lemma") for t in texts)


def test_unnumbered_algorithm_keeps_indentation(edge):
    nested = next(p for p in _paragraphs(edge.output) if text_of(p) == "nested")
    ind = nested.find(f"{q('w:pPr')}/{q('w:ind')}")
    # algorithmicx without line numbers: \labelwidth 0.5em + \labelsep 0.5em, then \algorithmicindent
    from sn2docx.latex import texdefs

    lay, em = texdefs.alg_layout(), 12.0  # the template's body size
    width = sum(texdefs.tex_length(x, em) for x in (lay.labelwidth_plain, lay.labelsep, lay.indent))
    assert ind is not None and ind.get(q("w:left")) == str(round(width * 20)) and ind.get(q("w:hanging")) is None


def test_cmidrule_becomes_border_under_spanned_cells_only(edge):
    with zipfile.ZipFile(edge.output) as z:
        doc = etree.fromstring(z.read("word/document.xml"))
    table = next(t for t in doc.iter(q("w:tbl")) if "Group" in text_of(t))
    first_row = table.findall(q("w:tr"))[0]
    bordered = [text_of(tc) for tc in first_row.findall(q("w:tc"))
                if tc.find(f"{q('w:tcPr')}/{q('w:tcBorders')}/{q('w:bottom')}") is not None]
    assert bordered == ["Group"]


def test_manual_citation_notes_rendered(edge):
    joined = " ".join(text_of(p) for p in _paragraphs(edge.output))
    assert "(see Knuth 1984, p. 5)" in joined.replace("\u00a0", " ")
    assert "author only: Lamport; year only: 1984." in joined


def test_svg_figure_embedded(edge):
    texts = [text_of(p) for p in _paragraphs(edge.output)]
    assert "Figure 3: A vector diagram supplied as SVG." in texts
    with zipfile.ZipFile(edge.output) as z:
        assert any(n.startswith("word/media/") for n in z.namelist())


def test_crefname_theorem_names_and_subfloat_letters(edge):
    joined = " ".join(text_of(p).replace("\u00a0", " ") for p in _paragraphs(edge.output))
    assert "Panel 2a. Named: Lemma 1, fig. 1 and Figs. 1 and 3." in joined


def test_parse_crefnames():
    from sn2docx.latex.preprocess import parse_crefnames

    names = parse_crefnames(r"\crefname{figure}{fig.}{figs.}\Crefname{equation}{Equation}{Equations}")
    assert names == {"figure": {"cref": ("fig.", "figs.")}, "equation": {"Cref": ("Equation", "Equations")}}
