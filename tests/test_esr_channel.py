# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""The ESR channel: one Socorro channel, several code lines, each line its own channel label.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_esr_channel

Socorro files every ESR build under ONE `release_channel`, `esr`, while Mozilla ships several ESR
lines at once -- 115, 140 and 153 on 2026-09-07, at 21.9k / 68k / 4k reports a week -- each from
its own repository (`releases/mozilla-esr<major>`), its own searchfox tree, its own Buildhub
version pattern, its own BMO tracking flag and its own build lineage. Everything in this codebase
that is a LINEAGE (`nodes`, `builds`, `lastdate`, the selection and candidate windows, the
proto-cluster dedup) is keyed by the channel label, so a line is its own label (`esr153`),
exactly the way `release` is one label for one repo. Everything that is a POLICY (thresholds, the
filing overlay, the calibration table, the build-flag partition, the prose, the Socorro query) is
keyed by the FAMILY `esr` (`config.channel_family`), and a line may override its family.

ONLY THE CURRENT LINE IS A CHANNEL. All three ran for two hours on 2026-09-07; esr115 (Windows 7,
32-bit, security-only) spent its first tick on 17 runs of one `OOM | large` signature, and
Calixte's decision was "remove esr115 and esr140, just keep the last one".

The Postgres half -- the enum migration that makes the new labels storable, and the per-line
`builds` lineage through the production writers -- is tests/test_enum_migration_pg.py.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import re  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from libmozdata.hgmozilla import Mercurial  # noqa: E402

from crashclouseau import (  # noqa: E402
    buildhub, compiled_out as co, config, models, report_bug, searchfox, sigage, spike_report,
    utils,
)
from crashclouseau.agent import roles, triage  # noqa: E402

LINES = ("esr153",)                       # the current ESR line; the family code takes any


class TestTheFamily(unittest.TestCase):
    def test_a_line_label_belongs_to_the_esr_family(self):
        for line, major in (("esr115", 115), ("esr140", 140), ("esr153", 153), ("ESR140", 140)):
            self.assertEqual(config.channel_family(line), "esr")
            self.assertEqual(config.esr_major(line), major)
        self.assertEqual(config.channel_family("esr"), "esr")
        self.assertIsNone(config.esr_major("esr"))
        for other in ("nightly", "beta", "release", "aurora", "", None, "esr140x", "Beta "):
            self.assertNotEqual(config.channel_family(other), "esr", other)
            self.assertIsNone(config.esr_major(other), other)
        # Lowercased, NOT stripped: exactly as strict as the label lookups it feeds, so
        # `"Beta "` stays undeclared (tests/test_beta_autofile.py pins that).
        self.assertEqual(config.channel_family("Beta "), "beta ")

    def test_a_knob_is_read_for_the_label_then_the_family_then_the_default(self):
        table = {"nightly": 1, "esr": 50, "esr153": 7}
        self.assertEqual(config._channel_value(table, "nightly", 0), 1)
        self.assertEqual(config._channel_value(table, "esr140", 0), 50)   # the family's
        self.assertEqual(config._channel_value(table, "esr153", 0), 7)    # its own override
        self.assertEqual(config._channel_value(table, "esr", 0), 50)
        self.assertEqual(config._channel_value(table, "release", 9), 9)   # the default
        self.assertEqual(config._channel_value({}, None, 3), 3)

    def test_the_shipped_esr_knobs_are_releases(self):
        """Almost the same model as release (Calixte, 2026-09-07): every selection knob of the
        family is release's, the rate path included (off, `rising_per_day` 0). Read per line,
        through the family. The one place ESR differs from release in scale is the smallest
        line -- esr153 at ~4k reports a week against release's 153k -- and a lower `esr153`
        entry beside the family's is how that would be tuned, not by moving the family."""
        for line in LINES + ("esr",):
            for typ in ("installs", "protos"):
                self.assertEqual(config.get_threshold(typ, "Firefox", line),
                                 config.get_threshold(typ, "Firefox", "release"), (line, typ))
            for typ in ("floor", "ratio", "mature_after_days", "mature_installs",
                        "min_build_installs", "rising_per_day", "rising_protos",
                        "real_installs", "real_alert_rate"):
                self.assertEqual(config.get_spike(typ, "Firefox", line),
                                 config.get_spike(typ, "Firefox", "release"), (line, typ))
        self.assertEqual(config.get_spike("rising_per_day", "Firefox", "esr140"), 0)
        self.assertEqual(config.get_threshold("installs", "Firefox", "esr140"), 50)
        # Nightly and beta read exactly what they read before.
        self.assertEqual(config.get_threshold("installs", "Firefox", "nightly"), 1)
        self.assertEqual(config.get_spike("floor", "Firefox", "beta"), 10)


class TestTheShippedChannels(unittest.TestCase):
    def test_only_the_current_line_is_a_channel(self):
        """esr153 is the current ESR line. esr115 and esr140 were declared with it and retired
        two hours later (Calixte, 2026-09-07); 128 has had no build since 2025; the bare family
        is policy, not a lineage. The family code still understands any line label, so the next
        line is one entry here and one `searchfox.Repo` member."""
        self.assertIn("esr153", config.get_channels())
        self.assertIn("esr153", models.CHANNEL_TYPE.enums)
        for retired in ("esr115", "esr140", "esr128", "esr"):
            self.assertNotIn(retired, config.get_channels(), retired)
        self.assertEqual(config.get_channels(), ["nightly", "beta", "release", "esr153"])

    def test_a_retired_label_still_reads_back(self):
        """A Postgres enum label cannot be dropped, and a retired line's rows may outlive the
        label in `config.channels`. SQLAlchemy would raise `LookupError` on every read of such a
        row -- `tasks.html` joins `Build.channel` for 500 rows, and did 500 on v166 -- so
        `CHANNEL_TYPE` hands the raw label back instead. sqlite here; the Postgres half, which
        is the one that failed in production (the dialect adapts an `Enum` subclass away), is
        in tests/test_enum_migration_pg.py."""
        from sqlalchemy import text

        from crashclouseau import db

        models.LastDate.__table__.create(bind=db.engine, checkfirst=True)
        db.session.execute(text("DELETE FROM lastdate WHERE channel = 'esr140'"))
        db.session.execute(text(
            "INSERT INTO lastdate (channel, mindate, maxdate) VALUES ('esr140', NULL, NULL)"))
        db.session.commit()
        try:
            rows = {r.channel for r in db.session.query(models.LastDate).all()}
            self.assertIn("esr140", rows)
            self.assertEqual(models.LastDate.get("esr140"), (None, None))
            # A single-column query, the shape `Dossier.list_tasks` reads `Build.channel` in.
            self.assertIn("esr140", {c for (c,) in db.session.query(models.LastDate.channel)})
        finally:
            db.session.execute(text("DELETE FROM lastdate WHERE channel = 'esr140'"))
            db.session.commit()
        # `api.py` validates `?channel=` against this; the wrapped Enum's list is still there.
        self.assertEqual(list(models.CHANNEL_TYPE.enums), config.get_channels())

    def test_the_enum_migration_carries_every_channel_label(self):
        """A long-lived Postgres has the enum it was created with; `_ensure_enum_values` adds
        the rest on the release phase (proved on a real Postgres in
        tests/test_enum_migration_pg.py). The labels are ours and still checked before they
        are quoted into DDL."""
        self.assertEqual(models._ENUM_ADDITIONS["CHANNEL_TYPE"], tuple(config.get_channels()))
        for label in models._ENUM_ADDITIONS["CHANNEL_TYPE"]:
            self.assertRegex(label, models._ENUM_LABEL)
        self.assertIsNone(models._ENUM_LABEL.match("esr'; DROP TYPE"))

    def test_each_line_is_declared_and_armed_by_the_family(self):
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            for line in LINES:
                with self.subTest(line=line):
                    self.assertTrue(config.autofile_channel_declared(line))
                    self.assertFalse(config.autofile_channel_held(line))
                    pol = config.get_agent_autofile(line)
                    self.assertTrue(pol["enabled"])
                    self.assertEqual(pol["comment_on_existing"], "skip")
                    self.assertEqual(pol["daily_cap"], 2)
                    self.assertEqual(pol["summary_prefix"], "[new in esr]")
                    self.assertTrue(pol["nominate_tracking"])
        with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "0"}):
            self.assertFalse(config.get_agent_autofile("esr140")["enabled"])
        # Not in the config file's `agent.channels`: like release, a line is triaged only when
        # a deployment names it in `AGENT_CHANNELS`.
        for line in LINES:
            self.assertNotIn(line, config.get_agent().get("channels"))

    def test_a_line_may_tighten_or_hold_its_familys_policy(self):
        agent = dict(config.get_agent())
        autofile = dict(agent["autofile"])
        autofile["channels"] = {**autofile["channels"],
                                "esr153": {"daily_cap": 1},
                                "esr166": {"enabled": False}}     # the next line, held
        agent["autofile"] = autofile
        with mock.patch.object(config, "get_agent", return_value=agent), \
                mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
            # esr153: its own cap, the family's everything else.
            pol = config.get_agent_autofile("esr153")
            self.assertEqual(pol["daily_cap"], 1)
            self.assertEqual(pol["summary_prefix"], "[new in esr]")
            self.assertTrue(pol["enabled"])
            # esr166: held on its own -- a decision, so still declared; esr153 untouched.
            self.assertTrue(config.autofile_channel_held("esr166"))
            self.assertTrue(config.autofile_channel_declared("esr166"))
            self.assertFalse(config.get_agent_autofile("esr166")["enabled"])
            self.assertFalse(config.autofile_channel_held("esr153"))
            self.assertTrue(config.get_agent_autofile("esr153")["enabled"])

    def test_a_line_publishes_no_calibrated_probability(self):
        """`agent.calibration.channels.esr = {}`: named and unmeasured, like release."""
        for line in LINES + ("esr",):
            self.assertEqual(config.get_agent_calibration(line), {})
        self.assertTrue(config.get_agent_calibration("nightly"))

    def test_a_line_has_no_population_rates(self):
        """Nobody measured ESR's bit-flip / broken-CPU rates -- release's are absent for the
        same reason -- so the prompt drops the comparison rather than quoting nightly's."""
        for line in LINES:
            self.assertIsNone(sigage.population_bit_flip_rate(line))
            self.assertIsNone(sigage.population_broken_cpu_rate(line))
            self.assertEqual(sigage.population_label(line), "Firefox")


class TestSocorroAndBuildhub(unittest.TestCase):
    def test_a_line_asks_socorro_about_the_family(self):
        for line in LINES:
            self.assertEqual(utils.get_search_channel(line), "esr")
        self.assertEqual(utils.get_search_channel("esr"), "esr")

    def test_buildhub_target_channel_and_version_pattern(self):
        for line in LINES:
            self.assertEqual(buildhub.target_channel(line), "esr")
        for ch in ("nightly", "beta", "release"):
            self.assertEqual(buildhub.target_channel(ch), ch)
            self.assertEqual(buildhub.version_pat(ch), buildhub.VERSION_PATS[ch])
        # The line's own major. Verified against Buildhub live 2026-09-07: this pattern returned
        # exactly 140.7.0esr .. 140.15.0esr for `esr140`, the 153 one 153.0esr/153.1.0esr/
        # 153.2.0esr, and release's pattern returned NO esr build at all (the suffix).
        pat = buildhub.version_pat("esr140")
        self.assertEqual(pat, r"140\.[0-9]+(\.[0-9]+)?esr")
        for v in ("140.15.0esr", "140.7.1esr", "140.0esr"):
            self.assertTrue(re.fullmatch(pat, v), v)
        for v in ("153.2.0esr", "115.40.0esr", "140.15.0", "1140.1.0esr", "140.15.0esrx"):
            self.assertFalse(re.fullmatch(pat, v), v)
        self.assertTrue(re.fullmatch(buildhub.version_pat("esr153"), "153.0esr"))
        self.assertEqual(buildhub.version_pat("esr"), r"[0-9]+\.[0-9]+(\.[0-9]+)?esr")
        self.assertFalse(re.fullmatch(buildhub.VERSION_PATS["release"], "140.15.0esr"))
        self.assertEqual(buildhub.version_pat("fenix"), "*")

    def test_buildhub_get_keys_the_result_by_our_label(self):
        """Buildhub's bucket says `esr`; `Build.put_data` writes the key straight into
        `builds.channel`, so the answer has to be keyed by the label asked for -- and the query
        has to ask Buildhub for `esr` with the line's own regexp."""
        fake = {"aggregations": {"products": {"buckets": [
            {"key": "firefox", "channels": {"buckets": [
                {"key": "esr", "buildids": {"buckets": [
                    {"key": "20260826142222",
                     "revisions": {"buckets": [{"key": "1ace7e56a4461234abcd"}]},
                     "versions": {"buckets": [{"key": "140.15.0esr"}]}}]}}]}}]}}}
        sent = []

        def fake_request(params, sleep, retry, callback):
            sent.append(params)
            return callback(fake)

        with mock.patch.object(buildhub, "make_request", side_effect=fake_request):
            got = buildhub.get("20260801000000", "esr140", prods="Firefox")
        self.assertEqual(list(got), ["Firefox"])
        self.assertEqual(list(got["Firefox"]), ["esr140"])
        (bid, info), = got["Firefox"]["esr140"].items()
        self.assertEqual(bid, utils.get_build_date("20260826142222"))
        self.assertEqual(info, {"revision": "1ace7e56a446", "version": "140.15.0esr"})
        filters = sent[0]["query"]["bool"]["filter"]
        self.assertIn({"term": {"target.channel": "esr"}}, filters)
        self.assertIn({"regexp": {"target.version": r"140\.[0-9]+(\.[0-9]+)?esr"}}, filters)
        # Release is untouched: same label in, same label out, same query.
        fake["aggregations"]["products"]["buckets"][0]["channels"]["buckets"][0]["key"] = "release"
        with mock.patch.object(buildhub, "make_request", side_effect=fake_request):
            got = buildhub.get("20260801000000", "release", prods="Firefox")
        self.assertEqual(list(got["Firefox"]), ["release"])
        filters = sent[1]["query"]["bool"]["filter"]
        self.assertIn({"term": {"target.channel": "release"}}, filters)
        self.assertIn({"regexp": {"target.version": buildhub.VERSION_PATS["release"]}}, filters)


class TestTheCodeReads(unittest.TestCase):
    def test_a_line_reads_its_own_searchfox_tree(self):
        for line in LINES:
            repo = searchfox.repo_for_channel(line)
            self.assertEqual(repo.value, "mozilla-" + line)
            self.assertEqual(repo.tree, "firefox-" + line)
        # A line searchfox does not index degrades to central like any unknown channel; so
        # does the bare family, which is never a crash's channel.
        self.assertEqual(searchfox.repo_for_channel("esr9"), searchfox.Repo.CENTRAL)
        self.assertEqual(searchfox.repo_for_channel("esr"), searchfox.Repo.CENTRAL)
        self.assertEqual(searchfox.repo_for_channel("release"), searchfox.Repo.RELEASE)

    def test_a_line_label_is_its_own_hg_repository(self):
        """libmozdata builds `releases/mozilla-<channel>` for anything but nightly, so the
        label IS the repo selector -- for the pushlog, raw-revs, annotate and every link."""
        for line in LINES:
            self.assertTrue(
                Mercurial.get_repo_url(line).endswith("/releases/mozilla-" + line), line)


class TestTheBuildType(unittest.TestCase):
    def test_esr_is_release_plus_moz_esr(self):
        """`set_define("MOZ_ESR", milestone.is_esr)` (build/moz.configure/init.configure, read
        2026-09-07): ON in an ESR build, so "off" is the wrong answer there; the rest of the
        partition is release's. Keyed by the family, so every line reads the same table."""
        for line in LINES + ("esr",):
            with self.subTest(channel=line):
                self.assertEqual(co.channel_on_deny(line),
                                 co.channel_on_deny("release") | {"MOZ_ESR"})
                self.assertEqual(co.channel_off(line), co.channel_off("release"))
                self.assertEqual(co.guard_deny(line), co.guard_deny("release") | {"MOZ_ESR"})
                # The two milestone macros are released from the deny list here as on release,
                # so a `#ifdef NIGHTLY_BUILD` hollow symbol is detectable on an ESR crash.
                self.assertEqual(
                    (co.build_type_deny(line) | co.PLATFORM_DENY) - co.guard_deny(line),
                    {"NIGHTLY_BUILD", "EARLY_BETA_OR_EARLIER"})
        # Nightly's table is byte-identical, and MOZ_ESR appears in no other channel's.
        self.assertNotIn("MOZ_ESR", co.CHANNEL_ON_DENY | co.channel_off("nightly"))
        self.assertNotIn("MOZ_ESR", co.channel_on_deny("release") | co.channel_off("release"))
        self.assertNotIn("MOZ_ESR", co.channel_on_deny("beta") | co.channel_off("beta"))

    def test_the_skeptic_prompt_names_the_esr_build_and_matches_the_gate(self):
        text = roles._compiled_out_text("esr140")
        self.assertIn("This crash is on ESR 140, NOT nightly", text)
        self.assertIn("ON in the ESR 140 build that crashed", text)
        # Rendered from the gate, so the two cannot drift: the ON list is the gate's ON set.
        self.assertIn(", ".join(sorted(co.channel_on_deny("esr140"))), text)
        self.assertIn(", ".join(sorted(co.channel_off("esr140"))), text)
        self.assertIn("MOZ_ESR", text)
        self.assertEqual(roles._build_name("esr153"), "ESR 153")
        self.assertEqual(roles._build_name("esr"), "ESR")
        self.assertEqual(roles._build_name("release"), "release")
        self.assertEqual(roles._build_name(None), "nightly")
        # Nightly's prompt is untouched.
        self.assertEqual(roles._compiled_out_text("nightly"), roles._COMPILED_OUT)


class TestTheProse(unittest.TestCase):
    def test_the_provenance_line_says_esr(self):
        for line in LINES:
            self.assertIn("analyses ESR crashes", report_bug._provenance(line))
        self.assertIn("analyses release crashes", report_bug._provenance("release"))

    def test_the_channel_age_line_speaks_of_the_family(self):
        """`signature_first_seen_channel` was measured over Socorro's one `esr` channel, so
        "new on ESR" is the honest sentence; "new on esr140" would claim a clock nobody read."""
        crash = {"channel": "esr140", "signature_first_seen_channel": "20260826142222",
                 "buildid": "20260826142222"}
        lines, guidance = triage._channel_age_lines(crash, "20250101000000", 600)
        self.assertEqual(len(lines), 1)
        self.assertIn("NEW ON ESR:", lines[0])
        self.assertIn("first ESR report is build 20260826142222", lines[0])
        self.assertIn("new to ESR users", guidance)
        # A channel with no label still says nothing, as before.
        self.assertEqual(
            triage._channel_age_lines(dict(crash, channel="nightly-asan"), "20250101000000", 600),
            ([], None))


class TestTheMarks(unittest.TestCase):
    def test_the_tracking_flag_is_the_esr_family_of_flags(self):
        """`cf_tracking_firefox_esr115/128/140/153` exist on BMO (`GET /rest/field/bug`,
        2026-09-07); `cf_tracking_firefox140` is Firefox 140's retired RELEASE flag and would
        put an ESR bug in nobody's queue."""
        self.assertEqual(report_bug._tracking_flag("140.15.0esr", "esr140"),
                         "cf_tracking_firefox_esr140")
        self.assertEqual(report_bug._tracking_flag("153.0esr", "esr153"),
                         "cf_tracking_firefox_esr153")
        self.assertEqual(report_bug._tracking_flag("115.40.0esr", "esr115"),
                         "cf_tracking_firefox_esr115")
        self.assertEqual(report_bug._tracking_flag("155.0.1", "release"), "cf_tracking_firefox155")
        self.assertEqual(report_bug._tracking_flag("155.0.1"), "cf_tracking_firefox155")
        self.assertIsNone(report_bug._tracking_flag("", "esr140"))

    def test_an_esr_spike_appearance_carries_the_familys_marks(self):
        brief = {"signature": "Foo::Bar", "channel": "esr140", "product": "Firefox",
                 "version": "140.15.0esr", "buildid": "20260826142222",
                 "spike": {"kind": "build_day", "count": 60, "installs": 55,
                           "baseline": [0, 0, 0]},
                 "first_seen_channel": "20260826142222"}
        self.assertTrue(spike_report.is_new_signature(brief))
        with mock.patch.object(spike_report, "build_spike_comment", return_value="c"):
            p = spike_report.build_spike_preview(brief, None, product="Core", component="General")
        self.assertEqual(p["title"], "[new in esr] Crash in [@ Foo::Bar]")
        self.assertEqual(p["tracking_flag"], "cf_tracking_firefox_esr140")
        self.assertEqual(p["version"], "unspecified")
        # An old signature getting loud: the nomination still, the "new in" mark not.
        rise = dict(brief, spike={"kind": "build_day", "count": 60, "installs": 55,
                                  "baseline": [5, 4, 6]})
        with mock.patch.object(spike_report, "build_spike_comment", return_value="c"):
            p = spike_report.build_spike_preview(rise, None, product="Core", component="General")
        self.assertEqual(p["title"], "Crash in [@ Foo::Bar]")
        self.assertEqual(p["tracking_flag"], "cf_tracking_firefox_esr140")


class TestVersionRates(unittest.TestCase):
    def test_esr_dot_releases_keep_their_order(self):
        self.assertLess(sigage._version_key("140.10.1esr"), sigage._version_key("140.10.2esr"))
        self.assertEqual(sigage._version_key("153.0esr"), (153, 0))
        self.assertEqual(sigage._version_key("140.15.0esr"), (140, 15, 0))
        self.assertEqual(sigage._version_key("155.0.1"), (155, 0, 1))      # unchanged
        self.assertEqual(sigage._version_key("156.0b3"), (156, 0, 3))

    @staticmethod
    def _result():
        def day(d, counts):
            return {"term": d, "facets": {"version": [{"term": v, "count": c}
                                                      for v, c in counts.items()]}}
        return {"facets": {
            "histogram_date": [
                day("2026-08-27T00:00:00", {"140.15.0esr": 400, "153.1.0esr": 10,
                                            "153.2.0esr": 30}),
                day("2026-08-28T00:00:00", {"140.15.0esr": 420, "153.1.0esr": 8,
                                            "153.2.0esr": 34}),
            ],
            "version": [
                {"term": "140.15.0esr", "facets": {"build_id": [{"term": "20260826142222"}]}},
                {"term": "153.1.0esr", "facets": {"build_id": [{"term": "20260811201151"}]}},
                {"term": "153.2.0esr", "facets": {"build_id": [{"term": "20260826022508"}]}},
            ]}}

    def test_a_line_sees_only_its_own_versions(self):
        """Without the cut the "preceding version" of 153.1.0esr is 140.15.0esr, and a "step"
        between two lines' populations is not a step. `version_rates` passes the line's major;
        the family-wide series is what a caller with no line gets, as before."""
        whole = sigage.summarize_version_rates(self._result(), min_reports=10)
        self.assertEqual([r["version"] for r in whole["versions"]],
                         ["140.15.0esr", "153.1.0esr", "153.2.0esr"])
        line = sigage.summarize_version_rates(self._result(), min_reports=10, major=153)
        self.assertEqual([r["version"] for r in line["versions"]], ["153.1.0esr", "153.2.0esr"])
        self.assertEqual(line["step"]["version"], "153.2.0esr")
        self.assertEqual(line["step"]["from_version"], "153.1.0esr")
        self.assertEqual(line["step"]["build_ids"], ["20260826022508"])
        other = sigage.summarize_version_rates(self._result(), min_reports=10, major=140)
        self.assertEqual([r["version"] for r in other["versions"]], ["140.15.0esr"])
        self.assertIsNone(other["step"])
        self.assertEqual(sigage.summarize_version_rates(self._result(), major=115)["versions"], [])

    def test_version_rates_asks_the_family_and_cuts_to_the_line(self):
        asked = []

        class FakeSuperSearch:
            URL = "https://crash-stats.example/api/SuperSearch/"   # `version_rates` reads it

            def __init__(self, queries=None, **kw):
                for q in queries or []:
                    asked.append(q.params)
                    q.handler({"facets": {"version": [], "histogram_date": []}}, q.handlerdata)

            def wait(self):
                return self

        with mock.patch.object(sigage.socorro, "SuperSearch", FakeSuperSearch), \
                mock.patch.object(sigage, "summarize_version_rates",
                                  return_value={"marker": 1}) as summ:
            self.assertEqual(sigage.version_rates("Foo::Bar", channel="esr153"), {"marker": 1})
            self.assertEqual(summ.call_args.kwargs["major"], 153)
            sigage.version_rates("Foo::Bar", channel="release")
            self.assertIsNone(summ.call_args.kwargs["major"])
        self.assertEqual(asked[0]["release_channel"], "esr")
        self.assertEqual(asked[1]["release_channel"], "release")


if __name__ == "__main__":
    unittest.main()
