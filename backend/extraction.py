"""Reading a material's file into anchored passages (slice-1 spec section 7.1; ticket 05).

`extract(data, media_type, stop)` parses one file from its own structure, never by a model:
- PDF with pypdfium2: per page, text with PDFium's character range and the characters' boxes
  merged per line (fractions of the page as displayed and rendered, its rotation applied, from its
  top left). A page with fewer than 100 characters whose images cover more than 60% of it, or
  whose characters mostly have no Unicode mapping, is scanned: it yields no passage and is
  counted for OCR (S1-20).
- DOCX from its XML (zip and the standard library): paragraphs by style, tables.
- HTML with the standard library's parser: text only; nothing it names is fetched.
- Markdown with a small parser of its own.
- LaTeX with pylatexenc: no TeX run, and no file but the material is read (\\input and
  \\include give nothing).

A passage is one paragraph on one page, at most MAX_PASSAGE characters; a longer one splits at
a sentence boundary. Tables and captions are their own passages, a reference list's entries are
`reference` passages, and the title and abstract are marked where the structure shows them.
Each passage keeps its section path (the headings above it) and its offsets: PDFium character
indices on its page for a PDF, offsets into the source text otherwise (a hint, ADR 0001).

pdfium is not thread-safe, so every use of it holds PDFIUM. Nothing here logs content, and the
libraries it drives are kept from logging it.
"""

import collections
import contextvars
import ctypes
import io
import logging
import math
import re
import struct
import threading
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from xml.etree import ElementTree

MAX_PASSAGE = 2000
MAX_FILE_BYTES = 100 * 1024 * 1024  # ponytail: uploads travel as base64 JSON; a streamed upload if books matter
# What any reading holds, whatever its file's shape (_Reading). A reading never holds an object for
# each of a file's lines, characters or marks beyond these: each allocation that grows with the
# file's structure (a list of its lines, cells, matches, attributes or nodes, or text copied once
# for each level it is nested in) is bounded before it is made, by a count taken first or by a
# check as it grows, and a file past a bound is unreadable_file before its memory grows. What is
# only as large as the file (its decoded text, a copy of one block of it) is bounded by its size.
MAX_TEXT_CHARS = 16 * 1024 * 1024  # the text a reading keeps: its passages and headings together
MAX_BUILT_CHARS = 4 * MAX_TEXT_CHARS  # the text it builds on the way (_Text, joined rows), each copy counted
MAX_BLOCK_CHARS = 1024 * 1024  # one block's text, or its source, before regular expressions run over it
MAX_BLOCKS = 200_000  # its blocks: paragraphs, headings, tables and captions, and tables' rows and cells
MAX_PAGE_CHARS = 100_000  # a PDF page's characters, as PDFium counts them before any is read
# A LaTeX file's marks: what may start one of pylatexenc's nodes (a macro, a group, a comment, math,
# or a special: & ~ -- `` '' !` ?`), counted before parsing. It holds a node for each and one for
# the text between two, about 570 bytes a mark as measured: 500,000 marks come to about 280 MiB.
# Measured, dense mathematics has about 160 marks in 1,000 characters (480 on a 3,000-character page)
# and ordinary prose about 60: a paper has some thousands, and a 1,000-page mathematical book in one
# file still fits.
MAX_LATEX_MARKS = 500_000
MAX_STYLES = 10_000  # a DOCX's styles by name: a file of more is refused, never read with some left out
MAX_TAG_ATTRIBUTES = 1024  # an HTML tag's attributes, counted before the parser lists them
MAX_XML_DEPTH = 1000  # a DOCX part's elements open at once
MAX_XML_TOKEN = 256 * 1024  # bytes a DOCX part's parser may take in without a tag or text ending
MAX_XML_BYTES = 64 * 1024 * 1024  # a DOCX part's size once unpacked, and the parts read together
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024  # a DOCX's members together, as the archive declares them unpacked
MAX_ARCHIVE_MEMBERS = 10_000
MAX_CENTRAL_DIRECTORY = MAX_ARCHIVE_MEMBERS * 256  # bytes: 46 for each entry and room for its name
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML = "text/html"
MARKDOWN = "text/markdown"
LATEX = "application/x-tex"
EXTENSIONS = {".pdf": PDF, ".docx": DOCX, ".html": HTML, ".htm": HTML, ".xhtml": HTML, ".md": MARKDOWN,
              ".markdown": MARKDOWN, ".tex": LATEX, ".latex": LATEX}
# Each media type's extractor and its version. Bump a version when its parser's output changes:
# extractions are shared by file and extractor version.
EXTRACTORS = {PDF: ("pdf", "pdf-3"), DOCX: ("docx", "docx-2"), HTML: ("html", "html-2"),
              MARKDOWN: ("markdown", "markdown-2"), LATEX: ("latex", "latex-2")}
PDFIUM = threading.Lock()
MAX_PAGE_PIXELS = 8 * 1024 * 1024  # a rendered page image's pixels: a letter page at scale 3 has 4.4 million

# pylatexenc logs what it parses: a tolerated parse error with the source around it (INFO), an
# unknown node whole (WARNING), each node (DEBUG). Its loggers never write, at any level. The other
# libraries read here log no content: pypdfium2 names unsupported PDF features and its own objects;
# html.parser, zipfile and ElementTree have no logger (zipfile's one warning on reading names a
# member, and only the two parts in _DOCX_PARTS are opened).
logging.getLogger("pylatexenc").setLevel(logging.CRITICAL + 1)

# The scanned-page check (section 13).
SCANNED_CHARS = 100
SCANNED_IMAGE_COVER = 0.6

_SENTENCE_END = re.compile(r"(?<=[.!?;])[\"')\]]*\s+|(?<=[。！？；])")
_CAPTION = re.compile(r"^(?:figure|fig\.|table|tab\.|exhibit|chart)\s*[0-9IVX]+[a-z]?\b|^(?:图|表)\s*[0-9一二三四五六七八九十]+",
                      re.IGNORECASE)
_REFERENCES = re.compile(r"^(?:\d+\.?\s*)?(?:references|bibliography|works cited|literature cited|reference list"
                         r"|参考文献|引用文献)\s*:?$", re.IGNORECASE)
_ABSTRACT = re.compile(r"^(?:abstract|摘要|摘\s*要)\s*[:：.—-]?\s*", re.IGNORECASE)

# Identifiers (F3a step 3): the DOI pattern Crossref recommends, and arXiv's two forms.
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
DOI_CHARS = 300  # a DOI's length at most, as the details form takes one
_DOI_FOUND = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>,\x00-\x1f]+", re.IGNORECASE)
ARXIV = re.compile(r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})", re.IGNORECASE)
_ARXIV_FOUND = re.compile(r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})"
                          r"(?:v\d+)?", re.IGNORECASE)
IDENTIFIER_PAGES = 2  # a PDF's first pages
IDENTIFIER_CHARS = 8000  # another format's first characters


class Unreadable(Exception):
    """The file cannot be read. code is stable: unreadable_file, encrypted_file."""

    def __init__(self, code="unreadable_file"):
        super().__init__(code)
        self.code = code


@dataclass
class Passage:
    kind: str
    text: str
    page: int | None = None
    section_path: list = field(default_factory=list)
    char_start: int | None = None
    char_end: int | None = None
    boxes: dict | None = None


@dataclass
class Extracted:
    extractor: str
    version: str
    passages: list
    pages: int | None = None
    ocr_pages: int = 0  # scanned pages, waiting for OCR


def media_type(name, data):
    """The supported media type of a file by its name's extension, checked against its bytes, or None."""
    suffix = name[name.rfind("."):].lower() if "." in name else ""
    found = EXTENSIONS.get(suffix)
    if found == PDF and b"%PDF-" not in data[:1024]:
        return None
    if found == DOCX:
        try:
            with _zip(data) as archive:
                if "word/document.xml" not in archive.namelist():
                    return None
        except (Unreadable, zipfile.BadZipFile, ValueError, OSError, OverflowError, struct.error):
            return None
    return found


_EOCD = struct.Struct("<4s4H2LH")  # a ZIP's end of central directory record
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_CENTRAL = struct.Struct("<4s4B4HL2L5H2L")  # a central directory record, its name, extra and comment after it


def _end_records(data):
    """(entries, central directory size, where it starts) as the installed zipfile takes them from an
    archive's end (CPython 3.13's _EndRecData and _EndRecData64, mirrored on the bytes), or None where
    it would refuse the archive. As there: the end record is the last 22 bytes, or else the last one in
    the final 64 KiB (its comment may follow it); a ZIP64 locator just before it, whenever present,
    puts its record's count, size and offset in place of the end record's, once checked against it;
    and the directory is read from the end record's place less its size. Every read is a slice of an
    exact length, so no offset, however large, raises."""
    def chunk(at, size):
        piece = data[at:at + size] if at >= 0 else b""
        return piece if len(piece) == size else None

    at = len(data) - _EOCD.size
    tail = chunk(at, _EOCD.size)
    if tail is None or not (tail[:4] == b"PK\x05\x06" and tail[-2:] == b"\0\0"):
        start = max(len(data) - 0xFFFF - _EOCD.size, 0)
        at = data.rfind(b"PK\x05\x06", start)
        tail = chunk(at, _EOCD.size) if at >= 0 else None
        if tail is None:
            return None
    _, _, _, _, entries, size, offset, _ = _EOCD.unpack(tail)
    location = at
    locator = chunk(at - _ZIP64_LOCATOR.size, _ZIP64_LOCATOR.size)
    if locator is not None and locator[:4] == b"PK\x06\x07":
        _, disk, record_at, disks = _ZIP64_LOCATOR.unpack(locator)
        if disk != 0 or disks > 1:
            return None
        before = at - _ZIP64_LOCATOR.size - _ZIP64_EOCD.size  # where the record ends, its extensible data after it
        if record_at > before:
            return None
        extra = before - record_at
        record = chunk(record_at, _ZIP64_EOCD.size)
        if record is not None and record[:4] != b"PK\x06\x06" and record_at != before:  # data prepended
            extra, record = 0, chunk(before, _ZIP64_EOCD.size)
        if record is None or record[:4] != b"PK\x06\x06":
            return None
        _, record_size, _, _, _, _, _, entries, size, offset = _ZIP64_EOCD.unpack(record)
        if offset + size != record_at or record_size + 12 != _ZIP64_EOCD.size + extra:
            return None
        location = before - extra
    if location - size < 0:  # the directory would begin before the file does
        return None
    return entries, size, location - size


def _records(data, size, start):
    """How many records zipfile will take from the central directory: as it does, record after record
    until their lengths use the directory's size, whatever count the end records gave; stopped at a
    cut record or a wrong signature, where zipfile stops too (refusing the archive), or once past
    MAX_ARCHIVE_MEMBERS."""
    directory, total, count = data[start:start + size], 0, 0
    while total < size and count <= MAX_ARCHIVE_MEMBERS:
        if total + _CENTRAL.size > len(directory):
            break
        record = _CENTRAL.unpack_from(directory, total)
        if record[0] != b"PK\x01\x02":
            break
        count += 1
        total += _CENTRAL.size + record[12] + record[13] + record[14]  # its name, extra field and comment
    return count


def _zip(data):
    """A ZIP archive opened, once its end records, read here as zipfile will read them
    (_end_records), show it within bounds: at most MAX_ARCHIVE_MEMBERS entries and a central
    directory of at most MAX_CENTRAL_DIRECTORY bytes, inside the file before its end record, and
    that directory, walked as zipfile will walk it (_records), holds at most MAX_ARCHIVE_MEMBERS
    records whatever its count says. Otherwise Unreadable, before zipfile reads that directory and
    builds an entry for each of its records."""
    try:
        found = _end_records(data)
    except (struct.error, OverflowError, ValueError):
        found = None
    if found is None or found[0] > MAX_ARCHIVE_MEMBERS or found[1] > MAX_CENTRAL_DIRECTORY:
        raise Unreadable()
    if _records(data, found[1], found[2]) > MAX_ARCHIVE_MEMBERS:
        raise Unreadable()
    return zipfile.ZipFile(io.BytesIO(data))


def extractor_of(kind):
    """(extractor, version) for a media type, the library's own version included where it parses."""
    name, version = EXTRACTORS[kind]
    if kind == PDF:
        import pypdfium2
        version += f"+pypdfium2-{pypdfium2.version.PYPDFIUM_INFO}"
    elif kind == LATEX:
        from pylatexenc.version import version_str
        version += f"+pylatexenc-{version_str}"
    return name, version


def extract(data, kind, stop=lambda: None, progress=lambda done, total: None):
    """The passages of a file of a supported media type. stop() is called between pages or
    blocks and may raise to abandon the work; progress(done, total) reports it."""
    parser = {PDF: _pdf, DOCX: _docx, HTML: _html, MARKDOWN: _markdown, LATEX: _latex}[kind]
    reading = _READING.set(_Reading())  # this reading's bounds, wherever its extractor keeps something
    try:
        if kind == PDF:
            return parser(data, stop, progress)
        extracted = parser(_text(data) if kind != DOCX else data, stop)
    finally:
        _READING.reset(reading)
    progress(1, 1)
    return extracted


def _text(data):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise Unreadable()


def identifiers(passages):
    """The identifiers a material's own text gives, in order: ("doi", "10.…") and ("arxiv", "…"),
    from a PDF's first pages or another format's first characters, outside reference passages,
    each validated by its pattern and normalized (DOIs in lower case). The characters' window ends
    inside the passage that crosses it: an identifier there counts only if it ends before the edge,
    so none past it is used and none is cut short by it."""
    found, seen, chars = [], set(), 0
    for passage in passages:
        if passage.page is not None and passage.page > IDENTIFIER_PAGES:
            break
        if passage.page is None and chars >= IDENTIFIER_CHARS:
            break
        edge = len(passage.text) if passage.page is not None else IDENTIFIER_CHARS - chars
        chars += len(passage.text)
        if passage.kind == "reference":
            continue
        for match in _DOI_FOUND.finditer(passage.text):
            if match.end() > edge:
                break
            doi = clean_doi(match.group(0))
            if doi and ("doi", doi) not in seen:
                seen.add(("doi", doi))
                found.append(("doi", doi))
        for match in _ARXIV_FOUND.finditer(passage.text):
            if match.end() > edge:
                break
            arxiv = match.group(1)
            if ARXIV.fullmatch(arxiv) and ("arxiv", arxiv) not in seen:
                seen.add(("arxiv", arxiv))
                found.append(("arxiv", arxiv))
    return found


def clean_doi(text):
    """A DOI as found in text, without trailing punctuation, in lower case; None if it fails the
    pattern, holds a "." or ".." segment, or is longer than DOI_CHARS."""
    doi = text.strip().rstrip(".,;:'\"")
    while doi.endswith((")", "]")) and doi.count(doi[-1]) > doi.count({")": "(", "]": "["}[doi[-1]]):
        doi = doi[:-1].rstrip(".,;:")
    doi = doi.lower()
    if len(doi) > DOI_CHARS or not DOI.fullmatch(doi) or {".", ".."} & set(doi.split("/")):
        return None  # a dot segment among them: a URL holding it would name another path once resolved
    return doi


# Passages


class _Reading:
    """What one reading has kept (MAX_TEXT_CHARS, MAX_BLOCKS) and built (MAX_BUILT_CHARS) so far."""

    def __init__(self):
        self.chars = self.blocks = self.built = 0

    def keep(self, text="", blocks=1):
        self.blocks += blocks
        self.chars += len(text)
        if self.blocks > MAX_BLOCKS or self.chars > MAX_TEXT_CHARS:
            raise Unreadable()

    def build(self, chars):
        self.built += chars
        if self.built > MAX_BUILT_CHARS:
            raise Unreadable()


_READING = contextvars.ContextVar("reading", default=None)  # set by extract for its reading


def _keep(text="", blocks=1):
    """Count blocks (and their text) against the reading's bounds; Unreadable past one."""
    reading = _READING.get()
    if reading is not None:
        reading.keep(text, blocks)


def _build(chars):
    """Count text about to be built (a copy, a join) against the reading's bound; Unreadable past it."""
    reading = _READING.get()
    if reading is not None:
        reading.build(chars)


class _Text:
    """Text built piece by piece, held as one growing buffer rather than a list of its pieces, each
    piece counted against the reading's bound on what it builds (_build), and given up (Unreadable)
    once longer than limit."""

    def __init__(self, limit=MAX_TEXT_CHARS):
        self.buffer, self.size, self.limit = io.StringIO(), 0, limit

    def add(self, piece):
        self.size += len(piece)
        if self.size > self.limit:
            raise Unreadable()
        _build(len(piece))
        self.buffer.write(piece)

    def __bool__(self):
        return self.size > 0

    def value(self):
        return self.buffer.getvalue()


def _normal(text):
    """Text with its whitespace runs as single spaces, trimmed, in NFC; Unreadable for a text longer
    than MAX_BLOCK_CHARS, before re.sub makes a piece for each of its runs."""
    if len(text) > MAX_BLOCK_CHARS:
        raise Unreadable()
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", text)).strip()


def _split(text, limit=MAX_PASSAGE):
    """Cut points that keep each piece at most limit characters: at the last sentence end before
    the limit, else the last space, else the limit itself."""
    cuts, start = [], 0
    while len(text) - start > limit:
        window = text[start:start + limit]
        ends = [m.end() for m in _SENTENCE_END.finditer(window) if m.end() > limit // 4]
        cut = ends[-1] if ends else (window.rfind(" ") + 1 if window.rfind(" ") > limit // 4 else limit)
        cuts.append(start + cut)
        start += cut
    return cuts


def _pieces(text, kind, page, path, start, end, char_boxes=None, page_size=None, source=None, base=0):
    """One block as passages, counted against the reading's bounds (_keep): split at sentence
    boundaries when longer than MAX_PASSAGE. char_boxes (one per character of text, or None) give
    each piece its line rectangles and its own range of PDFium's characters. Otherwise each piece
    takes its own part of the block's source range [start, end), where source (the document's text,
    or a DOCX paragraph's own, base its offset) is searched for each later piece's start (_located)."""
    if not text:
        return []
    _keep(text)
    bounds = [0, *_split(text), len(text)]
    spans = [(a, b, text[a:b].strip()) for a, b in zip(bounds, bounds[1:])]
    spans = [span for span in spans if span[2]]
    located = (_located([piece for _, _, piece in spans], source, base, start, end)
               if char_boxes is None and start is not None and end is not None else None)
    passages = []
    for n, (a, b, piece) in enumerate(spans):
        rects = _rects(char_boxes[a:b], page_size) if char_boxes and page_size else None
        if char_boxes is not None:  # a PDF piece: PDFium's characters it is made of
            chars = [i for i in char_boxes[a:b] if i is not None]
            first, last = (chars[0][4], chars[-1][4] + 1) if chars else (start, end)
        elif located is not None:
            first, last = located[n], located[n + 1] if n + 1 < len(located) else end
        else:
            first, last = start, end
        passages.append(Passage(kind, piece, page, list(path), first, last,
                                {"rects": rects} if rects else None))
    return passages


def _located(pieces, source, base, start, end):
    """Where each of a block's pieces begins in its source range [start, end): each where its first
    words are next found in source, the first from start (a block of one piece: at start) and each
    later one no sooner than the piece before it is long (a piece's text is never longer than its
    source); else, as when there is no source, at its share of the range. In order, and within it."""
    def find(piece, lower):
        if source is None:
            return None
        words = r"\s+".join(map(re.escape, piece.split(maxsplit=3)[:3]))
        match = re.compile(words).search(source, lower - base, end - base)
        return match.start() + base if match else None

    found, total, done = [start], sum(map(len, pieces)) or 1, 0
    if len(pieces) > 1:  # past what comes before its text: a fence's opening line, a list item's mark
        found[0] = find(pieces[0], start) or start
    for before, piece in zip(pieces, pieces[1:]):
        done += len(before)
        lower = min(found[-1] + len(before), end)
        at = find(piece, lower)
        found.append(at if at is not None else min(max(start + (end - start) * done // total, lower), end))
    return found


def _rects(boxes, size):
    """Character boxes (left, bottom, right, top, index, line) merged per line, as fractions of the
    page with a top-left origin, rounded."""
    width, height = size
    lines = {}
    for box in boxes:
        if box is None:
            continue
        left, bottom, right, top, _, line = box
        if right <= left or top <= bottom:
            continue
        merged = lines.get(line)
        lines[line] = (left, bottom, right, top) if merged is None else (
            min(merged[0], left), min(merged[1], bottom), max(merged[2], right), max(merged[3], top))
    return [[round(l / width, 4), round(1 - t / height, 4), round(r / width, 4), round(1 - b / height, 4)]
            for l, b, r, t in lines.values()]


class _Sections:
    """The heading path above a block, and the kind its text takes there (abstract, reference)."""

    def __init__(self):
        self.levels, self.mode = [], None  # (level, heading) from the outermost

    @property
    def path(self):
        return [text for _, text in self.levels]

    def heading(self, level, text):
        text = _normal(text)
        if not text:
            return
        _keep(text)
        while self.levels and self.levels[-1][0] >= level:
            self.levels.pop()
        self.levels.append((level, text))
        if _REFERENCES.match(text):
            self.mode = "reference"
        elif _ABSTRACT.fullmatch(text):
            self.mode = "abstract"
        else:
            self.mode = None

    def kind(self, text, default="paragraph"):
        if default != "paragraph":
            return default
        if self.mode is None and _ABSTRACT.match(text) and len(_ABSTRACT.sub("", text, count=1)) > 40:
            return "abstract"
        if _CAPTION.match(text):
            return "caption"
        return self.mode or "paragraph"


# PDF


def _pdf(data, stop, progress):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    with PDFIUM:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as error:
            raise Unreadable("encrypted_file" if "password" in str(error).lower() else "unreadable_file") from None
        try:
            count = len(document)
            # Two passes, so that no page's characters are held past its own reading: the first counts
            # the characters set in each font size, all the body text's size needs (_body_size), as a
            # few sizes, not an entry for each line; the second reads each page whole and makes its
            # passages at once.
            weights = collections.Counter()
            for number in range(count):
                stop()
                lines, _, _ = _pdf_read(document, number, raw, measure=True)
                for line in lines:
                    # A size past 2,000 points is counted as 2,000: at most 4,001 sizes, however many a file sets.
                    weights[round(min(max(line["size"], 0.0), 2000.0) * 2) / 2] += len(line["text"])
                progress(number + 1, 2 * count)
            body = _body_size(weights)
            sections, passages, scanned = _Sections(), [], 0
            for number in range(count):
                stop()
                lines, size, is_scanned = _pdf_read(document, number, raw)
                scanned += is_scanned
                passages += _pdf_blocks(number + 1, lines, size, body, sections)
                progress(count + number + 1, 2 * count)
        finally:
            document.close()
    return Extracted(*extractor_of(PDF), passages, count, scanned)


def _pdf_read(document, number, raw, measure=False):
    """The page read (_pdf_page). A page PDFium cannot load or read makes the file unreadable, as one
    whose text leaves out many characters at either end does: pypdfium2 steps over each of those by
    recursion, past Python's limit (raised from a ctypes call as ArgumentError)."""
    import pypdfium2 as pdfium

    try:
        page = document[number]
        try:
            return _pdf_page(page, raw, measure)
        finally:
            page.close()
    except (pdfium.PdfiumError, RecursionError, ctypes.ArgumentError):
        raise Unreadable() from None


def _pdf_page(page, raw, measure=False):
    """The page's lines: [{"text", "boxes" (per character: (l, b, r, t, index, line) or None), "size",
    "left", "right", "top", "bottom", "bold"}], its size, and whether it is scanned. Sizes, boxes and
    edges are of the page as displayed and rendered, its rotation and crop applied (_displayed), in points
    from its bottom left. With measure, only each line's text and size are true: no box is read and no
    line is checked for bold, the rest (lines, sizes, the scanned check) as without it."""
    width, height = page.get_size()  # as displayed: a page turned a quarter is as wide as it was high
    if not (width > 0 and height > 0):  # no area to place its text on, as render_page refuses it
        raise Unreadable()
    shown = _displayed(page, raw, width, height)
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        if count > MAX_PAGE_CHARS:  # refused before its text is decoded: a small page can hold a great deal
            raise Unreadable()
        text = textpage.get_text_range() if count else ""
        # The text leaves out some characters PDFium counts (control characters) when it is shorter:
        # each of its characters is then found among them by PDFium's own map (-1 for one it put in).
        aligned = len(text) == count
        mapped = unmapped = 0
        lines, chars, boxes = [], [], []
        for index, char in enumerate(text):
            if char in "\r\n":
                if chars:
                    lines.append((chars, boxes))
                chars, boxes = [], []
                continue
            box = None
            at = index if aligned else raw.FPDFText_GetCharIndexFromTextIndex(textpage, index)
            if at >= 0:
                if raw.FPDFText_HasUnicodeMapError(textpage, at) == 1:
                    unmapped += 1
                elif not char.isspace():
                    mapped += 1
                if not char.isspace():
                    left, bottom, right, top = (0.0, 0.0, 0.0, 0.0) if measure else shown(*textpage.get_charbox(at))
                    box = (left, bottom, right, top, at, len(lines), raw.FPDFText_GetFontSize(textpage, at))
            elif not char.isspace():
                mapped += 1
            chars.append(char)
            boxes.append(box)
        if chars:
            lines.append((chars, boxes))
        # Whether each line is bold, by its first character with a box.
        bold = [not measure and _bold(textpage, raw, next((b[4] for b in boxes if b), None)) for _, boxes in lines]
    finally:
        textpage.close()
    cover = 0.0
    if mapped < SCANNED_CHARS:
        for image in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            left, bottom, right, top = shown(*image.get_bounds())
            cover += max(0.0, min(right, width) - max(left, 0)) * max(0.0, min(top, height) - max(bottom, 0))
    scanned = (mapped < SCANNED_CHARS and cover > SCANNED_IMAGE_COVER * width * height) or \
        unmapped > mapped
    if scanned:
        return [], (width, height), True
    return [dict(_line(chars, boxes), bold=heavy) for (chars, boxes), heavy in zip(lines, bold)
            if "".join(chars).strip()], (width, height), False


_BOLD_FONT = re.compile(r"bold|black|heavy|demi|cmbx", re.IGNORECASE)


def _bold(textpage, raw, index):
    """Whether a character is set in a bold font: by its weight, its font's ForceBold flag, or its font's name."""
    if index is None:
        return False
    name, flags = ctypes.create_string_buffer(128), ctypes.c_int()
    raw.FPDFText_GetFontInfo(textpage, index, name, len(name), ctypes.byref(flags))
    return raw.FPDFText_GetFontWeight(textpage, index) >= 600 or bool(flags.value & (1 << 18)) or \
        bool(_BOLD_FONT.search(name.value.decode("latin-1")))


def _displayed(page, raw, width, height):
    """The map of a box (left, bottom, right, top) in the page's own PDF space to the page as PDFium
    displays and renders it (its /Rotate and crop box applied), in points from its bottom left: the
    transform PDFium renders with, read at three points (its output is whole device units, 1/64 pt)."""
    k = 64

    def device(x, y):
        dx, dy = ctypes.c_int(), ctypes.c_int()
        raw.FPDF_PageToDevice(page.raw, 0, 0, round(width * k), round(height * k), 0, float(x), float(y),
                              ctypes.byref(dx), ctypes.byref(dy))
        return dx.value / k, height - dy.value / k

    (ox, oy), (ax, ay), (bx, by) = device(0, 0), device(1000, 0), device(0, 1000)
    a, b, c, d = (ax - ox) / 1000, (ay - oy) / 1000, (bx - ox) / 1000, (by - oy) / 1000

    def shown(left, bottom, right, top):
        xs = [a * x + c * y + ox for x in (left, right) for y in (bottom, top)]
        ys = [b * x + d * y + oy for x in (left, right) for y in (bottom, top)]
        return min(xs), min(ys), max(xs), max(ys)
    return shown


def _line(chars, boxes):
    found = [b for b in boxes if b is not None]
    sizes = sorted(b[6] for b in found)
    return {"text": "".join(chars), "boxes": [b[:6] if b else None for b in boxes],
            "size": sizes[len(sizes) // 2] if sizes else 0.0,
            "left": min((b[0] for b in found), default=0.0), "right": max((b[2] for b in found), default=0.0),
            "top": max((b[3] for b in found), default=0.0), "bottom": min((b[1] for b in found), default=0.0)}


def _body_size(weights):
    """The most common line font size, by characters: the body text's. weights: the characters set
    in each size, rounded to half points, in the order the sizes were first met."""
    return max(weights, key=weights.get) if weights else 0.0


def _pdf_blocks(number, lines, size, body, sections):
    """A page's lines grouped into headings and paragraphs, as passages. A larger font than the
    body's marks a heading (the largest on the first page, the title); a vertical gap, a change of
    size, a short line ending a sentence, or a caption's start ends a paragraph."""
    passages, block, heading, previous, after_table = [], [], [], None, 0
    right_edge = max((line["right"] for line in lines), default=0.0)
    largest = max((line["size"] for line in lines), default=0.0)
    cells = [_cells(line) for line in lines]

    def flush():
        if heading:  # consecutive heading lines of one size are one heading
            text, boxes = _join(heading)
            if number == 1 and heading[0]["size"] == largest and largest >= body * 1.3 and not sections.path \
                    and not any(p.kind == "title" for p in passages):
                passages.extend(_pieces(text, "title", number, [], None, None, boxes, size))
            else:
                sections.heading(1 if heading[0]["size"] >= body * 1.4 else 2, text)
            heading.clear()
        if block:
            text, boxes = _join(block)
            kind = sections.kind(_normal(text))
            if kind == "abstract":
                text, boxes = _drop_label(text, boxes)
            passages.extend(_pieces(text, kind, number, sections.path, None, None, boxes, size))
            block.clear()

    for at, line in enumerate(lines):
        if at < after_table:
            continue
        end, rows = _table_at(lines, cells, at)
        if rows:  # a table starts here: what came before ends, and a reference list holds none
            flush()
            if sections.mode != "reference":
                text, boxes = _table(lines, rows)
                passages.extend(_pieces(text, "table", number, sections.path, None, None, boxes, size))
                previous, after_table = None, end
                continue
        text = line["text"].strip()
        larger = body and line["size"] >= body * 1.15 and len(text) < 200 and not text.endswith(".")
        if larger or (len(text) < 40 and (_REFERENCES.match(text) or _ABSTRACT.fullmatch(text))):
            if block or (heading and abs(heading[-1]["size"] - line["size"]) > 0.6):
                flush()
            heading.append(line)
            previous = None
            continue
        if heading:
            flush()
        if previous is not None:
            height = max(previous["top"] - previous["bottom"], 1.0)
            gap = previous["bottom"] - line["top"]
            short = previous["right"] < right_edge * 0.8 and previous["text"].rstrip().endswith(
                (".", "?", "!", "。", "？", "！", ":"))
            if gap > height * 0.8 or abs(line["size"] - previous["size"]) > 0.6 or short or _CAPTION.match(text) \
                    or (sections.mode == "reference" and _reference_start(text)):
                flush()
        block.append(line)
        previous = line
    flush()
    return passages


# A table in a PDF's text layer (section 7.1: tables are their own passages). A cell is a run of a
# line's characters set apart from the next by more than TABLE_GAP of its font size. A table is two
# or more rows in a run of lines of one font size, none further below the one above than two
# lines: its first line's cells set its columns, and a row has a cell lined up with each column (by
# its left or its right edge). A line with fewer cells, each within one column's span, set at line
# spacing below the line above, continues those cells (a wrapped cell, the last row's too), unless
# it reads as a heading: a section heading (References, Abstract and the like), or bold where the
# row above is not. A section heading always ends a table. A table is told from text set in columns
# (two columns of body text line up too) by its cells: every cell holds at most TABLE_CELL
# characters, or one column (after the first row, which may be a header) holds only cells of at most
# TABLE_SHORT, as a column of values or codes does, beside a label of any length.
# Not found, by design: a table whose cells are all longer than TABLE_SHORT with one over TABLE_CELL
# in every column; cells closer than TABLE_GAP (tight ruled tables); a cell spanning columns; rows of
# differing font size or more than two lines apart; a table PDFium gives column by column; a
# wrapped cell's line set further apart than the line spacing. Taken in wrongly: two columns of body
# text whose lines are all TABLE_CELL characters or shorter; a short line set at line spacing under
# a table's last row (a note, say) joins that row's cell; a same-font, non-bold heading that is not a
# known section heading, set between rows at line spacing, joins a cell.
TABLE_GAP = 1.5
TABLE_CELL = 40
TABLE_SHORT = 30


def _cells(line):
    """A line's cells: [(first, end, left, right)], character positions in its text and edges."""
    boxes, gap = line["boxes"], TABLE_GAP * max(line["size"], 1.0)
    spans, first, last = [], None, None
    for at, box in enumerate(boxes):
        if box is None:  # a space, or a character without its box
            continue
        if last is not None and box[0] - boxes[last][2] > gap:
            spans.append((first, last + 1))
            first = None
        first = at if first is None else first
        last = at
    if first is not None:
        spans.append((first, last + 1))
    return [(first, end, boxes[first][0], boxes[end - 1][2]) for first, end in spans]


def _table_at(lines, cells, start):
    """The table that starts at lines[start]: (the line after it, its rows), each row a list of its
    cells, each cell [(line index, first, end)] (more than one for a wrapped cell); else (start, None)."""
    head = cells[start]
    if len(head) < 2:
        return start, None
    tolerance, size = max(lines[start]["size"], 3.0), lines[start]["size"]
    rows, pending, end = [[[(start, c[0], c[1])] for c in head]], [], start + 1

    def column(cell):  # the column whose span holds the cell, if it stays inside it
        for k, (_, _, left, _) in enumerate(head):
            after = head[k + 1][2] if k + 1 < len(head) else float("inf")
            if left - tolerance <= cell[2] < after - tolerance and cell[3] < after:
                return k
        return None

    while end < len(lines):
        line, above = lines[end], lines[end - 1]
        text = line["text"].strip()
        gap, spacing = above["bottom"] - line["top"], max(above["top"] - above["bottom"], 1.0)
        if abs(line["size"] - size) > 0.6 or gap > 2 * spacing or \
                (len(text) < 40 and (_REFERENCES.match(text) or _ABSTRACT.fullmatch(text))):
            break  # another size, a gap, or a section heading (References always ends a table)
        row = cells[end]
        if len(row) == len(head) and all(abs(c[2] - h[2]) <= tolerance or abs(c[3] - h[3]) <= tolerance
                                         for c, h in zip(row, head)):
            for _, spans in pending:  # the wrapped cells' lines, joined to the row above them
                for k, piece in spans:
                    rows[-1][k].append(piece)
            pending = []
            rows.append([[(end, c[0], c[1])] for c in row])
        else:
            spans = [(column(c), (end, c[0], c[1])) for c in row]
            if not 1 <= len(row) < len(head) or any(k is None for k, _ in spans) or \
                    len({k for k, _ in spans}) != len(spans) or gap > spacing or \
                    (line["bold"] and not lines[rows[-1][0][0][0]]["bold"]):  # a bold heading under plain rows
                break
            pending.append((end, spans))
        end += 1
    for _, spans in pending:  # the last row's wrapped cells
        for k, piece in spans:
            rows[-1][k].append(piece)

    def length(cell):
        return sum(e - f for _, f, e in cell) + len(cell) - 1

    if len(rows) < 2 or not (all(length(cell) <= TABLE_CELL for row in rows for cell in row) or any(
            all(length(row[k]) <= TABLE_SHORT for row in rows[1:]) for k in range(len(head)))):
        return start, None
    return end, rows


def _table(lines, rows):
    """A table's rows as text, a row a line, its cells apart by " | " and a wrapped cell's lines by a
    space, with each character's box."""
    text, boxes = "", []
    for row in rows:
        if text:
            text += "\n"
            boxes.append(None)
        for number, cell in enumerate(row):
            if number:
                text += " | "
                boxes += [None] * 3
            for piece, (at, first, end) in enumerate(cell):
                if piece:
                    text += " "
                    boxes.append(None)
                text += lines[at]["text"][first:end]
                boxes += lines[at]["boxes"][first:end]
    return text, boxes


def _reference_start(text):
    return bool(re.match(r"^(?:\[\d+\]|\d+\.\s|[A-Z][\w'’-]+,\s+[A-Z])", text))


def _join(lines):
    """Lines as one paragraph's text, with each character's box: lines joined by a space (none
    between Han characters, and a line-end hyphen dropped before a lowercase letter)."""
    text, boxes = "", []
    for line in lines:
        chars, line_boxes = list(line["text"]), list(line["boxes"])
        while chars and chars[-1].isspace():
            chars.pop()
            line_boxes.pop()
        while chars and chars[0].isspace():
            chars.pop(0)
            line_boxes.pop(0)
        if not chars:
            continue
        if text:
            if text.endswith("-") and chars[0].islower() and len(text) > 1 and text[-2].isalpha():
                text, boxes = text[:-1], boxes[:-1]
            elif not (_han(text[-1]) and _han(chars[0])):
                text += " "
                boxes.append(None)
        text += "".join(chars)
        boxes += line_boxes
    return text, boxes


def _drop_label(text, boxes):
    match = _ABSTRACT.match(text)
    if match and len(text) - match.end() > 0:
        return text[match.end():], boxes[match.end():]
    return text, boxes


def _han(char):
    """Whether a character is Han, or CJK punctuation or a full-width form (no space goes between them)."""
    return "\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf" or "\u3000" <= char <= "\u303f" \
        or "\uff00" <= char <= "\uffef"


def render_page(data, number, scale=2.0):
    """Page number (from 1) of a PDF as a PNG image, rendered by pdfium in memory, at scale or less:
    never more than MAX_PAGE_PIXELS, however large the page says it is. Raises IndexError for a page
    it does not have, Unreadable for a file it cannot open or a page with no area."""
    import pypdfium2 as pdfium

    with PDFIUM:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            raise Unreadable() from None
        try:
            if not 1 <= number <= len(document):
                raise IndexError(number)
            try:
                page = document[number - 1]
            except pdfium.PdfiumError:
                raise Unreadable() from None
            try:
                width, height = page.get_size()
                if not (width > 0 and height > 0):
                    raise Unreadable()
                scale = min(scale, math.sqrt(MAX_PAGE_PIXELS / (width * height)))
                while math.ceil(width * scale) * math.ceil(height * scale) > MAX_PAGE_PIXELS:  # pypdfium2 rounds up
                    scale *= 0.99
                bitmap = page.render(scale=scale, rev_byteorder=True)
                width, height, stride, channels = bitmap.width, bitmap.height, bitmap.stride, bitmap.n_channels
                pixels = bytes(bitmap.buffer)
                bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
    return _png(pixels, width, height, stride, channels)


def _png(pixels, width, height, stride, channels):
    """A PNG of 8-bit RGB or RGBA rows, with the standard library only."""
    rows = b"".join(b"\x00" + pixels[y * stride:y * stride + width * channels] for y in range(height))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if channels == 4 else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")


# DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# The parts of a DOCX that are read, by exact name; nothing is reached through its relationships.
_DOCX_PARTS = ("word/document.xml", "word/styles.xml")
_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _undeclared(content):
    """Refuse (Unreadable) XML that declares a document type or an entity, so no parser sees one
    (no expansion, no external entity)."""
    probe = content
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16: looked at as text
        probe = content.decode("utf-16", errors="replace").encode("utf-8", errors="replace")
    if _DECLARATION.search(probe) or b"\x00<\x00!" in probe or b"<\x00!\x00" in probe:
        raise Unreadable()


def untrusted_xml(content):
    """Parse XML from a file or a response that is not ours, refused as _undeclared refuses it."""
    _undeclared(content)
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise Unreadable() from None


class _XmlEvents:
    """An expat parser's target: its events as (event, tag, attributes or text), with no tree built."""

    def __init__(self):
        self.events = []

    def start(self, tag, attrib):
        self.events.append(("start", tag, attrib))

    def end(self, tag):
        self.events.append(("end", tag, None))

    def data(self, text):
        self.events.append(("data", None, text))

    def close(self):
        return None


def _xml_events(content):
    """An untrusted XML part's events, ("start", tag, attributes), ("data", None, text) and ("end",
    tag, None), as it is parsed 64 KiB at a time (refused as _undeclared refuses it): no element is
    built, so nothing is held for the whole part but its bytes and one piece's events. Refused too
    (Unreadable) when more than MAX_XML_DEPTH elements are open at once, or when the parser takes in
    MAX_XML_TOKEN bytes with nothing ending: a tag that long would have its attributes made whole."""
    _undeclared(content)
    target = _XmlEvents()
    parser = ElementTree.XMLParser(target=target)
    depth, quiet = 0, 0
    try:
        for at in range(0, len(content) + 1, 1 << 16):
            piece = content[at:at + (1 << 16)]
            if piece:
                parser.feed(piece)
            else:
                parser.close()
            events, target.events = target.events, []
            quiet = 0 if events else quiet + len(piece)
            if quiet > MAX_XML_TOKEN:
                raise Unreadable()
            for event in events:
                depth += {"start": 1, "end": -1}.get(event[0], 0)
                if depth > MAX_XML_DEPTH:
                    raise Unreadable()
                yield event
    except ElementTree.ParseError:
        raise Unreadable() from None


def _part(archive, name, budget):
    """A part's bytes, read within what is left of the budget of bytes the parts may take unpacked."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > budget[0]:
        raise Unreadable()
    with archive.open(info) as source:
        content = source.read(budget[0] + 1)
    if len(content) > budget[0]:  # the archive understated its size
        raise Unreadable()
    budget[0] -= len(content)
    return content


def _docx(data, stop):
    try:
        with _zip(data) as archive:
            members = archive.infolist()
            # An archive that could unpack to more than any document needs, or that names a path
            # outside itself, is refused before any part is read.
            if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES or any(
                    m.filename.startswith(("/", "\\")) or ".." in m.filename.replace("\\", "/").split("/")
                    or "\x00" in m.filename for m in members):
                raise Unreadable()
            budget = [MAX_XML_BYTES]
            document, styles = (_part(archive, name, budget) for name in _DOCX_PARTS)
    except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, NotImplementedError, OverflowError, struct.error):
        raise Unreadable() from None
    if document is None:
        raise Unreadable()
    names = _docx_styles(styles) if styles is not None else {}
    sections, passages, offset = _Sections(), [], 0
    for tag, text, style in _docx_body(document, stop):
        if tag == "p":
            name = names.get(style, style.lower()) if style is not None else ""
            heading = re.fullmatch(r"heading ?(\d)", name)
            clean = _normal(text)
            if not clean:
                pass
            elif name == "title":
                passages += _pieces(clean, "title", None, [], offset, offset + len(text), source=text, base=offset)
            elif heading:
                sections.heading(int(heading.group(1)), clean)
            else:
                kind = "caption" if name == "caption" else sections.kind(clean)
                if kind == "abstract":
                    clean = _ABSTRACT.sub("", clean, count=1) or clean
                passages += _pieces(clean, kind, None, sections.path, offset, offset + len(text), source=text,
                                    base=offset)
        elif text:
            passages += _pieces(text, "table", None, sections.path, offset, offset + len(text), source=text, base=offset)
        offset += len(text) + 1
    return Extracted(*extractor_of(DOCX), passages)


def _docx_styles(content):
    """A styles part's style names (lower case) by style id, read event by event: each style's first
    name of its own; at most MAX_STYLES of them."""
    names, path, open_ = {}, [], []  # path: the open elements' tags; open_: the styles being read, [id, name]
    for event, tag, value in _xml_events(content):
        if event == "start":
            if tag == f"{_W}style":
                open_.append([value.get(f"{_W}styleId"), None])
            elif tag == f"{_W}name" and path and path[-1] == f"{_W}style" and open_[-1][1] is None:
                open_[-1][1] = value.get(f"{_W}val", "")
            path.append(tag)
        elif event == "end":
            path.pop()
            if tag == f"{_W}style":
                style, name = open_.pop()
                if style not in names and len(names) >= MAX_STYLES:  # never read with some left out
                    raise Unreadable()
                names[style] = (name or "").lower()
    return names


class _Collected(_Text):
    """A paragraph's or a cell's visible text gathered from events (_docx_body), at most
    MAX_BLOCK_CHARS, each piece counted against what the reading builds (a nested cell's text comes
    into every cell around it, each copy counted): how many pieces it has (a paragraph within it is
    set apart only after some), and once it ends (done), its text as a cell keeps it."""

    def __init__(self):
        super().__init__(MAX_BLOCK_CHARS)
        self.pieces, self.cell = 0, None

    def add(self, piece):
        super().add(piece)
        self.pieces += 1

    def done(self):
        self.cell, self.buffer = _normal(self.value()), None


def _docx_body(content, stop):
    """Each paragraph and table directly in a DOCX document's body, in order, as (tag, text, style):
    "p", its visible text and its style id (or None); or "tbl", its rows (each its cells' text, a
    nested table's cells and rows included) and None. Visible text: its runs' text, tabs and breaks,
    a paragraph within it set apart by a line break once some text has come; not deleted text or
    field codes. Read event by event (_xml_events): nothing is held for the whole part but its
    bytes, and a table's rows and cells count against the reading's bounds (_keep) as they start."""
    T, P, TBL, TR, TC = (f"{_W}{name}" for name in ("t", "p", "tbl", "tr", "tc"))
    path, in_body, block, style = [], False, None, None  # path: the open elements' tags, the document's first
    collectors, rows, open_rows, text, text_ended, bodies = [], [], [], None, False, 0
    for event, tag, value in _xml_events(content):
        if event == "data":
            if text is not None and path[-1] == T and not text_ended:  # a w:t's own text, before any child of it
                text.append(value)
            continue
        if event == "start":
            depth = len(path)  # the document 0, its body 1, the body's blocks 2
            text_ended = text is not None  # a child of a w:t ends its own text
            path.append(tag)
            if depth == 1 and tag == f"{_W}body":
                bodies += 1
                in_body = bodies == 1
            if not in_body or depth < 2:
                continue
            if depth == 2:  # a block of the body
                stop()
                block, style, rows, open_rows = tag, None, [], []
                collectors = [_Collected()] if tag == P else []
            elif block not in (P, TBL):
                continue
            elif tag == T:
                text, text_ended = [], False
            elif tag == P:
                for collector in collectors:
                    if collector.pieces:
                        collector.add("\n")
            elif tag == f"{_W}tab":
                for collector in collectors:
                    collector.add("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):
                for collector in collectors:
                    collector.add("\n")
            elif tag == f"{_W}pStyle" and block == P and depth == 4 and path[3] == f"{_W}pPr" and style is None:
                style = value.get(f"{_W}val")
            elif tag == TR and block == TBL:
                _keep()  # a row, as a block
                row = []
                rows.append(row)
                open_rows.append(row)
            elif tag == TC and block == TBL:
                _keep()  # a cell, as a block
                cell = _Collected()
                for row in open_rows:
                    row.append(cell)
                collectors.append(cell)
            continue
        path.pop()
        depth = len(path)
        if depth == 1 and tag == f"{_W}body":
            in_body = False
        if not in_body or depth < 2:
            continue
        if depth == 2:  # the block ends
            if block == P:
                yield "p", collectors[0].value(), style
            elif block == TBL:
                kept = [row[0] for row in rows if row[0].strip(" |")]
                _build(sum(map(len, kept)) + len(kept))  # before they are joined
                yield "tbl", "\n".join(kept), None
            block, collectors, rows, open_rows = None, [], [], []
        elif block not in (P, TBL):
            continue
        elif tag == T and text is not None:
            own = "".join(text)
            for collector in collectors:
                collector.add(own)
            text = None
        elif tag == TC and block == TBL:
            collectors.pop().done()
        elif tag == TR and block == TBL:
            row = open_rows.pop()
            _build(sum(len(cell.cell) + 3 for cell in row))  # before its cells, nested ones included, are joined
            row[:] = [" | ".join(cell.cell for cell in row)]


# HTML

_SKIPPED = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas", "select", "button",
            "nav"}
_BLOCKS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "li", "ul", "ol", "blockquote", "pre",
           "figure", "dl", "dt", "dd", "address", "hr", "form", "fieldset", "details", "summary"}
_HEADINGS = {f"h{n}": n for n in range(1, 7)}


class _HtmlTitle(HTMLParser):
    """An HTML document's <title> text (title): what every <title> element holds, together."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.in_title = _Text(MAX_BLOCK_CHARS), False

    def handle_starttag(self, tag, attrs):
        self.in_title = self.in_title or tag == "title"

    def handle_endtag(self, tag):
        self.in_title = self.in_title and tag != "title"

    def handle_data(self, data):
        if self.in_title:
            self.title.add(data)


class _HtmlReader(HTMLParser):
    """An HTML document's blocks, each given to emit(kind, text, start, end, level) as it ends. Its
    own text only: no resource it names is ever loaded. Nothing is held for the whole document but
    the parser's own input: positions are found from the line the parser is on, a block's text and
    a table's are each a _Text, and a table's cells count against the reading's bounds as they end."""

    def __init__(self, source, emit):
        super().__init__(convert_charrefs=True)
        self.source, self.emit, self.line, self.line_start = source, emit, 1, 0
        self.skip, self.in_title = 0, False
        self.text, self.start, self.end, self.kind, self.level = _Text(MAX_BLOCK_CHARS), None, None, "paragraph", 0
        self.table, self.row, self.cell, self.pre = None, None, None, 0

    def _at(self):
        line, column = self.getpos()
        while self.line < line:  # the parser only moves on: each newline is looked for once
            self.line_start = self.source.index("\n", self.line_start) + 1
            self.line += 1
        return self.line_start + column

    def _flush(self):
        if self.text:
            text = self.text.value()
            if text.strip():
                self.emit(self.kind, text if self.pre else _normal(text), self.start, self.end, self.level)
        self.text, self.start, self.end, self.kind, self.level = _Text(MAX_BLOCK_CHARS), None, None, "paragraph", 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        elif self.skip:
            return
        elif tag == "br":
            (self.cell if self.cell is not None else self.text).add("\n" if self.pre else " ")
        elif tag == "table":
            if self.table is None:
                self._flush()
                self.table, self.table_start, self.depth = _Text(), self._at(), 0
            self.depth += 1
        elif self.table is not None:  # a nested table's cells are its outer cell's text
            if tag == "tr" and self.depth == 1:
                self.row, self.row_cells, self.row_any = _Text(), 0, False
            elif tag in ("td", "th", "caption") and self.depth == 1:
                self.cell = _Text(MAX_BLOCK_CHARS)
            elif tag in ("td", "th") and self.cell is not None:
                self.cell.add(" ")
        elif tag in _HEADINGS:
            self._flush()
            self.kind, self.level = "heading", _HEADINGS[tag]
        elif tag in ("figcaption", "caption"):
            self._flush()
            self.kind = "caption"
        elif tag in _BLOCKS:
            self._flush()
            self.pre += tag == "pre"

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.in_title = False
        elif self.skip:
            return
        elif self.table is not None:
            if self.depth > 1 and tag != "table":
                return
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                cell = _normal(self.cell.value())
                _keep()  # a cell, as a block
                if self.row_cells:
                    self.row.add(" | ")
                self.row.add(cell)
                self.row_cells, self.row_any, self.cell = self.row_cells + 1, self.row_any or bool(cell), None
            elif tag == "tr" and self.row is not None:
                if self.row_any:
                    self.table.add(("\n" if self.table else "") + self.row.value())
                self.row = None
            elif tag == "caption" and self.cell is not None:
                caption = _normal(self.cell.value())
                if caption:
                    self.emit("caption", caption, self.table_start, self._at(), 0)
                self.cell = None
            elif tag == "table" and self.depth > 1:
                self.depth -= 1
            elif tag == "table":
                if self.table:
                    self.emit("table", self.table.value(), self.table_start, self._at(), 0)
                self.table, self.row, self.cell = None, None, None
        elif tag in _HEADINGS or tag in ("figcaption", "caption") or tag in _BLOCKS:
            self._flush()
            self.pre -= tag == "pre" and self.pre > 0

    def handle_data(self, data):
        if self.in_title or self.skip:  # the title is _HtmlTitle's
            return
        if self.table is not None:
            if self.cell is not None:
                self.cell.add(data)
            return
        if self.start is None and data.strip():
            self.start = self._at()
        self.text.add(data)
        self.end = self._at() + len(data)

    def close(self):
        super().close()
        self._flush()


def _html_tags(source):
    """Refuse (Unreadable) a document with a tag of more than MAX_TAG_ATTRIBUTES attributes, counted
    one at a time with html.parser's own patterns (Python 3.13's) before the parser sees it: it
    lists a tag's attributes whole before any handler is called. Every start tag is looked at once,
    each from where the one before ended."""
    from html import parser as stdlib

    at = 0
    while (start := stdlib.starttagopen.search(source, at)) is not None:
        end = stdlib.locatetagend.match(source, start.start() + 1).end()
        found, k = 0, stdlib.tagfind_tolerant.match(source, start.start() + 1).end()
        while k < end and (attribute := stdlib.attrfind_tolerant.match(source, k)) is not None:
            found += 1
            if found > MAX_TAG_ATTRIBUTES:
                raise Unreadable()
            k = attribute.end()
        at = max(end, start.start() + 1)


def _html(source, stop):
    """An HTML document's passages: its title first (its <title>, read in a first pass, or else its
    first h1), then its blocks as the reader gives them; its tags checked first (_html_tags)."""
    _html_tags(source)
    sections, passages = _Sections(), []
    titles = _HtmlTitle()
    try:
        titles.feed(source)
        titles.close()
    except (AssertionError, ValueError):
        raise Unreadable() from None
    title = [_normal(titles.title.value())]
    if title[0]:
        passages += _pieces(title[0], "title", None, [], None, None)

    def emit(kind, text, start, end, level):
        stop()
        if kind == "heading":
            if level == 1 and not title[0]:
                title[0] = text
                passages.extend(_pieces(text, "title", None, [], start, end, source=source))
            else:
                sections.heading(level, text)
            return
        kind = kind if kind in ("table", "caption") else sections.kind(text)
        passages.extend(_pieces(_ABSTRACT.sub("", text, count=1) if kind == "abstract" else text, kind, None,
                                sections.path, start, end, source=source))

    reader = _HtmlReader(source, emit)
    try:
        reader.feed(source)
        reader.close()
    except (AssertionError, ValueError):
        raise Unreadable() from None
    return Extracted(*extractor_of(HTML), passages)


# Markdown

_ATX = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_INLINE = re.compile(r"(\*\*|\*|`|~~)(?=\S)(.+?)(?<=\S)\1|(?<!\w)(__|_)(?=\S)(.+?)(?<=\S)\3(?!\w)")


def _markdown_inline(text):
    """Text without Markdown's inline marks; Unreadable past MAX_BLOCK_CHARS, before each re.sub
    makes a piece for each of its marks."""
    if len(text) > MAX_BLOCK_CHARS:
        raise Unreadable()
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    for _ in range(3):
        text = _INLINE.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(4), text)
    return text


def _markdown(source, stop):
    """A Markdown file's blocks, read line by line from the text itself (no list of its lines), each
    made into passages as it ends; a paragraph's text is taken from its range of the source then,
    rather than held line by line."""
    sections, passages, titled = _Sections(), [], [False]

    def emit(kind, text, start, end, level=0):
        if kind == "heading":
            if level == 1 and not titled[0] and not passages:
                passages.extend(_pieces(text, "title", None, [], start, end, source=source))
                titled[0] = True
            else:
                sections.heading(level, text)
            return
        kind = kind if kind in ("table", "caption") else sections.kind(text)
        passages.extend(_pieces(_ABSTRACT.sub("", text, count=1) if kind == "abstract" else text, kind, None,
                                sections.path, start, end, source=source))

    def line_at(at):
        """The line that starts at at and where the next one starts; (None, at) past the last.
        Unreadable for a line longer than MAX_BLOCK_CHARS, before it is copied out."""
        if at > len(source):
            return None, at
        end = source.find("\n", at)
        end = len(source) if end < 0 else end
        if end - at > MAX_BLOCK_CHARS:
            raise Unreadable()
        return source[at:end], end + 1

    paragraph = None  # (where it starts, where its last line ends, whether a list item starts it)

    def flush(end):
        nonlocal paragraph
        if paragraph is not None:
            start, last, item = paragraph
            if last - start > MAX_BLOCK_CHARS:  # before its marks are taken out, match by match
                raise Unreadable()
            first, _, rest = source[start:last].partition("\n") if item else ("", "", source[start:last])
            rest = re.sub(r"(?m)^[^\S\n]{0,3}>[^\S\n]?", "", rest)  # each line's quote mark
            joined = re.sub(r"\s*\n\s*", " ", (_LIST.sub("", first, count=1) + "\n" if item else "") + rest).strip()
            text = _normal(_markdown_inline(joined))
            if text:
                emit("caption" if _IMAGE.fullmatch(joined) else "paragraph", text, start, end)
        paragraph = None

    at = 0
    line, after = line_at(0)
    if line is not None and line.strip() == "---":  # front matter
        scan, past = line_at(after)
        while scan is not None:
            if scan.strip() in ("---", "..."):
                at = past
                break
            scan, past = line_at(past)
    line, after = line_at(at)
    count = 0
    while line is not None:
        count += 1
        if count % 200 == 0:
            stop()
        following, beyond = line_at(after)
        if _FENCE.match(line):
            flush(at)
            fence = _FENCE.match(line).group(1)
            closing, (scan, past) = after, (following, beyond)  # the closing line's start, and the line there
            while scan is not None and not scan.strip().startswith(fence):
                closing, (scan, past) = past, line_at(past)
            code = source[after:closing - 1 if scan is not None else len(source)].strip()
            if code:
                emit("paragraph", code, at, closing if scan is not None else len(source))  # unfinished: to the end
            at, (line, after) = past, line_at(past)
            continue
        heading = _ATX.match(line)
        if heading:
            flush(at)
            emit("heading", _normal(_markdown_inline(heading.group(2))), at, at + len(line), len(heading.group(1)))
            at, (line, after) = after, line_at(after)
            continue
        if line.strip() and following is not None and re.fullmatch(r" {0,3}(=+|-+)\s*", following) \
                and paragraph is None and not _LIST.match(line):
            emit("heading", _normal(_markdown_inline(line)), at, after + len(following), 1 if "=" in following else 2)
            at, (line, after) = beyond, line_at(beyond)
            continue
        if "|" in line and following is not None and _TABLE_RULE.match(following):
            flush(at)
            table, end = _Text(), after + len(following)  # a table of no row but its header ends at its rule

            def cells(row):  # its cells, counted as blocks before the row is split into them
                _keep(blocks=row.count("|") + 1)
                return " | ".join(_normal(_markdown_inline(cell)) for cell in row.strip().strip("|").split("|"))

            table.add(cells(line))
            row_at, (row, past) = beyond, line_at(beyond)
            while row is not None and "|" in row and row.strip():
                table.add("\n" + cells(row))
                end = row_at + len(row)
                row_at, (row, past) = past, line_at(past)
            emit("table", table.value(), at, end)
            at, (line, after) = row_at, line_at(row_at)
            continue
        if not line.strip():
            flush(at)
        elif _LIST.match(line):
            flush(at)
            paragraph = (at, at + len(line), True)
        else:
            paragraph = ((at, at + len(line), False) if paragraph is None
                         else (paragraph[0], at + len(line), paragraph[2]))  # it goes on to this line
        at, (line, after) = after, line_at(after)
    flush(len(source))
    return Extracted(*extractor_of(MARKDOWN), passages)


# LaTeX

_LATEX_HEADINGS = {"part": 1, "chapter": 1, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4}
_LATEX_DROPPED = {"input", "include", "includeonly", "includegraphics", "bibliography", "bibliographystyle", "label",
                  "maketitle", "tableofcontents", "newcommand", "renewcommand", "providecommand", "def", "usepackage",
                  "documentclass", "author", "date", "thanks", "vspace", "hspace", "newpage", "clearpage", "centering",
                  "noindent", "footnote", "affiliation", "email", "keywords", "cite", "citep", "citet", "citealp",
                  "citeauthor", "citeyear", "nocite", "ref", "eqref", "autoref", "cref", "Cref", "pageref"}
_LATEX_MATH = {"equation", "align", "gather", "multline", "eqnarray", "displaymath", "math", "flalign", "alignat"}
_LATEX_LISTS = {"itemize", "enumerate", "description"}
# What starts one of pylatexenc's nodes: a macro, math or an environment (\\), a group, a comment,
# inline math, and its default context's specials. Counted with str.count, so "---" counts once.
_LATEX_MARKS = ("\\", "{", "%", "$", "&", "~", "--", "``", "''", "!`", "?`")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")


def _latex(source, stop):
    from pylatexenc.latex2text import LatexNodes2Text
    from pylatexenc.latexwalker import (LatexCharsNode, LatexCommentNode, LatexEnvironmentNode, LatexMacroNode,
                                        LatexWalker, LatexWalkerError, get_default_latex_context_db)
    from pylatexenc.macrospec import EnvironmentSpec, MacroSpec

    converter = LatexNodes2Text(math_mode="text", strict_latex_spaces=False)  # never reads \input files

    def text_of(nodes):
        try:
            return converter.nodelist_to_text(nodes)
        except (RecursionError, LatexWalkerError, ValueError, KeyError, AttributeError, TypeError):
            return ""

    # The arguments the default context does not know, so a caption's text, a reference's key and
    # a bibliography's width are read as arguments rather than as text.
    # pylatexenc holds the whole file's nodes at once: their number is bounded first, by the marks
    # that can start one (with the text between them, at most twice as many nodes and one more).
    if sum(source.count(mark) for mark in _LATEX_MARKS) > MAX_LATEX_MARKS:
        raise Unreadable()
    for breaks, _ in enumerate(_BLANK_LINE.finditer(source), start=1):  # its paragraph breaks, one at a time
        if breaks > MAX_BLOCKS:  # each a block once read (walk): refused before pylatexenc reads them all
            raise Unreadable()
    context = get_default_latex_context_db()
    context.add_context_category("scholia", macros=[MacroSpec("caption", "[{"), MacroSpec("bibitem", "[{")],
                                 environments=[EnvironmentSpec("thebibliography", "{")], prepend=True)
    try:
        nodes, _, _ = LatexWalker(source, latex_context=context, tolerant_parsing=True).get_latex_nodes()
    except (RecursionError, LatexWalkerError, ValueError):
        raise Unreadable() from None
    sections, passages = _Sections(), []
    title = None
    pending, span = [], [None, None]  # the current paragraph's pieces and its source range
    steps = [0]

    def flush(kind=None):
        text = _normal("".join(pending))
        if text:
            chosen = kind or sections.kind(text)
            if chosen == "abstract":
                text = _ABSTRACT.sub("", text, count=1) or text
            passages.extend(_pieces(text, chosen, None, sections.path, span[0], span[1], source=source))
        pending.clear()
        span[0] = span[1] = None

    def add(text, start, end):
        if text:
            pending.append(text)
            span[0] = start if span[0] is None else span[0]
            span[1] = end

    def arg(node, last=True):
        args = [a for a in (node.nodeargd.argnlist if node.nodeargd else []) if a is not None]
        if not args:
            return ""
        chosen = args[-1] if last else args[0]
        return text_of(chosen.nodelist if hasattr(chosen, "nodelist") else [chosen])

    def walk(nodelist, kind=None):
        nonlocal title
        for node in nodelist or []:
            steps[0] += 1
            if steps[0] % 200 == 0:
                stop()
            if isinstance(node, LatexCommentNode):
                continue
            if isinstance(node, LatexCharsNode):  # paragraphs apart at blank lines, each its own range
                at, gap = 0, True
                while gap:  # one blank line at a time: none is looked for before the paragraph before it is kept
                    gap = _BLANK_LINE.search(node.chars, at)
                    end = gap.start() if gap else len(node.chars)
                    add(node.chars[at:end], node.pos + at, node.pos + end)
                    if gap:
                        _keep()  # each break counted as a block, so blank lines alone make no unbounded work
                        flush(kind)
                        at = gap.end()
                continue
            if isinstance(node, LatexMacroNode):
                name = node.macroname.rstrip("*")
                if name == "title":
                    title = _normal(arg(node))
                elif name in _LATEX_HEADINGS:
                    flush(kind)
                    sections.heading(_LATEX_HEADINGS[name], arg(node))
                elif name == "caption":
                    flush(kind)
                    caption = _normal(arg(node))
                    passages.extend(_pieces(caption, "caption", None, sections.path, node.pos, node.pos + node.len,
                                            source=source))
                elif name in ("bibitem", "item", "par"):
                    flush(kind)
                elif name in _LATEX_DROPPED:
                    if name == "maketitle" and title:
                        flush(kind)
                        passages.extend(_pieces(title, "title", None, [], node.pos, node.pos + node.len, source=source))
                        title = ""
                else:
                    add(text_of([node]), node.pos, node.pos + node.len)
                continue
            if isinstance(node, LatexEnvironmentNode):
                env = node.environmentname.rstrip("*")
                if env == "document":
                    walk(node.nodelist, kind)
                elif env == "abstract":
                    flush(kind)
                    walk(node.nodelist, "abstract")
                    flush("abstract")
                elif env == "thebibliography":
                    flush(kind)
                    walk(node.nodelist, "reference")
                    flush("reference")
                elif env in ("tabular", "tabularx", "longtable", "array"):
                    flush(kind)
                    rows = [" | ".join(_normal(cell) for cell in row.split("&"))
                            for row in re.split(r"\\\\|\\hline|\\toprule|\\midrule|\\bottomrule",
                                                _latex_table_text(node, text_of))]
                    table = "\n".join(row for row in rows if row.strip(" |"))
                    passages.extend(_pieces(table, "table", None, sections.path, node.pos, node.pos + node.len,
                                            source=source))
                elif env in ("figure", "table"):
                    flush(kind)
                    walk(node.nodelist, kind)
                    flush(kind)
                elif env in _LATEX_LISTS:
                    flush(kind)
                    walk(node.nodelist, kind)
                    flush(kind)
                elif env in _LATEX_MATH:
                    add(text_of([node]), node.pos, node.pos + node.len)
                else:
                    walk(node.nodelist, kind)
                continue
            add(text_of([node]), node.pos, node.pos + node.len)

    body = next((n for n in nodes if isinstance(n, LatexEnvironmentNode) and n.environmentname == "document"), None)
    if body is not None:
        for node in nodes:  # the preamble gives the title
            if isinstance(node, LatexMacroNode) and node.macroname == "title":
                title = _normal(arg(node))
        walk(body.nodelist)
    else:
        walk(nodes)
    flush()
    if title and not any(p.kind == "title" for p in passages):
        passages[:0] = _pieces(title, "title", None, [], None, None)  # counted and split as any block
    return Extracted(*extractor_of(LATEX), passages)


def _latex_table_text(node, text_of):
    """A tabular's cells as text, keeping its & and \\\\ separators (converted per cell)."""
    from pylatexenc.latexwalker import LatexCharsNode, LatexMacroNode, LatexSpecialsNode

    out = []
    for child in node.nodelist or []:
        if isinstance(child, LatexSpecialsNode) and child.specials_chars == "&":
            out.append("&")
        elif isinstance(child, LatexMacroNode) and child.macroname in ("\\", "hline", "toprule", "midrule",
                                                                        "bottomrule"):
            out.append("\\\\")
        elif isinstance(child, LatexCharsNode):
            out.append(child.chars)
        else:
            out.append(text_of([child]))
    return "".join(out)
