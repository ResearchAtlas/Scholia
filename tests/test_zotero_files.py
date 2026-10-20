"""Reading the file Zotero names for an attachment (S1-15; backend/zotero.py read_attachment): only a
regular PDF on this Mac's own disk, every link resolved first and every check made on the final file.
Synthetic files in a temporary folder only; never Zotero's own folders."""

import os
import stat
import sys

import pytest

import backend.zotero as zotero
from backend import extraction

PDF = b"%PDF-1.7\n% synthetic\n"


def url(path):
    """The path a file URL names (zotero.file_path), from a path's own URL (as Zotero's newFileURI writes
    one) or from a URL's text."""
    return zotero.file_path(path if isinstance(path, str) else path.as_uri())


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "论文 storage" / "A paper.pdf"
    path.parent.mkdir()
    path.write_bytes(PDF)
    return path


def test_a_local_pdf_is_read_with_its_path_percent_decoded(pdf):
    assert "%20" in pdf.as_uri() and "%E8" in pdf.as_uri()
    assert url(pdf) == str(pdf)
    assert zotero.read_attachment(url(pdf)) == (PDF, None)
    assert zotero.read_attachment(None) == (None, "pdf_missing")  # an attachment with no file


def test_the_url_forms_zotero_writes_name_their_path_as_is():
    # newFileURI leaves these unescaped (Mozilla's esc_Directory mask), so they are read as written
    assert zotero.file_path("file:///tmp/a[1]|b;c=d,e&f'g(h)!$*+~@:x.pdf") == "/tmp/a[1]|b;c=d,e&f'g(h)!$*+~@:x.pdf"
    assert zotero.file_path("file:///tmp/50%25%20%23%3F.pdf") == "/tmp/50% #?.pdf"
    assert zotero.file_path("file:///tmp/../x.pdf") == "/tmp/../x.pdf"  # read_attachment refuses it


@pytest.mark.parametrize("text", [
    None, "", "false", " file:///tmp/x.pdf", "file:///tmp/x.pdf\n", "file:///tmp/x.pdf ", "FILE:///tmp/x.pdf",
    "file:///tmp/pa\nper.pdf", "file:///tmp/pa\tper.pdf", "file:///tmp/a b.pdf", "file:///tmp/论文.pdf",
    "file://nas.example/share/paper.pdf", "file://localhost/tmp/x.pdf", "file://[broken/path.pdf",
    "file://[::1/x.pdf", "http://example.org/paper.pdf", "smb://nas/paper.pdf", "file:paper.pdf", "file:/tmp/x.pdf",
    "file:///tmp/x.pdf?q=1", "file:///tmp/x.pdf#p", "file:///tmp/%2", "file:///tmp/%zz.pdf", "file:///tmp/a\\b.pdf",
    "file:///tmp/%FF.pdf", "file:///tmp/a%00b.pdf", "file:///tmp/a%0Ab.pdf", "file:///tmp/a%7Fb.pdf",
    "file:///tmp/a%C2%85b.pdf", "file://%2Ftmp/x.pdf",
])
def test_anything_but_a_file_url_of_the_form_zotero_writes_is_refused(text):
    with pytest.raises(ValueError):
        zotero.file_path(text)


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


def test_no_link_is_followed_anywhere_in_the_path(tmp_path, pdf):
    link = tmp_path / "link.pdf"
    link.symlink_to(pdf)
    chain = tmp_path / "chain.pdf"
    chain.symlink_to(link)
    folder = tmp_path / "folder"
    folder.symlink_to(pdf.parent)
    database = tmp_path / "zotero.sqlite"
    database.write_bytes(PDF)  # even one that starts like a PDF
    named = tmp_path / "paper2.pdf"
    named.symlink_to(database)
    dangling = tmp_path / "dangling.pdf"
    dangling.symlink_to(tmp_path / "nowhere.pdf")
    loop = tmp_path / "loop.pdf"
    loop.symlink_to(loop)
    for path in (link, chain, folder / pdf.name, named, dangling, loop):
        assert zotero.read_attachment(url(path)) == (None, "pdf_link"), path
    assert zotero.read_attachment(url(database)) == (None, "pdf_not_a_pdf")
    assert zotero.read_attachment(url(pdf)) == (PDF, None)


@pytest.mark.parametrize("swapped", ["folder", "file"])
def test_a_folder_or_file_swapped_for_a_link_during_the_walk_is_refused(tmp_path, monkeypatch, swapped):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    (real / "inner" / "paper.pdf").write_bytes(PDF)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "inner").mkdir(parents=True)
    (elsewhere / "inner" / "paper.pdf").write_bytes(PDF + b"elsewhere")
    target = real / "inner" if swapped == "folder" else real / "inner" / "paper.pdf"
    real_open = os.open

    def swapping(path, flags, *args, **kwargs):  # the name is replaced by a link just before it is opened
        if path == target.name and not target.is_symlink():
            if swapped == "folder":
                target.rename(tmp_path / "moved")
                target.symlink_to(elsewhere / "inner")
            else:
                target.unlink()
                target.symlink_to(elsewhere / "inner" / "paper.pdf")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(zotero.os, "open", swapping)
    assert zotero.read_attachment(url(real / "inner" / "paper.pdf")) == (None, "pdf_link")


def test_network_folders_and_files_and_cloud_placeholders_are_never_read(tmp_path, pdf, monkeypatch):
    opened = []
    real_open = os.open
    monkeypatch.setattr(zotero.os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k))
    assert zotero.read_attachment(url(pdf), fs_type=lambda fd: "smbfs") == (None, "pdf_on_network")
    assert opened == ["/"]  # / itself is checked before anything in it is opened
    deeper = iter(["apfs", "apfs", "nfs"])
    assert zotero.read_attachment(url(pdf), fs_type=lambda fd: next(deeper, "apfs")) == (None, "pdf_on_network")
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


# A mount table as getfsstat gives it, with the automounts macOS's auto_master names and a direct map.
TABLE = [("/", "apfs"), ("/dev", "devfs"), ("/System/Volumes/Data", "apfs"), ("/System/Volumes/Data/home", "autofs"),
         ("/net", "autofs"), ("/Network/Servers", "autofs"), ("/Volumes/Share", "smbfs")]


@pytest.mark.parametrize("text", ["file:///net/nas.example/export/x.pdf", "file:///home/someone/x.pdf",
                                  "file:///Network/Servers/x.pdf", "file:////net/nas/x.pdf", "file:///%2Fnet/nas/x.pdf",
                                  "file:///System/Volumes/Data/home/x.pdf", "file:///System/Volumes/Data/net/nas/x.pdf",
                                  "file:///HOME/someone/x.pdf", "file:///Volumes/Share/x.pdf", "file:///net"])
def test_a_path_on_any_mount_not_local_is_refused_before_anything_is_opened(text, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("opened")

    monkeypatch.setattr(zotero.os, "open", never)
    assert zotero.read_attachment(url(text), mounts=lambda: TABLE) == (None, "pdf_on_network")


def test_a_direct_map_automount_under_a_local_folder_is_never_opened(tmp_path, monkeypatch):
    trigger = tmp_path / "Library" / "Shares"  # a custom mount point, under an APFS folder
    trigger.mkdir(parents=True)
    opened = []
    real_open = os.open
    monkeypatch.setattr(zotero.os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k))
    table = [("/", "apfs"), (str(trigger), "autofs")]
    assert zotero.read_attachment(url(trigger / "paper.pdf"), mounts=lambda: table) == (None, "pdf_on_network")
    assert opened == []  # nothing at all, not even /
    firmlinked = [("/", "apfs"), ("/System/Volumes/Data" + str(trigger), "autofs")]  # the table's other spelling
    assert zotero.read_attachment(url(trigger / "paper.pdf"), mounts=lambda: firmlinked) == (None, "pdf_on_network")
    assert zotero.read_attachment(url("file:///System/Volumes/Data" + str(trigger) + "/paper.pdf"),
                                  mounts=lambda: table) == (None, "pdf_on_network")  # the path's other spelling
    assert opened == []


def test_a_local_external_volume_is_read_and_an_unreadable_table_opens_nothing(pdf, monkeypatch):
    external = [("/", "apfs"), ("/" + pdf.parts[1], "exfat")]  # the PDF's folder as an exFAT drive
    assert zotero.read_attachment(url(pdf), mounts=lambda: external) == (PDF, None)
    opened = []
    real_open = os.open
    monkeypatch.setattr(zotero.os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k))

    def unreadable():
        raise OSError("getfsstat failed")

    assert zotero.read_attachment(url(pdf), mounts=unreadable) == (None, "pdf_unreadable")
    assert opened == []


@pytest.mark.parametrize("text", ["file:///homework/x.pdf", "file:///Volumes/Share2/x.pdf", "file:///networking/x.pdf"])
def test_mount_points_match_whole_names_only(text):
    assert zotero.read_attachment(url(text), mounts=lambda: TABLE) == (None, "pdf_missing")  # looked for, not refused


def test_a_mount_point_in_another_unicode_form_or_case_is_one_path(tmp_path, monkeypatch):
    import unicodedata
    opened = []
    monkeypatch.setattr(zotero.os, "open", lambda path, *a, **k: opened.append(path) or (_ for _ in ()).throw(OSError()))
    table = [("/", "apfs"), (unicodedata.normalize("NFD", "/Volumes/Café"), "autofs")]
    assert zotero.read_attachment(url("file:///volumes/CAF%C3%89/x.pdf"), mounts=lambda: table) == (None, "pdf_on_network")
    assert opened == []


@pytest.mark.parametrize("error", [OSError("failed"), AttributeError("no getfsstat")])
def test_a_mount_table_that_cannot_be_read_opens_nothing(pdf, monkeypatch, error):
    def broken():
        raise error

    monkeypatch.setattr(zotero.os, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    assert zotero.read_attachment(url(pdf), mounts=broken) == (None, "pdf_unreadable")


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac's mount table")
def test_this_macs_own_mount_table_is_read():
    table = zotero._mount_table()
    assert ("/", "apfs") in table and all(isinstance(point, str) and isinstance(kind, str) for point, kind in table)


def test_a_link_into_an_automounted_folder_is_never_followed(tmp_path, monkeypatch):
    link = tmp_path / "share.pdf"
    link.symlink_to("/net/nas.example/export/x.pdf")
    opened = []
    real_open = os.open
    monkeypatch.setattr(zotero.os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k))
    monkeypatch.setattr(zotero.os.path, "realpath", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolved")))
    assert zotero.read_attachment(url(link)) == (None, "pdf_link")
    assert "net" not in opened


def test_a_path_with_dot_segments_is_not_local():
    assert zotero.read_attachment(url("file:///tmp/../net/nas/x.pdf")) == (None, "pdf_not_local")


def test_only_a_pdfs_header_is_read_from_a_file_that_is_not_one(tmp_path, monkeypatch):
    backup = tmp_path / "zotero.sqlite.12.bak"
    backup.write_bytes(b"SQLite format 3\x00" + b"x" * 4096)
    reads = []
    real_read = os.read
    monkeypatch.setattr(zotero.os, "read", lambda fd, n: reads.append(n) or real_read(fd, n))
    assert zotero.read_attachment(url(backup)) == (None, "pdf_not_a_pdf")
    assert reads == [5]


@pytest.mark.parametrize("failing", ["fstat", "file system"])
def test_a_folder_or_file_whose_place_cannot_be_read_is_never_taken_for_local(pdf, monkeypatch, failing):
    def broken(*args):
        raise OSError("cannot be read")

    if failing == "fstat":
        monkeypatch.setattr(zotero.os, "fstat", broken)
        assert zotero.read_attachment(url(pdf)) == (None, "pdf_unreadable")
    else:
        assert zotero.read_attachment(url(pdf), fs_type=broken) == (None, "pdf_unreadable")
        monkeypatch.setattr(zotero.ctypes, "CDLL", lambda *a, **k: type("libc", (), {
            "fstatfs": type("f", (), {"argtypes": None, "__call__": lambda self, fd, info: -1})(),
            "__getitem__": lambda self, name: self.fstatfs})())
        assert zotero.read_attachment(url(pdf), mounts=lambda: [("/", "apfs")]) == (None, "pdf_unreadable")


@pytest.mark.parametrize("kind", ["autofs", "smbfs", "nfs", "webdav", "afpfs", "macfuse", "", None])
def test_only_local_file_systems_are_descended_into(pdf, monkeypatch, kind):
    opened = []
    real_open = os.open
    monkeypatch.setattr(zotero.os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k))
    kinds = iter(["apfs"])  # / is on this Mac's disk; the first folder below it is not
    assert zotero.read_attachment(url(pdf), fs_type=lambda fd: next(kinds, kind)) == (None, "pdf_on_network")
    assert opened == ["/", pdf.parts[1]]  # nothing below that folder is opened


@pytest.mark.parametrize("kind", ["apfs", "hfs", "exfat", "msdos"])
def test_this_macs_disks_and_attached_drives_are_read(pdf, kind):
    assert zotero.read_attachment(url(pdf), fs_type=lambda fd: kind) == (PDF, None)
