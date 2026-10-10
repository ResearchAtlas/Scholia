"""Reading a reference file into CSL JSON records (slice-1 spec F3b and section 18; ticket 06's
answer: "normalized to CSL JSON with provenance").

`parse(data, beat)` reads RIS (rispy), BibTeX and BibLaTeX (bibtexparser 2, LaTeX in values read by
pylatexenc), CSL JSON, NoteExpress and EndNote tagged files (the standard library), each found by
its content, never by its name, in UTF-8 (with or without its mark), UTF-16 with its mark, or
GB18030. It returns the records as CSL JSON and the records it skipped, counted by reason: no
visible title, or a block the parser could not read. It runs in a child process (backend/reading.py
`references`), which sends each record to the app, and `valid(csl)` is the app's check of what it
gets. It never imports the database, the server or the window.

Every bound holds before a value is worked on: the file's size (MAX_BYTES), its records
(MAX_RECORDS; more refuse the whole file), each field's characters (a value is cut before it is
cleaned, and kept at most to its field's bound), a field's continuation lines, and the authors and
keywords of a record. A record keeps only what CSL names here (KEYS); what a record points to is
never read: file attachments (RIS L1, L2 and L4, EndNote %>, BibTeX file) are not kept, and a URL is
kept only when it is http or https, never fetched. A DOI is kept only when it fits the pattern
(extraction.clean_doi), an arXiv ID only as extraction.ARXIV reads one. Names are kept as the file
gives them: "Family, Given" splits at its comma (BibTeX's by its name parts), and any other name,
a Han-script one included, is kept whole (literal). Nothing here logs content, and the parsers are
kept from logging it.
"""

import io
import json
import logging
import re
from urllib.parse import urlsplit

from backend.extraction import ARXIV, Unreadable, clean_doi
from backend.settings import visible

MAX_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_CHARS = 2 * MAX_BYTES  # the characters of a file's records together (size)
MAX_BLOCKS = 2 * MAX_RECORDS  # a BibTeX file's blocks of any kind (entries, @string, @comment, @preamble)
TITLE_CHARS = 1000  # the title and the venue, as the details form takes them
NAME_CHARS = 200  # a name, or each part of one, as backend/lookup.py keeps one
AUTHORS = 100
ABSTRACT_CHARS = 20_000
FIELD_CHARS = 300  # a short field: volume, issue, pages, publisher, ISBN, ISSN, language, number
URL_CHARS = 2000
KEYWORDS = 50
KEYWORD_CHARS = 200
CONTINUATION_LINES = 200  # a field's lines after its first; later ones are left out
CONTINUATION_CHARS = 2 * ABSTRACT_CHARS  # and their characters
RECORD_LINES = 1000  # a record's lines, a field (one an author) and its continuations; later fields are left out
BEAT_EVERY = 500  # records or lines between two beats
PARSER = ("references", "1")  # what the child names when it is ready (backend/reading.py)
FORMATS = ("ris", "bibtex", "csl_json", "noteexpress", "endnote")
SKIPPED = ("no_title", "unreadable")
KEYS = {"type", "title", "author", "issued", "container-title", "volume", "issue", "page", "publisher", "DOI",
        "URL", "ISBN", "ISSN", "language", "abstract", "keyword", "number"}
_SHORT = ("volume", "issue", "page", "publisher", "ISBN", "ISSN", "language", "number")
_TYPE = re.compile(r"[a-z]{1,30}(?:-[a-z]{1,30}){0,3}")  # a CSL type name, as backend/lookup.py reads one
_YEAR = re.compile(r"(?<!\d)(1\d{3}|2[01]\d{2}|2200)(?!\d)")  # the details form's years, 1000 to 2200
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_ARXIV_IN = re.compile(r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})"
                       r"(?:v\d+)?", re.IGNORECASE)
_LATEX = re.compile(r"[\\{}$~]")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")  # Unicode's control characters (Cc)
_SURROGATE = re.compile("[\ud800-\udfff]")  # half a pair, which only JSON's escapes can give: no text holds one
_BREAKS = re.compile("\r\n?|[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")  # every line end str.splitlines() takes, but \n

# The parsers log what they read (bibtexparser's splitter quotes a block it could not read). Their
# loggers never write, at any level; the child's own output goes nowhere (backend/reading.py).
for _name in ("bibtexparser", "rispy", "pylatexenc"):
    logging.getLogger(_name).setLevel(logging.CRITICAL + 1)


def parse(data, beat=lambda: None):
    """(records, skipped, format): the file's records as CSL JSON, the count of records skipped by
    reason, and the format it was read as. Raises Unreadable: unsupported_reference_file (no format
    read), unreadable_file (not text, or not the format it looks like), too_many_records or
    no_records."""
    if len(data) > MAX_BYTES:
        raise Unreadable("reference_file_too_large")
    text = _decode(data)
    del data
    if not text.strip():
        raise Unreadable("no_records")
    lined = _BREAKS.sub("\n", text)  # one line end, so a record counted is a record read
    kind = detect(lined)
    if kind is None:
        raise Unreadable("unsupported_reference_file")
    reader = {"ris": _ris, "bibtex": _bibtex, "csl_json": _csl_json, "noteexpress": _noteexpress,
              "endnote": _endnote}[kind]
    records, skipped, chars = [], {}, 0
    # Each record kept within its bounds as it comes. JSON keeps its own text: a line end in its strings is text.
    for done, record in enumerate(reader(text if kind == "csl_json" else lined, beat), start=1):
        if done % BEAT_EVERY == 0:
            beat()
        csl = None if record is None else _finish(record)
        if csl is None:
            reason = "unreadable" if record is None else "no_title"
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        chars += size(csl)
        if chars > MAX_CHARS:  # macros a file's entries share can make its records larger than the file
            raise Unreadable("reference_file_too_large")
        records.append(csl)
    if not records and not skipped:
        raise Unreadable("no_records")
    return records, skipped, kind


def size(csl):
    """The characters a record's text holds: the records of one file hold MAX_CHARS at most."""
    return sum(len(value) for value in csl.values() if isinstance(value, str)) \
        + sum(len(part) for author in csl.get("author", ()) for part in author.values())


def _decode(data):
    """The file's text: UTF-16 by its mark, else UTF-8 (its mark dropped), else GB18030."""
    encodings = ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise Unreadable("unreadable_file")


def detect(text):
    """The format a file's text is in, by its content, or None."""
    if re.match(r"\s*(?:\[|\{\s*[\"}])", text):  # a JSON list, or an object of named fields
        return "csl_json"
    if re.search(r"^\{Reference Type\}[ \t]*:", text, re.MULTILINE):
        return "noteexpress"
    if re.search(r"^TY  -", text, re.MULTILINE):
        return "ris"
    if re.search(r"^%0 ", text, re.MULTILINE):
        return "endnote"
    if re.search(r"^[ \t]*@[A-Za-z]+[ \t]*[{(]", text, re.MULTILINE):  # [ \t], never \s: no line's spaces read twice
        return "bibtex"
    return None


def _counted(count):
    if count > MAX_RECORDS:
        raise Unreadable("too_many_records")


# Each reader yields a file's records one at a time, each {CSL key: a value as the file has it}, its
# authors a list of names as text or as {"family", "given"} (None for a block it could not read).
# _finish cleans and bounds it.


def _lines(text, beat):
    """The text's lines, one at a time (never all of them held), a beat every so many."""
    for number, line in enumerate(io.StringIO(text)):
        if number % (BEAT_EVERY * 20) == 0:
            beat()
        yield line.rstrip("\n")


def _records(lines, starts, tagged, ends=lambda line: False):
    """Each record's lines, one record at a time: from a line starts() says begins one to the next such
    line, or to a line ends() says closes it. A record takes a line tagged() takes (a field) only while
    it holds fewer than RECORD_LINES lines, and each field at most CONTINUATION_LINES lines after its first, of
    CONTINUATION_CHARS characters together; a field left out takes its lines with it. Lines outside
    a record are left out."""
    record, after, chars = None, 0, 0
    for line in lines:
        if starts(line):
            if record:
                yield record
            record, after, chars = [line], 0, 0
        elif record is None:
            continue
        elif ends(line):
            yield record
            record = None
        elif tagged(line):
            kept = len(record) < RECORD_LINES
            if kept:
                record.append(line)
            after, chars = (0, 0) if kept else (CONTINUATION_LINES, CONTINUATION_CHARS)
        elif (after := after + 1) <= CONTINUATION_LINES and (chars := chars + len(line)) <= CONTINUATION_CHARS:
            record.append(line)
    if record:
        yield record


def _ris(text, beat):
    import rispy

    _counted(len(re.findall(r"^TY  -", text, re.MULTILINE)))
    parser = rispy.RisParser(skip_unknown_tags=True)
    tag = re.compile(r"[A-Z][A-Z0-9]  -")
    for lines in _records(_lines(text, beat), lambda line: line.startswith("TY  -"), tag.match,
                          lambda line: line.startswith("ER  -")):
        try:  # one record at a time, closed here whether or not the file closed it
            entry = next(iter(parser.parse_lines(iter([*lines, "ER  - "]))), None)
        except Exception:  # a record it could not take apart: its type alone, never its text, is known here
            entry = None
        if entry is None:
            yield None
            continue
        kind = _first(entry.get("type_of_reference")) or ""
        start, end = _first(entry.get("start_page")), _first(entry.get("end_page"))
        yield {
            "type": _RIS_TYPES.get(kind.strip().upper(), "article"),
            "title": _first(entry.get("title")) or _first(entry.get("primary_title"))
            or _first(entry.get("translated_title")),
            "author": _list(entry.get("authors")) + _list(entry.get("first_authors")),
            "issued": _first(entry.get("year")) or _first(entry.get("publication_year")) or _first(entry.get("date")),
            "container-title": _first(entry.get("secondary_title")) or _first(entry.get("journal_name"))
            or _first(entry.get("alternate_title3")) or _first(entry.get("alternate_title2"))
            or _first(entry.get("alternate_title1")),
            "volume": _first(entry.get("volume")), "issue": _first(entry.get("number")),
            "page": f"{start}-{end}" if start and end else start,
            "publisher": _first(entry.get("publisher")), "DOI": _first(entry.get("doi")),
            "URL": _list(entry.get("urls")), ("ISBN" if kind.strip().upper() in _BOOKS else "ISSN"): _first(entry.get("issn")),
            "language": _first(entry.get("language")),
            "abstract": _first(entry.get("abstract")) or _first(entry.get("notes_abstract")),
            "keyword": _list(entry.get("keywords")),
        }


def _bibtex(text, beat):
    import bibtexparser
    from bibtexparser.middlewares.names import parse_single_name_into_parts, split_multiple_persons_names
    from bibtexparser.model import Entry

    _counted(len(re.findall(r"^[ \t]*@(?!(?:string|comment|preamble)\b)[A-Za-z]+[ \t]*[{(]", text,
                            re.MULTILINE | re.IGNORECASE)))
    # Every block the parser would make, wherever it starts (an @ opens one anywhere), entries or not.
    if len(re.findall(r"@[A-Za-z]+\s*[{(]", text)) > MAX_BLOCKS:
        raise Unreadable("too_many_records")
    try:
        # No middleware: @string macros are resolved here, once each, never by concatenation (#),
        # so a value is never longer than the file.
        library = bibtexparser.parse_string(text, parse_stack=[])
    except Exception:
        raise Unreadable("unreadable_file") from None
    beat()
    strings = {block.key.lower(): _enclosed(block.value) for block in library.strings
               if isinstance(block.value, str)}
    entries, unread, macros, people = list(library.entries), 0, {}, {}
    for block in library.failed_blocks:  # an entry under a key or field name used twice is still read
        if isinstance(block.ignore_error_block, Entry):
            entries.append(block.ignore_error_block)
        else:
            unread += 1
    _counted(len(entries))
    yield from [None] * unread

    def value(fields, *names, limit=TITLE_CHARS):
        """The first of the fields named, a macro resolved, cut to what its bound may keep (a macro
        used by every entry is never copied whole into each), its LaTeX read."""
        for name in names:
            if name in fields and isinstance(fields[name].value, str):
                raw = fields[name].value.strip()
                if raw[:1] in "{\"":
                    return _latex(_enclosed(raw)[:limit * 4])
                if raw:  # a macro (or a number): read once for each bound, however many entries use it
                    if (raw.lower(), limit) not in macros:
                        macros[raw.lower(), limit] = _latex(strings.get(raw.lower(), raw)[:limit * 4])
                    return macros[raw.lower(), limit]
        return None

    def names(text):
        """A field's people, each at most once for each value however many entries share it (a macro)."""
        if text in people:
            return people[text]
        found = people[text] = []
        for name in split_multiple_persons_names(text)[:AUTHORS]:
            if "," not in name and " " not in name.strip():  # one word, as a Han-script name often is: whole
                found.append(name)
                continue
            parts = parse_single_name_into_parts(name, strict=False)
            family, given = " ".join(parts.von + parts.last), " ".join(parts.first + parts.jr)
            found.append({"family": family, "given": given} if family and given else name)
        return found

    for entry in entries:
        beat()  # each entry: a long value's LaTeX takes a moment
        fields = {key.lower(): field for key, field in entry.fields_dict.items()}
        kind = entry.entry_type.lower()
        eprint = value(fields, "eprint")
        arxiv = eprint if eprint and (value(fields, "archiveprefix", "eprinttype") or "").lower() == "arxiv" else None
        number = value(fields, "number")
        yield {
            "type": _BIBTEX_TYPES.get(kind, "article"),
            "title": value(fields, "title"),
            "author": names(value(fields, "author", limit=AUTHORS * NAME_CHARS) or ""),
            "issued": value(fields, "year", "date"),
            "container-title": value(fields, "journal", "journaltitle", "booktitle"),
            "volume": value(fields, "volume"),
            "issue": value(fields, "issue") or (number if kind == "article" else None),
            "page": (value(fields, "pages") or "").replace("--", "-") or None,
            "publisher": value(fields, "publisher", "institution", "school", "organization"),
            "DOI": value(fields, "doi"), "URL": [value(fields, "url")] + ([f"https://arxiv.org/abs/{arxiv}"] if arxiv else []),
            "ISBN": value(fields, "isbn"), "ISSN": value(fields, "issn"),
            "language": value(fields, "language", "langid"),
            "abstract": value(fields, "abstract", limit=ABSTRACT_CHARS),
            "keyword": re.split(r"[,;]", value(fields, "keywords", limit=KEYWORDS * KEYWORD_CHARS) or ""),
            "number": f"arXiv:{arxiv}" if arxiv else (number if kind != "article" else None),
        }


def _enclosed(value):
    """A BibTeX value without its braces or quotes."""
    value = value.strip()
    if len(value) >= 2 and (value[0], value[-1]) in (("{", "}"), ('"', '"')):
        return value[1:-1]
    return value


def _latex(value):
    """LaTeX in a value read as text (accents, escapes, braces), by pylatexenc, which runs nothing."""
    if not _LATEX.search(value):
        return value
    from pylatexenc.latex2text import LatexNodes2Text

    try:
        return LatexNodes2Text(math_mode="text", strict_latex_spaces=False).latex_to_text(value)
    except Exception:  # a value it cannot read is kept as written
        return value


def _csl_json(text, beat):
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser follows
        raise Unreadable("unreadable_file") from None
    if isinstance(found, dict):
        found = [found]
    if not isinstance(found, list):
        raise Unreadable("unreadable_file")
    _counted(len(found))
    for item in found:
        if not isinstance(item, dict):
            yield None
            continue
        issued = item.get("issued")
        parts = issued.get("date-parts") if isinstance(issued, dict) else None
        year = (parts[0][0] if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]
                else issued.get("raw") if isinstance(issued, dict) else issued)
        authors = []
        for author in item.get("author")[:AUTHORS * 4] if isinstance(item.get("author"), list) else []:
            if isinstance(author, dict):
                family, given = author.get("family"), author.get("given")
                authors.append({"family": family, "given": given} if isinstance(family, str) and isinstance(given, str)
                               else family if isinstance(family, str) else author.get("literal"))
        container = item.get("container-title")
        yield {
            "type": item.get("type") if isinstance(item.get("type"), str) and _TYPE.fullmatch(item["type"]) else "article",
            "title": item.get("title"), "author": authors, "issued": str(year) if isinstance(year, int) else year,
            "container-title": container[0] if isinstance(container, list) and container else container,
            **{key: item.get(key) for key in ("volume", "issue", "page", "publisher", "DOI", "ISBN", "ISSN",
                                              "language", "abstract", "number")},
            "URL": item.get("URL"), "keyword": re.split(r"[,;]", item["keyword"]) if isinstance(item.get("keyword"), str) else [],
        }


def _tagged(text, start, line, beat):
    """The records of a tagged format whose record starts with the tag start, one at a time, each a
    list of (tag, value) as the pattern line reads them; a line no tag starts continues the one before."""
    pattern = re.compile(line, re.MULTILINE)
    _counted(sum(1 for found in pattern.finditer(text) if found.group(1) == start))
    for lines in _records(_lines(text, beat), lambda row: (found := pattern.match(row)) is not None
                          and found.group(1) == start, pattern.match):
        fields = []
        for row in lines:
            if (found := pattern.match(row)) is not None:
                fields.append((found.group(1), [found.group(2)]))
            elif row.strip():
                fields[-1][1].append(row.strip())
        yield [(tag, " ".join(parts)) for tag, parts in fields]


def _endnote(text, beat):
    for fields in _tagged(text, "0", r"^%(\S) ?(.*)$", beat):
        one = {}
        for tag, value in fields:
            if tag in ("A", "K", "U"):
                one.setdefault(tag, []).append(value)
            else:
                one.setdefault(tag, value)
        kind = (one.get("0") or "").strip().lower()
        yield {
            "type": _NAMED_TYPES.get(kind, "article"), "title": one.get("T"), "author": one.get("A", []),
            "issued": one.get("D"), "container-title": one.get("J") or one.get("B"), "volume": one.get("V"),
            "issue": one.get("N"), "page": one.get("P"), "publisher": one.get("I"), "DOI": one.get("R"),
            "URL": one.get("U", []), ("ISBN" if _NAMED_TYPES.get(kind) in ("book", "chapter") else "ISSN"): one.get("@"),
            "language": one.get("G"), "abstract": one.get("X"), "keyword": one.get("K", []),
        }


def _noteexpress(text, beat):
    for fields in _tagged(text, "Reference Type", r"^\{([^{}\n]{1,40})\}[ \t]*:[ \t]?(.*)$", beat):
        one = {}
        for tag, value in fields:
            if tag == "Author":  # one a line, or several on one line, separated as CNKI's exports do
                one.setdefault(tag, []).extend(re.split(r"[;；]", value))
            else:
                one.setdefault(tag, value)
        kind = (one.get("Reference Type") or "").strip().lower()
        yield {
            "type": _NAMED_TYPES.get(kind, "article"), "title": one.get("Title"), "author": one.get("Author", []),
            "issued": one.get("Year") or one.get("Date"),
            "container-title": one.get("Journal") or one.get("Secondary Title") or one.get("Tertiary Title"),
            "volume": one.get("Volume"), "issue": one.get("Issue"), "page": one.get("Pages"),
            "publisher": one.get("Publisher"), "DOI": one.get("DOI"), "URL": [one.get("URL")],
            ("ISBN" if _NAMED_TYPES.get(kind) in ("book", "chapter") else "ISSN"): one.get("ISBN/ISSN"),
            "language": one.get("Language"), "abstract": one.get("Abstract"),
            "keyword": re.split(r"[;；,，]", one.get("Keywords") or ""),
        }


def _first(value):
    if isinstance(value, list):
        return next((v for v in value if isinstance(v, str) and v.strip()), None)
    return value if isinstance(value, str) else None


def _list(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return [value] if isinstance(value, str) else []


def _text(value, limit):
    """A field's text: cut before it is worked on, its control characters made spaces and half a
    surrogate pair (JSON's escapes can give one) the replacement character, its whitespace collapsed,
    cut to limit characters; None when nothing visible is left."""
    if isinstance(value, int) and not isinstance(value, bool):  # a volume or page CSL JSON gives as a number
        value = str(value)
    if not isinstance(value, str):
        return None
    value = " ".join(_SURROGATE.sub("\ufffd", _CONTROL.sub(" ", value[:limit * 4])).split())
    return visible(value[:limit])


def _name(value):
    if isinstance(value, dict):
        family, given = _text(value.get("family"), NAME_CHARS), _text(value.get("given"), NAME_CHARS)
        return {"family": family, "given": given} if family and given else {"literal": family} if family else None
    name = _text(value, 2 * NAME_CHARS + 2)
    if name is None:
        return None
    family, comma, given = name.partition(",")
    family, given = _text(family, NAME_CHARS), _text(given, NAME_CHARS)  # each cut, then seen to be visible still
    if comma and family and given:
        return {"family": family, "given": given}
    literal = _text(name, NAME_CHARS)
    return {"literal": literal} if literal else None


def _url(values):
    """The first http or https URL among values, or None; a local path or a file: URL never is one."""
    for value in values if isinstance(values, list) else [values]:
        url = _text(value, URL_CHARS + 1)
        if url and " " not in url and len(url) <= URL_CHARS:  # a longer one is left out, never cut short
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            if parts.scheme.lower() in ("http", "https") and parts.netloc:
                return url
    return None


def _doi(value):
    text = _text(value, 400)
    return clean_doi(_DOI_PREFIX.sub("", text)) if text else None


def _finish(record):
    """A reader's record as CSL JSON within its bounds, or None when it has no visible title."""
    title = _text(record.get("title"), TITLE_CHARS)
    if title is None:
        return None
    csl = {"type": record["type"] if isinstance(record.get("type"), str) and _TYPE.fullmatch(record["type"])
           else "article", "title": title}
    authors = [name for name in map(_name, (record.get("author") or [])[:AUTHORS * 4]) if name][:AUTHORS]
    if authors:
        csl["author"] = authors
    if isinstance(record.get("issued"), str) and (year := _YEAR.search(record["issued"][:100])):
        csl["issued"] = {"date-parts": [[int(year.group(1))]]}
    if venue := _text(record.get("container-title"), TITLE_CHARS):
        csl["container-title"] = venue
    for key in _SHORT:
        if text := _text(record.get(key), FIELD_CHARS):
            csl[key] = text
    if doi := _doi(record.get("DOI")):
        csl["DOI"] = doi
    if url := _url(record.get("URL")):
        csl["URL"] = url
    if abstract := _text(record.get("abstract"), ABSTRACT_CHARS):
        csl["abstract"] = abstract
    keywords = [k for k in (_text(k, KEYWORD_CHARS) for k in (record.get("keyword") or [])[:KEYWORDS * 4]) if k]
    if keywords:
        csl["keyword"] = ", ".join(dict.fromkeys(keywords[:KEYWORDS]))
    if "number" in csl and (arxiv := _ARXIV_IN.search(csl["number"])) and not ARXIV.fullmatch(arxiv.group(1)):
        del csl["number"]  # it names an arXiv ID the pattern does not take: not kept as one
    return csl


def identifier(csl):
    """The identifier a record gives (slice-1 spec section 3, F3a: from a reference file, validated
    by its pattern): ("doi", DOI), else ("arxiv", ID) from its number or its arxiv.org URL, or None."""
    if isinstance(csl.get("DOI"), str) and (doi := clean_doi(csl["DOI"])):
        return "doi", doi
    for key in ("number", "URL"):
        if isinstance(csl.get(key), str) and (found := _ARXIV_IN.search(csl[key][:URL_CHARS])) \
                and ARXIV.fullmatch(found.group(1)):
            return "arxiv", found.group(1)
    return None


def valid(csl):
    """Whether a record, as the app gets it from the child, is one _finish makes: CSL JSON of the
    KEYS only, each of its type and within its bound, with a visible title. Anything else is a
    wrong frame."""
    def text(value, limit):
        return type(value) is str and 0 < len(value) <= limit and visible(value) is not None \
            and not _SURROGATE.search(value)  # none the database or a JSON answer could not encode

    def name(author):
        return type(author) is dict and (list(author) == ["literal"] and text(author["literal"], NAME_CHARS)
                                         or set(author) == {"family", "given"} and text(author["family"], NAME_CHARS)
                                         and text(author["given"], NAME_CHARS))

    def year(issued):
        parts = issued.get("date-parts") if type(issued) is dict and list(issued) == ["date-parts"] else None
        return (type(parts) is list and len(parts) == 1 and type(parts[0]) is list and len(parts[0]) == 1
                and type(parts[0][0]) is int and 1000 <= parts[0][0] <= 2200)

    if type(csl) is not dict or not set(csl) <= KEYS or type(csl.get("type")) is not str \
            or not _TYPE.fullmatch(csl["type"]):
        return False
    bounds = {"title": TITLE_CHARS, "container-title": TITLE_CHARS, "URL": URL_CHARS, "abstract": ABSTRACT_CHARS,
              "keyword": KEYWORDS * (KEYWORD_CHARS + 2), **dict.fromkeys(_SHORT, FIELD_CHARS)}
    return (text(csl.get("title"), TITLE_CHARS) and all(text(csl[key], bound) for key, bound in bounds.items() if key in csl)
            and ("DOI" not in csl or type(csl["DOI"]) is str and clean_doi(csl["DOI"]) == csl["DOI"])
            and ("author" not in csl or type(csl["author"]) is list and 0 < len(csl["author"]) <= AUTHORS
                 and all(map(name, csl["author"])))
            and ("issued" not in csl or year(csl["issued"])))


# CSL types by each format's own names.
_RIS_TYPES = {"JOUR": "article-journal", "JFULL": "article-journal", "EJOUR": "article-journal",
              "MGZN": "article-magazine", "NEWS": "article-newspaper", "BOOK": "book", "EBOOK": "book",
              "EDBOOK": "book", "CHAP": "chapter", "ECHAP": "chapter", "CONF": "paper-conference",
              "CPAPER": "paper-conference", "THES": "thesis", "RPRT": "report", "ELEC": "webpage", "WEB": "webpage",
              "UNPB": "manuscript", "PAT": "patent", "DATA": "dataset", "COMP": "software", "MAP": "map",
              "GEN": "article"}
_BOOKS = {"BOOK", "EBOOK", "EDBOOK", "CHAP", "ECHAP"}
_BIBTEX_TYPES = {"article": "article-journal", "book": "book", "mvbook": "book", "inbook": "chapter",
                 "incollection": "chapter", "inproceedings": "paper-conference", "conference": "paper-conference",
                 "proceedings": "book", "phdthesis": "thesis", "mastersthesis": "thesis", "thesis": "thesis",
                 "techreport": "report", "report": "report", "manual": "report", "unpublished": "manuscript",
                 "online": "webpage", "dataset": "dataset", "software": "software", "patent": "patent"}
# EndNote's and NoteExpress's reference types (NoteExpress's in English or Chinese), lower-cased.
_NAMED_TYPES = {"journal article": "article-journal", "electronic article": "article-journal",
                "magazine article": "article-magazine", "newspaper article": "article-newspaper", "book": "book",
                "edited book": "book", "electronic book": "book", "book section": "chapter",
                "conference paper": "paper-conference", "conference proceedings": "paper-conference",
                "thesis": "thesis", "report": "report", "web page": "webpage", "patent": "patent",
                "unpublished work": "manuscript", "dataset": "dataset", "computer program": "software",
                "期刊文章": "article-journal", "图书": "book", "书籍": "book", "图书章节": "chapter", "学位论文": "thesis",
                "会议论文": "paper-conference", "报纸文章": "article-newspaper", "报告": "report", "专利": "patent",
                "电子文献": "webpage"}
