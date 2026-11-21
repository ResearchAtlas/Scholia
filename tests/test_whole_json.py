"""The backend's JSON keeps text whole (design review AA, Option 4): backend/whole_json.py decodes and writes it, with
half a surrogate pair the replacement character, and backend/json_answer.py answers it. The census has no judgment in
it: no backend module imports json or names JSONResponse but those two, and the exceptions named below with their
reason; both apps answer through the class."""

import ast
import json as stdlib_json
from decimal import Decimal
from pathlib import Path

from backend import whole_json

BACKEND = Path(__file__).resolve().parent.parent / "backend"
# modules that keep the standard library's json, and why
KEEPS_JSON = {
    "whole_json.py": "it is the one that keeps text whole",
    "reading.py": "a reading child's frame holding half a pair is refused (reference_files.valid), not cleaned",
    "outbound_gate.py": "the body it checks is the body sent, as it is",
    "self_test.py": "the self-test's own requests and answers, outside the product",
}
# the .json() decodes of an HTTP answer left as they are, by module and function, and why
DECODES = {
    ("app.py", "_valid_text"): "refuses a request body holding half a pair",
    ("openrouter.py", "_json"): "through whole_text",
    ("local_helper.py", "_post"): "the local helper's numbers: cleaning would encode every vector again",
    ("endpoint_pricing.py", "fetch_endpoints"): "no caller in the product",
    ("self_test.py", "check_backend"): "the self-test's own answers, outside the product",
    ("self_test.py", "drive"): "the self-test's own answers, outside the product",
}


def modules():
    return sorted(BACKEND.rglob("*.py"))


def test_no_backend_module_uses_json_or_a_json_answer_but_through_the_ones_that_keep_text_whole():
    imports, answers, decodes, apps = [], [], [], []
    for path in modules():
        name = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "json" for alias in node.names) \
                    or isinstance(node, ast.ImportFrom) and node.module == "json":
                if name not in KEEPS_JSON:
                    imports.append(name)
            if isinstance(node, ast.Name) and node.id == "JSONResponse" \
                    or isinstance(node, ast.alias) and node.name == "JSONResponse":
                if name != "json_answer.py":
                    answers.append(name)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "FastAPI" or \
                    isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FastAPI":
                apps.append((name, any(k.arg == "default_response_class" and getattr(k.value, "id", "") == "WholeJSONResponse"
                                       for k in node.keywords)))
        for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "json" and not node.args:
                    decodes.append((name, function.name))
    assert imports == [], "modules that import json itself: `from backend import whole_json as json`"
    assert answers == [], "modules that name JSONResponse: backend.json_answer.WholeJSONResponse"
    assert sorted(set(decodes) - set(DECODES)) == [], ".json() decodes of an answer, as they are: whole_json.loads"
    assert sorted(set(DECODES) - set(decodes)) == [], "named decodes that no longer exist"
    assert sorted(apps) == [("app.py", True), ("data_folder.py", True)], "an app whose default answer is not the class"


def test_loads_and_dumps_are_jsons_but_for_half_a_surrogate_pair():
    # half a pair in a value and in a key, as an escape and as raw bytes
    assert whole_json.loads('{"a\\ud83d": ["b\\udc00c"]}') == {"a\ufffd": ["b\ufffdc"]}
    raw = '{"a": "b\\ud83dc"}'.replace("\\ud83d", "\ud83d").encode("utf-8", "surrogatepass")
    assert stdlib_json.loads(raw) == {"a": "b\ud83dc"}  # the standard library keeps it from bytes too
    assert whole_json.loads(raw) == {"a": "b\ufffdc"}
    # a pair kept, as an escape and as text; an escaped backslash before u is no escape of half a pair
    assert whole_json.loads('"\\ud83d\\ude00 \U0001F600 \\\\ud83d"') == "\U0001F600 \U0001F600 \\ud83d"
    # the options passed on
    assert whole_json.loads('{"n": 1.5}', parse_float=Decimal) == {"n": Decimal("1.5")}  # whole: as json.loads
    assert whole_json.loads('[1, "\\ud83d"]', parse_int=str) == ["1", "\ufffd"]
    # dumps: half a pair is written as the replacement character, whatever the options; a pair as it is
    assert whole_json.dumps({"b": "x\ud83d", "a": 1}, sort_keys=True) == '{"a": 1, "b": "x\\ufffd"}'
    assert whole_json.dumps({"k\udc00": "x"}, ensure_ascii=False) == '{"k\ufffd": "x"}'
    assert whole_json.dumps("\U0001F600") == stdlib_json.dumps("\U0001F600")
    assert whole_json.dumps("\U0001F600", ensure_ascii=False) == '"\U0001F600"'
    assert whole_json.dumps("\\ud83d") == stdlib_json.dumps("\\ud83d")  # the text of an escape, not half a pair
    # the same object returned when the value is whole
    whole = {"a": ["b", 1]}
    assert whole_json.whole_text(whole) is whole


def test_the_answer_class_writes_half_a_surrogate_pair_as_the_replacement_character():
    from backend.json_answer import WholeJSONResponse
    assert WholeJSONResponse({"a": "x\ud83d"}).body == '{"a":"x\ufffd"}'.encode()
    assert WholeJSONResponse({"a": "x"}).body == b'{"a":"x"}'
