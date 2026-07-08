# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Pure Pydantic types for the eval harness (#13).

Kept in their own module (rather than in corpus.py/metrics.py per the plan sketch)
so metrics/runner can import them without pulling corpus.py's libmozdata/DB deps."""
from __future__ import annotations

from pydantic import BaseModel, Field


class CorpusCase(BaseModel):
    uuid: str
    signature: str = ""
    regressor_node: str = ""          # hg node (git regressors are git2hg-resolved at freeze)
    regressor_bug: int | None = None
    regressor_author: str = ""
    channel: str = "nightly"
    crash_json_path: str | None = None
    seed_nodes: list[str] = Field(default_factory=list)  # the stack-only candidate set
    on_stack_label: bool | None = None  # filled by the labeler; None = unlabeled


class SweepConfig(BaseModel):
    """A tuning sweep: role->model overrides layered onto the base agent.llm block,
    plus optional confidence thresholds. Empty = use the configured defaults."""

    name: str = "default"
    roles: dict[str, str] = Field(default_factory=dict)   # role -> model short name
    principal_model: str | None = None
    confidence_thresholds: dict[str, float] = Field(default_factory=dict)


class Metrics(BaseModel):
    offstack_recall: float = 0.0
    stackonly_recall: float = 0.0
    evidence_precision: float = 0.0
    lead_precision: float = 0.0       # of leads, share that reference the true regressor
    abstain_calibration: dict = Field(default_factory=dict)  # {strong,lead,abstain}x{find,unfind}
    n_cases: int = 0
    n_offstack: int = 0
    n_strong: int = 0
    n_lead: int = 0
    # Cost/usage, aggregated across the re-run (per-case avg + total). Lets a sweep or a
    # prompt change weigh a quality delta against its cost delta (a drop to a cheaper
    # model, or a heavier prompt, both show up here).
    mean_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    mean_output_tokens: float = 0.0
    mean_input_tokens: float = 0.0
    corpus_hash: str = ""
    sweep_config: dict = Field(default_factory=dict)
