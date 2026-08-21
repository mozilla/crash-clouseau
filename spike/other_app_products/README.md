# `_OTHER_APP_PRODUCTS` — the panel behind config.py's evidence block (2026-08-21)

Worklist rank 13 of the overfitting audit: is `config._OTHER_APP_PRODUCTS` an ungated context
rule, and can it be derived instead of hand-maintained? Answer: **both proposed repairs are
killed**, the map stays hand-maintained, and what ships is `bin/audit_products.py` — the
completeness claim turned into a check that fails today on purpose.

`RESULTS.json` carries every number quoted in `crashclouseau/config.py`,
`crashclouseau/eval/study_corpus.py` and `bin/audit_products.py`.

## The two killed repairs

* **Derive the map from the live Socorro product facet** (add Fenix / Focus / ReferenceBrowser /
  MozillaVPN). Benefit **0 of 51 filings, 0 of 300 nightly signatures** — the nominal hits are
  all `EMPTY: no frame data available; *`, verified 0 threads / 0 frames /
  `inspector.thread_for_analysis() is None`. `prod_exposure.json` records 3 over the 298 lookups
  that answered; retrying the 2 that 502'd adds a fourth, also `EMPTY:` (bug 1245570). Cost: bug
  1855806 stops being a venue.
* **Key the map on "Gecko-app-ness" via BMO `classification`.** `MailNews Core` and `GeckoView`
  are `Components` while `Firefox` and `Focus` are `Client Software`, so it **eats CE1** — crash
  `05381864-aa6e-402f-a1fd-56a3e0260816` goes back to bug 2057980 `MailNews Core`, the one case
  the map exists for — and strips BMO `Firefox` too. `/rest/product` exposes nothing else that
  could carry application or family, so the map cannot be derived at all.

## Re-running

Every script must be run from the **repo root** (`config._get_global` opens the relative path
`./config/global.json`); each writes into this directory:

    DATABASE_URL=sqlite:///:memory: REDIS_URL=redis://localhost:6379 \
      uv run python spike/other_app_products/<script>

| script | what it does | output |
|---|---|---|
| `q_socorro.py`          | Socorro `_facets=product` + per-product channel, 7d       | stdout / `socorro_products.json` |
| `q_socorro_long.py`     | same over 30/90/180/365d (retention is ~180d)             | `socorro_products_long.json` |
| `q_bmo_products.py`     | every BMO product + `is_active` + classification          | `bmo_products.json` |
| `q_sig_census.py`       | bugs carrying a `cf_crash_signature`, per product (~7min) | `bmo_sig_census.json`, `census.log` |
| `q_crash_products.py`   | Socorro product/channel/signature for the 51 filings      | `crash_products.json` |
| `q_venue_diff.py`       | the SHIPPED `_open_bugs_for_signature` for all 51         | `venue_candidates.json` |
| `q_diff_maps.py`        | venue under shipped/A/B/C maps, diffed                    | `venue_diff.json` |
| `q_collision.py`        | other-app bug signatures x the DESKTOP crash population   | `collision.json`*, `open_sig_bugs.json`*, `collision_by_product.json` |
| `q_collision_fenix.py`  | same x the FENIX / FOCUS / THUNDERBIRD populations        | `collision_multi.json`*, `collision_multi_summary.json` |
| `q_counterexamples.py`  | the counter-example matrix, 5 maps                        | stdout |
| `q_family.py` / `q_family_noGV.py` | the family-keyed Fenix-day designs D and D2    | stdout |
| `q_prod_exposure.py`    | 300 nightly signatures through the shipped lookup (~9min) | `prod_exposure.json`, `nightly_sig_facet.json`* |
| `q_empty_crash.py`      | proves the `EMPTY:` hits have 0 threads and 0 frames      | stdout |
| `q_audit_proto.py`      | the prototype of `bin/audit_products.py`, run live        | stdout |
| `q_prose_gen.py`        | the generated agent description, before it was shipped    | stdout |
| `q_regbug_products.py`  | products of the 51 filings' regressor bugs                | `regbug_products.json` |

`*` regenerable but not committed (297K-573K each). `filings_enriched.json` and
`filing_meta.json` are the 51-filing panel input, resolved by an earlier session from BMO
(`creator=cdenizet@mozilla.com`, `creation_time>=2026-08-05`, `short_desc` contains `Crash in [@`)
and re-verified against Socorro.

## The live half

`bin/audit_products.py` re-runs the only two queries that have to stay true — every BMO product
the map names exists and is active, and every application reporting to Socorro that we do not
triage is a key in the map. It is not in the unit suite, the release phase, predeploy or the
scheduler; its docstring says why for each.
