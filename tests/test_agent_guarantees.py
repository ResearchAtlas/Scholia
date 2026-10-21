"""The app's guarantees whatever the agent does (slice-1 spec section 14's test_agent_guarantees.py row, S1-19's
rows; S1-19c): a skill's `allowed-tools` is never honored; skill scripts never run; `read_skill_file` takes
only pinned file ids; an ask offers at most three options; asks are answered only through the ask endpoint;
the review lock holds at each skill activation and tool call. Nothing leaves the process."""

import ast
import asyncio
import json
import pathlib
import shutil

import pytest

from agentic import Scripted, call, tool_messages
from backend import asks, skills
from cited import cited, conversation_in, events_of, with_papers
from scholia_app import send
from test_agent_loop import asked
from test_review_lock import lock
from test_search import WAGES

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """The bundled skills with a script and an `allowed-tools` field added to `help`, pinned as if approved."""
    root = tmp_path / "scholia.skills"
    shutil.copytree(skills.ROOT, root)
    help_ = root / "help" / "SKILL.md"
    help_.write_text(help_.read_text().replace("description:", "allowed-tools: Bash Write\ndescription:", 1))
    canary = tmp_path / "ran"
    (root / "help" / "scripts").mkdir()
    (root / "help" / "scripts" / "run.py").write_text(f"open({str(canary)!r}, 'w').write('ran')\n")
    skills.pin(root)
    monkeypatch.setattr(skills, "ROOT", root)
    return canary


@pytest.mark.asyncio
async def test_allowed_tools_is_never_honored_and_no_skill_script_runs(tmp_path, bundle):
    provider = Scripted([call("use_skill", name="help")], [call("read_skill_file", call_id="c2", file_id="help#1")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "How do I add papers?")
        first, *later = provider.agent_steps
        assert all(b["tools"] == first["tools"] for b in later)  # the same tools before and after activation
        [activated] = tool_messages(later[0])
        assert "allowed-tools" not in activated["content"] and "scripts/run.py" not in activated["content"]
        assert json.loads(tool_messages(later[1])[-1]["content"])["error"] == "validation_error"  # not offered by id
        assert not bundle.exists()  # never run


@pytest.mark.asyncio
async def test_an_ask_offers_at_most_three_options(tmp_path):
    provider = Scripted([call("ask", question="Which?", options=["a", "b", "c", "d"])], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Which papers do I have?")
        assert json.loads(tool_messages(provider.agent_steps[1])[-1]["content"])["error"] == "validation_error"
        assert not [k for k, _, _ in await events_of(client, stream[0]["run_id"]) if k == "ask"]
    with pytest.raises(ValueError):
        asks.raise_ask(None, "run", "choice", ["a", "b", "c", "d"])


def test_asks_are_answered_only_through_the_ask_endpoint():
    """No module calls asks.answer but the endpoint (backend/asks.py answer_ask): no tool, step or skill can."""
    callers = []
    for path in (ROOT / "backend").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == "answer" \
                    and path.name != "asks.py":
                callers.append(f"{path.name}:{node.lineno}")
    assert callers == []
    tree = ast.parse((ROOT / "backend" / "asks.py").read_text())
    users = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
             for node in ast.walk(fn) if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "answer"}
    assert users == {"answer_ask"}


@pytest.mark.asyncio
async def test_the_review_lock_stops_the_loop_before_its_next_tool_call_or_skill_activation(tmp_path):
    provider = Scripted([call("ask", question="Go on?", options=["Yes"])],
                        [call("use_skill", call_id="c2", name="help"), call("list_materials", call_id="c3")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        sending = asyncio.create_task(send(client, conversation, "Which papers do I have?"))
        ask = await asked(client, conversation)
        locked = await lock(client, project, venue="A JOURNAL")
        assert locked.status_code == 200, locked.text
        refused = await client.post(f"/api/runs/{ask['run_id']}/asks/{ask['ask_id']}", json={"option": "Yes"})
        assert refused.status_code == 409  # the ask is invalid under the new policy, or its run has ended
        stream = await sending
        assert (stream[-1]["status"], stream[-1].get("cancel_reason")) == ("cancelled", "revoked")
        assert len(provider.agent_steps) == 1  # nothing more went out
        events = await events_of(client, ask["run_id"])
        assert not [k for k, _, _ in events if k in ("skill_activated",)]
        assert [d.get("withdrawn") for k, d, _ in events if k == "ask_answered"] == ["run_ended"]
