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
