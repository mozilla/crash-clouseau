# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Agent failures the orchestrator has to tell apart from a verdict.

Deliberately SDK-free (``AgentError`` is a bare ``Exception`` subclass) so
``orchestrator`` can import it at module level: the whole point of that module's
lazy ``run_crash_triage`` import is that the enqueue path / web dyno never pull
``claude-agent-sdk`` or spawn the bundled CLI.
"""
from __future__ import annotations

from crashclouseau.vendor.hackbot_runtime.errors import AgentError


class MissingHandoffError(AgentError):
    """The run ended with no readable ```json handoff.

    NOT a verdict. The principal system prompt requires the fenced block on every
    path -- ``abstain`` is a value inside that JSON, with a required
    ``abstain_reason`` -- so there is no sanctioned output where the model declines
    WITHOUT a fence. Its absence means the run never reached a conclusion:
    infrastructure (a backgrounded subagent fan-out, a truncated final message, a
    syntax error in the block the model did emit), which must surface as an `error`
    row rather than as a plausible-looking "insufficient evidence" abstain.

    Carries the raw final text and the usage counters so the failed row keeps the
    forensics and the SPEND: ``Dossier.set_status`` writes neither, and a run that
    got this far has already been paid for.
    """

    def __init__(
        self,
        message,
        *,
        raw_result="",
        cost_usd=None,
        num_turns=None,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
    ):
        super().__init__(message)
        self.raw_result = raw_result or ""
        self.cost_usd = cost_usd
        self.num_turns = num_turns
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
