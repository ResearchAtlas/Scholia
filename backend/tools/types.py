# Carried over from AI Advisory Board, backend/tools/types.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: only the call, result, error and
# definition contracts; the steward's evidence pack, key facts and usage limits stay behind (the agent
# loop records its own trace, backend/runs.py). A definition declares what section 6.3 asks of every
# tool: read-only, repeat-safe, network, output budget, class and model spend.
"""The agent's tool contracts (slice-1 spec section 6.3)."""

from typing import Any, Literal

from pydantic import BaseModel, Field

ToolClass = Literal["read_library", "pipeline", "agent_control"]
ErrorType = Literal["validation_error", "access_denied", "not_found", "tool_error", "budget_exceeded", "denied",
                    "over_window"]


class ToolCall(BaseModel):
    """A tool call the model asked for: its id (the provider's), name and arguments, as parsed."""
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    """A failed call's short, structured error, as the model sees it (section 6.6, compact errors)."""
    type: ErrorType
    message: str


class ToolResult(BaseModel):
    """A call's result: its text for the model, or an error. meta is for the trace (ids, counts), never text."""
    id: str
    ok: bool
    text: str = ""
    error: ToolError | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    """A registered tool and its declarations (section 6.3)."""
    name: str
    description: str
    args_schema: dict[str, Any]  # JSON Schema of its arguments
    read_only: bool
    repeat_safe: bool
    network: bool
    output_chars: int  # its output budget, before the preview bound
    tool_class: ToolClass
    spends: bool  # whether it makes model calls, whose estimate the budget check reserves before it runs
