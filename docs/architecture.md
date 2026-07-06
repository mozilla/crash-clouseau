<!-- This Source Code Form is subject to the terms of the Mozilla Public
     License, v. 2.0. If a copy of the MPL was not distributed with this file,
     You can obtain one at http://mozilla.org/MPL/2.0/. -->

# Architecture: crash report → view

This is the end-to-end flow of the LLM-agent-augmented crash-clouseau, from
ingesting a crash to what the operator sees in the UI. The dyno boundaries
(`clock`, `worker`, `agentworker`, `web`) match the `Procfile`.

```mermaid
flowchart TD
    subgraph EXT["External Mozilla services"]
        SOCORRO["Socorro / crash-stats<br/>crashes + processed dumps"]
        BUILDHUB["Buildhub<br/>builds ↔ revisions"]
        HG["hg.mozilla.org<br/>pushlog + raw diffs"]
        LANDO["Lando<br/>git ↔ hg map"]
        SFOX["searchfox-cli<br/>symbol / source lookup"]
        BZ["Bugzilla<br/>bug metadata"]
    end

    subgraph CLOCK["clock dyno · APScheduler"]
        SCHED["update_all(nightly)<br/>+ reap_orphans (15 min)"]
    end

    subgraph INGEST["worker dyno · RQ high/default/low"]
        UPDATE["update.put_report<br/>per new crash"]
        SCORE["inspector + patch<br/>score candidate changesets"]
        UUIDROW[("UUID row<br/>proto/stack hash · max_score")]
    end

    subgraph GATE["enqueue_agent — gating"]
        NIGHTLY{"channel in<br/>agent.channels?"}
        PROTO{"proto-signature<br/>already triaged?"}
        ENQ["enqueue RQ job<br/>agent queue · timeout"]
    end

    subgraph AGENT["agentworker dyno · RQ agent"]
        CLAIM{"claim_running<br/>atomic; skip if done/running"}
        SEED["build seed<br/>stack frames + candidate diffs"]
        TRIAGE["run_crash_triage · hackbot runtime"]
        subgraph TEAM["agent team · Claude Agent SDK"]
            PRINCIPAL["principal<br/>Sonnet 5 @ high"]
            EXPERTS["area-experts<br/>call-graph · data-flow"]
            SCOUT["patch-scout<br/>raw diffs + searchfox"]
            SKEPTIC["skeptic — veto"]
            NOISE["noise-filter"]
        end
        VALIDATE["parse_and_validate<br/>Pydantic schema + salvage"]
    end

    subgraph DB["Postgres"]
        DOSSIER[("Dossier<br/>status · cost · tokens · models")]
        VERDICT[("Verdict<br/>culprit / lead / abstain + confidence")]
    end

    subgraph WEB["web dyno · Flask"]
        REPORTS["reports.html<br/>build → signatures · scores · verdict badges"]
        STACK["crashstack.html<br/>stack · evidence · full-file diff / searchfox"]
        TASKS["tasks.html<br/>status · stalled · cost · duration · tokens"]
    end

    SCHED --> UPDATE
    SOCORRO --> UPDATE
    BUILDHUB --> UPDATE
    UPDATE --> SCORE
    HG --> SCORE
    LANDO --> SCORE
    SCORE --> UUIDROW --> NIGHTLY
    NIGHTLY -- no --> SKIP1["skip · non-nightly"]
    NIGHTLY -- yes --> PROTO
    PROTO -- yes --> SKIP2["skip · dedup"]
    PROTO -- no --> ENQ --> CLAIM
    CLAIM -- skip --> SKIP3["already done/running"]
    CLAIM -- claimed --> SEED --> TRIAGE --> PRINCIPAL
    PRINCIPAL <--> EXPERTS
    PRINCIPAL <--> SCOUT
    PRINCIPAL <--> SKEPTIC
    PRINCIPAL <--> NOISE
    SCOUT --> HG
    SCOUT --> SFOX
    TRIAGE --> VALIDATE --> DOSSIER
    VALIDATE --> VERDICT
    SCHED -. "reap: requeue stale 'running'" .-> ENQ

    UUIDROW --> REPORTS
    DOSSIER --> REPORTS
    VERDICT --> REPORTS
    UUIDROW --> STACK
    DOSSIER --> STACK
    VERDICT --> STACK
    BZ --> STACK
    DOSSIER --> TASKS
    VERDICT --> TASKS
```

## Walkthrough

1. **Ingest (clock → worker).** The `clock` dyno periodically runs
   `update.update_all` scoped to the configured channels (`$INGEST_CHANNELS`,
   default `nightly`). For each new crash `update.put_report` writes a `UUID`
   row and `inspector` + `patch` score the candidate changesets (raw hg diffs,
   `git → hg` mapping via Lando), recording `max_score`.

2. **Gate (`enqueue_agent`).** Before spending an LLM run the crash passes two
   gates: it must be on an **agent channel** (nightly), and its
   **proto-signature** must not already have a completed dossier (dedup). Only
   then is an RQ job queued on the `agent` queue with a job timeout.

3. **Triage (agentworker).** The worker **atomically claims** the run
   (`claim_running` — skips anything already done or running, so dyno restarts
   don't double-pay), builds a seed from the crash stack + candidate diffs, and
   runs the agent team via the hackbot runtime: a principal (Sonnet 5 @ high)
   coordinating area-experts (call-graph / data-flow), patch-scout (raw diffs +
   searchfox), a skeptic (veto) and a noise-filter. The result is validated
   against the Pydantic schema and persisted as a **Dossier** (status, cost,
   tokens, models) plus a **Verdict** (culprit / lead / abstain + confidence).

4. **Resilience.** A `reap_orphans` job (every 15 min, same threshold as the
   tasks view's "stalled") requeues `running` dossiers whose worker died past
   `job_timeout` + buffer — the dotted feedback edge.

5. **Views (web).** `reports.html` shows a build's signatures with candidate
   scores and verdict badges; `crashstack.html` shows the stack, the agent's
   evidence/citations and the full-file diff (or searchfox); `tasks.html` is the
   operational view of the runs themselves — status, stalled, cost, duration and
   tokens, with a fleet summary.
