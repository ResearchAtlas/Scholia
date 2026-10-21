# Carried over from AI Advisory Board, backend/tools/router.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: the control plane keeps its order
# (a call is checked, then run, and its result bounded) but applies the project's `[tools]` rules
# (deny, then ask, then allow) instead of an allowlist; validates arguments against the tool's schema;
# turns every failure into a short structured error for the model (section 6.6, compact errors); and
# keeps no cache, no rate table and no log of arguments or results (section 1.5). Call budgets are the
# harness's (backend/runs.py), which counts calls across the turn.
"""Checking and running one tool call."""

from .registry import ToolRegistry
from .types import ToolCall, ToolError, ToolResult

PREVIEW_CHARS = 8000  # section 13: a tool's output preview; paging past it comes with S1-21


class ToolRouter:
    def __init__(self, registry: ToolRegistry, *, deny=(), ask=(), allow=(), preview_chars=PREVIEW_CHARS):
        self.registry = registry
        self.deny, self.ask, self.allow = set(deny), set(ask), set(allow)
        self.preview_chars = preview_chars

    def rule(self, name) -> str:
        """The project's rule for a tool, in the order deny, then ask, then allow; a tool no list names is
        allowed (every tool in slice 1 is the app's own)."""
        if name in self.deny:
            return "deny"
        if name in self.ask:
            return "ask"
        return "allow"

    def check(self, call: ToolCall) -> ToolError | None:
        """Why a call cannot run as asked: an unknown tool, a denied one, or arguments its schema refuses."""
        definition = self.registry.get_definition(call.name)
        if definition is None:
            return ToolError(type="validation_error", message=f"No tool named {call.name[:60]!r}.")
        if self.rule(call.name) == "deny":
            return ToolError(type="denied", message="This project's rules do not allow this tool.")
        problem = _invalid(call.arguments, definition.args_schema)
        return ToolError(type="validation_error", message=problem) if problem else None

    async def execute(self, call: ToolCall, *context) -> ToolResult:
        """Run a call that passed check, with its context; its result's text bounded to the preview."""
        func = self.registry.get_implementation(call.name)
        result = await func(call, *context)
        if result.ok and len(result.text) > self.preview_chars:
            cut = result.text[:self.preview_chars]
            result = result.model_copy(update={"text": cut + f"\n[Cut at {self.preview_chars} characters; ask for less.]",
                                               "meta": {**result.meta, "cut": True}})
        return result


def failed(call_id, kind, message) -> ToolResult:
    return ToolResult(id=call_id, ok=False, error=ToolError(type=kind, message=message))


_TYPES = {"string": str, "integer": int, "array": list, "object": dict, "boolean": bool}


def _invalid(value, schema, where="arguments"):
    """The first way value breaks schema (the subset tools use: type, required, properties,
    additionalProperties false, items, minItems, maxItems, maxLength, minimum, maximum, enum), or None."""
    kind = schema.get("type")
    if kind and (not isinstance(value, _TYPES[kind]) or (kind == "integer" and isinstance(value, bool))):
        return f"{where} must be a {kind}."
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} must be one of {', '.join(map(str, schema['enum']))}."
    if kind == "string" and len(value) > schema.get("maxLength", len(value)):
        return f"{where} is too long."
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        return f"{where} is out of range."
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            return f"{where} has too many or too few items."
        for i, item in enumerate(value):
            if (problem := _invalid(item, schema.get("items", {}), f"{where}[{i}]")):
                return problem
    if kind == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                return f"{name} is required."
        for name, item in value.items():
            if name not in properties:
                if schema.get("additionalProperties") is False:
                    return f"{str(name)[:40]} is not an argument of this tool."
                continue
            if (problem := _invalid(item, properties[name], name)):
                return problem
    return None
