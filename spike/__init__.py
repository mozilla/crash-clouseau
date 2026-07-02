"""Phase-0 call-graph spike (throwaway; see plans/00-phase0-callgraph-spike.md).

Standalone package that tests the one assumption the evidence-agent rebuild rides
on: can a cheap LLM driving ``searchfox-cli`` build a call-graph *neighborhood*
from a crash's stack frames that reaches the true regressor function **even when
that function is off-stack**?

It reports one headline metric: off-stack recall (call-graph neighborhood) vs.
stack-only recall (today's baseline). Nothing here is wired into the
``crashclouseau`` package, the DB, or the RQ workers -- it is deliberately
isolated and disposable.
"""
