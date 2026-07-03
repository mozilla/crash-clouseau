"""Trimmed ``hackbot_runtime``: the read-only crash-triage + needinfo surface only.

The upstream ``__init__`` eagerly imported ``runtime`` -> ``anthropic_wif`` ->
``google.auth`` (a ``NoReturn`` ``SystemExit`` entry point plus GCP WIF); those
modules are intentionally NOT vendored (Clouseau runs its own ``run_crash_triage``
coroutine, not ``run``/``run_async``). The three names below need no
``claude-agent-sdk``; import the SDK-backed helpers (``Reporter`` from ``.claude``,
``actions_server_for`` from ``.actions.claude_sdk``) directly where used.
"""
from crashclouseau.vendor.hackbot_runtime.actions.recorder import ActionsRecorder
from crashclouseau.vendor.hackbot_runtime.errors import AgentError
from crashclouseau.vendor.hackbot_runtime.results import HackbotAgentResult

__all__ = ["ActionsRecorder", "AgentError", "HackbotAgentResult"]
