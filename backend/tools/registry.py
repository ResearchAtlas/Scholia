# Carried over from AI Advisory Board, backend/tools/registry.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: a registry is an instance, not class
# state shared by every importer; each registration declares section 6.3's six facts (types.py); the
# steward's prompt rendering is replaced by the function definitions a model request carries.
"""The tools the agent may call: their definitions and implementations."""

from typing import Any, Callable

from .types import ToolDefinition


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._implementations: dict[str, Callable] = {}

    def register(self, name: str, description: str, args_schema: dict[str, Any], **declared):
        """Decorator registering an implementation; declared holds every ToolDefinition declaration."""
        def decorator(func: Callable):
            self._tools[name] = ToolDefinition(name=name, description=description, args_schema=args_schema, **declared)
            self._implementations[name] = func
            return func
        return decorator

    def get_implementation(self, name: str) -> Callable | None:
        return self._implementations.get(name)

    def get_definition(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def to_request_format(self, names=None) -> list[dict]:
        """The function definitions a model request carries, for the tools named (all by default)."""
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.args_schema}}
                for t in self._tools.values() if names is None or t.name in names]
