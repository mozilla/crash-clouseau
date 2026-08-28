# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The typed crash-triage hand-off (#02).

``CrashTriageResult`` extends hackbot's ``HackbotAgentResult`` (``num_turns`` /
``total_cost_usd``) with the raw final message, the #03 ``Dossier`` parsed +
validated from the principal's trailing ```json block, and any recorded actions
(needinfo) captured on the ``ActionsRecorder``. ``.model_dump()`` is what #11
persists (mirrors hackbot's ``summary.json['findings']``)."""
from __future__ import annotations

from pydantic import Field

from crashclouseau.agent.schema import Confidence, Decision, Dossier
from crashclouseau.vendor.hackbot_runtime.results import HackbotAgentResult


class CrashTriageResult(HackbotAgentResult):
    result: str = ""
    dossier: Dossier | None = None
    actions: list[dict] = Field(default_factory=list)
    # Aggregate token usage summed across every model the run used (principal +
    # subagents), from the terminal ResultMessage's model_usage. Persisted by #11
    # for the tasks/monitoring view; cost is carried separately by total_cost_usd.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    # Which searchfox symbols this run actually asked about, and whether the answer was empty
    # (``triage._RunTrace.provenance``). Persisted by #11 into ``Dossier.payload['tool_calls']``
    # so "did the run enumerate the callers of the symbol its mechanism names?" is a query
    # rather than a two-hour log window. NOT model-authored and nothing gates on it; the point
    # is that a change to the call-graph tools or to the skeptic's enumeration duty can be told
    # to have stopped working. Additive: an older persisted result reads as ``{}``.
    tool_calls: dict = Field(default_factory=dict)

    @property
    def decision(self) -> Decision | None:
        if self.dossier and self.dossier.verdict:
            return self.dossier.verdict.decision
        return None

    @property
    def confidence(self) -> Confidence | None:
        if self.dossier and self.dossier.verdict:
            return self.dossier.verdict.confidence
        return None

    @property
    def actionable(self) -> bool:
        return self.decision == Decision.strong_evidence
