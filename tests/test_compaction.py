"""Compaction of completed turns (S1-21b; slice-1 spec section 8, Compaction; ticket 31's History and compaction;
checklist K1 to K6, L1, L3, L4, I5 to I7, G8; L8 is in test_deletion_deny_first.py): when a conversation's history reaches 90% of what it may take, the
oldest turns are summarized by one call through the dispatch path, recorded, labeled, reused and resumed from, and the
summary follows everything it summarizes. Model calls go to a test-owned mock; nothing leaves the process."""

import asyncio
import json
import logging
import os
import stat

import pytest

from backend import compaction, context, runs as runs_module
from agentic import call, steps_reply
from cited import Provider, cited, conversation_in, events_of, rows, with_papers
from scholia_app import background_idle, events, send, started
from test_materials import project_of
from test_search import WAGES

pytestmark = pytest.mark.asyncio
FILLER = "Cohort studies follow a group of people over time to see who develops an outcome, and when. "
HISTORY = 1600  # [context] history, in counted tokens: three of these turns pass 90% of it
SUMMARY = "SUMMARY-MARK: the researcher asked about cohort designs; the answers explained follow-up."


def is_compaction(body):
    return (body or {}).get("messages", [{}])[0].get("content", "").startswith(compaction.RULES)


class Long(Provider):
    """Answers each turn with a long synthetic answer naming its message's marker; compaction calls with `summary` (a
    text, a (status, body) pair, or a callable of the request, which may be a coroutine); keeps compaction requests."""

    def __init__(self, summary=SUMMARY, answer=None, **options):
        self.summary, self.summaries, self.answer_for = summary, [], answer
        super().__init__(answer_text=self._text, **options)

    def _text(self, body):
        if is_compaction(body):
            self.summaries.append(body)
            return self.summary(body) if callable(self.summary) else self.summary
        message = body["messages"][-1]["content"] if body["messages"][-1]["role"] == "user" else ""
        if self.answer_for is not None and (special := self.answer_for(message)) is not None:
            return special
        return f"ANSWER-TO[{message[:12]}] " + FILLER * 8

    @property
    def turns(self):
        return [b for b in self.answers if not is_compaction(b)]


async def put_settings(client, updates):
    current = (await client.get("/api/settings")).json()
    response = await client.put("/api/settings", json={"hash": current["hash"], "updates": updates})
    assert response.status_code == 200, response.text


async def small_history(client, history=HISTORY):
    await put_settings(client, {"context.history": history})


async def conversation(client):
    project = await project_of(client)
    return project, (await client.post("/api/conversations", json={"project_id": project, "title": "t"})).json()["id"]


def text_of(body):
    return json.dumps(body, ensure_ascii=False)


async def summary_rows(client):
    return await rows(client, "SELECT run_id, step, sha256 FROM run_bodies WHERE attempt = ?", compaction.SUMMARY_SLOT)


async def compactions(client, run_id):
    return [d for k, d, _ in await events_of(client, run_id) if k == "compaction"]


async def answer_steps(client, run_id):
    return [d for k, d, _ in await events_of(client, run_id) if k == "step_started" and d["phase"] == "answer"]


async def test_k1_older_turns_are_compacted_at_90_percent_and_the_newest_stay_verbatim(tmp_path):
    provider = Long()
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        for i in range(3):
            assert (await send(client, chat, f"QUESTION-{i} about cohorts"))[-1]["status"] == "succeeded"
        assert provider.summaries == []  # under 90%: nothing compacted
        stream = await send(client, chat, "QUESTION-3 about cohorts")
        assert stream[-1]["status"] == "succeeded"
        [asked] = provider.summaries  # one compaction call, of the oldest turns, never the current one
        sent = asked["messages"][-1]["content"]
        assert "QUESTION-0" in sent and "QUESTION-1" in sent and "QUESTION-3" not in sent
        assert "tools" not in asked and "tool_choice" not in asked  # a role call, never forcing a tool
        assert asked["model"] == provider.turns[-1]["model"]  # the turn's own route
        answer = provider.turns[-1]
        system = answer["messages"][0]["content"]
        assert f"## {compaction.LABEL}\n{SUMMARY}" in system  # labeled as a model's summary, never evidence
        assert "QUESTION-0" not in text_of(answer) and "QUESTION-1" not in text_of(answer)
        assert "QUESTION-2" in text_of(answer["messages"][1:])  # the newest earlier turn verbatim
        # Under 90% of what history may take, with the summary in place.
        [event] = await compactions(client, stream[0]["run_id"])
        assert event["outcome"] == "ok" and event["turns"] == [0, 1] and len(event["covers"]) == 2
        assert event["model"].endswith(asked["model"]) and "SUMMARY-MARK" not in json.dumps(event)  # ids, never text
        [step] = await answer_steps(client, stream[0]["run_id"])
        assert step["summary"] == {"turns": [0, 1]}


async def test_k2_the_summary_is_kept_owner_only_reused_and_extended_by_a_newer_one(tmp_path):
    provider = Long()
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        runs = [(await send(client, chat, f"QUESTION-{i} about cohorts"))[0]["run_id"] for i in range(4)]
        [(run, step, sha)] = await summary_rows(client)
        assert run == runs[3]
        path = client.state["db"].data_dir / "content" / sha[:2] / sha
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and path.read_text() == SUMMARY  # section 1.5, owner-only
        fifth = (await send(client, chat, "QUESTION-4 about cohorts"))[0]["run_id"]
        assert len(provider.summaries) == 1  # reused: no new compaction while history stays under 90%
        assert SUMMARY in provider.turns[-1]["messages"][0]["content"]
        assert (await answer_steps(client, fifth))[0]["summary"] == {"turns": [0, 1]}
        await send(client, chat, "QUESTION-5 about cohorts")
        assert len(provider.summaries) == 2  # a newer one covering more, from the earlier summary and the next turns
        extended = provider.summaries[-1]["messages"][-1]["content"]
        assert SUMMARY in extended and "QUESTION-2" in extended and "QUESTION-0" not in extended
        [event] = await compactions(client, (await rows(client, "SELECT run_id FROM turns WHERE seq = 5"))[0][0])
        assert event["turns"] == [0, 1, 2, 3]


def stopping(provider, flag):
    """An answer for QUESTION-3 that calls a tool while flag is on: with one agent step allowed, the turn stops at
    the limit, which offers Continue."""
    return lambda m: steps_reply(provider, [call("list_materials")]) if flag["on"] and "QUESTION-3" in m else None


async def limited(client):
    await put_settings(client, {"limits.agent_steps": 1})


async def test_k2_continue_resumes_from_the_summary_with_no_new_compaction(tmp_path):
    flag = {"on": False}
    provider = Long()
    provider.answer_for = stopping(provider, flag)
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        await limited(client)
        _, chat = await conversation(client)
        for i in range(3):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        flag["on"] = True
        stream = await send(client, chat, "QUESTION-3 about cohorts")
        assert stream[-1].get("cancel_reason") == "limit" and len(provider.summaries) == 1
        flag["on"] = False
        resumed = events(await client.post(f"/api/runs/{stream[0]['run_id']}/continue"))
        assert resumed[-1]["status"] == "succeeded"
        assert len(provider.summaries) == 1  # the summary is a resume point
        assert SUMMARY in provider.turns[-1]["messages"][0]["content"]


async def test_k3_a_failed_turn_is_never_in_a_summary(tmp_path):
    provider = Long(answer=lambda m: (500, {"error": {"message": "down"}}) if "FAILED-TURN" in m else None)
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        await send(client, chat, "QUESTION-0 about cohorts")
        assert (await send(client, chat, "FAILED-TURN about cohorts"))[-1]["status"] == "failed"
        for i in range(1, 4):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        [asked] = provider.summaries
        assert "QUESTION-0" in text_of(asked) and "FAILED-TURN" not in text_of(asked)  # section 1.5


async def test_k5_the_compaction_step_is_reserved_and_charged_to_the_conversation(tmp_path):
    provider = Long()
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        for i in range(4):
            stream = await send(client, chat, f"QUESTION-{i} about cohorts")
        run = stream[0]["run_id"]
        [started_step] = [d for k, d, _ in await events_of(client, run) if k == "step_started" and d["phase"] == "compact"]
        [(status, paying)] = await rows(client, "SELECT status, paying_conversation_id FROM budget_reservations WHERE id = ?",
                                        started_step["reservation_id"])
        assert (status, paying) == ("settled", chat)
        kinds = [k for k, d, _ in await events_of(client, run)]
        assert kinds.index("compaction") < kinds.index("citation_checks")  # before the answer's checks


async def test_k6_a_refused_compaction_leaves_older_turns_out_whole_and_is_not_asked_again(tmp_path):
    refuse = (400, {"error": {"message": "This request was refused by the provider's policy."}})
    flag = {"on": False}
    provider = Long(summary=refuse)
    provider.answer_for = stopping(provider, flag)
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        await limited(client)
        _, chat = await conversation(client)
        for i in range(3):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        flag["on"] = True
        stream = await send(client, chat, "QUESTION-3 about cohorts")
        assert len(provider.summaries) == 1
        answer = provider.turns[-1]
        assert compaction.LABEL not in text_of(answer)  # the turns are carried as they fit, the rest left out whole
        [event] = await compactions(client, stream[0]["run_id"])
        assert event["outcome"] != "ok" and len(event["covers"]) == 2  # traced
        [step] = await answer_steps(client, stream[0]["run_id"])
        assert "summary" not in step
        flag["on"] = False
        resumed = events(await client.post(f"/api/runs/{stream[0]['run_id']}/continue"))
        assert resumed[-1]["status"] == "succeeded"
        assert len(provider.summaries) == 1  # the same turns are not asked for again (charter rule 4)


async def test_i8_a_private_turns_compaction_carries_the_private_controls(tmp_path):
    from test_private_allowlist import PLAIN_MODEL, ZDR_MODEL, private_conversation
    provider = Long(catalog=[ZDR_MODEL, PLAIN_MODEL], zero_retention=[ZDR_MODEL])
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await private_conversation(client)
        for i in range(4):
            assert (await send(client, chat, f"QUESTION-{i} about cohorts", model=ZDR_MODEL))[-1]["status"] == "succeeded"
        [asked] = provider.summaries
        headers = provider.chat_headers[provider.chats.index(asked)]
        assert asked["provider"]["zdr"] is True and headers.get_list("x-openrouter-cache") == ["false"]
        assert asked["model"] == ZDR_MODEL


async def test_l1_a_stop_during_the_compaction_call_settles_it_once_and_keeps_no_summary(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(body):
        entered.set()
        await release.wait()
        return SUMMARY
    provider = Long(summary=held)
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        for i in range(3):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        sending = asyncio.create_task(send(client, chat, "QUESTION-3 about cohorts"))
        await asyncio.wait_for(entered.wait(), 10)
        run = (await rows(client, "SELECT run_id FROM turns WHERE seq = 3"))[0][0]
        assert (await client.post(f"/api/runs/{run}/cancel")).status_code == 200
        release.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled"
        statuses = await rows(client, "SELECT status FROM budget_reservations WHERE run_id = ?", run)
        assert statuses and all(s in ("settled", "released") for (s,) in statuses)
        assert await summary_rows(client) == [] and len(provider.turns) == 3  # no answer step after the Stop


async def test_l3_deleting_a_paper_a_covered_turn_drew_on_during_the_compaction_revokes_the_turn(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(body):
        entered.set()
        await release.wait()
        return SUMMARY
    provider = Long(summary=held)
    async with cited(tmp_path, provider) as client:
        await small_history(client)
        project, [material] = await with_papers(client, WAGES)
        chat = await conversation_in(client, project)
        await send(client, chat, "What do minimum wages do to earnings?")  # draws on the paper
        for i in range(1, 3):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        sending = asyncio.create_task(send(client, chat, "QUESTION-3 about cohorts"))
        await asyncio.wait_for(entered.wait(), 10)
        run = (await rows(client, "SELECT run_id FROM turns WHERE seq = 3"))[0][0]
        held_now = json.loads((await rows(client, "SELECT inputs FROM runs WHERE id = ?", run))[0][0])
        assert material in held_now["context_materials"]  # registered before the compaction left
        before = len(provider.turns)
        assert (await client.delete(f"/api/materials/{material}")).status_code == 200
        release.set()
        stream = await sending
        assert stream[-1]["status"] == "cancelled" and len(provider.turns) == before  # nothing more dispatched
        assert await summary_rows(client) == []  # its summary never kept


async def test_l4_a_tightening_after_the_compaction_request_is_built_sends_nothing(tmp_path, monkeypatch):
    provider = Long()
    async with cited(tmp_path, provider) as client:
        await small_history(client)
        project, _ = await with_papers(client, WAGES)
        chat = await conversation_in(client, project)
        for i in range(3):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        real = runs_module.Harness._role_call

        async def then_tighten(self, claim, ctx, model, function, *args, **kwargs):
            if function == "compact":
                response = await client.post(f"/api/projects/{project}/sensitivity", json={"level": "local_only"})
                assert response.status_code == 200
            return await real(self, claim, ctx, model, function, *args, **kwargs)
        monkeypatch.setattr(runs_module.Harness, "_role_call", then_tighten)
        await background_idle(client)
        before = len(provider.answers)
        stream = await send(client, chat, "QUESTION-3 about cohorts")
        assert provider.summaries == [] and len(provider.answers) == before  # nothing left the machine
        assert stream[-1]["status"] in ("cancelled", "failed")


async def test_forgetting_a_memory_record_a_covered_turn_held_deletes_the_summary(tmp_path):
    provider = Long()
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        project, chat = await conversation(client)
        await send(client, chat, "Remember: my method is semi-structured interviews")
        for i in range(1, 4):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        await background_idle(client)
        [(run, step, _)] = await summary_rows(client)
        [event] = await compactions(client, run)
        assert event["memory_ids"]  # a covered turn's request held the record
        [record] = event["memory_ids"][:1]
        assert (await client.delete(f"/api/memory/{record}")).status_code in (200, 204)
        assert await summary_rows(client) == []
        # Review 1 (sonnet P2-1): a record forgotten already is no dependency of the next summary, which is kept, not
        # paid for and thrown away at every later turn.
        stream = await send(client, chat, "QUESTION-4 about cohorts")
        [made] = await compactions(client, stream[0]["run_id"])
        assert made["outcome"] == "ok" and record not in made["memory_ids"] and len(await summary_rows(client)) == 1


async def test_a_compaction_carrying_an_earlier_summary_registers_its_memory_records_before_it_is_sent(tmp_path, monkeypatch):
    # Review 1 (fable P2-1): a Forget between reading the earlier summary and sending the compaction revokes it.
    provider = Long()
    seen = []
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        await send(client, chat, "Remember: my method is semi-structured interviews")
        for i in range(1, 4):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        [(run, _, _)] = await summary_rows(client)
        [first] = await compactions(client, run)
        real = runs_module.Harness._role_call

        async def noted(self, claim, ctx, model, function, *args, **kwargs):
            if function == "compact":
                seen.append(json.loads((await rows(client, "SELECT inputs FROM runs WHERE id = ?", claim.run_id))[0][0]))
            return await real(self, claim, ctx, model, function, *args, **kwargs)
        monkeypatch.setattr(runs_module.Harness, "_role_call", noted)
        for i in range(4, 6):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        assert seen and set(first["memory_ids"]) <= set(seen[0].get("summary_memory", []))


async def test_a_summary_whose_memory_record_is_gone_is_refused_at_dispatch(tmp_path):
    async with started(tmp_path / "data", Long()) as client:
        project, chat = await conversation(client)
        await send(client, chat, "QUESTION-0 about cohorts")
        [(run,)] = await rows(client, "SELECT run_id FROM turns")

        def mark(conn, ids):
            conn.execute("UPDATE runs SET status = 'running', inputs = json_set(inputs, '$.summary_memory', json(?))"
                         " WHERE id = ?", (json.dumps(ids), run))
            return compaction.sendable(conn, run)
        db = client.state["db"]
        assert await asyncio.to_thread(db.write, lambda conn: mark(conn, [])) is True
        assert await asyncio.to_thread(db.write, lambda conn: mark(conn, ["no-such-record"])) is False


async def test_i5_no_summary_text_reaches_the_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    provider = Long()
    async with started(tmp_path / "data", provider) as client:
        await small_history(client)
        _, chat = await conversation(client)
        for i in range(4):
            await send(client, chat, f"QUESTION-{i} about cohorts")
        assert provider.summaries
    assert "SUMMARY-MARK" not in caplog.text and "QUESTION-0" not in caplog.text


# The assembler with a summary, at the windows section 14 names (K1, G8)


def history(turns):
    return [(f"question {i} 队列研究", f"answer {i} {FILLER * 6}") for i in range(turns)][::-1]  # newest first


def build(window, **options):
    from backend.settings import _CONTEXT
    budgets = {name: default for name, (default, _) in _CONTEXT.items()}
    return context.assemble("answer", window=window, budgets=budgets, system_rules="You are Scholia.",
                            request="Compare these cohorts. 比较这些队列。", output=budgets["output_limit"], **options)


@pytest.mark.parametrize("window", [4096, 8192, 16384, 32768, 131072])
async def test_k1_g8_compaction_brings_history_under_90_percent_at_every_window(window):
    turns = history(200)
    room = build(window, history=turns).history_room
    costs = [compaction.turn_tokens(m, a) for m, a in turns]
    planned = compaction.plan(costs, room, 90)
    if room // compaction.SHARE < compaction.MIN_SLOT:  # no summary worth asking for: older turns left out whole
        assert planned is None and build(window, history=turns).cuts.get("history")
        return
    verbatim, share = planned
    summary = " ".join(["summary"] * (share // 8))  # a summary within its share as counted
    assert context.count(compaction.block(summary)) + 2 <= share
    request = build(window, history=turns[:verbatim], summary=summary)
    assert "summary" not in request.cuts and "history" not in request.cuts
    held = sum(costs[:verbatim]) + context.count(compaction.block(summary)) + 2
    assert held < 0.9 * room  # under 90% of what history may take
    assert request.input_tokens + request.max_tokens <= window
    system = request.messages[0]["content"]
    assert system.endswith(compaction.block(summary))  # last in the system message, before the verbatim turns
    chain = [{"role": "assistant", "content": "step"}]  # the turn's own chain is never compacted
    assert build(window, history=turns[:verbatim], summary=summary, chain=chain).messages[-1] == chain[0]


async def test_g8_a_summary_that_does_not_fit_is_left_out_with_the_turns_it_covers():
    turns = history(3)
    huge = "summary " * 20000
    request = build(4096, history=turns[:1], summary=huge)
    assert request.cuts["summary"] == 1 and "summary summary" not in request.messages[0]["content"]


async def test_plan_leaves_history_alone_under_90_percent_and_never_compacts_a_lone_summary():
    assert compaction.plan([100, 100], 1000, 90) is None
    assert compaction.plan([600, 600], 1000, 90) == (1, 250)  # the newest verbatim, beside a share of 250
    assert compaction.plan([700, 600], 1000, 90) == (1, 250)  # the newest stays verbatim, even past 90% with the share
    assert compaction.plan([11200, 2000, 2000], 16000, 90) == (1, 4000)
    assert compaction.plan([100], 1000, 90, summary=900) is None  # nothing older to compact
    assert compaction.plan([600, 600], 300, 90) is None  # no summary worth asking for fits
