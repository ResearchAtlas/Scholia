"""The app's log: owner-only files in the data folder's logs/ folder, kept 14 days.

Log calls carry safe codes, counts and ids only: never a prompt, an answer, a
source's text, reasoning, a key, a file name, a URL with its query, or a
provider's error body. Tests feed canaries through the app and check the log.
"""

import logging
import os
import stat
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

KEPT_DAYS = 14
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class PrivateRotatingHandler(TimedRotatingFileHandler):
    """Rotates at midnight, keeps KEPT_DAYS files, and creates each one owner-only (0600)."""

    def __init__(self, path):
        super().__init__(path, when="midnight", backupCount=KEPT_DAYS, encoding="utf-8", delay=True)

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & 0o600)  # an existing file is narrowed, never broadened
        return open(fd, self.mode, encoding=self.encoding, errors=self.errors)


def configure(data_dir, level=logging.INFO) -> logging.Handler:
    """Send the app's log records to <data_dir>/logs/scholia.log. Returns the handler."""
    folder = Path(data_dir) / "logs"
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        os.chmod(folder, stat.S_IMODE(folder.stat().st_mode) & 0o700)  # narrowed, never broadened
    handler = PrivateRotatingHandler(folder / "scholia.log")
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    # Request lines and library chatter stay out of the default log.
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler
