# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Dossier schema & strong-evidence contract (plan #03).

Pure-data Pydantic models + the every-claim-has-a-citation validation rule, plus
the parse/validate helpers the #02 substrate calls on the principal's best-effort
trailing ```json handoff (mirrors bugbug's ``parse_plan``). There is no strict
JSON-schema exported to any API: structured output is validated *after* parse, and
``parse_and_validate`` never lets an uncited/hallucinated claim survive: on a
validation failure it SALVAGES — keeps the sub-objects (and verdict) that validate,
drops the ones that don't — so a single malformed optional field can't discard a
properly-cited verdict; a verdict that fails its own grounding rules is still forced
to abstain. Only stdlib + pydantic + ``config``/``logger`` are imported here (no
db/SDK/network).
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
from crashclouseau.logger import logger


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
    # The searchfox query the edge came from. Kept a free-form str, not a strict
    # Literal["calls-from","calls-to","calls-between","define"]: the model routinely
    # annotates it (e.g. "calls-from (virtual)") and a narrow Literal then discards
    # the WHOLE dossier via parse_and_validate -> a false abstain on an otherwise good
    # run. Descriptive only (citations are the grounding gate), so a str is safe.
    via: str = ""


class CallPath(BaseModel):
    edges: list[CallEdge] = Field(default_factory=list)
    from_stackpos: int | None = None
    to_symbol: str = ""


class DiffHunk(Cited):
    node: str
    filename: str
    header: str = ""
    # Supplementary changed-line detail. Kept an untyped list, NOT
    # list[DiffLineCitation]: the model often emits bare strings (e.g. the raw diff
    # text) here, and a strict item type made parse_and_validate discard the WHOLE
    # dossier -> a false abstain (same failure mode as `via`/`operation`). Grounding
    # is enforced by the Cited `citations` requirement; well-formed diff_line dicts
    # here are still picked up by the code view, bare strings are ignored.
    lines: list = Field(default_factory=list)


class DataFlowHypothesis(Cited):
    summary: str
    object_name: str = ""
    # Free-form mechanism label (e.g. free / mutate / null_deref / uaf / oob /
    # double_free / uninitialized / other). Kept a plain str, not a strict enum:
    # it is descriptive, not grounding-critical (citations are the anti-hallucination
    # gate), and a narrow Literal caused false-abstains on real model output.
    operation: str = "other"
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


def _abstain(reason: str) -> Dossier:
    return Dossier(verdict=Verdict(decision=Decision.abstain, abstain_reason=reason))


# Top-level Dossier sub-objects salvaged independently (singular fields).
_SINGLE_FIELDS: dict[str, type[BaseModel]] = {
    "crash": CrashBrief,
    "candidate": Candidate,
    "data_flow": DataFlowHypothesis,
    "verdict": Verdict,
}
# ...and the Dossier list fields, salvaged per item.
_LIST_FIELDS: dict[str, type[BaseModel]] = {
    "hunks": DiffHunk,
    "skeptic": SkepticResult,
}


def _salvage(obj: dict):
    """Best-effort per-field validation: keep the sub-objects (and per-item list
    entries / call-path edges) that validate, drop the ones that don't. Nothing
    uncited SURVIVES — it is dropped, not kept — so the anti-hallucination guarantee
    holds while a single malformed optional field no longer discards the whole
    (properly-cited) verdict. Returns ``(kwargs, dropped)``."""
    kwargs: dict = {}
    dropped: list = []
    if isinstance(obj.get("schema_version"), int):
        kwargs["schema_version"] = obj["schema_version"]

    for name, model in _SINGLE_FIELDS.items():
        val = obj.get(name)
        if val is None:
            continue
        try:
            kwargs[name] = model.model_validate(val)
        except ValidationError:
            dropped.append(name)

    cp = obj.get("call_path")
    if isinstance(cp, dict):
        edges = []
        for i, edge in enumerate(cp.get("edges") or []):
            try:
                edges.append(CallEdge.model_validate(edge))
            except ValidationError:
                dropped.append("call_path.edges[{}]".format(i))
        try:
            kwargs["call_path"] = CallPath(
                edges=edges,
                from_stackpos=cp.get("from_stackpos"),
                to_symbol=cp.get("to_symbol") or "",
            )
        except ValidationError:
            dropped.append("call_path")

    for name, model in _LIST_FIELDS.items():
        items = obj.get(name)
        if not isinstance(items, list):
            continue
        kept = []
        for i, item in enumerate(items):
            try:
                kept.append(model.model_validate(item))
            except ValidationError:
                dropped.append("{}[{}]".format(name, i))
        if kept:
            kwargs[name] = kept

    return kwargs, dropped


def parse_and_validate(result: str | dict) -> Dossier:
    """Validate the best-effort handoff #02 parses from ``ResultMessage.result``.
    Never raises into the caller. On a missing/malformed ```json block or invalid
    JSON, returns an abstain Dossier. On a schema-validation failure, SALVAGES: keep
    the sub-objects/verdict that validate, drop the ones that don't — so one bad
    optional field can't discard a properly-cited verdict. A verdict that is absent
    or fails its own grounding rules (uncited strong-evidence, confidence below the
    floor, ...) is forced to abstain, but any salvaged evidence is still attached."""
    obj = result if isinstance(result, dict) else _extract_last_json_block(result)
    if obj is None:
        return _abstain("no parseable ```json block in the agent result")
    try:
        return validate_dossier(obj)
    except ValidationError as exc:
        kwargs, dropped = _salvage(obj)
        if dropped:
            logger.warning("dossier salvage: dropped %s", ", ".join(dropped))
        if "verdict" not in kwargs:
            # verdict absent or failed its own grounding rules -> cannot be trusted;
            # abstain, but keep whatever evidence was salvageable for the panel.
            kwargs["verdict"] = Verdict(
                decision=Decision.abstain,
                abstain_reason="dossier validation failed (verdict unusable): {}".format(exc),
            )
        try:
            return Dossier(**kwargs)
        except ValidationError:
            return _abstain("dossier validation failed: {}".format(exc))


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
