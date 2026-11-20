"""The app's log: owner-only files in the data folder's logs/ folder, kept 14 days.

Log calls carry safe codes, counts and ids only: never a prompt, an answer, a
source's text, reasoning, a key, a file name, a URL with its query, or a
provider's error body. Tests feed canaries through the app and check the log.
"""

import logging
import os
import stat
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

KEPT_DAYS = 14
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class PrivateRotatingHandler(TimedRotatingFileHandler):
    """Rotates at midnight, keeps KEPT_DAYS files, and creates each one owner-only (0600).
    It writes only to a regular file at its own path: never through a symbolic link."""

    def __init__(self, path):
        super().__init__(path, when="midnight", backupCount=KEPT_DAYS, encoding="utf-8", delay=True)

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise OSError("the log is not a regular file")
        os.fchmod(fd, stat.S_IMODE(info.st_mode) & 0o600)  # an existing file is narrowed, never broadened
        return open(fd, self.mode, encoding=self.encoding, errors=self.errors)


class Formatter(logging.Formatter):
    """FORMAT, with an exception written as its type and where it was raised, for it and each one it came from, never
    its message: a library's message can quote stored text, as SQLite's decoding error quotes a record a build before
    PR 57 stored with half a surrogate pair (the M2 audit's PR 57 rechecks), and the server logs what it answers 500 for."""

    def format(self, record):
        record = logging.makeLogRecord(record.__dict__)  # a copy, without the full traceback another handler cached on it
        record.exc_text = None
        return super().format(record)

    def formatException(self, ei):
        lines, error, seen = [], ei[1], set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            frames = traceback.extract_tb(error.__traceback__)
            where = f" at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else ""
            lines.append(f"{type(error).__name__}{where}")
            error = error.__cause__ or error.__context__
        return "\n".join(lines)


def configure(data_dir, level=logging.INFO) -> logging.Handler:
    """Send the app's log records to <data_dir>/logs/scholia.log. Returns the handler.
    Raises NotADirectoryError when logs/ is not a real folder (a link is never followed)."""
    folder = Path(data_dir) / "logs"
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            raise NotADirectoryError("the logs folder is not a folder") from None
        # An existing folder, its log and its older dated logs are narrowed to owner-only
        # (never broadened) before anything is written or rotated.
        os.chmod(folder, stat.S_IMODE(os.lstat(folder).st_mode) & 0o700)
        for path in folder.iterdir():
            if path.is_file() and not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.stat().st_mode) & 0o600)
    handler = PrivateRotatingHandler(folder / "scholia.log")
    handler.setFormatter(Formatter(FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    # Request lines and library chatter stay out of the default log.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler
