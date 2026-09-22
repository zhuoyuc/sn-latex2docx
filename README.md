# sn-latex2docx

Convert Springer Nature (`sn-jnl`) LaTeX manuscripts into editable Word documents
that follow the house Word template (`src/sn2docx/resources/template.docx`). Equations,
citations, cross-references, figures, tables and document structure are preserved.
Numbers and references come out as live Word fields, not frozen text.

```
uv sync
uv run sn2docx paper/main.tex -o paper.docx --check
```

## Requirements

| Tool | Why | Notes |
|---|---|---|
| Python ≥ 3.10 + [uv](https://docs.astral.sh/uv/) | runs the converter | `uv sync` installs `lxml`, `pymupdf`, `pillow`, `pylatexenc` (and `pytest`, `python-docx` for development) |
| [pandoc](https://pandoc.org) ≥ 3.0 | LaTeX → OOXML for text, math and tables; parses `.bib` files | on `PATH`, or `uv sync --extra pandoc` for the build shipped in `pypandoc_binary` |
| Ghostscript (`gswin64c`, `gs` or MiKTeX's `mgs`) | rasterises `.eps` figures | `epstopdf` is used as a fallback; PDF figures need nothing extra |
| Microsoft Word (Windows, optional) | `scripts/word_check.py` only: field check and PDF/PNG rendering | `uv sync --extra word` |

## Usage

```
sn2docx SOURCE.tex [-o OUT.docx] [--template T.docx] [--figure-width 3.25in|8cm|source]
                   [--date "May 1, 2026"] [--csl style.csl] [--keep-intermediate DIR] [--check] [-q]
```

* `--figure-width` – every figure is scaled to one width (3.25 in, as in the template).
  Use `source` to keep the `\includegraphics[width=…]` widths instead.
* `--date` – text in the page header (`A PREPRINT - <DATE>`). Defaults to `\date{…}`, else today.
* `--csl` – replace the bundled bibliography style.
* `--check` – run the structural validator on the output (see *Verification*).
* `--keep-intermediate DIR` – keep the preprocessed LaTeX and pandoc's raw docx for debugging.

From Python: `from sn2docx.pipeline import convert; convert(Path("main.tex"), Path("out.docx"))`.

## What is converted, and how it looks in Word

| LaTeX (sn-jnl) | Word output (following the template) |
|---|---|
| `\title`, `\author*[1,2]{\fnm{}\sur{}}`, `\affil`, `\email`, `\equalcont` | *Title* and one *Author* paragraph: names with affiliation superscripts, `*` corresponding and `†` equal-contribution marks, affiliation lines, the corresponding e-mail |
| `\abstract`, `\keywords` | *Abstract Title* + *Abstract* paragraphs; bold "Keywords:" line (`;`-separated) |
| `\section` … `\subsubsection` | *Heading 1–3* with the template's automatic numbering (1, 1.1, 1.1.1) |
| `\section*`, `\bmhead` | unnumbered *Heading 1* |
| `appendices` / `\appendix` | lettered headings (A, A.1) from the template's appendix list |
| `equation`, `align`, `gather`, `multline`, `eqnarray`, `\[…\]` | native Word equations; numbered rows get `(SEQ equation)` at a right tab stop; `\nonumber`/`\notag`/starred stay unnumbered; `\tag{}` is kept as static text |
| `figure`, `subfigure`, `\subfloat`, `\subcaptionbox`, `sidewaysfigure` | centred images (PNG/JPEG kept, PDF and EPS rasterised at 300 dpi), `(a)` sub-captions, *Image Caption* "Figure `SEQ figure`: …" |
| `table`, `tabular(*)`, `tabularx`, `longtable`, booktabs, `\cmidrule`, `\multicolumn`, `\multirow`, `\footnotetext`, `threeparttable` | template table style (top/bottom rules, header rule, header rows repeat), `\cmidrule`/`\cline` as rules under exactly the spanned cells, table notes, *Table Caption* "Table `SEQ table`: …" below the table |
| `algorithm` + `algorithmic` (algpseudocode) | "Algorithm `SEQ algorithm`" caption between rules, indented lines with bold keywords, line numbers when `[1]` is given |
| `\ref`, `\eqref`, `\cref`, `\Cref`, `\crefrange`, `\autoref` | `REF` fields to bookmarks (sections use `\w` for full numbers like 3.1.1); theorem/line/sub-figure targets become internal hyperlinks |
| `\cite`, `\citep`, `\citet`, `\citeauthor` + `.bib` | citeproc with a bundled CSL matching the template: `[1]`, `[1–4]`, `Roe et al. [2]`, `[1, p. 5]`; each number links to its entry; author–year styles (`sn-mathphys-ay`, `sn-basic`, `sn-apa`, `sn-chicago`, …) get the author–year CSL |
| `thebibliography` / pasted `.bbl` | parsed directly; numbered or author–year from `\bibitem[…]` labels, with pre/post notes, `\citeauthor`, `\citeyear` |
| `newtheorem` environments, `proof` | heads written from the manuscript's own counters: shared counters, `[section]` numbering ("Definition 3.1"), optional notes, the three sn-jnl theorem styles; □ at the end of proofs |
| `siunitx` (`\qty`, `\SI`, `\num`, `\unit`, lists, ranges, products), `mhchem` (`\ce`) | typeset text or math with correct superscripts/subscripts |
| `\newcommand`, `\def`, `\newenvironment`, `\DeclareMathOperator`, `\input`/`\include` | expanded before conversion |
| `verbatim`, `lstlisting`, `\verb` | monospace code blocks, untouched content |
| footnotes, URLs, `enumerate[label=(\roman*)]`, `itemize`, `quote` | Word footnotes, clickable links, template list glyphs and indents |

The page setup, header (`A PREPRINT - DATE`), footer page number, line numbering and all
styles come from the template, so changing `template.docx` (or passing `--template`)
restyles every output.

## How it works

```
main.tex ─► latex/ (preprocess) ─► pandoc ─► docx/ (post-process) ─► main.docx
             │                                ▲
             └── Registry: numbers, labels ───┘
```

1. **LaTeX stage** (`src/sn2docx/latex/`): inline `\input`, strip comments, expand user
   macros, extract the title block, and walk the body once in document order. The walk
   numbers headings, equations, floats, algorithms and theorems as LaTeX would, binds
   every `\label` to its target, and leaves *markers* (`@@EQ3@@`, `@@REF7@@`, …) where
   Word needs structure pandoc cannot produce. Tables are sanitised for pandoc, siunitx
   and mhchem are rewritten.
2. **pandoc** converts the result to docx with a reference document derived from the
   template (styles, header/footer and section settings, no sample content).
3. **docx stage** (`src/sn2docx/docx/`): replaces markers with the template's constructs:
   author block, SEQ/REF fields and bookmarks, equation paragraphs, figure/table/algorithm
   blocks, bibliography bookmarks (`bibref_N`), heading numbering copied from the template,
   header date and document properties.

## Project layout

```
src/sn2docx/
  cli.py, pipeline.py, pandoc.py, images.py, model.py
  latex/      scan.py (TeX scanning) · source.py (inputs, macros) · frontmatter.py
              transform.py (numbering walker, floats, algorithms, refs) · extras.py (siunitx, mhchem, natbib)
              preprocess.py (orchestration)
  docx/       package.py (zip parts) · ooxml.py (element builders) · postprocess.py · verify.py
  resources/  template.docx (house Word template) · csl/numeric.csl · csl/author-year.csl
templates/springer-nature/   the official sn-jnl LaTeX template (class, bst files, manual);
                             its sn-article.tex is also a test manuscript
examples/
  reference-manuscript/      the Word template's own content written as an sn-jnl manuscript
tests/                       unit tests, end-to-end tests, edge-case fixture
scripts/word_check.py        Word-based field verification and PDF/PNG rendering
```

## Verification

* `uv run pytest` – 51 tests: scanner, front matter, numbering, tables, algorithms,
  siunitx/mhchem, one regression test per fixed bug, and end-to-end conversions of all
  three manuscripts. The reference
  manuscript is compared with `template.docx`: same heading tree, caption numbers,
  SEQ fields, image and table counts, reference-list text.
* `sn2docx … --check` / `sn2docx.docx.verify.inspect()` – no leftover markers; unique
  bookmark names and ids; every `REF` field and internal hyperlink resolves; relationships
  and numbering ids exist.
* `uv run --extra word python scripts/word_check.py out.docx --png pages/` – opens the file
  in Word, updates every field, and reports any cached value that Word computes differently.
  It also exports PDF/PNG pages for visual review. All three example outputs report
  0 mismatches and 0 errors.

## Known limitations

* Display equations follow the template: an inline Word equation followed by a tab and the
  number, so large operators and fractions use Word's inline sizing.
* Figures are rasterised when they are PDF/EPS. SVG is not supported (a placeholder is inserted).
* Multi-panel figures stack panels vertically (one image per line with its sub-caption).
* Cell colours are dropped.
* Custom `cleveref` names (`\crefname`) are not read; the names in `CREF_NAMES`
  (`postprocess.py`) are used.
* Journal-specific commands outside sn-jnl and the common packages above are passed to
  pandoc as-is; check the warnings the CLI prints.

## Libraries versus in-house code

The converter leans on established tools wherever one does the job; the table records
what was checked and why the remaining pieces are written here.

| Job | Done by | Notes |
|---|---|---|
| LaTeX → Word text, OMML math, tables, footnotes, lists | pandoc | the core of the conversion |
| Citation formatting, bibliography | pandoc citeproc + CSL | two bundled CSL styles |
| Reading `.bib` files (names, years for `\citet`) | pandoc (`-t csljson`) | replaced a hand-written BibTeX reader; same parser as citeproc |
| LaTeX → plain text (document properties, tags, citation labels) | [pylatexenc](https://github.com/phfaist/pylatexenc) | replaced a regex/symbol-table converter; accents, symbols, `~` |
| PDF figures → PNG | PyMuPDF | |
| EPS figures → PNG | Ghostscript (`gs`/`gswin64c`/MiKTeX `mgs`), `epstopdf` fallback | Pillow's EPS plugin also calls Ghostscript but cannot find MiKTeX's `mgs` |
| Raster formats, TIFF → PNG | Pillow | |
| XML editing of the .docx parts | lxml | |
| Bundled pandoc (optional) | pypandoc_binary | `uv sync --extra pandoc` |
| Rendering and field checks | Microsoft Word via pywin32 | `scripts/word_check.py` |

Kept in-house, after checking the alternatives:

* **LaTeX scanning** (`latex/scan.py`, ~350 lines). The preprocessor rewrites the source in
  place and must hand everything it does not touch to pandoc byte for byte.
  pylatexenc's `LatexWalker` and TexSoup parse into node trees that need an argument spec for
  every macro and do not round-trip unknown input losslessly; TexSoup also fails on common
  manuscript constructs. A small position-based scanner is simpler and safer here.
* **Macro expansion** (`latex/source.py`). pandoc expands `\newcommand` itself, but only
  after our walker has run, and the walker must see `\ref`/`\label` hidden inside macros.
  plasTeX would do it, but it is a complete TeX engine.
* **siunitx and mhchem** (`latex/extras.py`). There is no Python implementation of either:
  mhchem lives in MathJax/KaTeX (JavaScript), and pandoc's own siunitx support drops `\per`
  exponents and list separators.
* **Package handling** (`docx/package.py`). python-docx covers paragraphs and core
  properties, but not fields, bookmarks, OMML or numbering definitions, so the
  post-processor would still work on raw lxml trees. The tests use python-docx to confirm
  that the output opens.
* **Word field verification** (`scripts/word_check.py`). docx2pdf only exports PDF. The
  script also compares every field's cached value with Word's own recalculation.

