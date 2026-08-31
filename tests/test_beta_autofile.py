# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Filing on a SECOND channel — the beta half of tests/test_autofile.py, whose fixtures this
# module imports rather than re-deriving. It is that file's sibling, not a rewrite: everything
# that is not about the channel must keep filing exactly the way it files today.
#
# WHY THE GATE EXISTS AT ALL. Nothing in the filing half knew about channels: `autofile_bug`'s
# twelve documented gates contained none, and the ONLY thing keeping filing nightly-only was
# `get_agent_channels()` inside `enqueue_agent` — which `enqueue_agent(..., force=True)`
# bypasses BY DESIGN, and that is exactly what `retrigger_agent` calls for a tasks.html click.
# With `AUTOFILE_BUGS=1` live in prod, the day `INGEST_CHANNELS` gained `beta` (a Heroku config
# var: no deploy, no code change) one retrigger would have filed a beta bug under nightly's
# rules — `comment_on_existing: comment`, i.e. a comment on somebody else's open bug — and a
# bulk retrigger is a Bugzilla WRITE (`retrigger-destroys-the-filing-record`).
#
# THE NUMBERS THIS FILE PINS, WITH THEIR DENOMINATORS (plan #18 §2.3 and §3 items 17-22):
#
#   * An open bug on the signature is beta's MAJORITY path, not its edge -- but the numbers
#     here were wrong twice over. CORRECTED 2026-08-31, through the filer's own chain
#     (`_open_bugs_for_signature` -> `_split_by_application` -> `_split_out_metas`) on the
#     signatures beta's own selector picks: 39 of 77 = 50.6% (Wilson 39.7-61.5) carry an open
#     same-application non-meta bug, 36 of 67 = 53.7% at the selection level, against a matched
#     NIGHTLY CONTROL of 26 of 120 = 21.7% (15.2-29.9) -- z = 4.22, p = 2.4e-5. About half, not
#     58-59%, and still ~2.3x nightly, so "comment there instead" remains what beta would do
#     about half the time. The old "58 of 98 = 59.2%" was a top-100-BY-INSTALLS panel, which
#     does not discriminate channels at all (nightly 63%, release 64%, beta 59%); and 58%/64%
#     skipped `_split_out_metas` despite the words "non-meta". `_split_out_metas` is the split
#     that moves it (6 of 45, trackers 1472062/1588498); `_split_by_application` moves 0.
#
#   * Filing a SECOND bug on a signature we already filed is a 22.2% risk, not a theoretical
#     one. Of the 58 parseable signatures behind the 60 bugs the canary filed since
#     2026-08-05, 18 also crash on beta+aurora over 21 days: for 11 our bug is still OPEN (the
#     existing dedup sees it and skips), 7 are CLOSED, and `_fixed_after_build_bug` catches 3
#     and MISSES 2060922 DUPLICATE, 2061726 INVALID, 2063364 INVALID, 2064066 WORKSFORME —
#     4 of 18 = 22.2% (95% CI 9.0-45.2%), and those four resolutions are exactly the ones
#     where a duplicate is worst. `_open_bugs_for_signature` filters `resolution: "---"`, so a
#     bug WE filed and a human then closed is invisible to every BMO-side guard.
#
#   * The memory-safety carve-out is ~1% of beta rung-70 verdicts and it is the highest-value
#     1%: the deterministic poison-address gate fires on 1 of 57 filings, and 59.2% of beta
#     signatures have an open venue. :mccr8 on bug 2065051: "Bugs on poison crashes like that
#     should always be filed initially a security issue."
#
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     uv run python -m unittest tests.test_beta_autofile
#
# The `filed_bug` JSONB half needs a disposable Postgres and SILENTLY SKIPS without one:
#   docker exec clouseau_test_pg psql -U clouseau -d clouseau_test \
#     -c 'CREATE DATABASE clouseau_beta_autofile'
#   DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/clouseau_beta_autofile \
#     REDIS_URL=redis://localhost:6379/0 uv run python -m unittest tests.test_beta_autofile
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, db, feedback, models, report_bug  # noqa: E402
from crashclouseau import config as cconfig  # noqa: E402

# THE FIXTURES ARE test_autofile's, imported rather than copied. Two hand-maintained copies of
# `_cfg` / `_PREVIEW` / `_Base` would drift, and a drifted copy hides exactly the regressions
# both files exist to catch: this file's whole claim is "beta differs HERE and nowhere else".
from tests.test_autofile import (  # noqa: E402
    _INFO, _PREVIEW, _UNSAFE, _Base, _bug, _cfg,
)


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


# Captured BEFORE `_Base` replaces them with mocks (nothing is patched at import time). The
# Postgres class needs the REAL queries while keeping every Bugzilla write mocked.
_REAL_FILED_BUGS_SINCE = models.Dossier.filed_bugs_since
_REAL_RECORD_FILED_BUG = models.Dossier.record_filed_bug
_REAL_ALREADY_FILED_FOR_SIGNATURE = models.Dossier.already_filed_for_signature

# The beta crash. 20260819090452 is a real 155.0b2 — the build plan #18 §3 item 4 measured the
# DevEdition loss on (raw `beta` 1,600 crashes vs `["beta","aurora"]` 2,149 = 25.5%). `buildid`
# is a DATETIME because that is what `UUID.get_bid_chan_by_uuid` hands the filer, and the result
# is written into JSONB: a `datetime` reaching `payload["filed_bug"]` is not JSON.
_BETA_BUILDID = datetime(2026, 8, 19, 9, 4, 52, tzinfo=timezone.utc)
_BETA_INFO = {"uuid": "u-1", "signature": "Foo::Bar", "channel": "beta",
              "product": "Firefox", "version": "155.0b2", "buildid": _BETA_BUILDID}

# What the shipped beta overlay says, as a dict, so a test can state the policy it exercises
# without re-reading the file. `test_default_and_nightly_are_identical` checks it against
# config/global.json.
_BETA_POLICY = {"comment_on_existing": "skip", "daily_cap": 3}


def _unknown_overlay_keys():
    """``{channel: [keys the merged dict does not have]}`` for every ``agent.autofile.channels``
    overlay, i.e. the typo detector.

    Reads whatever ``config.get_agent()`` currently returns, so the same function runs against
    the shipped file AND against a deliberately misspelled one — a detector nobody has watched
    fail is not a detector."""
    over = (cconfig.get_agent().get("autofile", {}) or {}).get("channels") or {}
    out = {}
    for channel, overlay in over.items():
        # `channels` is dropped by the merge on purpose (an overlay cannot nest overlays), so
        # it is not a typo.
        unknown = sorted(set(overlay) - set(cconfig.get_agent_autofile(channel)) - {"channels"})
        if unknown:
            out[channel] = unknown
    return out


class TestTheOverlayCannotMoveNightly(unittest.TestCase):
    """The config half, against the SHIPPED config/global.json — no mocks, no DB.

    Every gate in `autofile_bug` reads one dict, so one channel argument covers `enabled`,
    `min_confidence`, `verdicts`, `needinfo`, `daily_cap`, `comment_on_existing` and
    `comment_max_bug_age_days` at once. That is also why the merge order is load-bearing, and
    why nothing here may move nightly: nightly has filed 60 bugs under these values."""

    def _shipped_block(self):
        """`agent.autofile` straight out of the file, so this test cannot be satisfied by the
        reader agreeing with itself."""
        with open("./config/global.json") as In:
            return json.load(In)["agent"]["autofile"]

    def test_default_and_nightly_are_identical(self):
        default = cconfig.get_agent_autofile()
        nightly = cconfig.get_agent_autofile("nightly")
        self.assertEqual(default, nightly)
        # ...and both are the TOP-LEVEL block. `enabled` is excluded because it is the env
        # kill switch's, not the file's (see `test_the_env_kill_switch_beats_the_overlay`).
        block = self._shipped_block()
        for key in ("min_confidence", "verdicts", "needinfo", "daily_cap",
                    "comment_max_bug_age_days"):
            if key in block:
                self.assertEqual(nightly[key], block[key], key)
        self.assertEqual(nightly["comment_on_existing"],
                         cconfig.comment_mode(block["comment_on_existing"]))
        # NOT VACUOUS: nightly is unmoved because no overlay names it, not because the overlay
        # mechanism is dead. Beta's overlay really does change the two knobs it names, and
        # nothing else.
        beta = cconfig.get_agent_autofile("beta")
        overlay = block["channels"]["beta"]
        for key, value in _BETA_POLICY.items():
            self.assertEqual(overlay.get(key), value, key)
            self.assertEqual(beta[key], value, key)
        # Exempting the overlay's OWN keys rather than `_BETA_POLICY`, so adding a further
        # deliberate per-channel value (`enabled`, say) is not a test failure — while a knob
        # nobody named moving IS.
        self.assertEqual({k: v for k, v in beta.items() if k not in overlay},
                         {k: v for k, v in nightly.items() if k not in overlay},
                         "the beta overlay moved a knob it does not name")
        # THE REQUIREMENT ITSELF, on the shipped file: whatever the value is spelled as, beta's
        # mode is never `comment`. ~51% of beta's SELECTED signatures have an open same-application
        # non-meta bug, so `comment` here means most beta filings become a comment on somebody
        # else's bug. (What the filer then DOES is
        # `TestBetaNeverCommentsOnSomebodyElsesBug`'s.)
        self.assertNotEqual(cconfig.comment_mode(beta["comment_on_existing"]), "comment")

    def test_every_overlay_key_is_a_real_key(self):
        """A misspelled overlay key is a SILENT NO-OP, and this repo has shipped that shape
        twice: `_ENUM_ADDITIONS` could never fire (so `DEPLOY.md:8` was a lie for months), and
        `blocks` was discarded by `create` for weeks. `comment_on_exisitng: "skip"` would leave
        beta commenting on strangers' bugs while the config file says it does not."""
        self.assertEqual(_unknown_overlay_keys(), {},
                         "an overlay key that the merged dict does not carry does NOTHING")
        # The overlay must be non-empty, or the assertion above is trivially true.
        overlays = self._shipped_block()["channels"]
        self.assertTrue(overlays and all(o for o in overlays.values()))
        # ...and the detector itself fires, on the exact typo it is written for.
        typo = {"autofile": {"enabled": False, "daily_cap": 10,
                             "channels": {"beta": {"comment_on_exisitng": "skip"}}}}
        with mock.patch.object(cconfig, "get_agent", return_value=typo):
            self.assertEqual(_unknown_overlay_keys(), {"beta": ["comment_on_exisitng"]})

    def test_the_env_kill_switch_beats_the_overlay(self):
        """`AUTOFILE_BUGS` is applied AFTER the merge, so no JSON file can defeat it — a kill
        switch a config file can defeat is not one. It writes to production BMO on a schedule,
        so it has to be stoppable without a deploy (a deploy also kills every in-flight ~20-min
        run at ~$3)."""
        armed = {"autofile": {"enabled": False, "daily_cap": 10,
                              "channels": {"beta": {"enabled": True}}}}
        with mock.patch.object(cconfig, "get_agent", return_value=armed):
            with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "0"}):
                self.assertFalse(cconfig.get_agent_autofile("beta")["enabled"])
            # Without the switch the same overlay DOES arm the channel, so the assertion above
            # is about the switch and not about a dead overlay key.
            with mock.patch.dict(os.environ):
                os.environ.pop("AUTOFILE_BUGS", None)
                self.assertTrue(cconfig.get_agent_autofile("beta")["enabled"])
            # AND THE OTHER DIRECTION, which is the live prod state.
            #
            # SETTLED, AND THE RULE IS "THE STRICTEST OF THE TWO WINS", because the two are
            # different kinds of statement. `AUTOFILE_BUGS=0` is a KILL SWITCH and beats any
            # JSON (asserted above). An EXPLICIT `channels.<ch>.enabled: false` is a decision
            # about one channel, and a global arm must not undo it either -- the env var is
            # global, so otherwise `AUTOFILE_BUGS=1` silently arms every declared channel and
            # plan #18's Phase 4 ("beta triage on, beta filing held") cannot be expressed in
            # prod at all. This was the argument
            # tests/test_shipped_channels.py::test_a_global_arm_must_not_arm_a_channel_nobody_armed
            # raised as an xfail; it is now the shipped behaviour and that test passes.
            disarmed = {"autofile": {"enabled": True,
                                     "channels": {"beta": {"enabled": False}}}}
            with mock.patch.object(cconfig, "get_agent", return_value=disarmed):
                with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
                    self.assertFalse(cconfig.get_agent_autofile("beta")["enabled"])
                    # ...and the channel that did NOT ask to be held is still armed, so this is
                    # a per-channel veto and not a global one.
                    self.assertTrue(cconfig.get_agent_autofile("nightly")["enabled"])
            # An ABSENT `enabled` key is not a veto: it inherits the top-level default and the
            # global arm. That is how beta was configured until the triage-only phase; it now
            # sets `enabled: false` explicitly, so the two shapes have to stay distinguishable —
            # a silent inherit must never read as a decision to hold.
            silent = {"autofile": {"enabled": False,
                                   "channels": {"beta": {"daily_cap": 3}}}}
            with mock.patch.object(cconfig, "get_agent", return_value=silent):
                with mock.patch.dict(os.environ, {"AUTOFILE_BUGS": "1"}):
                    self.assertTrue(cconfig.get_agent_autofile("beta")["enabled"])

    def test_the_comment_mode_accepts_the_legacy_booleans(self):
        """`comment_on_existing` stopped being a boolean, and the two booleans have to keep
        meaning what they have always DONE — `False` is `skip` (no comment AND no new bug), not
        `file_new`. Two tests in test_autofile pin that by name, and every test in the suite
        that exercises the filer mocks `get_agent_autofile` with a plain dict holding `True`,
        so the raw booleans reach the read site for real."""
        self.assertEqual(cconfig.comment_mode(True), "comment")
        self.assertEqual(cconfig.comment_mode(False), "skip")
        # An unrecognised value must not silently stop the filer: a typo is a nuisance, a typo
        # that turns writing off is invisible. Same direction as today's default.
        for value in ("coment", "", None, "0", 1, [], "SKIP_", "file-new"):
            with self.subTest(value=value):
                self.assertEqual(cconfig.comment_mode(value), "comment")
        # The three real values survive whitespace and case, because they are hand-typed.
        for value, expected in ((" skip ", "skip"), ("SKIP", "skip"), ("File_New", "file_new"),
                                ("comment", "comment"), ("skip", "skip"),
                                ("file_new", "file_new")):
            with self.subTest(value=value):
                self.assertEqual(cconfig.comment_mode(value), expected)
        self.assertEqual(cconfig.COMMENT_ON_EXISTING, ("comment", "skip", "file_new"))


class _BetaBase(_Base):
    """`_Base` plus a beta crash.

    `Dossier.already_filed_for_signature` is a JSONB query that fails CLOSED (its own `except`
    returns a truthy sentinel and the filer then skips). On sqlite it cannot run at all, so
    leaving it live would make every test in this file "pass" by declining to file. It is mocked
    to `None` in `_Base` for exactly that reason (it broke two of that file's own tests when it
    landed); the patch is repeated here so this class owns a mock it can retarget per test, and
    the Postgres class below delegates it back to the real query against real rows, which is
    where it is actually tested.

    `get_agent_autofile` is mocked by `_Base` with a plain dict, i.e. ARGUMENT-INSENSITIVE, so
    `_file_beta` writes the beta policy in by hand. That the overlay really produces that
    policy is `TestTheOverlayCannotMoveNightly`'s job; this class is about what the filer DOES
    with it."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(bugzilla_apply.models.Dossier, "already_filed_for_signature",
                              return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def _reset(self):
        for recorded in (self.created, self.comments, self.puts, self.filed):
            recorded.clear()

    def _file_beta(self, info=None, dossier=None, verdict="lead", confidence=70, **cfg_over):
        cfg = dict(_BETA_POLICY)
        cfg.update(cfg_over)
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**cfg)
        return bugzilla_apply.autofile_bug(
            "u-1", info or _BETA_INFO, {}, dossier or {"candidate": {"node": "n"}},
            verdict, confidence)


class TestTheTriageOnlyHoldIsVisible(_BetaBase):
    """Plan #18 Phase 4 — beta TRIAGED, beta filing HELD — has to leave a trace.

    The hold's whole purpose is to find out how much beta WOULD file before arming it. A hold
    that is indistinguishable from "the run abstained", "the run never happened" and "the
    global switch is off" measures nothing, and is the silent-no-op shape this codebase has
    been bitten by four times.

    `_autofile` suppresses the log line for `"autofile disabled"` on purpose: with
    `AUTOFILE_BUGS=0` every run on every channel would print one. So the per-channel hold has
    to say something ELSE or it inherits that silence."""

    def test_the_per_channel_hold_is_not_the_global_switch(self):
        """Three states, three answers. Only the middle one is a decision about a channel."""
        agent = dict(cconfig.get_agent())
        agent["autofile"] = {**agent["autofile"], "channels": {
            "beta": {"enabled": False}, "release": {"daily_cap": 1}}}
        with mock.patch.object(cconfig, "get_agent", return_value=agent):
            self.assertTrue(cconfig.autofile_channel_held("beta"))
            self.assertFalse(cconfig.autofile_channel_held("nightly"))   # global default only
            self.assertFalse(cconfig.autofile_channel_held("release"))   # overlay, no `enabled`
            self.assertFalse(cconfig.autofile_channel_held("esr"))       # undeclared
            self.assertFalse(cconfig.autofile_channel_held(None))

    def test_the_shipped_config_holds_beta_and_says_so(self):
        """Against the REAL config, because a mechanism that works while the shipped value does
        not use it is the gap that let the first beta filing ride on a deploy."""
        self.assertTrue(cconfig.autofile_channel_held("beta"))
        self.assertFalse(cconfig.autofile_channel_held("nightly"))
        res = self._file_beta(enabled=False)
        self.assertFalse(res["filed"])
        self.assertIn("held for channel", res["skipped"])
        self.assertIn("beta", res["skipped"])
        self.assertEqual(res["channel"], "beta")
        self.assertEqual((self.created, self.comments, self.puts), ([], [], []))

    def test_the_hold_is_logged_and_the_global_switch_is_not(self):
        """The suppression in `orchestrator._autofile` keys on the STRING, so this pins the two
        strings against the one predicate that reads them."""
        from crashclouseau.agent import orchestrator

        cases = {"autofile disabled": False,
                 "autofile held for channel 'beta' (triage-only)": True}
        for skipped, should_log in cases.items():
            with self.subTest(skipped=skipped):
                # `_autofile` imports `bugzilla_apply` INSIDE the function, so the name to
                # patch is the module's own, not an attribute of `orchestrator`.
                with mock.patch.object(orchestrator.models.CrashStack, "get_by_uuid",
                                       return_value=({}, {"channel": "beta"})), \
                        mock.patch.object(bugzilla_apply, "autofile_bug",
                                          return_value={"filed": False, "skipped": skipped}), \
                        self.assertLogs(level="INFO") as caught:   # `logger` IS the root logger
                    orchestrator.logger.info("marker")   # so assertLogs always has a record
                    orchestrator._autofile(
                        "u-1", {"dossier": {}}, {"verdict": "lead", "confidence": 90})
                logged = any("not filed" in line for line in caught.output)
                self.assertEqual(logged, should_log)


class TestTheChannelGate(_BetaBase):
    """`autofile_bug` fails closed on a channel nobody has decided about.

    "Somebody set this to false" and "nobody has thought about this channel" must not look the
    same from the filer, which is why the gate is a separate predicate from `enabled`. The gap
    it closes is `release`: it is a configured INGEST channel (`config.channels` is
    `["nightly", "beta", "release"]`) with no filing policy anywhere, and `update_all`'s
    `os.getenv("INGEST_CHANNELS", "").split() or config.get_channels()` means CLEARING the
    variable to "turn beta on" turns release on too."""

    def test_an_undeclared_channel_files_nothing(self):
        """`release` moved OUT of this list on 2026-08-31: it is now declared and HELD, which is
        a different gate (`enabled: false`) reached later in the same function. An undeclared
        channel is one nobody has decided about at all -- `esr` is the live example."""
        for channel in ("esr", "aurora", "Beta ", "", None):
            with self.subTest(channel=channel):
                self._reset()
                res = self._file_beta(info={**_BETA_INFO, "channel": channel})
                self.assertFalse(res["filed"])
                self.assertIn("autofile configuration", res["skipped"])
                self.assertEqual((self.created, self.comments, self.puts), ([], [], []))
        # The three channels somebody HAS decided about are not caught by it — the gate must not
        # be a global off switch.
        for channel in ("nightly", "beta", "release", "NIGHTLY"):
            with self.subTest(channel=channel):
                self.assertTrue(cconfig.autofile_channel_declared(channel))
        for channel in ("esr", None, ""):
            with self.subTest(channel=channel):
                self.assertFalse(cconfig.autofile_channel_declared(channel))

    def test_the_filer_reads_the_config_of_the_crashs_own_channel(self):
        """The overlay is dead config if the ONE call site drops its argument, and no test in
        the suite would notice: every mock of `get_agent_autofile` is argument-insensitive
        (`return_value`), and nothing uses `autospec=True`."""
        self._file_beta()
        bugzilla_apply.config.get_agent_autofile.assert_called_once_with("beta")
        bugzilla_apply.config.get_agent_autofile.reset_mock()
        bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)
        bugzilla_apply.config.get_agent_autofile.assert_called_once_with("nightly")

    def test_the_result_carries_the_channel_and_the_buildid(self):
        """Nothing downstream could answer "how is beta doing" without them.
        `feedback._filed_bugs` builds its ReviewNote row from exactly these keys and
        `_NOTE_MODES = ("new_bug",)` is precisely a never-comment channel's mode, so beta
        filings enter the review corpus POOLED with nightly's unless the row says which channel
        it came from — and retuning either arm against a pooled denominator is the mistake the
        hardware-noise work was written up to prevent."""
        res = self._file_beta(comment_on_existing="file_new")
        self.assertTrue(res["filed"])
        self.assertEqual(res["channel"], "beta")
        # A STRING, not the datetime the filer was handed: this dict is written into JSONB.
        self.assertEqual(res["buildid"], "20260819090452")
        # ...and it is the PERSISTED info that feedback reads, not just the return value.
        self.assertEqual(self.filed[-1][1]["channel"], "beta")
        self.assertEqual(self.filed[-1][1]["buildid"], "20260819090452")
        self.assertEqual(res["mode"], "new_bug")
        self._reset()
        res = bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}},
                                          "lead", 70)
        self.assertEqual(res["channel"], "nightly")


class TestBetaNeverCommentsOnSomebodyElsesBug(_BetaBase):
    """About half of beta's selected signatures already have an open same-application non-meta
    bug — 39/77 = 50.6% against a matched nightly control of 26/120 = 21.7% (p = 2.4e-5) — so
    this is a majority-ish path on beta, and the requirement is that none of it becomes a
    comment. `_split_out_metas` is the split that moves that number, not
    `_split_by_application`, which moves 0."""

    def test_beta_never_comments_on_an_existing_bug(self):
        # (That the SHIPPED beta config is not `comment` is asserted against config/global.json
        # in `test_default_and_nightly_are_identical` — `get_agent_autofile` is mocked here.)
        # `skip` is what the legacy `False` always did; `file_new` is the mode beta moves to
        # after the self-duplication guard lands. Neither may write on the open bug, and
        # `False` must behave exactly like `skip` or the two named tests in test_autofile are
        # pinning a different thing from what beta ships.
        for mode in ("skip", False, "file_new"):
            with self.subTest(mode=mode):
                self._reset()
                bugzilla_apply._open_bugs_for_signature.return_value = [_bug(12345)]
                res = self._file_beta(comment_on_existing=mode)
                self.assertEqual(self.comments, [], "commented on somebody else's bug")
                # A needinfo PUT on a stranger's bug is a write too, and it is the one that
                # reaches a human. (The PUTs in `file_new` are on bug 999, the one we filed:
                # `blocks` and `regressed_by`.)
                self.assertEqual([b for b, _ in self.puts if b == 12345], [])
                self.assertNotEqual(res.get("bug"), 12345)
                if mode == "file_new":
                    self.assertTrue(res["filed"])
                    self.assertEqual(res["mode"], "new_bug")
                else:
                    self.assertFalse(res["filed"])
                    self.assertEqual(self.created, [])
                    self.assertIn("open bug 12345 exists", res["skipped"])

    def test_file_new_mode_files_and_names_the_bug_it_did_not_comment_on(self):
        """A new bug that does not say why it is not a comment on the open bug reads as a
        broken deduplicator — and the reason has to be the POLICY, not a claim that the
        evidence ruled that bug out. "We checked and it cannot be this" and "we do not write on
        other people's bugs from this channel" are different sentences, and a reader who cannot
        tell them apart cannot overrule either."""
        bugzilla_apply._open_bugs_for_signature.return_value = [_bug(12345)]
        with mock.patch("crashclouseau.report_bug.build_bug_preview",
                        return_value=_PREVIEW) as preview:
            res = self._file_beta(comment_on_existing="file_new")
        self.assertTrue(res["filed"])
        # The open bug reaches the comment builder as a related bug, and the flag that says
        # WHY reaches it too.
        self.assertEqual(preview.call_args.kwargs["related_bugs"], [12345])
        self.assertIs(preview.call_args.kwargs["never_comment"], True)
        # ...and it is recorded on the dossier, because this is the one place the filer
        # knowingly creates something that looks like a duplicate.
        self.assertEqual(res["predating_bugs"], [12345])

        # The note itself — the real function, since that is what a triager reads.
        note = report_bug.build_related_bugs_note([12345], never_comment=True)
        self.assertIn("bug 12345", note)
        self.assertIn("does not comment on existing bugs", note)
        self.assertIn("may well be the right place", note)
        # NOT the evidence claims: `predating` ("all predate the suspected regressor") and
        # `landing_unresolved` ("we could not establish when the candidate landed") are the two
        # other reasons this paragraph exists, and neither happened here.
        for phrase in ("predate", "could not", "cannot have"):
            self.assertNotIn(phrase, note)
        # The policy reason WINS over an unresolved landing date, so a filing routed by policy
        # is never reported as one routed by a failed hg lookup.
        self.assertEqual(
            report_bug.build_related_bugs_note([12345], landing_unresolved=True,
                                               never_comment=True),
            note)
        # Two bugs, and it still names both rather than pluralising into vagueness.
        both = report_bug.build_related_bugs_note([12345, 2057980], never_comment=True)
        self.assertIn("bug 12345", both)
        self.assertIn("bug 2057980", both)

    def test_the_prior_filing_guard_is_only_consulted_on_a_never_comment_channel(self):
        """Nightly must stay byte-identical: it files 60 bugs a month under `comment`, and the
        self-duplication guard is the unshipped half of plan #17 defect A. A guard that starts
        firing on nightly the day beta lands is a change nobody asked for."""
        guard = bugzilla_apply.models.Dossier.already_filed_for_signature
        bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}}, "lead", 70)
        guard.assert_not_called()
        for mode in ("skip", "file_new", False):
            with self.subTest(mode=mode):
                guard.reset_mock()
                self._reset()
                self._file_beta(comment_on_existing=mode)
                # CHANNEL-BLIND, and this is the SETTLED side of the argument the sibling
                # test raised. The guard's whole measured value is cross-channel: 4 of the 18
                # nightly-filed signatures that also crash on beta (22.2%, 95% CI 9.0-45.2%)
                # would collect a second Clouseau bug, and all four target bugs are CLOSED
                # (DUPLICATE / INVALID / INVALID / WORKSFORME) so nothing else can see them.
                # A `channel=` argument here would make the guard blind to exactly that
                # population. What keeps nightly byte-identical is the MODE test in
                # `autofile_bug` -- nightly's mode is `comment`, so nightly never reaches the
                # call at all, which is what `guard.assert_not_called()` above pins.
                guard.assert_called_once_with("Foo::Bar")

    def test_a_prior_filing_lookup_failure_produces_silence_not_a_duplicate(self):
        """Every sibling guard fails closed and this one has to as well — it returns a truthy
        sentinel rather than `None` on a DB error. It is also the sqlite reality: the JSONB path
        cannot compile there, so the query raises and the sentinel is what the filer sees."""
        bugzilla_apply.models.Dossier.already_filed_for_signature.return_value = {
            "skipped": "prior-filing lookup failed"}
        res = self._file_beta()
        self.assertFalse(res["filed"])
        self.assertEqual((self.created, self.comments), ([], []))

    def test_skip_is_not_a_global_off_switch_for_beta(self):
        """The 41% that makes beta worth arming at all: `skip` only declines the ~59% of
        signatures that already carry an open bug. A signature with none is filed normally, at
        beta's own cap — otherwise `skip` would be a very expensive way of doing nothing (4.2-5.8
        dossiers/day at $1-3 each)."""
        bugzilla_apply._open_bugs_for_signature.return_value = []
        res = self._file_beta()
        self.assertTrue(res["filed"], res.get("skipped"))
        self.assertEqual((res["bug"], res["mode"]), (999, "new_bug"))
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.comments, [])

    def test_a_withheld_crash_files_restricted_past_an_open_public_bug(self):
        """THE SECURITY REGRESSION THAT `skip` WOULD OTHERWISE INTRODUCE. `sensitive.is_withheld`
        used to be consulted ~100 lines below the skip, so a poison-address crash whose
        signature has an open PUBLIC bug produced NOTHING on beta: no restricted bug, no
        comment, no record. :mccr8 on bug 2065051 — "Bugs on poison crashes like that should
        always be filed initially a security issue" — and it is the one class that must never be
        dropped silently. Reach: 1 of 57 filings trips the poison gate and 59.2% of beta
        signatures have an open venue, so ~1% of beta rung-70 verdicts, and the highest-value
        1%."""
        for mode in ("skip", "file_new"):
            with self.subTest(mode=mode):
                self._reset()
                bugzilla_apply._open_bugs_for_signature.return_value = [_bug(2064600)]
                report_bug.build_bug_preview.return_value = dict(
                    _PREVIEW, groups=["core-security"], cc=["dev@moz.example"])
                res = self._file_beta(dossier=_UNSAFE, comment_on_existing=mode)
                # Not skipped, and not commented: the venue is public by construction
                # (`_open_bugs_for_signature` is deliberately unauthenticated), so a comment
                # there discloses exactly what the group protects.
                self.assertTrue(res["filed"], res.get("skipped"))
                self.assertEqual(self.comments, [])
                self.assertEqual(res["mode"], "new_bug")
                # The group reaches the POSTED BODY, not merely the preview: a key the payload
                # filter drops is a silent no-op, and for `groups` the silent no-op publishes a
                # use-after-free.
                self.assertEqual(self.created[0]["groups"], ["core-security"])
                self.assertEqual(self.created[0]["cc"], ["dev@moz.example"])
                self.assertEqual(res["security_groups"], ["core-security"])
                self.assertTrue(res["memory_unsafe_signals"])
                # THE SECURITY BRANCH, NOT THE POLICY BRANCH, OWNS THIS DECISION, and the order
                # is load-bearing: the never-comment override runs first and sets `bug_id =
                # None`, so without its `and not withheld` the branch below could never fire —
                # the restricted bug would carry the generic "this filer does not comment on
                # existing bugs" note, which ends "please duplicate if it is", and the audit
                # field would be lost. What a triager must read instead is WHY it was split.
                self.assertEqual(res["public_venue_declined"], 2064600)
                self.assertIn("Probably a duplicate of bug 2064600",
                              self.created[0]["description"])
                self.assertIn("memory-safety fault", self.created[0]["description"])
                # ...and never as `see_also`: BMO mirrors a local reference onto the referenced
                # bug, which would advertise on the PUBLIC bug that a restricted one exists.
                self.assertNotIn("see_also", self.created[0])
        # THE CONTRAST that makes the carve-out a carve-out: the same crash without the poison
        # address, same open bug, same config, files nothing at all. Under a bare `skip` the
        # withheld crash produced this — nothing — which is the one outcome mccr8 ruled out.
        self._reset()
        report_bug.build_bug_preview.return_value = _PREVIEW
        ordinary = self._file_beta()
        self.assertFalse(ordinary["filed"])
        self.assertEqual((self.created, self.comments), ([], []))


class TestTheDailyCapIsPerChannel(_BetaBase):
    """Beta's selections are 48% concentrated in the 4 days after a merge — exactly when a
    freshly uplifted regression is worth filing — so a shared cap of 10 lets that burst spend
    nightly's whole budget, and nightly's ordinary 2.86 bugs/day would eat beta's 3."""

    def _counter(self, **per_channel):
        def count(when, channel=None):
            return per_channel.get(channel, 0)
        bugzilla_apply.models.Dossier.filed_bugs_since.side_effect = count

    def test_the_daily_cap_is_per_channel(self):
        self._counter(nightly=10)
        res = self._file_beta(comment_on_existing="file_new")
        self.assertTrue(res["filed"], res.get("skipped"))
        # ...and the counter was asked about THIS crash's channel. Without the argument the
        # nightly count answers for beta and beta is dead every day nightly hits its cap.
        bugzilla_apply.models.Dossier.filed_bugs_since.assert_called_once_with(
            mock.ANY, channel="beta")
        # The mirror: beta's own cap of 3 still binds, and it is beta's cap, not nightly's 10.
        self._reset()
        self._counter(beta=3, nightly=0)
        res = self._file_beta(comment_on_existing="file_new")
        self.assertFalse(res["filed"])
        self.assertIn("daily cap 3", res["skipped"])
        self.assertIn("beta", res["skipped"])
        self.assertEqual(self.created, [])
        # And a beta burst does not stop nightly.
        self._reset()
        self._counter(beta=99)
        res = bugzilla_apply.autofile_bug("u-1", _INFO, {}, {"candidate": {"node": "n"}},
                                          "lead", 70)
        self.assertTrue(res["filed"], res.get("skipped"))


@unittest.skipUnless(_is_postgres(),
                     "the filed_bug JSONB queries need a disposable Postgres")
class TestTheFiledBugJsonbHalf(_Base):
    """The three JSONB queries the channel work touches, against a real backend: a path
    predicate cannot be exercised on sqlite, where `already_filed_for_signature` raises and
    returns its fail-closed sentinel instead.

    `_Base`'s Bugzilla mocks are kept — nothing here posts — but `already_filed_for_signature`
    is NOT mocked (it is what is under test) and `filed_bugs_since` / `record_filed_bug` are
    delegated to the real functions captured at import."""

    SIG = "Beta::Autofile"
    OTHER = "Beta::Other"
    # The build of crash 2064537's own report, and a real 155.0b2.
    NIGHTLY_BUILDID = datetime(2026, 8, 16, 8, 38, 33, tzinfo=timezone.utc)
    BETA_BUILDID = _BETA_BUILDID
    # The four measured misses: our own nightly filings whose bug a human then closed with a
    # resolution `_fixed_after_build_bug` deliberately ignores ("ONLY FIXED COUNTS"), which is
    # 4 of the 18 nightly-filed signatures that also crash on beta = 22.2% (CI 9.0-45.2%).
    CLOSED = ((2060922, "DUPLICATE"), (2061726, "INVALID"), (2063364, "INVALID"),
              (2064066, "WORKSFORME"))

    def setUp(self):
        super().setUp()
        # `_Base` mocks the guard to `None` (it has to: on sqlite the JSONB query raises and its
        # fail-closed sentinel would silence every non-comment-mode test in that file). Here the
        # query IS what is under test, so delegate the mock to the real function captured at
        # import — same shape `filed_bugs_since` / `record_filed_bug` use below.
        bugzilla_apply.models.Dossier.already_filed_for_signature.side_effect = (
            _REAL_ALREADY_FILED_FOR_SIGNATURE)
        models.create()
        self._clean()
        self.sig = models.Signature.get_id(self.SIG)
        self.other_sig = models.Signature.get_id(self.OTHER)
        self.nightly = models.Build(self.NIGHTLY_BUILDID, "Firefox", "nightly", "156.0a1", None)
        self.beta = models.Build(self.BETA_BUILDID, "Firefox", "beta", "155.0b2", None)
        db.session.add_all([self.nightly, self.beta])
        db.session.commit()

    def _clean(self):
        db.session.rollback()
        # Deleting the build cascades to uuids and dossiers.
        for buildid, channel in ((self.NIGHTLY_BUILDID, "nightly"), (self.BETA_BUILDID, "beta")):
            db.session.query(models.Build).filter(
                models.Build.buildid == buildid,
                models.Build.product == "Firefox",
                models.Build.channel == channel,
            ).delete(synchronize_session=False)
        db.session.commit()

    def tearDown(self):
        self._clean()

    def _uuid(self, name, build, sigid=None):
        db.session.add(models.UUID(name, sigid or self.sig, name, build.id))
        db.session.commit()
        models.Dossier.upsert(name, payload={"dossier": {}}, status="done")
        return name

    def _filed(self, name, build, info, sigid=None):
        """A done dossier carrying `filed_bug`, exactly as `record_filed_bug` writes it."""
        self._uuid(name, build, sigid=sigid)
        _REAL_RECORD_FILED_BUG(name, info)
        return name

    @staticmethod
    def _info(bug, signature, channel="nightly", filed=True, mode="new_bug", buildid=None):
        return {"filed": filed, "bug": bug, "signature": signature, "mode": mode,
                "channel": channel, "buildid": buildid or "20260816083833"}

    def test_a_bug_we_filed_is_found_after_a_human_closed_it(self):
        """The only guard that survives the target bug being CLOSED. `_open_bugs_for_signature`
        filters `resolution: "---"`, so `existing` is empty, the `comment_on_existing` branch
        never fires, `_bug_for_this_regression` is never asked and `already_commented` is only
        consulted once a venue has been CHOSEN — dead on this path. This query never asks BMO,
        which is also why it sees a RESTRICTED bug the unauthenticated venue lookup cannot."""
        for i, (bug, resolution) in enumerate(self.CLOSED):
            with self.subTest(bug=bug, resolution=resolution):
                sig = "{}::{}".format(self.SIG, bug)
                sigid = models.Signature.get_id(sig)
                self._filed("bta-01{:02d}-aaaa-bbbb-ccccddddeeee".format(i), self.nightly,
                            self._info(bug, sig), sigid=sigid)
                found = models.Dossier.already_filed_for_signature(sig)
                self.assertEqual(found["bug"], str(bug))
        # Another signature on the same panel is not a match, and a recorded SKIP is not a
        # filing: beta ships at `skip` and 59% of its signatures have an open bug, so most
        # beta rows will BE skips.
        self.assertIsNone(models.Dossier.already_filed_for_signature(self.OTHER))
        self._filed("bta-0110-aaaa-bbbb-ccccddddeeee", self.nightly,
                    {"filed": False, "skipped": "open bug 12345 exists",
                     "signature": self.OTHER, "channel": "nightly"},
                    sigid=self.other_sig)
        self.assertIsNone(models.Dossier.already_filed_for_signature(self.OTHER))
        self.assertIsNone(models.Dossier.already_filed_for_signature(""))

    def test_already_filed_for_signature_blocks_the_second_bug(self):
        """FIXED. The guard used to be called `channel=channel`, so it could only ever see
        filings from the SAME channel — and the population it was measured on is entirely
        CROSS-channel.

        The measurement (plan #18 item 19): of the 58 signatures behind our 60 nightly filings,
        18 also crash on beta; 11 still have our OPEN bug, and of the 7 closed ones
        `_fixed_after_build_bug` catches 3 and misses 4 — 2060922 DUPLICATE, 2061726 INVALID,
        2063364 INVALID, 2064066 WORKSFORME = 22.2% (CI 9.0-45.2%). Every one of those four is
        a NIGHTLY filing that a BETA run would duplicate, so scoping the query to the running
        crash's own channel makes the guard fire on nothing it was built for: beta has never
        filed anything, so `channel="beta"` matches zero rows on day one and the second bug is
        filed anyway.

        Item 19 says "channel-blind" in its own title; the scoping argument exists so nightly's
        behaviour stays byte-identical, and nightly already stays byte-identical because the
        guard is not consulted in `comment` mode at all
        (`test_the_prior_filing_guard_is_only_consulted_on_a_never_comment_channel`).

        The fix was one argument at the call site: `already_filed_for_signature(signature)`,
        with the MODE test above it doing the nightly-preservation job instead."""
        self._filed("bta-0200-aaaa-bbbb-ccccddddeeee", self.nightly,
                    self._info(2060922, self.SIG))
        # BMO shows nothing: the human resolved it DUPLICATE, so `_open_bugs_for_signature`
        # (resolution "---") cannot see it and `_fixed_after_build_bug` ignores it (not FIXED).
        bugzilla_apply._open_bugs_for_signature.return_value = []
        # THE BETA POLICY, explicitly. `_Base` mocks `get_agent_autofile` with `return_value=`,
        # which is ARGUMENT-INSENSITIVE, so without this the beta call gets nightly's `comment`
        # mode and the guard is never consulted at all -- for the same reason nightly never
        # consults it in production, not because of the query's scope. Every other never-comment
        # test goes through `_file_beta`; this one drives `autofile_bug` directly because it
        # needs its own uuid, so it has to set the policy itself.
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**_BETA_POLICY)
        res = bugzilla_apply.autofile_bug(
            "bta-0201-aaaa-bbbb-ccccddddeeee",
            {**_BETA_INFO, "signature": self.SIG}, {}, {"candidate": {"node": "n"}},
            "lead", 70)
        self.assertFalse(res["filed"], "filed a second bug for a signature we already filed")
        self.assertEqual(self.created, [])
        self.assertIn("already filed", res["skipped"])

    def test_the_scoped_guard_is_what_makes_that_filing_possible(self):
        """The mechanism behind the expected failure above, at the query level, so the diagnosis
        is not buried in an xfail: the row IS findable, and the query's own channel predicate is
        doing exactly what it documents. The defect is the ARGUMENT the filer passes
        (`bugzilla_apply.py:963`, the crash's own channel), which is why this test stays true
        after the fix."""
        self._filed("bta-0210-aaaa-bbbb-ccccddddeeee", self.nightly,
                    self._info(2060922, self.SIG))
        self.assertEqual(models.Dossier.already_filed_for_signature(self.SIG)["bug"], "2060922")
        self.assertEqual(
            models.Dossier.already_filed_for_signature(self.SIG, channel="nightly")["bug"],
            "2060922")
        self.assertIsNone(
            models.Dossier.already_filed_for_signature(self.SIG, channel="beta"),
            "a nightly filing is invisible to the beta run that would duplicate it")

    def test_the_daily_cap_counts_only_this_channels_actual_filings(self):
        """`filed_bugs_since` counted every row with a `filed_bug` KEY, and a SKIP is recorded
        under that same key. Beta ships at `skip` with 59% of its signatures carrying an open
        bug, so the moment beta is armed its skips would have started eating nightly's cap — a
        global filing stop caused by DECLINING to file."""
        since = datetime.now(timezone.utc) - timedelta(days=1)
        for i in range(10):
            self._filed("bta-03{:02d}-aaaa-bbbb-ccccddddeeee".format(i), self.nightly,
                        self._info(2060900 + i, self.OTHER), sigid=self.other_sig)
        # Three beta rows that are NOT filings: two skips and one row with no `filed_bug`.
        for i, skipped in enumerate(("open bug 12345 exists", "autofile disabled")):
            self._filed("bta-04{:02d}-aaaa-bbbb-ccccddddeeee".format(i), self.beta,
                        {"filed": False, "skipped": skipped, "signature": self.OTHER,
                         "channel": "beta"}, sigid=self.other_sig)
        self._uuid("bta-0410-aaaa-bbbb-ccccddddeeee", self.beta, sigid=self.other_sig)

        self.assertEqual(_REAL_FILED_BUGS_SINCE(since, channel="nightly"), 10)
        self.assertEqual(_REAL_FILED_BUGS_SINCE(since, channel="beta"), 0)
        self.assertEqual(_REAL_FILED_BUGS_SINCE(since), 10)
        # ...so ten nightly filings do not stop the beta one. End to end, with the real counter
        # behind the filer's own call.
        bugzilla_apply.models.Dossier.filed_bugs_since.side_effect = _REAL_FILED_BUGS_SINCE
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(**_BETA_POLICY)
        res = bugzilla_apply.autofile_bug(
            "bta-0420-aaaa-bbbb-ccccddddeeee", _BETA_INFO, {},
            {"candidate": {"node": "n"}}, "lead", 70)
        self.assertTrue(res["filed"], res.get("skipped"))

    def test_the_filed_row_reaches_the_review_corpus_with_its_channel(self):
        """`feedback._filed_bugs` is the input to the ReviewNote corpus and `_NOTE_MODES =
        ("new_bug",)` is exactly a never-comment channel's mode, so every beta filing lands in
        it. Without the channel on the row the corpus pools two populations whose filing
        policies differ, and any rate read off it describes neither."""
        uuid = "bta-0500-aaaa-bbbb-ccccddddeeee"
        self._uuid(uuid, self.beta)
        bugzilla_apply.models.Dossier.record_filed_bug.side_effect = _REAL_RECORD_FILED_BUG
        bugzilla_apply.config.get_agent_autofile.return_value = _cfg(
            **dict(_BETA_POLICY, comment_on_existing="file_new"))
        res = bugzilla_apply.autofile_bug(
            uuid, {**_BETA_INFO, "signature": self.SIG}, {},
            {"candidate": {"node": "n"}}, "lead", 70)
        self.assertTrue(res["filed"], res.get("skipped"))
        rows = [r for r in feedback._filed_bugs() if r["uuid"] == uuid]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "beta")
        self.assertEqual(rows[0]["buildid"], "20260819090452")
        self.assertEqual(rows[0]["mode"], "new_bug")
        self.assertIn(rows[0]["mode"], feedback._NOTE_MODES)


if __name__ == "__main__":
    unittest.main()
