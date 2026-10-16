"""One dispatch path for every model call (ticket 72 B; the M2 charter's "sweep the class").

Every model call goes through Harness._call in backend/runs.py, which sends only a request the
context assembler built, for an admitted call holding its reservation, through the gate with the
dispatch check. A static check over the backend: nothing else calls `openrouter.query_model`,
names it to call it later, or builds a `context.Request` outside the assembler.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted((ROOT / "backend").rglob("*.py"))


def callers(source):
    """(function, line) for every use of query_model in source other than its own definition: a call,
    an attribute read or an import of the name."""
    tree = ast.parse(source)
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Attribute) and child.attr == "query_model":
                found.append((function, child.lineno))
            elif isinstance(child, ast.Name) and child.id == "query_model":
                found.append((function, child.lineno))
            elif isinstance(child, ast.ImportFrom) and any(a.name in ("query_model", "*") for a in child.names) \
                    and (child.module or "").endswith("openrouter"):
                found.append((function, child.lineno))
            visit(child, name)

    visit(tree, None)
    return found


def requests_built(source):
    """Lines that construct a context.Request (Request(...) or context.Request(...))."""
    return [node.lineno for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == "Request")
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == "Request"
                     and isinstance(node.func.value, ast.Name) and node.func.value.id == "context"))]


def test_only_the_dispatch_path_calls_the_provider_adapter():
    uses = {path.name: callers(path.read_text()) for path in MODULES}
    assert {name: found for name, found in uses.items() if found} == {
        "runs.py": [("_call", uses["runs.py"][0][1])]}
    assert all(function != "query_model" for function, _ in uses["openrouter.py"])  # defined there, never called


def test_the_dispatch_path_sends_what_the_assembler_built():
    source = (ROOT / "backend/runs.py").read_text()
    call = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "_call")
    text = ast.unparse(call)
    assert "isinstance(request, context.Request)" in text  # refused otherwise, before anything is sent
    adapter = next(node for node in ast.walk(call) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "query_model")
    assert ast.unparse(adapter.args[3]) == "request.messages"
    assert ast.unparse(next(k.value for k in adapter.keywords if k.arg == "max_tokens")) == "request.max_tokens"
    assert "may_dispatch(conn, active.run_id)" in text  # the dispatch check, in the gate's decision transaction
    assert "call.reservation_id" in text  # an admitted call: its reservation is read, settled or released
    for path in MODULES:  # only the assembler makes a request
        if path.name != "context.py":
            assert requests_built(path.read_text()) == [], path.name


def test_the_checks_catch_another_caller():
    # A module planted with a second caller, in any of the forms the check reads, is found.
    for planted in ("from backend import openrouter\nasync def f(c):\n    await openrouter.query_model(c)\n",
                    "from backend.openrouter import query_model\n",
                    "import backend.openrouter as o\nsend = o.query_model\n"):
        assert callers(planted), planted
    assert requests_built("from backend import context\nr = context.Request('answer', [], 1, 1, 1)\n")
