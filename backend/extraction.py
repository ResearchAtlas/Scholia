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
MAX_XML_BYTES = 64 * 1024 * 1024  # a DOCX part's size once unpacked, and the parts read together
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024  # a DOCX's members together, as the archive declares them unpacked
MAX_ARCHIVE_MEMBERS = 10_000
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
HTML = "text/html"
MARKDOWN = "text/markdown"
LATEX = "application/x-tex"
EXTENSIONS = {".pdf": PDF, ".docx": DOCX, ".html": HTML, ".htm": HTML, ".xhtml": HTML, ".md": MARKDOWN,
              ".markdown": MARKDOWN, ".tex": LATEX, ".latex": LATEX}
# Each media type's extractor and its version. Bump a version when its parser's output changes:
# extractions are shared by file and extractor version.
EXTRACTORS = {PDF: ("pdf", "pdf-2"), DOCX: ("docx", "docx-1"), HTML: ("html", "html-1"),
              MARKDOWN: ("markdown", "markdown-1"), LATEX: ("latex", "latex-1")}
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
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" not in archive.namelist():
                    return None
        except (zipfile.BadZipFile, ValueError):
            return None
    return found


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
    if kind == PDF:
        return parser(data, stop, progress)
    extracted = parser(_text(data) if kind != DOCX else data, stop)
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
    each validated by its pattern and normalized (DOIs in lower case)."""
    found, seen, chars = [], set(), 0
    for passage in passages:
        if passage.page is not None and passage.page > IDENTIFIER_PAGES:
            break
        if passage.page is None and chars > IDENTIFIER_CHARS:
            break
        chars += len(passage.text)
        if passage.kind == "reference":
            continue
        for match in _DOI_FOUND.finditer(passage.text):
            doi = clean_doi(match.group(0))
            if doi and ("doi", doi) not in seen:
                seen.add(("doi", doi))
                found.append(("doi", doi))
        for match in _ARXIV_FOUND.finditer(passage.text):
            arxiv = match.group(1)
            if ARXIV.fullmatch(arxiv) and ("arxiv", arxiv) not in seen:
                seen.add(("arxiv", arxiv))
                found.append(("arxiv", arxiv))
    return found


def clean_doi(text):
    """A DOI as found in text, without trailing punctuation, in lower case; None if it fails the pattern."""
    doi = text.strip().rstrip(".,;:'\"")
    while doi.endswith((")", "]")) and doi.count(doi[-1]) > doi.count({")": "(", "]": "["}[doi[-1]]):
        doi = doi[:-1].rstrip(".,;:")
    doi = doi.lower()
    return doi if DOI.fullmatch(doi) else None


# Passages


def _normal(text):
    """Text with its whitespace runs as single spaces, trimmed, in NFC."""
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


def _pieces(text, kind, page, path, start, end, char_boxes=None, page_size=None):
    """One block as passages: split at sentence boundaries when longer than MAX_PASSAGE. char_boxes
    (one per character of text, or None) give each piece its line rectangles."""
    if not text:
        return []
    bounds = [0, *_split(text), len(text)]
    passages = []
    for a, b in zip(bounds, bounds[1:]):
        piece = text[a:b].strip()
        if not piece:
            continue
        rects = _rects(char_boxes[a:b], page_size) if char_boxes and page_size else None
        # Offsets: a PDF piece's own character range; a text piece's share of its block's source range.
        if char_boxes is not None:
            span = [i for i in char_boxes[a:b] if i is not None]
            first, last = (span[0][4], span[-1][4] + 1) if span else (start, end)
        else:
            first, last = start, end
        passages.append(Passage(kind, piece, page, list(path), first, last,
                                {"rects": rects} if rects else None))
    return passages


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
            pages, scanned = [], 0
            for number in range(count):
                stop()
                page = document[number]
                try:
                    lines, size, is_scanned = _pdf_page(page, raw)
                finally:
                    page.close()
                scanned += is_scanned
                pages.append((number + 1, lines, size))
                progress(number + 1, count)
        finally:
            document.close()
    body = _body_size([line for _, lines, _ in pages for line in lines])
    sections, passages = _Sections(), []
    for number, lines, size in pages:
        stop()
        passages += _pdf_blocks(number, lines, size, body, sections)
    return Extracted(*extractor_of(PDF), passages, count, scanned)


def _pdf_page(page, raw):
    """The page's lines: [{"text", "boxes" (per character: (l, b, r, t, index, line) or None), "size",
    "left", "right", "top", "bottom"}], its size, and whether it is scanned. Sizes, boxes and edges
    are of the page as displayed and rendered, its rotation and crop applied (_displayed), in points
    from its bottom left."""
    width, height = page.get_size()  # as displayed: a page turned a quarter is as wide as it was high
    shown = _displayed(page, raw, width, height)
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        text = textpage.get_text_range() if count else ""
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
            if aligned:
                if raw.FPDFText_HasUnicodeMapError(textpage, index) == 1:
                    unmapped += 1
                elif not char.isspace():
                    mapped += 1
                if not char.isspace():
                    left, bottom, right, top = shown(*textpage.get_charbox(index))
                    box = (left, bottom, right, top, index, len(lines), raw.FPDFText_GetFontSize(textpage, index))
            elif not char.isspace():
                mapped += 1
            chars.append(char)
            boxes.append(box)
        if chars:
            lines.append((chars, boxes))
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
    return [_line(chars, boxes) for chars, boxes in lines if "".join(chars).strip()], (width, height), False


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


def _body_size(lines):
    """The most common line font size, by characters: the body text's."""
    weights = {}
    for line in lines:
        weights[round(line["size"] * 2) / 2] = weights.get(round(line["size"] * 2) / 2, 0) + len(line["text"])
    return max(weights, key=weights.get) if weights else 0.0


def _pdf_blocks(number, lines, size, body, sections):
    """A page's lines grouped into headings and paragraphs, as passages. A larger font than the
    body's marks a heading (the largest on the first page, the title); a vertical gap, a change of
    size, a short line ending a sentence, or a caption's start ends a paragraph."""
    passages, block, heading, previous = [], [], [], None
    right_edge = max((line["right"] for line in lines), default=0.0)
    largest = max((line["size"] for line in lines), default=0.0)

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

    for line in lines:
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
            page = document[number - 1]
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


def untrusted_xml(content):
    """Parse XML from a file or a response that is not ours: refused (Unreadable) when it declares a
    document type or an entity, so the parser never sees one (no expansion, no external entity)."""
    probe = content
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16: looked at as text
        probe = content.decode("utf-16", errors="replace").encode("utf-8", errors="replace")
    if _DECLARATION.search(probe) or b"\x00<\x00!" in probe or b"<\x00!\x00" in probe:
        raise Unreadable()
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise Unreadable() from None


def _part(archive, name, budget):
    """A part read and parsed within what is left of the budget of bytes the parts may take unpacked."""
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
    return untrusted_xml(content)


def _docx(data, stop):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            # An archive that could unpack to more than any document needs, or that names a path
            # outside itself, is refused before any part is read.
            if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES or any(
                    m.filename.startswith(("/", "\\")) or ".." in m.filename.replace("\\", "/").split("/")
                    or "\x00" in m.filename for m in members):
                raise Unreadable()
            budget = [MAX_XML_BYTES]
            document, styles = (_part(archive, name, budget) for name in _DOCX_PARTS)
    except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, NotImplementedError):
        raise Unreadable() from None
    if document is None:
        raise Unreadable()
    names = {}
    for style in (styles.iter(f"{_W}style") if styles is not None else ()):
        name = style.find(f"{_W}name")
        names[style.get(f"{_W}styleId")] = (name.get(f"{_W}val") if name is not None else "").lower()
    body = document.find(f"{_W}body")
    sections, passages, offset = _Sections(), [], 0
    for element in (body if body is not None else ()):
        stop()
        if element.tag == f"{_W}p":
            text = _docx_text(element)
            style = element.find(f"{_W}pPr/{_W}pStyle")
            name = names.get(style.get(f"{_W}val"), style.get(f"{_W}val", "").lower()) if style is not None else ""
            heading = re.fullmatch(r"heading ?(\d)", name)
            clean = _normal(text)
            if not clean:
                pass
            elif name == "title":
                passages += _pieces(clean, "title", None, [], offset, offset + len(text))
            elif heading:
                sections.heading(int(heading.group(1)), clean)
            else:
                kind = "caption" if name == "caption" else sections.kind(clean)
                if kind == "abstract":
                    clean = _ABSTRACT.sub("", clean, count=1) or clean
                passages += _pieces(clean, kind, None, sections.path, offset, offset + len(text))
            offset += len(text) + 1
        elif element.tag == f"{_W}tbl":
            rows = [" | ".join(_normal(_docx_text(cell)) for cell in row.iter(f"{_W}tc"))
                    for row in element.iter(f"{_W}tr")]
            text = "\n".join(row for row in rows if row.strip(" |"))
            if text:
                passages += _pieces(text, "table", None, sections.path, offset, offset + len(text))
            offset += len(text) + 1
    return Extracted(*extractor_of(DOCX), passages)


def _docx_text(element):
    """A paragraph's or cell's visible text: runs, tabs and breaks, not deleted text or field codes."""
    parts = []
    for node in element.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
        elif node.tag == f"{_W}p" and node is not element and parts:
            parts.append("\n")
    return "".join(parts)


# HTML

_SKIPPED = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas", "select", "button",
            "nav"}
_BLOCKS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "li", "ul", "ol", "blockquote", "pre",
           "figure", "dl", "dt", "dd", "address", "hr", "form", "fieldset", "details", "summary"}
_HEADINGS = {f"h{n}": n for n in range(1, 7)}


class _HtmlReader(HTMLParser):
    """Blocks of an HTML document, in order: (kind, text, start, end, level). Its own text only:
    no resource it names is ever loaded."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.lines = [0]
        for match in re.finditer("\n", source):
            self.lines.append(match.end())
        self.blocks, self.skip, self.title = [], 0, None
        self.text, self.start, self.end, self.kind, self.level = [], None, None, "paragraph", 0
        self.table, self.row, self.cell, self.in_title, self.pre = None, None, None, False, 0

    def _at(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def _flush(self):
        text = "".join(self.text)
        if text.strip():
            self.blocks.append((self.kind, text if self.pre else _normal(text), self.start, self.end, self.level))
        self.text, self.start, self.end, self.kind, self.level = [], None, None, "paragraph", 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
        elif self.skip:
            return
        elif tag == "br":
            (self.cell if self.cell is not None else self.text).append("\n" if self.pre else " ")
        elif tag == "table":
            if self.table is None:
                self._flush()
                self.table, self.table_start, self.depth = [], self._at(), 0
            self.depth += 1
        elif self.table is not None:  # a nested table's cells are its outer cell's text
            if tag == "tr" and self.depth == 1:
                self.row = []
            elif tag in ("td", "th", "caption") and self.depth == 1:
                self.cell = []
            elif tag in ("td", "th") and self.cell is not None:
                self.cell.append(" ")
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
                self.row.append(_normal("".join(self.cell)))
                self.cell = None
            elif tag == "tr" and self.row is not None:
                if any(self.row):
                    self.table.append(" | ".join(self.row))
                self.row = None
            elif tag == "caption" and self.cell is not None:
                caption = _normal("".join(self.cell))
                if caption:
                    self.blocks.append(("caption", caption, self.table_start, self._at(), 0))
                self.cell = None
            elif tag == "table" and self.depth > 1:
                self.depth -= 1
            elif tag == "table":
                if self.table:
                    self.blocks.append(("table", "\n".join(self.table), self.table_start, self._at(), 0))
                self.table, self.row, self.cell = None, None, None
        elif tag in _HEADINGS or tag in ("figcaption", "caption") or tag in _BLOCKS:
            self._flush()
            self.pre -= tag == "pre" and self.pre > 0

    def handle_data(self, data):
        if self.in_title:
            self.title = (self.title or "") + data
            return
        if self.skip:
            return
        if self.table is not None:
            if self.cell is not None:
                self.cell.append(data)
            return
        if self.start is None and data.strip():
            self.start = self._at()
        self.text.append(data)
        self.end = self._at() + len(data)

    def close(self):
        super().close()
        self._flush()


def _html(source, stop):
    reader = _HtmlReader(source)
    try:
        reader.feed(source)
        reader.close()
    except (AssertionError, ValueError):
        raise Unreadable() from None
    sections, passages = _Sections(), []
    title = _normal(reader.title or "")
    if title:
        passages += _pieces(title, "title", None, [], None, None)
    for kind, text, start, end, level in reader.blocks:
        stop()
        if kind == "heading":
            if level == 1 and not title:
                title = text
                passages += _pieces(text, "title", None, [], start, end)
            else:
                sections.heading(level, text)
            continue
        kind = kind if kind in ("table", "caption") else sections.kind(text)
        passages += _pieces(_ABSTRACT.sub("", text, count=1) if kind == "abstract" else text, kind, None,
                            sections.path, start, end)
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
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    for _ in range(3):
        text = _INLINE.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(4), text)
    return text


def _markdown(source, stop):
    lines = source.split("\n")
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line) + 1
    blocks, i = [], 0  # (kind, text, start, end, level)
    if lines and lines[0].strip() == "---":  # front matter
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                i = j + 1
                break
    paragraph, start = [], None

    def flush(end):
        nonlocal paragraph, start
        if paragraph:
            text = _normal(_markdown_inline(" ".join(paragraph)))
            if text:
                image = _IMAGE.fullmatch(" ".join(paragraph).strip())
                blocks.append(("caption" if image else "paragraph", text, start, end, 0))
        paragraph, start = [], None

    while i < len(lines):
        if i % 200 == 0:
            stop()
        line = lines[i]
        here = offsets[i]
        if _FENCE.match(line):
            flush(here)
            fence = _FENCE.match(line).group(1)
            code, j = [], i + 1
            while j < len(lines) and not lines[j].strip().startswith(fence):
                code.append(lines[j])
                j += 1
            if "\n".join(code).strip():
                blocks.append(("paragraph", "\n".join(code).strip(), here, offsets[min(j, len(lines) - 1)], 0))
            i = j + 1
            continue
        heading = _ATX.match(line)
        if heading:
            flush(here)
            blocks.append(("heading", _normal(_markdown_inline(heading.group(2))), here, here + len(line),
                           len(heading.group(1))))
            i += 1
            continue
        if line.strip() and i + 1 < len(lines) and re.fullmatch(r" {0,3}(=+|-+)\s*", lines[i + 1]) and not paragraph \
                and not _LIST.match(line):
            blocks.append(("heading", _normal(_markdown_inline(line)), here, offsets[i + 1] + len(lines[i + 1]),
                           1 if "=" in lines[i + 1] else 2))
            i += 2
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            flush(here)
            rows, j = [line], i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(lines[j])
                j += 1
            cells = [" | ".join(_normal(_markdown_inline(cell)) for cell in row.strip().strip("|").split("|"))
                     for row in rows]
            blocks.append(("table", "\n".join(cells), here, offsets[j - 1] + len(lines[j - 1]), 0))
            i = j
            continue
        if not line.strip():
            flush(here)
        elif _LIST.match(line):
            flush(here)
            paragraph, start = [_LIST.sub("", line, count=1)], here
        else:
            stripped = re.sub(r"^\s{0,3}>\s?", "", line)
            if start is None:
                start = here
            paragraph.append(stripped.strip())
        i += 1
    flush(total)
    sections, passages, titled = _Sections(), [], False
    for kind, text, start, end, level in blocks:
        if kind == "heading":
            if level == 1 and not titled and not passages:
                passages += _pieces(text, "title", None, [], start, end)
                titled = True
            else:
                sections.heading(level, text)
            continue
        kind = kind if kind in ("table", "caption") else sections.kind(text)
        passages += _pieces(_ABSTRACT.sub("", text, count=1) if kind == "abstract" else text, kind, None,
                            sections.path, start, end)
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
            passages.extend(_pieces(text, chosen, None, sections.path, span[0], span[1]))
        pending.clear()
        span[0] = span[1] = None

    def add(text, node):
        if text:
            pending.append(text)
            span[0] = node.pos if span[0] is None else span[0]
            span[1] = node.pos + node.len

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
            if isinstance(node, LatexCharsNode):
                parts = re.split(r"\n[ \t]*\n", node.chars)
                for n, part in enumerate(parts):
                    if n:
                        flush(kind)
                    add(part, node)
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
                    passages.extend(_pieces(caption, "caption", None, sections.path, node.pos, node.pos + node.len))
                elif name in ("bibitem", "item", "par"):
                    flush(kind)
                elif name in _LATEX_DROPPED:
                    if name == "maketitle" and title:
                        flush(kind)
                        passages.extend(_pieces(title, "title", None, [], node.pos, node.pos + node.len))
                        title = ""
                else:
                    add(text_of([node]), node)
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
                    passages.extend(_pieces(table, "table", None, sections.path, node.pos, node.pos + node.len))
                elif env in ("figure", "table"):
                    flush(kind)
                    walk(node.nodelist, kind)
                    flush(kind)
                elif env in _LATEX_LISTS:
                    flush(kind)
                    walk(node.nodelist, kind)
                    flush(kind)
                elif env in _LATEX_MATH:
                    add(text_of([node]), node)
                else:
                    walk(node.nodelist, kind)
                continue
            add(text_of([node]), node)

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
        passages.insert(0, Passage("title", title, None, [], None, None))
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
