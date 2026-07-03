# 12 — Product wiring: UI evidence panel + recorded-action review/apply (needinfo)

> **MECHANISM (2026-07-02): the needinfo/bug path is the hackbot RECORD-then-APPLY split.**
> The evidence agent does **not** post to Bugzilla. Through the vendored `actions` MCP server
> (wired into `run_crash_triage` by #02), the principal/skeptic **records** a `bugzilla.add_comment`
> and/or a `bugzilla.update_bug` (needinfo = `update_bug` with `changes={'flags':[{'name':'needinfo',
> 'status':'?','requestee':...}]}`). Each recorded action is a plain dict `{type, params, reasoning}`
> (hackbot's `ActionsRecorder.record`; mirrors `summary.json['actions']`) — **nothing is mutated on
> Bugzilla by the agent.** hackbot ships **no dedicated needinfo action and no apply step**; Clouseau
> BUILDS both: (a) the read-only audit UI that renders each recorded action + its `reasoning`, and
> (b) an APPLY/REPLAY step that, on **explicit human confirm**, executes those recorded actions via
> `libmozdata` Bugzilla REST. Nothing is ever auto-filed.

## Objective
Surface the persisted evidence dossier and verdict for a scored crash in the existing Flask UI as a
read-only "evidence" panel on `crashstack.html`, with every claim rendered as a clickable citation
(searchfox permalink, exact diff line, exact stack frame). Alongside the dossier, render the agent's
**recorded Bugzilla actions** (`bugzilla.add_comment`, `bugzilla.update_bug`/needinfo) together with
each action's `reasoning` as a human-review **audit trail** — recorded, not posted. For
`confidence=high` STRONG-EVIDENCE verdicts, gate an **apply/replay** control behind an explicit human
click (the same "nothing happens without a human click" gate as today's "Create a new bug" link) that
executes the recorded actions via `libmozdata` Bugzilla REST: post the recorded comment, set the
recorded needinfo flag, attach. The existing `report_bug.py` `enter_bug` draft flow is preserved for
filing a genuinely new bug (human-fill), and a recorded `bugzilla.create_bug` prefills that draft
rather than being auto-created. Nothing is filed automatically; the agent never touches Bugzilla.

> NOTE on terminology (three distinct "who to ni" sources — do not conflate them):
> 1. `report_bug.improve()` returns the **existing bug's `assigned_to`** (the current assignee of the
>    regressed bug), and `bug.html` already displays that as the `ni` mailto target in the `enter_bug`
>    draft. It is NOT derived from the culprit patch.
> 2. The dossier's `culprit` author is the **patch author** — resolved from `Node.hgauthor`, an integer
>    FK into `hgauthors`, via `HGAuthor` (email/real/nick) — surfaced as a *second* suggested target.
> 3. The **recorded `bugzilla.update_bug` needinfo action** carries the agent's chosen `requestee` in
>    its `params.changes.flags[].requestee`. That requestee is what the apply/replay step would set on
>    confirm; it may or may not equal (1) or (2). The panel shows all three so a human can compare.

## Scope
**In scope**
- Reading the dossier/verdict **and the recorded actions** for a UUID and rendering them in the UI
  (`html.py`, `crashstack.html`, `static/clouseau.js`, `clouseau.css`).
- A read-only JSON endpoint (`/api/evidence`) backing the panel, now also carrying the recorded
  actions list (`[{type, params, reasoning}]`).
- The read-only **audit trail** UI: each recorded `bugzilla.*` action rendered with its target bug,
  the concrete change (comment body / needinfo requestee / attachment), and its `reasoning`.
- The **apply/replay** step Clouseau builds: a new internal module that, on explicit human confirm,
  executes the recorded actions via `libmozdata` Bugzilla REST (`add_comment` → POST comment;
  `update_bug`/needinfo → PUT bug flags; `add_attachment` → POST attachment) and records the outcome
  (applied-at + resulting id) so it is idempotent and auditable. Gated behind an explicit human click.
- Preserving and threading the existing `bug.html` / `report_bug.py` `enter_bug` **draft** flow for
  new-bug filing (human-fill): surface the suggested needinfo author(s) and the principal's evidence
  summary in the draft; a recorded `bugzilla.create_bug` **prefills** this draft (never REST-created).
- An `agent`/`llm` UI-relevant config read (display thresholds, "show abstain" flag, which recorded
  action `types` are apply-enabled, the confidence level at which the apply control is offered).

**Out of scope (owned by other sub-plans)**
- Defining the Pydantic dossier/verdict schema, the additive persistence table, and persisting the
  recorded-actions list (dossier-contract + persistence sub-plans, and #11). This unit only *reads*
  what they wrote and depends on read accessors existing on the model.
- Wiring the `actions` MCP server into the agent's `ClaudeAgentOptions` so the agent can *record*
  a comment/needinfo — that happens in `run_crash_triage` (#02) via `actions_server_for(recorder,
  types=[...])` + `actions_to_tool_names([...])`. This unit only *consumes* the recorded output and
  defines the enabled-`types` policy it reads from config.
- The RQ evidence-agent worker, the `run_crash_triage` coroutine, the Claude Agent SDK / hackbot
  substrate, the searchfox-cli adapter, and enqueue-from-`update.py` (agent sub-plans #02/#11).
- **Automatic** Bugzilla filing/needinfo (no human confirm) — explicitly *not* done. Every write in
  this unit is gated behind an explicit human click; the agent posts nothing.
- Eval harness / threshold calibration (eval sub-plan).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| flask | python-lib | `flask>=3.1.3` (in requirements.txt) | existing | `request`, `render_template`, `jsonify`, `abort`, `redirect` for the panel route, `/api/evidence`, and the POST apply route. |
| flask_sqlalchemy | python-lib | `flask_sqlalchemy>=3.1.1` | existing | `db` session to read the persisted dossier/verdict/recorded-actions via the model accessor, and to persist the apply outcome. |
| sqlalchemy | python-lib | `sqlalchemy>=2.0.51` | existing | ORM query for the verdict row (read) + a small write of the apply outcome (applied-at/result id). |
| jinja2 | python-lib | `jinja2>=3.1.6` | existing | `crashstack.html` panel + audit-trail block; `bug.txt`/`bug.html` render. |
| flask_cors | python-lib | `flask_cors>=6.0.5` | existing | `@cross_origin()` on the `/api/evidence` route registered in `__init__.py` (same pattern as `/api/reports`). |
| libmozdata | python-lib | `libmozdata>=0.2.12` | existing (**new write use**) | `hgmozilla.Mercurial.get_repo_url(channel)` for rev links (as today), **and** `libmozdata.bugzilla.Bugzilla` REST **writes** for the apply/replay step (`Bugzilla(...).put(...)` for the needinfo flag / bug update; comment + attachment POSTs). Reads the Bugzilla API token from `libmozdata.config` (see the token externality). |
| requests | python-lib | `requests>=2.34.2` | existing | Used by `report_bug.get_info_helper` (unchanged) **and** as the transport for any Bugzilla REST write not wrapped by a libmozdata helper (comment/attachment POST), with the token from libmozdata config. |
| hackbot_runtime.actions | vendored-lib (data shape) | `mozilla/bugbug` `libs/hackbot-runtime` — `ActionsRecorder.record(type, params, reasoning=)`, `actions.bugzilla` (`add_comment`/`update_bug`/`add_attachment`/`create_bug`), `actions_server_for`/`actions_to_tool_names` | NEW (consumed) | Defines the recorded-action **shape** this unit renders + replays: `{type, params, reasoning}` where `type ∈ {"bugzilla.add_comment","bugzilla.update_bug","bugzilla.add_attachment","bugzilla.create_bug"}`. `add_comment` auto-appends a footer; needinfo lives in `update_bug`'s `params.changes.flags`. hackbot RECORDS only — it has **no apply step** and **no dedicated needinfo action**. The MCP server is instantiated in #02; this unit consumes its persisted output. |
| crashclouseau.html | internal-module | `crashclouseau/html.py` | existing (modify) | Extend `crashstack()` to read the dossier + recorded actions and pass them to the template; extend `bug()` to thread an evidence summary + a recorded `create_bug` prefill. |
| crashclouseau.api | internal-module | `crashclouseau/api.py` | existing (modify) | Add `evidence()` JSON handler (mirror `api.reports()`); add an `apply_actions()` handler that executes the confirmed recorded actions and returns the per-action result. |
| crashclouseau.__init__ | internal-module | `crashclouseau/__init__.py` | existing (modify) | Register `@app.route("/api/evidence", methods=["GET"])` + `@cross_origin()` and `@app.route("/api/evidence/apply", methods=["POST"])` delegating to `api`. |
| crashclouseau.bugzilla_apply | internal-module | `crashclouseau/bugzilla_apply.py` (NEW) | NEW | The **apply/replay** step Clouseau builds: `apply_recorded_actions(uuid, indices) -> list[result]`. Executes recorded `bugzilla.add_comment`/`update_bug`(needinfo)/`add_attachment` via `libmozdata` Bugzilla REST **only when called from the human-confirmed route**; records applied-at + result id; skips already-applied. Routes a recorded `create_bug` back through the `report_bug` `enter_bug` draft (never REST-creates). |
| crashclouseau.report_bug | internal-module | `crashclouseau/report_bug.py`: `get_info(uuid, changeset)` (151), `get_info_helper` (105), `improve()` (42, returns the regressed bug's **`assigned_to`**), `finalize_comment()` (76) | existing (modify) | Reuse to build the `enter_bug` URL (preserved draft flow); extend to thread the principal's evidence summary into the drafted comment (`bug.txt`) and accept a recorded-`create_bug` prefill. `improve()`'s return is the existing-bug assignee, NOT the culprit author, NOT the recorded needinfo requestee. |
| crashclouseau.models | internal-module | `crashclouseau/models.py`: `CrashStack.get_by_uuid` (1264, returns `(stack, uuid_info)`; `({}, {})` when absent), `UUID.get_bid_chan_by_uuid` (938), `UUID.get_info` (791, used by `report_bug`), `Node` (127; `hgauthor` is an **int FK** to `hgauthors.id`), `Node.get_bugid` (169), `HGAuthor` (550; resolve FK→email/real/nick) | existing (modify: add `Verdict.get_by_uuid` read accessor + `Verdict.mark_action_applied` write helper only if the persistence sub-plan hasn't) | Read uuid_info, stack, verdict/dossier, **and the recorded-actions list**; resolve the culprit author FK for display; persist the apply outcome. |
| crashclouseau.utils | internal-module | `crashclouseau/utils.py`: `make_url_for_signature` (207), `get_colors` (33), `get_buildid` (129), `get_file_url(repo_url, filename, node, line, original)` (78), `get_major` (28) | existing | Existing crashstack rendering helpers, reused unchanged. |
| crashclouseau.config | internal-module | `crashclouseau/config.py` reading `./config/global.json` via `_get_global()` | existing (modify) | Add `get_agent_ui()` reading the `agent` block (display thresholds / show-abstain / apply-enabled action types / apply confidence gate). |
| Verdict/Dossier + recorded-actions model | data-source | new additive table (owned by persistence sub-plan / #11); read accessor e.g. `Verdict.get_by_uuid(uuid)` returning `{verdict, confidence, dossier, actions:[{type,params,reasoning}]}` | NEW (consumed) | Source of the dossier JSON, verdict fields, and the persisted recorded actions this unit renders/replays. This unit *reads* it; it does not define the schema. |
| config/global.json `agent`/`llm` block | config | NEW top-level key (current keys: `products`, `channels`, `facets_limit`, `backward_lookup_ndays`, `max_ndays`, `score`, `thresholds`). New keys: `agent.ui.show_abstain` (bool), `agent.ui.high_confidence_label` (str), `agent.confidence.needinfo_min` (e.g. `"high"`), `agent.apply.enabled_types` (e.g. `["bugzilla.add_comment","bugzilla.update_bug"]`) | NEW | UI/apply knobs: whether to show the panel for ABSTAIN, the confidence level at which the apply control is offered, and which recorded action types the replay is allowed to execute. (LLM model-tier keys live in the same block but are owned by the agent sub-plans.) |
| Bugzilla API token | config | in `~/.mozdata.ini` (libmozdata merges it) / dyno config — **never** the tracked repo `mozdata.ini` (see memory `clouseau-secrets-in-mozdata-ini`) | NEW | Credential the apply/replay step needs to write to Bugzilla via libmozdata. Absent ⇒ the apply route hard-fails with a clear error; the read-only panel/audit trail still work. |
| crashstack.html | config/template | `templates/crashstack.html` (injects `UUID`, `REPOURL`, `CHANNEL` JS consts at lines 15-17) | existing (modify) | Add the evidence panel + per-frame citation links + the recorded-actions audit trail + a conditional confirm-gated "Apply recorded actions" / "Draft bug" control. |
| bug.html | config/template | `templates/bug.html` (needinfo mailto at line 39) | existing (modify) | Preserved `enter_bug` draft: show evidence summary + suggested needinfo author(s); prefill from a recorded `create_bug` when present. |
| bug.txt | config/template | `templates/bug.txt` | existing (modify) | Inject the principal's evidence summary lines into the drafted bug comment. |
| clouseau.js | config/static | `static/clouseau.js` (`reportBug()` at 66, menu wiring at ~81/126, `getParams` at 13) | existing (modify) | Panel toggle, fetch `/api/evidence`, render citations + audit trail, `confirm()`-then-POST to `/api/evidence/apply` for the replay, and `confirm()`-then-navigate for the `enter_bug` draft. |
| clouseau.css | config/static | `static/clouseau.css` | existing (modify) | Panel + confidence-badge + audit-trail/action styling. |
| Bugzilla `enter_bug` | REST-API | `https://bugzilla.mozilla.org/enter_bug.cgi?<urlencoded query>` | existing | Draft URL target built by `finalize_comment`; opened only on human click. No needinfo param (displayed instead) — preserved for the new-bug path. |
| Bugzilla REST bug | REST-API | `https://bugzilla.mozilla.org/rest/bug` | existing (read) / NEW (write) | `get_info_helper` reads product/component/assigned_to (unchanged). The apply/replay step **writes** here on human confirm: `PUT /rest/bug/<id>` (needinfo flag / update), `POST /rest/bug/<id>/comment`, `POST /rest/bug/<id>/attachment`. |
| Socorro report page | REST-API | `https://crash-stats.mozilla.org/report/index/{uuid}` | existing | Linked from panel header. |
| Socorro SuperSearch | REST-API | `https://crash-stats.mozilla.org/api/SuperSearch/` | existing | Already called by `get_info_helper`. No new use. |
| searchfox-cli | CLI | `calls-from` / `calls-to` / `calls-between` / `define` (call-graph misses virtual/indirect/fn-ptr/template/macro + cross-language edges; indexes ~tip, not the crash build node) | NEW (NOT invoked here) | This unit only renders the citations (permalinks/symbol-ids) already stored in the dossier; it never shells out to searchfox-cli. |

## Interfaces
**Inputs consumed**
- `request.args.get("uuid", "")` on `/crashstack.html` and `GET /api/evidence`.
- Existing `CrashStack.get_by_uuid(uuid) -> (stack, uuid_info)`. `uuid_info` is the dict from
  `UUID.get_bid_chan_by_uuid` and already contains `uuid`, `id`, `signature`, `buildid`, `channel`,
  `product`, `java`, `node`. When the UUID is absent/not-analyzed it returns `({}, {})` and
  `crashstack()` currently `abort(404)`s; the panel must live inside the existing `if uuid_info:`
  branch. NOTE: `crashstack()` does NOT call `UUID.get_info` — that accessor is only used by
  `report_bug`.
- The persisted verdict/dossier **and recorded-actions list** for the UUID via the read accessor
  exposed by the persistence sub-plan. Dossier fields this unit **reads** (per PLAN §2/§3): `verdict`
  ∈ {STRONG_EVIDENCE, ABSTAIN}; `confidence` (e.g. high/medium/low); `crash_mechanism`; `culprit`
  `{node, bug, author}` (author resolved from `Node.hgauthor` → `HGAuthor` if the dossier stored the
  FK rather than the email); `changed_function` + searchfox permalink; `call_path` (edges, each with
  a searchfox citation); `mechanism` (diff hunk + exact line cite); `consistency` (stack-frame cite);
  `skeptic` notes (pass/fail per claim). **Recorded actions**: a list of
  `{type, params, reasoning}` dicts (hackbot `ActionsRecorder` shape) where, e.g., a needinfo is
  `{"type":"bugzilla.update_bug","params":{"bug_id":N,"changes":{"flags":[{"name":"needinfo",
  "status":"?","requestee":"user@moz"}]}},"reasoning":"..."}` and a comment is
  `{"type":"bugzilla.add_comment","params":{"bug_id":N,"text":"...<auto footer>","is_private":false},
  "reasoning":"..."}`. Each action also carries whatever apply-outcome markers the persistence layer
  stored (`applied_at`, `result_id`, or `None` when not yet applied).
- `POST /api/evidence/apply` body: `{uuid, indices:[int]}` — the human-confirmed subset of recorded
  actions to execute (indices into the recorded-actions list).
- `config.get_agent_ui()` for display thresholds / show-abstain flag / apply-enabled `types` / apply
  confidence gate.

**Outputs produced**
- Rendered evidence panel HTML on `crashstack.html`, including the read-only recorded-actions **audit
  trail** (each action's target, concrete change, and `reasoning`).
- `GET /api/evidence?uuid=...` JSON: the verdict + dossier (citation-bearing) + recorded actions,
  shaped for the JS panel. **This endpoint writes nothing.**
- `POST /api/evidence/apply` (human-confirmed): executes the selected recorded actions via libmozdata
  Bugzilla REST, marks them applied (persists `applied_at`/`result_id`), and returns the per-action
  result. This is the **only** place in the product that writes to Bugzilla, and only from a human
  click. Already-applied actions are skipped (idempotent).
- For the new-bug case: the preserved `bug.html` `enter_bug` draft (Bugzilla `enter_bug` URL +
  suggested needinfo author(s) + evidence summary in the comment), optionally prefilled from a
  recorded `bugzilla.create_bug`. The `enter_bug` path still **files nothing** — it opens a
  human-filled draft. The verdict's `culprit.node` is passed as the existing `changeset` arg into
  `report_bug.get_info`.

**Depends on / feeds**
- **Depends on:** dossier-contract sub-plan (field names), persistence sub-plan + #11 (table +
  `Verdict.get_by_uuid` accessor returning verdict/dossier/actions, and an apply-outcome write path),
  #02 (which wires the `actions` MCP server so the agent can *record* the comment/needinfo this unit
  renders/replays). Degrades gracefully (panel hidden) when no verdict row exists yet.
- **Feeds:** end-users (the dev/triager) — terminal consumer; the human review + confirm is the
  gate on every Bugzilla write. Feeds the eval sub-plan indirectly (the panel is how humans validate
  "strong evidence" verdicts before applying).

## Implementation steps
1. Confirm the read accessor with the persistence sub-plan (#11): agree on
   `Verdict.get_by_uuid(uuid) -> dict | None` returning `{verdict, confidence, dossier, actions}`,
   where `actions` is the persisted list of recorded `{type, params, reasoning[, applied_at,
   result_id]}` dicts (hackbot `ActionsRecorder` shape). If not yet present, add a minimal read-only
   accessor in `models.py` (query the additive table, return `None` when absent). Confirm whether the
   dossier stores the culprit author as an email/name or as the `hgauthors.id` FK; if the latter, the
   accessor joins `HGAuthor`. Agree a `Verdict.mark_action_applied(uuid, index, result_id)` write
   helper for the apply step to record the outcome.
2. Add `config.get_agent_ui()` to `crashclouseau/config.py` reading `_get_global().get("agent", {})`
   with safe defaults (`ui.show_abstain=False`, `ui.high_confidence_label="STRONG EVIDENCE"`,
   `confidence.needinfo_min="high"`, `apply.enabled_types=["bugzilla.add_comment",
   "bugzilla.update_bug"]`); add the corresponding block to `config/global.json` (a NEW top-level
   `agent` key). `apply.enabled_types` bounds what the replay route is permitted to execute.
3. In `html.py::crashstack()`, inside the existing `if uuid_info:` branch (after `sgn_url` is built),
   call the accessor and add `verdict=...` (or `None`), `actions=...` (list or `[]`), and
   `can_apply=...` to the existing `render_template("crashstack.html", ...)` call. Compute
   `can_apply = bool(verdict) and verdict["verdict"]=="STRONG_EVIDENCE" and _ge(verdict["confidence"],
   needinfo_min) and bool(actions)` using an explicit ordinal map for the string confidence compare.
4. Add `api.evidence()` in `crashclouseau/api.py` (mirror `api.reports()`: `request.args.get("uuid")`,
   `abort(400, ...)` on missing, `jsonify` the verdict/dossier/actions; read-only). Add
   `api.apply_actions()`: read the human-confirmed `{uuid, indices}` from the POST body, look up the
   recorded actions, delegate to `bugzilla_apply.apply_recorded_actions(uuid, indices)`, and return
   the per-action results. Register both routes in `crashclouseau/__init__.py`:
   `@app.route("/api/evidence", methods=["GET"])` + `@cross_origin()`, and
   `@app.route("/api/evidence/apply", methods=["POST"])`, delegating to lazily-imported `api`.
5. Write `crashclouseau/bugzilla_apply.py` — the apply/replay step Clouseau builds (hackbot has
   none). `apply_recorded_actions(uuid, indices) -> list[dict]`:
   - Re-read the persisted actions for `uuid` (never trust a client-supplied action body — the client
     only sends indices).
   - Reject any `type` not in `config.get_agent_ui()["apply"]["enabled_types"]`.
   - Skip actions already marked applied (idempotent; return their prior `result_id`).
   - Execute per `type` via libmozdata Bugzilla REST (token from libmozdata config; hard-fail with a
     clear message if absent): `bugzilla.add_comment` → POST `/rest/bug/<id>/comment`;
     `bugzilla.update_bug` → `Bugzilla(...).put({...changes...})` (this is how the **needinfo** flag
     gets set — from the recorded `changes.flags`); `bugzilla.add_attachment` → POST
     `/rest/bug/<id>/attachment` (fetch the artifact by its recorded `uploaded_key`).
   - Route a recorded `bugzilla.create_bug` **not** to REST but back through the `report_bug`
     `enter_bug` draft (return a draft URL for the human) — new bugs stay human-filed.
   - Persist the outcome via `Verdict.mark_action_applied(uuid, index, result_id)` and return
     `[{index, type, ok, result_id|error}]`.
6. Edit `templates/crashstack.html`: add an evidence panel block (guarded by `{% if verdict %}`) above
   the stack table showing crash mechanism, culprit (node→rev link, bug link, resolved author), call
   path as an ordered list of edges each linking its searchfox permalink, the mechanism diff-line cite
   (link via the existing `/diff.html` route: `filename`/`line`/`node`/`changeset`/`channel`),
   consistency stack-frame cite, skeptic pass/fail, and a confidence badge. Below it, a read-only
   **audit-trail** block (`{% for a in actions %}`) rendering each recorded action: its `type`, target
   `bug_id`, the concrete change (comment text, or needinfo `requestee` from
   `params.changes.flags`, or attachment name), its `reasoning`, and an "applied" marker when
   `applied_at` is set. When `verdict=="ABSTAIN"`, render a muted "insufficient evidence — no
   action recorded" note only if `show_abstain`. When `can_apply`, render an "Apply recorded actions"
   control (disabled/absent otherwise).
7. In `static/clouseau.js`: add `loadEvidence()` (fetch `/api/evidence?uuid=" + UUID`, render the
   dossier + audit trail into the panel container, reusing `REPOURL`/`CHANNEL`/`UUID` injected by the
   template at lines 15-17), a panel show/hide toggle, `applyActions(indices)` that requires an
   explicit `confirm()` (spelling out that it will post to Bugzilla) before `POST`ing
   `{uuid, indices}` to `/api/evidence/apply` and re-rendering with the results, and a
   `draftBug(node)` that requires an explicit `confirm()` before navigating to
   `bug.html?changeset=<node>&uuid=" + UUID` (same navigation shape as `reportBug()` at line 66) for
   the preserved `enter_bug` draft.
8. Extend `report_bug.py` (preserve the draft flow): add an optional `evidence_summary=None` arg
   threaded `get_info(uuid, changeset, evidence_summary=None) -> get_info_helper ->
   finalize_comment` and inject it into `bug.txt` (default-empty so the rendered comment is unchanged
   when omitted). `html.bug()` reads the verdict for `(uuid, changeset)`, passes the summary, and
   passes BOTH the existing `ni` (the regressed bug's assignee, from `improve()`) and the dossier's
   resolved culprit author to `bug.html` as suggested needinfo targets; if a recorded
   `bugzilla.create_bug` exists, use it to prefill the draft. Keep `improve()`'s return shape as-is.
9. Add panel + badge + audit-trail CSS to `static/clouseau.css` (confidence colors can reuse the
   `utils.get_colors()` convention).
10. Manual + automated verification (see Acceptance criteria). NOTE: the existing `tests/` suite
    (`test_buildhub.py`, `test_java.py`) has no Flask-app-context / DB fixture, so new tests for
    `api.evidence()`, `html.crashstack()`, and `bugzilla_apply` must add app-context + DB (or mock the
    model accessor + a fake Bugzilla REST) scaffolding — budget for that. The apply tests must mock
    the Bugzilla REST client so **no real write** happens.

## Risks & open questions
- **Schema coupling:** exact dossier field names/nesting and the persisted recorded-actions shape are
  owned upstream. Mitigation: render defensively (`{% if %}` guards, `.get()` in JS), and pin the
  accessor contract (including the `actions` list) in step 1 before building the template/apply.
- **The apply step is a real Bugzilla write — and Clouseau builds it from scratch.** hackbot RECORDS
  only; there is no upstream apply/replay to reuse (the recorder docstring only *mentions* "a
  downstream apply step"). Every write must be behind the explicit human-confirm POST, bounded by
  `apply.enabled_types`, idempotent (skip already-applied), and must never trust a client-supplied
  action body (client sends indices; server re-reads the persisted action). Requires a Bugzilla API
  token in `~/.mozdata.ini`/dyno config, never the tracked repo file.
- **Needinfo is now settable (on confirm), not just displayed.** Unlike the `enter_bug` draft (which
  cannot carry needinfo and only shows a mailto), the apply/replay path *sets* the needinfo flag via
  REST `PUT` from the recorded `update_bug` `changes.flags`. Confirm product owners accept a
  human-confirmed REST needinfo on the existing/regressed bug; the `enter_bug` new-bug path stays
  display-only (mailto) as today.
- **Three ni targets can disagree** (recorded `update_bug` `requestee`, `improve()` assignee, dossier
  culprit `HGAuthor`). The panel surfaces all three; the apply step sets only the recorded
  `requestee`. `Node.hgauthor` is an integer FK into `hgauthors` (`HGAuthor` has `email`/`real`/
  `nick`) — resolve to an email before display; do not assume a plain author string.
- **Confidence ordering:** `confidence` is a string ("high" > "medium" > "low"); needs an explicit
  ordinal map for the `>= needinfo_min` comparison. Open question: enum vs. numeric — confirm with the
  principal sub-plan.
- **Citation link shapes:** searchfox permalinks are stored by the searchfox-cli adapter; this unit
  assumes absolute URLs in the dossier. If only symbol-ids are stored, the panel must construct the
  URL — confirm what the adapter persists.
- **Revision drift (PLAN §7 / searchfox-cli limit):** a searchfox permalink indexes ~tip, not the
  crash build node. Label permalinks "@ indexed rev" so a human isn't misled.
- **Double-apply / partial failure:** a replay that posts a comment then fails on the needinfo PUT
  must leave the comment marked applied so a retry does not re-post it; return per-action results so
  the UI shows exactly what landed. Mark-applied is a DB write — mind the per-request session reset
  (`_remove_db_session`) and the known web-dyno session wedge after a DB wipe (restart the web dyno).
- **Stale/absent verdict after DB clean:** UUIDs older than `max_ndays=30` are hard-deleted
  (`Node.clean`/`UUID.clean`); `get_bid_chan_by_uuid` only returns rows where `analyzed=True` and
  `useless=False`. The accessor + template must tolerate a missing verdict row (panel hidden); the
  apply route must `abort(404)` cleanly when the UUID/verdict no longer exists.
- **Open:** does the panel show for *every* scored UUID or only those the agent processed? Default:
  show only when a verdict row exists; ABSTAIN shown only when `show_abstain`; the apply control shown
  only when `can_apply`.

## Acceptance criteria
- Visiting `/crashstack.html?uuid=<uuid-with-verdict>` renders the evidence panel (crash mechanism,
  culprit with clickable rev + bug + resolved author, call-path edges each with a working searchfox
  link, diff-line and stack-frame citations, skeptic results, confidence badge) **and** the read-only
  recorded-actions audit trail — each recorded `bugzilla.add_comment`/`update_bug` shown with its
  target bug, concrete change (comment body / needinfo requestee), and `reasoning`.
- A UUID with **no** verdict renders the page exactly as today (no panel, no errors); a UUID with no
  analyzed crashstack still `abort(404)`s as today; an **ABSTAIN** verdict shows the panel only when
  `agent.ui.show_abstain` is true and never shows an apply control.
- `GET /api/evidence?uuid=<uuid>` returns the verdict/dossier/actions JSON (200), `400` when `uuid` is
  missing, and **writes nothing** to Bugzilla or the DB.
- For a `confidence=high` STRONG_EVIDENCE verdict with recorded actions, the "Apply recorded actions"
  control appears, requires an explicit `confirm()`, and `POST`s to `/api/evidence/apply`; the server
  re-reads the persisted actions, executes only those in `apply.enabled_types` via libmozdata Bugzilla
  REST (comment posted / needinfo flag set from the recorded `changes.flags`), marks them applied, and
  returns per-action results. **No Bugzilla request fires without the human confirm** (verify network
  is silent until confirm), and a second apply of the same actions is a no-op (idempotent).
- The preserved `enter_bug` draft still works: the "Draft bug" control requires an explicit
  `confirm()` and lands on `bug.html` with the culprit changeset prefilled; the drafted `enter_bug`
  URL includes the evidence summary in the comment and the page displays the suggested needinfo
  author(s); a recorded `bugzilla.create_bug` prefills the draft; nothing is filed until the human
  submits the Bugzilla form.
- `report_bug.get_info(uuid, changeset, evidence_summary=...)` returns the existing
  `(url, ni, sgn, bugsdata)` 4-tuple unchanged in shape (backward compatible when `evidence_summary`
  omitted; rendered `bug.txt` byte-identical when omitted).
- `bugzilla_apply.apply_recorded_actions` refuses a type outside `apply.enabled_types`, skips
  already-applied actions, and never executes from anywhere but the human-confirmed POST route.
- `flake8` (config in `.flake8`) passes; existing `tests/` still green; new tests (with app-context/DB
  or model-mock + mocked Bugzilla REST scaffolding) assert `api.evidence()` 400-on-missing-uuid,
  `html.crashstack()` tolerates a `None` verdict, and `apply_recorded_actions` makes no real write
  (mocked client) while honoring the enabled-types bound and idempotency.
