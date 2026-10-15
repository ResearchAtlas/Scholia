"""Reading reference files into CSL JSON (slice-1 spec F3b; ticket 06's answer; S1-14), in this process
(backend/reference_files.py) and in the reading child (backend/reading.py references), on synthetic
files made by the tests. Hostile and malformed input first: the file is untrusted."""

import builtins
import json
import os
import pathlib

import pytest

from backend import extraction, reading
from backend import reference_files as rf
import synthetic_materials as synthetic


def parse(text):
    return rf.parse(text.encode() if isinstance(text, str) else text)


def unreadable(data):
    with pytest.raises(extraction.Unreadable) as raised:
        rf.parse(data if isinstance(data, bytes) else data.encode())
    return raised.value.code


# Malformed and hostile input


@pytest.mark.parametrize("data, code", [
    (b"", "no_records"), (b"  \n\t", "no_records"), (b"hello world, no format", "unsupported_reference_file"),
    (b"%PDF-1.7\n\xff\xfe\x00binary", "unreadable_file"), (b"\x80\x81\xff\xfe\x00\x01", "unreadable_file"),
    (b"[1, 2", "unreadable_file"), (b"[" * 100_000, "unreadable_file"), (b'"a string"', "unsupported_reference_file"),
])
def test_a_file_that_is_no_reference_file_is_refused_by_code(data, code):
    assert unreadable(data) == code


def test_a_file_past_the_size_bound_is_refused_before_it_is_read():
    assert unreadable(b"TY  - JOUR\n" + b"x" * rf.MAX_BYTES) == "reference_file_too_large"


@pytest.mark.parametrize("record, start", [("TY  - JOUR\nTI  - T\nER  - \n", "TY  -"), ("%0 Book\n%T T\n\n", "%0"),
                                            ("{Reference Type}: Book\n{Title}: T\n", "{Reference Type}"),
                                            ("@misc{k, title={T}}\n", "@misc")])
def test_more_records_than_the_bound_refuse_the_whole_file(record, start):
    text = record * (rf.MAX_RECORDS + 1)
    assert text.count(start) == rf.MAX_RECORDS + 1
    assert unreadable(text) == "too_many_records"
    assert len(parse(record * rf.MAX_RECORDS)[0]) == rf.MAX_RECORDS


def test_too_many_csl_json_items_refuse_the_whole_file():
    assert unreadable(json.dumps([{"title": "T"}] * (rf.MAX_RECORDS + 1))) == "too_many_records"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "gb18030"])
def test_each_encoding_is_read_and_mixed_line_endings_too(encoding):
    text = "TY  - JOUR\r\nTI  - 合成数据的研究\rAU  - 张三\nPY  - 2021\r\nER  - \r\n"
    [record], skipped, kind = parse(text.encode(encoding))
    assert (record["title"], record["author"], kind, skipped) == ("合成数据的研究", [{"literal": "张三"}], "ris", {})


def test_utf16_without_its_mark_is_not_guessed():  # its NULs are UTF-8, so it reads as no format
    assert unreadable("TY  - JOUR\nTI  - T\nER  - \n".encode("utf-16-le")) == "unsupported_reference_file"


def test_a_ris_record_without_its_end_tag_is_still_read():
    records, _, _ = parse("TY  - JOUR\nTI  - First\nER  - \nTY  - BOOK\nTI  - Last, no end\n")
    assert [r["title"] for r in records] == ["First", "Last, no end"]


def test_bibtex_blocks_it_cannot_read_are_counted_and_the_rest_imported():
    text = "@article{a, title={Fine one}, year=2020}\n@article{bad, title={unbalanced, year=2020\n" \
           "@book{b, title={Fine two}}\n@article{a, title={Same key, read too}}\n"
    records, skipped, kind = parse(text)
    assert kind == "bibtex" and skipped == {"unreadable": 1}
    assert sorted(r["title"] for r in records) == ["Fine one", "Fine two", "Same key, read too"]


# BibTeX macros (Harold's reading, 2026-10-15): one literal, a number, or one macro name (a month, or an
# @string defined once, before its use, as one literal); anything else refuses the record.


def test_bibtex_months_and_a_macro_defined_once_before_its_use_as_one_literal():
    text = ('@string{jss = {Journal of Synthetic Studies}}\n@string{Year = "2020"}\n'
            '@article{k, title = "T", journal = JSS, year = year, volume = 7, abstract = mar}\n'
            '@misc{l, title = {T2}, journal = jss}\n')
    first, second = parse(text)[0]
    assert (first["container-title"], first["issued"], first["volume"], first["abstract"]) == (
        "Journal of Synthetic Studies", {"date-parts": [[2020]]}, "7", "March")
    assert second["container-title"] == "Journal of Synthetic Studies"


@pytest.mark.parametrize("strings, value", [
    ('@string{prefix = {A }}\n', "prefix # {study}"),  # concatenation
    ('@string{a = {x}}\n@string{b = a}\n', "b"),  # an alias
    ('@string{a = "x" # "y"}\n', "a"),  # a definition that is no one literal
    ("", "nowhere"),  # undefined
    ('@string{a = {x}}\n@string{a = {x}}\n', "a"),  # defined twice, alike
    ('@string{a = {x}}\n@string{A = {y}}\n', "a"),  # defined twice (names are case-insensitive)
    ('@string{a = "x"}\n@string{a = a # "y"}\n', "a"),
])
def test_a_bibtex_value_outside_what_is_read_refuses_its_record(strings, value):
    records, skipped, _ = parse(f"{strings}@misc{{bad, title = {value}}}\n@misc{{good, title = {{Kept}}}}\n")
    assert [r["title"] for r in records] == ["Kept"] and skipped == {"unreadable": 1}


@pytest.mark.parametrize("text", [
    "@misc{before, title = a}\n@string{a = {x}}\n",  # defined after its use
    "@misc{before, title = jan}\n@string{jan = {Janvier}}\n",  # a month an @string defines, used before it
    "@string{a = {x}}\n@misc{before, title = a}\n@string{a = {y}}\n",  # defined again after the use
])
def test_a_macro_defined_after_its_use_or_twice_refuses_its_record(text):
    records, skipped, _ = parse(text + "@misc{good, title = {Kept}}\n")
    assert [r["title"] for r in records] == ["Kept"] and skipped == {"unreadable": 1}


@pytest.mark.parametrize("text", [
    "@string{a={Old}}\n@string{a={Broken}\n@misc{k,title=a}\n",  # recheck 3's finding 11
    "@string{jan={Broken}\n@misc{k,title=jan}\n",  # a malformed month leaves no January behind
    "@string{x={X}}\n@STRING{y={Broken}\n@misc{k,title=x}\n",  # every later macro, whichever the block named
])
def test_after_an_unreadable_at_string_block_every_later_macro_use_refuses_its_record(text):
    records, skipped, _ = parse(text + "@misc{good, title = {Kept}}\n")
    assert [r["title"] for r in records] == ["Kept"] and skipped == {"unreadable": 2}  # the block and the record


def test_a_macro_every_entry_uses_is_read_for_latex_once(monkeypatch):
    calls = []
    real = rf._latex
    monkeypatch.setattr(rf, "_latex", lambda text: calls.append(text) or real(text))
    parse("@string{j = {Caf\\'e Journal}}\n" + "".join(f"@misc{{k{i}, title={{T}}, journal=j}}\n" for i in range(50)))
    assert calls.count("Caf\\'e Journal") == 1


@pytest.mark.parametrize("title", [
    "{Known " + "\\relax " * 700 + "tail}",  # one braced value past its cap
    "long",  # a macro past the cap of the field that uses it
])
def test_a_value_past_its_cap_is_refused_never_cut_and_read(title):
    strings = "@string{long = {" + "w" * (rf.TITLE_CHARS * 4 + 1) + "}}\n"
    records, skipped, _ = parse(f"{strings}@misc{{bad, title = {title}}}\n@misc{{good, title = {{Kept}}}}\n")
    assert [r["title"] for r in records] == ["Kept"] and skipped == {"unreadable": 1}


def test_a_macro_longer_than_any_field_takes_is_refused_never_cut():
    shared = "@string{m = {\\'e " + "w" * (rf.MAX_BYTES // 2) + "}}\n"
    entries = "".join(f"@misc{{k{i}, title={{T{i}}}, author=m, abstract=m}}\n" for i in range(5000))
    records, skipped, _ = parse(shared + entries)
    assert records == [] and skipped == {"unreadable": 5000}


def test_bibtex_names_are_split_on_their_text_as_written_then_read():
    text = (r'@misc{k, title={T}, author={{Research and Development Council} and {van der} Berg and '
            r'M{\"u}ller, J{\"o}rg and {Barnes and Noble, Inc.} and Ludwig van Beethoven and {\v{S}}koda, Ji{\v{r}}{\'\i}'
            r' and 王五}}')
    [record], _, _ = parse(text)
    assert record["author"] == [
        {"literal": "Research and Development Council"}, {"family": "Berg", "given": "van der"},  # as BibTeX splits it
        {"family": "Müller", "given": "Jörg"}, {"literal": "Barnes and Noble, Inc."},
        {"family": "van Beethoven", "given": "Ludwig"}, {"family": "Škoda", "given": "Jiří"}, {"literal": "王五"}]


def test_only_the_keywords_a_record_keeps_are_read(monkeypatch):
    calls = []
    real = rf._latex
    monkeypatch.setattr(rf, "_latex", lambda text: calls.append(text) or real(text))
    parse("@misc{k, title={T}, keywords={" + ",".join(["\\a"] * 5000) + "}}")
    assert len(calls) <= rf.KEYWORDS * 4 + 1


def test_a_field_given_twice_is_read_as_bibtex_reads_it_the_first():
    [record], _, _ = parse("@misc{k, title={First}, TITLE={Second}, doi={10.5555/first}, doi={10.5555/second}}")
    assert (record["title"], record["DOI"]) == ("First", "10.5555/first")


def test_an_empty_field_gives_way_to_the_next_one_named():
    [record], skipped, _ = parse("@misc{k, title={T}, year={}, date={2020-01-01}, journal={}, booktitle={B}}")
    assert (record["issued"], record["container-title"], skipped) == ({"date-parts": [[2020]]}, "B", {})


def test_keywords_are_split_on_their_text_as_written_then_read():
    [record], _, _ = parse('@misc{k, title={T}, keywords={{\\"u}ber; {a;b}}}')
    assert record["keyword"] == "über, a;b"


def test_a_csl_json_name_given_whole_keeps_its_commas():
    [record], _, _ = parse(json.dumps([{"title": "T", "author": [{"literal": "Barnes and Noble, Inc."}, {"family": "Doe, Ltd"}]}]))
    assert record["author"] == [{"literal": "Barnes and Noble, Inc."}, {"literal": "Doe, Ltd"}]


# Identifiers and URLs (Harold's reading, 2026-10-15): a lookup identifier only from a DOI field or an arXiv
# eprint, its value the field's own text, whole, on one line; a URL display only, whole; else left out, counted.

_PADDED_DOI = "10.5555/example" + " " * 1600 + "garbage"
_PADDED_URL = "https://arxiv.org/abs/2101.00001" + " " * 8100 + "garbage"


@pytest.mark.parametrize("data, doi", [
    ("TY  - JOUR\nTI  - T\nDO  - 10.5555/Example \nER  - \n", "10.5555/example"),  # RIS
    ("%0 Journal Article\n%T T\n%R 10.5555/example\n", "10.5555/example"),  # EndNote
    ("{Reference Type}: Journal Article\n{Title}: T\n{DOI}: 10.5555/example\n", "10.5555/example"),  # NoteExpress
    ('@misc{k, title={T}, doi = "10.5555/a_b--c"}', "10.5555/a_b--c"),  # BibTeX: inside its delimiters, as written
    (json.dumps([{"title": "T", "DOI": "\t10.5555/example\n"}]), "10.5555/example"),  # CSL JSON
    ("TY  - JOUR\nTI  - T\nDO  - 10.5555/same\nDO  - 10.5555/same\nER  - \n", "10.5555/same"),  # given twice alike
])
def test_a_doi_is_its_fields_own_text_whole(data, doi):
    [record], skipped, _ = parse(data)
    assert (rf.identifier(record), record["DOI"], skipped) == (("doi", doi), doi, {})


@pytest.mark.parametrize("data", [
    json.dumps([{"title": "T", "DOI": _PADDED_DOI}]),
    f"TY  - JOUR\nTI  - T\nDO  - {_PADDED_DOI}\nER  - \n",
    f"%0 Journal Article\n%T T\n%R {_PADDED_DOI}\n",
    "{Reference Type}: Journal Article\n{Title}: T\n{DOI}: " + _PADDED_DOI + "\n",
    "@misc{k, title={T}, doi={" + _PADDED_DOI + "}}",
    # continued on the next line, whatever comes between (recheck 4's finding 12)
    "TY  - JOUR\nTI  - T\nDO  - 10.5555/example\n" + "g" * (rf.CONTINUATION_CHARS + 10) + "\nER  - \n",
    "TY  - JOUR\nTI  - T\nDO  - 10.5555/example\n" + " " * (rf.CONTINUATION_CHARS + 10) + "garbage\nER  - \n",
    "TY  - JOUR\nTI  - T\nDO  - 10.5555/example\n" + "\n" * (rf.CONTINUATION_LINES + 10) + "garbage\nER  - \n",
    "%0 Journal Article\n%T T\n%R 10.5555/example\n" + "  \n" * (rf.CONTINUATION_LINES + 10) + "garbage\n",
    "{Reference Type}: Journal Article\n{Title}: T\n{DOI}: 10.5555/example\ngarbage\n",
    "@misc{k, title={T}, doi={10.5555/example\ngarbage}}",
    # anything around it but spaces, tabs and line ends; no prefix, no escape, no other script (finding 13)
    json.dumps([{"title": "T", "DOI": "10.5555/example."}]),
    json.dumps([{"title": "T", "DOI": "https://doi.org/10.5555/example"}]),
    json.dumps([{"title": "T", "DOI": "doi:10.5555/example"}]),
    json.dumps([{"title": "T", "DOI": "\u000010.5555/example"}]),
    json.dumps([{"title": "T", "DOI": "10.5555/example\u001f"}]),
    "TY  - JOUR\nTI  - T\nDO  - 10.5555/example\x1f\nER  - \n",
    json.dumps([{"title": "T", "DOI": "10.١٢٣٤/abc"}]),
    json.dumps([{"title": "T", "DOI": "10.1000/K"}]),
    json.dumps([{"title": "T", "DOI": "10.5555/../etc"}]),
    r"@misc{k, title={T}, doi={10.1000/ab\_c}}",
    "@misc{k, title={T}, doi=d}\n",  # a macro is not the field's own text
    "TY  - JOUR\nTI  - T\nDO  - 10.5555/one\nDO  - 10.5555/two\nER  - \n",  # given twice differently
    json.dumps([{"title": "T", "DOI": 1234}]),  # not text (recheck 4's finding 16)
    # a control character that splitlines() takes for a line end is no line end here (internal review, batch 4)
    *(f"TY  - JOUR\nTI  - T\nDO  - 10.5555/abc{c}\nER  - \n" for c in "\x0b\x0c\x1c\x1d\x1e"),
    "%0 Journal Article\n%T T\n%R 10.5555/abc\x1c\n",
    "@misc{k, title={T}, doi={10.5555/abc\x1c}}",
    json.dumps([{"title": "T", "DOI": "10.5555/abc)"}]),  # parentheses that do not close in order
    json.dumps([{"title": "T", "DOI": "10.5555/a)b(c"}]),
    "TY  - JOUR\nTI  - T\nDO  - 10.5555/abc\nER  -X\nER  - \n",  # ER without its space continues the DOI
])
def test_a_doi_that_is_not_whole_its_fields_own_text_is_left_out_and_counted(data):
    [record], skipped, _ = parse(data)
    assert "DOI" not in record and rf.identifier(record) is None and skipped == {"invalid_field": 1}


@pytest.mark.parametrize("c", ["\x0b", "\x0c", "\x1c", "\x1e"])
def test_a_url_holding_a_control_character_is_left_out_and_counted(c):
    [record], skipped, _ = parse(f"TY  - JOUR\nTI  - T\nUR  - https://example.org/a{c}\nER  - \n")
    assert "URL" not in record and skipped == {"invalid_field": 1}



def test_an_endnote_tag_needs_its_space():
    [record], skipped, _ = parse("%0 Journal Article\n%T T\n%R10.5555/abc\n")
    assert "DOI" not in record and record["title"] == "T %R10.5555/abc" and skipped == {}  # text of the title


@pytest.mark.parametrize("hidden", [
    "@ string{jan = {X}}\n",  # left as text
    "@comment{ @string{jan={X}} }\n", "@preamble{ @string{jan={X}} }\n",
    "@comment{unclosed @string{jan={X}}\n",  # in an unreadable comment (internal recheck, batch 4)
    "@misc{k, title={T}\n@string\n{jan={X}}\n",  # swallowed by an unreadable entry
    "@misc{k, title={T} @string{jan={X}}}\n",  # swallowed by an entry
])
def test_an_at_string_hidden_in_another_block_refuses_every_later_macro_use(hidden):
    records, _, _ = parse(f"@misc{{before, title=jan}}\n{hidden}@article{{b, title={{B}}, journal=jan}}\n")
    assert [r["title"] for r in records] == ["January"]  # before it; the one after is refused, never "January"


def test_an_at_string_the_parser_leaves_as_text_is_still_a_definition_of_some_macro():
    text = "@misc{before, title=jan}\n@ string{jan = {X}}\n@article{b, title={B}, journal=jan}\n@misc{c, title={Kept}}\n"
    records, skipped, _ = parse(text)
    assert [r["title"] for r in records] == ["January", "Kept"] and skipped == {"unreadable": 1}


@pytest.mark.parametrize("eprint, arxiv", [("2101.00001", "2101.00001"), ("2101.00001v2", "2101.00001v2"),
                                           ("math.GT/0309136", "math.GT/0309136"), (" 2101.00001\n", "2101.00001")])
def test_an_arxiv_id_comes_from_a_bibtex_eprint_whole(eprint, arxiv):
    [record], skipped, _ = parse(f"@misc{{k, title={{T}}, eprint={{{eprint}}}, archivePrefix={{arXiv}}}}")
    assert (record["arxiv"], rf.identifier(record), skipped) == (arxiv, ("arxiv", arxiv.split("v")[0]), {})


@pytest.mark.parametrize("eprint", ["2101.123456", "2101.00001garbage", "2101.00001 2101.00002", "x2101.00001",
                                    "arXiv:2101.00001", "2101.00001V2", "hep-th/９９０１００１", "math\\_ph/0301001",
                                    "2101.00001" + " " * 1200 + "garbage", "2101.00001\ngarbage"])
def test_an_eprint_that_is_not_whole_an_arxiv_id_is_left_out_and_counted(eprint):
    [record], skipped, _ = parse(f"@misc{{k, title={{T}}, eprint={{{eprint}}}, eprinttype={{arxiv}}}}")
    assert "arxiv" not in record and rf.identifier(record) is None and skipped == {"invalid_field": 1}


@pytest.mark.parametrize("data", [
    json.dumps([{"title": "T", "number": "arXiv:2101.00001"}]),
    "@misc{k, title={T}, number={arXiv:2101.00001}}",
    "@misc{k, title={T}, number={{arXiv}:2101.00001\\garbage}}",
    json.dumps([{"title": "T", "number": "arXiv:2101.00001" + " " * 1200 + "garbage"}]),
    json.dumps([{"title": "T", "URL": "https://arxiv.org/abs/2101.00001"}]),
    "TY  - JOUR\nTI  - T\nUR  - https://arxiv.org/abs/2101.00001\nER  - \n",
    "@misc{k, title={T}, url={https://arxiv.org/abs/2101.00001}}",
    "@misc{k, title={T}, eprint={2101.00001}}",  # an eprint of no archive named arXiv
])
def test_no_identifier_comes_from_a_number_a_url_or_another_eprint(data):
    [record], skipped, _ = parse(data)
    assert rf.identifier(record) is None and "arxiv" not in record and skipped == {}


@pytest.mark.parametrize("url", ["https://example.org", "http://example.org/a", "https://example.org:8080/a?b=1#c",
                                 "https://arxiv.org/abs/2101.00001v2", "https://example.org/~researcher/a_b%20c"])
def test_a_url_that_is_whole_one_is_kept_for_display(url):
    [record], skipped, _ = parse(json.dumps([{"title": "T", "URL": url}]))
    assert (record["URL"], skipped) == (url, {})


@pytest.mark.parametrize("data", [
    json.dumps([{"title": "T", "URL": url}]) for url in [
        "https://:80/abc", "https://@/abc", "https://arxiv.org:bad/abs/2101.00001", "https://arxiv.org:99999/abs/x",
        "https://arxiv.org:0/x", "http://localhost/x", "https://u:p@arxiv.org/x", "ftp://example.org/a",
        "file:///etc/hosts", "https://example.org/a b", "https://example.org/a\\b", "https://example.org/é",
        _PADDED_URL, "https://example.org/a\u001f", 5]
] + [
    f"TY  - JOUR\nTI  - T\nUR  - {_PADDED_URL}\nER  - \n",
    "TY  - JOUR\nTI  - T\nUR  - https://example.org/a\ngarbage\nER  - \n",
    "TY  - JOUR\nTI  - T\nUR  - https://example.org/a;garbage b\nER  - \n",
    "%0 Journal Article\n%T T\n%U https://example.org/a\ngarbage\n",
    "@misc{k, title={T}, url={http://example.com/a\\_b}}",
])
def test_a_url_that_is_not_whole_one_is_left_out_and_counted(data):
    [record], skipped, _ = parse(data)
    assert "URL" not in record and skipped == {"invalid_field": 1}


@pytest.mark.parametrize("text, titles", [
    ("TY  - JOUR\nTI  - A\nER  -X\nDO  - 10.5555/a\nER  - \n", ["A ER  -X"]),  # no end without its space
    ("TY  - JOUR\nTI  - A\nTY  -Xfoo\nER  - \n", ["A TY  -Xfoo"]),  # no start without its space
    ("TY  - JOUR\nTI  - T\nDO  -X10.5555/example\nER  - \n", ["T DO  -X10.5555/example"]),
])
def test_a_ris_tag_needs_its_space_the_records_own_tags_too(text, titles):
    records, skipped, _ = parse(text)
    assert [" ".join(r["title"].split()) for r in records] == [" ".join(t.split()) for t in titles]
    assert all(rf.identifier(r) in (None, ("doi", "10.5555/a")) for r in records)


def test_csl_json_that_is_not_a_list_of_objects():
    records, skipped, kind = parse(json.dumps([{"title": "Kept"}, 3, "text", None]))
    assert ([r["title"] for r in records], skipped, kind) == (["Kept"], {"unreadable": 3}, "csl_json")
    [one], _, _ = parse(json.dumps({"title": "One object"}))
    assert one["title"] == "One object"
    assert unreadable(json.dumps({"a": {"b": [1] * 3}})[:-1]) == "unreadable_file"


@pytest.mark.parametrize("title", ["", "   ", "​‍", "️", "\x00\x01"])
def test_a_record_without_a_visible_title_is_skipped_and_counted(title):
    records, skipped, _ = parse(f"TY  - JOUR\nTI  - {title}\nER  - \nTY  - JOUR\nTI  - Kept\nER  - \n")
    assert [r["title"] for r in records] == ["Kept"] and skipped == {"no_title": 1}


def test_fields_past_their_bounds_are_cut_and_never_stored_unbounded():
    authors = "".join(f"AU  - Family{i}, Given\n" for i in range(rf.AUTHORS + 50))
    keywords = "".join(f"KW  - k{i}\n" for i in range(rf.KEYWORDS + 50))
    long_line = "x" * (rf.ABSTRACT_CHARS * 3)
    text = (f"TY  - JOUR\nTI  - {'t' * 5000}\n{authors}T2  - {'v' * 5000}\nVL  - {'9' * 1000}\nAB  - {long_line}\n"
            + "\n".join(["  continued"] * (rf.CONTINUATION_LINES * 5)) + f"\n{keywords}UR  - https://e.org/{'u' * 5000}\nER  - \n")
    [record], _, _ = parse(text)
    assert len(record["title"]) == rf.TITLE_CHARS and len(record["container-title"]) == rf.TITLE_CHARS
    assert len(record["author"]) == rf.AUTHORS and len(record["volume"]) == rf.FIELD_CHARS
    assert len(record["abstract"]) == rf.ABSTRACT_CHARS and "URL" not in record  # a URL cut short is no URL
    assert len(record["keyword"].split(", ")) == rf.KEYWORDS
    assert rf.valid(record)


@pytest.mark.parametrize("doi", ["10.5555/../etc", "10.1/short", "not a doi", "10.5555/" + "x" * 400, "10.5555/a b"])
def test_a_doi_failing_its_pattern_is_not_kept(doi):
    [record], _, _ = parse(f"TY  - JOUR\nTI  - T\nDO  - {doi}\nER  - \n")
    assert "DOI" not in record and rf.identifier(record) is None


def test_nothing_a_record_points_to_is_read_or_kept(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("a reference file's record made the parser open a file")

    monkeypatch.setattr(builtins, "open", refused)
    monkeypatch.setattr(os, "open", refused)
    monkeypatch.setattr(pathlib.Path, "read_bytes", refused)
    monkeypatch.setattr(pathlib.Path, "read_text", refused)
    for data in (synthetic.references_ris(), synthetic.references_bibtex(), synthetic.references_endnote()):
        for record in rf.parse(data)[0]:
            kept = json.dumps(record)
            assert "/etc/" not in kept and "file:" not in kept and "/Users/someone" not in kept
            assert record.get("URL", "https://").startswith(("http://", "https://"))


# Each format, to CSL JSON


def test_ris_records_in_english_and_chinese():
    records, skipped, kind = rf.parse(synthetic.references_ris())
    assert (kind, skipped, len(records)) == ("ris", {"invalid_field": 1}, 3)  # its file: URL, left out and counted
    english, chinese, chapter = records
    assert english == {
        "type": "article-journal", "title": "Synthetic Evidence from Reference Files",
        "author": [{"family": "Example", "given": "Ana"}, {"family": "Researcher", "given": "Bo"}],
        "issued": {"date-parts": [[2022]]}, "container-title": "Journal of Synthetic Studies", "volume": "7",
        "issue": "2", "page": "11-29", "language": "en", "DOI": synthetic.REF_DOI,
        "abstract": "We study synthetic wage records drawn from a made-up reference file.",
        "keyword": "wages, synthetic data"}
    assert chinese["title"] == "合成数据中的最低工资研究" and chinese["language"] == "zh"
    assert chinese["author"] == [{"literal": "张三"}, {"literal": "李四"}]  # a Han name is kept whole
    assert chapter["type"] == "chapter" and "DOI" not in chapter and rf.identifier(chapter) is None


def test_bibtex_with_macros_latex_names_and_an_arxiv_eprint():
    records, skipped, kind = rf.parse(synthetic.references_bibtex())
    assert (kind, skipped) == ("bibtex", {})
    article, preprint = records
    assert article["title"] == "Café Wages and Synthetic Workers"
    assert article["container-title"] == "Journal of Synthetic Studies"
    assert article["author"] == [{"family": "Example", "given": "Ana"}, {"family": "Researcher", "given": "Bo"},
                                 {"literal": "王五"}]
    assert article["abstract"] == "An abstract with LaTeX in it." and article["DOI"] == "10.5555/scholia.bibtex.001"
    assert preprint["arxiv"] == synthetic.ARXIV and "number" not in preprint and "URL" not in preprint
    assert rf.identifier(preprint) == ("arxiv", synthetic.ARXIV) and rf.identifier(article)[0] == "doi"


def test_csl_json_noteexpress_and_endnote():
    [csl], _, kind = rf.parse(synthetic.references_csl_json())
    assert kind == "csl_json" and csl["page"] == "12" and csl["issued"] == {"date-parts": [[2018]]}
    assert csl["author"] == [{"family": "Roe", "given": "Dee"}]
    noteexpress, skipped, kind = rf.parse(synthetic.references_noteexpress())
    assert (kind, skipped, [r["type"] for r in noteexpress]) == ("noteexpress", {}, ["article-journal", "thesis"])
    assert noteexpress[0]["author"] == [{"literal": "赵六"}, {"literal": "钱七"}]
    assert noteexpress[0]["keyword"] == "劳动力市场, 合成数据" and noteexpress[0]["page"] == "33-51"
    [endnote], _, kind = rf.parse(synthetic.references_endnote())
    assert kind == "endnote" and endnote["abstract"] == "An abstract in a tagged file, continued on a second line."
    assert endnote["URL"] == "https://example.org/synthetic" and endnote["DOI"] == "10.5555/scholia.endnote.001"


def test_the_format_is_found_by_the_content_not_the_name():
    assert rf.detect("{Reference Type}: Book\n{Title}: T") == "noteexpress"
    assert rf.detect('[{"title": "T"}]') == "csl_json" and rf.detect("%0 Book\n%T T") == "endnote"
    assert rf.detect("  @Article{k,\n title={T}}") == "bibtex" and rf.detect("TY  - JOUR") == "ris"


def test_valid_takes_only_what_parse_makes():
    for data in (synthetic.references_ris(), synthetic.references_bibtex(), synthetic.references_csl_json(),
                 synthetic.references_noteexpress(), synthetic.references_endnote()):
        assert all(rf.valid(r) for r in rf.parse(data)[0])
    good = {"type": "article", "title": "T"}
    assert rf.valid(good)
    for bad in ({"title": "T"}, {**good, "extra": 1}, {**good, "title": "​"}, {**good, "title": "t" * 1001},
                {**good, "DOI": "10.5555/X"}, {**good, "author": []}, {**good, "author": [{"literal": "a", "x": 1}]},
                {**good, "issued": {"date-parts": [[999]]}}, {**good, "issued": {"date-parts": [[2020, 1]]}},
                {**good, "type": "Article"}, {**good, "volume": 3}, [good], "text"):
        assert not rf.valid(bad), bad


# In the child


def test_a_reference_file_is_read_in_the_child_and_its_records_checked():
    records, skipped, kind = reading.references(synthetic.references_ris())
    assert (kind, skipped, len(records)) == ("ris", {"invalid_field": 1}, 3) and records == rf.parse(synthetic.references_ris())[0]
    assert not reading.LIVE


@pytest.mark.parametrize("data, code", [(b"no format", "unsupported_reference_file"), (b"", "no_records"),
                                         (b"[1, 2", "unreadable_file")])
def test_the_childs_refusal_comes_back_by_code(data, code):
    with pytest.raises(extraction.Unreadable) as raised:
        reading.references(data)
    assert raised.value.code == code and not reading.LIVE


def test_a_large_reference_file_goes_to_the_child_whole():
    text = "".join(f"TY  - JOUR\nTI  - Record {i}\nAB  - {'words ' * 150}\nER  - \n" for i in range(9000))
    assert len(text) > 8 * 1024 * 1024  # far more than a pipe holds at once
    records, _, _ = reading.references(text.encode())
    assert len(records) == 9000 and records[-1]["title"] == "Record 8999"


def test_a_child_past_its_memory_ceiling_is_stopped():
    with pytest.raises(extraction.Unreadable) as raised:
        reading.references(synthetic.references_ris(), ceiling=1024 * 1024)
    assert raised.value.code == "memory_limit" and not reading.LIVE


def test_a_stop_ends_the_child(tmp_path):
    class Stopped(Exception):
        pass

    def stop():
        raise Stopped()

    with pytest.raises(Stopped):
        reading.references(synthetic.references_ris(), stop)
    assert not reading.LIVE


def test_a_wrong_record_from_the_child_is_a_wrong_frame():
    received = reading._References()
    received.take(json.dumps({"ready": list(rf.PARSER)}).encode())
    with pytest.raises(reading._Bad):
        received.take(json.dumps({"record": {"type": "article", "title": "T", "note": "x"}}).encode())
    received = reading._References()
    received.take(json.dumps({"ready": list(rf.PARSER)}).encode())
    with pytest.raises(reading._Bad):
        received.take(json.dumps({"done": {"format": "ris", "skipped": {}}}).encode())  # nothing read, nothing skipped
    with pytest.raises(reading._Bad):
        received.take(json.dumps({"done": {"format": "docx", "skipped": {"no_title": 1}}}).encode())


# From the writer's first review (2026-10-10)


def test_half_a_surrogate_pair_from_json_never_reaches_a_record():
    text = ('[{"title": "Emoji \\ud83d cut", "abstract": "\\udc00 abstract", "container-title": "Venue \\ud800",'
            ' "author": [{"family": "Fam\\ud800", "given": "Giv"}, {"literal": "\\udfff Lit"}]}]')
    [record], _, _ = parse(text)
    assert record["title"] == "Emoji � cut" and record["abstract"] == "� abstract"
    assert record["container-title"] == "Venue �" and record["author"][0] == {"family": "Fam�", "given": "Giv"}
    assert rf.valid(record) and json.dumps(record, ensure_ascii=False).encode()  # the database and JSON answers take it
    assert not rf.valid({"type": "article", "title": "Half \ud800 a pair"})
    assert not rf.valid({"type": "article", "title": "T", "author": [{"literal": "\udfff"}]})


@pytest.mark.parametrize("end", ["\r", "\u2028", "\x85", "\r\n"])
def test_every_line_end_counts_and_reads_records_alike(end):
    ris = f"TY  - JOUR{end}TI  - T{end}ER  - {end}"
    assert unreadable(ris * (rf.MAX_RECORDS + 1)) == "too_many_records"
    assert len(parse(ris * 3)[0]) == 3
    endnote = f"%0 Book{end}%T T{end}{end}"
    assert unreadable(endnote * (rf.MAX_RECORDS + 1)) == "too_many_records"
    assert [r["title"] for r in parse(f"{{Reference Type}}: Book{end}{{Title}}: One{end}" * 2)[0]] == ["One", "One"]


def test_a_record_of_very_many_lines_keeps_its_bounds():
    [ris], _, _ = parse("TY  - JOUR\nTI  - T\n" + "AU  - Family, Given\n" * 50_000 + "ER  - \n")
    [endnote], _, _ = parse("%0 Book\n%T T\n" + "%A Family, Given\n" * 50_000)
    [noteexpress], _, _ = parse("{Reference Type}: Book\n{Title}: T\n" + "{Author}: 甲;乙\n" * 50_000)
    assert [len(r["author"]) for r in (ris, endnote, noteexpress)] == [rf.AUTHORS] * 3
    [csl], _, _ = parse(json.dumps([{"title": "T", "author": [{"literal": "x"}] * 50_000}]))
    assert len(csl["author"]) == rf.AUTHORS


def test_a_name_cut_to_its_bound_is_still_a_visible_name_or_none():
    for name in ("​" * 300 + "Visible", "​" * 199 + "AB", "Fam" + "​" * 300 + ", Given"):
        [record], _, _ = parse(f"TY  - JOUR\nTI  - T\nAU  - {name}\nAU  - Kept, Name\nER  - \n")
        assert rf.valid(record) and {"family": "Kept", "given": "Name"} in record["author"]


def test_a_noteexpress_tag_is_on_its_own_line():  # as the reader takes it, so the count and the read agree
    broken = "{Reference Type}\n: Book\n{Title}: T\n"
    assert rf.detect("Exported\n" + broken) is None
    assert unreadable("Exported\n" + broken * 3) == "unsupported_reference_file"
    one = "{Reference Type}: Book\n{Title}: T\n"
    assert len(parse(one + broken * (rf.MAX_RECORDS + 1))[0]) == 1  # one record, counted as one


# From the writer's second recheck (2026-10-10)


@pytest.mark.parametrize("block", ["@string{a=1}\n", "@string{}", "@comment{x}", "@preamble{x}\n", "@{}\n", "@{", "@1{}",
                                   "@a_b{}", "@é{}", "@ {}", "@string1{a=1}\n", "@comment1{x}"])
def test_bibtex_blocks_of_any_kind_count_toward_the_bound(block):  # as bibtexparser opens one: an @, any word, { or (
    assert unreadable("@misc{k, title={T}}\n" + block * (rf.MAX_BLOCKS + 1)) == "too_many_records"


@pytest.mark.parametrize("kind", ["1", "", "a_b", "é"])
def test_bibtex_entries_of_any_type_count_toward_the_record_bound(kind):
    assert unreadable("@misc{a, title={T}}\n" + f"@{kind}{{k, title={{T}}}}\n" * rf.MAX_RECORDS) == "too_many_records"


def test_bibtex_indented_by_any_space_is_found():
    assert rf.detect("\u3000@article{k, title={T}}") == "bibtex" and rf.detect("\t @misc{k}") == "bibtex"


def test_many_blank_lines_are_read_in_linear_time():
    import time
    started = time.monotonic()
    assert unreadable("\n" * 1_000_000 + "hello") == "unsupported_reference_file"
    parse("\n" * 1_000_000 + "@misc{k, title={T}}")  # BibTeX's detection and count too
    assert time.monotonic() - started < 5


def test_json_whose_strings_hold_other_line_ends_is_still_json():
    text = json.dumps([{"title": "A {Reference Type}: B"}], ensure_ascii=False)
    [record], _, kind = parse(text)
    assert kind == "csl_json" and record["title"] == "A {Reference Type}: B"


def test_the_bibtex_bound_counts_with_the_parsers_own_block_start():
    import re

    from bibtexparser import splitter
    assert splitter._BLOCK_START == r"@[\w]*( |\t)*(?=[{(])"  # bibtexparser 2's, which reference_files' equals
    for text in ("@misc{", "@ {", "@\t(", "@1x_é {", "a@b{", "@{", "@x y{", "@@x{", "@ \n{", "@string1{a=1}"):
        assert [m.group(0) for m in re.finditer(splitter._BLOCK_START, text)] == \
            [m.group(0) for m in rf._BLOCK_START.finditer(text)], text


@pytest.mark.parametrize("block", ["@{\n", "@string1{a=1}\n", "@1{}", "@é{}"])
def test_too_many_bibtex_blocks_are_refused_before_the_parser_runs(block, monkeypatch):
    import bibtexparser

    def never(*args, **kwargs):
        raise AssertionError("the parser ran")

    monkeypatch.setattr(bibtexparser, "parse_string", never)
    assert unreadable("@misc{k, title={T}}\n" + block * (rf.MAX_BLOCKS + 1)) == "too_many_records"


def test_a_run_of_spaces_after_an_at_sign_is_read_as_one():
    [record], _, _ = parse("@misc{k, title={T}}\n@" + " " * (4 * 1024 * 1024) + "x")
    assert record["title"] == "T"


def test_marks_inside_values_never_refuse_a_file_within_the_record_bound():
    entry = "@article{k%d, title={T}, author={LeQua @ {CLEF}}}\n"  # bibtexparser's own example of an @ in a value
    assert len(parse("".join(entry % i for i in range(rf.MAX_RECORDS)))[0]) == rf.MAX_RECORDS
