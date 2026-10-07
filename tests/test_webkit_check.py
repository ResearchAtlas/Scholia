"""tests/webkit_check.py exits unsuccessfully when its check fails, and successfully when it passes."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def webkit_check(monkeypatch):
    spec = importlib.util.spec_from_file_location("webkit_check", Path(__file__).parent / "webkit_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Window:
        class events:
            class loaded:
                @staticmethod
                def wait(seconds):
                    return True

        def destroy(self):
            pass

    monkeypatch.setattr(module.webview, "create_window", lambda *args, **kwargs: Window())
    monkeypatch.setattr(module.webview, "start", lambda run: run())  # no window: run the check in place
    return module


@pytest.mark.parametrize("outcome, code", [("pass", 0), ("fail", 1)])
def test_exit_code_follows_the_check(webkit_check, monkeypatch, tmp_path, outcome, code):
    def check(window, args):
        if outcome == "fail":
            raise RuntimeError("a step failed")

    monkeypatch.setattr(webkit_check, "check", check)
    assert webkit_check.main(["http://127.0.0.1:1/#session=s", "--out", str(tmp_path)]) == code


def test_a_page_that_never_loads_fails(webkit_check, monkeypatch, tmp_path):
    monkeypatch.setattr(webkit_check.webview, "create_window", lambda *args, **kwargs: type(
        "W", (), {"events": type("E", (), {"loaded": type("L", (), {"wait": staticmethod(lambda s: False)})}),
                  "destroy": lambda self: None})())
    monkeypatch.setattr(webkit_check, "check", lambda window, args: None)
    assert webkit_check.main(["http://127.0.0.1:1/#session=s", "--out", str(tmp_path)]) == 1


def test_a_callback_that_never_comes_fails(webkit_check):
    import threading
    event = threading.Event()
    with pytest.raises(RuntimeError, match="timed out"):
        webkit_check.finished(event, 0.01, "a snapshot")
    event.set()
    webkit_check.finished(event, 0.01, "a snapshot")  # a callback that came passes


@pytest.mark.parametrize("box, size, fails", [
    ({"error": "no image"}, 10, True), ({"written": False}, 10, True), ({"written": True}, 0, True),
    ({"written": True}, None, True), ({"written": True}, 10, False)])
def test_a_snapshot_not_written_fails(webkit_check, tmp_path, box, size, fails):
    path = tmp_path / "shot.png"
    if size is not None:
        path.write_bytes(b"x" * size)
    if fails:
        with pytest.raises(RuntimeError, match="was not written"):
            webkit_check.written(box, path)
    else:
        webkit_check.written(box, path)


def test_a_window_closed_before_the_check_finishes_fails(webkit_check, monkeypatch, tmp_path):
    # pywebview returns when the window closes, whether or not the check thread has finished.
    monkeypatch.setattr(webkit_check.webview, "start", lambda run: None)
    assert webkit_check.main(["http://127.0.0.1:1/#session=s", "--out", str(tmp_path)]) == 1
