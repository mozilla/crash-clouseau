"""Framework-neutral agent-tools registry (vendored subset).

Only ``registry.py`` + ``claude_sdk.py`` are vendored. This ``__init__`` imports
only the registry (pydantic-only, no agent framework), mirroring upstream: import
``crashclouseau.vendor.agent_tools.claude_sdk`` directly at the wiring site, since
it pulls ``claude-agent-sdk``.
"""
from crashclouseau.vendor.agent_tools.registry import (
    ToolDefinition,
    ToolError,
    tool,
    tools_in,
)

__all__ = ["ToolDefinition", "ToolError", "tool", "tools_in"]
