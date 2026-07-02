# 12 — Product wiring: UI evidence panel + needinfo draft

## Objective
Surface the persisted evidence dossier and verdict for a scored crash in the existing Flask UI as a read-only "evidence" panel on `crashstack.html`, with every claim rendered as a clickable citation (searchfox permalink, exact diff line, exact stack frame). For `confidence=high` STRONG-EVIDENCE verdicts only, prefill a Bugzilla bug/needinfo draft through the existing `report_bug.py` path and display a suggested needinfo author (Bugzilla `enter_bug` cannot auto-set needinfo), gated behind the existing explicit human "Create a new bug" link so nothing is filed automatically.

> NOTE on terminology: the existing `report_bug.improve()` returns the **existing bug's `assigned_to`** (the current assignee of the regressed bug), and `bug.html` already displays that as the `ni` mailto target. It is NOT derived from the culprit patch. This unit additionally surfaces the dossier's `culprit` author (the patch author) as a *second* suggested needinfo target — and that author lives in the `hgauthors` table as an integer FK on `Node.hgauthor`, so it must be resolved to an email via `HGAuthor` before display.

## Scope
**In scope**
- Reading the dossier/verdict for a UUID and rendering it in the UI (`html.py`, `crashstack.html`, `static/clouseau.js`, `clouseau.css`).
- A read-only JSON endpoint (`/api/evidence`) backing the panel.
- Wiring the verdict's suggested culprit changeset + author into the existing `bug.html` / `report_bug.py` draft flow, surfacing the suggested needinfo author(s) and the principal's evidence summary in the draft, behind the existing human-click "Create a new bug" link.
- An `agent`/`llm` UI-relevant config read (display thresholds, "show abstain" flag).

**Out of scope (owned by other sub-plans)**
- Defining the Pydantic dossier/verdict schema and the additive persistence table (the dossier-contract + persistence sub-plans). This unit only *reads* what they wrote and depends on a read accessor existing on the model.
- The RQ evidence-agent worker, `llm_call()` abstraction, `anthropic` client, searchfox-cli adapter, the seniors/principal, and enqueue-from-`update.py` (agent sub-plans).
- Bugzilla auto-filing/auto-needinfo via REST (explicitly *not* done — draft + human click only; this matches today's `report_bug` "display author, don't auto-set" behavior, where the displayed author is the regressed bug's assignee).
- Eval harness / threshold calibration (eval sub-plan).

## Externalities

| Name | Kind | Version / Endpoint / Command | Status | Purpose |
|------|------|------------------------------|--------|---------|
| flask | python-lib | `flask>=3.1.3` (in requirements.txt) | existing | `request`, `render_template`, `jsonify`, `abort`, `redirect` for the panel route + `/api/evidence`. |
| flask_sqlalchemy | python-lib | `flask_sqlalchemy>=3.1.1` | existing | `db` session to read the persisted dossier/verdict via the model accessor. |
| sqlalchemy | python-lib | `sqlalchemy>=2.0.51` | existing | ORM query for the verdict row (read-only here). |
| jinja2 | python-lib | `jinja2>=3.1.6` | existing | `crashstack.html` panel block + `bug.txt`/`bug.html` render. |
| flask_cors | python-lib | `flask_cors>=6.0.5` | existing | `@cross_origin()` on the `/api/evidence` route registered in `__init__.py` (same pattern as `/api/reports`). |
| libmozdata | python-lib | `libmozdata>=0.2.12` | existing | `hgmozilla.Mercurial.get_repo_url(channel)` for rev links; already used in `html.py` and `report_bug.py`. |
| requests | python-lib | `requests>=2.34.2` | existing | Already used by `report_bug.get_info_helper`; no new use here. |
| crashclouseau.html | internal-module | `crashclouseau/html.py` | existing (modify) | Extend `crashstack()` to read the dossier and pass it to the template; extend `bug()` to thread an evidence summary. |
| crashclouseau.api | internal-module | `crashclouseau/api.py` | existing (modify) | Add `evidence()` JSON handler (module functions are plain, route + `@cross_origin` live in `__init__.py`). |
| crashclouseau.__init__ | internal-module | `crashclouseau/__init__.py` | existing (modify) | Register `@app.route("/api/evidence", methods=["GET"])` + `@cross_origin()` delegating to `api.evidence()`. |
| crashclouseau.report_bug | internal-module | `crashclouseau/report_bug.py`: `get_info(uuid, changeset)` (151), `get_info_helper` (105), `improve()` (42, returns the regressed bug's **`assigned_to`**), `finalize_comment()` (76) | existing (modify) | Reuse to build the `enter_bug` URL; extend to thread the principal's evidence summary into the drafted comment (`bug.txt`). `improve()`'s return is the existing-bug assignee, NOT the culprit author. |
| crashclouseau.models | internal-module | `crashclouseau/models.py`: `CrashStack.get_by_uuid` (1264, returns `(stack, uuid_info)`; `({}, {})` when absent), `UUID.get_bid_chan_by_uuid` (938, source of uuid_info), `UUID.get_info` (791, used by `report_bug`, NOT by `crashstack()`), `Node` (127; `hgauthor` is an **int FK** to `hgauthors.id`), `Node.get_bugid` (169), `HGAuthor` (550; resolve FK→email/real/nick) | existing (modify: add `Verdict.get_by_uuid` read accessor only if the persistence sub-plan hasn't) | Read uuid_info, stack, and the persisted verdict/dossier; resolve the culprit author FK for display. |
| crashclouseau.utils | internal-module | `crashclouseau/utils.py`: `make_url_for_signature` (207), `get_colors` (33), `get_buildid` (129), `get_file_url(repo_url, filename, node, line, original)` (78), `get_major` (28) | existing | Existing crashstack rendering helpers, reused unchanged. |
| crashclouseau.config | internal-module | `crashclouseau/config.py` reading `./config/global.json` via `_get_global()` | existing (modify) | Add `get_agent_ui()` reading the `agent` block (display thresholds / show-abstain). |
| Verdict/Dossier model + table | data-source | new additive table (owned by persistence sub-plan); read accessor e.g. `Verdict.get_by_uuid(uuid)` | NEW (consumed) | Source of the dossier JSON + verdict fields the panel renders. This unit *reads* it; it does not define the schema. |
| config/global.json `agent`/`llm` block | config | NEW top-level key (current keys: `products`, `channels`, `facets_limit`, `backward_lookup_ndays`, `max_ndays`, `score`, `thresholds`). New keys: `agent.ui.show_abstain` (bool), `agent.ui.high_confidence_label` (str), `agent.confidence.needinfo_min` (e.g. `"high"`) | NEW | UI-facing knobs: whether to show the panel for ABSTAIN, the confidence level at which the bug-draft CTA is enabled. (LLM model-tier keys live in the same block but are owned by the agent sub-plans.) |
| crashstack.html | config/template | `templates/crashstack.html` (injects `UUID`, `REPOURL`, `CHANNEL` JS consts at lines 15-17) | existing (modify) | Add the evidence panel + per-frame citation links + conditional "draft bug" CTA. |
| bug.html | config/template | `templates/bug.html` (needinfo mailto at line 39) | existing (modify) | Show evidence summary + suggested needinfo author(s). |
| bug.txt | config/template | `templates/bug.txt` | existing (modify) | Inject the principal's evidence summary lines into the drafted bug comment. |
| clouseau.js | config/static | `static/clouseau.js` (`reportBug()` at 66, menu wiring at ~81/126, `getParams` at 13) | existing (modify) | Panel toggle, fetch `/api/evidence`, render citations, confirm-before-draft on the CTA. |
| clouseau.css | config/static | `static/clouseau.css` | existing (modify) | Panel + confidence-badge styling. |
| Bugzilla `enter_bug` | REST-API | `https://bugzilla.mozilla.org/enter_bug.cgi?<urlencoded query>` | existing | Draft URL target built by `finalize_comment`; opened only on human click. No needinfo param (displayed instead). |
| Bugzilla REST bug | REST-API | `https://bugzilla.mozilla.org/rest/bug` (read, for product/component/assigned_to) | existing | Already called by `get_info_helper`; supplies the assignee surfaced as `ni`. No new use. |
| Socorro report page | REST-API | `https://crash-stats.mozilla.org/report/index/{uuid}` | existing | Linked from panel header (already used in `report_bug` and crashstack page). |
| Socorro SuperSearch | REST-API | `https://crash-stats.mozilla.org/api/SuperSearch/` | existing | Already called by `get_info_helper` for crash stats. No new use. |
| anthropic | python-lib | `anthropic` SDK | NEW (NOT used here) | Listed for completeness; this unit renders a *persisted* verdict and makes no live LLM calls. Owned by agent/principal sub-plans. Model ids it will use (for cross-reference): `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-4-8`, `claude-fable-5`. |
| searchfox-cli | CLI | `calls-from` / `calls-to` / `calls-between` / `define` (call-graph misses virtual/indirect/fn-ptr/template/macro + cross-language edges; indexes ~tip, not the crash build node) | NEW (NOT invoked here) | This unit only renders the citations (permalinks/symbol-ids) already stored in the dossier; it never shells out to searchfox-cli. |

## Interfaces
**Inputs consumed**
- `request.args.get("uuid", "")` on `/crashstack.html` and `/api/evidence`.
- Existing `CrashStack.get_by_uuid(uuid) -> (stack, uuid_info)`. `uuid_info` is the dict from `UUID.get_bid_chan_by_uuid` and already contains `uuid`, `id`, `signature`, `buildid`, `channel`, `product`, `java`, `node`. When the UUID is absent/not-analyzed it returns `({}, {})` and `crashstack()` currently `abort(404)`s; the panel must live inside the existing `if uuid_info:` branch. NOTE: `crashstack()` does NOT call `UUID.get_info` — that accessor is only used by `report_bug`.
- The persisted verdict/dossier for the UUID via the read accessor exposed by the persistence sub-plan. Dossier fields this unit **reads** (per PLAN §2/§3): `verdict` ∈ {STRONG_EVIDENCE, ABSTAIN}; `confidence` (e.g. high/medium/low); `crash_mechanism`; `culprit` `{node, bug, author}` (author resolved from `Node.hgauthor` → `HGAuthor` email/real/nick if the dossier stored the FK rather than the email); `changed_function` + searchfox permalink; `call_path` (list of edges, each with a searchfox citation); `mechanism` (diff hunk + exact line cite); `consistency` (stack-frame cite); `skeptic` notes (pass/fail per claim); optional `needinfo_draft`.
- `config.get_agent_ui()` for display thresholds / show-abstain flag.

**Outputs produced**
- Rendered evidence panel HTML on `crashstack.html`.
- `/api/evidence?uuid=...` JSON: the verdict + dossier (citation-bearing), shaped for the JS panel.
- For high-confidence verdicts: the existing `bug.html` draft (Bugzilla `enter_bug` URL + suggested needinfo author + evidence summary in the comment). This unit **writes nothing** to Bugzilla or to the dossier table — it is read/draft-only. The verdict's `culprit.node` is passed as the existing `changeset` arg into `report_bug.get_info`.

**Depends on / feeds**
- **Depends on:** dossier-contract sub-plan (field names), persistence sub-plan (table + `Verdict.get_by_uuid` accessor), principal sub-plan (verdict population). Degrades gracefully (panel hidden) when no verdict row exists yet.
- **Feeds:** end-users (the dev/triager) — terminal consumer. Feeds the eval sub-plan only indirectly (the panel is how humans validate "strong evidence" verdicts).

## Implementation steps
1. Confirm the read accessor with the persistence sub-plan: agree on `Verdict.get_by_uuid(uuid) -> dict | None` returning the verdict + parsed dossier JSON. If not yet present, add a minimal read-only accessor in `models.py` (query the additive table, return `None` when absent) — no schema/table definition here. Confirm whether the dossier stores the culprit author as an email/name or as the `hgauthors.id` FK; if the latter, the accessor joins `HGAuthor` to return a displayable email.
2. Add `config.get_agent_ui()` to `crashclouseau/config.py` reading `_get_global().get("agent", {}).get("ui", {})` with safe defaults (`show_abstain=False`, `high_confidence_label="STRONG EVIDENCE"`); add the `agent.ui` + `agent.confidence.needinfo_min` block to `config/global.json` (a NEW top-level `agent` key).
3. In `html.py::crashstack()`, inside the existing `if uuid_info:` branch (after `sgn_url` is built), call the accessor and add `verdict=...` (or `None`) plus `can_draft=...` to the existing `render_template("crashstack.html", ...)` call. Compute `can_draft = bool(verdict) and verdict["verdict"]=="STRONG_EVIDENCE" and _ge(verdict["confidence"], needinfo_min)` using an explicit ordinal map for the string confidence comparison.
4. Add `api.evidence()` in `crashclouseau/api.py` (mirror `api.reports()`: `request.args.get("uuid")`, `abort(400, ...)` on missing, `jsonify` the verdict/dossier; `abort` and `jsonify` already imported). Register the route in `crashclouseau/__init__.py` as `@app.route("/api/evidence", methods=["GET"])` + `@cross_origin()` delegating to a lazily-imported `api.evidence()` (same shape as `api_reports`).
5. Edit `templates/crashstack.html`: add an evidence panel block (guarded by `{% if verdict %}`) above the stack table showing crash mechanism, culprit (node→rev link, bug link, resolved author), call path as an ordered list of edges each linking its searchfox permalink, the mechanism diff-line cite (link via the existing `/diff.html` route, which takes `filename`/`line`/`node`/`changeset`/`channel`), consistency stack-frame cite, skeptic pass/fail, and a confidence badge. When `verdict=="ABSTAIN"`, render a muted "insufficient evidence — no needinfo" note only if `show_abstain`. When `can_draft`, render a "Draft bug & needinfo" button carrying `culprit.node`.
6. In `static/clouseau.js`: add `loadEvidence()` (fetch `/api/evidence?uuid=" + UUID`, render into the panel container, reusing `REPOURL`/`CHANNEL`/`UUID` already injected by the crashstack template at lines 15-17), a panel show/hide toggle, and a `draftBug(node)` that requires an explicit `confirm()` before navigating to `bug.html?changeset=<node>&uuid=" + UUID` (same navigation shape as `reportBug()` at line 66).
7. Extend `report_bug.py`: add an optional `evidence_summary=None` arg threaded `get_info(uuid, changeset, evidence_summary=None) -> get_info_helper -> finalize_comment` and inject it into `bug.txt` (default-empty so the rendered comment is unchanged when omitted). `html.bug()` reads the verdict for `(uuid, changeset)`, passes the summary, and passes BOTH the existing `ni` (the regressed bug's assignee, from `improve()`) and the dossier's resolved culprit author to `bug.html` as suggested needinfo targets. Keep `improve()`'s return shape as-is.
8. Add panel + badge CSS to `static/clouseau.css` (confidence colors can reuse the `utils.get_colors()` convention used for scores).
9. Manual + automated verification (see Acceptance criteria). NOTE: the existing `tests/` suite (`test_buildhub.py`, `test_java.py`) has no Flask-app-context / DB fixture, so a new test for `api.evidence()`/`html.crashstack()` must add app-context + DB (or mock the model accessor) scaffolding — budget for that.

## Risks & open questions
- **Schema coupling:** exact dossier field names/nesting are owned upstream. Mitigation: render defensively (`{% if %}` guards, `.get()` in JS), and pin the accessor contract in step 1 before building the template.
- **Culprit author is an FK, not a string:** `Node.hgauthor` is an integer FK into `hgauthors` (`HGAuthor` has `email`/`real`/`nick`). The dossier/accessor must resolve it to an email before the panel/needinfo can display it; do not assume a plain author string. The existing `bug.html` `needinfo` value is a *different* author (the regressed bug's `assigned_to` returned by `improve()`), so the panel may surface two distinct suggested ni targets.
- **Confidence ordering:** `confidence` is a string ("high" > "medium" > "low"); needs an explicit ordinal map for the `>= needinfo_min` comparison. Open question: enum vs. numeric — confirm with the principal sub-plan.
- **Citation link shapes:** searchfox permalinks are stored by the searchfox-cli adapter; this unit assumes they are absolute URLs in the dossier. If only symbol-ids are stored, the panel must construct the searchfox URL — confirm what the adapter persists.
- **Revision drift (PLAN §7 / searchfox-cli limit):** a searchfox permalink indexes ~tip, not the crash build node. The panel should label permalinks as "@ indexed rev" so a human isn't misled; the citation is still verifiable, just not at the exact build node.
- **No auto-needinfo:** Bugzilla `enter_bug` can't set needinfo; we only display the author(s). Confirm product owners accept "human copies the ni? target" rather than any REST auto-set (consistent with current `bug.html` behavior; explicitly out of scope).
- **Stale/absent verdict after DB clean:** UUIDs older than `max_ndays=30` are hard-deleted (`Node.clean`/`UUID.clean`); also `get_bid_chan_by_uuid` only returns rows where `analyzed=True` and `useless=False`. The accessor + template must tolerate a missing verdict row (panel simply hidden). Also mind the known web-dyno session wedge after a DB wipe (restart web dyno) and that `__init__.py` now resets the DB session per request (`_remove_db_session`).
- **Open:** does the panel show for *every* scored UUID or only those the agent processed? Default: show only when a verdict row exists; ABSTAIN shown only when `show_abstain`.

## Acceptance criteria
- Visiting `/crashstack.html?uuid=<uuid-with-verdict>` renders the evidence panel with the crash mechanism, culprit (clickable rev + bug + resolved author), call path edges each with a working searchfox link, diff-line and stack-frame citations, skeptic results, and a confidence badge.
- A UUID with **no** verdict renders the page exactly as today (no panel, no errors), and a UUID with no analyzed crashstack still `abort(404)`s as today; an **ABSTAIN** verdict shows the panel only when `agent.ui.show_abstain` is true and never shows a draft CTA.
- `GET /api/evidence?uuid=<uuid>` returns the verdict/dossier JSON (200) and `400` when `uuid` is missing.
- For a `confidence=high` STRONG_EVIDENCE verdict, the "Draft bug & needinfo" CTA appears, requires an explicit `confirm()`, and lands on `bug.html` with the culprit changeset prefilled; the drafted Bugzilla `enter_bug` URL includes the evidence summary in the comment and the page displays the suggested needinfo author(s). No Bugzilla request is fired without the human clicking through (verify network is silent until click).
- `report_bug.get_info(uuid, changeset, evidence_summary=...)` returns the existing `(url, ni, sgn, bugsdata)` 4-tuple unchanged in shape (backward compatible when `evidence_summary` omitted; rendered `bug.txt` byte-identical when omitted).
- `flake8` (config in `.flake8`) passes; existing `tests/` still green; a new test (with the app-context/DB or model-mock scaffolding it requires) asserts `api.evidence()` 400-on-missing-uuid and that `html.crashstack()` tolerates a `None` verdict.
