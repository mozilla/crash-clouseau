# 22 — A real spike files a bug, culprit or not (Claude Fable 5.1 escalation)

Status: **implemented 2026-09-07, not deployed** (Calixte's rule, stated that day; every number
below is the shipped configuration). Code: `crashclouseau/spikes.py` (the predicate),
`crashclouseau/agent/spike_escalation.py` (sweep / run / file), `crashclouseau/agent/spike_agent.py`
(the investigator), `crashclouseau/agent/tools/crashstats.py` (its two new tools),
`crashclouseau/spike_report.py` (the bug text), `models.SpikeEscalation` (the table,
`_ADDED_TABLES`), `bin/schedule.py::spike_escalation_job`, `GET /api/spikes`. Tests:
`tests/test_spike_escalation.py`.

## The rule

Whatever the channel, a REAL spike of crashes is a fact by itself and must reach Bugzilla, with a
culprit when we have one and without when we do not. Ideally the bug tells the developers what
could be wrong: a culprit candidate, or a fact-based path to the crash, never a guess. A strong
model (Claude Fable 5.1, effort xhigh) is spent on that, and only on that: never on noise.

## What "real" means here — and why 0 → 1 is not it

`utils.is_spike` decides what the pipeline SPENDS on; 88% of its selections are the from-zero
branch and 67% are a single crash. `spikes.is_real_spike` decides what it TELLS PEOPLE, the way
the crash-spikes dashboard (`../crash-spikes/spikes/dashboard`) decides, minus the seasonal model:

| condition | value | where it comes from |
|---|---|---|
| crash floor | `spike.floor` 3 / 10 / 50 (nightly / beta / release) | the selector's own |
| distinct installations | `spike.real_installs` 3 / 6 / 50 | the channel install threshold, rounded up to "more than one machine" (an imaged fleet mints several install_times a minute) — the dashboard's "installs are first class" |
| ratio | `spike.ratio` 3× the loudest of the preceding build-days | the selector's own |
| Poisson excess | Anscombe `2(√(n+3/8) − √(e+3/8))` ≥ z of `spike.real_alert_rate` 0.015% → 3.6 | the dashboard scores every series with this residual; 0.015% is its `major` false-alarm rate, and it documents the Gaussian quantile as a floor real tails never go under |

All four AND-ed. From a zero baseline that is 6 crashes from 3 machines on nightly; over a
baseline of 1, 9; over 3, 13; above ~10 the ratio binds. A rate-path pick (`rising_rate`) has no
build-day count, so it is judged on its 7-day installs against the installs its own 56-day rate
predicts (`sigtrend.trend_facts`), same install floor, same z. A lambda's two demanglings are one
spike. Declined pairs are never candidates: nothing of theirs was ingested.

Untested by design, noted as the next gap: an `untestable_prefix` / first-build-of-a-cycle spike
(the beta QuotaManager case) has no ingested reports to investigate. It would need the sweep to
fetch uuids from Socorro itself.

## The loop

1. `sweep_real_spikes` (clock, every 10 min): `Selection.escalation_candidates` (ever-selected or
   rising-rate pairs, last `lookback_days` 5) → judge → for a real spike: not already escalated
   for that family within `once_per_days` 7; `grace_s` 3600 since first selected; no ordinary run
   on the picked build still pending/running; if an ordinary run FILED, record and stop; else pick
   the representative report (a stored stack, a finished run preferred), create the
   `spike_escalations` row, enqueue `run_spike_escalation` on the agent queue with its own
   `job_timeout` 3600. Bounded: `max_per_tick` 2, `max_runs_per_day` 4 per channel. A failed run
   is retried once; a `running` row older than timeout+300 s is failed.
2. `run_spike_escalation`: `build_spike_brief` — `build_seed` for the representative (or a minimal
   seed when it refuses), `triage._crash_facts`, up to `max_stacks` 3 distinct proto clusters
   with their report facts, the ordinary runs' verdicts / abstain reasons / candidates / second
   opinions / filer declines, the on-stack scored candidates AND the spiking build's pushlog
   window (`_offstack_window`, widened when rising). `spike_agent.run_spike_agent`:
   `claude-fable-5-1`, `effort` xhigh, `max_turns` 40, `fallback_model` opus, `max_budget_usd`
   40, **`tools=[]`** (built-in Bash/Read/Write/WebFetch/Agent not registered — the first agent
   here with the sandbox actually set; live-probed 2026-09-07: `--tools ""` keeps the MCP tools
   and drops Bash, while with `tools` unset the same prompt ran Bash under bypassPermissions;
   the second opinion got the same `tools=[]` that day, and the triage principal
   `tools=["Agent", "Task"]` after a second probe showed a so-restricted principal still spawns
   subagents that keep their MCP tools), scoped MCP tools: searchfox, pinned source + blame, patch
   diff, Bugzilla reads, `socorro.crash_stats`, and the two new `crashstats` tools (`facets` split
   at the spike build = the annotation diff; `report` = one report's annotations + any thread's
   stack). Handoff = `SpikeFindings` (summary, assessment, product/component + reason, culprit
   {node, bug, confidence, why}, trigger_path, evidence [{claim, source}], ruled_out,
   open_questions), parsed leniently.
3. `validate_findings`: a culprit must be one of the brief's candidates (prefix match, bug filled
   from the candidate) or a changeset hg resolves that landed before the build — else dropped;
   evidence without a source is dropped; `culprit_in_window` recorded. A run with zero tool calls
   is not `grounded`: its prose is not published, the volume is.
4. `file_spike_bug`: gates are the global `AUTOFILE_BUGS` and `daily_cap` 3 filings per channel
   per day — the per-channel culprit hold (`channels.beta.enabled: false`) deliberately does NOT
   apply. Venue: open same-application non-meta bug → COMMENT (the volume is news to its owner),
   choosing a bug filed for this spike (created from the day before the spike day) over the
   oldest; a bug filed for the spike that already names `regressed_by` gets nothing; `skip` /
   `file_new` modes exist (`comment_on_existing`). New bug: title `Crash in [@ sig]`, with the channel's
   `summary_prefix` (release: `[new in release]`) when the spike is an APPEARANCE — an all-zero
   baseline and no earlier report of the signature on that channel by its first-seen clock
   (`spike_report.is_new_signature`; a failed lookup cannot make an appearance look old) — and
   no prefix when an old signature got loud; product::component = the investigator's pair if
   Bugzilla has it → the signature's existing bugs' most common pair → `Core :: General`; keywords
   `crash` + `regression` iff a grounded culprit at medium+; `regressed_by` iff high AND in the
   window; needinfo = the culprit's author (`_needinfo_person`, hg `user` fallback); `blocks`
   clouseau; release nominates tracking. Memory-safety crashes: restricted group or not filed;
   a public venue is declined and a restricted new bug names it. A decline for a reason that may
   clear (venue lookup failed, cap) is retried from the sweep without re-running the model
   (`_retry_filings`). Nothing is stamped on `Dossier.payload['filed_bug']` — that key is the
   ordinary filer's idempotence and daily-cap key; the two filers meet on Bugzilla.

## Cost and switches

One escalation = one Fable 5.1 run, expect $5–40 (the budget cap). `SPIKE_ESCALATION_ENABLED=0`
stops the spend without a deploy; `AUTOFILE_BUGS=0` stops the writes; `AGENT_CHANNELS` scopes the
channels. Everything else is `agent.spike_escalation` in `config/global.json`.

## What to read after the first week

`select signature, channel, build_day, kind, status, payload->'spike'->>'z', payload->'filing'->>'bug', payload->'filing'->>'skipped', cost_usd from spike_escalations order by created desc;`
Questions, in order: did anything escalate that a human would not call a spike (the predicate is
the thing to adjust, per channel, never a threshold fit on one case)? Did the investigator ground
its claims (`payload->'grounded'`, `payload->'dropped'`)? Did the investigator use its tools
(`payload->'usage'->'tools_used'`; `tools=[]` is verified to keep them, so an empty map is a
prompt problem, not a registration one)? Were the components right
(`payload->'filing'->>'component_from'`)? Then the bugs themselves.
