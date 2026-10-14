"""Reading a reference file into CSL JSON records (slice-1 spec F3b and section 18; ticket 06's
answer: "normalized to CSL JSON with provenance").

`parse(data, beat)` reads RIS (rispy), BibTeX and BibLaTeX (bibtexparser 2, LaTeX in values read by
pylatexenc), CSL JSON, NoteExpress and EndNote tagged files (the standard library), each found by
its content, never by its name, in UTF-8 (with or without its mark), UTF-16 with its mark, or
GB18030. It returns the records as CSL JSON and what it skipped, counted by reason: a record with no
visible title, a record it could not read (a block the parser could not read, or a BibTeX value it
cannot read as written), and a kept record's value left out as not what its field takes (a year,
DOI, URL or arXiv ID: invalid_field). It runs in a child process (backend/reading.py
`references`), which sends each record to the app, and `valid(csl)` is the app's check of what it
gets. It never imports the database, the server or the window.

Every bound holds before a value is worked on: the file's size (MAX_BYTES), its records
(MAX_RECORDS; more refuse the whole file), each field's characters (a value is cut before it is
cleaned, and kept at most to its field's bound; a BibTeX value longer than four times its bound, or
whose macros expand past that, skips its record instead, as its LaTeX could read as far less), a field's continuation lines, and the authors and
keywords of a record. A record keeps only what CSL names here (KEYS); what a record points to is
never read: file attachments (RIS L1, L2 and L4, EndNote %>, BibTeX file) are not kept, and a URL is
kept only when it is http or https, never fetched. The values an identifier comes from (a DOI, a URL,
a number naming arXiv, a BibTeX eprint, the last two read as written, not as LaTeX) are checked whole,
as the file gives them, before anything is cut or cleaned. A DOI is kept only when it fits the pattern
(extraction.clean_doi), an arXiv ID only as extraction.ARXIV reads one, and only whole: a BibTeX
eprint that is the ID and nothing else, a number that is "arXiv:" and the ID (and its category), an
arxiv.org URL whose whole path is the ID's page. A BibTeX field given twice is read as its first. Names are kept as the file
gives them: "Family, Given" splits at its comma (BibTeX's by its name parts, split on the text as
written so a braced group is one name), and any other name, a Han-script one included, is kept whole
(literal), as is CSL JSON's literal. BibTeX's macros (aliases, # and the month names) are expanded
as BibTeX reads them, in file order (a value sees the definitions before it, a later one replacing an
earlier), within MACRO_DEPTH and MACRO_CHARS, or the record is skipped; its literal fields (url, doi, eprint) keep their characters, TeX's escapes of _ & % # $
read as those characters. Nothing here logs content, and the parsers are kept from logging it.
"""

import io
import json
import logging
import re
from urllib.parse import urlsplit

from backend.extraction import ARXIV, DOI_CHARS, Unreadable, clean_doi
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
MACRO_DEPTH = 10  # a BibTeX macro's macros, and theirs, this deep at most (files use one or two)
MACRO_CHARS = 4 * max(ABSTRACT_CHARS, AUTHORS * NAME_CHARS)  # a longer macro is refused: past what any field takes
EXPANDED_CHARS = MAX_BYTES  # what a file's macros expand to, all of it together: no more than the file may hold
BEAT_EVERY = 500  # records or lines between two beats
PARSER = ("references", "1")  # what the child names when it is ready (backend/reading.py)
FORMATS = ("ris", "bibtex", "csl_json", "noteexpress", "endnote")
SKIPPED = ("no_title", "unreadable", "invalid_field")  # records, records, and a kept record's values left out
KEYS = {"type", "title", "author", "issued", "container-title", "volume", "issue", "page", "publisher", "DOI",
        "URL", "ISBN", "ISSN", "language", "abstract", "keyword", "number"}
_SHORT = ("volume", "issue", "page", "publisher", "ISBN", "ISSN", "language", "number")
_TYPE = re.compile(r"[a-z]{1,30}(?:-[a-z]{1,30}){0,3}")  # a CSL type name, as backend/lookup.py reads one
_YEAR = re.compile(r"(?<!\d)(1\d{3}|2[01]\d{2}|2200)(?!\d)")  # the details form's years, 1000 to 2200
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
# A number that names an arXiv ID is that ID and nothing else: "arXiv:" and the ID, its version, and the
# category arXiv's own exports add ("arXiv:2101.00001v2 [cs.CL]"); extraction.ARXIV then checks the ID.
_ARXIV_NUMBER = re.compile(r"\s*arxiv\s*:\s*(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?:\s*\[[a-z.-]{1,20}\])?\s*",
                           re.IGNORECASE | re.ASCII)  # ASCII: a digit is 0 to 9, as in every arXiv ID
_ARXIV_NAMED = re.compile(r"arxiv\s*:|arxiv\.org/", re.IGNORECASE)  # a value that says it names one
_ARXIV_WHOLE = re.compile(rf"(?:{ARXIV.pattern})(?:v\d+)?", re.IGNORECASE | re.ASCII)  # an eprint: the ID and nothing else
_ARXIV_PREFIX = re.compile(r"^arxiv\s*:\s*", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"V(\d+)$")  # arXiv writes a version with a lower-case v
_LATEX = re.compile(r"[\\{}$~]")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")  # Unicode's control characters (Cc)
_SURROGATE = re.compile("[\ud800-\udfff]")  # half a pair, which only JSON's escapes can give: no text holds one
# bibtexparser's block start (splitter._BLOCK_START, `@[\w]*( |\t)*(?=[{(])`, a test holds them equal), its
# run of spaces a plain repeat here: the parser's group keeps state for each space it passes.
_BLOCK_START = re.compile(r"@\w*[ \t]*(?=[{(])")
_BLOCK_SPACES = re.compile(r"(@\w*)[ \t]{2,}")
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
        csl, dropped = (None, 0) if record is None else _finish(record)
        if csl is None:
            reason = "unreadable" if record is None else "no_title"
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        if dropped:
            skipped["invalid_field"] = skipped.get("invalid_field", 0) + dropped
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
    if re.search(r"^[^\S\n]*@[A-Za-z]+[ \t]*[{(]", text, re.MULTILINE):  # never \s: no line's spaces read twice
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
    CONTINUATION_CHARS characters together (the line that passes them is cut, never left out whole, so a
    field that went on still shows it did: a DOI with text after it is no DOI); a blank line adds nothing
    and is left out; a field left out takes its lines with it. Lines outside a record are left out."""
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
        elif not line.strip():
            continue
        elif (after := after + 1) <= CONTINUATION_LINES and chars < CONTINUATION_CHARS:
            record.append(line[:CONTINUATION_CHARS - chars])
            chars += len(line)
    if record:
        yield record


def _ris(text, beat):
    import rispy

    _counted(len(re.findall(r"^TY  -", text, re.MULTILINE)))
    parser = rispy.RisParser(skip_unknown_tags=True)
    tag = re.compile(r"[A-Z][A-Z0-9]  -")
    for lines in _records(_lines(text, beat), lambda line: line.startswith("TY  -"), tag.match,
                          lambda line: line.startswith("ER  -")):
        lines = _joined(lines, tag, "UR  -")
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


def _joined(lines, tag, field):
    """A record's lines with each continuation of a field (rispy makes a list tag's continuation an item of
    its own: a URL followed by text would still read as that URL) joined to that field's line."""
    out, joining = [], False
    for line in lines:
        if tag.match(line):
            joining = line.startswith(field)
        elif joining:
            out[-1] = f"{out[-1]} {line.strip()}"
            continue
        out.append(line)
    return out


def _bibtex(text, beat):
    import bibtexparser
    from bibtexparser.middlewares.names import parse_single_name_into_parts, split_multiple_persons_names
    from bibtexparser.model import Entry, ParsingFailedBlock, String

    # Spaces after an @ and its type are one space: the parser's pattern holds state for each space it passes
    # there (a few MiB of them would take the child past its ceiling), and they mean nothing in BibTeX.
    text = _BLOCK_SPACES.sub(r"\1 ", text)
    # bibtexparser opens a block at a mark of its block start wherever one comes between blocks: every such
    # mark is counted as it comes (those inside values too), at least the blocks the parser would make, so
    # past the bound the parser never runs. Its entries are counted exactly once it has made them (below).
    for blocks, _ in enumerate(_BLOCK_START.finditer(text), start=1):
        if blocks > MAX_BLOCKS:
            raise Unreadable("too_many_records")
    try:
        # No middleware: values come as the file writes them (macros, #, braces), read below.
        library = bibtexparser.parse_string(text, parse_stack=[])
    except Exception:
        raise Unreadable("unreadable_file") from None
    beat()
    # BibTeX reads a file in order: an entry, and an @string's own value, see the macros defined before it
    # (the month names from the start), a later @string of the same name (in any case) replacing it from
    # there on. env: name -> (text, depth), or None for a definition that cannot be read as written.
    env, values, people, spent = {}, {}, {}, [0]
    _counted(sum(isinstance(block, Entry) or isinstance(getattr(block, "ignore_error_block", None), Entry)
                 for block in library.blocks))

    def spend(chars):
        """What expanding macros makes, all of it together, holds EXPANDED_CHARS at most: macros
        concatenated, or one long macro read again by many entries, could otherwise make far more than
        the file."""
        spent[0] += chars
        if spent[0] > EXPANDED_CHARS:
            raise Unreadable("reference_file_too_large")

    def expand(parts, cap):
        """(A value's parts joined by #, each macro by its definition at this point of the file, its
        depth). _Refused when a macro is not defined here or its definition could not be read, when they
        nest more than MACRO_DEPTH deep, or when the whole is longer than cap: every part is checked,
        whatever the length so far, and a value past its cap is never cut short and read."""
        texts, length, depth = [], 0, 0
        for literal, part in parts:
            if not literal:
                name = part.lower()
                found = env[name] if name in env else (_MONTHS[name], 0) if name in _MONTHS else None
                if found is None:
                    raise _Refused
                part, depth = found[0], max(depth, found[1] + 1)
            length += len(part)
            if length <= cap:
                texts.append(part)
        if length > cap or depth > MACRO_DEPTH:
            raise _Refused
        return (texts[0] if len(texts) == 1 else "".join(texts)), depth

    def define(block):
        """An @string, read now, against the macros defined before it."""
        name = block.key.lower()
        try:
            if not isinstance(block.value, str):
                raise _Refused
            parts = _parts(block.value.strip())
            text, depth = expand(parts, MACRO_CHARS)
            if len(parts) > 1:
                spend(len(text))
            env[name] = (text, depth)
        except _Refused:
            env[name] = None
        values.clear()  # a value read before may mean something else from here on

    def value(fields, *names, limit=TITLE_CHARS, literal=False):
        """The first of the fields named, as the file means it here: its macros and # expanded, its LaTeX
        read unless it is a literal field (a URL, DOI or eprint keeps the characters it has). _Refused
        when it cannot be read as written, or is longer than four times its field's bound (never cut
        before it is read: the bound itself is applied to the text read)."""
        for name in names:
            if name in fields and isinstance(fields[name].value, str) and (raw := fields[name].value.strip()):
                parts, cap = _parts(raw), limit * 4
                if len(parts) == 1 and parts[0][0]:  # braced, quoted or a number: the file's own text
                    text, _ = expand(parts, cap)
                    text = text if literal else _latex(text)
                else:
                    if (raw, cap, literal) not in values:  # a macro every entry uses is expanded and read once
                        text, _ = expand(parts, cap)
                        spend(len(text))
                        values[raw, cap, literal] = text if literal else _latex(text)
                    text = values[raw, cap, literal]
                if text.strip():  # an empty one ({}) gives way to the next field named
                    return text
        return None

    def names(text):
        """A field's people, split on its text as written: a braced group is one name (an organization,
        say), and each name's parts are read for LaTeX once they are apart. Each value is read once."""
        if text in people:
            return people[text]
        found = people[text] = []
        for name in split_multiple_persons_names(text)[:AUTHORS]:
            name = name.strip()
            group = name[:1] == "{" and _closing(name, 0) == len(name) - 1
            if group or not re.search(r"[,\s]", name):  # one group, or one word as a Han-script name often is: whole
                found.append({"literal": _latex(name[1:-1] if group else name)})
                continue
            parts = parse_single_name_into_parts(name, strict=False)
            family, given = _latex(" ".join(parts.von + parts.last)), _latex(" ".join(parts.first + parts.jr))
            found.append({"family": family, "given": given} if family.strip() and given.strip()
                         else {"literal": family if family.strip() else _latex(name)})
        return found

    for block in library.blocks:
        found = getattr(block, "ignore_error_block", None) if isinstance(block, ParsingFailedBlock) else block
        if isinstance(found, String):  # a macro defined again is read as BibTeX reads it, in its place
            define(found)
            continue
        if isinstance(block, ParsingFailedBlock) and not isinstance(found, Entry):
            yield None  # a block the parser could not read
            continue
        if not isinstance(found, Entry):  # a comment or a preamble
            continue
        entry = found
        beat()  # each entry: a long value's LaTeX takes a moment
        fields = {}
        for field in entry.fields:  # a field given twice (in any case): BibTeX reads the first, and so here
            fields.setdefault(field.key.lower(), field)
        kind = entry.entry_type.lower()
        try:
            eprint = _ARXIV_VERSION.sub(r"v\1", _ARXIV_PREFIX.sub("", (_escapes(value(fields, "eprint", literal=True)) or "").strip()))
            arxiv = eprint if eprint and (value(fields, "archiveprefix", "eprinttype") or "").lower() == "arxiv" else None
            valid = bool(arxiv) and _ARXIV_WHOLE.fullmatch(arxiv) is not None
            written = _escapes(value(fields, "number", literal=True))  # an identifier's field: as written
            number = written if written and _ARXIV_NAMED.search(written) else value(fields, "number")
            record = {
                "type": _BIBTEX_TYPES.get(kind, "article"),
                "title": value(fields, "title"),
                "author": names(value(fields, "author", limit=AUTHORS * NAME_CHARS, literal=True) or ""),
                "issued": value(fields, "year", "date"),
                "container-title": value(fields, "journal", "journaltitle", "booktitle"),
                "volume": value(fields, "volume"),
                "issue": value(fields, "issue") or (number if kind == "article" else None),
                "page": (value(fields, "pages") or "").replace("--", "-") or None,
                "publisher": value(fields, "publisher", "institution", "school", "organization"),
                "DOI": _escapes(value(fields, "doi", literal=True)),
                "URL": [_escapes(value(fields, "url", literal=True))] + ([f"https://arxiv.org/abs/{arxiv}"] if valid else []),
                "ISBN": value(fields, "isbn"), "ISSN": value(fields, "issn"),
                "language": value(fields, "language", "langid"),
                "abstract": value(fields, "abstract", limit=ABSTRACT_CHARS),
                "keyword": [_latex(k) for k in _split_top(value(fields, "keywords", limit=KEYWORDS * KEYWORD_CHARS,
                                                                literal=True) or "")[:KEYWORDS * 4]],  # as _finish keeps
                # an arXiv eprint the whole of which is no arXiv ID is left out, and counted
                "number": f"arXiv:{arxiv}" if valid else (number if kind != "article" else None),
                "number_written": None if valid or kind == "article" else written,  # what _finish checks
                "dropped": 1 if arxiv and not valid else 0,
            }
        except _Refused:  # a value it cannot read as written: the record is skipped, never read wrong
            record = None
        yield record


class _Refused(Exception):
    """A BibTeX value that cannot be read as written: malformed, longer than its cap, or a macro not
    defined before it (or whose own definition could not be read), or nested past MACRO_DEPTH."""


_DELIMITERS = re.compile(r'[{}"]')


def _closing(value, start):
    """Where the brace or quote opening value at start closes (braces inside it balanced), or None."""
    depth, at = 0, start + 1
    while found := _DELIMITERS.search(value, at):
        at = found.end()
        if found.group() == "{":
            depth += 1
        elif found.group() == "}":
            if depth == 0:
                return found.start() if value[start] == "{" else None
            depth -= 1
        elif depth == 0 and value[start] == '"':
            return found.start()
    return None


def _parts(raw):
    """A BibTeX value's parts, [(literal, text)]: braced or quoted text without its delimiters, or a
    number (each literal), or a macro's name, joined by #. _Refused when it is not that."""
    parts, at = [], 0
    while True:
        at = _SPACES.match(raw, at).end()
        if at == len(raw):
            raise _Refused  # nothing, or nothing after a #
        if raw[at] in '{"':
            end = _closing(raw, at)
            if end is None:
                raise _Refused
            parts.append((True, raw[at + 1:end]))
            at = end + 1
        else:
            word = _WORD.match(raw, at).group()
            if not word:
                raise _Refused
            parts.append((word.isascii() and word.isdigit(), word))
            at += len(word)
        at = _SPACES.match(raw, at).end()
        if at == len(raw):
            return parts
        if raw[at] != "#":
            raise _Refused
        at += 1


def _split_top(value):
    """A BibTeX keywords field's keywords: split at its commas and semicolons outside braces, on the text
    as written (a braced group is one keyword), before its LaTeX is read."""
    found, start, depth = [], 0, 0
    for mark in _KEYWORD_MARKS.finditer(value):
        if mark.group() == "{":
            depth += 1
        elif mark.group() == "}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            found.append(value[start:mark.start()])
            start = mark.end()
    return found + [value[start:]]


_KEYWORD_MARKS = re.compile(r"[{},;]")


def _escapes(value):
    """A literal field's text with TeX's escapes of _ & % # $ read as those characters, as exports write
    them in a URL or DOI (where a backslash cannot stand); anything else is kept as written."""
    return _ESCAPED.sub(r"\1", value) if value and "\\" in value else value


_ESCAPED = re.compile(r"\\([_&%#$])")
_WORD = re.compile(r"[^\s#{}\"]*")
_SPACES = re.compile(r"\s*")
_MONTHS = {name[:3].lower(): name for name in ("January", "February", "March", "April", "May", "June", "July",
                                                 "August", "September", "October", "November", "December")}


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
                authors.append({"family": family, "given": given} if isinstance(family, str)
                               else {"literal": author.get("literal")})
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
    if isinstance(value, dict) and "literal" in value:  # a name given whole: an organization's commas are its own
        literal = _text(value["literal"], NAME_CHARS)
        return {"literal": literal} if literal else None
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
    """The first http or https URL among values, or None, and how many of the values given were not
    one: a local path or a file: URL never is, nor a longer one (left out, never cut short), nor one
    with a backslash (LaTeX's escapes are no part of a URL)."""
    found, wrong = None, 0
    for value in values if isinstance(values, list) else [values]:
        if not isinstance(value, str) or visible(value) is None:
            continue
        url = value.strip()  # checked whole, as the file gives it: never cut or cleaned first
        try:
            parts = urlsplit(url) if len(url) <= URL_CHARS and not _NOT_IN_URL.search(url) else None
        except ValueError:
            parts = None
        if parts and parts.scheme.lower() in ("http", "https") and parts.netloc:
            found = found or url
        else:
            wrong += 1
    return found, wrong


_NOT_IN_URL = re.compile("[\\s\\\\\x00-\x1f\x7f-\x9f\ud800-\udfff]")  # spaces, backslashes, controls, half pairs


def _doi(value):
    """(The DOI a value is, whole, or None; 1 when a value was given and is not one, else 0.) Only its
    surrounding spaces and a doi.org or doi: prefix are taken off, and only its case is changed: a DOI
    the pattern takes followed by anything else, spaces included, is no DOI, nor one of other than ASCII
    characters (a digit of another script, a letter that lower-cases to an ASCII one)."""
    if not isinstance(value, str) or visible(value) is None:
        return None, 0
    text = _DOI_PREFIX.sub("", value.strip())
    doi = clean_doi(text) if len(text) <= DOI_CHARS else None
    return (doi, 0) if doi and text.isascii() and doi == text.lower() else (None, 1)


def _finish(record):
    """(A reader's record as CSL JSON within its bounds, or None when it has no visible title; how many
    of its values were left out as not what their field takes: a year, a DOI, a URL, an arXiv ID)."""
    title = _text(record.get("title"), TITLE_CHARS)
    if title is None:
        return None, 0
    dropped = record.get("dropped", 0)  # what a reader left out itself (BibTeX: an eprint no arXiv ID)
    csl = {"type": record["type"] if isinstance(record.get("type"), str) and _TYPE.fullmatch(record["type"])
           else "article", "title": title}
    authors = [name for name in map(_name, (record.get("author") or [])[:AUTHORS * 4]) if name][:AUTHORS]
    if authors:
        csl["author"] = authors
    if isinstance(record.get("issued"), str) and (year := _YEAR.search(record["issued"][:100])):
        csl["issued"] = {"date-parts": [[int(year.group(1))]]}
    elif _text(record.get("issued"), 100):
        dropped += 1
    if venue := _text(record.get("container-title"), TITLE_CHARS):
        csl["container-title"] = venue
    for key in _SHORT:
        if key != "number" and (text := _text(record.get(key), FIELD_CHARS)):
            csl[key] = text
    # The values an identifier comes from (a number naming arXiv, a DOI, a URL) are checked whole, as the
    # file gives them, before anything is cut or cleaned; one that is not what its field takes is left out.
    # A number names arXiv when the file's text does or the text kept would (after LaTeX is read, control
    # characters made spaces); then the file's own text, whole, must be "arXiv:" and an ID, or it is left out.
    number, text = record.get("number"), _text(record.get("number"), FIELD_CHARS)
    written = record.get("number_written") if isinstance(record.get("number_written"), str) else number
    if isinstance(written, str) and _ARXIV_NAMED.search(written) or text and _ARXIV_NAMED.search(text):
        whole = written.strip() if isinstance(written, str) else ""
        found = _ARXIV_NUMBER.fullmatch(whole) if len(whole) <= 100 and not _CONTROL.search(whole) else None
        if found and ARXIV.fullmatch(found.group(1)):
            csl["number"] = " ".join(whole.split())
        else:
            dropped += 1  # it names an arXiv ID the pattern does not take: not kept as one
    elif text:
        csl["number"] = text
    doi, wrong = _doi(record.get("DOI"))
    dropped += wrong
    if doi:
        csl["DOI"] = doi
    url, wrong = _url(record.get("URL"))
    dropped += wrong
    if url:
        csl["URL"] = url
    if abstract := _text(record.get("abstract"), ABSTRACT_CHARS):
        csl["abstract"] = abstract
    keywords = [k for k in (_text(k, KEYWORD_CHARS) for k in (record.get("keyword") or [])[:KEYWORDS * 4]) if k]
    if keywords:
        csl["keyword"] = ", ".join(dict.fromkeys(keywords[:KEYWORDS]))
    return csl, dropped


def identifier(csl):
    """The identifier a record gives (slice-1 spec section 3, F3a: from a reference file, validated
    by its pattern): ("doi", DOI), else ("arxiv", ID) from its number or its arxiv.org URL, or None."""
    if isinstance(csl.get("DOI"), str) and (doi := clean_doi(csl["DOI"])):
        return "doi", doi
    found = _ARXIV_NUMBER.fullmatch(csl["number"]) if isinstance(csl.get("number"), str) else None
    if found is None and isinstance(csl.get("URL"), str):  # an arxiv.org URL whose whole path is an ID's page
        try:
            url = urlsplit(csl["URL"])
            found = _ARXIV_PATH.fullmatch(url.path) if (url.hostname or "") in _ARXIV_HOSTS else None
        except ValueError:
            found = None
    return ("arxiv", found.group(1)) if found and ARXIV.fullmatch(found.group(1)) else None


_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_ARXIV_PATH = re.compile(r"/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?/?", re.IGNORECASE | re.ASCII)


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
