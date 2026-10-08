"""Reading files into passages (slice-1 spec section 7.1; ticket 05), on synthetic files: one paragraph
on one page, at most 2,000 characters split at sentence boundaries, tables and captions their own,
references marked, and each format read from its own structure with nothing fetched or included."""

import io
import struct
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
        ("reference", "Smith, J. (2020). An earlier synthetic paper. doi:10.5555/cited.paper.002"),
        ("reference", "Doe, A. (2019). Another synthetic paper.")]
    assert read.passages[3].section_path == ["1 Introduction"] and read.passages[6].section_path == ["References"]


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
