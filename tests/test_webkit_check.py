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
