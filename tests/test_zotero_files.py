"""Reading the file Zotero names for an attachment (S1-15; backend/zotero.py read_attachment): only a
regular PDF on this Mac's own disk, every link resolved first and every check made on the final file.
Synthetic files in a temporary folder only; never Zotero's own folders."""

import os
import stat

import pytest

import backend.zotero as zotero
from backend import extraction

PDF = b"%PDF-1.7\n% synthetic\n"


def url(path):
    return path.as_uri()


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "论文 storage" / "A paper.pdf"
    path.parent.mkdir()
    path.write_bytes(PDF)
    return path


def test_a_local_pdf_is_read_with_its_path_percent_decoded(pdf):
    assert "%20" in url(pdf) and "%E8" in url(pdf)
    assert zotero.read_attachment(url(pdf)) == (PDF, None)
    assert zotero.read_attachment(url(pdf) + "\n") == (PDF, None)  # as Zotero's text answer may end


@pytest.mark.parametrize("text, reason", [
    (None, "pdf_missing"), ("", "pdf_missing"), ("false", "pdf_missing"),
    ("file://nas.example/share/paper.pdf", "pdf_not_local"), ("file://localhost/tmp/x.pdf", "pdf_not_local"),
    ("http://example.org/paper.pdf", "pdf_not_local"), ("smb://nas/paper.pdf", "pdf_not_local"),
    ("file:paper.pdf", "pdf_not_local"), ("file:///tmp/a%00b.pdf", "pdf_not_local"),
    ("file:///tmp/%FF.pdf", "pdf_not_local"), ("file:///tmp/x.pdf?q=1", "pdf_not_local"),
])
def test_anything_but_a_local_file_url_is_never_read(text, reason):
    assert zotero.read_attachment(text) == (None, reason)


def test_files_that_are_not_regular_pdfs_are_refused(tmp_path, pdf):
    missing = tmp_path / "gone.pdf"
    other = tmp_path / "notes.pdf"
    other.write_bytes(b"not a pdf")
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    assert zotero.read_attachment(url(missing)) == (None, "pdf_missing")
    assert zotero.read_attachment(url(tmp_path)) == (None, "pdf_not_a_file")
    assert zotero.read_attachment(url(fifo)) == (None, "pdf_not_a_file")  # never waits for a writer
    assert zotero.read_attachment(url(other)) == (None, "pdf_not_a_pdf")


def test_links_are_resolved_and_the_final_file_decides(tmp_path, pdf):
    link = tmp_path / "link.pdf"
    link.symlink_to(pdf)
    chain = tmp_path / "chain.pdf"
    chain.symlink_to(link)
    folder = tmp_path / "folder"
    folder.symlink_to(pdf.parent)
    assert zotero.read_attachment(url(link)) == (PDF, None)
    assert zotero.read_attachment(url(chain)) == (PDF, None)
    assert zotero.read_attachment(url(folder / pdf.name)) == (PDF, None)
    database = tmp_path / "zotero.sqlite"
    database.write_bytes(PDF)  # even one that starts like a PDF
    named = tmp_path / "paper2.pdf"
    named.symlink_to(database)
    assert zotero.read_attachment(url(named)) == (None, "pdf_not_a_pdf")
    assert zotero.read_attachment(url(database)) == (None, "pdf_not_a_pdf")
    dangling = tmp_path / "dangling.pdf"
    dangling.symlink_to(tmp_path / "nowhere.pdf")
    assert zotero.read_attachment(url(dangling)) == (None, "pdf_missing")
    loop = tmp_path / "loop.pdf"
    loop.symlink_to(loop)
    assert zotero.read_attachment(url(loop)) == (None, "pdf_unreadable")


def test_a_link_put_in_place_after_resolving_is_not_followed(tmp_path, pdf, monkeypatch):
    target = tmp_path / "swapped.pdf"
    target.write_bytes(PDF)
    real_open = os.open

    def swapped(path, flags, *args):  # the final file becomes a link between the checks and the open
        if str(path) == str(target) and not target.is_symlink():
            target.unlink()
            target.symlink_to(pdf)
        return real_open(path, flags, *args)

    monkeypatch.setattr(zotero.os, "open", swapped)
    assert zotero.read_attachment(url(target)) == (None, "pdf_unreadable")


def test_network_files_and_cloud_placeholders_are_never_opened(tmp_path, pdf, monkeypatch):
    assert zotero.read_attachment(url(pdf), fs_type=lambda path: "smbfs") == (None, "pdf_on_network")
    link = tmp_path / "remote.pdf"
    link.symlink_to(pdf)
    seen = []
    assert zotero.read_attachment(url(link), fs_type=lambda path: seen.append(path) or "nfs") == (None, "pdf_on_network")
    assert seen == [str(pdf)]  # the final file's file system, not the link's
    monkeypatch.setattr(zotero, "SF_DATALESS", stat.UF_HIDDEN)  # a flag a test can set, standing in for dataless
    os.chflags(pdf, stat.UF_HIDDEN)
    assert zotero.read_attachment(url(pdf)) == (None, "pdf_not_downloaded")


def test_a_file_over_the_limit_is_refused_and_read_no_further(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, "MAX_FILE_BYTES", 64)
    big = tmp_path / "big.pdf"
    big.write_bytes(PDF + b"x" * 64)
    assert zotero.read_attachment(url(big)) == (None, "pdf_too_large")
    exact = tmp_path / "exact.pdf"
    exact.write_bytes((PDF + b"x" * 64)[:64])
    assert zotero.read_attachment(url(exact))[1] is None


def test_reading_leaves_the_file_as_it_was(pdf):
    before = pdf.stat()
    zotero.read_attachment(url(pdf))
    after = pdf.stat()
    assert (after.st_mode, after.st_mtime_ns, after.st_size) == (before.st_mode, before.st_mtime_ns, before.st_size)


def test_a_pdf_named_like_a_database_topic_is_read_but_a_database_never(tmp_path):
    named = tmp_path / "Hipp - 2020 - SQLite as a file format.pdf"
    named.write_bytes(PDF)
    assert zotero.read_attachment(url(named)) == (PDF, None)
    for name in ("zotero.sqlite", "zotero.sqlite-wal", "zotero.sqlite.bak", "x.sqlite3", "zotero.sqlite-journal"):
        (tmp_path / name).write_bytes(PDF)
        assert zotero.read_attachment(url(tmp_path / name)) == (None, "pdf_not_a_pdf"), name


@pytest.mark.parametrize("text", ["file:///net/nas.example/export/x.pdf", "file:///home/someone/x.pdf",
                                  "file:///Network/Servers/x.pdf", "file:///tmp/../net/nas/x.pdf"])
def test_automounted_network_folders_are_refused_before_they_are_resolved(text, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("resolved")

    monkeypatch.setattr(zotero.os.path, "realpath", never)
    assert zotero.read_attachment(text) == (None, "pdf_on_network")


def test_a_file_url_with_a_host_it_cannot_read_is_refused():
    assert zotero.read_attachment("file://[::1/x.pdf") == (None, "pdf_not_local")
