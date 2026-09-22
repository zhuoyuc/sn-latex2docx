"""End-to-end conversions of the example manuscripts (requires pandoc)."""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from sn2docx.docx.package import q
from sn2docx.docx.verify import inspect
from sn2docx.pipeline import convert, default_template

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")


@pytest.fixture(scope="session")
def out_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("docx")


@pytest.fixture(scope="session")
def reference(out_dir):
    return convert(ROOT / "examples/reference-manuscript/manuscript.tex", out_dir / "reference.docx", date="August 23, 2026")


@pytest.fixture(scope="session")
def sample(out_dir):
    return convert(ROOT / "examples/sn-sample/sn-article.tex", out_dir / "sn-article.docx")


@pytest.fixture(scope="session")
def edge(out_dir):
    return convert(ROOT / "tests/fixtures/edge/edge.tex", out_dir / "edge.docx")


def _xml(path: Path, part: str = "word/document.xml"):
    with zipfile.ZipFile(path) as z:
        return etree.fromstring(z.read(part))


def _texts(path: Path) -> list[str]:
    body = _xml(path).find(q("w:body"))
    # "~" becomes a no-break space; compare with ordinary spaces
    return ["".join(t.text or "" for t in p.iter(q("w:t"))).replace(" ", " ") for p in body.iter(q("w:p"))]


# ------------------------------------------------------------ structural checks
@pytest.mark.parametrize("name", ["reference", "sample", "edge"])
def test_documents_are_structurally_valid(name, request):
    res = request.getfixturevalue(name)
    summary = inspect(res.output)
    assert summary.problems == []


def test_python_docx_can_open_output(reference, sample):
    docx = pytest.importorskip("docx")
    for res in (reference, sample):
        d = docx.Document(str(res.output))
        assert len(d.paragraphs) > 20


# ------------------------------------------------ reference manuscript vs template
def test_reference_matches_template_structure(reference):
    ours = inspect(reference.output)
    tpl = inspect(default_template())
    # same heading hierarchy and titles (template puts a leading space before numbered titles)
    assert [(s, t.strip()) for s, t in ours.headings] == [(s, t.strip()) for s, t in tpl.headings]
    # same caption numbering
    lead = lambda caps: [re.match(r"(Figure|Table) \d+", c).group(0) for c in caps]  # noqa: E731
    assert lead(ours.captions) == lead(tpl.captions)
    # same set of SEQ fields (numbers of equations, figures, tables)
    seq = lambda fields: sorted(f.split()[1] for f in fields if f.startswith("SEQ"))  # noqa: E731
    assert seq(ours.fields) == seq(tpl.fields)
    assert ours.images == tpl.images == 4
    assert ours.tables == tpl.tables == 2


def test_reference_bibliography_format(reference):
    texts = _texts(reference.output)
    refs = [t for t in texts if re.match(r"\[\d\] ", t)]
    assert refs[0] == (
        "[1] Jane Doe and Richard Roe. A placeholder study of diffusion in layered media. "
        "Journal of Placeholder Results, 12 (3): 101–118, 2020. doi: https://doi.org/10.1000/placeholder.2020.101."
    )
    assert refs[2] == "[3] Ada Poe. Methods for Documents That Do Not Exist. Fictional Press, Nowhere, 2019."
    assert len(refs) == 5
    assert any("a textual one: Roe et al. [2] reported" in t for t in texts)
    assert any("compressed range, [1–4]," in t for t in texts)


def test_reference_cross_references_and_units(reference):
    texts = " ".join(_texts(reference.output))
    assert "Section 2 capitalises the type name, section 3 does not, and (1) prints" in texts
    assert "Combining Eqs. (2)–(3)" in texts
    assert "is given in appendix A" in texts
    assert "8.27 MPa" in texts and "57 655" in texts and "10 min" in texts
    assert "Figure 1: A two-panel figure." in texts


def test_reference_equation_paragraph_layout(reference):
    """Numbered equations: math, tab, (SEQ) with right tab stop; unnumbered: left aligned."""
    body = _xml(reference.output).find(q("w:body"))
    numbered = unnumbered = 0
    for p in body.iter(q("w:p")):
        if p.find(q("m:oMath")) is None or p.getparent().tag != q("w:body"):
            continue
        instr = "".join(i.text for i in p.iter(q("w:instrText")))
        tabs = p.find(f"{q('w:pPr')}/{q('w:tabs')}")
        if "SEQ equation" in instr:
            numbered += 1
            assert tabs is not None and tabs[0].get(q("w:val")) == "right"
        elif p.find(q("w:r")) is not None and p.find(f"{q('w:pPr')}/{q('w:jc')}") is not None:
            unnumbered += 1
    assert numbered == 6
    assert unnumbered == 2


def test_header_date_and_properties(reference):
    hdr = _xml(reference.output, "word/header1.xml")
    assert "".join(t.text or "" for t in hdr.iter(q("w:t"))) == "A PREPRINT - AUGUST 23, 2026"
    core = _xml(reference.output, "docProps/core.xml")
    title = core.find("{http://purl.org/dc/elements/1.1/}title").text
    assert title == "A reference manuscript template for LaTeX to Word conversion"


def test_headings_use_template_numbering(reference):
    body = _xml(reference.output).find(q("w:body"))
    appendix = [p for p in body.iter(q("w:p")) if "Supporting derivation" in "".join(t.text or "" for t in p.iter(q("w:t")))]
    num = appendix[0].find(f"{q('w:pPr')}/{q('w:numPr')}/{q('w:numId')}").get(q("w:val"))
    numbering = _xml(reference.output, "word/numbering.xml")
    abs_id = numbering.find(f"{q('w:num')}[@{q('w:numId')}='{num}']").find(q("w:abstractNumId")).get(q("w:val"))
    lvl0 = numbering.find(f"{q('w:abstractNum')}[@{q('w:abstractNumId')}='{abs_id}']").find(q("w:lvl"))
    assert lvl0.find(q("w:numFmt")).get(q("w:val")) == "upperLetter"


# ------------------------------------------------------------------ SN sample
def test_sample_content(sample):
    assert sample.warnings == []
    texts = _texts(sample.output)
    joined = " ".join(texts)
    assert texts[0] == "Article Title"
    assert "First Author1,2,*, Second Author2,3,†, Third Author1,2,†" in texts[1]
    assert "Table 1: Caption text" in texts and "Figure 1: This is a widefig." in joined
    assert "Algorithm 1 Calculate" in joined
    assert "line 2 of Algorithm 1" in joined
    assert "(refer Subsubsection 3.1.1)" in joined
    assert any(t.startswith("[13] ") for t in texts)
    # verbatim blocks are kept verbatim (no \footnote rewriting inside them)
    assert "\\footnotetext[1]{Example for a first table footnote." in joined


def test_sample_table_headers(sample):
    body = _xml(sample.output).find(q("w:body"))
    tables = list(body.iter(q("w:tbl")))
    heads = [sum(1 for tr in t.findall(q("w:tr")) if tr.find(f"{q('w:trPr')}/{q('w:tblHeader')}") is not None) for t in tables]
    assert heads[:3] == [1, 2, 2]


# -------------------------------------------------------------------- edge cases
def test_edge_cases(edge):
    joined = " ".join(_texts(edge.output))
    assert "See Lemma 1, Corollary 2, equation (⋆), Fig. 1 and panel 1b." in joined
    assert "An undefined reference ??." in joined
    assert "(Knuth, 1984; Lamport, 1994)" in joined and "Lamport (1994)" in joined
    assert "Line 2 of Algorithm 1" in joined
    assert "undefined reference: nope" in edge.warnings
    assert any("image not found" in w for w in edge.warnings)
    foot = _xml(edge.output, "word/footnotes.xml")
    instr = "".join(i.text or "" for i in foot.iter(q("w:instrText")))
    assert "REF sec_macros" in instr
