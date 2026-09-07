# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
"""Retire a channel label: delete every row it owns, in one transaction.

A channel is retired in three steps (`DEPLOY.md`, "Retiring a line"): drop it from
``INGEST_CHANNELS`` and ``AGENT_CHANNELS``, delete its rows, drop the label from
``config.channels``. This module is the middle step. ``nodes`` cascades to ``changesets``,
``builds``, ``uuids``, ``crashstack``, ``dossiers`` and ``verdicts``; ``selection`` and
``lastdate`` are keyed by channel directly. A Postgres enum label cannot be dropped and is left
in the type; ``models.CHANNEL_TYPE`` reads it back if a row ever carries it again.

Written for esr115 and esr140 (2026-09-07): 536 nodes, 4 builds, 66 crashes, 36 dossiers (7 of
them ``error`` rows that sat at the top of tasks.html), 2,240 selection rows, 2 lastdate rows.
It REFUSES a label a live switch still names -- retiring what a tick is still ingesting would
be undone twenty minutes later -- and warns about a label ``config.channels`` still lists.
Postgres only (``::text`` casts, ``= ANY(array)``)."""
from sqlalchemy import text

from . import config, db
from .logger import logger

# (table, column) pairs that own rows by channel. Order matters only for reporting; the DELETEs
# run in one transaction and `nodes` carries the cascade.
_DIRECT = (("selection", "channel"), ("lastdate", "channel"), ("nodes", "channel"))
# What the `nodes` delete takes with it, counted for the report.
_CASCADED = {
    "builds": "SELECT count(*) FROM builds WHERE channel::text = ANY(:labels)",
    "uuids": "SELECT count(*) FROM uuids u JOIN builds b ON b.id = u.buildid "
             "WHERE b.channel::text = ANY(:labels)",
    "dossiers": "SELECT count(*) FROM dossiers d JOIN uuids u ON u.id = d.uuidid "
                "JOIN builds b ON b.id = u.buildid WHERE b.channel::text = ANY(:labels)",
}


def live_labels(labels):
    """The labels among *labels* that a live switch still names."""
    live = set(config.get_ingest_channels()) | set(config.get_agent_channels())
    return sorted(set(labels) & live)


def counts(labels, conn=None):
    """``{table: rows}`` owned by *labels*, direct and cascaded."""
    labels = list(labels)
    out = {}

    def run(c):
        for table, column in _DIRECT:
            out[table] = c.execute(
                text("SELECT count(*) FROM {} WHERE {}::text = ANY(:labels)".format(
                    table, column)), {"labels": labels}).scalar()
        for table, sql in _CASCADED.items():
            out[table] = c.execute(text(sql), {"labels": labels}).scalar()

    if conn is not None:
        run(conn)
    else:
        with db.engine.connect() as c:
            run(c)
    return out


def retire(labels, execute=False):
    """Delete the rows *labels* own. ``(before, after)`` counts; ``after`` is ``None`` on a dry
    run. Raises ``ValueError`` for a label a live switch still names."""
    labels = [str(lab) for lab in labels]
    live = live_labels(labels)
    if live:
        raise ValueError(
            "refusing to retire {}: still named by INGEST_CHANNELS / AGENT_CHANNELS. Drop it "
            "from both first, or the next tick re-ingests it.".format(", ".join(live)))
    still = sorted(set(labels) & set(config.get_channels()))
    if still:
        logger.warning("retire: %s still listed in config.channels; its rows go, the label stays",
                       ", ".join(still))
    before = counts(labels)
    if not execute:
        return before, None
    with db.engine.begin() as conn:
        for table, column in _DIRECT:
            conn.execute(text("DELETE FROM {} WHERE {}::text = ANY(:labels)".format(
                table, column)), {"labels": labels})
        after = counts(labels, conn=conn)
    logger.info("retire: deleted rows of %s: %s", ", ".join(labels), before)
    return before, after
