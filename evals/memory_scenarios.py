"""The memory scenarios (S1-21a; slice-1 spec section 14, "Memory behaves as specified"; Harold's spec section 26.3):
scripted multi-session scenarios, each with an exact oracle, replayed offline through the code the app runs
(backend/memory.py's commands, extraction effects, Keep and recall; the deletion service) with recorded extractor
replies. No model is called: what a real extractor proposes is measured only by a paid run, which M2 never makes.

    uv run python -I evals/memory_scenarios.py [scenarios.json]

prints each scenario that fails with the first difference, and the pass counts by language and category; it exits 1
unless every scenario passes. `memory_scenarios/scenarios.json` is Claude's draft (about 40, half English and half
Chinese): synthetic and provisional until Harold reviews it with the gold sets.

A scenario's steps run in order on a new data folder: `project`, `paper`, `turn` (a researcher's message in a
conversation, its explicit command applied as the app applies it; `papers`: what its answer drew on), `start` and
`finish` (an extraction run of a turn, started and finished apart so their order is the scenario's; `replaces` names a
record by its content), `keep`, `edit`, `forget`, `delete_paper`, `delete_conversation`, `expire`, `note` (an agent's
note) and `add_papers` (progress). The oracle is checked at the end, exactly: the active and proposed records' contents,
those marked older, what a project's turn is given (USER MEMORY's records, newest first and at most 10, then PROJECT
STATE's project state records) and Where we left off's notes.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import memory  # noqa: E402
from backend.db import Database, deletion, new_id  # noqa: E402

FILE = Path(__file__).with_name("memory_scenarios") / "scenarios.json"
CATEGORIES = ("fact_retrieval", "cross_session", "temporal", "knowledge_update", "superseded", "source_deletion",
              "abstention", "project_state_recovery")
ORDERINGS = ("conflict", "after_command", "earlier_after_later", "later_replaces")


class _Store:  # the deletion service's content collection: nothing is stored here
    def collect_garbage(self):
        return 0


def _by_content(conn, content, missing=None):
    found = conn.execute("SELECT id FROM memory_records WHERE content = ? ORDER BY rowid DESC LIMIT 1",
                         (content,)).fetchone()
    if found is None:
        if missing is not None:
            return missing
        raise LookupError(f"no record {content!r}")
    return found[0]


def run(scenario):
    """None when the scenario's oracle holds, else the first difference."""
    with tempfile.TemporaryDirectory() as folder, Database(Path(folder)) as db:
        names = {}  # a step's ids (projects, conversations, papers, turns, extractions) -> database ids

        def write(fn):
            result = db.write(fn)
            time.sleep(0.003)  # each step after the last, by the clock the records keep
            return result

        def project(name, level="normal"):
            if name not in names:
                names[name] = new_id()
                write(lambda conn: conn.execute("INSERT INTO projects (id, name, kind, sensitivity) VALUES (?, ?,"
                                                " 'research', ?)", (names[name], name, level)))
            return names[name]

        project("p1")
        for step in scenario["steps"]:
            op = step["op"]
            if op == "project":
                project(step["id"], step.get("level", "normal"))
            elif op == "paper":
                names[step["id"]] = new_id()
                write(lambda conn, s=step: conn.execute(
                    "INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, ?, 'upload')",
                    (names[s["id"]], project(s.get("project", "p1")), s["id"])))
            elif op == "turn":
                where = project(step.get("project", "p1"))
                conversation = step.get("conversation", "c1")
                if conversation not in names:
                    names[conversation] = new_id()
                    write(lambda conn: conn.execute("INSERT INTO conversations (id, project_id) VALUES (?, ?)",
                                                    (names[conversation], where)))
                names[step["id"]] = turn = new_id()
                papers = [names[p] for p in step.get("papers", ())]

                def admitted(conn, s=step, turn=turn, where=where, conversation=names[conversation], papers=papers):
                    conn.execute("INSERT INTO runs (id, project_id, conversation_id, kind, workflow, inputs, status)"
                                 " VALUES (?, ?, ?, 'turn', 'agent', ?, 'succeeded')",
                                 (turn, where, conversation, json.dumps({"context_materials": papers})))
                    (seq,) = conn.execute("SELECT count(*) FROM turns WHERE conversation_id = ?",
                                          (conversation,)).fetchone()
                    conn.execute("INSERT INTO turns (run_id, conversation_id, seq, author, user_message, answer)"
                                 " VALUES (?, ?, ?, ?, ?, ?)", (turn, conversation, seq, s.get("author", "researcher"),
                                                               json.dumps({"text": s["text"]}),
                                                               json.dumps({"text": s.get("answer", "")})))
                    asked = memory.command(s["text"])
                    if asked and asked["text"] is not None and asked["action"] != "forget":
                        _, ids = memory.save_command(conn, project_id=where, run_id=turn, action=asked["action"],
                                                     text=asked["text"])
                        conn.execute("INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 0, 'memory_proposed', ?)",
                                     (turn, json.dumps({"command": asked["action"], "applied": 1, "records": ids})))
                    return asked
                asked = write(admitted)
                if asked and asked["action"] == "forget" and asked["text"] is not None:
                    scored = db.read(lambda conn: memory.forget_candidates(conn, where, asked["text"]))
                    if memory.clear(scored):
                        deletion.delete(db, _Store(), "memory", scored[0][1]["id"])
                        write(lambda conn, turn=turn: conn.execute(
                            "INSERT INTO run_events (run_id, seq, type, data) VALUES (?, 0, 'memory_proposed', ?)",
                            (turn, json.dumps({"command": "forget", "applied": 1, "count": 1}))))
            elif op == "start":
                names[step["extraction"]] = new_id()
                turn = names[step["turn"]]
                write(lambda conn, s=step, turn=turn: conn.execute(
                    "INSERT INTO runs (id, project_id, kind, workflow, source_turn_id, inputs) SELECT ?, project_id,"
                    " 'background', 'memory', id, '{}' FROM runs WHERE id = ?", (names[s["extraction"]], turn)))
            elif op == "finish":
                def finish(conn, s=step):
                    # a record named by its content; one forgotten since, by an id no record has (as the extractor
                    # would still name it)
                    records = [{**r, "replaces": _by_content(conn, r["replaces"], new_id()) if r.get("replaces") else ""}
                               for r in s["records"]]
                    candidates = memory.parse(json.dumps({"records": records}))
                    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (names[s["extraction"]],)).fetchone() is None:
                        return None  # its turn was deleted: it writes nothing
                    return memory.apply_extraction(conn, names[s["extraction"]], candidates)
                write(finish)
            elif op == "keep":
                write(lambda conn, s=step: memory.keep(conn, _by_content(conn, s["content"]), None))
            elif op == "edit":
                def edit(conn, s=step):
                    record = _by_content(conn, s["content"])
                    version = conn.execute("SELECT version FROM memory_records WHERE id = ?", (record,)).fetchone()[0]
                    return memory.edit(conn, record, version, content=s["to"])
                write(edit)
            elif op == "forget":
                deletion.delete(db, _Store(), "memory", db.read(lambda conn, s=step: _by_content(conn, s["content"])))
            elif op == "delete_paper":
                deletion.delete(db, _Store(), "material", names[step["paper"]])
            elif op == "delete_conversation":
                deletion.delete(db, _Store(), "conversation", names[step["conversation"]])
            elif op == "expire":
                write(lambda conn, s=step: conn.execute("UPDATE memory_records SET valid_to = '2000-01-01T00:00:00.000Z'"
                                                        " WHERE id = ?", (_by_content(conn, s["content"]),)))
            elif op == "note":
                write(lambda conn, s=step: memory.note(conn, project_id=project(s.get("project", "p1")), run_id=None,
                                                       kind=s["kind"], text=s["text"], materials=()))
            elif op == "add_papers":
                ids = [new_id() for _ in range(step["count"])]

                def added(conn, ids=ids, s=step):
                    where = project(s.get("project", "p1"))
                    for paper in ids:
                        conn.execute("INSERT INTO materials (id, project_id, title, source) VALUES (?, ?, 'x', 'upload')",
                                     (paper, where))
                    memory.materials_added(conn, where, ids)
                write(added)
            else:
                raise ValueError(f"unknown step {op!r}")
        return _check(db, scenario["oracle"], project)


def _check(db, oracle, project):
    def read(conn):
        statuses = conn.execute("SELECT content, status, json_extract(structured, '$.older') FROM memory_records"
                                " WHERE type IN ('profile', 'preference', 'project_state', 'decision', 'event')").fetchall()
        return statuses
    statuses = db.read(read)
    found = {"active": sorted(c for c, s, _ in statuses if s in ("confirmed", "inferred")),
             "proposed": sorted(c for c, s, _ in statuses if s == "proposed"),
             "older": sorted(c for c, s, older in statuses if s == "proposed" and older)}
    for key in ("active", "proposed", "older"):
        if key in oracle and sorted(oracle[key]) != found[key]:
            return f"{key}: expected {sorted(oracle[key])}, found {found[key]}"
    for name, expected in (oracle.get("recall") or {}).items():
        recalled = db.read(lambda conn, name=name: [r["content"] for r in memory.ranked(
            memory.recall_candidates(conn, project(name)))] + [
            r["content"] for r in memory.in_scope(conn, project(name), kinds=("project_state",))])
        if recalled != expected:
            return f"recall {name}: expected {expected}, found {recalled}"
    if "left_off" in oracle:
        left = db.read(lambda conn: memory.left_off(conn, project("p1")))
        shown = {"next_step": (left["next_step"] or {}).get("content"),
                 "questions": [r["content"] for r in left["questions"]],
                 "done": [r["structured"].get("count") for r in left["done"]]}
        for key, expected in oracle["left_off"].items():
            if shown[key] != expected:
                return f"left_off {key}: expected {expected}, found {shown[key]}"
    return None


def load(path=FILE):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    scenarios = load(argv[0] if argv else FILE)
    counts, failed = {}, 0
    for scenario in scenarios:
        problem = run(scenario)
        key = (scenario["language"], scenario["category"])
        passed, total = counts.get(key, (0, 0))
        counts[key] = (passed + (problem is None), total + 1)
        if problem is not None:
            failed += 1
            print(f"FAIL {scenario['id']} ({scenario['title']}): {problem}")
    for (language, category), (passed, total) in sorted(counts.items()):
        print(f"{language} {category}: {passed} of {total}")
    print(f"{len(scenarios) - failed} of {len(scenarios)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
