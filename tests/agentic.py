"""Recorded tool-calling replies for agent-loop tests (S1-19c): a script of the model's steps, each a final
answer (text) or a list of tool calls, answered in order by the test-owned provider (cited.Provider), its
judge and router replies apart. Nothing leaves the process."""

import json

from cited import Provider, is_judge, is_router

REASONING = [{"type": "reasoning.encrypted", "data": "opaque-blob-Zx9", "id": "r1", "format": "anthropic-claude-v1"}]


def call(tool, call_id=None, **arguments):
    return {"id": call_id or f"call_{tool}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(arguments)}}


def steps_reply(provider, calls, content=None, reasoning_details=None, cost=None):
    message = {"role": "assistant", "content": content, "tool_calls": calls}
    if reasoning_details is not None:
        message["reasoning_details"] = reasoning_details
    return 200, {"choices": [{"message": message, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
                           "cost": provider.cost if cost is None else cost}}


class Scripted(Provider):
    """Answers the turn's steps from `script` in order: a str is a final answer; a list is tool calls (a dict
    with "calls" and optional "content" and "reasoning_details" for more); a callable gets the request body.
    Inner answer_from_library steps (their request offers no tools) take `inner` replies. `steps` keeps every
    agent step's request body; once the script is spent, steps answer `after`."""

    def __init__(self, *script, inner=(), after="Done.", **options):
        self.script, self.inner, self.after, self.steps, self.inners = list(script), list(inner), after, [], []
        super().__init__(answer_text=self._step, **options)

    def _step(self, body):
        if not body.get("tools"):
            self.inners.append(body)
            reply = self.inner.pop(0) if self.inner else self.after
            return reply(body) if callable(reply) else reply
        self.steps.append(body)
        reply = self.script.pop(0) if self.script else self.after
        if callable(reply):
            reply = reply(body)
        if isinstance(reply, list):
            reply = {"calls": reply}
        if isinstance(reply, dict):
            return steps_reply(self, reply["calls"], reply.get("content"), reply.get("reasoning_details"))
        return reply

    @property
    def agent_steps(self):
        return [b for b in self.chats if b.get("tools") and not is_judge(b) and not is_router(b)]


def tool_messages(body):
    return [m for m in body["messages"] if m.get("role") == "tool"]
