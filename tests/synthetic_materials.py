"""Synthetic materials for tests and walkthroughs: small PDF, DOCX, HTML, Markdown and LaTeX files
made here, with made-up text and identifiers (10.5555 is a test prefix). Never real papers."""

import ctypes
import hashlib
import io
import math
import zipfile

DOI = "10.5555/scholia.synthetic.001"
ARXIV = "2401.00001"
BODY = ("Minimum wages raise the earnings of low-paid workers in the synthetic panel. "
        "Employment effects are small and not significant in most specifications.")


def pdf(pages, *, scanned=(), size=(612, 792), rotation=0):
    """A PDF whose pages hold [(x, y, font size, text), ...] lines in Helvetica (or the standard font a
    fifth item names, such as Helvetica-Bold); a page number in
    scanned holds only a full-page image (no text), as a scanned page does. With rotation (90, 180
    or 270), each page is stored turned and carries /Rotate, its text drawn turned back: it shows
    upright, size wide and high, with each line where (x, y) says, as a landscape scan or a
    rotated page does."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    width, height = size if rotation in (0, 180) else size[::-1]  # the page as stored
    place = {0: lambda x, y: (1, 0, 0, 1, x, y), 90: lambda x, y: (0, 1, -1, 0, width - y, x),
             180: lambda x, y: (-1, 0, 0, -1, width - x, height - y), 270: lambda x, y: (0, -1, 1, 0, y, height - x)}
    document = pdfium.PdfDocument.new()
    for number, lines in enumerate(pages, start=1):
        page = document.new_page(width, height)
        page.set_rotation(rotation)
        if number in scanned:
            image = pdfium.PdfImage.new(document)
            bitmap = pdfium.PdfBitmap.new_native(40, 40, raw.FPDFBitmap_BGR)
            bitmap.fill_rect((180, 180, 180, 255), 0, 0, 40, 40)
            image.set_bitmap(bitmap)
            image.set_matrix(pdfium.PdfMatrix().scale(width, height))
            page.insert_obj(image)
        for x, y, font_size, text, *font in lines:
            obj = raw.FPDFPageObj_NewTextObj(document, (font[0] if font else "Helvetica").encode(), font_size)
            encoded = (text + "\0").encode("utf-16-le")
            raw.FPDFText_SetText(obj, ctypes.cast(ctypes.c_char_p(encoded), ctypes.POINTER(raw.FPDF_WCHAR)))
            raw.FPDFPageObj_Transform(obj, *place[rotation](x, y))
            raw.FPDFPage_InsertObject(page, obj)
        page.gen_content()
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


TABLE = [("Region", "Workers", "Share"), ("North", "120", "0.40"), ("South", "95", "0.32"), ("East", "81", "0.28")]


def paper_pdf(title="A Synthetic Study of Minimum Wages", doi=DOI, scanned=0, rotation=0):
    """A two-page paper: a title, its DOI, an abstract, a section with two paragraphs, a figure's
    caption, and a table (its caption, then rows of cells set apart in columns), and references on the
    second page (with a DOI of their own that must not be taken); then `scanned` pages holding only
    an image."""
    first = [(72, 720, 20, title), (72, 696, 9, f"doi:{doi}" if doi else "Synthetic Working Paper"),
             (72, 670, 12, "Abstract"),
             (72, 652, 10, "We study minimum wages in a synthetic panel of regions."),
             (72, 640, 10, "The data are made up for testing."),
             (72, 610, 14, "1 Introduction"),
             (72, 590, 10, "Minimum wages raise the earnings of low-paid workers, and"),
             (72, 578, 10, "employment effects are small in most specifications."),
             (72, 552, 10, "A second paragraph begins after a gap and discusses methods."),
             (72, 520, 10, "Figure 1. Earnings by region in the synthetic panel."),
             (72, 496, 10, "Table 1. Employment by region.")]
    first += [(x, 480 - 12 * row, 10, cell) for row, cells in enumerate(TABLE) for x, cell in zip((72, 200, 300), cells)]
    second = [(72, 720, 14, "References"),
              (72, 700, 10, "Smith, J. (2020). An earlier synthetic paper. doi:10.5555/cited.paper.002"),
              (72, 684, 10, "Doe, A. (2019). Another synthetic paper.")]
    return pdf([first, second] + [[]] * scanned, scanned=range(3, 3 + scanned), rotation=rotation)


def scanned_letter(doi=DOI):
    """A one-page scanned letter (S1-20): an image of English lines, a Chinese line and its DOI, drawn
    by AppKit at 300 dpi, with no text layer, so only text recognition reads it. macOS only."""
    from backend.self_test import scanned_pdf

    return scanned_pdf([(72, 720, 16, "A Scanned Letter on Synthetic Wages"), (72, 692, 10, f"doi:{doi}"),
                        (72, 650, 11, "Dear colleague, this synthetic letter was scanned for the walkthrough."),
                        (72, 634, 11, "Minimum wages in the synthetic panel rose by ten percent."),
                        (72, 596, 11, "这是一封用于演示的合成扫描信件。")])


def docx(paragraphs, *, tables=()):
    """A DOCX of [(style or None, text), ...] paragraphs, then tables of rows of cells."""
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def para(style, text):
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f'<w:p>{props}<w:r><w:t xml:space="preserve">{_xml(text)}</w:t></w:r></w:p>'

    body = "".join(para(style, text) for style, text in paragraphs)
    for rows in tables:
        body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{para(None, cell)}</w:tc>" for cell in row) + "</w:tr>"
                                    for row in rows) + "</w:tbl>"
    styles = (f'<w:styles {w}>' + "".join(
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/></w:style>'
        for sid, name in (("Title", "Title"), ("Heading1", "heading 1"), ("Heading2", "heading 2"), ("Caption", "caption")))
        + "</w:styles>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                         'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        archive.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
        archive.writestr("word/styles.xml", f'<?xml version="1.0"?>{styles}')
    return out.getvalue()


def paper_docx(title="Synthetic Evidence on Wages", doi=DOI):
    return docx([("Title", title), (None, f"https://doi.org/{doi}"), ("Heading1", "Introduction"),
                 (None, BODY), ("Caption", "Table 1. Synthetic earnings."), ("Heading1", "References"),
                 (None, "Smith, J. (2020). An earlier synthetic paper.")],
                tables=[[["Region", "Earnings"], ["North", "12.5"], ["South", "11.0"]]])


def paper_html(title="Synthetic Wages in Cities", doi=DOI):
    return f"""<!doctype html><html><head><title>{title}</title><style>p {{ color: red }}</style>
<script>fetch("https://example.org/track")</script></head><body>
<p>DOI: {doi}</p><h2>Introduction</h2><p>{BODY}</p><img src="https://example.org/figure.png" alt="A figure">
<figure><figcaption>Figure 1. Synthetic earnings by city.</figcaption></figure>
<table><tr><th>City</th><th>Earnings</th></tr><tr><td>East</td><td>10.1</td></tr></table>
<h2>References</h2><p>Smith, J. (2020). An earlier synthetic paper.</p></body></html>""".encode()


def paper_markdown(title="Synthetic Notes on Labour Markets", arxiv=ARXIV):
    return f"""# {title}

arXiv:{arxiv}

## Introduction

{BODY}

| Year | Earnings |
| --- | --- |
| 2020 | 10.0 |

## References

- Smith, J. (2020). An earlier synthetic paper.
""".encode()


def paper_latex(title="A Synthetic Model of Wage Floors", doi=DOI):
    return f"""\\documentclass{{article}}
\\title{{{title}}}
\\begin{{document}}
\\maketitle
\\begin{{abstract}}
We model wage floors in a synthetic economy. DOI {doi}
\\end{{abstract}}
\\section{{Introduction}}
{BODY} \\cite{{smith}}

\\begin{{table}}\\caption{{Synthetic parameters}}\\begin{{tabular}}{{ll}} alpha & 0.5 \\\\ beta & 0.9 \\end{{tabular}}\\end{{table}}
\\input{{/etc/hosts}}
\\begin{{thebibliography}}{{9}}
\\bibitem{{smith}} Smith, J. (2020). An earlier synthetic paper.
\\end{{thebibliography}}
\\end{{document}}
""".encode()


# Reference files (S1-14): one of each format, English and Chinese records, made-up 10.5555 DOIs. A
# record names a local file and a file: URL, which are never read or kept.
REF_DOI = "10.5555/scholia.reference.001"  # its lookup's record is retracted in the walkthrough's stand-ins
REF_DOI_ZH = "10.5555/scholia.reference.002"


def references_ris(doi=REF_DOI, title="Synthetic Evidence from Reference Files"):
    return f"""TY  - JOUR
TI  - {title}
AU  - Example, Ana
AU  - Researcher, Bo
PY  - 2022///
T2  - Journal of Synthetic Studies
VL  - 7
IS  - 2
SP  - 11
EP  - 29
DO  - {doi.upper()}
AB  - We study synthetic wage records drawn from a made-up reference file.
KW  - wages
KW  - synthetic data
L1  - file:///etc/passwd
UR  - file:///etc/hosts
LA  - en
ER  -

TY  - JOUR
TI  - 合成数据中的最低工资研究
AU  - 张三
AU  - 李四
PY  - 2021
T2  - 中国合成研究
DO  - {REF_DOI_ZH}
AB  - 本文使用虚构的数据研究最低工资。
LA  - zh
ER  -

TY  - CHAP
TI  - A Chapter without an Identifier
AU  - Writer, Cy
PY  - 2019
T2  - A Synthetic Handbook
ER  -
""".encode()


def references_bibtex():
    return f"""@string{{jss = {{Journal of Synthetic Studies}}}}
@article{{example2020,
  title = {{Caf\\'e Wages and {{S}}ynthetic Workers}},
  author = {{Example, Ana and Bo Researcher and 王五}},
  journal = jss,
  year = 2020,
  doi = {{10.5555/scholia.bibtex.001}},
  file = {{/Users/someone/papers/example.pdf}},
  abstract = {{An abstract with \\emph{{LaTeX}} in it.}}
}}
@misc{{preprint2024,
  title = {{A Synthetic Preprint}},
  author = {{Writer, Cy}},
  eprint = {{{ARXIV}}},
  archivePrefix = {{arXiv}},
  year = {{2024}}
}}
""".encode()


def references_csl_json():
    import json
    return json.dumps([{"type": "article-journal", "title": "Synthetic CSL Record", "author": [{"family": "Roe", "given": "Dee"}],
                        "issued": {"date-parts": [[2018, 5]]}, "DOI": "10.5555/scholia.csl.001", "page": 12,
                        "container-title": "Synthetic Review"}], ensure_ascii=False).encode()


def references_noteexpress():
    return """{Reference Type}: Journal Article
{Title}: 虚构面板数据中的劳动力市场
{Author}: 赵六;钱七
{Journal}: 合成经济研究
{Year}: 2020
{Issue}: 4
{Pages}: 33-51
{Keywords}: 劳动力市场;合成数据
{Abstract}: 本文基于虚构的面板数据。
{DOI}: 10.5555/scholia.noteexpress.001
{Database Provider}: Synthetic

{Reference Type}: 学位论文
{Title}: 合成样本的学位论文
{Author}: 孙八
{Year}: 2019
""".encode()


def references_endnote():
    return """%0 Journal Article
%T Tagged Synthetic Evidence
%A Example, Ana
%A 周九
%D 2017
%J Synthetic Letters
%R 10.5555/scholia.endnote.001
%X An abstract in a tagged file,
continued on a second line.
%> /Users/someone/papers/tagged.pdf
%U https://example.org/synthetic

""".encode()


def _xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def embedding(text, dimensions=1024):
    """A synthetic stand-in for the search model's embedding of text (a query's retrieval instruction
    left out), never a model's: its search tokens hashed into dimensions and normalized, so texts that
    share words are near. For the test-owned helper of tests and walkthroughs (S1-17)."""
    from backend.search_index import tokens

    values = [0.0] * dimensions
    for _, _, token, _ in tokens(text.split("Query:", 1)[-1]):
        values[int(hashlib.sha256(token.encode()).hexdigest(), 16) % dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


# Papers for the search walkthrough (S1-17): English and Chinese, with no identifier.
SEARCH_NOTES = b"""# Wage Floors and Employment

## Findings

Minimum wage increases raised the earnings of low-paid workers in every synthetic region.

Employment fell slightly in the smallest firms, and stayed flat elsewhere in the synthetic panel.

## Methods

The synthetic panel follows 120 regions over ten years, with a wage floor raised in half of them.
"""
CHINESE_NOTES = """# 最低工资与就业笔记

## 研究发现

最低工资上调后，各合成地区低收入工人的收入都有所提高。

小企业的就业略有下降，其他企业的就业基本不变。

## 研究方法

合成面板追踪 120 个地区十年的数据，其中一半地区上调了最低工资。
""".encode()


# S1-18's finding (embedding inputs over the helper's slot): a paper with a Han passage past the slot,
# embedded on its bounded prefix, and a passage of private-use characters (glyphs PDF extraction left
# unmapped) that the stand-in helper refuses (GLYPHS_REFUSED), for the walkthrough.
GLYPHS_REFUSED = "\ue000" * 100
GLYPH_NOTES = ("# Unmapped Glyphs (synthetic)\n\n## Findings\n\n"
               "Minimum wages raised the earnings of low-paid workers; this passage is embedded as it is.\n\n"
               + "最低工资提高了低收入工人的收入" * 130 + "\n\n" + "\ue000" * 120 + "\n").encode()


def helper_tokenizer(path, body, slot, *, extra=0, refuse=None, count=None):
    """The stand-in helper's tokenizer and slot, as llama-server answers them, as (status, JSON), or None
    for a path it leaves to its caller: one token per UTF-8 byte (byte-level BPE at its most spread out;
    `extra` tokens more), or count(text) tokens when a test gives its own count, and an embedding input
    over the slot (its tokens and the one special token the server adds) refused with HTTP 400, as is
    one containing `refuse`."""
    tokens = count or (lambda text: len(text.encode()) + extra)
    if path == "/tokenize":
        return 200, {"tokens": [32] * tokens(body["content"])}
    if path == "/v1/embeddings":
        for text in body["input"]:
            if refuse is not None and refuse in text:
                return 400, {"error": {"code": 400, "type": "invalid_request_error"}}
            if (counted := tokens(text) + 1) > slot:
                return 400, {"error": {"code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": counted,
                                       "n_ctx": slot, "message": f"request ({counted} tokens) exceeds the available"
                                                                 f" context size ({slot} tokens)"}}
    return None
