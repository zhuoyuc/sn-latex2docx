"""Unit tests of the LaTeX stage: front matter, numbering, labels, tables, algorithms."""

from pathlib import Path

from sn2docx.latex.extras import (
    author_text,
    format_number,
    format_unit,
    mhchem_to_math,
    read_bib_authors,
    rewrite_packages,
    rewrite_textual_citations,
)
from sn2docx.latex.frontmatter import extract_frontmatter
from sn2docx.latex.preprocess import citation_mode, parse_theorems
from sn2docx.latex.source import collect_macros, expand_macros
from sn2docx.latex.transform import (
    Transformer,
    clean_colspec,
    header_rows,
    parse_algorithmic,
    replace_refs,
    sanitize_tabular,
)
from sn2docx.model import MARKER_RE, Registry

ROOT = Path(__file__).resolve().parents[1]


def walk(body: str, theorems=None) -> tuple[str, Registry]:
    reg = Registry()
    out = Transformer(reg, theorems).walk(body)
    return out, reg


# ------------------------------------------------------------------ front matter
def test_sn_frontmatter():
    body = r"""
\title[Short]{Full \emph{title}}
\author*[1,2]{\fnm{First} \sur{Author}}\email{a@x.org}
\author[2]{\fnm{Second} \sur{Author}}\email{b@x.org}\equalcont{Equal.}
\affil*[1]{\orgdiv{Dept}, \orgname{Org}, \orgaddress{\street{Street}, \city{City}, \country{Country}}}
\affil[2]{\orgname{Other}}
\abstract{The abstract.}
\keywords{one, two, three}
\maketitle
Body."""
    fm, _, rest = extract_frontmatter("", body)
    assert fm.title == r"Full \emph{title}" and fm.short_title == "Short"
    assert [a.corresponding for a in fm.authors] == [True, False]
    assert fm.authors[0].affils == ["1", "2"] and fm.authors[0].emails == ["a@x.org"]
    assert fm.authors[1].equal == "Equal."
    assert [a.number for a in fm.affiliations] == [1, 2]
    assert "Street" in fm.affiliations[0].text and "orgname" not in fm.affiliations[0].text
    assert fm.keywords == ["one", "two", "three"]
    assert "\\title" not in rest and "Body." in rest


def test_citation_mode_from_class_options():
    assert citation_mode(["pdflatex", "sn-mathphys-num"], None) == "numeric"
    assert citation_mode(["sn-mathphys-ay"], None) == "author-year"
    assert citation_mode(["sn-basic"], None) == "author-year"
    assert citation_mode(["sn-basic", "Numbered"], None) == "numeric"
    assert citation_mode([], "plainnat") == "numeric"
    assert citation_mode([], "sn-chicago") == "author-year"


# ---------------------------------------------------------------------- macros
def test_macro_expansion_with_args_and_defaults():
    _, table = collect_macros(r"\newcommand{\vect}[1]{\mathbf{#1}}\newcommand{\greet}[1][World]{Hello #1}\def\foo{bar}")
    out = expand_macros(r"$\vect{v}$ \greet \greet[You] \foo \foobar \verb|\foo|", table)
    assert out == r"$\mathbf{v}$ Hello World Hello You bar \foobar \verb|\foo|"


# --------------------------------------------------------------------- headings
def test_heading_numbering_unnumbered_and_appendix():
    body = r"""
\section{One}\label{s1}
\subsection{One.one}\label{s11}
\section*{Unnumbered}
\bmhead{Acknowledgements}
\section{Two}
\begin{appendices}
\section{App}\label{a}
\subsection{AppSub}\label{a1}
\end{appendices}"""
    _, reg = walk(body)
    assert [(h.level, h.numbered, h.appendix, h.number) for h in reg.headings] == [
        (1, True, False, "1"), (2, True, False, "1.1"), (1, False, False, ""), (1, False, False, ""),
        (1, True, False, "2"), (1, True, True, "A"), (2, True, True, "A.1"),
    ]
    assert reg.labels["s11"].text == "1.1" and reg.labels["a"].kind == "appendix"
    assert reg.labels["a1"].text == "A.1"


# -------------------------------------------------------------------- equations
def test_equation_numbering_rows_nonumber_and_tags():
    body = r"""
\begin{equation}a=b\label{e1}\end{equation}
\begin{align}
x &= 1 \nonumber\\
y &= 2 \label{e2}\\
z &= 3
\end{align}
\begin{equation*}u=v\end{equation*}
\[ w \]
\begin{equation}c\tag{A}\label{et}\end{equation}
\begin{align*} p &= q \\ r &= s \end{align*}"""
    out, reg = walk(body)
    assert [e.number for e in reg.equations] == ["1", None, "2", "3", None, None, "A", None]
    assert reg.labels["e2"].text == "2" and reg.labels["e2"].field
    assert reg.labels["et"].text == "A" and not reg.labels["et"].field
    # align* stays a single aligned display; numbered align rows lose their &
    assert r"\begin{aligned}" in out
    assert "y  = 2" in out


# ---------------------------------------------------------------------- figures
def test_figure_with_subfigures():
    body = r"""
\begin{figure}
\begin{subfigure}{0.5\textwidth}\includegraphics{a.png}\caption{First}\label{f:a}\end{subfigure}
\begin{subfigure}{0.5\textwidth}\includegraphics{b.png}\caption{Second}\label{f:b}\end{subfigure}
\caption{Both panels.}\label{f}
\end{figure}"""
    out, reg = walk(body)
    fig = reg.figures[0]
    assert fig.number == "1" and [p.letter for p in fig.panels] == ["a", "b"]
    assert reg.labels["f:b"].text == "1b" and reg.labels["f"].field
    names = [m.group(1) for m in MARKER_RE.finditer(out)]
    assert names == ["FIG", "SUBCAP", "SUBCAP", "FIGCAP", "FIGEND"]


# ----------------------------------------------------------------------- tables
def test_colspec_and_tabular_sanitising():
    assert clean_colspec(r"@{}l*{2}{c}S[table-format=1.2]p{3cm}|X@{}") == "lcccp{3cm}l"
    tab = sanitize_tabular(
        r"\begin{tabular*}{\textwidth}{@{\extracolsep\fill}lcc}\toprule & \multicolumn{2}{@{}c@{}}{H\footnotemark[1]} \\"
        r"\cmidrule(lr){2-3} A & B & C \\ \midrule 1 & 2 & 3 \\ \botrule\end{tabular*}"
    )
    assert tab.startswith(r"\begin{tabular}{lcc}")
    assert r"\bottomrule" in tab and "cmidrule" not in tab and r"\textsuperscript{1}" in tab
    assert r"\multicolumn{2}{c}" in tab
    assert header_rows(tab) == 2


def test_table_env_caption_notes_and_label():
    body = r"""
\begin{table}\caption{Cap}\label{t}
\begin{tabular}{ll}\toprule a & b\\\midrule 1 & 2\\\botrule\end{tabular}
\footnotetext{Source: x.}\footnotetext[1]{Note one.}
\end{table}"""
    out, reg = walk(body)
    assert reg.tables[0].number == "1" and reg.tables[0].notes == 2
    assert reg.labels["t"].kind == "table"
    names = [m.group(1) for m in MARKER_RE.finditer(out)]
    assert names == ["TAB", "TABCAP", "TBLHDR", "TABNOTE", "TABNOTE", "TABEND"]


# ------------------------------------------------------------------- algorithms
def test_algorithmic_indentation_and_numbering():
    src = r"""\Require $x$
\State $y \gets 1$
\If{$x$}\label{l:if}
  \State a
\Else
  \State b
\EndIf"""
    lines = parse_algorithmic(src, 1)
    assert [(i, n) for i, n, _, _ in lines] == [(0, 0), (0, 1), (0, 2), (1, 3), (0, 4), (1, 5), (0, 6)]
    assert lines[2][3] == ["l:if"]
    assert r"\textbf{if}" in lines[2][2] and r"\textbf{then}" in lines[2][2]


# --------------------------------------------------------------------- theorems
def test_theorem_counters_shared():
    envs, lines = parse_theorems(r"\newtheorem{theorem}{Theorem}\newtheorem{lemma}[theorem]{Lemma}\newtheorem*{rem}{Remark}")
    assert envs["lemma"][0] == "theorem" and envs["rem"][0] is None
    out, reg = walk(r"\begin{theorem}\label{t1}A\end{theorem}\begin{lemma}\label{l2}B\end{lemma}", envs)
    assert reg.labels["t1"].text == "1" and reg.labels["l2"].text == "2"
    assert out.count("@@ANCHOR") == 2


# ------------------------------------------------------------------ references
def test_ref_variants():
    reg = Registry()
    out = replace_refs(r"\cref{a,b} \Cref{c} \eqref{d} \crefrange{e}{f} \pageref{g}", reg)
    assert [v for _, v in reg.refs] == ["cref+", "bare", "Cref", "eq", "cref+", "bare"]
    assert "pageref" not in out and out.count("@@REF") == 6


# ---------------------------------------------------------------- siunitx etc.
def test_siunitx_text_and_math():
    assert format_unit(r"\metre\squared\per\second", False) == "m\\textsuperscript{2} s\\textsuperscript{−1}"
    assert format_unit(r"\kilo\gram", True) == r"\mathrm{kg}"
    assert format_number("57655", False) == "57 655"
    assert format_number("1.0e-16", False) == "1.0 × 10\\textsuperscript{−16}"
    out = rewrite_packages(r"\qty{9.4 \pm 0.6}{\giga\pascal}, \qtyrange{1}{2}{\kelvin}, $x=\SI{3}{\metre}$")
    assert out == "9.4 ± 0.6 GPa, 1 K to 2 K, $x=3\\,\\mathrm{m}$"
    assert rewrite_packages(r"\qtylist{0;5;10}{\minute}") == "0 min, 5 min and 10 min"


def test_mhchem():
    assert mhchem_to_math("CaCO3") == r"\mathrm{CaCO}_{3}"
    assert mhchem_to_math("H2SO4") == r"\mathrm{H}_{2}\mathrm{SO}_{4}"
    assert rewrite_packages(r"\ce{H2O}") == r"$\mathrm{H}_{2}\mathrm{O}$"


def test_textual_citations_from_bib():
    bib = read_bib_authors([ROOT / "examples/reference-manuscript/references.bib"])
    assert bib["roe2021"] == (["Roe", "Poe", "Moe"], "2021")
    assert author_text(bib["doe2020"][0]) == "Doe and Roe"
    assert rewrite_textual_citations(r"\citet{roe2021}", bib) == r"Roe et al.~\cite{roe2021}"
