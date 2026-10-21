# Carried over from AI Advisory Board, backend/tools/parser.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: it reads the structured tool calls
# a provider returns (OpenAI's `tool_calls`) instead of a steward's JSON reply; arguments must be one
# JSON object, a Markdown fence around it allowed as before; nothing it reads is logged (section 1.5).
"""Reading the model's tool calls."""

import json
import re

from .types import ToolCall

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class ToolParser:
    @staticmethod
    def parse_call(raw) -> tuple[ToolCall | None, str | None]:
        """(the call, None), or (a call naming what could be read, the reason it cannot run): a call that
        is not a function call, has no id or name, or whose arguments are not one JSON object."""
        if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
            return None, "not_a_function_call"
        call_id, function = raw.get("id"), raw["function"]
        name = function.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            return None, "not_a_function_call"
        arguments = ToolParser.arguments(function.get("arguments"))
        if arguments is None:
            return ToolCall(id=call_id, name=name), "arguments_not_an_object"
        return ToolCall(id=call_id, name=name, arguments=arguments), None

    @staticmethod
    def arguments(text) -> dict | None:
        """The arguments as a dict, or None. An empty string is no arguments."""
        if isinstance(text, dict):
            return text
        if not isinstance(text, str):
            return None
        if not text.strip():
            return {}
        fenced = _FENCE.match(text)
        try:
            value = json.loads(fenced.group(1) if fenced else text)
        except (ValueError, RecursionError):  # not JSON, or nested past what the parser follows
            return None
        return value if isinstance(value, dict) else None
