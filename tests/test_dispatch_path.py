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
    """Lines that construct a context.Request (Request(...) or context.Request(...)), import it by name,
    or copy a request with changes (dataclasses.replace)."""
    return [node.lineno for node in ast.walk(ast.parse(source))
            if (isinstance(node, ast.Call)
                and ((isinstance(node.func, ast.Name) and node.func.id == "Request")
                     or (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                         and ((node.func.value.id, node.func.attr) == ("context", "Request")
                              or ((node.func.value.id, node.func.attr) == ("dataclasses", "replace") and node.args
                                  and "request" in ast.unparse(node.args[0]).lower())))))
            or (isinstance(node, ast.ImportFrom) and (node.module or "").endswith("context")
                and any(a.name in ("Request", "*") for a in node.names))]


def test_only_the_dispatch_path_calls_the_provider_adapter():
    uses = {str(path.relative_to(ROOT)): callers(path.read_text()) for path in MODULES}
    assert {name: found for name, found in uses.items() if found} == {
        "backend/runs.py": [("_call", uses["backend/runs.py"][0][1])]}
    # Nor by name in a string (getattr, a module's __dict__), nor posting to a chat URL itself: the
    # adapter's own module is the only one that sends a chat request.
    for path in MODULES:
        text = path.read_text()
        allowed = {"openrouter.py": 1, "runs.py": 1}.get(path.name, 0) if path.parent.name == "backend" else 0
        assert text.count("query_model") <= allowed + (1 if path.name == "openrouter.py" else 0), path
        assert "chat_url" not in text or path.name in ("openrouter.py", "providers.py"), path


def test_the_dispatch_path_sends_what_the_assembler_built():
    source = (ROOT / "backend/runs.py").read_text()
    call = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "_call")
    text = ast.unparse(call)
    assert "isinstance(request, context.Request)" in text  # refused otherwise, before anything is sent
    assert "context.serialized_tokens(request.messages, request.tools) + request.max_tokens > request.window" \
        in text  # and one that does not fit, counted again as it is sent
    assert "b.status = 'open'" in text  # an admitted call whose reservation is still open
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
    for planted in ("from backend import context\nr = context.Request('answer', [], 1, 1, 1)\n",
                    "from backend.context import Request as R\n",
                    "import dataclasses\nr = dataclasses.replace(request, messages=[])\n"):
        assert requests_built(planted), planted
