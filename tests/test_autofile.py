# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Automatic Bugzilla filing — the ONE unattended write in the product.
#   DATABASE_URL=sqlite:// python -m unittest tests.test_autofile
import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import inspect  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug  # noqa: E402
from crashclouseau import config as cconfig  # noqa: E402


_PREVIEW = {
    "title": "Crash in [@ Foo::Bar]",
    "comment": "the whole bug opener",
    "product": "Core", "component": "DOM: Core & HTML",
    "version": "Trunk", "type": "defect",
    "keywords": ["crash", "regression"],
    "cf_crash_signature": "[@ Foo::Bar]",
    "blocked": ["clouseau", 42],
    "needinfo": ":dev, can you have a look please?",
    "needinfo_email": "dev@moz.example",
}
_INFO = {"uuid": "u-1", "signature": "Foo::Bar", "channel": "nightly"}


def _cfg(**over):
    base = {"enabled": True, "min_confidence": 70, "verdicts": ["lead", "culprit"],
            "needinfo": True, "daily_cap": 10, "comment_on_existing": True}
    base.update(over)
    return base


class _Base(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.comments = []
        self.puts = []
        self.filed = []
        p = [
            mock.patch.object(bugzilla_apply.config, "get_agent_autofile",
                              return_value=_cfg()),
            mock.patch.object(bugzilla_apply.config, "get_bugzilla_token",
                              return_value="tok"),
            mock.patch.object(bugzilla_apply.models.Dossier, "already_filed",
                              return_value=None),
            mock.patch.object(bugzilla_apply.models.Dossier, "filed_bugs_since",
                              return_value=0),
            mock.patch.object(bugzilla_apply.models.Dossier, "record_filed_bug",
                              side_effect=lambda u, i: self.filed.append((u, i)) or True),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[]),
            mock.patch.object(bugzilla_apply, "_create_bug",
                              side_effect=lambda p, t: self.created.append(p) or 999),
            mock.patch.object(bugzilla_apply, "_post_comment",
                              side_effect=lambda b, t, pv, tok: self.comments.append((b, t)) or 7),
            mock.patch.object(bugzilla_apply, "_put_bug",
                              side_effect=lambda b, c, t: self.puts.append((b, c)) or b),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        pv = mock.patch("crashclouseau.report_bug.build_bug_preview", return_value=_PREVIEW)
        pv.start()
        self.addCleanup(pv.stop)

    def _file(self, verdict="lead", confidence=70, **cfg_over):
        if cfg_over:
            bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**cfg_over)
        return bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}},
                                           verdict, confidence)


class TestGates(_Base):
    def test_files_a_rung70_lead(self):
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (999, "new_bug"))
        self.assertEqual(len(self.created), 1)

    def test_disabled_is_the_kill_switch(self):
        res = self._file(enabled=False)
        self.assertFalse(res["filed"])
        self.assertEqual(self.created, [])

    def test_below_the_rung_is_not_filed(self):
        # The whole point of choosing rung 70: the medium rung (50) is ~2.5x the volume
        # and the population the blind reviewer refutes most often.
        for conf in (None, 25, 50, 69):
            with self.subTest(conf=conf):
                self.assertFalse(self._file(confidence=conf)["filed"])
        self.assertEqual(self.created, [])

    def test_abstain_is_never_filed(self):
        self.assertFalse(self._file(verdict="abstain", confidence=90)["filed"])
        self.assertEqual(self.created, [])

    def test_never_files_twice_for_one_crash(self):
        # The orphan reaper re-runs a crashed run; without this it would re-file.
        bugzilla_apply.models.Dossier.already_filed.return_value = {"bug": 5}
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertEqual(res["prior"], {"bug": 5})
        self.assertEqual(self.created, [])

    def test_daily_cap_stops_a_runaway(self):
        bugzilla_apply.models.Dossier.filed_bugs_since.return_value = 10
        self.assertFalse(self._file()["filed"])
        self.assertEqual(self.created, [])

    def test_no_token_does_not_file(self):
        bugzilla_apply.config.get_bugzilla_token.return_value = ""
        self.assertFalse(self._file()["filed"])
        self.assertEqual(self.created, [])

    def test_unresolved_component_does_not_file(self):
        # `resolve_product_component` is best-effort and empties on a Bugzilla read
        # failure. Filing then gets rejected outright ("Bad argument param sent to
        # Bugzilla::Product::new"), but the reason to check is that a HALF-resolved pair
        # would land the bug on a team with no idea why they got it.
        for pc in ({"product": "", "component": "DOM"}, {"product": "Core", "component": ""}):
            with self.subTest(**pc):
                with mock.patch("crashclouseau.report_bug.build_bug_preview",
                                return_value={**_PREVIEW, **pc}):
                    res = self._file()
                self.assertFalse(res["filed"])
                self.assertIn("wrong component", res["skipped"])
        self.assertEqual(self.created, [])

    def test_offstack_observe_only_is_not_filed(self):
        # `_apply_offstack_observe_only` empties `result.actions` to "SUPPRESS any outward
        # action" while the off-stack canary's calibration is watched. This filer builds its
        # own payload and never reads `result.actions`, so it walked straight through that
        # suppression — 14 of 66 rung-70 verdicts in 30 days carry the flag.
        res = bugzilla_apply.autofile_bug(
            "u-1", _INFO, {}, {"candidate": {"node": "n"},
                               "corroborations": {"offstack_observe_only": True}},
            "lead", 70)
        self.assertFalse(res["filed"])
        self.assertIn("observe-only", res["skipped"])
        self.assertEqual(self.created, [])

    def test_an_unsymbolicated_signature_is_not_filed(self):
        # "Crash in [@ @0xe2ba40f948]" matches nothing and dedupes against nothing — the
        # address differs per crash — and if no frame resolves to code there is nothing
        # tying the crash to the candidate.
        info = {**_INFO, "signature": "@0xe2ba40f948"}
        res = bugzilla_apply.autofile_bug("u-1", info, {}, {"candidate": {"node": "n"}},
                                          "lead", 70)
        self.assertFalse(res["filed"])
        self.assertIn("unsymbolicated", res["skipped"])
        self.assertEqual(self.created, [])

    def test_a_partly_symbolicated_signature_still_files(self):
        # The guard must require EVERY component to be a bare address: this real signature
        # carries "unknown" and a raw frame yet is perfectly actionable.
        info = {**_INFO,
                "signature": "OOM | unknown | memcpy_repmovs_Intel | RTCEncodedFrameBase"}
        self.assertTrue(self._file_with(info)["filed"])

    def _file_with(self, info):
        return bugzilla_apply.autofile_bug("u-1", info, {}, {"candidate": {"node": "n"}},
                                           "lead", 70)

    def test_unsymbolicated_predicate(self):
        for sig in ("@0xe2ba40f948", "0xdeadbeef", "@0x0 | @0x1"):
            self.assertTrue(bugzilla_apply._is_unsymbolicated(sig), sig)
        for sig in ("@0x0 | js::gc::TraceEdgeInternal", "mozilla::Foo::Bar", "", None):
            self.assertFalse(bugzilla_apply._is_unsymbolicated(sig), sig)

    def test_no_candidate_does_not_file(self):
        with mock.patch("crashclouseau.report_bug.build_bug_preview", return_value=None):
            self.assertFalse(self._file()["filed"])
        self.assertEqual(self.created, [])


class TestDuplicates(_Base):
    def test_existing_open_bug_gets_a_comment_not_a_duplicate(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [12345]
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual((res["bug"], res["mode"]), (12345, "comment_on_existing"))
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [(12345, "the whole bug opener")])
        self.assertEqual(self.puts[0][0], 12345)

    def test_the_oldest_open_bug_is_preferred(self):
        # With several open bugs for one signature the earliest is the canonical one.
        # Newest-first would prefer a recent duplicate — possibly one we filed ourselves.
        bugzilla_apply._open_bugs_for_signature.return_value = [1990812, 2060922]
        res = self._file()
        self.assertEqual(res["bug"], 1990812)

    def test_a_failed_lookup_skips_rather_than_risking_a_duplicate(self):
        # `_open_bugs_for_signature` returns None on a network failure. A missed filing is
        # recoverable; a duplicate on BMO is not.
        bugzilla_apply._open_bugs_for_signature.return_value = None
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("not risking a duplicate", res["skipped"])
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [])

    def test_comment_on_existing_off_skips_it_does_not_file_anyway(self):
        bugzilla_apply._open_bugs_for_signature.return_value = [12345]
        res = self._file(comment_on_existing=False)
        self.assertFalse(res["filed"])
        self.assertEqual(self.created, [])
        self.assertEqual(self.comments, [])


class TestPayload(_Base):
    def test_needinfo_flag_is_on_the_created_bug(self):
        self._file()
        self.assertEqual(self.created[0]["flags"],
                         [{"name": "needinfo", "status": "?", "requestee": "dev@moz.example"}])

    def test_needinfo_can_be_turned_off_without_stopping_filing(self):
        res = self._file(needinfo=False)
        self.assertTrue(res["filed"])
        self.assertNotIn("flags", self.created[0])
        self.assertIsNone(res["needinfo"])

    def test_regressed_by_is_never_set(self):
        # The field that would assert causation as structured data. The suspected regressor
        # is named in the comment prose instead, where a human can weigh it.
        self._file()
        self.assertNotIn("regressed_by", self.created[0])


class TestBlockerLinking(_Base):
    """BMO's create endpoint accepts `blocks`/`blocked` and silently DISCARDS both —
    allizom filings 1852344/1852345/1852346 all came back 200 with blocks=[]. Only a
    follow-up PUT works, and that PUT is atomic: one unknown id rejects the whole list."""

    def test_blockers_are_never_sent_on_create(self):
        self._file()
        for key in ("blocked", "blocks"):
            self.assertNotIn(key, self.created[0])

    def test_blockers_are_linked_by_a_follow_up_put(self):
        res = self._file()
        self.assertEqual(self.puts, [(999, {"blocks": {"add": ["clouseau", 42]}})])
        self.assertEqual(res["blocks"], ["clouseau", 42])

    def test_an_unknown_regressor_bug_does_not_cost_the_meta_link(self):
        # The PUT rejects the WHOLE list on one bad id (code 101), so a restricted or wrong
        # regressor bug would otherwise also drop the `clouseau` link.
        calls = []

        def put(bug, changes, token):
            calls.append(changes)
            if 42 in changes["blocks"]["add"]:
                raise RuntimeError("Bug 42 does not exist.")
            return bug

        bugzilla_apply._put_bug.side_effect = put
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["blocks"], ["clouseau"])
        self.assertEqual([c["blocks"]["add"] for c in calls], [["clouseau", 42], ["clouseau"]])

    def test_a_failed_link_never_unfiles_the_bug(self):
        bugzilla_apply._put_bug.side_effect = RuntimeError("bugzilla 500")
        res = self._file()
        self.assertTrue(res["filed"])
        self.assertEqual(res["bug"], 999)
        self.assertEqual(res["blocks"], [])

    def test_what_failed_to_link_is_recorded(self):
        # A restricted regressor bug (BMO answers 102) silently cost 2 of the first 3 real
        # filings their regressor link. The gap has to be auditable.
        def put(bug, changes, token):
            if 42 in changes["blocks"]["add"]:
                raise RuntimeError("Bug 42 does not exist.")
            return bug
        bugzilla_apply._put_bug.side_effect = put
        res = self._file()
        self.assertEqual(res["blocks"], ["clouseau"])
        self.assertEqual(res["blocks_unlinked"], [42])

    def test_nothing_unlinked_records_nothing(self):
        res = self._file()
        self.assertNotIn("blocks_unlinked", res)


class TestSignatureMatching(_Base):
    def test_specific_signatures_also_search_summaries(self):
        for sig in ("mozilla::MediaDecoder::SetCDMProxy",
                    "OOM | unknown | memcpy_repmovs_Intel | RTCEncodedFrameBase"):
            self.assertTrue(bugzilla_apply._is_specific_signature(sig), sig)

    def test_short_or_bare_tokens_do_not(self):
        # Searching summaries for "memcpy" returns 32 open bugs; commenting on the wrong
        # one is worse than filing a duplicate.
        for sig in ("memcpy", "OOM", "", None, "shortish"):
            self.assertFalse(bugzilla_apply._is_specific_signature(sig), repr(sig))

    def test_payload_carries_what_bmo_requires(self):
        self._file()
        p = self.created[0]
        self.assertEqual(p["summary"], "Crash in [@ Foo::Bar]")
        self.assertEqual(p["description"], "the whole bug opener")
        for required in ("product", "component", "version", "type"):
            self.assertTrue(p.get(required), "BMO rejects a create without " + required)
        # `title`/`comment`/`needinfo*` are preview-only keys and must not be posted.
        for leaked in ("title", "comment", "needinfo", "needinfo_email"):
            self.assertNotIn(leaked, p)

    def test_the_outcome_is_recorded_for_audit_and_idempotence(self):
        self._file()
        uuid, info = self.filed[0]
        self.assertEqual(uuid, "u-1")
        self.assertEqual((info["bug"], info["mode"], info["filed"]), (999, "new_bug", True))
        self.assertTrue(info["at"])


class TestFailuresAreContained(_Base):
    def test_a_bugzilla_error_never_raises(self):
        bugzilla_apply._create_bug.side_effect = RuntimeError("bugzilla 500")
        res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("bugzilla write failed", res["skipped"])
        self.assertEqual(self.filed, [])          # nothing recorded, so a retry may re-file

    def test_a_preview_error_never_raises(self):
        with mock.patch("crashclouseau.report_bug.build_bug_preview",
                        side_effect=ValueError("boom")):
            res = self._file()
        self.assertFalse(res["filed"])
        self.assertIn("preview failed", res["skipped"])


class TestTokenResolution(unittest.TestCase):
    """Where the write token comes from.

    The reason this class exists: ``AUTOFILE_BUGS`` was armed on 08-05 with the key in
    ``LIBMOZDATA_CFG_BUGZILLA_TOKEN``, and every crash for the next day skipped with "no
    Bugzilla API token configured" — 13 rung-70 verdicts, 0 filings. libmozdata installs
    ``ConfigIni`` as its global provider and never calls ``set_config``, so the
    ``LIBMOZDATA_CFG_*`` variables its ``ConfigEnv`` class documents are read by nobody.
    Nothing about that failure was visible from either end: the variable was set, and
    ``libmozdata.config.get`` returned "" without complaint."""

    def _resolve(self, env, ini=""):
        with mock.patch.dict(os.environ, env, clear=False):
            for k in ("BUGZILLA_TOKEN", "LIBMOZDATA_CFG_BUGZILLA_TOKEN"):
                if k not in env:
                    os.environ.pop(k, None)
            with mock.patch.object(cconfig.libmozdata.config, "get", return_value=ini):
                return cconfig.get_bugzilla_token()

    def test_the_libmozdata_env_var_is_honoured(self):
        # The name libmozdata WOULD read if it read the environment at all.
        self.assertEqual(
            self._resolve({"LIBMOZDATA_CFG_BUGZILLA_TOKEN": "from-lmd-env"}),
            "from-lmd-env")

    def test_our_own_env_var_is_honoured(self):
        # Mirrors SOCORRO_TOKEN, the convention crashclouseau/__init__.py already uses.
        self.assertEqual(self._resolve({"BUGZILLA_TOKEN": "from-env"}), "from-env")

    def test_our_own_env_var_wins(self):
        self.assertEqual(
            self._resolve({"BUGZILLA_TOKEN": "ours",
                           "LIBMOZDATA_CFG_BUGZILLA_TOKEN": "theirs"}), "ours")

    def test_the_ini_still_works(self):
        # Unset environment -> ~/.mozdata.ini, which is how this runs locally.
        self.assertEqual(self._resolve({}, ini="from-ini"), "from-ini")

    def test_nothing_configured_is_empty_not_none(self):
        # `autofile_bug` tests `if not token`, and apply puts it in a header; None there
        # would be a TypeError deep in requests instead of a clean skip.
        self.assertEqual(self._resolve({}, ini=None), "")

    def test_the_writers_go_through_the_resolver(self):
        # The bug was one call site reading the token a way that could not work. Assert
        # no writer has drifted back to libmozdata's config directly.
        for mod in (bugzilla_apply, report_bug):
            src = inspect.getsource(mod)
            self.assertNotIn('"Bugzilla", "token"', src, mod.__name__)


if __name__ == "__main__":
    unittest.main()
