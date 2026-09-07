# 23 — ESR channel support

**Status: implemented 2026-09-07, not deployed.** Calixte: "add the ESR channel now almost on the
same model as for release". This is the record of what "almost" turned out to mean, and of the
live facts the design rests on. Read `20-release-channel-support.md` first; everything release
does, an ESR line does the same way, and only the differences are written up here.

## 1. What is live (read 2026-09-07)

| source | fact |
|---|---|
| Socorro, 7 days | `release_channel=esr`: 127,046 reports (release: 153,114, 10%-sampled). By line: **140.x 68k**, **115.x 38k**, **153.x 4.2k**, 128.x ~2k (no build since 2025). |
| Buildhub, 30 days | `target.channel=esr` builds from THREE repositories: `releases/mozilla-esr115` (115.40.0esr), `-esr140` (140.15.0esr), `-esr153` (153.2.0esr), all built 2026-08-26; the previous set on 08-11. |
| Buildhub regexp | `140\.[0-9]+(\.[0-9]+)?esr` returns exactly the 140 line (140.7.0esr..140.15.0esr); `153\.…` returns 153.0esr/153.1.0esr/153.2.0esr. **Release's pattern returns no esr build** (the suffix), so the family needs its own. |
| searchfox | `firefox-esr115`, `firefox-esr140`, `firefox-esr153` all 200; `searchfox-cli -R mozilla-esr*` accepted. |
| BMO fields | `cf_tracking_firefox_esr115` / `_esr128` / `_esr140` / `_esr153` exist. `cf_tracking_firefox140` is Firefox 140's RELEASE flag, retired. |
| init.configure | `set_define("MOZ_ESR", milestone.is_esr)` — an ESR build is release's build type plus `MOZ_ESR`. |
| libmozdata | `Mercurial.get_repo("esr140")` = `releases/mozilla-esr140`: a line label is already the hg repo selector. |

## 2. The one design decision: a line is a label, the family is the policy

One Socorro channel, three code lines. Everything in this codebase that is a **lineage** is keyed
by the channel label: `nodes`/`builds`/`lastdate`, `Build.get_last_versions` (the selection
window), `get_two_last` (the pushlog pair), `get_pushdate_before` (the candidate window's lower
bound), the per-channel proto-cluster dedup, the filing cap. A single `esr` label would have
needed a repo discriminator on every node, a same-major filter in each of those queries, and a
major threaded through ~15 hg-URL call sites — two notions of "channel" flowing through the same
code, which is the `get_search_channel` class of trap (7 of 13 sites were wrong for aurora).

So **each line is its own channel label** — `esr115`, `esr140`, `esr153` — exactly the way
`release` is one label for one repo, and the release machinery works per line unchanged.
Everything that is a **policy** is keyed by the **family** `esr` via `config.channel_family()`:
thresholds and spike knobs (`_channel_value`: label, then family, then default), the filing
overlay (`_autofile_overlay`: family entry with the line's layered on top), the calibration
table, the build-flag partition, the prose labels, and the Socorro query
(`utils.get_search_channel("esr140") == "esr"`).

Costs of this shape, accepted: a new ESR line (yearly) is one label in `config.channels`, one
`searchfox.Repo` member, a redeploy (the release phase adds the enum label), and its name in the
two env vars. Enum labels are forever (a Postgres enum value cannot be dropped), so
`config.channels` grows by one a year; `esr128` was not added because it has had no build since
2025.

## 3. What differs from release ("almost")

* **Buildhub** (`buildhub.target_channel`, `version_pat`): every line asks for `target.channel=esr`
  with its own major's regexp; `buildhub.get` keys its result by OUR label, not Buildhub's bucket
  (`Build.put_data` writes that key into the enum column).
* **Socorro**: a line asks about the whole family. Build-scoped queries are already scoped by the
  line's own build ids; channel-wide clocks ("how old is this signature on ESR") are meant to be
  family-wide — a crash that ran on esr140 for a year is not "new in esr" on esr153.
  `sigage.version_rates` is the one family-wide query that must be cut to the line
  (`summarize_version_rates(major=…)`), or 153.0esr's "preceding version" is 140.15.0esr.
  `_version_key` strips the `esr` suffix so dot releases keep their order.
* **Build type** (`compiled_out`): release's partition plus `MOZ_ESR` ON. Nightly's table is
  untouched (pinned byte-identical); the skeptic prompt says "This crash is on ESR 140".
* **Marks**: `[new in esr]` and `cf_tracking_firefox_esr<major>` (a different flag FAMILY on BMO,
  chosen from the channel in `report_bug._tracking_flag`), for culprit filings and spike
  appearances alike. Provenance line: "analyses ESR crashes".
* **Enum migration**: `_ensure_enum_values` had never once run its ALTER (it re-used a connection
  whose `SELECT` had autobegun a transaction; SQLAlchemy raised on the AUTOCOMMIT switch; the
  `except` logged it). Fixed with a separate AUTOCOMMIT connection and **proved on a real
  Postgres** (`tests/test_enum_migration_pg.py`, run against a `pgserver` instance: a DB created
  with the pre-ESR enum gains the three labels on `models.create()`, idempotently, and the label
  is usable). `_ENUM_ADDITIONS["CHANNEL_TYPE"]` is `config.get_channels()`, so the next line
  migrates itself.
* **`builds.version` was VARCHAR(10)**, found by the first run of the Postgres test: `140.15.0esr`
  is 11 characters, so the first esr140 / esr115 tick would have died at `Build.put_data`. Now
  VARCHAR(24), widened on a long-lived DB by `models._ensure_column_widths` on the release phase
  (`_WIDENED_COLUMNS`), idempotently; the sqlite suite cannot see this class of defect.
* **Knobs**: release's for the whole family (installs 50, protos 20, floor 50, rate path off,
  cap 2, `skip`), pinned by `tests/test_esr_channel.py`. Not measured on ESR — a copy, stated as
  such. The place a number would move first is `esr153`'s `installs`: the smallest line, ~4k
  reports a week, and a per-line entry beside the family's is how it would be tuned.
* **Not done**, as on release: no `sigage._POPULATION_RATES` arm (prompts drop the hardware
  comparison), `sigtrend` refuses the channel, no per-channel retention.

## 4. Filing: armed, like release, and inert until the env vars move

`channels.esr` is `enabled: true` because release's current model is armed; the switch that
actually decides whether an ESR bug can be filed is `AGENT_CHANNELS` naming a line, which is a
Heroku config var nobody has set. If a held week is wanted first, as beta and release had, it is
one line: `"esr": {"enabled": false, …}` — or per line, `"esr115": {"enabled": false}`.

## 5. Deploy order

Deploy (release phase adds the enum labels; check the log or `pg_enum`) → `INGEST_CHANNELS` gains
the lines → `AGENT_CHANNELS` gains the lines. Naming a line in `INGEST_CHANNELS` before the deploy
fails its ticks with `invalid input value for enum` until the deploy lands. `DEPLOY.md` has the
commands and the first-day watch list.
