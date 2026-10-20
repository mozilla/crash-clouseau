# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Run with: uv run python -m unittest tests.test_queue_front
# Database fixtures require disposable Postgres (setup in README.md).
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import db, models, utils  # noqa: E402
from crashclouseau.agent import orchestrator as orch  # noqa: E402


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


class _Redis:
    """SET NX stub; records expiry without expiring keys."""

    def __init__(self):
        self.keys = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True


class TestEnqueueAtFront(unittest.TestCase):
    def _enqueue(self, uuid, sig, conn):
        q = mock.MagicMock()
        q.connection = conn
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch.config, "get_agent_channels", return_value=["nightly"]), \
             mock.patch.object(orch.config, "get_agent_products", return_value=["Firefox"]), \
             mock.patch.object(orch.models.UUID, "get_signature", return_value="S"), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.models.UUID, "unanalysed_signature", return_value=sig), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent(uuid, "nightly", product="Firefox")
        q.enqueue_call.assert_called_once()
        return q.enqueue_call.call_args.kwargs["at_front"]

    def test_the_first_crash_of_a_new_signature_goes_to_the_front(self):
        conn = _Redis()
        self.assertIs(self._enqueue("u-1", (7, "Firefox"), conn), True)
        self.assertEqual(conn.keys["clouseau:agent:front:7:Firefox"],
                         ("u-1", orch._FRONT_CLAIM_S))

    def test_its_siblings_stay_in_line(self):
        conn = _Redis()
        self.assertIs(self._enqueue("u-1", (7, "Firefox"), conn), True)
        self.assertIs(self._enqueue("u-2", (7, "Firefox"), conn), False)
        self.assertIs(self._enqueue("u-3", (8, "Firefox"), conn), True)

    def test_an_analysed_signature_stays_in_line_and_takes_no_claim(self):
        conn = _Redis()
        self.assertIs(self._enqueue("u-1", None, conn), False)
        self.assertEqual(conn.keys, {})

    def test_a_failed_check_still_enqueues_in_line(self):
        q = mock.MagicMock()
        with mock.patch.object(orch.config, "get_agent_enabled", return_value=True), \
             mock.patch.object(orch, "_proto_already_triaged", return_value=False), \
             mock.patch.object(orch.models.UUID, "unanalysed_signature",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(orch.worker, "get_queue", return_value=q):
            orch.enqueue_agent("u-1", force=True)
        q.enqueue_call.assert_called_once()
        self.assertIs(q.enqueue_call.call_args.kwargs["at_front"], False)


@unittest.skipUnless(_is_postgres(), "fixtures require disposable Postgres")
class TestUnanalysedSignature(unittest.TestCase):
    SIG = "QueueFront::NewSignature"
    BUILDID = datetime(2026, 10, 20, 9, 17, tzinfo=timezone.utc)
    UUID_PREFIX = "qf-"

    def setUp(self):
        models.create()
        self._clean()
        self.sig = models.Signature.get_id(self.SIG)
        self.nightly = models.Build(self.BUILDID, "Firefox", "nightly", "9.9.9qf1", None)
        self.beta = models.Build(self.BUILDID, "Firefox", "beta", "9.9.9qf2", None)
        self.fenix = models.Build(self.BUILDID, "Fenix", "nightly", "9.9.9qf3", None)
        db.session.add_all([self.nightly, self.beta, self.fenix])
        db.session.commit()

    def _clean(self):
        db.session.rollback()
        db.session.query(models.Build).filter(
            models.Build.buildid == self.BUILDID,
        ).delete(synchronize_session=False)
        db.session.query(models.UUID).filter(
            models.UUID.uuid.like(self.UUID_PREFIX + "%")
        ).delete(synchronize_session=False)
        db.session.commit()

    def tearDown(self):
        self._clean()

    def _uuid(self, name, build, proto="proto|frame0"):
        u = models.UUID(name, self.sig, utils.hash(proto), build.id)
        db.session.add(u)
        db.session.commit()
        return name

    def test_no_dossier_yet_is_new(self):
        u = self._uuid("qf-1", self.nightly)
        self.assertEqual(models.UUID.unanalysed_signature(u), (self.sig, "Firefox"))

    def test_a_dossier_in_any_status_on_another_proto_or_channel_is_not_new(self):
        first = self._uuid("qf-1", self.nightly, proto="proto|a")
        later = self._uuid("qf-2", self.beta, proto="proto|b")
        models.Dossier.upsert(first, payload={}, status="error")
        self.assertIsNone(models.UUID.unanalysed_signature(later))

    def test_a_dossier_on_another_product_does_not_count(self):
        desktop = self._uuid("qf-1", self.nightly)
        android = self._uuid("qf-2", self.fenix)
        models.Dossier.upsert(desktop, payload={}, status="done")
        self.assertEqual(models.UUID.unanalysed_signature(android), (self.sig, "Fenix"))

    def test_an_unknown_uuid_is_not_new(self):
        self.assertIsNone(models.UUID.unanalysed_signature("qf-missing"))


if __name__ == "__main__":
    unittest.main()
