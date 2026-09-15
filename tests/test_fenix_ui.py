# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Fenix nightly in the UI and the trigger API (plans/16): a second PRODUCT on the channel
label ``nightly``, so every product-blind view here needed to learn the product -- the tasks
page, the spike-filing fallback in its Bug column, the selection log's filter, and the trigger
reply. And the Java stack's R8-remapped line numbers (``java.trust_line_numbers`` false) must
render as what they are: a number Socorro shows, not a line of the source.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_fenix_ui

No network, no Postgres: the models are stood in for, the templates render for real.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from flask import render_template  # noqa: E402

from crashclouseau import app, html, models, trigger  # noqa: E402
from crashclouseau.agent import orchestrator  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
STALE = 2100
_UUID = "8f2c1a3e-4b7d-4c9e-9a1b-2c3d4e260914"
_KT = "mobile/android/fenix/app/src/main/java/org/mozilla/fenix/components/Keystore.kt"
_REPO = "https://hg.mozilla.org/mozilla-central"


def _row(**kw):
    """A ``Dossier.list_tasks`` row with NO channel and NO product, like
    tests/test_tasks_view._row: the view must keep tolerating rows without them."""
    base = dict(
        uuid="0" * 36, signature="sig", status="done",
        created=NOW - timedelta(minutes=20), updated=NOW - timedelta(minutes=2),
        cost_usd=None, input_tokens=None, output_tokens=None, cache_read_tokens=None,
        worker_models=None, verdict=None, confidence=None, filed_bug=None, filed_mode=None,
        filed_needinfo=None, run_started=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _spike(**kw):
    """A ``SpikeEscalation.to_dict()`` row; the table has carried its product from the start."""
    base = dict(
        id=9, signature="OOM | large", product="Fenix", channel="nightly",
        build_day="2026-09-14", buildid="20260914092731", uuid="c" * 36, kind="build_day",
        status="done", attempts=1,
        spike={"kind": "build_day", "count": 19, "installs": 6, "ratio": 4.8, "z": 4.6},
        spike_sentence="19 reports from 6 installations on the 2026-09-14 build",
        siblings=None, skipped=None, findings={"assessment": "unknown", "culprit": None},
        filing={"filed": True, "bug": 2070999, "mode": "spike_new_bug"},
        error=None, cost_usd=1.2, input_tokens=100, output_tokens=50, cache_read_tokens=0,
        created=(NOW - timedelta(minutes=6)).isoformat(),
        updated=(NOW - timedelta(minutes=1)).isoformat(),
    )
    base.update(kw)
    return base


class TestTheProductReachesTheTasksPage(unittest.TestCase):
    """`Dossier.list_tasks` selects `Build.product`; `_task_view` is the literal dict between
    it and the template, so the key has to be named there (the channel column was born dead
    for exactly this reason, tests/test_tasks_view.TestTheChannelReachesTheView). On the page a
    Fenix nightly run and a Firefox nightly run were both a bare `N`."""

    def test_the_product_reaches_the_task_dict(self):
        tasks, _ = html._task_view([_row(product="Fenix", channel="nightly")], STALE, NOW)
        self.assertEqual(tasks[0]["product"], "Fenix")

    def test_a_row_without_a_product_still_renders_as_before(self):
        tasks, summary = html._task_view([_row()], STALE, NOW)
        self.assertIsNone(tasks[0]["product"])
        with app.test_request_context():
            out = render_template("tasks.html", tasks=tasks, summary=summary)
        self.assertIn('title="unknown">?</td>', out)
        self.assertNotIn("task-prod", out)

    def test_fenix_nightly_is_told_from_firefox_nightly(self):
        tasks, summary = html._task_view(
            [_row(uuid="f" * 36, product="Fenix", channel="nightly", version="156.0a1"),
             _row(uuid="d" * 36, product="Firefox", channel="nightly", version="156.0a1")],
            STALE, NOW)
        with app.test_request_context():
            out = render_template("tasks.html", tasks=tasks, summary=summary)
        # The product leads the tooltip on both; only the non-Firefox one gets the marker.
        self.assertIn('title="Fenix nightly 156.0a1">N'
                      '<span class="task-prod task-prod-fenix">fenix</span></td>', out)
        self.assertIn('title="Firefox nightly 156.0a1">N</td>', out)
        self.assertEqual(out.count("task-prod-fenix"), 1)

    def test_the_spike_row_is_marked_too_and_its_json_link_is_scoped(self):
        client = app.test_client()
        with mock.patch.object(models.Dossier, "list_tasks", return_value=[]), \
                mock.patch.object(models.SpikeEscalation, "recent", return_value=[_spike()]):
            rv = client.get("/tasks.html")
        self.assertEqual(rv.status_code, 200)
        body = rv.get_data(as_text=True)
        self.assertIn('title="Fenix nightly 20260914092731">N'
                      '<span class="task-prod task-prod-fenix">fenix</span></td>', body)
        # /api/spikes validates ?product=, so the row's record link narrows to its product.
        self.assertIn("&amp;channel=nightly&amp;product=Fenix", body)


class TestSpikeFilingsArePerProduct(unittest.TestCase):
    """The Bug column's fallback keyed a spike filing on (signature, channel) only. Fenix
    nightly and Firefox nightly share `nightly` and share native signatures, so a Fenix spike
    bug would have shown on a desktop run of the same signature, and vice versa."""

    def test_a_fenix_spike_filing_stays_off_the_desktop_run(self):
        filings = html._spike_filings([_spike()])
        self.assertEqual(filings[("OOM | large", "nightly", "Fenix")]["bug"], "2070999")
        self.assertEqual(filings[("OOM | large", "nightly")]["bug"], "2070999")
        desktop = _row(uuid="a" * 36, signature="OOM | large", channel="nightly",
                       product="Firefox")
        fenix = _row(uuid="b" * 36, signature="OOM | large", channel="nightly", product="Fenix")
        # No product: a dossier whose build row is gone, or a row written by hand.
        unknown = _row(uuid="d" * 36, signature="OOM | large", channel="nightly")
        tasks, summary = html._task_view([desktop, fenix, unknown], STALE, NOW,
                                         spike_filings=filings)
        self.assertIsNone(tasks[0]["spike_bug"])
        self.assertEqual(tasks[1]["spike_bug"], "2070999")
        self.assertEqual(tasks[2]["spike_bug"], "2070999")   # channel-only key, as before
        self.assertEqual(summary["filed"], 0)                # the tile stays the filer's

    def test_the_crash_itself_is_matched_whatever_the_product_says(self):
        filings = html._spike_filings([_spike(uuid="e" * 36)])
        row = _row(uuid="e" * 36, signature="something else", channel="beta", product="Firefox")
        tasks, _ = html._task_view([row], STALE, NOW, spike_filings=filings)
        self.assertEqual(tasks[0]["spike_bug"], "2070999")

    def test_a_sibling_gets_both_keys(self):
        filings = html._spike_filings([_spike(
            signature="Foo::Bar::<T>::operator()",
            siblings=["Foo::Bar::<T>::operator()", "Foo::Bar::{lambda}::operator()"])])
        self.assertIn(("Foo::Bar::{lambda}::operator()", "nightly", "Fenix"), filings)
        self.assertIn(("Foo::Bar::{lambda}::operator()", "nightly"), filings)


def _selection_row(**kw):
    base = dict(
        signature="OOM | large", product="Fenix", channel="nightly", build_day="2026-09-14",
        outcome="selected", number=19, position=2, evaluable=True, baseline=["3", "4"],
        bids={"20260914092731": {"count": 19, "installs": 6}}, picked="20260914092731",
        run_date=NOW.isoformat(), ever_selected=True, first_run_date=NOW.isoformat(),
    )
    base.update(kw)
    return base


class TestTheSelectionPageHasAProductSelect(unittest.TestCase):
    """`/api/selection?product=Fenix` could narrow to one product and the page could not: it
    offered no widget and showed the two products' rows indistinguishably."""

    def setUp(self):
        self.client = app.test_client()

    def _render(self, query, rows):
        with mock.patch.object(models.Selection, "recent", return_value=rows) as recent, \
                mock.patch.object(models.Selection, "summary", return_value={"selected": 1}):
            rv = self.client.get("/selection.html" + query)
        self.assertEqual(rv.status_code, 200)
        return rv.get_data(as_text=True), recent

    def test_the_select_lists_every_product_and_keeps_the_choice(self):
        body, recent = self._render("?product=Fenix&channel=nightly", [_selection_row()])
        self.assertIn('<select name="product"', body)
        self.assertIn('<option value="">all products</option>', body)
        for p in models.PRODUCT_TYPE.enums:
            self.assertIn('<option value="{}"'.format(p), body)
        self.assertIn('<option value="Fenix" selected>Fenix</option>', body)
        # `?channel=` has no widget yet; it must survive a Show click.
        self.assertIn('<input type="hidden" name="channel" value="nightly">', body)
        self.assertIn("<th>product</th>", body)
        self.assertIn("<td>Fenix</td>", body)
        recent.assert_called_once_with(None, product="Fenix", channel="nightly")

    def test_all_products_by_default(self):
        body, recent = self._render("", [_selection_row(product="Firefox")])
        self.assertNotIn("selected>", body)
        self.assertNotIn('name="channel"', body)
        self.assertIn("<td>Firefox</td>", body)
        recent.assert_called_once_with(None, product=None, channel=None)

    def test_an_unknown_product_is_ignored_not_a_404(self):
        body, recent = self._render("?product=Thunderbird", [])
        self.assertIn("No recorded decisions.", body)
        recent.assert_called_once_with(None, product=None, channel=None)


class TestTheCrashStackMarksAnUntrustedLine(unittest.TestCase):
    """`CrashStack.get_by_uuid` stamps `line_trusted` False on a Java frame under the shipped
    pref and drops the `#l` anchor from the annotate URL; the template has to follow -- show
    the number (it is what Socorro shows) marked as R8-remapped, and link the FILE, never the
    line, from the codeview glass too. Keystore.kt:269 of the motivating crash is a KDoc
    comment. A native frame renders exactly as it always did."""

    def setUp(self):
        self.client = app.test_client()
        self.uuid_info = {
            "uuid": _UUID, "id": 1, "signature": "java.lang.IllegalStateException: keystore",
            "buildid": datetime(2026, 9, 14, 9, 27, 31, tzinfo=timezone.utc),
            "channel": "nightly", "product": "Fenix", "version": "156.0a1", "java": True,
            "node": "0123456789ab",
        }

    def _frame(self, filename, url, **kw):
        frame = {
            "stackpos": 0, "filename": filename,
            "function": "org.mozilla.fenix.components.Keystore.getOrCreateKey",
            "module": "org.mozilla.fenix.components.Keystore",
            "changesets": OrderedDict([("abc123def456", {
                "score": 5, "backedout": False, "pushdate": NOW, "bugid": 0})]),
            "line": 269, "node": "0123456789ab", "original": filename + ":269",
            "internal": True, "url": url,
        }
        frame.update(kw)
        return frame

    def _render(self, frame):
        with mock.patch.object(models.CrashStack, "get_by_uuid",
                               return_value=({"frames": [frame]}, self.uuid_info)), \
                mock.patch.object(html.bugzilla_apply, "build_evidence", return_value=None), \
                mock.patch.object(html.population, "for_crash", return_value=None):
            rv = self.client.get("/crashstack.html?uuid=" + _UUID)
        self.assertEqual(rv.status_code, 200)
        return rv.get_data(as_text=True)

    def test_an_untrusted_line_is_marked_and_nothing_links_to_it(self):
        # What `get_by_uuid` hands over for an untrusted frame: the annotate URL, no anchor.
        url = "{}/annotate/0123456789ab/{}".format(_REPO, _KT)
        body = self._render(self._frame(_KT, url, line_trusted=False))
        self.assertIn('<span id="line-0" class="line-untrusted" '
                      'title="R8-remapped line, not the real source line">~269</span>', body)
        self.assertIn('href="{}"'.format(url), body)
        self.assertNotIn("#l269", body)
        self.assertNotIn("&line=269", body)
        self.assertIn("&node=abc123def456&channel=nightly", body)   # the codeview glass: file only
        self.assertIn("the file, not the line", body)

    def test_a_trusted_line_renders_as_before(self):
        url = "{}/annotate/0123456789ab/dom/base/nsFoo.cpp#l269".format(_REPO)
        self.uuid_info.update(java=False, product="Firefox",
                              signature="mozilla::dom::Foo::Bar")
        body = self._render(self._frame("dom/base/nsFoo.cpp", url, line_trusted=True,
                                        function="mozilla::dom::Foo::Bar",
                                        module="xul.dll"))
        self.assertIn('<span id="line-0">269</span>', body)
        self.assertIn("#l269", body)
        self.assertIn("&node=abc123def456&line=269&channel=nightly", body)
        self.assertNotIn("line-untrusted", body)

    def test_a_frame_without_the_key_is_trusted(self):
        # Frames built before the key existed, and hand-built ones: the native rendering.
        url = "{}/annotate/0123456789ab/dom/base/nsFoo.cpp#l269".format(_REPO)
        frame = self._frame("dom/base/nsFoo.cpp", url)
        self.assertNotIn("line_trusted", frame)
        body = self._render(frame)
        self.assertIn('<span id="line-0">269</span>', body)
        self.assertNotIn("line-untrusted", body)

    def test_an_unlinked_untrusted_frame_is_marked_too(self):
        body = self._render(self._frame("", "", line_trusted=False, changesets=OrderedDict(),
                                        original="org.mozilla.fenix.components.Keystore"))
        self.assertIn('org.mozilla.fenix.components.Keystore:<span class="line-untrusted" '
                      'title="R8-remapped line, not the real source line">~269</span>', body)


class TestTheTriggerReplyNamesTheProduct(unittest.TestCase):
    """A Fenix uuid is accepted by `POST /api/tasks/trigger` the moment Fenix is a configured
    product, and its run files nothing whatever `file_bug` says (the product hold). The reply
    has to say which product was triggered, on both branches."""

    def _retrigger(self, uuid, channel=None):
        return {"uuid": uuid, "cancelled": False, "already_filed": None}

    def test_an_ingested_fenix_uuid(self):
        info = {"product": "Fenix", "channel": "nightly", "buildid": "20260914092731",
                "signature": "java.lang.IllegalStateException: keystore"}
        with mock.patch.object(models.UUID, "exists", return_value=False), \
                mock.patch.object(trigger, "ingest", return_value=info), \
                mock.patch.object(models.Dossier, "set_run_options", return_value=True), \
                mock.patch.object(orchestrator, "retrigger_agent", self._retrigger):
            out = trigger.trigger_one(_UUID, file_bug=True)
        self.assertEqual((out["ok"], out["ingested"], out["product"], out["channel"]),
                         (True, True, "Fenix", "nightly"))
        self.assertTrue(out["run_options"]["autofile"])   # recorded; the filer holds Fenix

    def test_a_known_fenix_uuid(self):
        info = {"buildid": "20260914092731", "product": "Fenix", "channel": "nightly",
                "version": "156.0a1", "signature": "java.lang.IllegalStateException: keystore"}
        with mock.patch.object(models.UUID, "exists", return_value=True), \
                mock.patch.object(models.UUID, "get_info", return_value=info), \
                mock.patch.object(models.Dossier, "set_run_options", return_value=True), \
                mock.patch.object(orchestrator, "retrigger_agent", self._retrigger):
            out = trigger.trigger_one(_UUID)
        self.assertEqual((out["ok"], out["ingested"], out["product"], out["channel"],
                          out["signature"]),
                         (True, False, "Fenix", "nightly", info["signature"]))

    def test_a_known_uuid_without_a_build_row_still_answers(self):
        # `UUID.get_info` joins builds INNER; `uuids.buildid` is nullable -- it answers None.
        with mock.patch.object(models.UUID, "exists", return_value=True), \
                mock.patch.object(models.UUID, "get_info", return_value=None), \
                mock.patch.object(models.UUID, "get_channel", return_value="nightly"), \
                mock.patch.object(models.UUID, "get_signature", return_value="sig"), \
                mock.patch.object(models.Dossier, "set_run_options", return_value=True), \
                mock.patch.object(orchestrator, "retrigger_agent", self._retrigger):
            out = trigger.trigger_one(_UUID)
        self.assertEqual((out["ok"], out["product"], out["channel"], out["signature"]),
                         (True, None, "nightly", "sig"))

    def test_the_no_stack_reason_names_both_stack_shapes(self):
        processed = {"uuid": _UUID, "product": "Fenix", "release_channel": "nightly",
                     "version": "156.0a1", "build": "20260914092731",
                     "signature": "java.lang.IllegalStateException: keystore"}
        with mock.patch.object(trigger.inspector, "get_crash_data", return_value=processed), \
                mock.patch.object(models.Build, "get_id", return_value=7), \
                mock.patch.object(models.Signature, "get_id", return_value=3), \
                mock.patch.object(models.UUID, "add", return_value=True), \
                mock.patch.object(trigger.tools, "get_changeset", return_value="abc123def456"), \
                mock.patch.object(trigger.update, "put_report", return_value=None):
            with self.assertRaisesRegex(
                    trigger.TriggerError,
                    r"no usable stack .* neither a json_dump nor a java_stack_trace"):
                trigger.ingest(_UUID)


if __name__ == "__main__":
    unittest.main()
