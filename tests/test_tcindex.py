# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# THE FENIX BUILD SOURCE (plan 16 §13 D5/D6): `crashclouseau.tcindex` answers
# `buildhub`'s four questions for a product Buildhub does not carry, from the TaskCluster index.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     uv run python -m unittest tests.test_tcindex
#   CLOUSEAU_LIVE=1 ... for the one class that talks to the real index.
#
# Every fixture below is a live shape recorded 2026-09-15: the 2026-09-10 day namespace (9
# pushdate children + `latest`), its two `fenix-nightly` leaves (002721 and 214118) and seven
# `ResourceNotFound` 404s, the signing tasks' `metadata.source`/routes on central AND on
# `releases/mozilla-beta` (extra segment, double slash), Buildhub-as-firefox WITH buckets
# (214118 -> 8590488daa5e / 158.0a1) and WITHOUT (002721 is a Fenix-only push), hg-edge
# `json-pushes` at the buildid second, and `mobile/android/version.txt`.
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import json  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import pytz  # noqa: E402

from crashclouseau import buildhub, buildsource, db, hgedge, models  # noqa: E402
from crashclouseau import pushlog, tcindex, tools, utils  # noqa: E402

INDEX = tcindex.INDEX
QUEUE = tcindex.QUEUE
HG = hgedge._edge_base("nightly")

DAY_0910 = "gecko.v2.mozilla-central.pushdate.2026.09.10"
CHILDREN_0910 = [
    "20260910002721", "20260910044218", "20260910044335", "20260910085851", "20260910113828",
    "20260910143252", "20260910162643", "20260910162834", "20260910214118",
]
REV_214118 = "8590488daa5e145a97fb79b385c9f5d515271038"
REV_002721 = "920a52be5d43046ea3df130dea626f8ba9a04e97"
GIT_214118 = "88fa72d2f463129e64c2eb5c5227ef20b5c08574"
REV_BETA = "f698bd67d5a3a3614e22ee46c8c4517be99cb566"
TASK_214118 = "SDaKrPdWSoy_lk9_oP7QRg"
TASK_002721 = "VTqxMnkcTayl3N2t0uqqwQ"


def _namespaces(day_ns, names, token=None):
    payload = {"namespaces": [
        {"namespace": "{}.{}".format(day_ns, n), "name": n, "expires": "2027-09-13T00:00:00.000Z"}
        for n in names
    ]}
    if token:
        payload["continuationToken"] = token
    return payload


LEAF_404 = {
    "code": "ResourceNotFound",
    "message": "Indexed task not found\n\n---\n\n* method:     findTask\n* errorCode:  "
               "ResourceNotFound\n* statusCode: 404\n* time:       2026-09-15T18:08:24.687Z",
}


def _leaf_200(task_id, rank):
    return {"namespace": "...", "taskId": task_id, "rank": rank, "data": {},
            "expires": "2027-09-10T22:02:32.422Z"}


def _task(repo, rev, git_rev, bid, source):
    """A signing-apk task as the queue returns it: routes in the live insertion order (the git
    `revision.` route right after the hg one) and `metadata.source`."""
    day = "{}.{}.{}".format(bid[0:4], bid[4:6], bid[6:8])
    leaf = "mobile.fenix-nightly"
    return {
        "routes": [
            "index.gecko.v2.{}.latest.{}".format(repo, leaf),
            "index.gecko.v2.{}.pushdate.{}.{}.{}".format(repo, day, bid, leaf),
            "index.gecko.v2.{}.pushdate.{}.latest.{}".format(repo, day, leaf),
            "index.gecko.v2.{}.pushlog-id.45306.{}".format(repo, leaf),
            "index.gecko.v2.{}.revision.{}.{}".format(repo, rev, leaf),
            "index.gecko.v2.{}.revision.{}.{}".format(repo, git_rev, leaf),
            "index.gecko.v2.trunk.revision.{}.{}".format(rev, leaf),
            "tc-treeherder.v2.{}.{}".format(repo, rev),
        ],
        "metadata": {
            "name": "signing-apk-fenix-nightly",
            "owner": "cron@noreply.mozilla.org",
            "source": source,
            "description": "Sign Android APKs",
        },
        "extra": {"index": {"rank": int(utils.get_build_date(bid).timestamp())}},
        "created": "2026-09-10T22:02:32.422Z",
    }


CENTRAL_SOURCE = "https://hg.mozilla.org/mozilla-central/file/{}/taskcluster/kinds/signing-apk"
BETA_SOURCE = ("https://hg.mozilla.org/releases/mozilla-beta/file/{}//builds/worker/checkouts/"
               "gecko/taskcluster/kinds/signing-apk")

TASKS = {
    TASK_214118: _task("mozilla-central", REV_214118, GIT_214118, "20260910214118",
                       CENTRAL_SOURCE.format(REV_214118)),
    TASK_002721: _task("mozilla-central", REV_002721, "8db09b47689dcfc25671b178619b03ad214c6369",
                       "20260910002721", CENTRAL_SOURCE.format(REV_002721)),
}

BUILDHUB_BUCKETS = {
    # Present: one POST gives revision AND version.
    "20260910214118": {"revisions": [{"key": REV_214118, "doc_count": 6}],
                       "versions": [{"key": "158.0a1", "doc_count": 6}]},
    # Absent: a Fenix-only push, no firefox document -> empty buckets, not an error.
    "20260910002721": {"revisions": [], "versions": []},
}

PUSHES = {
    # json-pushes over (bid - 1 s, bid + 1 s): exactly the one push whose date is the buildid.
    "2026-09-10 00:27:20": {"lastpushid": 45336, "pushes": {"45292": {
        "changesets": [REV_002721], "date": 1789000041,
        "git_changesets": ["8db09b47689dcfc25671b178619b03ad214c6369"],
        "user": "ctuns@mozilla.com"}}},
    "2026-09-10 21:41:17": {"lastpushid": 45336, "pushes": {"45306": {
        "changesets": ["a5d237776d14b997905ce4bbd8caff2a51704cd8", REV_214118],
        "date": 1789076478, "user": "x@mozilla.com"}}},
    # A non-CI buildid (20260202034059): no push at that second.
    "2026-02-02 03:40:58": {"lastpushid": 45336, "pushes": {}},
}


def _resp(status, payload=None, text=None):
    # NonCallable: `FakeHTTP._answer` calls a callable handler, and a plain Mock is one.
    r = mock.NonCallableMock()
    r.status_code = status
    r.headers = {}
    if payload is not None:
        r.json.return_value = payload
        r.text = json.dumps(payload)
    else:
        r.text = text if text is not None else ""
        r.json.side_effect = ValueError("not json")
    return r


class FakeHTTP:
    """URL-keyed stand-ins for `net.get` / `net.post`. A handler is a response or a callable
    `(url, kwargs) -> response`; an URL nobody registered is a test failure, so no test can
    pass on traffic it did not mean to send."""

    def __init__(self):
        self.gets = {}
        self.posts = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._answer(self.gets, url, kw)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._answer(self.posts, url, kw)

    @staticmethod
    def _answer(table, url, kw):
        handler = table.get(url)
        if handler is None:
            raise AssertionError("unexpected request to {} {}".format(url, kw))
        return handler(url, kw) if callable(handler) else handler

    def urls(self, method):
        return [u for m, u, _ in self.calls if m == method]

    # -- the live 2026-09-10 world ---------------------------------------------------------

    def day(self, day_ns, names, token=None):
        self.posts["{}/namespaces/{}".format(INDEX, day_ns)] = _resp(
            200, _namespaces(day_ns, names, token))

    def leaf(self, day_ns, bid, task_id=None, status=None):
        url = "{}/task/{}.{}.mobile.fenix-nightly".format(INDEX, day_ns, bid)
        if task_id:
            rank = int(utils.get_build_date(bid).timestamp())
            self.gets[url] = _resp(200, _leaf_200(task_id, rank))
        elif status:
            self.gets[url] = _resp(status, {"code": "InternalServerError", "message": "boom"})
        else:
            self.gets[url] = _resp(404, LEAF_404)

    def task(self, task_id, payload):
        self.gets["{}/task/{}".format(QUEUE, task_id)] = _resp(200, payload)

    def buildhub(self, buckets=BUILDHUB_BUCKETS):
        def answer(url, kw):
            params = json.loads(kw["data"])
            terms = [f["term"] for f in params["query"]["bool"]["filter"] if "term" in f]
            bid = [t for t in terms if "build.id" in t][0]["build.id"]
            b = buckets.get(bid, {"revisions": [], "versions": []})
            return _resp(200, {"aggregations": {
                "revisions": {"buckets": b["revisions"]}, "versions": {"buckets": b["versions"]}}})
        self.posts[buildhub.URL] = answer

    def hg(self):
        def pushes(url, kw):
            return _resp(200, PUSHES[kw["params"]["startdate"]])
        self.gets["{}/json-pushes".format(HG)] = pushes
        # version.txt AS OF each revision: the 002721 push predates the 2026-09-10 bump to 158
        # (live 2026-09-15), which is exactly why the version is read at the build's own rev.
        for rev, version in ((REV_214118, "158.0a1"), (REV_002721, "157.0a1")):
            self.gets["{}/raw-file/{}/mobile/android/version.txt".format(HG, rev)] = _resp(
                200, text=version + "\n")

    def world_0910(self):
        """The 2026-09-10 day as it really is: 9 children + `latest`, leaves on 002721 and 214118,
        404s on the other seven, both tasks, Buildhub and hg-edge."""
        self.day(DAY_0910, CHILDREN_0910 + ["latest"])
        for bid in CHILDREN_0910:
            self.leaf(DAY_0910, bid)
        self.leaf(DAY_0910, "20260910214118", TASK_214118)
        self.leaf(DAY_0910, "20260910002721", TASK_002721)
        for tid, payload in TASKS.items():
            self.task(tid, payload)
        self.buildhub()
        self.hg()
        return self


def _patched(http):
    return mock.patch.multiple("crashclouseau.net", get=http.get, post=http.post)


def _bid(s):
    return utils.get_build_date(s)


class TestDayChildren(unittest.TestCase):
    def test_latest_never_becomes_a_build(self):
        """`latest` is a REAL child of the day namespace and sorts after every digit string, so
        `max()` over raw names or `!= "latest"` is not the filter -- the shape is."""
        http = FakeHTTP()
        http.day(DAY_0910, CHILDREN_0910 + ["latest"])
        with _patched(http):
            got = tcindex._day_children("mozilla-central", datetime(2026, 9, 10).date())
        self.assertEqual(got, CHILDREN_0910)
        self.assertNotIn("latest", got)

    def test_every_namespace_post_carries_a_json_body_and_follows_the_token(self):
        """A bodyless POST is a 400 MalformedPayload (live 2026-09-15). The continuation token
        goes back as the body of the next page."""
        http = FakeHTTP()
        pages = iter([
            _resp(200, _namespaces(DAY_0910, CHILDREN_0910[:5] + ["latest"], token="tok-1")),
            _resp(200, _namespaces(DAY_0910, CHILDREN_0910[5:])),
        ])
        http.posts["{}/namespaces/{}".format(INDEX, DAY_0910)] = lambda url, kw: next(pages)
        with _patched(http):
            got = tcindex._day_children("mozilla-central", datetime(2026, 9, 10).date())
        self.assertEqual(got, CHILDREN_0910)
        bodies = [kw.get("json") for m, u, kw in http.calls if m == "POST"]
        self.assertEqual(bodies, [{}, {"continuationToken": "tok-1"}])

    def test_an_empty_day_and_a_failed_day_are_both_empty_but_only_one_is_silent(self):
        http = FakeHTTP()
        http.day("gecko.v2.mozilla-central.pushdate.2026.07.11", [])
        http.posts["{}/namespaces/gecko.v2.mozilla-central.pushdate.2026.07.12".format(INDEX)] = (
            _resp(503, {"code": "ServiceUnavailable"}))
        with _patched(http), mock.patch.object(tcindex.time, "sleep") as sleep:
            with self.assertNoLogs(level="WARNING"):
                self.assertEqual(
                    tcindex._day_children("mozilla-central", datetime(2026, 7, 11).date()), [])
            with self.assertLogs(level="WARNING") as logs:
                self.assertEqual(
                    tcindex._day_children("mozilla-central", datetime(2026, 7, 12).date()), [])
        self.assertIn("503", logs.output[0])
        # The 5xx was retried once (a cold sweep probes a day only once), the 200 was not.
        sleep.assert_called_once_with(tcindex._RETRY_SLEEP)
        self.assertEqual(http.urls("POST").count(
            "{}/namespaces/gecko.v2.mozilla-central.pushdate.2026.07.12".format(INDEX)), 2)


class TestTaskRevision(unittest.TestCase):
    def test_source_regex_on_central_and_on_releases_with_the_double_slash(self):
        """The scratch regex anchored on ONE host segment and a trailing slash; beta/release's
        source has `releases/` in front and `//` behind the rev (verified live on
        bLY59bhKSYunaNyQPVtLdg and Zji82EYFQJ20I4F3EwztPw)."""
        self.assertEqual(tcindex._SOURCE_REV.search(CENTRAL_SOURCE.format(REV_214118)).group(1),
                         REV_214118)
        self.assertEqual(tcindex._SOURCE_REV.search(BETA_SOURCE.format(REV_BETA)).group(1),
                         REV_BETA)
        release = ("https://hg.mozilla.org/releases/mozilla-release/file/{}//builds/worker/"
                   "checkouts/gecko/taskcluster/kinds/signing-apk".format(REV_BETA))
        self.assertEqual(tcindex._SOURCE_REV.search(release).group(1), REV_BETA)

    def test_task_rev_cross_checks_the_treeherder_route(self):
        http = FakeHTTP()
        http.task("beta-task", _task("mozilla-beta", REV_BETA, "0" * 40, "20260914090352",
                                     BETA_SOURCE.format(REV_BETA)))
        http.task(TASK_214118, TASKS[TASK_214118])
        with _patched(http):
            self.assertEqual(tcindex._task_rev("beta-task"), REV_BETA)
            self.assertEqual(tcindex._task_rev(TASK_214118), REV_214118)

    def test_task_rev_is_none_on_disagreement_or_absence(self):
        """Half an answer is no answer: the caller falls back to the push at the buildid second."""
        http = FakeHTTP()
        disagree = _task("mozilla-central", REV_214118, GIT_214118, "20260910214118",
                         CENTRAL_SOURCE.format(REV_002721))
        no_route = _task("mozilla-central", REV_214118, GIT_214118, "20260910214118",
                         CENTRAL_SOURCE.format(REV_214118))
        no_route["routes"] = [r for r in no_route["routes"] if not r.startswith("tc-treeherder")]
        no_source = _task("mozilla-central", REV_214118, GIT_214118, "20260910214118", "")
        http.task("disagree", disagree)
        http.task("no-route", no_route)
        http.task("no-source", no_source)
        http.gets["{}/task/gone".format(QUEUE)] = _resp(404, {"code": "ResourceNotFound"})
        with _patched(http), self.assertLogs(level="WARNING"):
            for tid in ("disagree", "no-route", "no-source", "gone"):
                self.assertIsNone(tcindex._task_rev(tid), tid)


class TestGet(unittest.TestCase):
    def test_get_returns_buildhubs_exact_shape(self):
        """Keyed by product, OUR channel label, tz-aware UTC datetime; 12-char revisions; the
        version from Buildhub-as-firefox where it has the build and from version.txt where it
        does not (002721, a Fenix-only push). Seven 404 leaves yield no row and NO warning."""
        http = FakeHTTP().world_0910()
        with _patched(http), self.assertNoLogs(level="WARNING"):
            got = tcindex.get("20260910000000", "nightly", prods="Fenix",
                              max_buildid="20260910235959")
        self.assertEqual(got, {"Fenix": {"nightly": {
            _bid("20260910002721"): {"revision": "920a52be5d43", "version": "157.0a1"},
            _bid("20260910214118"): {"revision": "8590488daa5e", "version": "158.0a1"},
        }}})
        for key in got["Fenix"]["nightly"]:
            self.assertEqual(key.tzinfo.utcoffset(key).total_seconds(), 0)
        # Mirrors what `buildhub.get` hands `Build.put_data`.
        self.assertEqual(
            {k for k in got["Fenix"]["nightly"][_bid("20260910214118")]}, {"revision", "version"})
        # Exactly one leaf probe per child, one task read per leaf, one Buildhub POST per build.
        leaf_gets = [u for u in http.urls("GET") if ".mobile.fenix-nightly" in u]
        self.assertEqual(len(leaf_gets), 9)
        self.assertEqual(sorted(u for u in http.urls("GET") if u.startswith(QUEUE)),
                         sorted("{}/task/{}".format(QUEUE, t) for t in TASKS))
        self.assertEqual(http.urls("POST").count(buildhub.URL), 2)

    def test_the_version_falls_back_to_version_txt_only_when_buildhub_has_no_bucket(self):
        http = FakeHTTP().world_0910()
        with _patched(http):
            tcindex.get("20260910000000", "nightly", max_buildid="20260910235959")
        raw = [u for u in http.urls("GET") if "/raw-file/" in u]
        self.assertEqual(raw, ["{}/raw-file/{}/mobile/android/version.txt".format(HG, REV_002721)])

    def test_a_503_leaf_yields_no_row_and_a_warning(self):
        """A transient failure must never read as "no build": that answer is permanent for the
        crash (no `builds` row -> abstain) while the next tick is twenty minutes away."""
        http = FakeHTTP().world_0910()
        http.leaf(DAY_0910, "20260910214118", status=503)
        with _patched(http), mock.patch.object(tcindex.time, "sleep") as sleep, \
                self.assertLogs(level="WARNING") as logs:
            got = tcindex.get("20260910000000", "nightly", max_buildid="20260910235959")
        self.assertEqual(list(got["Fenix"]["nightly"]), [_bid("20260910002721")])
        self.assertTrue(any("503" in line and "20260910214118" in line for line in logs.output),
                        logs.output)
        # Retried once, then given up on for this tick; the 404s were not retried.
        sleep.assert_called_once()
        leaf_gets = [u for u in http.urls("GET") if ".mobile.fenix-nightly" in u]
        self.assertEqual(len(leaf_gets), 10)

    def test_a_datetime_lower_bound_and_a_list_of_products_as_update_builds_passes_them(self):
        http = FakeHTTP().world_0910()
        with _patched(http):
            got = tcindex.get(_bid("20260910120000"), "nightly", prods=["Fenix"],
                              max_buildid=_bid("20260910235959"))
        self.assertEqual(list(got["Fenix"]["nightly"]), [_bid("20260910214118")])
        # Children below the bound are not even probed.
        self.assertFalse(any("20260910002721" in u for u in http.urls("GET")))

    def test_the_buildhub_revision_is_compared_with_the_tasks(self):
        http = FakeHTTP().world_0910()
        http.buildhub({"20260910214118": {"revisions": [{"key": REV_002721}],
                                          "versions": [{"key": "158.0a1"}]}})
        with _patched(http), self.assertLogs(level="WARNING") as logs:
            got = tcindex.get("20260910200000", "nightly", max_buildid="20260910235959")
        # The task's revision wins; the disagreement is on record.
        self.assertEqual(got["Fenix"]["nightly"][_bid("20260910214118")]["revision"],
                         "8590488daa5e")
        self.assertTrue(any("differs" in line for line in logs.output), logs.output)

    def test_a_build_whose_revision_cannot_be_resolved_is_skipped_not_raised(self):
        http = FakeHTTP().world_0910()
        broken = dict(TASKS[TASK_214118])
        broken["metadata"] = {"source": ""}
        http.task(TASK_214118, broken)
        http.gets["{}/json-pushes".format(HG)] = lambda url, kw: _resp(
            200, {"lastpushid": 1, "pushes": {}})
        with _patched(http), self.assertLogs(level="WARNING") as logs:
            got = tcindex.get("20260910200000", "nightly", max_buildid="20260910235959")
        self.assertEqual(got, {})
        self.assertTrue(any("skipped" in line for line in logs.output), logs.output)

    def test_a_task_miss_falls_back_to_the_push_at_the_buildid_second(self):
        http = FakeHTTP().world_0910()
        http.gets["{}/task/{}".format(QUEUE, TASK_214118)] = _resp(
            404, {"code": "ResourceNotFound"})
        with _patched(http):
            got = tcindex.get("20260910200000", "nightly", max_buildid="20260910235959")
        self.assertEqual(got["Fenix"]["nightly"][_bid("20260910214118")]["revision"],
                         "8590488daa5e")
        (_, url, kw), = [c for c in http.calls if c[1].endswith("/json-pushes")]
        self.assertTrue(url.startswith("https://hg-edge.mozilla.org/"), url)
        self.assertEqual(kw["params"], {"version": 2, "startdate": "2026-09-10 21:41:17",
                                        "enddate": "2026-09-10 21:41:19"})

    def test_an_unserved_channel_or_product_returns_nothing_without_a_request(self):
        http = FakeHTTP()
        with _patched(http):
            self.assertEqual(tcindex.get("20260910000000", "beta", prods="Fenix",
                                         max_buildid="20260910235959"), {})
            self.assertEqual(tcindex.get("20260910000000", "nightly", prods="Firefox",
                                         max_buildid="20260910235959"), {})
            self.assertIsNone(tcindex.get_rev_from("20260910214118", "beta", "Fenix"))
            self.assertIsNone(tcindex.get_two_last("20260910214118", "release", "Fenix"))
        self.assertEqual(http.calls, [])


class TestGetRevFrom(unittest.TestCase):
    def test_buildhub_first_then_the_leaf_then_the_push(self):
        http = FakeHTTP().world_0910()
        with _patched(http):
            # Buildhub has 214118: one POST, no TC traffic.
            self.assertEqual(tcindex.get_rev_from("20260910214118", "nightly", "Fenix"),
                             "8590488daa5e")
            self.assertEqual(http.urls("GET"), [])
            # 002721 is Fenix-only: Buildhub misses, the leaf + task answer.
            self.assertEqual(tcindex.get_rev_from(_bid("20260910002721"), "nightly", "Fenix"),
                             "920a52be5d43")
            self.assertFalse(any(u.endswith("/json-pushes") for u in http.urls("GET")))
            # Leaf gone too: the push at the buildid second.
            http.leaf(DAY_0910, "20260910002721")
            self.assertEqual(tcindex.get_rev_from("20260910002721", "nightly", "Fenix"),
                             "920a52be5d43")
            self.assertTrue(any(u.endswith("/json-pushes") for u in http.urls("GET")))

    def test_a_non_ci_buildid_is_none(self):
        """20260202034059: not a child of its day, no leaf, no push at that second."""
        http = FakeHTTP()
        http.buildhub()
        http.leaf("gecko.v2.mozilla-central.pushdate.2026.02.02", "20260202034059")
        http.hg()
        with _patched(http):
            self.assertIsNone(tcindex.get_rev_from("20260202034059", "nightly", "Fenix"))


class TestGetTwoLast(unittest.TestCase):
    ROWS = [
        {"buildid": "20260910002721", "revision": "920a52be5d43", "version": "157.0a1"},
        {"buildid": "20260910214118", "revision": "8590488daa5e", "version": "158.0a1"},
    ]

    def test_the_table_answers_first(self):
        http = FakeHTTP()
        with _patched(http), mock.patch.object(models.Build, "get_two_last",
                                               return_value=list(self.ROWS)) as table:
            got = tcindex.get_two_last("20260910214118", "nightly", "Fenix")
        self.assertEqual(got, self.ROWS)
        table.assert_called_once_with(_bid("20260910214118"), "nightly", "Fenix")
        self.assertEqual(http.calls, [])

    def test_a_table_that_lacks_the_asked_build_does_not_answer(self):
        """`Build.get_two_last` is `<=`: with the asked build missing it returns the two builds
        BEFORE it, which would silently bound the window at the wrong place."""
        http = FakeHTTP().world_0910()
        stale = [{"buildid": "20260909090000", "revision": "a" * 12, "version": "158.0a1"},
                 self.ROWS[0]]
        with _patched(http), mock.patch.object(models.Build, "get_two_last", return_value=stale):
            got = tcindex.get_two_last("20260910214118", "nightly", "Fenix")
        self.assertEqual(got, self.ROWS)

    def test_the_walk_back_crosses_an_empty_day(self):
        """2026-07-11 was a Saturday with no push: a 200 with no namespaces. The predecessor of
        the first build after it is the last build before it, not None."""
        http = FakeHTTP().world_0910()
        day_12 = "gecko.v2.mozilla-central.pushdate.2026.09.12"
        rev_12 = "c" * 40
        http.day(day_12, ["20260912090000", "20260912070000", "latest"])
        http.leaf(day_12, "20260912070000")
        http.leaf(day_12, "20260912090000", "task-12")
        http.task("task-12", _task("mozilla-central", rev_12, "d" * 40, "20260912090000",
                                   CENTRAL_SOURCE.format(rev_12)))
        http.day("gecko.v2.mozilla-central.pushdate.2026.09.11", [])
        http.buildhub(dict(BUILDHUB_BUCKETS, **{"20260912090000": {
            "revisions": [{"key": rev_12}], "versions": [{"key": "158.0a1"}]}}))
        with _patched(http), mock.patch.object(models.Build, "get_two_last", return_value=[]):
            got = tcindex.get_two_last("20260912090000", "nightly", "Fenix")
        self.assertEqual(got, [
            {"buildid": "20260910214118", "revision": "8590488daa5e", "version": "158.0a1"},
            {"buildid": "20260912090000", "revision": "c" * 12, "version": "158.0a1"},
        ])
        # The same-day earlier child was probed and found leafless, the empty day was read,
        # and the walk stopped at the first leaf on 09-10 (the 404 children were not probed).
        leaf_gets = [u for u in http.urls("GET") if ".mobile.fenix-nightly" in u]
        self.assertIn("{}/task/{}.20260912070000.mobile.fenix-nightly".format(INDEX, day_12),
                      leaf_gets)
        self.assertFalse(any("20260910162834" in u for u in leaf_gets))
        self.assertIn("{}/namespaces/gecko.v2.mozilla-central.pushdate.2026.09.11".format(INDEX),
                      http.urls("POST"))
        for m, u, kw in http.calls:
            if m == "POST" and "/namespaces/" in u:
                self.assertEqual(kw.get("json"), {}, u)

    def test_no_leaf_for_the_asked_build_or_no_predecessor_is_none(self):
        http = FakeHTTP().world_0910()
        with _patched(http), mock.patch.object(models.Build, "get_two_last", return_value=[]):
            # 162834 is a child without a shipped APK.
            self.assertIsNone(tcindex.get_two_last("20260910162834", "nightly", "Fenix"))
            # 002721 is the first build of its day; every earlier day is empty.
            for k in range(1, tcindex._MAX_WALKBACK_DAYS + 1):
                d = datetime(2026, 9, 10) - timedelta(days=k)
                http.day("gecko.v2.mozilla-central.pushdate.{:%Y.%m.%d}".format(d), [])
            self.assertIsNone(tcindex.get_two_last("20260910002721", "nightly", "Fenix"))


class TestEnclosingBuildsFromTheTable(unittest.TestCase):
    """`get_enclosing_builds` reads the `builds` table ONLY (its consumer is a UI link). sqlite:
    the three tables it joins, seeded through the model constructors."""

    @classmethod
    def setUpClass(cls):
        for t in (models.HGAuthor, models.Node, models.Build):
            t.__table__.create(db.engine, checkfirst=True)

    def setUp(self):
        self._nodes = []
        with mock.patch.object(models.HGAuthor, "get_id", return_value=None):
            for bid, rev in (("20260910002721", "920a52be5d43"),
                             ("20260910214118", "8590488daa5e")):
                node = models.Node("nightly", {"node": rev, "date": _bid(bid), "backedout": False,
                                               "merge": False, "bug": -1, "author": None})
                db.session.add(node)
                db.session.commit()
                self._nodes.append(node)
                models.Build.put_build(_bid(bid), node.id, "Fenix", "nightly", "158.0a1")

    def tearDown(self):
        db.session.query(models.Build).filter(models.Build.product == "Fenix").delete()
        db.session.query(models.Node).filter(
            models.Node.id.in_([n.id for n in self._nodes])).delete(synchronize_session=False)
        db.session.commit()

    def test_before_and_after_between_two_builds(self):
        http = FakeHTTP()
        with _patched(http):
            got = tcindex.get_enclosing_builds(_bid("20260910120000"), "nightly", "Fenix")
        self.assertEqual(got, [
            {"buildid": "20260910002721", "revision": "920a52be5d43", "version": "158.0a1"},
            {"buildid": "20260910214118", "revision": "8590488daa5e", "version": "158.0a1"},
        ])
        self.assertEqual(http.calls, [])

    def test_the_edges_are_none_and_the_url_helper_survives_them(self):
        self.assertEqual(
            tcindex.get_enclosing_builds("20260909000000", "nightly", "Fenix")[0], None)
        self.assertEqual(
            tcindex.get_enclosing_builds("20260911000000", "nightly", "Fenix")[1], None)
        # `>=`: a pushdate that IS a build is its own `after`.
        self.assertEqual(
            tcindex.get_enclosing_builds("20260910214118", "nightly", "Fenix")[1]["buildid"],
            "20260910214118")
        self.assertIsNone(pushlog.pushlog_for_pushdate_url("20260909000000", "nightly", "Fenix"))
        url = pushlog.pushlog_for_pushdate_url("20260911000000", "nightly", "Fenix")
        self.assertTrue(url.endswith("fromchange=8590488daa5e&tochange=tip"), url)
        # Another product's rows are not this product's builds.
        self.assertEqual(tcindex.get_enclosing_builds("20260910120000", "nightly", "Firefox"),
                         [None, None])


class TestDispatch(unittest.TestCase):
    def test_for_product(self):
        self.assertIs(buildsource.for_product("Firefox"), buildhub)
        self.assertIs(buildsource.for_product("Thunderbird"), buildhub)
        self.assertIs(buildsource.for_product("Fenix"), tcindex)
        for name in ("get", "get_rev_from", "get_two_last", "get_enclosing_builds"):
            self.assertTrue(callable(getattr(tcindex, name)), name)

    def test_tools_get_changeset_never_asks_socorro_for_a_tc_product(self):
        """`datacollector.get_changeset` votes over `hg:hg.mozilla.org/` frames; a Fenix
        report's frames are `git:github.com/...`, so the rung is a paid-for None."""
        with mock.patch.object(models.Build, "get_changeset", return_value=None), \
                mock.patch.object(tcindex, "get_rev_from", return_value=None) as tc, \
                mock.patch.object(buildhub, "get_rev_from", return_value=None) as bh, \
                mock.patch.object(tools.datacollector, "get_changeset",
                                  return_value="deadbeef0000") as vote:
            self.assertIsNone(tools.get_changeset("20260910214118", "nightly", "Fenix"))
            tc.assert_called_once_with("20260910214118", "nightly", "Fenix")
            bh.assert_not_called()
            vote.assert_not_called()
            # Firefox: the identical Buildhub function, then the vote, as before.
            self.assertEqual(tools.get_changeset("20260910214118", "nightly", "Firefox"),
                             "deadbeef0000")
            bh.assert_called_once_with("20260910214118", "nightly", "Firefox")
            vote.assert_called_once_with("20260910214118", "nightly", "Firefox")

    def test_pushlog_helpers_dispatch_on_the_product(self):
        rows = [{"buildid": "20260910002721", "revision": "920a52be5d43", "version": "158.0a1"},
                {"buildid": "20260910214118", "revision": "8590488daa5e", "version": "158.0a1"}]
        with mock.patch.object(tcindex, "get_two_last", return_value=rows) as tc, \
                mock.patch.object(buildhub, "get_two_last", return_value=rows) as bh, \
                mock.patch.object(pushlog, "pushlog_for_revs", return_value=[]) as revs:
            pushlog.pushlog_for_buildid("20260910214118", "nightly", "Fenix")
            tc.assert_called_once_with("20260910214118", "nightly", "Fenix")
            bh.assert_not_called()
            pushlog.pushlog_for_buildid("20260910214118", "nightly", "Firefox")
            bh.assert_called_once_with("20260910214118", "nightly", "Firefox")
            self.assertEqual(revs.call_count, 2)
            self.assertEqual(revs.call_args.args[:2], ("920a52be5d43", "8590488daa5e"))
        # The URL helper runs on the web dyno (one gunicorn worker, 30 s router timeout): a
        # TC product answers from the table only -- the index walk-back (up to 15 namespace
        # POSTs + a leaf GET per child) is a worker-side cost, never a page's.
        with mock.patch.object(models.Build, "get_two_last", return_value=[]), \
                mock.patch.object(tcindex, "get_two_last", return_value=rows) as tc, \
                mock.patch.object(buildhub, "get_two_last") as bh:
            self.assertIsNone(pushlog.pushlog_for_buildid_url("20260910214118", "nightly",
                                                              "Fenix"))
            tc.assert_not_called()
            bh.assert_not_called()
        with mock.patch.object(models.Build, "get_two_last", return_value=rows), \
                mock.patch.object(tcindex, "get_two_last") as tc:
            url = pushlog.pushlog_for_buildid_url("20260910214118", "nightly", "Fenix")
            self.assertIn("fromchange=920a52be5d43&tochange=8590488daa5e", url)
            tc.assert_not_called()
        with mock.patch.object(models.Build, "get_two_last", return_value=[]), \
                mock.patch.object(buildhub, "get_two_last", return_value=rows) as bh:
            url = pushlog.pushlog_for_buildid_url("20260910214118", "nightly", "Firefox")
            self.assertIn("fromchange=920a52be5d43&tochange=8590488daa5e", url)
            bh.assert_called_once_with("20260910214118", "nightly", "Firefox")
        with mock.patch.object(buildhub, "get_enclosing_builds", return_value=[None, None]) as bh:
            self.assertIsNone(pushlog.pushlog_for_pushdate_url("20260910120000", "nightly",
                                                               "Firefox"))
            bh.assert_called_once()


@unittest.skipUnless(os.environ.get("CLOUSEAU_LIVE") == "1", "set CLOUSEAU_LIVE=1 for the index")
class TestLiveIndex(unittest.TestCase):
    """The real index, queue, Buildhub and hg-edge. Pins the 2026-09-10 facts the fixtures
    above were recorded from; the index keeps a day ~1 year (`expires` 2027-09-13)."""

    def test_20260910214118_resolves_to_8590488daa5e_158_0a1(self):
        self.assertEqual(tcindex.get_rev_from("20260910214118", "nightly", "Fenix"),
                         "8590488daa5e")
        got = tcindex.get("20260910214118", "nightly", prods="Fenix",
                          max_buildid="20260910214118")
        self.assertEqual(got, {"Fenix": {"nightly": {
            _bid("20260910214118"): {"revision": "8590488daa5e", "version": "158.0a1"}}}})

    def test_the_day_has_two_shipped_builds_of_nine_children(self):
        got = tcindex.get("20260910000000", "nightly", max_buildid="20260910235959")
        self.assertEqual(sorted(utils.get_buildid(k) for k in got["Fenix"]["nightly"]),
                         ["20260910002721", "20260910214118"])
        # A Fenix-only push (no Buildhub document) whose version.txt still says 157: the
        # 2026-09-10 bump to 158 landed later that day.
        self.assertEqual(got["Fenix"]["nightly"][_bid("20260910002721")],
                         {"revision": "920a52be5d43", "version": "157.0a1"})
        day = datetime(2026, 9, 10, tzinfo=pytz.utc).date()
        self.assertEqual(tcindex._day_children("mozilla-central", day), CHILDREN_0910)


if __name__ == "__main__":
    unittest.main()
