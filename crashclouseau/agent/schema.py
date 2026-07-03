# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Dossier schema & strong-evidence contract (plan #03).

Pure-data Pydantic models + the every-claim-has-a-citation validation rule, plus
the parse/validate helpers the #02 substrate calls on the principal's best-effort
trailing ```json handoff (mirrors bugbug's ``parse_plan``). There is no strict
JSON-schema exported to any API: structured output is validated *after* parse, and
``parse_and_validate`` ABSTAINS rather than let an uncited/hallucinated claim
survive. Only stdlib + pydantic + ``config`` are imported here (no db/SDK/network).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from crashclouseau import config


# --------------------------------------------------------------------------- #
# Enums (str-valued so they serialize cleanly to/from JSON)
# --------------------------------------------------------------------------- #
class FailureClass(str, Enum):
    uaf = "uaf"
    null_deref = "null_deref"
    assertion = "assertion"
    oob = "oob"
    shutdownhang = "shutdownhang"
    other = "other"


class Decision(str, Enum):
    strong_evidence = "strong-evidence"
    abstain = "abstain"


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class SkepticStatus(str, Enum):
    passed = "pass"
    failed = "fail"
    unverifiable = "unverifiable"


class CitationKind(str, Enum):
    searchfox = "searchfox"
    diff_line = "diff_line"
    stack_frame = "stack_frame"


# Categorical confidence -> numeric, for the abstain_below_confidence floor.
CONFIDENCE_SCORE: dict[Confidence, float] = {
    Confidence.low: 0.25,
    Confidence.medium: 0.5,
    Confidence.high: 0.85,
}


# --------------------------------------------------------------------------- #
# Citations (a discriminated union on ``kind``)
# --------------------------------------------------------------------------- #
class SearchfoxCitation(BaseModel):
    kind: Literal["searchfox"] = "searchfox"
    permalink: str
    symbol_id: str
    repo: str
    rev: str = ""


class DiffLineCitation(BaseModel):
    kind: Literal["diff_line"] = "diff_line"
    node: str
    filename: str
    line: int
    side: Literal["added", "deleted", "context"]
    content: str


class StackFrameCitation(BaseModel):
    kind: Literal["stack_frame"] = "stack_frame"
    uuid: str
    stackpos: int
    filename: str
    function: str
    line: int
    node: str


Citation = Annotated[
    Union[SearchfoxCitation, DiffLineCitation, StackFrameCitation],
    Field(discriminator="kind"),
]


class Cited(BaseModel):
    """Base for any claim-bearing model: it must carry at least
    ``config.get_min_citations_per_claim()`` citations or validation fails."""

    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_citations(self):
        minimum = config.get_min_citations_per_claim()
        if len(self.citations) < minimum:
            raise ValueError(
                f"{type(self).__name__} needs >= {minimum} citation(s), "
                f"got {len(self.citations)}"
            )
        return self


# --------------------------------------------------------------------------- #
# Content models
# --------------------------------------------------------------------------- #
class CrashFrame(BaseModel):
    stackpos: int
    function: str = ""
    filename: str = ""
    line: int = 0
    node: str = ""
    inlines: list[str] = Field(default_factory=list)
    trust: str | None = None


class CrashBrief(BaseModel):
    uuid: str
    signature: str = ""
    failure_class: FailureClass = FailureClass.other
    faulting_address: str | None = None
    moz_crash_reason: str | None = None
    reason: str | None = None
    crashing_thread: int = 0
    frames: list[CrashFrame] = Field(default_factory=list)
    phc_kind: str | None = None
    phc_alloc_stack: bool | None = None
    phc_free_stack: bool | None = None
    async_shutdown_timeout: str | None = None


class Candidate(BaseModel):
    node: str
    bug: int | None = None
    author: str = ""
    channel: str = ""
    pushdate: datetime | None = None
    backedout: bool = False
    seed_score: int | None = None
    changed_functions: list[str] = Field(default_factory=list)


class CallEdge(Cited):
    caller_symbol: str
    callee_symbol: str
    via: Literal["calls-from", "calls-to", "calls-between", "define"]


class CallPath(BaseModel):
    edges: list[CallEdge] = Field(default_factory=list)
    from_stackpos: int | None = None
    to_symbol: str = ""


class DiffHunk(Cited):
    node: str
    filename: str
    header: str = ""
    lines: list[DiffLineCitation] = Field(default_factory=list)


class DataFlowHypothesis(Cited):
    summary: str
    object_name: str = ""
    operation: Literal["free", "mutate", "null", "oob", "other"] = "other"
    crash_site: StackFrameCitation | None = None


class SkepticResult(BaseModel):
    claim_ref: str
    status: SkepticStatus
    note: str = ""
    citations: list[Citation] = Field(default_factory=list)


class Claim(Cited):
    """A cited free-text claim (the ``mechanism``/``consistency`` of a verdict)."""

    statement: str = ""


class Verdict(BaseModel):
    decision: Decision
    confidence: Confidence = Confidence.low
    needinfo_draft: str | None = None
    abstain_reason: str | None = None
    mechanism: Claim | None = None
    consistency: Claim | None = None

    @model_validator(mode="after")
    def _consistency_rule(self):
        if self.decision == Decision.strong_evidence:
            floor = config.get_abstain_below_confidence()
            if CONFIDENCE_SCORE[self.confidence] < floor:
                raise ValueError(
                    "strong-evidence requires confidence at/above the "
                    f"configured floor ({floor})"
                )
            if self.mechanism is None or not self.mechanism.citations:
                raise ValueError("strong-evidence requires a cited mechanism claim")
            if self.consistency is None or not self.consistency.citations:
                raise ValueError("strong-evidence requires a cited consistency claim")
        elif self.decision == Decision.abstain:
            if not self.abstain_reason:
                raise ValueError("abstain requires an abstain_reason")
            if self.needinfo_draft:
                raise ValueError("abstain must not carry a needinfo_draft")
        return self


class Dossier(BaseModel):
    schema_version: int = Field(default_factory=config.get_agent_schema_version)
    crash: CrashBrief | None = None
    candidate: Candidate | None = None
    call_path: CallPath | None = None
    hunks: list[DiffHunk] = Field(default_factory=list)
    data_flow: DataFlowHypothesis | None = None
    skeptic: list[SkepticResult] = Field(default_factory=list)
    verdict: Verdict | None = None
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Per-role fragment models validated from a role's parsed trailing-```json block.
_ROLE_FRAGMENTS: dict[str, type[BaseModel]] = {
    "crash-interpreter": CrashBrief,
    "call-graph-explorer": CallPath,
    "data-flow-tracer": DataFlowHypothesis,
    "skeptic": SkepticResult,
}
_DIFF_HUNK_LIST = TypeAdapter(list[DiffHunk])


# --------------------------------------------------------------------------- #
# Parse / validate helpers (the SDK-path anti-hallucination boundary)
# --------------------------------------------------------------------------- #
# Same shape as bugbug's frontend_triage._JSON_BLOCK / parse_plan: last block wins.
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_last_json_block(text: str | None):
    """Return the LAST ```json {...} ``` object in *text*, or None on no
    match / invalid JSON (never raises)."""
    if not text:
        return None
    matches = _JSON_BLOCK.findall(text)
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def validate_dossier(obj: dict) -> Dossier:
    """Strict gate: ``Dossier.model_validate`` — raises ``ValidationError`` on any
    uncited claim or malformed field."""
    return Dossier.model_validate(obj)


def parse_and_validate(result: str | dict) -> Dossier:
    """Validate the best-effort handoff #02 parses from ``ResultMessage.result``
    and ABSTAIN on failure. Never raises into the caller: on a missing/malformed
    ```json block, invalid JSON, or an uncited claim, returns a Dossier whose
    verdict is ``abstain`` with an ``abstain_reason`` naming the failure, so
    hallucinated content never survives into the persisted verdict."""
    reason: str | None = None
    obj: dict | None = None
    if isinstance(result, dict):
        obj = result
    else:
        obj = _extract_last_json_block(result)
        if obj is None:
            reason = "no parseable ```json block in the agent result"
    if obj is not None:
        try:
            return validate_dossier(obj)
        except ValidationError as exc:
            reason = f"dossier validation failed: {exc}"
    return Dossier(
        verdict=Verdict(
            decision=Decision.abstain,
            abstain_reason=reason or "empty agent result",
        )
    )


def validate_role_fragment(role: str, obj):
    """Validate one role's parsed sub-block against its fragment model
    (call-graph-explorer->CallPath, patch-scout->list[DiffHunk],
    data-flow-tracer->DataFlowHypothesis, skeptic->SkepticResult,
    crash-interpreter->CrashBrief). Raises ``ValidationError`` on an uncited
    claim; the caller (#02) decides whether to abstain."""
    if role == "patch-scout":
        return _DIFF_HUNK_LIST.validate_python(obj)
    model = _ROLE_FRAGMENTS.get(role)
    if model is None:
        raise ValueError(f"unknown role fragment: {role!r}")
    return model.model_validate(obj)


def dossier_to_db_json(d: Dossier) -> dict:
    """JSON-serializable dict for the persistence sub-plan's JSONB column
    (mirrors ``CrashTriageResult.model_dump()``)."""
    return d.model_dump(mode="json")


def dossier_from_db_json(d: dict) -> Dossier:
    version = d.get("schema_version")
    current = config.get_agent_schema_version()
    if version is not None and version > current:
        raise ValueError(
            f"dossier schema_version {version} is newer than supported {current}"
        )
    return Dossier.model_validate(d)
