"""The skill-choice set (S1-33; slice-1 spec section 14, "The agent picks the right skill and tools"; SPEC:1182, 1195,
1220): labeled researcher messages replayed offline through the agent loop the app runs, with recorded model replies.
No model is called: whether a real model picks the right skill is measured only by the preview's paid evaluation,
which M2 never makes (Harold, 2026-10-21); it records its replies in the form this runner replays.

    uv run python -I evals/skill_choice.py [--recorded FILE] [--json OUT]

`skill_choice/messages.jsonl` is Claude's draft: 150 synthetic messages, 75 English and 75 Chinese, each labeled with
its expected skill among the six first-party skills (SPEC:1054) or none, the tools it must call where its task needs
them and the tools it must not call; provisional until Harold reviews it. Each message is sent as a turn in a new
conversation of one project (an empty library, memory off) on a new data folder, and its model steps are answered
from FILE: {"responses": {message id: [step, ...]}}, each step {"calls": [{"name", "arguments"}]} or {"text": ...}.
Without FILE the oracle replies are replayed (the labels' own skill and tools), which checks the replay and its
scoring, never a model. A message passes on skill when the run activated exactly its expected skill (none for none),
and on tools when it called every required tool and no forbidden one, read from the run's record (skill_activated,
tool_call_started). Only a turn that succeeded on its recording, step for step, is evidence: a message with no
recording, one whose recording ran out (no reply is invented for it), one with steps the turn never took, and one whose
turn did not succeed fail both, each with its problem.

It prints, as integer counts with English and Chinese apart, the preview's score (messages whose expected skill ships,
`help` and `literature-review`, or none: SPEC:1195 "The preview is evaluated only on the skills it ships") and every
message's, and the versions replayed (the core system prompt's and each shipped skill's SHA-256), so a prompt or skill
change is replayed here and listed for the paid evaluation (SPEC:761; the scope notes' reading 14). It exits 1 unless
the preview's skill passes reach 85% and its tool passes 100%.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FILE = Path(__file__).with_name("skill_choice") / "messages.jsonl"
SKILLS = ("brainstorm", "literature-search", "literature-review", "writing", "critique", "help")  # SPEC:1054
SHIPPED = ("help", "literature-review")  # the preview's (SPEC:1220)
LANGUAGES = ("en", "zh-CN")
SKILL_PERCENT, TOOLS_PERCENT = 85, 100  # SPEC:1195
ORIGIN = "http://127.0.0.1:1"
PRICING = {"prompt": "0.000001", "completion": "0.000005"}
ARGUMENTS = {  # what the oracle passes each tool a label may name
    "answer_from_library": lambda row: {"question": row["message"]},
    "search_passages": lambda row: {"query": row["message"]},
    "list_materials": lambda row: {}, "read_project_state": lambda row: {}, "read_memory": lambda row: {},
    "open_place": lambda row: {"place": "settings.general"},
}


def load(path=FILE):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def oracle(row):
    """The replies the labels describe: the expected skill activated, then the required tools called, then an answer."""
    steps = [{"calls": [{"name": "use_skill", "arguments": {"name": row["expected_skill"]}}]}] if row["expected_skill"] else []
    if row["required_tools"]:
        steps.append({"calls": [{"name": tool, "arguments": ARGUMENTS[tool](row)} for tool in row["required_tools"]]})
    return [*steps, {"text": "Done."}]


def versions():
    """What is replayed: the core system prompt (with the agent's rules) and each skill as loaded, by SHA-256."""
    from backend import runs, skills
    loaded, _ = skills.load()
    return {"core_prompt": hashlib.sha256((runs.SYSTEM_RULES + runs.AGENT_RULES).encode()).hexdigest(),
            "skills": {name: skill.sha256 for name, skill in sorted(loaded.items())}}


class _Body(httpx.AsyncByteStream):
    """An answer as a server's arrives, read by the caller (the catalog reader bounds a body itself)."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class Replay:
    """The model provider, in process: the catalog and endpoint listings a turn's admission reads, the recorded steps
    of the message being replayed (`steps`), and fixed replies for the calls a turn may also make (the support judge,
    answer_from_library's own answer step, a memory extraction)."""

    def __init__(self):
        self.steps, self.ran_out = [], False

    async def __call__(self, request):
        from backend import budget_router
        from backend.citations import JUDGE_RULES
        from backend.memory import EXTRACT_RULES

        def answer(payload):
            return httpx.Response(200, stream=_Body(json.dumps(payload).encode()), headers={"content-type": "application/json"})
        path = request.url.path
        if path.endswith("/models"):
            return answer({"data": [{"id": m, "context_length": 128000, "pricing": PRICING}
                                    for m in sorted(budget_router.RECOMMENDED)]})
        if path.endswith("/endpoints") and "/models/" in path:
            return answer({"data": {"endpoints": [{"tag": "replay", "context_length": 128000, "pricing": PRICING,
                                                   "supported_parameters": ["max_tokens", "tools", "tool_choice"]}]}})
        if "/endpoints/" in path:
            return answer({"data": []})
        body = json.loads(request.content)
        first = body["messages"][0]["content"]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0001}
        if first.startswith(JUDGE_RULES):
            count = body["messages"][-1]["content"].count("Pair ")
            text = "\n".join(f"{i}: supported" for i in range(1, count + 1))
        elif first.startswith(EXTRACT_RULES):
            text = '{"records": []}'
        elif not body.get("tools"):  # answer_from_library's own answer step
            text = "The library holds nothing on this."
        elif not self.steps:  # a step the recording has no reply for: it fails, never answered by an invented one
            self.ran_out = True
            return httpx.Response(500, json={"error": {"message": "no recorded step"}})
        else:
            step = self.steps.pop(0)
            if "calls" in step:
                calls = [{"id": f"call_{n}_{c['name']}", "type": "function",
                          "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                         for n, c in enumerate(step["calls"])]
                return answer({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls},
                                            "finish_reason": "tool_calls"}], "usage": usage})
            text = step["text"]
        return answer({"choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage})


def score(row, activated, called, problem=None):
    expected = {row["expected_skill"]} if row["expected_skill"] else set()
    return {"skill": problem is None and set(activated) == expected, "problem": problem,
            "tools": problem is None and set(row["required_tools"]) <= set(called)
            and not set(row["forbidden_tools"]) & set(called)}


async def replay(rows, recorded=None):
    """Each row's turn replayed: [{"id", "language", "preview", "skill", "tools", "problem", "activated", "called",
    "status"}]; problem is None, no_recording, recording_ran_out, recording_left_over or run_<status>."""
    from backend.app import create_app
    from backend.self_test import _Keys
    provider = Replay()
    results = []
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Path(folder) / "data", origin=ORIGIN, keyring_backend=_Keys(),
                         transport=httpx.MockTransport(provider))
        async with app.app.router.lifespan_context(app.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                         headers={"X-Scholia-Client": "local"}, timeout=120) as client:
                await client.post("/api/setup", json={"openrouter_key": "sk-or-replay-not-a-real-key"})
                project = (await client.post("/api/projects", json={"name": "Skill choice"})).json()["id"]
                settings = (await client.get("/api/settings", params={"project_id": project})).json()
                await client.put("/api/settings", json={"project_id": project, "hash": settings["hash"],
                                                        "updates": {"memory.enabled": False}})
                read = app.app.state.scholia["db"].read
                for row in rows:
                    preview = row["expected_skill"] in (*SHIPPED, None)
                    if recorded is not None and row["id"] not in recorded:
                        results.append({"id": row["id"], "language": row["language"], "preview": preview,
                                        **score(row, [], [], "no_recording"), "activated": [], "called": [],
                                        "status": None})
                        continue
                    conversation = (await client.post("/api/conversations", json={
                        "project_id": project, "title": row["id"]})).json()["id"]
                    provider.steps = list(recorded[row["id"]] if recorded is not None else oracle(row))
                    provider.ran_out = False
                    response = await client.post(f"/api/conversations/{conversation}/message/stream",
                                                 json={"content": row["message"]})
                    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                    run_id = events[0]["run_id"] if events and events[0].get("type") == "run_started" else None
                    record = await asyncio.to_thread(read, lambda conn: conn.execute(
                        "SELECT type, json_extract(data, '$.name') FROM run_events WHERE run_id = ?"
                        " AND type IN ('skill_activated', 'tool_call_started') ORDER BY seq", (run_id,)).fetchall())
                    activated = [n for kind, n in record if kind == "skill_activated"]
                    called = [n for kind, n in record if kind == "tool_call_started" and n]
                    status = events[-1].get("status") if events else response.status_code
                    problem = ("recording_ran_out" if provider.ran_out else "recording_left_over" if provider.steps
                               else None if status == "succeeded" else f"run_{status}")
                    results.append({"id": row["id"], "language": row["language"], "preview": preview,
                                    **score(row, activated, called, problem), "activated": activated, "called": called,
                                    "status": status})
    return results


def report(results, recorded_name):
    def counts(chosen):
        found = {}
        for language in LANGUAGES:
            mine = [r for r in chosen if r["language"] == language]
            found[language] = {"messages": len(mine), "skill": sum(r["skill"] for r in mine),
                               "tools": sum(r["tools"] for r in mine), "invalid": sum(bool(r["problem"]) for r in mine)}
        return found
    preview = [r for r in results if r["preview"]]
    total = Counter()
    for r in preview:
        total.update(messages=1, skill=r["skill"], tools=r["tools"])
    met = total["messages"] > 0 and total["skill"] * 100 >= SKILL_PERCENT * total["messages"] \
        and total["tools"] * 100 >= TOOLS_PERCENT * total["messages"]
    return {"set_sha256": hashlib.sha256(FILE.read_bytes()).hexdigest(),
            "recorded": recorded_name, "versions": versions(), "preview": counts(preview), "all": counts(results),
            "preview_total": dict(total), "met": met,
            "failures": [r for r in results if not (r["skill"] and r["tools"])]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recorded", help="recorded replies to replay (default: the labels' oracle)")
    parser.add_argument("--json", help="write the report here as JSON")
    args = parser.parse_args(argv)
    rows = load()
    recorded = json.loads(Path(args.recorded).read_text(encoding="utf-8"))["responses"] if args.recorded else None
    results = asyncio.run(replay(rows, recorded))
    found = report(results, Path(args.recorded).name if args.recorded else "oracle")
    for failure in (f for f in found["failures"] if f["preview"]):  # a skill that does not ship cannot be activated
        print(f"{failure['id']}: " + (f"not evidence ({failure['problem']})" if failure["problem"] else
              f"skill {'ok' if failure['skill'] else 'wrong'}, tools {'ok' if failure['tools'] else 'wrong'}"
              f" (activated {failure['activated']}, called {failure['called']})"))
    for name in ("preview", "all"):
        print(name, ", ".join(f"{lang}: skill {c['skill']}/{c['messages']}, tools {c['tools']}/{c['messages']},"
                              f" not evidence {c['invalid']}" for lang, c in found[name].items()))
    print("versions:", json.dumps(found["versions"]))
    if args.json:
        Path(args.json).write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if found["met"] else 1


if __name__ == "__main__":
    sys.exit(main())
