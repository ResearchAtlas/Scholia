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
    "data_folder.py": "its one record, the chosen folder's path, round-trips the file system's bytes as they are",
}
# JSON decoded, encoded or answered outside those two places, by module and function, and why
LEFT = {
    (".json()", "app.py", "_valid_text"): "refuses a request body holding half a pair",
    (".json()", "openrouter.py", "_json"): "through whole_text",
    (".json()", "local_helper.py", "_post"): "the local helper's numbers: cleaning would encode every vector again",
    (".json()", "endpoint_pricing.py", "fetch_endpoints"): "no caller in the product",
    ("application/json", "answers.py", "body"): "a stored outbound body served as the bytes that were sent (ASCII)",
}
OTHER_JSON = {"orjson", "simplejson", "ujson", "rapidjson"}


def modules():
    return sorted(BACKEND.rglob("*.py"))


def uses(tree):
    """(what, enclosing function) for each way the module touches JSON outside whole_json and the answer class."""
    found = []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Import):
            found.extend(("import", alias.name) for alias in node.names
                         if alias.name.split(".")[0] in {"json", *OTHER_JSON})
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"json", *OTHER_JSON}:
            found.append(("import", node.module))
        if isinstance(node, ast.Name) and node.id == "JSONResponse" or isinstance(node, ast.Attribute) and \
                node.attr == "JSONResponse" or isinstance(node, ast.alias) and node.name == "JSONResponse":
            found.append(("JSONResponse", function))
        if isinstance(node, ast.Call):
            if getattr(node.func, "attr", None) in ("json", "model_dump_json"):
                found.append((f".{node.func.attr}()", function))
            if getattr(node.func, "attr", getattr(node.func, "id", None)) == "import_module" and node.args and \
                    isinstance(node.args[0], ast.Constant) and str(node.args[0].value).split(".")[0] in {"json", *OTHER_JSON}:
                found.append(("import", node.args[0].value))
            for keyword in node.keywords:
                if keyword.arg == "json":  # httpx encodes it with the standard library
                    found.append(("json=", function))
                if keyword.arg == "media_type" and getattr(keyword.value, "value", None) == "application/json":
                    found.append(("application/json", function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_no_backend_module_uses_json_or_a_json_answer_but_through_the_ones_that_keep_text_whole():
    imports, answers, other, apps = [], [], set(), []
    for path in modules():
        name = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for what, where in uses(tree):
            if what == "import":
                if name not in KEEPS_JSON or where.split(".")[0] != "json":
                    imports.append((name, where))
            elif what == "JSONResponse":
                if name != "json_answer.py":
                    answers.append((name, where))
            elif name not in KEEPS_JSON:  # a module that keeps json keeps its decodes and encodes too
                other.add((what, name, where))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and "FastAPI" in (getattr(node.func, "attr", None), getattr(node.func, "id", None)):
                apps.append((name, any(k.arg == "default_response_class" and getattr(k.value, "id", "") == "WholeJSONResponse"
                                       for k in node.keywords)))
    assert imports == [], "modules that import json (or another JSON library) itself: `from backend import whole_json as json`"
    assert answers == [], "modules that name JSONResponse: backend.json_answer.WholeJSONResponse"
    assert sorted(other - set(LEFT)) == [], "JSON decoded, encoded or answered as it is: through whole_json"
    assert sorted(set(LEFT) - other) == [], "named uses that no longer exist"
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
