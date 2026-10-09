import hashlib
import json
import math
import queue
import sys
from http.server import BaseHTTPRequestHandler

import pytest

from backend import self_test as st
from network_guard import mock_http_server


def test_sqlite_and_index_checks_pass_from_source():
    assert st.check_sqlite()["sqlite"]
    assert st.check_index()["sqlite_vec"] == st.SQLITE_VEC_VERSION


def test_the_backend_check_runs_a_turn_from_source():
    assert st.check_backend() == {"turn": "succeeded"}


def test_the_interface_check_serves_the_page_and_what_it_names(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("console.log(1)")
    (tmp_path / "assets/app.css").write_text("body{}")
    page = '<script src="/assets/app.js"></script><link href="/assets/app.css"><div id="root"></div>'
    (tmp_path / "index.html").write_text(page)
    assert st.check_interface(tmp_path) == {"assets": 2}
    (tmp_path / "assets/app.css").unlink()
    with pytest.raises(RuntimeError, match="app.css is not served"):
        st.check_interface(tmp_path)
    with pytest.raises(RuntimeError, match="page is not served"):
        st.check_interface(tmp_path / "missing")


def test_the_materials_check_reads_a_pdf_and_latex_and_renders_a_page():
    assert st.check_materials() == {"pdf": "pdf-3+pypdfium2-5.14.0", "latex": "latex-2+pylatexenc-2.11"}


def test_the_encrypted_zip_check_writes_and_reads_back_an_aes_zip():
    assert st.check_encrypted_zip() == {"aes": True}


def test_the_encrypted_zip_check_fails_on_a_zip_that_is_not_encrypted(monkeypatch):
    from backend import backups
    real = backups._write_zip
    monkeypatch.setattr(backups, "_write_zip", lambda *args, **options: real(*args[:3], None, **options))
    with pytest.raises(RuntimeError, match="not AES-encrypted"):
        st.check_encrypted_zip()


def test_sqlite_checks_refuse_a_version_before_secure_delete(monkeypatch):
    monkeypatch.setattr(st, "MIN_SQLITE", (99, 0, 0))
    with pytest.raises(RuntimeError, match="older than 3.42"):
        st.check_sqlite()
    with pytest.raises(RuntimeError, match="older than 3.42"):
        st.check_index()


@pytest.mark.skipif(sys.platform != "darwin", reason="Vision is macOS only")
def test_ocr_reads_the_english_and_chinese_lines():
    assert st.check_ocr()["lines"] == st.OCR_LINES


def test_helper_flags():
    assert st.helper_command("/app/llama-server", "/models/m.gguf") == [
        "/app/llama-server", "-m", "/models/m.gguf", "--offline", "--host", "127.0.0.1",
        "--port", "0", "--no-webui", "--embedding", "--pooling", "last",
        "-c", "4096", "-ub", "2048", "-np", "2", "--cache-ram", "0",
    ]


def _lines(*items):
    q = queue.Queue()
    for item in items:
        q.put(item)
    return q


def test_port_comes_from_the_listening_line():
    log = []
    lines = _lines("load_model: loading\n", "srv  main: listening on http://127.0.0.1:50123\n")
    assert st.wait_for_port(lines, log, st.time.monotonic() + 1) == 50123
    assert len(log) == 2


def test_helper_that_exits_or_stays_silent_is_not_ready():
    with pytest.raises(RuntimeError, match="exited before it was ready"):
        st.wait_for_port(_lines("error: bad model\n", None), [], st.time.monotonic() + 1)
    with pytest.raises(RuntimeError, match="not ready within"):
        st.wait_for_port(_lines("loading\n"), [], st.time.monotonic() + 0.1)


def _helper_stub(key, vector=(0.5,) * 4):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if key and self.headers.get("Authorization") != f"Bearer {key}":
                self.send_response(401)
                self.end_headers()
                return
            data = json.dumps({"data": [{"embedding": list(vector), "input": body["input"]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def test_embedding_request_sends_the_key_and_requests_without_it_are_refused():
    with mock_http_server(_helper_stub("k1")) as url:
        port = int(url.rsplit(":", 1)[1])
        assert st.embed(port, "k1", "text") == [0.5] * 4
        assert st.refused_without_key(port)
    with mock_http_server(_helper_stub(None)) as url:  # a helper that ignores the key
        assert not st.refused_without_key(int(url.rsplit(":", 1)[1]))


def test_model_must_match_its_pin(tmp_path, monkeypatch):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"weights")
    pin = {"size": 7, "sha256": hashlib.sha256(b"weights").hexdigest()}
    monkeypatch.setattr(st, "EMBEDDING_MODEL", pin)
    st.verify_model(model)
    model.write_bytes(b"Weights")
    with pytest.raises(RuntimeError, match="SHA-256"):
        st.verify_model(model)
    model.write_bytes(b"weights!")
    with pytest.raises(RuntimeError, match="8 bytes, expected 7"):
        st.verify_model(model)


def test_every_check_runs_and_any_failure_fails_the_self_test(monkeypatch, tmp_path, capsys):
    calls = []

    def passing(name):
        return lambda *args: calls.append(name) or {}

    def failing(*args):
        calls.append("embedding")
        raise RuntimeError("no helper")

    for name in ("check_sqlite", "check_index", "check_backend", "check_interface", "check_encrypted_zip",
                 "check_materials", "check_ocr"):
        monkeypatch.setattr(st, name, passing(name))
    monkeypatch.setattr(st, "check_embedding", failing)
    argv = ["--self-test", "--model", str(tmp_path / "m.gguf")]
    assert st.main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["checks"]["embedding"] == {"ok": False, "error": "RuntimeError: no helper"}
    assert result["checks"]["ocr"] == {"ok": True}
    assert len(calls) == 8
    monkeypatch.setattr(st, "check_embedding", passing("embedding"))
    assert st.main(argv) == 0


def test_self_test_mode_is_required():
    with pytest.raises(SystemExit):
        st.main(["--model", "m.gguf"])


UNIT = [1 / math.sqrt(st.DIMENSIONS)] * st.DIMENSIONS


@pytest.mark.parametrize(
    "vector, error",
    [
        ([0.0] * st.DIMENSIONS, "norm is 0.0000"),  # a model that did not run
        ([0.5] * st.DIMENSIONS, "norm is 16.0000"),  # not normalized
        (UNIT[:-1], "1023 values"),
        ([float("nan")] + UNIT[1:], "finite"),
    ],
)
def test_helper_embeddings_must_be_unit_vectors(vector, error):
    with mock_http_server(_helper_stub("k1", vector)) as url:
        returned = st.embed(int(url.rsplit(":", 1)[1]), "k1", "text")
    with pytest.raises(RuntimeError, match=error):
        st.verify_embedding(returned)


def test_a_unit_embedding_passes():
    with mock_http_server(_helper_stub("k1", UNIT)) as url:
        returned = st.embed(int(url.rsplit(":", 1)[1]), "k1", "text")
    assert st.verify_embedding(returned) == pytest.approx(1.0)
