"""The tool layer: defined once, used by the Claude agent loop and the MCP server."""

from resolve.tools import handlers as _handlers  # noqa: F401  (registers the tools)
from resolve.tools.base import (
    REGISTRY,
    ToolContext,
    ToolResult,
    ToolSpec,
    call_tool,
    outcome_label,
)

__all__ = [
    "REGISTRY",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "anthropic_tools",
    "call_tool",
    "outcome_label",
]


def anthropic_tools() -> list[dict[str, object]]:
    """Tool definitions in registry order: stable bytes keep the prompt cache warm."""
    return [spec.anthropic_definition() for spec in REGISTRY.values()]
