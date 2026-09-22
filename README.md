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
| Python ≥ 3.10 + [uv](https://docs.astral.sh/uv/) | runs the converter | `uv sync` installs `lxml`, `pymupdf`, `pillow` (and `pytest`, `python-docx` for development) |
| [pandoc](https://pandoc.org) ≥ 3.0 | LaTeX → OOXML for text, math and tables | must be on `PATH` |
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
| `table`, `tabular(*)`, `tabularx`, booktabs, `\multicolumn`, `\multirow`, `\footnotetext`, `threeparttable` | template table style (top/bottom rules, header rule, header rows repeat), table notes, *Table Caption* "Table `SEQ table`: …" below the table |
| `algorithm` + `algorithmic` (algpseudocode) | "Algorithm `SEQ algorithm`" caption between rules, numbered and indented lines with bold keywords |
| `\ref`, `\eqref`, `\cref`, `\Cref`, `\crefrange`, `\autoref` | `REF` fields to bookmarks (sections use `\w` for full numbers like 3.1.1); theorem/line/sub-figure targets become internal hyperlinks |
| `\cite`, `\citep`, `\citet`, `\citeauthor` + `.bib` | citeproc with a bundled CSL matching the template: `[1]`, `[1–4]`, `Roe et al. [2]`; each number links to its entry; author–year styles (`sn-mathphys-ay`, `sn-basic`, `sn-apa`, `sn-chicago`, …) get the author–year CSL |
| `thebibliography` / pasted `.bbl` | parsed directly; numbered or author–year from `\bibitem[…]` labels |
| `newtheorem` environments, `proof` | numbered heads (shared counters honoured), □ at the end of proofs |
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
templates/springer-nature/   the official sn-jnl LaTeX template (class, bst files, manual)
examples/
  sn-sample/                 Springer Nature's sample article (sn-article.tex)
  reference-manuscript/      the Word template's own content written as an sn-jnl manuscript
tests/                       unit tests, end-to-end tests, edge-case fixture
scripts/word_check.py        Word-based field verification and PDF/PNG rendering
```

## Verification

* `uv run pytest` – 35 tests: scanner, front matter, numbering, tables, algorithms,
  siunitx/mhchem, and end-to-end conversions of all three manuscripts. The reference
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
* `\cmidrule` is approximated by rules under spanning header cells. Cell colours are dropped.
* Custom `cleveref` names (`\crefname`) are not read; the names in `CREF_NAMES`
  (`postprocess.py`) are used.
* Journal-specific commands outside sn-jnl and the common packages above are passed to
  pandoc as-is; check the warnings the CLI prints.
