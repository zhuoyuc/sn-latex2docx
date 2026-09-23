# sn-latex2docx

sn-latex2docx converts a Springer Nature (`sn-jnl`) LaTeX manuscript into a Word document
in the layout of `src/sn2docx/resources/template.docx`. Section, equation, figure, table and
algorithm numbers are Word fields, and cross-references and citations link to their targets.

```
uv sync
uv run sn2docx paper/main.tex paper.docx
```

## Requirements

| Tool | Purpose |
|---|---|
| Python 3.10 or later, [uv](https://docs.astral.sh/uv/) | runs the converter; `uv sync` installs lxml, PyMuPDF, Pillow, pylatexenc, pint and python-docx |
| [pandoc](https://pandoc.org) 3.0 or later | converts text, mathematics and tables; taken from `PATH`, or from `pypandoc_binary` after `uv sync --extra pandoc` |
| TeX Live or MiKTeX | `kpsewhich` locates class and package files, `bibtex` formats the reference list |
| Ghostscript (`gswin64c`, `gs` or MiKTeX `mgs`) | converts EPS figures; `epstopdf` is the fallback |
| Microsoft Word on Windows (optional) | field check and page rendering in `scripts/word_check.py`; `uv sync --extra word` |

## Usage

```
sn2docx SOURCE.tex [OUTPUT.docx]
```

`SOURCE.tex` is the main file of a manuscript folder with the same contents as the
Springer Nature template zip: `sn-jnl.cls`, the `.bst` files, the figures and the `.bib`
files. `OUTPUT.docx` defaults to the source path with a `.docx` suffix. Every run ends with
a structural check of the output and a one-line summary.

Python API:

```python
from pathlib import Path
from sn2docx.pipeline import convert

convert(Path("main.tex"), Path("main.docx"))
```

## Sources of text and formatting

| Output element | Source |
|---|---|
| Caption labels and separators, equation brackets, "Abstract", "Keywords:", "Corresponding author:", author separator, "References", bullet glyphs | `template.docx` |
| Paragraph, run and table properties of titles, author block, abstract, keywords, equations, pictures, captions, table cells and rules, bibliography entries, links and lists | matching paragraphs in `template.docx` |
| Figure width, image resolution, text block, body font size, document language | `template.docx` |
| `\refname`, `\figurename`, `\keywordname`, "Contributing authors:", e-mail separator, `\equalcont` mark, reference style and natbib options per class option, theorem styles | `sn-jnl.cls` |
| Header date | `\date{…}`, otherwise `\today` from `article.cls` |
| Cross-reference names, list conjunctions, parenthesised equation numbers | `cleveref.sty` and the manuscript's `\crefname`/`\Crefname` |
| Citation brackets and separators, range dash, numeric bibliography labels | `natbib.sty` with the class options and `\setcitestyle` |
| Reference list | BibTeX with the manuscript's or the class's `.bst` |
| Algorithm keywords, block structure, comment marker, line numbers | `algorithmicx.sty`, `algpseudocode.sty`, `algcompatible.sty` |
| Algorithm caption rules and indents | `float.sty` ruled style, `algorithm.sty`, algorithmicx list lengths |
| "Algorithm", "Listing", sub-figure labels | `algorithm.sty`, `listings.sty`, `subcaption.sty` and `caption3.sty` |
| Theorem head and body fonts, punctuation | `\newtheoremstyle` in the class, otherwise `amsthm.sty` |
| siunitx phrases, prefixes and powers | `siunitx.sty` |
| Booktabs rule weights | `booktabs.sty`, scaled to the template's table rule |
| Table-note and line-number sizes | `\footnotesize` and `\normalsize` in the class |
| Mark for undefined references | `\@setref` in `latex.ltx` |
| Image file extensions | `\Gin@extensions` in `pdftex.def`; `svg.sty` for `\includesvg` |
| Code font | pandoc's default `reference.docx` |
| Symbols | pandoc in document text, pylatexenc in plain-text fields |
| Unit conversion | pint for TeX units, python-docx for Word units |

`scripts/find_hardcoded.py` reports non-ASCII literals and Unicode escapes under `src/`, and
the test suite runs it.

## Supported LaTeX

| LaTeX | Word |
|---|---|
| `\title`, `\author`, `\affil`, `\email`, `\equalcont` | Title paragraph and one Author paragraph with affiliation, corresponding and equal-contribution marks |
| `\abstract`, `\keywords` | Abstract heading, abstract paragraphs, keywords line |
| `\section` to `\subsubsection` | Heading 1 to 3 with the template's numbering |
| `\section*`, `\bmhead` | unnumbered Heading 1 |
| `appendices`, `\appendix` | lettered headings from the template's appendix list |
| `equation`, `align`, `gather`, `multline`, `eqnarray`, `\[…\]` | Word equations; numbered rows carry a `SEQ equation` field, `\tag` values stay as text |
| `figure`, `subfigure`, `\subfloat`, `\subcaptionbox`, `sidewaysfigure`, `\includesvg` | PNG or JPEG pictures, sub-captions, caption with a `SEQ figure` field |
| `table`, `tabular`, `tabular*`, `tabularx`, `longtable`, booktabs, `\multicolumn`, `\multirow`, `threeparttable` | tables in the template style, repeated header rows, `\cmidrule` and `\cline` under the spanned cells, table notes, caption with a `SEQ table` field |
| `algorithm`, `algorithmic` | caption with a `SEQ algorithm` field, indented and optionally numbered lines |
| `\ref`, `\eqref`, `\cref`, `\Cref`, `\crefrange`, `\autoref` | `REF` fields and internal hyperlinks |
| `\cite`, `\citep`, `\citet`, `\citealp`, `\citeauthor`, `\citeyear` | natbib citations linked to the reference list |
| `thebibliography`, pasted `.bbl` | reference list |
| `\newtheorem`, `proof` | theorem heads with the manuscript's counters and the class's styles |
| siunitx, mhchem | text and mathematics |
| `\newcommand`, `\def`, `\newenvironment`, `\DeclareMathOperator`, `\input`, `\include` | expanded before conversion |
| `verbatim`, `lstlisting`, `\verb` | code blocks |
| footnotes, URLs, `enumerate`, `itemize`, `quote` | footnotes, hyperlinks, lists in the template's list style |

Page setup, header, footer, line numbering and styles are those of `template.docx`.

## Pipeline

```
main.tex ─► latex/ ─► pandoc ─► docx/ ─► main.docx
```

1. `src/sn2docx/latex/` reads the manuscript, expands macros, extracts the title block,
   runs BibTeX and walks the body in document order. The walk numbers headings, equations,
   floats, algorithms and theorems, binds each `\label` to its target and inserts markers
   such as `@@EQ3@@` and `@@REF7@@`.
2. pandoc converts the result to docx with a reference document built from the template.
3. `src/sn2docx/docx/` replaces the markers with template paragraphs, `SEQ` and `REF`
   fields, bookmarks, pictures, tables, algorithm blocks, citations and the reference list,
   and sets the header date and document properties.

## Repository layout

```
src/sn2docx/
  cli.py, pipeline.py, pandoc.py, images.py, model.py
  latex/      scan.py, source.py, frontmatter.py, texdefs.py, bibliography.py,
              transform.py, extras.py, preprocess.py
  docx/       package.py, ooxml.py, template_text.py, postprocess.py, verify.py
  resources/  template.docx
templates/springer-nature/   official sn-jnl template; sn-article.tex is a test manuscript
examples/reference-manuscript/   the content of template.docx as an sn-jnl manuscript
tests/                       unit tests, end-to-end tests, edge-case fixture
scripts/word_check.py        field check and page rendering in Word
scripts/find_hardcoded.py    scan for hard-coded characters in src/
```

## Tests

* `uv run pytest` runs 59 tests: LaTeX scanning, front matter, class and package parsing,
  numbering, tables, algorithms, siunitx, mhchem, BibTeX, regressions, and end-to-end
  conversions of the three manuscripts. The reference manuscript is compared with
  `template.docx` for headings, caption numbers, fields, pictures, tables and reference
  text.
* `sn2docx.docx.verify.inspect()`, called after every conversion, checks markers, bookmark
  names and ids, `REF` targets, hyperlinks, relationships and numbering ids.
* `uv run --extra word python scripts/word_check.py out.docx [--png pages/] [--pdf out.pdf]`
  updates all fields in Word and lists cached values that differ from Word's results.

## Limitations

* The class and `.bst` files are looked up next to the manuscript and in the TeX
  installation.
* Display equations are inline Word equations followed by a tab and the number, as in the
  template, so large operators use inline sizing.
* PDF, EPS and SVG figures are rasterised.
* Panels of multi-panel figures are stacked vertically.
* Table cell colours are dropped.
* Commands from packages outside the list above go to pandoc unchanged; the CLI prints a
  warning for each problem it detects.

## Dependencies

| Task | Library or tool |
|---|---|
| Text, mathematics, tables, footnotes, lists | pandoc |
| Reference list | BibTeX |
| Class and package files | TeX distribution (`kpsewhich`) |
| Plain text from LaTeX | [pylatexenc](https://github.com/phfaist/pylatexenc) |
| TeX units | [pint](https://pint.readthedocs.io) |
| Word units | python-docx |
| PDF and SVG figures | PyMuPDF |
| EPS figures | Ghostscript, `epstopdf` |
| Raster images | Pillow |
| docx XML | lxml |
| Word field check | Microsoft Word via pywin32 |

The LaTeX scanner (`latex/scan.py`), macro expansion (`latex/source.py`), package parsing
(`latex/texdefs.py`), mhchem conversion (`latex/extras.py`) and docx package handling
(`docx/package.py`) are part of this repository.
