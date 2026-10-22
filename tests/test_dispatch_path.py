"""One dispatch path for every model call (ticket 72 B; the M2 charter's "sweep the class").

Every model call goes through Harness._call in backend/runs.py, which sends only a request the
context assembler built, for an admitted call holding its reservation, through the gate with the
dispatch check. A static check of the backend's structure, read with the AST: the only outbound
POST to a model endpoint is the adapter's (`openrouter.query_model`), the adapter is reached only
from `_call`, inside a gate client whose admit callback is the dispatch check, and the dispatch
check runs only inside a gate client's admit callback; nothing else builds a `context.Request`.
The reviewer's two bypasses (a role helper posting to /chat/completions itself, and the dispatch
check moved out of the gate's callback) are planted here to show the check finds them.
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


# POSTs the backend may make, by module and function: the adapter's, to a model provider; the local
# helper's, to the embedding and reranking helper on loopback (no model call); and the frozen
# self-test's, to the app in process (test_no_bypass.py checks its transport).
POSTS = {("openrouter.py", "query_model"), ("local_helper.py", "_post"), ("self_test.py", "drive")}
# Modules that may name a model endpoint: the adapter, the provider routes (chat_url), the gate, which
# checks the path a Private request uses, and the self-test, which checks the calls its stand-in saw.
NAMES_ENDPOINT = {"openrouter.py", "providers.py", "outbound_gate.py", "self_test.py"}


def _functions(tree):
    """(node, the name of the function it is in) for every node."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            found.append((child, function))
            visit(child, child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function)

    visit(tree, None)
    return found


def _admit_callbacks(tree):
    """The callbacks passed as admit= to a gate client (async_client): lambdas, and functions by name."""
    lambdas, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "async_client":
            for keyword in node.keywords:
                if keyword.arg == "admit" and isinstance(keyword.value, ast.Lambda):
                    lambdas.add(id(keyword.value))
                elif keyword.arg == "admit" and isinstance(keyword.value, ast.Name):
                    names.add(keyword.value.id)
    return lambdas, names


def structure_problems(sources):
    """What breaks the dispatch path's structure, for {module file name: source}."""
    problems = []
    for name, source in sources.items():
        tree = ast.parse(source)
        decorators = {id(d) for n in ast.walk(tree) if hasattr(n, "decorator_list") for d in n.decorator_list}
        lambdas, callbacks = _admit_callbacks(tree)
        for node, function in _functions(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and id(node) not in decorators:
                method = node.func.attr
                post = method == "post" or (method in ("stream", "request") and node.args
                                            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "POST")
                if post and (name, function) not in POSTS:
                    problems.append(f"{name}:{node.lineno} posts outside the adapter")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "completions" in node.value \
                    and name not in NAMES_ENDPOINT:
                problems.append(f"{name}:{node.lineno} names a model endpoint")
        # The dispatch check, wherever it is used, runs in a gate client's admit callback.
        def inside(node, parents):
            return any(id(p) in lambdas or (isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and p.name in callbacks)
                       for p in parents)

        def walk(node, parents):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Name) and child.id == "may_dispatch" and not inside(child, parents):
                    problems.append(f"{name}:{child.lineno} checks dispatch outside a gate's admit callback")
                walk(child, [*parents, child])

        walk(tree, [])
    runs = ast.parse(sources["runs.py"])
    call = next(n for n in ast.walk(runs) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call")
    lambdas, _ = _admit_callbacks(call)
    guarded = [w for w in ast.walk(call) if isinstance(w, ast.AsyncWith) and any(
        isinstance(i.context_expr, ast.Call) and any(k.arg == "admit" and id(k.value) in lambdas
                                                     and "may_dispatch" in ast.unparse(k.value)
                                                     for k in i.context_expr.keywords) for i in w.items)]
    if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "query_model"
               for w in guarded for n in ast.walk(w)):
        problems.append("runs.py: _call does not send inside a gate client whose admit callback checks dispatch")
    return problems


def _sources():
    """Every module under backend/, nested packages included, by its path there ("db/database.py")."""
    return {str(path.relative_to(ROOT / "backend")): path.read_text() for path in MODULES}


def test_the_dispatch_path_keeps_its_structure():
    assert structure_problems(_sources()) == []


def test_the_structure_check_finds_the_reviewers_two_bypasses():
    # A role helper that posts to the model endpoint itself, through a gate client: no assembly, no
    # reservation, no dispatch check.
    planted = {**_sources(), "new_role.py": (
        "async def judge(gate, project, provider, body):\n"
        "    async with gate.async_client(project) as client:\n"
        '        return await client.post(provider.base_url + "/chat/completions", json=body)\n')}
    assert any("new_role.py" in problem for problem in structure_problems(planted))
    # The dispatch check moved into an ordinary read, and the gate's admit callback removed.
    sources = _sources()
    old = "async with self.gate.async_client(project_id[0], admit=lambda conn: may_dispatch(conn, active.run_id, past),"
    assert old in sources["runs.py"]
    sources["runs.py"] = sources["runs.py"].replace(old, "await self._read(lambda conn: may_dispatch(conn, active.run_id, past))\n"
                                                          "        async with self.gate.async_client(project_id[0],")
    problems = structure_problems(sources)
    assert any("outside a gate's admit callback" in p for p in problems) and any("_call does not send" in p
                                                                                 for p in problems)
    # A role helper in a nested package that posts to the model endpoint itself (external review a2).
    planted = {**_sources(), "roles/judge.py": (
        "async def judge(gate, project, provider, body):\n"
        "    async with gate.async_client(project) as client:\n"
        '        return await client.post(provider.base_url + "/chat/completions", json=body)\n')}
    assert any(problem.startswith("roles/judge.py") for problem in structure_problems(planted))


def test_every_backend_module_is_checked():
    assert "db/database.py" in _sources() and len(_sources()) == len(MODULES)


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
