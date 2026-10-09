"""Reading files into passages (slice-1 spec section 7.1; ticket 05), on synthetic files: one paragraph
on one page, at most 2,000 characters split at sentence boundaries, tables and captions their own,
references marked, and each format read from its own structure with nothing fetched or included."""

import io
import struct
import textwrap
import zipfile
import zlib

import pytest

from backend import extraction
from backend.extraction import Passage, extract, identifiers, media_type
import synthetic_materials as synthetic


def kinds(passages):
    return [(p.kind, p.text) for p in passages]


# Passages


def test_a_long_paragraph_splits_at_sentence_boundaries_and_none_is_longer_than_the_limit():
    sentence = "Minimum wages raise earnings in the synthetic panel of regions studied here. "
    text = (sentence * 70).strip()
    [*pieces] = extraction._pieces(text, "paragraph", None, ["Results"], 0, len(text))
    assert len(pieces) >= 3 and all(len(p.text) <= extraction.MAX_PASSAGE for p in pieces)
    assert all(p.text.endswith(".") for p in pieces)  # each ends a sentence
    assert " ".join(p.text for p in pieces) == text and all(p.section_path == ["Results"] for p in pieces)


def test_chinese_text_splits_after_its_full_stop_and_text_without_one_at_a_space():
    chinese = "最低工资提高了合成样本中低收入劳动者的收入。" * 150
    pieces = extraction._pieces(chinese, "paragraph", None, [], 0, 0)
    assert len(pieces) > 1 and all(len(p.text) <= extraction.MAX_PASSAGE and p.text.endswith("。") for p in pieces)
    words = ("word " * 900).strip()
    pieces = extraction._pieces(words, "paragraph", None, [], 0, 0)
    assert all(len(p.text) <= extraction.MAX_PASSAGE for p in pieces) and " ".join(p.text for p in pieces) == words


# PDF


def test_a_pdf_gives_one_paragraph_per_page_with_its_character_range_and_boxes():
    lines_one = [(72, 720, 10, "This paragraph begins on the first page and goes on")]
    lines_two = [(72, 720, 10, "past the page break into the second page.")]
    read = extract(synthetic.pdf([lines_one, lines_two]), extraction.PDF)
    assert [(p.page, p.text) for p in read.passages] == [
        (1, "This paragraph begins on the first page and goes on"), (2, "past the page break into the second page.")]
    first = read.passages[0]
    assert (first.char_start, first.char_end) == (0, len(first.text))  # PDFium's character indices on its page
    [[left, top, right, bottom]] = first.boxes["rects"]  # one line, as fractions of the page from its top left
    assert 0.11 < left < 0.13 and top < bottom and 0.08 < top < 0.1 and right < 1
    assert read.pages == 2 and read.ocr_pages == 0 and read.extractor == "pdf"


def _pdf_leaving_out(line, code=b"~"):
    """A one-page PDF whose line is in Helvetica, its code character mapped (ToUnicode) to a control
    character, which PDFium counts among the page's characters but leaves out of its text."""
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T def 1 begincodespacerange"
            b" <00> <FF> endcodespacerange 1 beginbfchar <" + code.hex().encode() + b"> <0002> endbfchar endcmap"
            b" CMapName currentdict /CMap defineresource pop end end")
    content = b"BT /F1 10 Tf 72 72 Td (" + line + b") Tj ET"
    return _raw_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >>"
                     b" /Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
                     *[b"<< /Length %d >>\nstream\n%s\nendstream" % (len(data), data) for data in (content, cmap)]])


def _raw_pdf(objects):
    """A PDF of these objects, numbered from 1, the first its catalog."""
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % at for at in offsets)
    return bytes(out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))


def test_a_pdf_page_whose_text_leaves_characters_out_keeps_its_character_ranges_and_boxes():
    import pypdfium2 as pdfium

    data = _pdf_leaving_out(b"~~~This paragraph begins~ here and goes on")
    textpage = pdfium.PdfDocument(data)[0].get_textpage()
    assert (textpage.count_chars(), len(textpage.get_text_range())) == (42, 38)  # four characters left out
    [passage] = extract(data, extraction.PDF).passages
    assert passage.text == "This paragraph begins here and goes on"
    assert (passage.char_start, passage.char_end) == (3, 42)  # PDFium's character indices, the left-out ones counted
    [[left, top, right, bottom]] = passage.boxes["rects"]
    assert abs(left - (72 + 3 * 5.84) / 612) < 0.002 and 0.89 < top < bottom < 0.92  # after three tildes' width
    plain = extract(_pdf_leaving_out(b"This paragraph begins here and goes on", code=b"#"), extraction.PDF).passages[0]
    assert abs(right - plain.boxes["rects"][0][2] - 4 * 5.84 / 612) < 0.002  # the line ends four tildes further on


@pytest.mark.parametrize("where", ["leading", "trailing"])
def test_a_pdf_page_whose_text_leaves_out_too_many_characters_at_an_end_is_an_unreadable_file(where):
    line = b"~" * 1500 + b"Text" if where == "leading" else b"Text" + b"~" * 1500  # past Python's recursion limit
    with pytest.raises(extraction.Unreadable) as unreadable:  # pypdfium2 recurses once for each left-out end character
        extract(_pdf_leaving_out(line), extraction.PDF)
    assert unreadable.value.code == "unreadable_file"


def test_a_pdf_page_pdfium_cannot_load_makes_the_file_unreadable_and_has_no_image():
    data = _raw_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 2 >>",  # one page of two
                     b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"])
    with pytest.raises(extraction.Unreadable) as unreadable:
        extract(data, extraction.PDF)
    assert unreadable.value.code == "unreadable_file"
    assert extraction.render_page(data, 1, 1.0)[:4] == b"\x89PNG"
    with pytest.raises(extraction.Unreadable):
        extraction.render_page(data, 2, 1.0)


def test_a_pdf_papers_title_sections_abstract_caption_and_references():
    read = extract(synthetic.paper_pdf(), extraction.PDF)
    assert kinds(read.passages) == [
        ("title", "A Synthetic Study of Minimum Wages"),
        ("paragraph", f"doi:{synthetic.DOI}"),
        ("abstract", "We study minimum wages in a synthetic panel of regions. The data are made up for testing."),
        ("paragraph", "Minimum wages raise the earnings of low-paid workers, and employment effects are small in most"
                      " specifications."),
        ("paragraph", "A second paragraph begins after a gap and discusses methods."),
        ("caption", "Figure 1. Earnings by region in the synthetic panel."),
        ("caption", "Table 1. Employment by region."),
        ("table", "Region | Workers | Share\nNorth | 120 | 0.40\nSouth | 95 | 0.32\nEast | 81 | 0.28"),
        ("reference", "Smith, J. (2020). An earlier synthetic paper. doi:10.5555/cited.paper.002"),
        ("reference", "Doe, A. (2019). Another synthetic paper.")]
    assert read.passages[3].section_path == ["1 Introduction"] and read.passages[8].section_path == ["References"]
    assert len(read.passages[7].boxes["rects"]) == 4  # the table's boxes: one per row


def test_a_pdf_table_is_its_own_passage_and_what_only_looks_like_one_is_not():
    def row(y, *cells, at=(72, 200, 300)):
        return [(x, y, 10, cell) for x, cell in zip(at, cells)]

    page = [(72, 750, 18, "Tables in a Synthetic Paper"), (72, 720, 10, "Text before the table ends here."),
            *row(700, "Year", "Wage"), *row(688, "2020", "10.0"), *row(676, "2021", "10.5"),
            (72, 650, 10, "Text after the table."),
            *row(630, "Alone", "on its line"),  # one row is no table
            *row(600, "Left", "column"), *row(588, "Shifted", "far", at=(72, 420)),  # columns that do not line up
            (72, 560, 14, "References"),
            *row(540, "[1]", "Smith (2020)."), *row(528, "[2]", "Doe (2019).")]  # a reference list holds none
    read = extract(synthetic.pdf([page]), extraction.PDF)
    tables = [p.text for p in read.passages if p.kind == "table"]
    assert tables == ["Year | Wage\n2020 | 10.0\n2021 | 10.5"]
    assert "Text before the table ends here." in [p.text for p in read.passages]
    assert all(p.kind == "reference" for p in read.passages if p.section_path == ["References"])


def test_a_pdf_table_with_long_labels_and_a_wrapped_cell_is_one_passage_and_two_text_columns_are_not():
    def row(y, *cells, at=(72, 330, 430)):
        return [(x, y, 10, cell) for x, cell in zip(at, cells)]

    labels = ["Employment rate of workers aged 25 to 54 years", "Share of jobs paid at the minimum wage floor"]
    assert all(len(label) > 40 for label in labels)
    page = [(72, 750, 18, "Longer Tables in a Synthetic Paper"),
            *row(720, "Measure", "Value"), *row(708, labels[0], "0.81"), *row(696, labels[1], "0.12"),
            (72, 670, 10, "Between the tables."),
            *row(650, "Region", "Workers", "Share"),
            *row(638, "North-eastern coastal districts of the", "120", "0.40"),
            (72, 626, 10, "synthetic panel"),  # the cell above, wrapped
            *row(614, "South", "95", "0.32"),
            (72, 590, 10, "Text set in two columns lines up as well, but neither holds short cells."),
            # Body text lines of 50 characters (lines of 40 or fewer in both columns read as a table).
            *row(570, "Minimum wages raise the earnings of low-paid staff", "employment effects are small in most of the panels",
                 at=(60, 330)),
            *row(558, "in the synthetic panel of regions studied for this", "specifications that the made-up data allow, as the",
                 at=(60, 330)),
            *row(546, "work, and the estimates were made up for the tests", "authors note in the text that follows these tables",
                 at=(60, 330))]
    read = extract(synthetic.pdf([page]), extraction.PDF)
    tables = [p for p in read.passages if p.kind == "table"]
    assert [t.text for t in tables] == [
        f"Measure | Value\n{labels[0]} | 0.81\n{labels[1]} | 0.12",
        "Region | Workers | Share\nNorth-eastern coastal districts of the synthetic panel | 120 | 0.40\nSouth | 95 | 0.32"]
    assert [len(t.boxes["rects"]) for t in tables] == [3, 4]  # a box per line, the wrapped cell's own included
    assert "Between the tables." in [p.text for p in read.passages]
    assert not any("Minimum wages" in t.text for t in tables)  # two columns of body text stay paragraphs


def test_a_same_font_references_heading_ends_a_table_and_its_dois_are_never_looked_up():
    def row(y, *cells, at=(72, 330)):
        return [(x, y, 10, cell) for x, cell in zip(at, cells)]

    page = [(72, 750, 18, "A Paper With a Table"), (72, 720, 10, "A paragraph with no identifier of its own."),
            *row(700, "Measure", "Value"), *row(688, "Employment rate", "0.81"), *row(676, "Coverage", "0.12"),
            (72, 664, 10, "References"),  # the table's font, at its line spacing
            *row(652, "[1]", "Smith, J. (2020). doi:10.5555/only.in.references"),  # lined up with its columns
            *row(640, "[2]", "Doe, A. (2019). Another synthetic paper.")]
    read = extract(synthetic.pdf([page]), extraction.PDF)
    assert [p.text for p in read.passages if p.kind == "table"] == ["Measure | Value\nEmployment rate | 0.81\nCoverage | 0.12"]
    references = [p for p in read.passages if p.section_path == ["References"]]
    assert [p.kind for p in references] == ["reference", "reference"] and "doi:10.5555" in references[0].text
    assert identifiers(read.passages) == []  # a DOI only in the references is never offered for lookup


def test_a_bold_line_under_a_table_is_a_heading_not_a_wrapped_cell():
    def row(y, *cells, at=(72, 330)):
        return [(x, y, 10, cell) for x, cell in zip(at, cells)]

    page = [(72, 750, 18, "A Paper With Notes"),
            *row(700, "Measure", "Value"), *row(688, "Employment rate", "0.81"), *row(676, "Coverage", "0.12"),
            (72, 664, 10, "Notes", "Helvetica-Bold"),  # same size, bold, at line spacing
            *row(652, "Source", "Made up"), *row(640, "Years", "2020 to 2021")]
    tables = [p.text for p in extract(synthetic.pdf([page]), extraction.PDF).passages if p.kind == "table"]
    assert tables[0] == "Measure | Value\nEmployment rate | 0.81\nCoverage | 0.12"
    assert not any("Notes" in table for table in tables)


def test_a_table_of_36_character_ids_is_a_table():
    def row(y, *cells):
        return [(x, y, 10, cell) for x, cell in zip((72, 330), cells)]

    ids = ["0b6f0e7a-3c1d-4f2a-9e57-1a2b3c4d5e6f", "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a",
           "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", "1c2d3e4f-5a6b-4c7d-9e8f-0a1b2c3d4e5f"]
    assert all(len(i) == 36 for i in ids)  # no column of short cells, but no cell over 40 characters
    page = [(72, 750, 18, "A Table of Codes"), *row(720, "Record", "Linked record"),
            *row(708, ids[0], ids[1]), *row(696, ids[2], ids[3])]
    read = extract(synthetic.pdf([page]), extraction.PDF)
    assert [p.text for p in read.passages if p.kind == "table"] == [
        f"Record | Linked record\n{ids[0]} | {ids[1]}\n{ids[2]} | {ids[3]}"]


def test_a_wrapped_cell_in_a_tables_last_row_joins_its_row():
    def row(y, *cells):
        return [(x, y, 10, cell) for x, cell in zip((72, 330, 430), cells)]

    page = [(72, 750, 18, "A Table That Ends Wrapped"),
            *row(650, "Region", "Workers", "Share"), *row(638, "North", "120", "0.40"),
            *row(626, "Western coastal districts of the", "88", "0.30"),
            (72, 614, 10, "synthetic panel"),  # the last row's first cell, wrapped
            (72, 588, 10, "Text after the table, further down.")]
    read = extract(synthetic.pdf([page]), extraction.PDF)
    assert [p.text for p in read.passages if p.kind == "table"] == [
        "Region | Workers | Share\nNorth | 120 | 0.40\nWestern coastal districts of the synthetic panel | 88 | 0.30"]
    assert "Text after the table, further down." in [p.text for p in read.passages]


def pixels(image):
    """A PNG from render_page as rows of darkness (0 white to 255 black), from its unfiltered rows."""
    width, height = struct.unpack(">II", image[16:24])
    channels = 4 if image[25] == 6 else 3
    data, at = b"", 8
    while at < len(image):
        (length,) = struct.unpack(">I", image[at:at + 4])
        data += image[at + 8:at + 8 + length] if image[at + 4:at + 8] == b"IDAT" else b""
        at += 12 + length
    raw, stride = zlib.decompress(data), 1 + width * channels
    return [[255 - min(raw[y * stride + 1 + x * channels:y * stride + 1 + x * channels + 3]) for x in range(width)]
            for y in range(height)]


def ink_on(data, passages):
    """Of page 1 as rendered: the share of its dark pixels inside its passages' boxes, and each box's
    share of dark pixels."""
    dark = pixels(extraction.render_page(data, 1, scale=1.0))
    height, width = len(dark), len(dark[0])
    rects = [[int(left * width), int(top * height), int(right * width), int(bottom * height)]
             for p in passages if p.page == 1 for left, top, right, bottom in p.boxes["rects"]]
    inside = {(x, y) for left, top, right, bottom in rects
              for y in range(top - 1, bottom + 2) for x in range(left - 1, right + 2)}
    ink = {(x, y) for y in range(height) for x in range(width) if dark[y][x] > 128}
    return len(ink & inside) / len(ink), [
        sum((x, y) in ink for y in range(top, bottom + 1) for x in range(left, right + 1))
        / ((bottom - top + 1) * (right - left + 1)) for left, top, right, bottom in rects]


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_a_rotated_page_keeps_its_passages_and_its_boxes_sit_on_its_rendered_text(rotation):
    upright_data, data = synthetic.paper_pdf(), synthetic.paper_pdf(rotation=rotation)  # stored turned, shown upright
    upright, turned = extract(upright_data, extraction.PDF).passages, extract(data, extraction.PDF).passages
    assert kinds(turned) == kinds(upright) and all(p.boxes for p in turned)
    for a, b in zip(upright, turned):  # the same rectangles on the page as it is shown
        assert all(abs(u - v) < 0.01 for ra, rb in zip(a.boxes["rects"], b.boxes["rects"]) for u, v in zip(ra, rb))
    covered, density = ink_on(data, turned)
    # As much of the rendered text lies in the boxes as on the upright page (all but its two
    # headings, which are no passage), and every box is on text.
    assert covered > 0.9 and abs(covered - ink_on(upright_data, upright)[0]) < 0.005
    assert min(density) > 0.03  # a table row's box spans the gaps between its cells


def test_image_only_pages_wait_for_ocr_and_give_no_passage():
    read = extract(synthetic.paper_pdf(scanned=1), extraction.PDF)
    assert (read.pages, read.ocr_pages) == (3, 1) and {p.page for p in read.passages} == {1, 2}


def test_a_damaged_pdf_is_unreadable():
    with pytest.raises(extraction.Unreadable) as error:
        extract(b"%PDF-1.7\nnot really", extraction.PDF)
    assert error.value.code == "unreadable_file"


def test_a_page_renders_as_a_valid_png():
    image = extraction.render_page(synthetic.paper_pdf(), 1, scale=0.5)
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", image[16:24])
    assert (width, height) == (306, 396)
    data, at = b"", 8
    while at < len(image):
        (length,) = struct.unpack(">I", image[at:at + 4])
        tag, body = image[at + 4:at + 8], image[at + 8:at + 8 + length]
        assert struct.unpack(">I", image[at + 8 + length:at + 12 + length])[0] == zlib.crc32(tag + body)
        data += body if tag == b"IDAT" else b""
        at += 12 + length
    channels = 4 if image[25] == 6 else 3
    assert len(zlib.decompress(data)) == height * (1 + width * channels)
    with pytest.raises(IndexError):
        extraction.render_page(synthetic.paper_pdf(), 3)


@pytest.mark.parametrize("size", [(14_400, 14_400), (14_400, 3)])
def test_a_huge_declared_page_renders_within_the_pixel_bound(size):
    small = synthetic.pdf([[(72, 72, 10, "A small file that declares a huge page")]], size=size)
    assert len(small) < 4096  # unbounded, its page would need about 1.4 GB at the viewer's scale
    image = extraction.render_page(small, 1, scale=3.0)
    width, height = struct.unpack(">II", image[16:24])
    assert width * height <= extraction.MAX_PAGE_PIXELS
    assert abs(width / height - size[0] / size[1]) / (size[0] / size[1]) < 0.4  # its shape kept (a sliver rounds up)


# DOCX, HTML, Markdown and LaTeX


def test_a_docx_keeps_only_its_visible_text():
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = ("<w:p><w:r><w:t>Kept </w:t></w:r><w:del><w:r><w:delText>deleted words</w:delText></w:r></w:del>"
            "<w:r><w:instrText>HYPERLINK secret-field</w:instrText></w:r><w:r><w:t>text.</w:t></w:r></w:p>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    read = extract(out.getvalue(), extraction.DOCX)
    assert kinds(read.passages) == [("paragraph", "Kept text.")]


def test_a_docx_part_too_large_once_unpacked_is_refused(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_XML_BYTES", 100)
    with pytest.raises(extraction.Unreadable):
        extract(synthetic.docx([(None, "x" * 500)]), extraction.DOCX)


def test_html_text_only_with_nothing_it_names_kept_or_fetched():
    read = extract(synthetic.paper_html(), extraction.HTML)
    text = " ".join(p.text for p in read.passages)
    assert "fetch(" not in text and "color: red" not in text and "example.org" not in text
    nested = extract(b"<table><tr><td>A</td><td><table><tr><td>inner</td></tr></table></td></tr></table><p>After</p>",
                     extraction.HTML)
    assert kinds(nested.passages) == [("table", "A | inner"), ("paragraph", "After")]


def test_markdown_inline_marks_go_and_names_with_underscores_stay():
    read = extract(b"Some **bold** and *em* and `code` with snake_case_name and a [link](https://example.org).",
                   extraction.MARKDOWN)
    assert kinds(read.passages) == [("paragraph", "Some bold and em and code with snake_case_name and a link.")]


def test_latex_reads_no_other_file_and_no_comment(tmp_path):
    canary = tmp_path / "included.tex"
    canary.write_text("Included-File-Canary")
    source = (f"\\begin{{document}}\nVisible text % a hidden comment\n\\input{{{canary}}}\\include{{{canary}}}\n"
              "\\[ x^2 \\]\n\\end{document}").encode()
    read = extract(source, extraction.LATEX)
    text = " ".join(p.text for p in read.passages)
    assert "Visible text" in text and "Canary" not in text and "hidden comment" not in text


# Identifiers


def test_identifiers_come_from_the_first_pages_outside_references_validated_and_normalized():
    passages = [Passage("paragraph", "See https://doi.org/10.5555/ABC.(1)2. and arXiv:2401.00001v2,", page=1),
                Passage("reference", "Cited: doi:10.5555/cited.one", page=1),
                Passage("paragraph", "Also arXiv:hep-th/9901001 and 10.5555/bad\u0000", page=2),
                Passage("paragraph", "Later doi:10.5555/too.late", page=3)]
    assert identifiers(passages) == [("doi", "10.5555/abc.(1)2"), ("arxiv", "2401.00001"), ("doi", "10.5555/bad"),
                                     ("arxiv", "hep-th/9901001")]  # by passage, DOIs before arXiv IDs
    assert extraction.clean_doi("10.5555/x).") == "10.5555/x" and extraction.clean_doi("10.12/too-short") is None


def test_another_formats_identifiers_come_only_from_its_first_characters_even_inside_a_passage():
    window = extraction.IDENTIFIER_CHARS
    before = Passage("paragraph", "x" * (window - 100))  # the next passage starts 100 characters before the edge
    inside = "doi:10.5555/inside.window"
    straddling, past = "doi:10.5555/across.the.edge", "arXiv:2401.00002 and doi:10.5555/just.past"
    text = f"{inside} {'y' * (96 - len(inside) - 1 - 10)} {straddling} {'z' * 200} {past}"
    assert text.index(straddling) < 100 < text.index(straddling) + len(straddling)  # it crosses the edge
    assert identifiers([before, Passage("paragraph", text)]) == [("doi", "10.5555/inside.window")]
    pdf = [Passage("paragraph", "x" * (window + 1000), page=1), Passage("paragraph", f"{past}", page=2)]
    assert [i for _, i in identifiers(pdf)] == ["10.5555/just.past", "2401.00002"]  # a PDF's window is its pages


@pytest.mark.parametrize("name, data, expected", [
    ("a.pdf", b"%PDF-1.7 ...", extraction.PDF), ("a.PDF", b"%PDF-1.4", extraction.PDF), ("a.pdf", b"hello", None),
    ("a.docx", synthetic.docx([(None, "x")]), extraction.DOCX), ("a.docx", b"PK\x03\x04junk", None),
    ("a.htm", b"<p>", extraction.HTML), ("a.markdown", b"#", extraction.MARKDOWN), ("a.tex", b"\\x", extraction.LATEX),
    ("a.txt", b"text", None), ("noextension", b"%PDF-1.7", None)])
def test_a_file_is_known_by_its_extension_checked_against_its_bytes(name, data, expected):
    assert media_type(name, data) == expected


# DOCX and other XML from outside: no document type, no entity, bounded


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
LAUGHS = ('<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "ha"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
          '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
          f'<w:document {W}><w:body><w:p><w:r><w:t>&c;</w:t></w:r></w:p></w:body></w:document>')
EXTERNAL = ('<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY secret SYSTEM "file:///etc/hosts">]>'
            f'<w:document {W}><w:body><w:p><w:r><w:t>&secret;</w:t></w:r></w:p></w:body></w:document>')


def zipped(parts):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return out.getvalue()


@pytest.mark.parametrize("document", [
    LAUGHS, EXTERNAL, LAUGHS.encode("utf-16"),  # with its byte-order mark
    f'<!doctype x><w:document {W}><w:body/></w:document>',  # in any case
])
def test_a_docx_part_that_declares_a_document_type_or_an_entity_is_refused_unparsed(document, monkeypatch):
    parsed = []
    monkeypatch.setattr(extraction.ElementTree, "fromstring", lambda content: parsed.append(content))
    with pytest.raises(extraction.Unreadable):
        extract(zipped({"word/document.xml": document}), extraction.DOCX)
    assert parsed == []  # the parser never saw it


def test_a_docx_styles_part_is_held_to_the_same_rule():
    plain = f'<w:document {W}><w:body><w:p><w:r><w:t>Text</w:t></w:r></w:p></w:body></w:document>'
    with pytest.raises(extraction.Unreadable):
        extract(zipped({"word/document.xml": plain, "word/styles.xml": EXTERNAL.replace("w:document", "w:styles")}),
                extraction.DOCX)
    assert kinds(extract(zipped({"word/document.xml": plain}), extraction.DOCX).passages) == [("paragraph", "Text")]


@pytest.mark.parametrize("name", ["../outside.xml", "/etc/absolute.xml", "word/../../up.xml", "a\\..\\b.xml"])
def test_a_docx_that_names_a_path_outside_itself_is_refused(name):
    plain = f'<w:document {W}><w:body/></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", plain)
        archive.writestr(zipfile.ZipInfo(name), b"x")
    with pytest.raises(extraction.Unreadable):
        extract(out.getvalue(), extraction.DOCX)


def test_a_long_pdfs_reading_holds_one_page_of_characters_at_a_time():
    import tracemalloc
    words = "The synthetic panel shows minimum wages raise earnings of low paid workers in every region studied"
    page = [(54, 760 - 11.5 * i, 9, f"{i:02d} {words}") for i in range(64)]  # a dense page: 6,400 characters
    data = synthetic.pdf([page] * 24)
    tracemalloc.start()
    try:
        read = extract(data, extraction.PDF)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert read.pages == 24 and sum(len(p.text) for p in read.passages) >= 24 * sum(len(line[3]) for line in page)
    # Held for every page at once, each character's box would come to about 29 MiB here; one page at a time,
    # the reading stays near 4 MiB whatever the length (2,000 such pages: 134 MiB of the process at its peak).
    assert peak < 10 * 2**20, f"{peak / 2**20:.1f} MiB"


def test_a_pdfs_first_pass_holds_its_font_sizes_not_one_entry_per_line(monkeypatch):
    import tracemalloc
    line = {"size": 9.0, "text": "x"}  # a short line, as a highly compressed text layer holds millions of

    def read(document, number, raw, measure=False):  # each page's lines as PDFium would give them
        return ([line] * 2000 if measure else []), (612, 792), False

    monkeypatch.setattr(extraction, "_pdf_read", read)
    data = synthetic.pdf([[]] * 500)  # a million lines in all
    tracemalloc.start()
    try:
        read_whole = extract(data, extraction.PDF)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert read_whole.pages == 500
    assert peak < 2 * 2**20, f"{peak / 2**20:.1f} MiB"  # a (size, length) for each line would be over 60 MiB


# What a reading keeps (MAX_TEXT_CHARS, MAX_BLOCKS and each format's own bound)

LONG = " ".join(f"Sentence {i} of a long paragraph says something new about wages." for i in range(90))


@pytest.mark.parametrize("media", ["html", "markdown", "docx", "latex"])
def test_a_long_paragraphs_pieces_each_take_their_own_range_of_its_source(media):
    wrapped = textwrap.fill(LONG, 70)  # its lines, as an editor wraps them
    if media == "html":
        source, kind = f"<html><body><h1>Title</h1><p>{wrapped}</p></body></html>", extraction.HTML
    elif media == "markdown":
        source, kind = f"# Title\n\nAn introduction.\n\n{wrapped}\n", extraction.MARKDOWN
    elif media == "latex":
        source, kind = f"\\documentclass{{article}}\\begin{{document}}\n\\section{{S}}\nFirst.\n\n{wrapped}\n\\end{{document}}", \
            extraction.LATEX
    else:  # a DOCX's ranges are of its paragraphs' text, each after the one before and a line break
        source, kind = f"An introduction.\n{LONG}", extraction.DOCX
    data = synthetic.docx([(None, "An introduction."), (None, LONG)]) if media == "docx" else source.encode()
    pieces = [p for p in extract(data, kind).passages if "Sentence" in p.text]
    assert len(pieces) >= 2 and " ".join(p.text for p in pieces) == LONG
    assert [p.char_start for p in pieces] == sorted({p.char_start for p in pieces})  # each its own, in order
    for piece in pieces:
        assert extraction._normal(source[piece.char_start:piece.char_end]) == piece.text


@pytest.mark.parametrize("bound", ["MAX_TEXT_CHARS", "MAX_BLOCKS"])
@pytest.mark.parametrize("media", [extraction.PDF, extraction.DOCX, extraction.HTML, extraction.MARKDOWN, extraction.LATEX])
def test_a_reading_past_what_it_may_keep_is_unreadable(monkeypatch, media, bound):
    data = {extraction.PDF: synthetic.paper_pdf(), extraction.DOCX: synthetic.paper_docx(),
            extraction.HTML: synthetic.paper_html(), extraction.MARKDOWN: synthetic.paper_markdown(),
            extraction.LATEX: synthetic.paper_latex()}[media]
    assert extract(data, media).passages  # within the bounds as they are
    monkeypatch.setattr(extraction, bound, 50 if bound == "MAX_TEXT_CHARS" else 2)
    with pytest.raises(extraction.Unreadable) as unreadable:
        extract(data, media)
    assert unreadable.value.code == "unreadable_file"


def _docx_of(body):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    return out.getvalue()


@pytest.mark.parametrize("shape", ["Markdown of newlines", "an HTML paragraph of newlines", "a DOCX of empty paragraphs"])
def test_a_files_shape_holds_no_memory_for_each_of_its_lines(shape):
    import tracemalloc
    data, media, bound = {  # each once held an object for every line or paragraph: 24, 36 and 23 MiB here
        "Markdown of newlines": (b"\n" * 500_000, extraction.MARKDOWN, 4),
        "an HTML paragraph of newlines": (b"<p>" + b"\n" * 1_000_000 + b"x</p>", extraction.HTML, 8),
        "a DOCX of empty paragraphs": (_docx_of("<w:p/>" * 250_000), extraction.DOCX, 8)}[shape]
    tracemalloc.start()
    try:
        extract(data, media)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < bound * 2**20, f"{peak / 2**20:.1f} MiB"


def _peak(read):
    """The memory read() takes at its peak (tracemalloc), and whether it was refused as unreadable."""
    import tracemalloc
    tracemalloc.start()
    try:
        try:
            read()
            refused = False
        except extraction.Unreadable:
            refused = True
        return tracemalloc.get_traced_memory()[1], refused
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("media", ["html", "markdown", "docx", "latex"])
def test_a_block_longer_than_its_bound_is_refused_before_its_runs_and_marks_are_matched(monkeypatch, media):
    words = " ".join(["word"] * 1500)  # 7,500 characters in one paragraph
    data = {"html": f"<p>{words}</p>".encode(), "markdown": words.encode(),
            "docx": synthetic.docx([(None, words)]),
            "latex": f"\\documentclass{{article}}\\begin{{document}}{words}\\end{{document}}".encode()}[media]
    kind = {"html": extraction.HTML, "markdown": extraction.MARKDOWN, "docx": extraction.DOCX, "latex": extraction.LATEX}
    assert extract(data, kind[media]).passages
    monkeypatch.setattr(extraction, "MAX_BLOCK_CHARS", 5000)
    with pytest.raises(extraction.Unreadable):
        extract(data, kind[media])


def test_a_docx_of_nested_tables_is_refused_before_their_copies_of_its_text_grow(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_BUILT_CHARS", 2 * 2**20)
    body = '<w:p><w:r><w:t>' + "x" * 20_000 + '</w:t></w:r></w:p>'
    for _ in range(40):  # each cell's text holds the cells within it: 40 copies, rows of up to 40 cells
        body = f"<w:tbl><w:tr><w:tc>{body}</w:tc></w:tr></w:tbl>"
    peak, refused = _peak(lambda: extract(_docx_of(body), extraction.DOCX))
    assert refused and peak < 8 * 2**20, f"{peak / 2**20:.1f} MiB"  # its rows would come to 16 million characters


@pytest.mark.parametrize("unit", ["--a ", "``a'' ", "!`a ", "~a ", "a&b ", "\\x ", "{a} ", "$a$ ", "% c\n"])
def test_a_latex_file_of_more_marks_than_its_bound_is_refused_before_it_is_parsed(monkeypatch, unit):
    import pylatexenc.latexwalker as walker

    def never(*args, **kwargs):
        raise AssertionError("the file was parsed")

    monkeypatch.setattr(extraction, "MAX_LATEX_MARKS", 1000)
    monkeypatch.setattr(walker, "LatexWalker", never)
    with pytest.raises(extraction.Unreadable):  # each unit is a mark pylatexenc makes a node of, with its text
        extract((unit * 1001).encode(), extraction.LATEX)


def test_a_latex_files_paragraphs_are_found_one_at_a_time(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_BLOCKS", 50)
    peak, refused = _peak(lambda: extract(("a\n\n" * 100_000).encode(), extraction.LATEX))
    assert refused and peak < 4 * 2**20, f"{peak / 2**20:.1f} MiB"  # every blank line found first: 14 MiB


def test_a_latex_title_without_maketitle_is_counted_and_split_as_any_block(monkeypatch):
    title = " ".join(f"Word{i}" for i in range(500))  # about 3,400 characters
    source = f"\\documentclass{{article}}\\title{{{title}}}\\begin{{document}}Text.\\end{{document}}".encode()
    pieces = [p for p in extract(source, extraction.LATEX).passages if p.kind == "title"]
    assert len(pieces) == 2 and all(len(p.text) <= extraction.MAX_PASSAGE for p in pieces)
    assert " ".join(p.text for p in pieces) == title
    monkeypatch.setattr(extraction, "MAX_TEXT_CHARS", 1000)
    with pytest.raises(extraction.Unreadable):
        extract(source, extraction.LATEX)


def test_a_markdown_tables_cells_are_counted_before_its_rows_are_split():
    header = "|" + "a|" * 300_000  # more cells than a reading's blocks, in one line
    peak, refused = _peak(lambda: extract(f"{header}\n|--|--|\n".encode(), extraction.MARKDOWN))
    assert refused and peak < 4 * 2**20, f"{peak / 2**20:.1f} MiB"


def test_an_unfinished_markdown_fence_runs_to_the_end_of_the_file():
    source = "```\n" + "x" * 5000
    pieces = extract(source.encode(), extraction.MARKDOWN).passages
    assert len(pieces) == 3 and "".join(p.text for p in pieces) == "x" * 5000
    assert [p.char_start for p in pieces] == sorted({p.char_start for p in pieces}) and pieces[-1].char_end == len(source)
    assert all(source[p.char_start:p.char_end].strip().strip("`").strip() == p.text for p in pieces)


@pytest.mark.parametrize("attributes", [1000, 2000])
def test_an_html_tag_of_too_many_attributes_is_refused_before_the_parser_lists_them(monkeypatch, attributes):
    from html.parser import HTMLParser
    parsed = []
    real = HTMLParser.parse_starttag
    monkeypatch.setattr(HTMLParser, "parse_starttag", lambda self, i: parsed.append(i) or real(self, i))
    source = ("<html><body><p" + "".join(f" a{n}" for n in range(attributes)) + ">Text.</p></body></html>").encode()
    if attributes <= extraction.MAX_TAG_ATTRIBUTES:
        assert [p.text for p in extract(source, extraction.HTML).passages] == ["Text."]
    else:
        with pytest.raises(extraction.Unreadable):
            extract(source, extraction.HTML)
        assert source.index(b"<p") not in parsed  # refused before the parser listed that tag's attributes


def test_a_docx_of_more_styles_than_its_bound_is_refused_not_read_with_some_left_out(monkeypatch):
    data = synthetic.paper_docx()  # four styles: title, two headings and caption
    assert extract(data, extraction.DOCX).passages
    monkeypatch.setattr(extraction, "MAX_STYLES", 3)
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.DOCX)


@pytest.mark.parametrize("shape", ["nested past the depth bound", "a tag past the token bound"])
def test_a_docx_part_too_deep_or_with_too_long_a_tag_is_refused_as_it_is_read(shape):
    if shape == "nested past the depth bound":
        body = "<w:p>" * 1001 + "</w:p>" * 1001
    else:  # its attributes would be made whole once its tag ended
        body = "<w:p " + " ".join(f'a{n}="1"' for n in range(60_000)) + "/>"
    peak, refused = _peak(lambda: extract(_docx_of(body), extraction.DOCX))
    assert refused and peak < 8 * 2**20, f"{peak / 2**20:.1f} MiB"


def test_a_pdfs_font_sizes_are_counted_in_at_most_a_few_thousand_buckets(monkeypatch):
    lines = [{"size": 10.0 + n, "text": "x"} for n in range(10_000)]  # a size of its own for every line
    monkeypatch.setattr(extraction, "_pdf_read", lambda document, number, raw, measure=False: (
        (lines if measure else []), (612, 792), False))
    held = []
    real = extraction._body_size
    monkeypatch.setattr(extraction, "_body_size", lambda weights: held.append(len(weights)) or real(weights))
    extract(synthetic.pdf([[]]), extraction.PDF)
    assert held == [1991]  # 10 to 1,999 points, and 2,000 for every larger size


def test_a_markdown_line_past_the_block_bound_is_refused_before_it_is_copied_or_transformed():
    data = b"a " * 4 * 2**20  # 8 MiB in one nonblank line
    peak, refused = _peak(lambda: extract(data, extraction.MARKDOWN))
    assert refused and peak < 3 * len(data), f"{peak / 2**20:.1f} MiB"  # its text decoded, no more: 91 MiB before


def test_a_latex_file_of_more_paragraph_breaks_than_blocks_is_refused_before_it_is_parsed(monkeypatch):
    import pylatexenc.latexwalker as walker

    def never(*args, **kwargs):
        raise AssertionError("the file was parsed")

    extract(b"x", extraction.LATEX)  # pylatexenc imported before memory is measured
    monkeypatch.setattr(walker, "LatexWalker", never)
    data = b"\n\n" * (extraction.MAX_BLOCKS + 1)  # no mark at all, and a break for every two characters
    peak, refused = _peak(lambda: extract(data, extraction.LATEX))
    assert refused and peak < 4 * 2**20, f"{peak / 2**20:.1f} MiB"


def test_a_latex_files_breaks_are_counted_as_blocks_as_they_are_found(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_BLOCKS", 1000)
    data = b"a\n\n" * 600  # 600 breaks pass the count before parsing; with their 600 paragraphs, 1,200 blocks
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.LATEX)
    monkeypatch.setattr(extraction, "MAX_BLOCKS", 1200)
    assert len(extract(data, extraction.LATEX).passages) == 600


def test_a_pdf_page_of_no_width_or_height_is_unreadable_not_an_error(monkeypatch):
    import pypdfium2
    real = pypdfium2.PdfPage.get_size
    monkeypatch.setattr(pypdfium2.PdfPage, "get_size", lambda page: (0.0, real(page)[1]))  # as a page may declare
    with pytest.raises(extraction.Unreadable):
        extract(synthetic.paper_pdf(), extraction.PDF)


@pytest.mark.parametrize("media", ["markdown", "html", "docx", "latex"])
def test_a_long_heading_over_many_paragraphs_is_cut_and_its_copies_counted(monkeypatch, media):
    heading = " ".join(["Heading"] * 512)  # 4,095 characters, carried by each of 5,000 passages under it
    data = {"markdown": (f"## {heading}\n\n" + "p\n\n" * 5000).encode(),
            "html": (f"<h2>{heading}</h2>" + "<p>p</p>" * 5000).encode(),
            "docx": synthetic.docx([("Heading2", heading)] + [(None, "p")] * 5000),
            "latex": (f"\\documentclass{{article}}\\begin{{document}}\\section{{{heading}}}\n\n"
                      + "p\n\n" * 5000 + "\\end{document}").encode()}[media]
    kind = {"markdown": extraction.MARKDOWN, "html": extraction.HTML, "docx": extraction.DOCX, "latex": extraction.LATEX}
    passages = extract(data, kind[media]).passages
    assert len(passages) == 5000 and {len(p.section_path[0]) for p in passages} == {extraction.MAX_HEADING_CHARS}
    assert heading.startswith(passages[0].section_path[0])  # cut at the bound
    monkeypatch.setattr(extraction, "MAX_BUILT_CHARS", 2**20)  # 5,000 copies of a 500-character path: 2.5 Mi
    with pytest.raises(extraction.Unreadable):
        extract(data, kind[media])


def _docx_document(xml):
    """A DOCX whose document part is exactly xml."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return out.getvalue()


W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
KEPT = '<w:p><w:r><w:t>Kept</w:t></w:r></w:p>'


@pytest.mark.parametrize("cut", ["its body and document left open", "inside another paragraph",
                                 "inside an unfinished comment", "at a length past one piece"])
def test_a_docx_part_cut_short_is_unreadable_not_read_as_far_as_it_goes(cut):
    xml = {"its body and document left open": f"<w:document {W_NS}><w:body>{KEPT}",
           "inside another paragraph": f"<w:document {W_NS}><w:body>{KEPT}<w:p><w:r><w:t>Cu",
           "inside an unfinished comment": f"<w:document {W_NS}><w:body>{KEPT}<!-- unfinished",
           "at a length past one piece": f"<w:document {W_NS}><w:body>{KEPT * 2000}<w:p>"}[cut]
    assert extract(_docx_document(f"<w:document {W_NS}><w:body>{KEPT}</w:body></w:document>"),
                   extraction.DOCX).passages[0].text == "Kept"
    with pytest.raises(extraction.Unreadable):
        extract(_docx_document(xml), extraction.DOCX)


@pytest.mark.parametrize("shape", ["distinct names", "a long namespace"])
def test_a_docx_parts_names_are_bounded_as_the_parser_reports_them(shape):
    if shape == "distinct names":  # each an element Scholia does not read, each kept by the parser
        body = "".join(f"<n{i}/>" for i in range(20_000))
    else:  # every name in it would carry its 8,192 characters
        body = f'<x:a xmlns:x="urn:{"u" * 8192}">' + "".join(f"<x:n{i}/>" for i in range(2000)) + "</x:a>"
    data = _docx_document(f"<w:document {W_NS}><w:body>{KEPT}{body}</w:body></w:document>")
    peak, refused = _peak(lambda: extract(data, extraction.DOCX))
    assert refused and peak < 3 * 2**20, f"{peak / 2**20:.1f} MiB"


def test_a_docx_text_runs_references_are_counted_as_they_come():
    data = _docx_document(f"<w:document {W_NS}><w:body><w:p><w:r><w:t>{'&#x10000;' * 1_100_000}</w:t></w:r></w:p>"
                          "</w:body></w:document>")  # one fragment for each, once
    peak, refused = _peak(lambda: extract(data, extraction.DOCX))
    assert refused and peak < 24 * 2**20, f"{peak / 2**20:.1f} MiB"  # its 10 MB part and a block: 90 MiB before


@pytest.mark.parametrize("shape", ["bare attributes", "attributes hidden from a scan of the source by a comment",
                                   "an end tag's attributes", "a run of spaces"])
def test_an_html_tag_is_bounded_where_the_parser_starts_it(shape):
    many = " a" * 200_000
    source = {"bare attributes": f"<p{many}>x</p>",
              "attributes hidden from a scan of the source by a comment": f'<!-- <x a=" --> <p{many}>Text.</p>" >',
              "an end tag's attributes": f"<p>x</p{many}>",
              "a run of spaces": "<p" + " " * 200_000 + ">x</p>"}[shape].encode()
    peak, refused = _peak(lambda: extract(source, extraction.HTML))
    assert refused and peak < 4 * 2**20, f"{peak / 2**20:.1f} MiB"  # bare attributes: 69 MiB before


@pytest.mark.parametrize("where", ["a paragraph", "the title"])
def test_html_references_are_turned_a_piece_at_a_time_and_bounded(where):
    references = "&copy;" * 2_000_000
    source = (f"<p>{references}</p>" if where == "a paragraph" else f"<title>{references}</title><p>x</p>").encode()
    peak, refused = _peak(lambda: extract(source, extraction.HTML))
    assert refused and peak < 1.5 * len(source), f"{peak / 2**20:.1f} MiB"  # its text decoded and a piece: 41 MiB before
    assert [p.text for p in extract(b"<title>a &amp; b &copy</title><p>&copyright &notin; x</p>", extraction.HTML).passages] \
        == ["a & b \u00a9", "\u00a9right \u2209 x"]  # turned as before


def test_a_pdf_page_past_its_character_bound_is_refused_before_its_text_is_read(monkeypatch):
    import pypdfium2

    def never(*args, **kwargs):
        raise AssertionError("the page's text was read")

    monkeypatch.setattr(extraction, "MAX_PAGE_CHARS", 50)
    monkeypatch.setattr(pypdfium2.PdfTextPage, "get_text_range", never)
    with pytest.raises(extraction.Unreadable) as unreadable:
        extract(synthetic.paper_pdf(), extraction.PDF)
    assert unreadable.value.code == "unreadable_file"


def _end_records(entries, size, zip64):
    """A ZIP's tail declaring entries and a central directory of size bytes, as a ZIP64 archive's when
    zip64 (its classic record saturated), after a little of a first member."""
    body = b"PK\x03\x04" + b"\0" * 60
    if not zip64:
        return body + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, min(entries, 0xFFFF), min(entries, 0xFFFF),
                                  size, len(body), 0)
    record = len(body)
    body += struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, size, 0)
    body += struct.pack("<4sLQL", b"PK\x06\x07", 0, record, 1)
    return body + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)


@pytest.mark.parametrize("zip64", [False, True])
@pytest.mark.parametrize("declares", ["entries", "directory size"])
def test_a_docx_declaring_too_many_entries_or_too_large_a_directory_is_refused_before_it_is_read(monkeypatch, zip64,
                                                                                               declares):
    entries, size = ((extraction.MAX_ARCHIVE_MEMBERS + 1, 46) if declares == "entries"
                     else (1, extraction.MAX_CENTRAL_DIRECTORY + 1))
    if zip64 and declares == "entries":
        entries = 500_000  # far past what a 16-bit count can say
    data = _end_records(entries, size, zip64)

    def never(*args, **kwargs):
        raise AssertionError("the archive's directory was read")

    monkeypatch.setattr(zipfile, "ZipFile", never)
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.DOCX)
    assert media_type("paper.docx", data) is None  # nor taken as a DOCX when added


def _zip64_of(archive, saturate=True):
    """A ZIP archive (no comment) rewritten with ZIP64 end records, as zipfile reads them: its classic
    record saturated, or left with its own small values beside them."""
    at = len(archive) - 22
    _, _, _, _, entries, size, offset, _ = struct.unpack("<4s4H2LH", archive[at:])
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, size, offset)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, at, 1)
    classic = ((0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF) if saturate else (entries, entries, size, offset))
    return archive[:at] + record + locator + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, *classic, 0)


def _refused_unread(monkeypatch, data):
    def never(*args, **kwargs):
        raise AssertionError("the archive's directory was read")

    monkeypatch.setattr(zipfile, "ZipFile", never)
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.DOCX)
    assert media_type("paper.docx", data) is None  # refused when added, not a server error


@pytest.mark.parametrize("declares", ["entries", "directory size"])
def test_a_zip64_record_beside_a_small_classic_one_governs_and_is_bounded(monkeypatch, declares):
    body = b"PK\x03\x04" + b"\0" * 60
    directory = b"\0" * (extraction.MAX_CENTRAL_DIRECTORY + 1 if declares == "directory size" else 46)
    entries = 500_000 if declares == "entries" else 1
    at = len(body) + len(directory)  # where the ZIP64 record starts: right after the directory, as zipfile checks
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, len(directory), len(body))
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, at, 1)
    classic = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 46, len(body), 0)  # small: nothing saturated
    _refused_unread(monkeypatch, body + directory + record + locator + classic)


@pytest.mark.parametrize("zip64", [False, True])
def test_a_directory_holding_more_records_than_its_count_says_is_refused_before_it_is_read(monkeypatch, zip64):
    body = b"PK\x03\x04" + b"\0" * 60
    record = struct.pack("<4s4B4HL2L5H2L", b"PK\x01\x02", 20, 0, 20, 0, *[0] * 14)  # one entry, no name
    directory = record * (extraction.MAX_ARCHIVE_MEMBERS + 1)  # within the size bound, past the count's
    data = body + directory + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(directory), len(body), 0)
    data = _zip64_of(data) if zip64 else data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:  # zipfile reads records until the size is used, whatever the count
        assert len(archive.filelist) == extraction.MAX_ARCHIVE_MEMBERS + 1
    _refused_unread(monkeypatch, data)


@pytest.mark.parametrize("case", ["a locator offset past any file", "a directory before the file's start",
                                  "a directory past its end record"])
def test_end_records_pointing_outside_the_file_are_refused_as_unreadable(monkeypatch, case):
    body = b"PK\x03\x04" + b"\0" * 60
    if case == "a locator offset past any file":
        data = body + struct.pack("<4sLQL", b"PK\x06\x07", 0, 0xFFFFFFFFFFFFFFFF, 1) + struct.pack(
            "<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 46, 0, 0)
    elif case == "a directory before the file's start":
        data = body + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 10_000, 0, 0)  # larger than what precedes it
    else:  # a ZIP64 record whose directory ends after the record it should precede
        record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, 46, len(body) + 1000)
        data = body + record + struct.pack("<4sLQL", b"PK\x06\x07", 0, len(body), 1) + struct.pack(
            "<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    _refused_unread(monkeypatch, data)


@pytest.mark.parametrize("saturate", [True, False])
def test_a_zip64_docx_still_reads(saturate):
    data = _zip64_of(synthetic.docx([(None, "A paragraph of synthetic text.")]), saturate)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:  # zipfile reads it by its ZIP64 records
        assert "word/document.xml" in archive.namelist()
    assert [p.text for p in extract(data, extraction.DOCX).passages] == ["A paragraph of synthetic text."]
    assert media_type("paper.docx", data) == extraction.DOCX


def test_a_docx_within_its_bounds_still_reads():
    read = extract(synthetic.docx([(None, "A paragraph of synthetic text.")]), extraction.DOCX)
    assert [p.text for p in read.passages] == ["A paragraph of synthetic text."]
    assert media_type("paper.docx", synthetic.docx([(None, "x")])) == extraction.DOCX


def test_a_docx_is_bounded_by_its_members_and_its_parts_together(monkeypatch):
    plain = f'<w:document {W}><w:body><w:p><w:r><w:t>{"x" * 300}</w:t></w:r></w:p></w:body></w:document>'
    styles = f'<w:styles {W}>{" " * 300}</w:styles>'
    data = zipped({"word/document.xml": plain, "word/styles.xml": styles, "word/media/image.bin": b"\0" * 2000})
    assert extract(data, extraction.DOCX).passages
    monkeypatch.setattr(extraction, "MAX_ARCHIVE_BYTES", 1000)  # what the archive says it unpacks to
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.DOCX)
    monkeypatch.setattr(extraction, "MAX_ARCHIVE_BYTES", 10**6)
    monkeypatch.setattr(extraction, "MAX_XML_BYTES", 500)  # each part fits, the two together do not
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.DOCX)
    monkeypatch.setattr(extraction, "MAX_ARCHIVE_MEMBERS", 2)
    monkeypatch.setattr(extraction, "MAX_XML_BYTES", 10**6)
    with pytest.raises(extraction.Unreadable):
        extract(data, extraction.DOCX)


def test_an_arxiv_answer_that_declares_an_entity_is_refused():
    from backend import lookup
    with pytest.raises(lookup.Failed) as failed:
        lookup._arxiv("2401.00001", ('<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x SYSTEM "file:///etc/hosts">]>'
                                     '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&x;</title></entry></feed>')
                      .encode())
    assert failed.value.code == "unavailable"
