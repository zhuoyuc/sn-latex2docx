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
| Python ≥ 3.10 + [uv](https://docs.astral.sh/uv/) | runs the converter | `uv sync` installs `lxml`, `pymupdf`, `pillow`, `pylatexenc`, `pint` (and `pytest`, `python-docx` for development) |
| [pandoc](https://pandoc.org) ≥ 3.0 | LaTeX → OOXML for text, math, symbols and tables | on `PATH`, or `uv sync --extra pandoc` for the build shipped in `pypandoc_binary` |
| A TeX distribution (TeX Live or MiKTeX) | `kpsewhich` finds the package sources the converter reads (cleveref, natbib, amsthm, algorithmicx, siunitx, caption …); `bibtex` formats the reference list | any standard installation; nothing is copied into this repository |
| Ghostscript (`gswin64c`, `gs` or MiKTeX's `mgs`) | rasterises `.eps` figures | `epstopdf` is used as a fallback; PDF and SVG figures need nothing extra |
| Microsoft Word (Windows, optional) | `scripts/word_check.py` only: field check and page renders | `uv sync --extra word` |

## Usage

```
sn2docx SOURCE.tex [-o OUT.docx] [--template T.docx] [--figure-width 3.25in|8cm|source]
                   [--date "May 1, 2026"] [--tex-dir DIR] [--keep-intermediate DIR] [--check] [-q]
```

* `--figure-width` – every figure is scaled to one width (3.25 in, as in the template).
  Use `source` to keep the `\includegraphics[width=…]` widths instead.
* `--date` – text in the page header (`A PREPRINT - <DATE>`). Defaults to `\date{…}`, else today.
* `--tex-dir DIR` – where to find `sn-jnl.cls` and its `.bst` files when they are neither next
  to the manuscript nor installed in the TeX distribution, e.g.
  `--tex-dir templates/springer-nature` for the examples in this repository. Repeatable.
* `--check` – run the structural validator on the output (see *Verification*).
* `--keep-intermediate DIR` – keep the preprocessed LaTeX and pandoc's raw docx for debugging.

From Python: `from sn2docx.pipeline import convert; convert(Path("main.tex"), Path("out.docx"), tex_dirs=(...,))`.

## Where the words and symbols come from

The code holds no symbol tables and no fixed words. `tests/test_convert.py` runs
`scripts/find_hardcoded.py`, which fails on any non-ASCII literal or Unicode escape under
`src/`. Instead:

| Content | Source |
|---|---|
| Caption labels and separators ("Figure 1: "), equation brackets, "Abstract", "Keywords:", "Corresponding author:", author separator, "References", bullet glyphs | read from `template.docx` itself (`docx/template_text.py`) |
| Class names (`\refname`, `\figurename`, `\keywordname`), "Contributing authors:", e-mail separator, `\equalcont` mark, reference style per class option, natbib options, theorem styles `thmstyleone…` | parsed from `sn-jnl.cls` (`latex/texdefs.py`) |
| Cross-reference names and conjunctions ("Figure", "eqs.", " and ", " to "), which types get "(1)" | cleveref.sty for the chosen language and options, then the manuscript's `\crefname`/`\Crefname` |
| Citation brackets, separators, author–year separator, range dash, `[1]` bibliography labels | natbib.sty after the class/manuscript options and `\setcitestyle` |
| Reference list text | BibTeX runs the manuscript's (or the class's) `.bst` on the `.bib` files; the `.bbl` is read like `thebibliography` |
| Algorithm keywords, block structure, comment marker, line-number format | algorithmicx.sty, algpseudocode.sty, algcompatible.sty |
| "Algorithm", "Listing", sub-figure labels "(a) " | algorithm.sty `\ALG@name`, listings.sty `\lstlistingname`, subcaption.sty + caption3.sty |
| Theorem head/body fonts and punctuation | the class's `\newtheoremstyle`, else amsthm's `\th@plain`/`\th@definition`/`\th@remark` |
| siunitx list/range/product phrases, prefixes, powers | siunitx.sty |
| Symbols (`\times`, `\dagger`, `--`, `\,` …) | pandoc (document text) and pylatexenc (plain-text contexts) |
| Length units in `\includegraphics[width=…]` | pint |

## What is converted, and how it looks in Word

| LaTeX (sn-jnl) | Word output (following the template) |
|---|---|
| `\title`, `\author*[1,2]{\fnm{}\sur{}}`, `\affil`, `\email`, `\equalcont` | *Title* and one *Author* paragraph: names with affiliation superscripts, corresponding and equal-contribution marks, affiliation lines, e-mail lines |
| `\abstract`, `\keywords` | *Abstract Title* + *Abstract* paragraphs; the keywords line as written |
| `\section` … `\subsubsection` | *Heading 1–3* with the template's automatic numbering (1, 1.1, 1.1.1) |
| `\section*`, `\bmhead` | unnumbered *Heading 1* |
| `appendices` / `\appendix` | lettered headings (A, A.1) from the template's appendix list |
| `equation`, `align`, `gather`, `multline`, `eqnarray`, `\[…\]` | native Word equations; numbered rows get `(SEQ equation)` at a right tab stop; `\nonumber`/`\notag`/starred stay unnumbered; `\tag{}` is kept as static text |
| `figure`, `subfigure`, `\subfloat`, `\subcaptionbox`, `sidewaysfigure` | centred images, always embedded as PNG or JPEG (PDF, SVG and EPS are rasterised at 300 dpi), sub-captions, *Image Caption* "Figure `SEQ figure`: …" |
| `table`, `tabular(*)`, `tabularx`, `longtable`, booktabs, `\cmidrule`, `\multicolumn`, `\multirow`, `\footnotetext`, `threeparttable` | template table style (top/bottom rules, header rule, header rows repeat), `\cmidrule`/`\cline` as rules under exactly the spanned cells, table notes, *Table Caption* "Table `SEQ table`: …" |
| `algorithm` + `algorithmic` (algpseudocode, algcompatible) | "Algorithm `SEQ algorithm`" caption between rules, indented lines, keywords as the packages print them, line numbers when `[n]` is given |
| `\ref`, `\eqref`, `\cref`, `\Cref`, `\crefrange`, `\autoref` | `REF` fields to bookmarks (sections use `\w` for full numbers like 3.1.1); theorem/line/sub-figure targets become internal hyperlinks |
| `\cite`, `\citep`, `\citet`, `\citealp`, `\citeauthor`, `\citeyear` + `.bib` | BibTeX with the manuscript's or class's style; numeric, superscript or author–year citations punctuated as natbib does, with pre/post notes; each citation links to its entry |
| `thebibliography` / pasted `.bbl` | read directly, with the macros the `.bbl` and the class define |
| `newtheorem` environments, `proof` | heads written from the manuscript's own counters: shared counters, `[section]` numbering ("Definition 3.1"), optional notes, the class's theorem styles |
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
             │   ▲                            ▲   ▲
             │   └ TeX sources (kpsewhich),   │   └ template.docx labels
             │     bibtex                     │
             └── Registry: numbers, labels ───┘
```

1. **LaTeX stage** (`src/sn2docx/latex/`): inline `\input`, strip comments, expand user
   macros, extract the title block, run BibTeX, and walk the body once in document order.
   The walk numbers headings, equations, floats, algorithms and theorems as LaTeX would,
   binds every `\label` to its target, and leaves *markers* (`@@EQ3@@`, `@@REF7@@`, …) where
   Word needs structure pandoc cannot produce.
2. **pandoc** converts the result to docx with a reference document derived from the
   template (styles, header/footer and section settings, no sample content).
3. **docx stage** (`src/sn2docx/docx/`): replaces markers with the template's constructs:
   author block, SEQ/REF fields and bookmarks, equation paragraphs, figure/table/algorithm
   blocks, citations and bibliography bookmarks (`bibref_N`), heading numbering copied from
   the template, header date and document properties.

## Project layout

```
src/sn2docx/
  cli.py, pipeline.py, pandoc.py, images.py, model.py
  latex/      scan.py (TeX scanning) · source.py (inputs, macros) · frontmatter.py
              texdefs.py (reads class and package sources) · bibliography.py (BibTeX, .bbl)
              transform.py (numbering walker, floats, algorithms, refs) · extras.py (siunitx, mhchem)
              preprocess.py (orchestration)
  docx/       package.py (zip parts) · ooxml.py (element builders) · template_text.py
              postprocess.py · verify.py
  resources/  template.docx (house Word template)
templates/springer-nature/   the official sn-jnl LaTeX template (class, bst files, manual);
                             its sn-article.tex is also a test manuscript
examples/
  reference-manuscript/      the Word template's own content written as an sn-jnl manuscript
tests/                       unit tests, end-to-end tests, edge-case fixture
scripts/word_check.py        Word-based field verification and page rendering
scripts/find_hardcoded.py    fails on hard-coded symbols in src/
```

## Verification

* `uv run pytest` – 59 tests: scanner, front matter, class/package parsing, numbering,
  tables, algorithms, siunitx/mhchem, BibTeX, one regression test per fixed bug, end-to-end
  conversions of all three manuscripts, "only PNG/JPEG media", and the hard-coded-content
  scan. The reference manuscript is compared with `template.docx`: same heading tree,
  caption numbers, SEQ fields, image and table counts, reference-list text.
* `sn2docx … --check` / `sn2docx.docx.verify.inspect()` – no leftover markers; unique
  bookmark names and ids; every `REF` field and internal hyperlink resolves; relationships
  and numbering ids exist.
* `uv run --extra word python scripts/word_check.py out.docx [--png pages/] [--pdf out.pdf]` –
  opens the file in Word, updates every field, and reports any cached value that Word
  computes differently; optionally renders the pages for visual review. The example outputs
  report 0 mismatches and 0 errors.

## Known limitations

* The document class and `.bst` files must be findable: next to the manuscript, in the TeX
  installation, or through `--tex-dir`. Without the class, its names, reference style and
  theorem styles are unavailable (the CLI warns).
* Display equations follow the template: an inline Word equation followed by a tab and the
  number, so large operators and fractions use Word's inline sizing.
* PDF, EPS and SVG figures are rasterised at 300 dpi (Word cannot embed them as vectors).
* Multi-panel figures stack panels vertically (one image per line with its sub-caption).
* Cell colours are dropped.
* Journal-specific commands outside sn-jnl and the common packages above are passed to
  pandoc as-is; check the warnings the CLI prints.

## Libraries versus in-house code

| Job | Done by | Notes |
|---|---|---|
| LaTeX → Word text, OMML math, tables, footnotes, lists, symbols | pandoc | the core of the conversion |
| Reference list formatting | BibTeX with the journal's `.bst` | the same output LaTeX would typeset |
| Package and class definitions | the TeX installation (`kpsewhich`) | read, never copied |
| LaTeX → plain text (document properties, tags, labels) | [pylatexenc](https://github.com/phfaist/pylatexenc) | accents, symbols, `~` |
| TeX length units | [pint](https://pint.readthedocs.io) | `pt`, `bp`, `pc`, `dd`, `cc`, `sp` … |
| PDF and SVG figures → PNG | PyMuPDF | |
| EPS figures → PNG | Ghostscript (`gs`/`gswin64c`/MiKTeX `mgs`), `epstopdf` fallback | Pillow's EPS plugin also calls Ghostscript but cannot find MiKTeX's `mgs` |
| Raster formats, TIFF → PNG | Pillow | |
| XML editing of the .docx parts | lxml | |
| Bundled pandoc (optional) | pypandoc_binary | `uv sync --extra pandoc` |
| Rendering and field checks | Microsoft Word via pywin32 | `scripts/word_check.py` |

Kept in-house, after checking the alternatives:

* **LaTeX scanning** (`latex/scan.py`). The preprocessor rewrites the source in place and
  must hand everything it does not touch to pandoc byte for byte. pylatexenc's
  `LatexWalker` and TexSoup parse into node trees that need an argument spec for every
  macro and do not round-trip unknown input losslessly.
* **Macro expansion** (`latex/source.py`). pandoc expands `\newcommand` itself, but only
  after our walker has run, and the walker must see `\ref`/`\label` hidden inside macros.
* **Reading package definitions** (`latex/texdefs.py`). No Python library evaluates `.sty`
  files; the parsers read the specific definitions they need (`\crefname@preamble`,
  `\algdef`, `\newtheoremstyle`, `\DeclareOption` bodies …) and evaluate simple `\if…\fi`
  switches.
* **mhchem** (`latex/extras.py`). There is no Python implementation (it lives in
  MathJax/KaTeX); the formula syntax is rewritten into LaTeX math for pandoc.
* **Package handling** (`docx/package.py`). python-docx does not cover fields, bookmarks,
  OMML or numbering definitions; the tests use it to confirm that the output opens.
