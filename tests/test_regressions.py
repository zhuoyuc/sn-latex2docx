"""Regression tests for bugs found in code review (one test per bug)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from sn2docx.docx.package import q, text_of
from sn2docx.latex.extras import mhchem_to_math, rewrite_textual_citations
from sn2docx.latex.preprocess import _SKIP_RE, _manual_citations
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


def test_citet_single_optional_argument_is_postnote():
    bib = {"k": (["Smith"], "2020")}
    assert rewrite_textual_citations(r"\citet[p.~5]{k}", bib) == r"Smith~\cite[p.~5]{k}"
    assert rewrite_textual_citations(r"\citet[see][p.~5]{k}", bib) == r"Smith~\cite[see][p.~5]{k}"


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


def test_manual_citations_keep_notes_and_command():
    reg = Registry()
    _manual_citations(r"\citep[see][p.~5]{k} \citeauthor{k} \citep[p.~9]{k}", reg)
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
    return convert(ROOT / "tests/fixtures/edge/edge.tex", tmp_path_factory.mktemp("edge") / "edge.docx")


def _paragraphs(path: Path) -> list[etree._Element]:
    with zipfile.ZipFile(path) as z:
        return list(etree.fromstring(z.read("word/document.xml")).iter(q("w:p")))


def test_theorem_heads_and_section_numbering(edge):
    texts = [text_of(p).replace(" ", " ") for p in _paragraphs(edge.output)]
    assert any(t.startswith("Definition 3.1 (Charge). Ions such as") for t in texts)
    assert "See Definition 3.1." in texts
    assert any(t.startswith("Lemma 1. A lemma") for t in texts)


def test_unnumbered_algorithm_keeps_indentation(edge):
    nested = next(p for p in _paragraphs(edge.output) if text_of(p) == "nested")
    ind = nested.find(f"{q('w:pPr')}/{q('w:ind')}")
    assert ind is not None and ind.get(q("w:left")) == "360" and ind.get(q("w:hanging")) is None


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
    assert "(see Knuth, 1984, p. 5)" in joined or "(see Knuth, 1984, p. 5)" in joined
    assert "author only: Lamport; year only: 1984." in joined
