# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# TWO CLOCKS ON A RELEASE BRANCH, and only one of them is a landing date (plan #18 item 12).
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     uv run python -m unittest tests.test_beta_pushdate
#
# Every landing date in the pipeline comes from the push record in the crash's OWN repo
# (`pushlog.collect` stamps `pushdate = push["date"]` on every changeset; `sigage.json_rev` reads
# `json-rev/<node>` on `Mercurial.get_repo_url(channel)`) -- and a central->beta merge is ONE push
# carrying a whole development cycle, so on beta every merged changeset reported the MERGE date.
#
# THE MEASUREMENT (plan #18 §1.4 / §7 contradiction 4, taken live 2026-08-25). Four sampled
# non-merge members of beta push 27990 -- `f44045181a24`, `7f4d7e8c27d6`, `2c98bfc534ef`,
# `02297ff55cd2` -- all report beta pushdate `2026-08-13T14:15:59` against central
# `2026-07-21T09:46:48`: a 23.2-day forward shift, IDENTICAL across the push, because the push is
# the merge. Six sampled members of push 27533 (6,917 changesets, all stamped
# `2026-07-20T17:14:53`) give central dates from 06-15 to 07-13 -- a drift of 6.9 to 34.8 days
# depending on where in the cycle the change landed. So the error is not a constant offset that
# could be subtracted; it is "when in the cycle did this land", which only the origin repo knows.
#
# WHY THE ORIGIN REPO CAN ANSWER: 1,932 of 1,932 merge-window beta candidates have a node hash
# that also exists in the mozilla-central pushlog (5,116 of 5,124 non-merge = 99.8%), and both
# repos serve byte-identical `raw-rev`. The complement is the UPLIFT, where the fallback is not a
# degradation but the right answer: an uplift is an hg graft with a NEW hash, so 0 of 1,009
# candidate-bearing in-cycle beta changesets exist on m-c under the same hash, and there the beta
# push date IS the landing date.
#
# Five consumers read this number and every one wants "when did this code first exist". This file
# pins the two that changed (`sigage.pushdate_for_node`, `bugzilla_apply._candidate_landed`), the
# two that must NOT have moved to central (`backedout_by_for_node` / `same_push_backout_target`
# ask what happened to the changeset ON THIS BRANCH -- a different question), the cost on nightly,
# and -- as expected failures -- the two consumers the fix does not reach.
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, sigage  # noqa: E402
from crashclouseau.agent import orchestrator as orch, triage  # noqa: E402
from crashclouseau.agent.schema import (  # noqa: E402
    Candidate,
    Confidence,
    Decision,
    Dossier,
    Verdict,
)

# The measured pair, written as the dates rather than as epochs so a reader can check them
# against §1.4. hg's `json-rev` returns `pushdate` as `[epoch, tzoffset]`.
_CENTRAL_ISO = "2026-07-21T09:46:48"
_BETA_ISO = "2026-08-13T14:15:59"


def _hg_pushdate(iso):
    return [int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()), 0]


CENTRAL = _hg_pushdate(_CENTRAL_ISO)
BETA = _hg_pushdate(_BETA_ISO)

# A non-merge member of beta push 27990, and the one the plan sampled first.
NODE = "f44045181a24"
_FULL = NODE + "0123456789abcdef0123456789abcdef"

_CENTRAL_URL = "/mozilla-central/"
_BETA_URL = "/releases/mozilla-beta/"


def _hg(central=None, beta=None):
    """A `crashclouseau.net.get` stand-in that answers as each repo really did: whatever
    `central`/`beta` say, keyed on the repo in the URL. `None` = that repo 404s (`raise_for_status`
    raises, which is how `json_rev` learns "this repo has never heard of this node")."""
    def get(url, **kw):
        payload = beta if _BETA_URL in url else central
        r = mock.Mock()
        if payload is None:
            r.raise_for_status.side_effect = RuntimeError("404 not found: {}".format(url))
        r.json.return_value = payload or {}
        return r
    return get


def _rev(pushdate, **extra):
    return {"node": _FULL, "pushdate": pushdate, **extra}


class _CacheClearing(unittest.TestCase):
    def setUp(self):
        # Both caches are module-global and keyed on (node, channel); a leftover entry from
        # another test would make a request count meaningless.
        sigage._JSON_REV_CACHE.clear()
        sigage._PUSH_CACHE.clear()
        self.addCleanup(sigage._JSON_REV_CACHE.clear)
        self.addCleanup(sigage._PUSH_CACHE.clear)


class TestLandingPair(_CacheClearing):
    """`sigage.landing_pair` / `pushdate_for_node` on a channel that is not the origin."""

    def test_the_origin_pushdate_wins_when_central_knows_the_node(self):
        # The whole point: 2026-07-21 is when this code was written, 2026-08-13 is when the
        # branch received it. Anything asking "can this changeset be the origin of a crash"
        # wants the first.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL), beta=_rev(BETA))) as get:
            self.assertEqual(sigage.pushdate_for_node(NODE, "beta"), CENTRAL)
            urls = [c[0][0] for c in get.call_args_list]
            # One extra json-rev, and only one: the endpoint is measured at 8-13 s, so a second
            # ask for the same node must come out of the cache.
            self.assertEqual(len(urls), 2)
            self.assertEqual(sigage.pushdate_for_node(NODE, "beta"), CENTRAL)
            self.assertEqual(get.call_count, 2)
        self.assertTrue(any(_BETA_URL in u for u in urls), urls)
        self.assertTrue(any(_CENTRAL_URL in u for u in urls), urls)

    def test_an_uplift_falls_back_to_the_channel_s_own_date(self):
        # A beta uplift is an hg graft with a NEW hash, so central has never heard of it: 0 of
        # 1,009 candidate-bearing in-cycle beta changesets exist on m-c under the same hash. The
        # beta push date is then not a merge date at all -- it is the landing date, and losing it
        # would blind the stale gate and the venue window on the whole in-cycle population.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=None, beta=_rev(BETA))):
            self.assertEqual(sigage.landing_pair(NODE, "beta"), (None, BETA))
            self.assertEqual(sigage.pushdate_for_node(NODE, "beta"), BETA)

    def test_an_unreachable_origin_repo_degrades_to_the_channel_date(self):
        # hg is not distinguishable from "central does not know it" -- `json_rev` swallows every
        # failure and NEGATIVE-caches `{}`. So an hg 406 makes a merged changeset look like an
        # uplift and returns the merge date: the pre-fix answer, i.e. it degrades rather than
        # losing the date or raising on the filing path.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=None, beta=_rev(BETA))):
            self.assertEqual(sigage.pushdate_for_node(NODE, "beta"), BETA)
        # And neither repo knowing it is still `None`, not an exception.
        sigage._JSON_REV_CACHE.clear()
        with mock.patch("crashclouseau.net.get", side_effect=_hg(central=None, beta=None)):
            self.assertEqual(sigage.landing_pair(NODE, "beta"), (None, None))
            self.assertIsNone(sigage.pushdate_for_node(NODE, "beta"))

    def test_both_dates_are_available_to_a_caller(self):
        # `landing_pair` returns the pair, in that order, so a prompt or a bug comment can say
        # "landed on central 2026-07-21, reached beta 2026-08-13" instead of picking one and
        # calling it "landed". Both must survive as parseable dates, not just as truthy values.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL), beta=_rev(BETA))):
            origin, own = sigage.landing_pair(NODE, "beta")
        origin_dt, own_dt = sigage.to_datetime(origin), sigage.to_datetime(own)
        self.assertEqual(origin_dt.date().isoformat(), "2026-07-21")
        self.assertEqual(own_dt.date().isoformat(), "2026-08-13")
        self.assertLess(origin_dt, own_dt)
        # The measured shift for push 27990. Not a constant to subtract -- push 27533's members
        # run 6.9 to 34.8 days -- which is why both dates have to be carried, not one plus an
        # offset.
        self.assertEqual(round((own_dt - origin_dt).total_seconds() / 86400.0, 1), 23.2)

    def test_nightly_is_one_request_and_unchanged(self):
        # On nightly the two clocks are the same repo, so the pair must be FREE: mozilla-central
        # is both the origin and the channel. This is the regression test for the 100% of prod
        # traffic that is not beta -- a second json-rev here would put 8-13 s on every run.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL), beta=_rev(BETA))) as get:
            self.assertEqual(sigage.landing_pair(NODE, "nightly"), (CENTRAL, CENTRAL))
            self.assertEqual(sigage.pushdate_for_node(NODE, "nightly"), CENTRAL)
        self.assertEqual(get.call_count, 1)
        self.assertIn(_CENTRAL_URL, get.call_args_list[0][0][0])

    def test_an_absent_channel_asks_nobody(self):
        # An empty channel has no repo (`Mercurial.get_repo_url("")` yields a nonexistent
        # `releases/mozilla-`), and the answer stays `None` rather than quietly becoming
        # mozilla-central's: inventing a landing date for a channel we were not told is worse
        # than having none, because `None` is handled everywhere and a wrong date is not.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL), beta=_rev(BETA))) as get:
            self.assertEqual(sigage.landing_pair(NODE, ""), (None, None))
            self.assertIsNone(sigage.pushdate_for_node(NODE, ""))
        get.assert_not_called()


class TestTheBackoutLookupsStayOnTheBranch(_CacheClearing):
    """`backedout_by_for_node` / `same_push_backout_target` ask a DIFFERENT question -- what
    happened to this changeset on THIS branch -- so moving them to central would be wrong, and
    silently so: a changeset backed out on beta only (the ordinary shape of a beta backout, since
    the beta backout is its own graft) reads as clean on central, and a clean read un-suppresses
    a candidate the backout gate exists to suppress."""

    def test_the_backout_lookups_still_read_the_channel_repo(self):
        backout_desc = "Backed out changeset 1111aaaa2222 (bug 1900000) for causing failures"
        member = "1111aaaa2222333344445555666677778888aaaa"
        beta_rev = _rev(BETA, backedoutby="beefdead1234", desc=backout_desc)
        # Central's copy of the same node is CLEAN and has a different description -- which is
        # exactly what a beta-only backout looks like from central.
        central_rev = _rev(CENTRAL, desc="Bug 1900000 - do a thing")

        def get(url, **kw):
            r = mock.Mock()
            if "json-pushes" in url:
                r.json.return_value = {"pushes": {"27990": {"changesets": [{"node": member},
                                                                           {"node": _FULL}]}}}
            else:
                r.json.return_value = beta_rev if _BETA_URL in url else central_rev
            return r

        with mock.patch("crashclouseau.net.get", side_effect=get) as g:
            self.assertEqual(sigage.backedout_by_for_node(NODE, "beta"), "beefdead1234")
            self.assertEqual(sigage.desc_for_node(NODE, "beta"), backout_desc)
            self.assertEqual(sigage.same_push_backout_target(NODE, "beta"), member)
            urls = [c[0][0] for c in g.call_args_list]
        # Not one request to the origin repo on this path: these three answers are the branch's.
        self.assertEqual([u for u in urls if _CENTRAL_URL in u], [])
        self.assertTrue(any("json-pushes" in u and _BETA_URL in u for u in urls), urls)

    def test_a_backout_that_only_central_knows_about_is_not_this_branchs_answer(self):
        # The mirror, and the one that would break if someone "unified" the repo choice: central
        # says the changeset was backed out (on trunk, later, or by a different sheriff), beta
        # says it was not. The candidate is live in the beta build being triaged, so "" is right.
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL, backedoutby="deadbeef9999"),
                                        beta=_rev(BETA))):
            self.assertEqual(sigage.backedout_by_for_node(NODE, "beta"), "")


class TestTheFilersClock(_CacheClearing):
    """`bugzilla_apply._candidate_landed` -> `_bug_for_this_regression`'s
    `comment_max_bug_age_days` = 30 window (plan #18 §4)."""

    def test_the_filers_landing_date_follows(self):
        dossier = {"candidate": {"node": NODE}}
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL), beta=_rev(BETA))):
            landed = bugzilla_apply._candidate_landed(dossier, "beta")
        self.assertEqual(landed, sigage.to_datetime(CENTRAL))
        self.assertNotEqual(landed, sigage.to_datetime(BETA))
        # And the channel really is threaded through, not defaulted: on an uplift (central 404s)
        # the beta date has to come back. A caller that dropped the channel would answer CENTRAL
        # for the merged node above and `None` here -- and `None` is no longer neutral on the
        # filing path, it costs every open bug its venue (`_bug_for_this_regression`).
        sigage._JSON_REV_CACHE.clear()
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=None, beta=_rev(BETA))):
            self.assertEqual(bugzilla_apply._candidate_landed(dossier, "beta"),
                             sigage.to_datetime(BETA))

    def test_the_venue_window_swings_by_23_days_on_that_one_value(self):
        # WHICH WAY IT SWINGS, measured rather than argued, because both `pushdate_for_node`'s
        # docstring and plan #18 item 12 state the opposite direction ("errs toward accepting an
        # OLD bug as this crash's venue") -- see `defects_found`. The predicate is
        # `landed - created <= max_age_days`, so a clock 23.2 days LATE makes the gap BIGGER and
        # REJECTS venues: bug 1798397 open since 2026-07-01 is 20.4 days older than the cause on
        # the central clock (a venue, so we comment) and 43.6 days older on the beta clock (not a
        # venue, so we file a new bug past it). The direction the late clock errs in is DUPLICATE,
        # not stale-venue.
        bug = {"id": 1798397, "creation_time": "2026-07-01T00:00:00Z",
               "product": "Core", "keywords": []}
        with mock.patch.object(bugzilla_apply, "_last_reopened", return_value=None):
            central = bugzilla_apply._bug_for_this_regression(
                [bug], sigage.to_datetime(CENTRAL), 30)
            beta = bugzilla_apply._bug_for_this_regression(
                [bug], sigage.to_datetime(BETA), 30)
        self.assertEqual(central, (1798397, []))
        self.assertEqual(beta, (None, [1798397]))


class TestTheConsumersTheFixDoesNotReach(_CacheClearing):
    """The other two consumers item 12 names. Both read the pushdate the SEED already carries --
    `pushlog.collect`'s `push["date"]` in the channel's own repo, i.e. the merge date -- and
    `pushdate_for_node` is only consulted when that map has no entry. So the origin clock reaches
    neither on the ordinary path. Both are FIXED now -- the gate by resolving the origin date for
    the chosen candidate, the prompt by refusing to call a merge arrival a landing date."""

    _MERGE_DAY = datetime(2026, 8, 13, 14, 15, 59, tzinfo=timezone.utc)
    _FIRST_SEEN = "20260801000000"          # 2026-08-01, i.e. AFTER the code was written

    def _beta_seed(self):
        return {"uuid": "u-beta", "signature": "S", "channel": "beta", "is_offstack": True,
                "signature_first_seen_buildid": self._FIRST_SEEN,
                "candidate_pushdates": {NODE: self._MERGE_DAY}}

    def test_the_stale_gates_own_clock_follows_too(self):
        # The 4%-of-beta-selections class §7 contradiction 4 says item 12 exists for: "new on
        # beta, long-lived on nightly", 3 of 77 emulated selections, where the beta clock gives
        # +14 to +766 days (the gate fires) and the central clock gives a negative (silent).
        # Here: signature first seen 2026-08-01, code written 2026-07-21 (-10.4 d, so this
        # candidate CAN be the origin), merge arrival 2026-08-13 (+12.6 d, past
        # `min_age_days` = 7). The gate reads the seed map first, so it clamps a probable lead to
        # medium on a changeset whose own repo says it predates the crash -- and
        # `pushdate_for_node`, the only thing that knows about the origin repo, was called 0
        # times. FIXED: off nightly the gate now prefers the origin pushdate over the seeded
        # arrival date, at the cost of ONE hg lookup for the ONE chosen candidate (cached, and
        # usually already warm from the backout gate and the git-commit link).
        d = Dossier(candidate=Candidate(node=NODE, bug=1900000),
                    verdict=Verdict(decision=Decision.lead, confidence=Confidence.probable,
                                    needinfo_draft="could you take a look?"))
        with mock.patch("crashclouseau.net.get",
                        side_effect=_hg(central=_rev(CENTRAL), beta=_rev(BETA))):
            orch._apply_signature_age_gate(d, self._beta_seed())
        self.assertEqual(d.verdict.confidence, Confidence.probable)
        self.assertNotIn("stale_signature", d.corroborations or {})

    def test_the_prompt_does_not_print_the_merge_date_as_the_landing_date(self):
        # `landed=` is built straight from the seeded candidate's `pushdate`
        # (`triage.py` candidate list), so on beta it renders `landed=2026-08-13` for a changeset
        # written on 2026-07-21 -- measured on this fixture. §7 contradiction 4's own words for
        # the second half of item 12: "the printed `landed=` date is simply false".
        #
        # `_NEW_TO_CHANNEL_GUIDANCE` (item 15) does warn the model that these dates are arrival
        # dates -- but only in the "new on beta, old everywhere" branch, which needs
        # `signature_first_seen_channel` and a channel age within `NEW_SIGNATURE_DAYS`. The 84% of
        # beta selections that sit on a long-lived signature (median beta first-seen 393 d) get
        # the merge date with no caveat at all, which is the crash shaped like this fixture.
        crash = {
            "uuid": "u-beta", "signature": "mozilla::dom::Foo::Bar", "channel": "beta",
            "product": "Firefox", "buildid": "20260817142839", "version": "155.0b3",
            "raw_crash": {"reason": "EXCEPTION_ACCESS_VIOLATION_READ",
                          "json_dump": {"crash_info": {
                              "type": "EXCEPTION_ACCESS_VIOLATION_READ",
                              "address": "0x0", "crashing_thread": 0}}},
            "is_offstack": True,
            "candidates": [{"node": NODE, "score": None, "bug": 1900000,
                            "pushdate": self._MERGE_DAY, "via_merge": True,
                            "desc": "Bug 1900000 - do a thing"}],
        }
        prompt = triage._user_prompt(crash)
        self.assertIn(NODE, prompt)
        # FIXED BY LABELLING, NOT BY LOOKING UP, and that is a decision rather than a
        # weakening. The true landing date needs one hg `json-rev` per candidate, and an
        # off-stack merge window is up to `agent.offstack.max_candidates` = 150 of them at
        # 8-13 s each — so resolving them all is not affordable, while the ONE chosen candidate
        # DOES get the lookup (`orchestrator._apply_signature_age_gate`, the sibling test
        # above). What the model must not be told is a falsehood, and `landed=<merge date>` is
        # one; naming it as an arrival is free and is strictly more information than the date
        # alone, because it also says the code was written earlier and on trunk.
        self.assertNotIn("landed=2026-08-13", prompt)
        self.assertIn("arrived-with-the-cycle-merge=2026-08-13", prompt)
        self.assertIn("NOT its landing date", prompt)
        # An ordinary uplift is still a landing, and must still read as one.
        uplift = dict(crash, candidates=[dict(crash["candidates"][0], via_merge=False)])
        self.assertIn("landed=2026-08-13", triage._user_prompt(uplift))


if __name__ == "__main__":
    unittest.main()
