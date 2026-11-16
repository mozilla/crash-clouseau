# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Shared harnesses for selector and agent-gate tests."""
from datetime import datetime, timedelta, timezone
from unittest import mock

from crashclouseau import bugzilla_apply, config, datacollector as dc, models, spikes, utils
from crashclouseau.agent import orchestrator, spike_escalation as se


def selection_row(number, baseline, installs, signature, outcome=utils.SELECTED,
                  build_day="2026-09-13", picked="20260913212439"):
    first_run = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    return {"signature": signature, "product": "Fenix", "channel": "nightly",
            "build_day": build_day, "outcome": outcome, "number": number, "position": 5,
            "evaluable": True, "baseline": list(baseline),
            "bids": {picked: {"count": number, "installs": installs}}, "picked": picked,
            "run_date": first_run, "ever_selected": True, "first_run_date": first_run}


class FakeEscalation:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def set_status(self, status, error=None, commit=True):
        self.status = status

    def merge_payload(self, values, commit=True):
        self.payload = dict(self.payload or {}, **(values or {}))


class SelectorHarness:
    def _collect(self, population, builds):
        class FakeSuperSearch:
            def __init__(self, params=None, handler=None, handlerdata=None):
                bid = params["build_id"]
                facets = [
                    {"term": sgn,
                     "facets": {"cardinality_install_time": {"value": installs},
                                "build_id": [{"term": bid, "count": count}]}}
                    for sgn, (count, installs) in population.get(bid, {}).items()
                ]
                handler({"errors": None, "facets": {"signature": facets}}, handlerdata)

            def wait(self):
                pass

        rising = mock.Mock(return_value={})
        with mock.patch.object(dc.socorro, "SuperSearch", FakeSuperSearch), \
                mock.patch.object(dc, "get_proto_small"), \
                mock.patch.object(dc, "get_proto_big"), \
                mock.patch.object(dc, "get_uuids_java"), \
                mock.patch.object(dc, "_rising_picks", rising), \
                mock.patch.object(dc, "_onset_picks", return_value={}), \
                mock.patch.object(dc, "get_builds", return_value=(list(builds), ">=2026-09-11")):
            data, selection = dc.get_new_signatures("Fenix", "nightly", datetime(2026, 9, 14))
        return data, selection, rising


class AgentHarness:
    def _enqueue(self, signature):
        queue = mock.Mock()
        with mock.patch.object(config, "get_agent_enabled", return_value=True), \
                mock.patch.object(config, "get_agent_channels", return_value=["nightly"]), \
                mock.patch.object(config, "get_agent_products", return_value=["Firefox", "Fenix"]), \
                mock.patch.object(orchestrator, "_product_of", return_value="Fenix"), \
                mock.patch.object(orchestrator, "_goes_to_front", return_value=False), \
                mock.patch.object(orchestrator, "_proto_already_triaged", return_value=False), \
                mock.patch.object(models.UUID, "get_signature", return_value=signature), \
                mock.patch.object(orchestrator.worker, "get_queue", return_value=queue) as gq:
            orchestrator.enqueue_agent("u-fenix", channel="nightly")
        return gq, queue

    def _run(self, signature):
        with mock.patch.object(config, "get_agent_channels", return_value=["nightly"]), \
                mock.patch.object(config, "get_agent_products", return_value=["Firefox", "Fenix"]), \
                mock.patch.object(orchestrator, "_product_of", return_value="Fenix"), \
                mock.patch.object(models.UUID, "get_channel", return_value="nightly"), \
                mock.patch.object(models.UUID, "get_signature", return_value=signature), \
                mock.patch.object(models.Dossier, "skip_triage", return_value=False), \
                mock.patch.object(orchestrator, "_proto_already_triaged", return_value=False), \
                mock.patch.object(orchestrator, "build_seed", return_value=None) as seed:
            orchestrator.run_evidence_agent("u-fenix")
        return seed


class SpikeSweepHarness:
    def setUp(self):
        self.cfg = config.get_agent_spike_escalation()
        self.created = []
        self.enqueued = []

        def create(signature, product, channel, build_day, buildid=None, uuid=None,
                   kind="build_day", payload=None, commit=True):
            row = FakeEscalation(id=len(self.created) + 1, signature=signature, product=product,
                                 channel=channel, build_day=build_day, buildid=buildid,
                                 uuid=uuid, kind=kind, payload=payload or {}, status="pending",
                                 attempts=0)
            self.created.append(row)
            return row

        patches = [
            mock.patch.object(models.SpikeEscalation, "for_pair", return_value=None),
            mock.patch.object(models.SpikeEscalation, "latest_for_signature", return_value=None),
            mock.patch.object(models.SpikeEscalation, "count_since", return_value=0),
            mock.patch.object(models.SpikeEscalation, "deferred", return_value=[]),
            mock.patch.object(models.SpikeEscalation, "retryable", return_value=[]),
            mock.patch.object(models.SpikeEscalation, "create", side_effect=create),
            mock.patch.object(se, "_enqueue", side_effect=lambda i, c: self.enqueued.append(i)),
            mock.patch.object(se, "classic_runs", return_value=[
                {"uuid": "u-1", "status": "done", "verdict": "abstain", "filed_bug": None}]),
            mock.patch.object(se, "representative_uuid", return_value="u-1"),
            mock.patch.object(se, "_handoff_for_spike", return_value=None),
            mock.patch.object(se, "_trend", return_value={}),
            mock.patch.object(spikes, "build_history", return_value=[]),
            mock.patch.object(bugzilla_apply, "_open_bugs_for_signature", return_value=[]),
            mock.patch.object(se, "resolve_venue_below_public", return_value=None),
            mock.patch.object(se, "_raptor_lake_before_spending", return_value=None),
            mock.patch.object(spikes, "judge_selection", wraps=spikes.judge_selection),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _sweep(self, rows, room=2):
        with mock.patch.object(models.Selection, "escalation_candidates", return_value=rows):
            return se._sweep_channel("Fenix", "nightly", self.cfg, room)
