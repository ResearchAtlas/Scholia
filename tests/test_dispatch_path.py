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

import pytest

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
    old = ("async with self.gate.async_client(project_id[0], admit=lambda conn: may_dispatch(conn, active.run_id, past, off,\n"
           + " " * 89 + "revoked),")
    assert old in sources["runs.py"]
    sources["runs.py"] = sources["runs.py"].replace(
        old, "await self._read(lambda conn: may_dispatch(conn, active.run_id, past, off, revoked))\n"
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


# S1-33 (PR 52 cycle 3, design review A): one recorder of what each call's attempts did. The settlement authority is
# reached only through it, so no other reader of a stand-in (a sticky flag, a body count) can settle a call.


def calls_to(source, name):
    """(the chain of functions it is in, line) for each call of `name`, as a plain name or an attribute, and each import
    of it by name."""
    found = []

    def visit(node, chain):
        for child in ast.iter_child_nodes(node):
            inner = [*chain, child.name] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else chain
            if isinstance(child, ast.Call) and name in (getattr(child.func, "attr", None), getattr(child.func, "id", None)):
                found.append((tuple(chain), child.lineno))
            elif isinstance(child, ast.ImportFrom) and any(a.name == name for a in child.names):
                found.append((tuple(chain), child.lineno))
            visit(child, inner)

    visit(ast.parse(source), [])
    return found


def recorder_problems(sources):
    """What breaks the one-recorder rule, for {module file name: source}."""
    problems = []
    for name, source in sources.items():
        for callee, allowed in (("settle", "_settle_attempts"), ("_settle_attempts", "_record_attempts"),
                                ("settle_left_open", "recover")):
            for chain, line in calls_to(source, callee):
                if name != "runs.py" or allowed not in chain:
                    problems.append(f"{name}:{line} calls {callee} outside {allowed}")
    runs = ast.parse(sources["runs.py"])
    record = next(n for n in ast.walk(runs) if isinstance(n, ast.FunctionDef) and n.name == "_record_attempts")
    close = next(n for n in ast.walk(runs) if isinstance(n, ast.FunctionDef) and n.name == "_close_call")
    call = next(n for n in ast.walk(runs) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call")
    for function in (close, call):  # the receipt and the close of a call cut off: each through the one recorder
        if "_record_attempts(" not in ast.unparse(function):
            problems.append(f"runs.py: {function.name} does not record through _record_attempts")
    if "call.attempts" not in ast.unparse(record):
        problems.append("runs.py: _record_attempts does not read the call's own attempts")
    adapter = next(n for n in ast.walk(call) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "query_model")
    if not any(k.arg == "attempts" and ast.unparse(k.value) == "call.attempts" for k in adapter.keywords):
        problems.append("runs.py: _call does not give the adapter the call's own list of attempts")
    for node in ast.walk(runs):  # a call's dispatch is read from its attempts, never set
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
            node, (ast.AugAssign, ast.AnnAssign)) else []
        for target in (t for whole in targets for t in ast.walk(whole)):
            if isinstance(target, ast.Attribute) and target.attr == "dispatched":
                problems.append(f"runs.py:{node.lineno} sets a dispatched flag")
    return problems


def test_one_recorder_settles_every_model_call():
    assert recorder_problems(_sources()) == []
    from backend import runs
    with pytest.raises(AttributeError):  # read from its attempts only
        runs._Call("r", 0, "b").dispatched = True


def test_the_recorder_check_finds_a_second_settlement_and_a_sticky_flag():
    sources = _sources()
    planted = {**sources, "runs.py": sources["runs.py"] + (
        "\n\ndef _close_quickly(conn, call):\n    spending.settle(conn, call.reservation_id)\n"
        "\n\ndef _gate_said(call):\n    call.dispatched = True\n")}
    problems = recorder_problems(planted)
    assert any("calls settle outside _settle_attempts" in p for p in problems)
    assert any("sets a dispatched flag" in p for p in problems)
    planted = {**sources, "app.py": sources["app.py"] + "\nfrom backend.spending import settle_left_open\n"}
    assert any(p.startswith("app.py") for p in recorder_problems(planted))
    old = "_record_attempts(conn, call, _running(conn, call.run_id))"
    assert old in sources["runs.py"]
    unrecorded = {**sources, "runs.py": sources["runs.py"].replace(old, "spending.release(conn, call.reservation_id)")}
    assert any("_close_call does not record" in p for p in recorder_problems(unrecorded))


# S1-33 (PR 52 cycle 4, design review A2's Option 2): what an attempt did has one store, written when the request
# leaves. The gate's notice (the post's DISPATCHED extension) adds the attempt to the caller's list itself, as sent;
# only that notice and the attempt's ending (end) add to the list or decide whether an attempt was sent, so no exit
# between the notice and the ending can leave a request that went out unrecorded.


def adapter_problems(source):
    """What breaks that rule in openrouter.py's source."""
    tree = ast.parse(source)
    adapter = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "query_model")
    notice = next((value.id for node in ast.walk(adapter) if isinstance(node, ast.Dict)
                   for key, value in zip(node.keys, node.values)
                   if isinstance(key, ast.Name) and key.id == "DISPATCHED" and isinstance(value, ast.Name)), None)
    nested = {n.name: n for n in adapter.body if isinstance(n, ast.FunctionDef)}

    def appends(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend", "insert") and ast.unparse(node.func.value) == "attempts") \
            or (isinstance(node, ast.AugAssign) and ast.unparse(node.target) == "attempts")

    def decides(node):
        return (isinstance(node, ast.Call) and any(k.arg == "dispatched" for k in node.keywords)) or (
            isinstance(node, (ast.Assign, ast.AugAssign)) and any(
                isinstance(t, ast.Attribute) and t.attr == "dispatched"
                for whole in (node.targets if isinstance(node, ast.Assign) else [node.target]) for t in ast.walk(whole)))

    problems = []
    if notice not in nested:
        problems.append("the post's DISPATCHED extension is not a function of query_model")
    elif not any(appends(n) for n in ast.walk(nested[notice])):
        problems.append(f"the gate's notice ({notice}) does not add the attempt to the caller's list")
    allowed = {("query_model", notice), ("query_model", "end")}

    def visit(node, chain):
        for child in ast.iter_child_nodes(node):
            inner = (*chain, child.name) if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else chain
            if chain not in allowed and appends(child):
                problems.append(f"openrouter.py:{child.lineno} adds to attempts outside the notice and end")
            if chain not in allowed and decides(child):
                problems.append(f"openrouter.py:{child.lineno} decides whether an attempt was sent outside the notice"
                                " and end")
            visit(child, inner)

    visit(tree, ())
    return problems


def test_the_gates_notice_records_each_attempt_as_sent_and_only_its_ending_completes_it():
    assert adapter_problems(_sources()["openrouter.py"]) == []


def test_the_notice_check_finds_a_second_append_a_notice_kept_aside_and_a_flag_set_elsewhere():
    source = _sources()["openrouter.py"]
    ending, notice = 'end("ok", response.status_code, usage)', "extensions = {DISPATCHED: dispatched}"
    assert ending in source and notice in source
    second = source.replace(ending, 'attempts.append(Attempt("ok", response.status_code, 0, usage))')
    assert any("adds to attempts outside" in p for p in adapter_problems(second))
    aside = source.replace(notice, "kept = []\n\n    def aside(terms=None):\n        kept.append(terms)\n\n"
                                   "    extensions = {DISPATCHED: aside}")
    assert any("does not add the attempt" in p for p in adapter_problems(aside))
    flagged = source.replace(ending, ending + "\n        attempts[-1].dispatched = bool(kept)")
    assert any("decides whether an attempt was sent outside" in p for p in adapter_problems(flagged))
