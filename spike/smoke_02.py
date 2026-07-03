# Throwaway live smoke for plan #02 (not committed; spike/ is scratch).
# Loads ANTHROPIC_API_KEY from ~/.mozdata.ini into the env (never printed) so the
# bundled Claude Code CLI the Agent SDK spawns can authenticate, then drives
# run_crash_triage against a real off-stack regression case from the spike corpus.
import configparser
import os

_c = configparser.ConfigParser()
_c.read(os.path.expanduser("~/.mozdata.ini"))
os.environ.setdefault("ANTHROPIC_API_KEY", _c["Anthropic"]["token"])
os.environ.setdefault("DATABASE_URL", "sqlite://")

import asyncio  # noqa: E402

from crashclouseau import inspector  # noqa: E402
from crashclouseau.agent.triage import run_crash_triage  # noqa: E402

UUID = "412ecdde-fcd3-4d62-b50c-cc78c0260623"
SIG = "mozilla::ContentCache::AssertIfInvalid"
CHANNEL = "nightly"


def render_stack(uuid):
    try:
        data = inspector.get_crash_data(uuid)
    except Exception as exc:  # network/Socorro
        return f"(could not fetch crash data: {exc})"
    if not data:
        return "(no processed crash available)"
    dump = data.get("json_dump", {})
    ct = dump.get("crash_info", {}).get("crashing_thread", 0) or 0
    threads = dump.get("threads", [])
    frames = threads[ct]["frames"] if ct < len(threads) else []
    out = []
    for fr in frames[:20]:
        fn = fr.get("function") or fr.get("module") or "?"
        loc = fr.get("file") or ""
        line = fr.get("line") or ""
        out.append(f"#{fr.get('frame', '?')} {fn}  {loc}:{line}".rstrip(":"))
    return "\n".join(out) or "(no frames)"


stack = render_stack(UUID)
print("=== stack (top frames) ===")
print(stack[:1500], flush=True)

llm_cfg = {
    "principal": {"model": "sonnet", "max_turns": 40},
    "roles": {
        "crash-interpreter": {"model": "haiku"},
        "call-graph-explorer": {"model": "sonnet"},
        "patch-scout": {"model": "haiku"},
        "data-flow-tracer": {"model": "sonnet"},
        "skeptic": {"model": "haiku"},
    },
}
crash = {"uuid": UUID, "signature": SIG, "channel": CHANNEL, "stack": stack}

print("\n=== running run_crash_triage (sonnet principal, max_turns=40) ===", flush=True)
from crashclouseau.vendor.hackbot_runtime.errors import AgentError  # noqa: E402

try:
    res = asyncio.run(run_crash_triage(crash=crash, llm_cfg=llm_cfg))
except AgentError as exc:
    print(f"\n=== AgentError (run did not finish cleanly): {exc} ===")
    print("(cost/turns for the run are on the Reporter '[done]' line above)")
    raise SystemExit(2)

print("\n=== RESULT ===")
print("num_turns:", res.num_turns)
print("total_cost_usd:", res.total_cost_usd)
print("decision:", res.decision)
print("confidence:", res.confidence)
print("dossier_present:", res.dossier is not None)
if res.dossier and res.dossier.verdict:
    print("abstain_reason:", res.dossier.verdict.abstain_reason)
    cp = res.dossier.call_path
    print("call_path_edges:", len(cp.edges) if cp else 0)
    print("hunks:", len(res.dossier.hunks))
print("result_text[:900]:")
print((res.result or "")[:900])
