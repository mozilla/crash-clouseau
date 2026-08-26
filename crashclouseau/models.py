# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import re
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from libmozdata.hgmozilla import Mercurial
import sqlalchemy.dialects.postgresql as pg
from sqlalchemy import and_, inspect, func, not_, or_, text
from sqlalchemy.orm import aliased
import pytz
from . import config, db, utils
from .logger import logger


CHANNEL_TYPE = db.Enum(*config.get_channels(), name="CHANNEL_TYPE")
PRODUCT_TYPE = db.Enum(*config.get_products(), name="PRODUCT_TYPE")

# Evidence-agent persistence (#04). The dossier JSON content schema is owned by
# the dossier-builder sub-plan (#03); this layer stores the envelope + verdict.
DOSSIER_SCHEMA_VERSION = 1

# Corroboration flags marking a verdict suppressed for a reason specific to ONE CRASH REPORT —
# a broken installation, a corrupted fault address, a defective CPU — rather than to the
# signature or the candidate. A run carrying one of these must not close its proto-signature
# cluster; see ``UUID.proto_already_analyzed``.
#
# ``hardware_noise_signature_suppressed`` is deliberately ABSENT, though it comes from the same
# gate as ``possible_bit_flip_suppressed``: it says the SIGNATURE is mostly hardware error, which
# is equally true of every report in the cluster, so it closes the cluster exactly as the backout
# gate does. Re-deriving that answer costs ~$3 a report and cannot come out differently.
_INSTANCE_SUPPRESSED = (
    "bad_machine_suppressed", "possible_bit_flip_suppressed", "broken_cpu_suppressed",
)

# ``abstain_reason`` prefixes meaning CLOUSEAU broke — the agent's handoff had no readable
# JSON block, or the dossier failed validation and the verdict was dropped. Such a run
# reached ``done`` without examining anything, so it must not close its proto-signature
# cluster either (see ``UUID.proto_already_analyzed``): measured on prod 2026-08-12, 107 of
# 2178 done dossiers are one of these, and they had permanently suppressed 15 later crashes
# across 11 clusters — five of them carrying a real on-stack score.
#
# Prefixes of the reasons ``agent.schema`` builds (``NO_HANDOFF_REASON`` and the two
# "dossier validation failed" branches of ``parse_and_validate``), spelled out here rather than
# imported: models.py is on the web/ingestion import path and must not pull in
# pydantic/claude-agent-sdk. ``tests/test_persistence`` pins the two together so they
# cannot drift apart.
_UNUSABLE_VERDICT_PREFIXES = ("dossier validation failed", "no parseable ```json block")

# How quiet a ``running`` dossier must go before the RQ job that OWNS it may re-take it
# (``Dossier.claim_running(..., own_job_id=)``). Deliberately a small multiple of
# ``agent.orchestrator._HEARTBEAT_INTERVAL_S`` (120s): a live run stamps ``updated`` every
# beat, so it can never look this stale, and a retry therefore cannot displace a horse that
# is still working — which is possible, because RQ's abandoned-execution cleanup keys on
# the worker PARENT's heartbeat and will retry a job whose forked horse is alive. Two beats
# of slack absorbs a late beat; a retry cannot physically land sooner than the registry TTL
# (~90s) plus the retry interval anyway. Kept here rather than imported to avoid
# models -> agent.orchestrator; ``tests/test_reaper_recovery`` pins the relationship so the
# two cannot drift apart.
_OWN_JOB_RECLAIM_AFTER_S = 240
VERDICT_TYPE = db.Enum(
    "culprit", "lead", "unrelated", "abstain", "error", name="VERDICT_TYPE"
)
AGENT_STATUS_TYPE = db.Enum(
    "pending", "running", "done", "error", name="AGENT_STATUS_TYPE"
)


class LastDate(db.Model):
    __tablename__ = "lastdate"

    channel = db.Column(CHANNEL_TYPE, primary_key=True)
    mindate = db.Column(db.DateTime(timezone=True))
    maxdate = db.Column(db.DateTime(timezone=True))

    def __init__(self, channel, mindate, maxdate):
        self.channel = channel
        self.mindate = mindate
        self.maxdate = maxdate

    @staticmethod
    def update(mindate, maxdate, channel):
        q = db.session.query(LastDate).filter(LastDate.channel == channel)
        q = q.first()
        if q:
            if mindate:
                q.mindate = mindate
            q.maxdate = maxdate
            db.session.add(q)
        else:
            db.session.add(LastDate(channel, mindate, maxdate))
        db.session.commit()
        return mindate, maxdate

    @staticmethod
    def get(channel):
        d = db.session.query(LastDate).filter(LastDate.channel == channel)
        d = d.first()
        if d:
            mindate = d.mindate.astimezone(pytz.utc) if d.mindate else None
            maxdate = d.maxdate.astimezone(pytz.utc) if d.maxdate else None
            return mindate, maxdate
        return None, None


class File(db.Model):
    __tablename__ = "files"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(512), unique=True)

    def __init__(self, name):
        self.name = name

    @staticmethod
    def get_id(name):
        sel = db.select(db.literal(name)).where(
            ~db.exists().where(File.name == name)
        )
        ins = (
            db.insert(File)
            .from_select([File.name], sel)
            .returning(File.id)
            .cte("inserted")
        )
        rs = (
            db.session.query(File.id)
            .filter(File.name == name)
            .union_all(
                db.session.query(File.id).select_from(ins).filter(File.id == ins.c.id)
            )
        )

        first = rs.first()
        if first is None:
            first = rs.first()

        id = first[0]
        db.session.commit()
        return id

    @staticmethod
    def get_ids(names):
        rs = db.session.query(File).filter(File.name.in_(names))
        ids = {f.name: f.id for f in rs}
        newnames = set(names) - set(ids.keys())
        news = []
        for n in newnames:
            f = File(n)
            news.append(f)
            db.session.add(f)
        db.session.commit()
        for n in news:
            ids[n.name] = n.id

        return ids

    @staticmethod
    def get_full_path(name):
        m = db.session.query(File.name).filter(File.name.like("%/" + name)).first()
        if m:
            return m[0]
        return name

    @staticmethod
    def populate(files, check=False):
        if check:
            for f in files:
                File.get_id(f)
        else:
            for f in files:
                db.session.add(File(f))
            db.session.commit()


class Node(db.Model):
    __tablename__ = "nodes"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    channel = db.Column(CHANNEL_TYPE)
    node = db.Column(db.String(12))
    pushdate = db.Column(db.DateTime(timezone=True))
    backedout = db.Column(db.Boolean)
    merge = db.Column(db.Boolean)
    bug = db.Column(db.Integer)
    hgauthor = db.Column(db.Integer, db.ForeignKey("hgauthors.id", ondelete="CASCADE"))

    def __init__(self, channel, info):
        self.channel = channel
        self.node = info["node"]
        self.pushdate = info["date"]
        self.backedout = info["backedout"]
        self.merge = info["merge"]
        self.bug = info["bug"]
        self.hgauthor = HGAuthor.get_id(info["author"])

    @staticmethod
    def authors_for(nodes, channel):
        """Map each node hash -> {email, real, nick, bug, backedout, pushdate} from the
        local DB (Node join HGAuthor), for the given channel. Migration-proof (no
        network); used to surface area-experts (#15 phase 2). Skips the empty/default
        author row."""
        if not nodes:
            return {}
        rows = (
            db.session.query(
                Node.node, Node.bug, Node.backedout, Node.pushdate,
                HGAuthor.email, HGAuthor.real, HGAuthor.nick,
            )
            .join(HGAuthor, Node.hgauthor == HGAuthor.id)
            .filter(Node.channel == channel, Node.node.in_(list(nodes)))
            .all()
        )
        out = {}
        for node, bug, backedout, pushdate, email, real, nick in rows:
            if not (email or real or nick):
                continue  # the "" / default author carries no signal
            out.setdefault(node, {
                "email": email or "", "real": real or "", "nick": nick or "",
                "bug": bug, "backedout": bool(backedout), "pushdate": pushdate,
            })
        return out

    @staticmethod
    def recent_bugs_by_author(email, channel, limit=50):
        """Distinct recent bug numbers from local changesets authored by ``email`` on
        ``channel`` (the author's recent patches), newest first. DB-only (no network) —
        used as the bug-preview product::component fallback when the regressor bug itself
        is unreadable (e.g. a security bug). Empty when ``email`` is falsy or unknown."""
        if not email:
            return []
        rows = (
            db.session.query(Node.bug)
            .join(HGAuthor, Node.hgauthor == HGAuthor.id)
            .filter(Node.channel == channel, HGAuthor.email == email, Node.bug > 0)
            .order_by(Node.pushdate.desc())
            .limit(limit)
            .all()
        )
        seen = []
        for (bug,) in rows:
            if bug not in seen:
                seen.append(bug)
        return seen

    @staticmethod
    def get_min_date(channel):
        m = (
            db.session.query(db.func.min(Node.pushdate))
            .filter(Node.channel == channel)
            .first()[0]
        )
        # m is None when there are no nodes yet (e.g. a fresh database).
        return m.astimezone(pytz.utc) if m is not None else None

    @staticmethod
    def get_max_date(channel):
        m = (
            db.session.query(db.func.max(Node.pushdate))
            .filter(Node.channel == channel)
            .first()[0]
        )
        # m is None when there are no nodes yet (e.g. a fresh database).
        return m.astimezone(pytz.utc) if m is not None else None

    @staticmethod
    def get_bugid(node, channel):
        m = (
            db.session.query(Node.bug)
            .filter(Node.channel == channel, Node.node == node)
            .first()[0]
        )
        return m if m > 0 else 0

    @staticmethod
    def clean(date, channel):
        ndays_ago = date - relativedelta(days=config.get_ndays_of_data())
        db.session.query(Node).filter(
            Node.pushdate <= ndays_ago, Node.channel == channel
        ).delete()
        db.session.commit()
        return LastDate.update(Node.get_min_date(channel), date, channel)

    @staticmethod
    def get_ids(revs, channel):
        res = {}
        if revs:
            qs = db.session.query(Node.id, Node.node).filter(
                Node.node.in_(list(revs)), Node.channel == channel
            )
            for q in qs:
                res[q.node] = q.id
        return res

    @staticmethod
    def get_id(rev, channel):
        if rev:
            qs = db.session.query(Node.id).filter(
                Node.node == rev, Node.channel == channel
            )
            return qs.first()
        return None

    @staticmethod
    def has_channel(channel):
        q = db.session.query(Node.channel).filter(Node.channel == channel).first()
        return bool(q)


class Changeset(db.Model):
    __tablename__ = "changesets"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nodeid = db.Column(db.Integer, db.ForeignKey("nodes.id", ondelete="CASCADE"))
    fileid = db.Column(db.Integer, db.ForeignKey("files.id", ondelete="CASCADE"))
    added_lines = db.Column(pg.ARRAY(db.Integer), default=[])
    deleted_lines = db.Column(pg.ARRAY(db.Integer), default=[])
    touched_lines = db.Column(pg.ARRAY(db.Integer), default=[])
    isnew = db.Column(db.Boolean, default=False)
    analyzed = db.Column(db.Boolean, default=False)

    def __init__(self, nodeid, fileid):
        self.nodeid = nodeid
        self.fileid = fileid

    @staticmethod
    def reset(revs, channel=None):
        """Un-analyse the given changesets so ``to_analyze`` offers them again — the recovery
        for a patch parse that stored nothing usable.

        ``channel`` because a hash can have a ``nodes`` row per channel (see ``get_scores``):
        without it, resetting a beta graft also resets the central original, re-fetching a
        patch that was parsed correctly. ``None`` resets every channel, which is what a
        hand-run repair usually wants."""
        q = db.session.query(Changeset).join(Node)
        if channel:
            q = q.filter(Node.channel == channel)
        q = q.filter(Node.node.in_(revs)).update(
            {
                "analyzed": False,
                "isnew": False,
                "added_lines": [],
                "deleted_lines": [],
                "touched_lines": [],
            },
            synchronize_session="fetch",
        )
        db.session.commit()

    @staticmethod
    def to_analyze(chgsets=[], channel=""):
        if not channel:
            fl = (
                db.session.query(Changeset.nodeid, Node.node, Node.channel)
                .select_from(Changeset)
                .join(Node)
            )
            fl = (
                fl.filter(Node.merge.is_(False), Changeset.analyzed.is_(False))
                .distinct(Node.id)
                .first()
            )

            return (fl.nodeid, fl.node, fl.channel) if fl else (None, None, None)

        if not chgsets:
            return []

        chgsets = list(chgsets)
        fls = (
            db.session.query(Changeset.id, Node.id, Node.node)
            .select_from(Changeset)
            .join(Node)
        )
        fls = fls.filter(
            Node.node.in_(chgsets),
            Node.channel == channel,
            Node.merge.is_(False),
            Changeset.analyzed.is_(False),
        ).distinct(Node.id)

        res = [(nodeid, node) for _, nodeid, node in fls]
        return res

    @staticmethod
    def add(chgsets, date, channel):
        if not chgsets:
            return LastDate.update(Node.get_min_date(channel), date, channel)

        nodes = []
        files = set()
        for chgset in chgsets:
            node = Node(channel, chgset)
            db.session.add(node)
            nodes.append((node, chgset))
            files |= set(chgset["files"])
        db.session.commit()

        if files:
            ids = File.get_ids(files)
            for node, chgset in nodes:
                nodeid = node.id
                for f in chgset["files"]:
                    c = Changeset(nodeid, ids[f])
                    db.session.add(c)
            db.session.commit()

        return Node.clean(date, channel)

    @staticmethod
    def add_analyzis(data, nodeid, channel, commit=True):
        """Store one changeset's parsed diff onto its ``changesets`` rows and mark them
        analysed.

        AN EMPTY ``data`` WITH ROWS TO FILL IS LOGGED, LOUDLY. It used to be the silent
        signature of an hg **406**: ``patch.parse`` fetched raw-rev with a bare
        ``requests.get`` (no allowlisted User-Agent), the throttled body parsed to ``{}``, and
        this method still set ``analyzed=True`` with no added/deleted/touched lines — so that
        candidate scored 0 against every frame, forever, and only ``Changeset.reset`` could
        undo it. ``patch.parse`` now raises on a non-200, so a throttled fetch never reaches
        here at all and the caller leaves the changeset un-analysed for a later retry.

        WHAT THIS DELIBERATELY DOES **NOT** DO is leave ``analyzed=False`` on an empty parse.
        That would turn any changeset whose diff legitimately parses to nothing — the parser
        drops binary files and, with ``skip_comments=True``, comment-only hunks — into a
        permanent re-fetch loop on a SERIAL, self-re-enqueuing chain
        (``update.analyze_one_patch``), which is the same failure class as the livelock fixed
        in ``UUID.to_analyze``. Measured 2026-08-25 over one day of mozilla-central: 11 of 11
        non-merge changesets with an interesting file parsed non-empty through ``net.get``, 0
        fetch failures — which is too small a sample to prove the comment-only case
        unreachable, so the warning is the instrument. If it ever fires in prod, count it
        before adding a retry."""
        rows = (
            db.session.query(Changeset).filter(Changeset.nodeid == nodeid).count()
            if not data
            else 0
        )
        if rows:
            logger.warning(
                "Empty patch parse for nodeid %s on %s, but %d changeset row(s) expected "
                "lines: they will score 0 until reset", nodeid, channel, rows,
            )
        db.session.query(Changeset).filter(Changeset.nodeid == nodeid).update(
            {"analyzed": True}
        )
        if data:
            chgs = (
                db.session.query(Changeset, File.name)
                .select_from(Changeset)
                .join(File)
                .filter(Changeset.nodeid == nodeid)
            )
            for chg, name in chgs:
                # if the filename is not in data,
                # then it means that the file has been deleted
                info = data.get(name)
                if info:
                    added = info.get("added")
                    if added:
                        chg.added_lines = added
                    deleted = info.get("deleted")
                    if deleted:
                        chg.deleted_lines = deleted
                    touched = info.get("touched")
                    if touched:
                        chg.touched_lines = touched
                    new = info.get("new")
                    if new:
                        chg.isnew = True
                db.session.add(chg)

        if commit:
            db.session.commit()

    @staticmethod
    def find(filenames, mindate, maxdate, channel):
        if not filenames:
            return None

        chgs = (
            db.session.query(Changeset.id, File.name, Node.node)
            .select_from(Changeset)
            .join(Node)
            .join(File)
        )
        chgs = chgs.filter(
            File.name.in_(filenames),
            mindate <= Node.pushdate,
            Node.pushdate <= maxdate,
            Node.channel == channel,
            Node.merge.is_(False),
        )
        res = {}
        for _, fname, node in chgs:
            if fname not in res:
                res[fname] = []
            res[fname].append(node)
        return res

    @staticmethod
    def get_scores(filename, line, chgsets, csid, channel=None):
        """Line-proximity scores for one crash frame against the candidate changesets
        ``find`` produced for it.

        ``channel`` MUST be passed by anything that scores a real crash. ``find`` is
        channel-filtered and this was not, and the same changeset hash legitimately has a
        ``nodes`` row per channel: the cycle merge pushes all of mozilla-central onto
        mozilla-beta with the hashes preserved (measured: 1,932 of 1,932 merge-window
        candidates also exist on m-c under the same hash, 5,116 of 5,124 non-merge = 99.8%,
        with both repos serving byte-identical ``raw-rev``). Unfiltered, one candidate then
        yields two rows per frame, ``Score.set`` inserts both, and ``CrashStack.get_by_uuid``
        renders the same changeset twice with two different push dates — the beta one being
        the merge date, which is up to ~35 days later than the truth.

        Latent on the in-cycle beta window, which is the only one reachable today: a beta
        uplift is an hg graft with a NEW hash, and 0 of 1,009 candidate-bearing in-cycle
        changesets exist on m-c under the same hash. It stops being latent the moment the
        ``mindate`` boundary that excludes the merge push moves (see
        ``tests/test_beta_windows.py``). ``None`` keeps the old, unfiltered behaviour for a
        caller that genuinely has no channel."""
        chgs = db.session.query(Changeset).select_from(Changeset).join(Node).join(File)
        chgs = chgs.filter(
            Node.node.in_(chgsets), File.name == filename, Changeset.analyzed.is_(True)
        )
        if channel:
            chgs = chgs.filter(Node.channel == channel)
        res = []
        M = config.get_max_score()
        for chg in chgs:
            if chg.isnew:
                res.append((chg.id, csid, M))
            else:
                added = chg.added_lines
                deleted = chg.deleted_lines
                touched = chg.touched_lines
                sc = max(
                    utils.get_line_score(line, touched),
                    utils.get_line_score(line, added),
                )
                if sc < 5:
                    sc = max(sc, utils.get_line_score(line, deleted))
                res.append((chg.id, csid, sc))

        return res


class Build(db.Model):
    __tablename__ = "builds"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    buildid = db.Column(db.DateTime(timezone=True))
    product = db.Column(PRODUCT_TYPE)
    channel = db.Column(CHANNEL_TYPE)
    version = db.Column(db.String(10))
    nodeid = db.Column(db.Integer, db.ForeignKey("nodes.id", ondelete="CASCADE"))
    __table_args__ = (
        db.UniqueConstraint("buildid", "product", "channel", name="uix_builds"),
    )

    def __init__(self, buildid, product, channel, version, nodeid):
        self.buildid = buildid
        self.product = product
        self.channel = channel
        self.version = version
        self.nodeid = nodeid

    @staticmethod
    def put_data(data):
        revs = defaultdict(lambda: set())
        for prod, i in data.items():
            for chan, j in i.items():
                revs_c = revs[chan]
                for k in j.values():
                    revs_c.add(k["revision"])
        for chan, r in revs.items():
            revs[chan] = Node.get_ids(r, chan)
        for prod, i in data.items():
            for chan, j in i.items():
                revs_c = revs[chan]
                for bid, k in j.items():
                    rev = k["revision"]
                    if rev in revs_c:
                        version = k["version"]
                        ins = pg.insert(Build).values(
                            buildid=bid,
                            product=prod,
                            channel=chan,
                            version=version,
                            nodeid=revs_c[rev],
                        )
                        upd = ins.on_conflict_do_nothing()
                        db.session.execute(upd)
        db.session.commit()

    @staticmethod
    def put_build(buildid, nodeid, product, channel, version):
        db.session.add(Build(buildid, product, channel, version, nodeid))
        db.session.commit()

    @staticmethod
    def get_two_last(buildid, channel, product):
        qs = (
            db.session.query(Build.buildid, Build.version, Node.node)
            .select_from(Build)
            .filter(
                Build.buildid <= buildid,
                Build.product == product,
                Build.channel == channel,
            )
        )
        qs = qs.join(Node).order_by(Build.buildid.desc()).limit(2)
        res = [
            {
                "buildid": utils.get_buildid(q.buildid),
                "revision": q.node,
                "version": q.version,
            }
            for q in qs
        ]
        if len(res) == 2:
            x = res[0]
            res[0] = res[1]
            res[1] = x

        return res

    @staticmethod
    def get_last_versions(date, channel, product, n=0):
        """The newest ``n`` builds at or before ``date``, newest first — the SELECTION WINDOW
        for a non-nightly channel (``datacollector.get_builds``, ``n=3``).

        THE MAJOR-VERSION BREAK IS GONE, AND IT WAS TURNING BETA OFF FOR TWO DAYS A CYCLE.
        ``.limit(n)`` is applied by the database BEFORE the old Python-side
        ``major != get_major(q.version): break``, so the day after a central->beta merge the
        three newest rows are ``155.0b1 / 154.0b10 / 154.0b9``, the break fired on row two,
        ``len(res) >= 2`` failed, and this returned ``[]``. ``get_builds`` then returned no
        buildids and ``get_new_signatures`` logged one warning and returned ``({}, [])`` --
        no ``Stats``, no ``uuids``, and no ``Selection`` rows either (``record_many([])``
        returns early), so the one table built to answer "why did you do nothing" was silent
        too. Measured over the Buildhub build list: **12 of 127 beta run-days (9.4%) across 5
        merges, 2/2/2/2/4 days each** — and they land exactly on the days a freshly uplifted
        regression first reaches beta users. At a 2-week cycle it would be 14-18%.

        Mixing two majors in the window is not a defect, it is the question: the window asks
        "is this build crashier than the ones before it", and the builds before the first beta
        of a cycle ARE the previous cycle's last betas. A signature new since the merge still
        spikes from zero against them; one that was already crashing at the same rate
        correctly does not.

        WHAT THE BREAK LOOKED LIKE IT PROTECTED, AND DOES NOT: Buildhub also carries 26-30
        RC/dot-release builds on ``target.channel=beta`` (154.0, 154.0.1, 153.0.4 ...) which
        interleave with the betas by buildid. ``buildhub.VERSION_PATS["beta"]`` already keeps
        them out of this table, and they report ``release_channel=release`` in Socorro, never
        beta/aurora (20260812182057 = 154.0: 72,019 release / 0 beta) -- so do NOT "fix" that
        regexp either; it is what protects this window.

        ``len(res) >= 1`` rather than ``>= 2``: a short window is a worse window, not a broken
        one, and **a silent switch-off is the worse failure**. With one build the caller
        evaluates one build-day, which ``evaluate_days`` declines as an untestable prefix and
        RECORDS. Nightly never calls this (its builds come from Socorro), so nightly behaviour
        is untouched either way."""
        qs = (
            db.session.query(Build.buildid, Build.version, Node.node)
            .select_from(Build)
            .filter(
                Build.buildid <= date,
                Build.product == product,
                Build.channel == channel,
            )
        )
        qs = qs.join(Node).order_by(Build.buildid.desc())
        if n >= 1:
            qs = qs.limit(n)

        return [
            {
                "buildid": utils.get_buildid(q.buildid),
                "revision": q.node,
                "version": q.version,
            }
            for q in qs
        ]

    @staticmethod
    def get_pushdate_before(buildid, channel, product):
        """The push date of the build BEFORE ``buildid`` — the lower bound of a non-nightly
        crash's candidate window (``update.put_report``). ``None`` when there is no earlier
        build row.

        THE ``None`` IS THE POINT. This used to be ``return qs.pushdate`` on a ``.first()``
        that is ``None`` whenever no earlier row exists, i.e. ``AttributeError: 'NoneType'
        object has no attribute 'pushdate'`` — which on the ingestion path becomes
        ``UUID.set_error`` on a report that had nothing wrong with it. The selector cannot
        reach it (a picked build-day sits at window index >= 1, so an older row exists by
        construction) but two other paths can: ``redo.reset()`` on a UUID whose older build
        rows were cascade-deleted (``Node.clean`` prunes at ``max_ndays`` and
        ``builds.nodeid`` is ``ON DELETE CASCADE``, and on beta a whole merge push's ~5,100
        nodes age out in one instant), and any un-analysed backlog that outlives that
        pruning. Nightly cannot hit it at all — its ``mindate`` is arithmetic on the buildid.

        The caller decides what an unknown lower bound means; see ``update.put_report``,
        which falls back to the nightly rule with a warning rather than losing the report."""
        qs = (
            db.session.query(Build.buildid, Node.pushdate).select_from(Build).join(Node)
        )
        qs = (
            qs.filter(
                Build.buildid < buildid,
                Build.product == product,
                Build.channel == channel,
            )
            .order_by(Build.buildid.desc())
            .first()
        )
        return qs.pushdate if qs else None

    @staticmethod
    def get_id(bid, channel, product):
        q = (
            db.session.query(Build.id)
            .filter(
                Build.buildid == bid, Build.product == product, Build.channel == channel
            )
            .first()
        )
        if q:
            return q[0]
        return None

    @staticmethod
    def get_products(channel):
        prods = db.session.query(Build.product).filter(Build.channel == channel)
        prods = prods.distinct().order_by(Build.product.desc())
        res = [p.product for p in prods]
        return res

    @staticmethod
    def get_changeset(bid, channel, product):
        q = db.session.query(Build.id, Node.node).select_from(Build).join(Node)
        q = q.filter(
            Build.buildid == bid, Build.product == product, Build.channel == channel
        ).first()
        if q:
            return q[1]
        return None


class HGAuthor(db.Model):
    __tablename__ = "hgauthors"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(254))
    real = db.Column(db.String(128))
    nick = db.Column(db.String(64))
    bucketid = db.Column(db.Integer, default=-1)
    __table_args__ = (
        db.UniqueConstraint("email", "real", "nick", name="uix_hgauthors"),
    )

    def __init__(self, *args):
        self.email = args[0]
        self.real = args[1]
        self.nick = args[2]

    @staticmethod
    def get_id(info):
        if not info:
            return HGAuthor.get_default_id()

        info = info[0]
        return HGAuthor._get_or_create_id(*info)

    @staticmethod
    def get_default_id():
        return HGAuthor._get_or_create_id("", "", "")

    @staticmethod
    def _get_or_create_id(email, real, nick):
        ins = pg.insert(HGAuthor).values(email=email, real=real, nick=nick)
        ins = ins.on_conflict_do_nothing(
            index_elements=["email", "real", "nick"]
        ).returning(HGAuthor.id)
        first = db.session.execute(ins).first()
        if first is None:
            first = (
                db.session.query(HGAuthor.id)
                .filter(
                    HGAuthor.email == email,
                    HGAuthor.real == real,
                    HGAuthor.nick == nick,
                )
                .first()
            )
        id = first[0]
        db.session.commit()
        return id

    @staticmethod
    def put(data):
        HGAuthor.get_default_id()
        if data:
            for info in sorted(data):
                HGAuthor._get_or_create_id(*info)


class Signature(db.Model):
    __tablename__ = "signatures"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    signature = db.Column(db.String(512))

    def __init__(self, signature):
        self.signature = signature

    @staticmethod
    def get_id(signature):
        sel = db.select(db.literal(signature)).where(
            ~db.exists().where(Signature.signature == signature)
        )
        ins = (
            db.insert(Signature)
            .from_select([Signature.signature], sel)
            .returning(Signature.id)
            .cte("inserted")
        )
        rs = (
            db.session.query(Signature.id)
            .filter(Signature.signature == signature)
            .union_all(
                db.session.query(Signature.id)
                .select_from(ins)
                .filter(Signature.id == ins.c.id)
            )
        )

        first = rs.first()
        if first is None:
            first = rs.first()

        id = first[0]
        db.session.commit()
        return id

    @staticmethod
    def get_reports(signatures, product=None, channel=None):
        reports = (
            db.session.query(
                Build.buildid,
                Build.product,
                Build.channel,
                Signature.signature,
                UUID.id,
                UUID.uuid,
                UUID.max_score,
            )
            .select_from(Signature)
            .join(UUID)
            .join(Build)
            .filter(
                Signature.signature.in_(signatures),
                UUID.useless.is_(False),
                UUID.analyzed.is_(True),
            )
        )

        if product is not None:
            reports = reports.filter(Build.product == product)

        if channel is not None:
            reports = reports.filter(Build.channel == channel)

        reports_map = {
            report.id: {
                "uuid": report.uuid,
                "build_id": int(utils.get_buildid(report.buildid)),
                "product": report.product,
                "channel": report.channel,
                "signature": report.signature,
                "max_score": report.max_score,
                "changesets": [],
            }
            for report in reports
        }

        if not reports_map:
            # No reports found, no need to continue
            return []

        changeset_aggregated_columns = (
            CrashStack.uuidid,
            Node.node,
            Node.channel,
            Node.pushdate,
            Node.backedout,
            Node.merge,
            Node.bug,
        )

        changesets = (
            db.session.query(
                *changeset_aggregated_columns,
                func.max(Score.score).label("max_score"),
            )
            .select_from(CrashStack)
            .join(Score)
            .join(Changeset)
            .join(Node)
            .filter(
                CrashStack.uuidid.in_(reports_map.keys()),
            )
            .group_by(*changeset_aggregated_columns)
        )

        for changeset in changesets:
            reports_map[changeset.uuidid]["changesets"].append(
                {
                    "changeset": changeset.node,
                    "channel": changeset.channel,
                    "push_date": changeset.pushdate,
                    "is_backedout": changeset.backedout,
                    "is_merge": changeset.merge,
                    "bug_id": changeset.bug,
                    "max_score": changeset.max_score,
                }
            )

        return list(reports_map.values())


class Stats(db.Model):
    __tablename__ = "stats"

    signatureid = db.Column(
        db.Integer, db.ForeignKey("signatures.id", ondelete="CASCADE"), primary_key=True
    )
    buildid = db.Column(
        db.Integer, db.ForeignKey("builds.id", ondelete="CASCADE"), primary_key=True
    )
    number = db.Column(db.Integer, default=0)
    installs = db.Column(db.Integer, default=-1)

    def __init__(self, signatureid, buildid, number, installs):
        self.signatureid = signatureid
        self.buildid = buildid
        self.number = number
        self.installs = installs

    @staticmethod
    def get_for(signature, buildid, channel, product):
        """``{crashes, installs}`` for one (signature, build), or None — what ingestion measured
        for this exact build, for ``population.for_crash`` to show next to the wider
        signature-level numbers without spending a query on it.

        ``installs`` is stored as -1 when unknown (the column default), so it is returned as None
        rather than passed through: a stats block that prints "-1 installations" is worse than one
        that admits it does not know."""
        row = (
            db.session.query(Stats.number, Stats.installs)
            .select_from(Stats)
            .join(Signature, Signature.id == Stats.signatureid)
            .join(Build, Build.id == Stats.buildid)
            .filter(
                Signature.signature == signature,
                Build.buildid == buildid,
                Build.channel == channel,
                Build.product == product,
            )
            .first()
        )
        if row is None:
            return None
        return {
            "crashes": row.number,
            "installs": row.installs if (row.installs or -1) >= 0 else None,
        }

    @staticmethod
    def add(signatureid, buildid, number, installs, commit=True):
        ins = pg.insert(Stats).values(
            signatureid=signatureid, buildid=buildid, number=number, installs=installs
        )
        upd = ins.on_conflict_do_update(
            index_elements=["signatureid", "buildid"],
            set_=dict(number=number, installs=installs),
        )
        db.session.execute(upd)
        if commit:
            db.session.commit()


# Sourced from utils so the API's validation and the writer can never drift apart.
SELECTION_OUTCOMES = frozenset(
    {
        utils.SELECTED,
        utils.NOT_SPIKING,
        utils.UNTESTABLE_PREFIX,
        utils.BELOW_INSTALL_THRESHOLD,
        utils.IMMATURE,
        utils.DROPPED_NO_USERS,
    }
)


class Selection(db.Model):
    """What the spike selector decided about a (signature, build-day) pair — including the
    pairs it DECLINED.

    ``stats`` records decisions taken and never decisions declined: ``datacollector``
    computes a count and an install cardinality for every signature on every build in the
    window, then drops the non-spiking ones with ``data[sgn] = None``. So "why is there no
    analysis for signature X?" had no answer inside the system — the only way to settle it
    was to rebuild the selector's inputs from Socorro by hand, which is exactly how
    ``mozilla::places::History::History`` was diagnosed on 2026-08-11.

    Deliberately NOT a full census. A row is written for any outcome other than
    ``not_spiking``, plus loud days that did not spike (``count >= floor``); quieter
    non-events are dropped because they are the overwhelming majority and carry no signal.
    ``signature`` is stored as text rather than a FK: ``Signature.get_id`` INSERTS, and
    logging a declined signature must not manufacture rows in a table other queries join
    against.

    One row per pair, upserted — the clock runs ``update_all`` every 20 minutes, so
    anything keyed per-run would write the same fact 72 times a day."""

    __tablename__ = "selection"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    signature = db.Column(db.String(512), nullable=False)
    product = db.Column(db.String(32), nullable=False)
    channel = db.Column(db.String(16), nullable=False)
    build_day = db.Column(db.Date, nullable=False)
    # selected | untestable_prefix | below_install_threshold | immature | not_spiking
    # | dropped_no_users
    outcome = db.Column(db.String(24), nullable=False)
    number = db.Column(db.Integer, nullable=False, default=0)
    # Position in the build-day series and whether that position is testable at all
    # (the first `ndays` never are) — the pair that explains an untestable_prefix row.
    position = db.Column(db.Integer, nullable=False, default=0)
    evaluable = db.Column(db.Boolean, nullable=False, default=False)
    baseline = db.Column(pg.JSONB, nullable=False, default=list)
    # {buildid: {"count": n, "installs": k}} — self-describing, so a reader does not need
    # to re-derive which build of that day carried the crashes.
    bids = db.Column(pg.JSONB, nullable=False, default=dict)
    picked = db.Column(db.String(14), nullable=True)
    run_date = db.Column(db.DateTime(timezone=True), nullable=False)
    # `outcome` is the LATEST verdict, and a pair's verdict legitimately changes as its
    # build ages past `mature_after_days` (selected -> immature on the same inputs). These
    # two are sticky, because the upsert would otherwise overwrite the answer this table
    # exists to give: a week later the log would say we declined a signature we had in
    # fact analysed, and possibly filed a bug for.
    ever_selected = db.Column(db.Boolean, nullable=False, default=False)
    first_run_date = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "signature", "product", "channel", "build_day", name="selection_pair"
        ),
        db.Index("selection_build_day_idx", "build_day"),
        db.Index("selection_outcome_idx", "outcome"),
    )

    @staticmethod
    def _row(record, product, channel, run_date):
        bids = {
            utils.get_buildid(bid): {
                "count": count,
                "installs": record["installs"].get(bid, 0),
            }
            for bid, count in record["bids"].items()
        }
        picked = record.get("picked")
        return {
            "signature": record["signature"][:512],
            "product": product,
            "channel": channel,
            "build_day": record["day"].date(),
            "outcome": record["outcome"],
            "number": record["count"],
            "position": record["index"],
            "evaluable": record["evaluable"],
            "baseline": list(record["baseline"]),
            "bids": bids,
            "picked": utils.get_buildid(picked) if picked else None,
            "run_date": run_date,
            "ever_selected": record["outcome"] == utils.SELECTED,
            "first_run_date": run_date,
        }

    # Postgres caps a statement at 65535 bind parameters. A live nightly run emits ~2600
    # records x 15 columns = ~39k, which fits -- but only 2.5x under the cliff, and the row
    # count grows with the window and the signature count. Chunked so the ceiling is a
    # property of this constant instead of a latent failure at some future volume.
    _CHUNK = 1000

    @staticmethod
    def record_many(records, product, channel, run_date=None, commit=True):
        """Upsert one row per record. Never raises: observability must not be able to stop
        the pipeline triaging crashes."""
        if not records:
            return 0
        if run_date is None:
            run_date = datetime.now(timezone.utc)
        try:
            values = [
                Selection._row(record, product, channel, run_date)
                for record in records
            ]
            written = 0
            for start in range(0, len(values), Selection._CHUNK):
                written += Selection._upsert(values[start:start + Selection._CHUNK])
            if commit:
                db.session.commit()
            return written
        except Exception:
            logger.error("Cannot record the selection log", exc_info=True)
            db.session.rollback()
            return 0

    @staticmethod
    def _upsert(values):
        if not values:
            return 0
        ins = pg.insert(Selection).values(values)
        upd = ins.on_conflict_do_update(
            constraint="selection_pair",
            set_=dict(
                outcome=ins.excluded.outcome,
                number=ins.excluded.number,
                position=ins.excluded.position,
                evaluable=ins.excluded.evaluable,
                baseline=ins.excluded.baseline,
                bids=ins.excluded.bids,
                # Sticky: never lose the fact that we DID analyse this pair, nor which
                # build carried it, when a later run downgrades the verdict.
                picked=func.coalesce(ins.excluded.picked, Selection.picked),
                ever_selected=or_(ins.excluded.ever_selected, Selection.ever_selected),
                run_date=ins.excluded.run_date,
                first_run_date=func.coalesce(
                    Selection.first_run_date, ins.excluded.first_run_date
                ),
            ),
        )
        db.session.execute(upd)
        return len(values)

    @staticmethod
    def prune(days=60):
        """Drop rows for build-days older than ``days``. The log is a live view of the
        window, not an archive; without this it grows without bound."""
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
            n = (
                db.session.query(Selection)
                .filter(Selection.build_day < cutoff)
                .delete(synchronize_session=False)
            )
            db.session.commit()
            return n
        except Exception:
            logger.error("Cannot prune the selection log", exc_info=True)
            db.session.rollback()
            return 0

    @staticmethod
    def for_signature(signature, product=None, channel=None, limit=200):
        """Every recorded decision about a signature, most recent build-day first."""
        query = db.session.query(Selection).filter(Selection.signature == signature)
        if product is not None:
            query = query.filter(Selection.product == product)
        if channel is not None:
            query = query.filter(Selection.channel == channel)
        rows = query.order_by(Selection.build_day.desc()).limit(limit).all()
        return [row.to_dict() for row in rows]

    @staticmethod
    def recent(outcome=None, days=14, limit=500):
        """Recent decisions, optionally of one outcome — ``untestable_prefix`` is the
        blind-spot feed, ``immature`` is what the maturity bar is costing."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        query = db.session.query(Selection).filter(Selection.build_day >= cutoff)
        if outcome is not None:
            query = query.filter(Selection.outcome == outcome)
        rows = (
            query.order_by(Selection.build_day.desc(), Selection.number.desc())
            .limit(limit)
            .all()
        )
        return [row.to_dict() for row in rows]

    @staticmethod
    def summary(days=14):
        """``{outcome: count}`` over the last ``days`` build-days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        rows = (
            db.session.query(Selection.outcome, func.count(Selection.id))
            .filter(Selection.build_day >= cutoff)
            .group_by(Selection.outcome)
            .all()
        )
        return {outcome: count for outcome, count in rows}

    def to_dict(self):
        return {
            "signature": self.signature,
            "product": self.product,
            "channel": self.channel,
            "build_day": self.build_day.isoformat() if self.build_day else None,
            "outcome": self.outcome,
            "number": self.number,
            "position": self.position,
            "evaluable": self.evaluable,
            "baseline": self.baseline,
            "bids": self.bids,
            "picked": self.picked,
            "run_date": self.run_date.isoformat() if self.run_date else None,
            "ever_selected": self.ever_selected,
            "first_run_date": (
                self.first_run_date.isoformat() if self.first_run_date else None
            ),
        }


def _unusable_verdict():
    """SQL for "this dossier's verdict says CLOUSEAU broke, not what the crash was"
    (``_UNUSABLE_VERDICT_PREFIXES``).

    NULL-SAFE BY CONSTRUCTION, and that is the whole point of writing it once. 565 of the ~2200
    ``done`` dossiers in prod have no ``abstain_reason`` at all — they are the culprits and the
    leads. A bare ``reason NOT LIKE 'dossier validation failed%'`` evaluates to NULL for those, so
    the row would satisfy neither ``unusable`` nor ``not_(unusable)`` and drop out of BOTH arms:
    every cluster ever closed by a SUCCESSFUL run would silently reopen and be re-analysed at ~$3
    a time. ``reason IS NOT NULL AND (...)`` is FALSE rather than NULL there, so the negation
    keeps the row. Same for a ``done`` row with no dossier payload at all, which is what an
    operator marking a row done by hand leaves behind."""
    reason = Dossier.payload["dossier"]["verdict"]["abstain_reason"].astext
    return and_(
        reason.isnot(None),
        or_(*[reason.like(p + "%") for p in _UNUSABLE_VERDICT_PREFIXES]),
    )


def _cluster_dossiers(signatureid, protohash, channel):
    """Query of the ``done`` dossiers on one proto-signature cluster that are allowed to speak
    for it: instance-suppressed runs (``_INSTANCE_SUPPRESSED``) are excluded, since a broken
    machine or a corrupted fault address says nothing about the next report of the same
    signature. Shared by ``UUID.proto_already_analyzed`` (which asks "has this cluster been
    triaged?") and ``UUID.untriaged`` (which asks the same question of every crash at once), so
    the sweeper can never disagree with the gate about what counts as triaged.

    THE CLUSTER IS PER CHANNEL, and it has to be said in SQL because nothing else says it:
    ``uuids`` has no channel column, and ``protohash = utils.hash(proto_signature)`` is the
    same string on every channel, so before this argument existed whichever channel was
    analysed FIRST closed the cluster for the other one, permanently. The dangerous direction
    is beta closing a NIGHTLY cluster — enabling a second channel would then silently reduce
    desktop coverage. And the two are genuinely different questions: a beta filing is a
    different bug, against a different repo, from a different build, with a different candidate
    window (the cycle's uplifts rather than three days of central). Measured blast radius: of
    the 224 beta (signature, proto) clusters behind 40 emulated selections, 37 (16.5%) also
    occur verbatim on nightly within 60 days — an upper bound, since the gate additionally
    needs the nightly cluster to hold a ``status=done`` dossier.

    ``channel`` may be a plain string or a SQL column expression, so ``untriaged`` can
    correlate it against its own ``builds`` row.

    The sibling join is ALIASED, which is load-bearing for the second caller: ``untriaged``
    correlates this as a subquery against its own ``uuids`` row, and without an alias
    ``UUID.signatureid == signatureid`` would resolve both sides to the same table and be
    trivially true — the cluster test would then match any dossier at all. The sibling's
    ``builds`` join needs its own alias for exactly the same reason."""
    corrob = Dossier.payload["dossier"]["corroborations"]
    sib = aliased(UUID)
    sib_build = aliased(Build)
    return (
        db.session.query(Dossier.id)
        .join(sib, Dossier.uuidid == sib.id)
        .join(sib_build, sib_build.id == sib.buildid)
        .filter(
            sib.signatureid == signatureid,
            sib.protohash == protohash,
            sib_build.channel == channel,
            Dossier.status == "done",
            *[
                or_(corrob[flag].astext.is_(None), corrob[flag].astext != "true")
                for flag in _INSTANCE_SUPPRESSED
            ],
        )
    )


class SweepMark(db.Model):
    """A named cursor, so a periodic sweep can offer each crash to the agent AT MOST ONCE.

    The bound has to be persistent, and it cannot be inferred from the data. The sweep's own
    candidates are crashes with NO dossier row (``UUID.untriaged``), and the two reasons a crash
    ends up there are not distinguishable after the fact: a job lost from Redis (recoverable —
    the Mini plan has no persistence, so a restart drops the queue silently) and a run that
    returned before writing anything (``build_seed`` found nothing to reason about, which will
    happen again identically). Re-offering the first is the point; re-offering the second every
    six hours forever is an unbounded hg/pushlog cost for a guaranteed no-op. Since neither
    leaves a trace, only a cursor can tell them apart from "not yet examined"."""

    __tablename__ = "sweepmarks"

    name = db.Column(db.String(32), primary_key=True)
    position = db.Column(db.Integer, default=0)
    updated = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    def __init__(self, name, position=0):
        self.name = name
        self.position = position

    @staticmethod
    def get(name):
        row = db.session.query(SweepMark.position).filter(SweepMark.name == name).first()
        return row.position if row else 0

    @staticmethod
    def set(name, position, commit=True):
        """Advance the cursor. Never moves BACKWARDS: two sweeps overlapping (a slow pass and the
        next tick) would otherwise let the later-but-behind one rewind the mark and re-offer
        everything between."""
        ins = pg.insert(SweepMark).values(name=name, position=position)
        upd = ins.on_conflict_do_update(
            index_elements=["name"],
            set_={"position": position, "updated": db.func.now()},
            where=SweepMark.position < position,
        )
        db.session.execute(upd)
        if commit:
            db.session.commit()


class UUID(db.Model):
    __tablename__ = "uuids"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuid = db.Column(db.String(36), unique=True)
    buildid = db.Column(db.Integer, db.ForeignKey("builds.id", ondelete="CASCADE"))
    signatureid = db.Column(
        db.Integer, db.ForeignKey("signatures.id", ondelete="CASCADE")
    )
    protohash = db.Column(db.String(56))
    stackhash = db.Column(db.String(56))
    jstackhash = db.Column(db.String(56))
    analyzed = db.Column(db.Boolean, default=False)
    useless = db.Column(db.Boolean, default=False)
    max_score = db.Column(db.Integer, default=0)
    error = db.Column(db.Boolean, default=False)
    created = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    def __init__(self, uuid, signatureid, protohash, buildid):
        self.uuid = uuid
        self.signatureid = signatureid
        self.protohash = protohash
        self.buildid = buildid

    @staticmethod
    def get_info(uuid):
        q = (
            db.session.query(
                UUID.id,
                Build.buildid,
                Build.product,
                Build.channel,
                Build.version,
                Signature.signature,
            )
            .select_from(UUID)
            .join(Build)
            .join(Signature)
        )
        q = q.filter(UUID.uuid == uuid).first()

        return {
            "buildid": utils.get_buildid(q.buildid),
            "product": q.product,
            "channel": q.channel,
            "version": q.version,
            "signature": q.signature,
        }

    @staticmethod
    def reset(uuids):
        qs = db.session.query(UUID.id).filter(UUID.uuid.in_(uuids))
        qs.update(
            {"analyzed": False, "useless": False, "stackhash": "", "jstackhash": ""},
            synchronize_session="fetch",
        )

        res = [q.id for q in qs]
        db.session.commit()

        return res

    @staticmethod
    def set_max_score(uuidid, score, commit=True):
        q = db.session.query(UUID).filter(UUID.id == uuidid)
        q.update({"max_score": score})
        if commit:
            db.session.commit()

    @staticmethod
    def set_error(uuid, commit=True):
        q = db.session.query(UUID).filter(UUID.uuid == uuid)
        q.update({"error": True})
        if commit:
            db.session.commit()

    @staticmethod
    def add(uuid, signatureid, proto, buildid, commit=True):
        ret = True
        protohash = utils.hash(proto)
        q = (
            db.session.query(UUID)
            .filter(
                UUID.signatureid == signatureid,
                UUID.protohash == protohash,
                UUID.buildid == buildid,
            )
            .first()
        )
        ret = not bool(q)
        if ret:
            ins = pg.insert(UUID).values(
                uuid=uuid, signatureid=signatureid, protohash=protohash, buildid=buildid
            )
            upd = ins.on_conflict_do_update(
                index_elements=["uuid"],
                set_=dict(
                    signatureid=signatureid, protohash=protohash, buildid=buildid
                ),
            )
            db.session.execute(upd)
            if commit:
                db.session.commit()

        return ret

    @staticmethod
    def proto_already_analyzed(uuid):
        """True if any UUID sharing this uuid's ``(signatureid, protohash)`` already has a
        DONE Dossier — i.e. the proto-signature has been SUCCESSFULLY triaged. Lets the
        evidence agent run ONCE per proto-signature cluster instead of once per crash
        uuid: the same crash recurring on a newer nightly build (a different uuid, same
        proto) is not worth paying to re-triage. Deliberately ignores ``buildid`` (dedup
        across builds).

        Only a ``status == "done"`` dossier counts. A ``running``/``error``/``pending``
        sibling must NOT suppress the cluster: ``run_evidence_agent`` commits a
        ``running`` row before the LLM call and flips it to ``error`` on the transient
        failures this pipeline isolates against (and a killed worker can leave one stuck
        at ``running``). Counting those would let a single failed/stuck run permanently
        drop every other uuid in the cluster; keying on ``done`` instead means an errored
        first run is naturally retried by the next same-proto uuid. Returns False for an
        unknown/proto-less uuid (so it never blocks a first run).

        A run suppressed for a reason specific to ITS OWN CRASH REPORT does not count either
        (``_INSTANCE_SUPPRESSED``). This is what stops a bad machine silencing a real bug: the
        bad-machine and bit-flip gates conclude "this REPORT is noise" — one broken installation,
        one corrupted fault address — which says nothing about the next report of the same
        signature from a healthy machine. Without this, one scattergun crash arriving first would
        close the cluster and every later uuid in it would be skipped, turning a false negative
        from a delay into a permanent loss. The backout gate is deliberately NOT in the list: it
        suppresses on the CANDIDATE being gone from the tree, which is equally true for every
        crash in the cluster.

        Nor does a run that BROKE (``_UNUSABLE_VERDICT_PREFIXES``) — reaching ``done`` is not
        the same as having triaged anything. A dossier that failed validation, or whose agent
        returned no readable handoff, carries a forced abstain that says nothing whatsoever
        about the crash; counting it closed the cluster over a CLOUSEAU-side failure, which is
        the one reason that has nothing to do with the crash at all. This was live for a
        month: a $4.35 run on 2026-07-20 died on a ``stack_frame.node`` field that commit
        421e484 has since made optional, and it was still, on 2026-08-12, the reason three
        later indexedDB crashes were never looked at.

        Bounded, though, by ``agent.proto_max_unusable``: a cluster whose stack reliably
        breaks the schema would otherwise re-pay ~$3 for every new uuid in it forever. Once
        that many broken runs have accumulated the cluster is treated as triaged, loudly."""
        row = (
            db.session.query(UUID.signatureid, UUID.protohash, Build.channel)
            .select_from(UUID)
            .join(Build, Build.id == UUID.buildid)
            .filter(UUID.uuid == uuid)
            .first()
        )
        if not row or not row.protohash:
            return False
        q = _cluster_dossiers(row.signatureid, row.protohash, row.channel)
        if db.session.query(q.filter(not_(_unusable_verdict())).exists()).scalar():
            return True
        broken = q.filter(_unusable_verdict()).count()
        cap = config.get_agent_proto_max_unusable()
        # `cap and ...`, not `broken and ...`: cap 0 means "retry without a bound", so it has
        # to disable the check rather than satisfy `broken >= 0` on the first failure.
        if cap and broken >= cap:
            # Not silent: this is the cluster being abandoned, and the only place it is
            # visible. A cap that fires unseen reads exactly like the bug fixed above.
            logger.warning(
                "agent: %d broken run(s) on this proto-signature (cap %d); "
                "treating the cluster as triaged and skipping %s",
                broken,
                cap,
                uuid,
            )
            return True
        return False

    @staticmethod
    def untriaged(after_id, min_age_s, max_age_s, limit, channels=None):
        """Ingested crashes that have NO dossier at all and whose proto-signature cluster has
        never been usably triaged — i.e. the ones the pipeline would analyse if it were offered
        them again. Returns ``[(id, uuid, channel)]`` in id order.

        Measured on prod 2026-08-12: 86 of these, ~3.4/day, arriving in bursts (8 on 07-24, 8 on
        08-07). 16 carried an on-stack score, which means ``build_seed`` would have produced a
        seed and the run would have written a dossier — so for those the JOB never ran at all.
        With Redis on the Mini plan (``Persistence: None``) a restart drops the whole queue, and a
        job lost that way leaves no trace anywhere: no dossier, no error, nothing in the log. A
        periodic sweep is the only thing that can notice.

        ``min_age_s`` is a grace period, and it is not optional: a crash whose job is simply
        QUEUED also has no dossier row (``claim_running`` is what inserts one), and the queue runs
        hours deep — three workers at ~16 minutes drain ~11/hour, so the tail of one ingestion
        batch legitimately waits well over an hour. Without the grace the sweep would re-enqueue
        the entire live queue on every tick.

        ``max_age_s`` bounds the other end: a crash from a month ago is on a build nobody is
        shipping any more, and paying ~$3 to triage it is worth less than the same money spent on
        today's.

        ``channels`` restricts to the channels the agent will actually accept. Filtered HERE
        rather than after the query so the caller's per-tick cap is spent on crashes that can be
        enqueued: with beta ingestion on, an unfiltered ``limit`` of 3 could return three beta
        crashes, which ``enqueue_agent`` drops on the floor — starving the nightly ones behind
        them while the log claims three were swept."""
        now = datetime.now(timezone.utc)
        rows = (
            db.session.query(UUID.id, UUID.uuid, Build.channel)
            .select_from(UUID)
            .join(Build, Build.id == UUID.buildid)
            .outerjoin(Dossier, Dossier.uuidid == UUID.id)
            .filter(
                Dossier.id.is_(None),
                UUID.id > after_id,
                UUID.useless.is_(False),
                UUID.analyzed.is_(True),
                UUID.protohash.isnot(None),
                UUID.created <= now - timedelta(seconds=min_age_s),
                UUID.created >= now - timedelta(seconds=max_age_s),
                # The cluster test, as a correlated NOT EXISTS so one query answers it for every
                # candidate. Deliberately the SAME predicate `proto_already_analyzed` uses, minus
                # the broken-run cap: a cluster at the cap is closed by the gate anyway, so
                # enqueuing it would just be skipped — harmlessly, and one log line louder.
                # ...and per CHANNEL: `Build` is already joined here as the candidate's own
                # build, so `Build.channel` correlates and a nightly dossier can no longer
                # answer for a beta crash (or the reverse).
                ~_cluster_dossiers(UUID.signatureid, UUID.protohash, Build.channel)
                .filter(not_(_unusable_verdict()))
                .exists(),
            )
        )
        if channels:
            rows = rows.filter(Build.channel.in_(list(channels)))
        rows = rows.order_by(UUID.id).limit(limit).all()
        return [(r.id, r.uuid, r.channel) for r in rows]

    @staticmethod
    def add_stack_hash(uuid, sh, jsh, commit=True):
        q = db.session.query(UUID).filter(UUID.uuid == uuid)
        if sh:
            q.update({"stackhash": sh})
        elif jsh:
            q.update({"jstackhash": jsh})
        if commit:
            db.session.commit()

    @staticmethod
    def set_analyzed(uuid, useless, commit=True):
        q = db.session.query(UUID).filter(UUID.uuid == uuid)
        q.update({"useless": useless, "analyzed": True})
        if commit:
            db.session.commit()

    @staticmethod
    def to_analyze(report_uuid):
        """The next report to score, as ``(uuid, buildid, channel, product, node)``.

        TWO PREDICATES THAT LOOK COSMETIC AND ARE NOT, because this feeds a SERIAL,
        SELF-RE-ENQUEUING chain (``update.analyze_one_report`` calls ``analyze_reports``,
        which enqueues the next one):

        * ``error.is_(False)``. ``set_error`` sets only ``error=True`` and leaves
          ``analyzed=False``, so a report that fails every time -- a crash whose
          ``json_dump`` Socorro cannot serve, an hg fetch that keeps 406ing -- was handed
          straight back to the chain on the next tick, forever, and NOTHING ELSE ever got
          analysed. A livelock, on any channel, with no log line saying so.
        * ``ORDER BY id``. Without it the row is whatever Postgres finds first, which is
          stable enough with one channel to hide the problem and stops being stable the
          moment there are two: a beta backlog could interleave arbitrarily with nightly's
          and starve it. Oldest-first is also the order that drains a backlog.

        A specific ``report_uuid`` bypasses both -- that is ``redo.reset``/``single.py``
        asking for one named report on purpose, including a retry of an errored one."""
        uuid = (
            db.session.query(
                UUID.uuid, Build.buildid, Build.channel, Build.product, Node.node
            )
            .select_from(UUID)
            .join(Build)
            .join(Node)
        )
        if report_uuid:
            uuid = uuid.filter(UUID.uuid == report_uuid).first()
        else:
            uuid = (
                uuid.filter(UUID.analyzed.is_(False), UUID.error.is_(False))
                .order_by(UUID.id)
                .first()
            )
        return uuid

    @staticmethod
    def get_bid_chan(uuid):
        r = (
            db.session.query(UUID.id, Build.buildid, Build.channel)
            .select_from(UUID)
            .join(Build)
        )
        r = r.filter(UUID.uuid == uuid, UUID.useless.is_(False)).first()
        return r.buildid.astimezone(pytz.utc), r.channel

    @staticmethod
    def get_bid_chan_by_id(uuidid):
        r = (
            db.session.query(
                UUID.uuid, Signature.signature, Build.buildid, Build.channel, Node.node
            )
            .select_from(UUID)
            .join(Build)
            .join(Node)
            .join(Signature)
        )
        r = r.filter(UUID.id == uuidid).first()
        if r:
            return {
                "uuid": r.uuid,
                "signature": r.signature,
                "buildid": r.buildid.astimezone(pytz.utc),
                "channel": r.channel,
                "node": r.node,
            }
        return {}

    @staticmethod
    def get_bid_chan_by_uuid(uuid):
        r = (
            db.session.query(
                UUID.id,
                UUID.jstackhash,
                Signature.signature,
                Build.buildid,
                Build.product,
                Build.channel,
                Node.node,
            )
            .select_from(UUID)
            .join(Build)
            .join(Node)
            .join(Signature)
        )
        r = r.filter(
            UUID.uuid == uuid, UUID.useless.is_(False), UUID.analyzed.is_(True)
        ).first()
        if r:
            return {
                "uuid": uuid,
                "id": r.id,
                "signature": r.signature,
                "buildid": r.buildid.astimezone(pytz.utc),
                "channel": r.channel,
                "product": r.product,
                "java": bool(r.jstackhash),
                "node": r.node,
            }
        return {}

    @staticmethod
    def get_uuids_from_buildid(buildid, product, channel):
        sbid = buildid
        buildid = utils.get_build_date(buildid)
        uuids = db.session.query(
            UUID.uuid, UUID.max_score, Signature.signature, Stats.number, Stats.installs
        ).select_from(UUID)
        uuids = uuids.join(Signature).join(Build)
        uuids = uuids.join(
            Stats, db.and_(Signature.id == Stats.signatureid, Build.id == Stats.buildid)
        )
        uuids = (
            uuids.filter(
                Build.buildid == buildid,
                Build.product == product,
                Build.channel == channel,
                UUID.useless.is_(False),
                UUID.analyzed.is_(True),
            )
            .distinct(UUID.id)
            .order_by(UUID.id)
        )

        _res = {}
        for uuid in uuids:
            t = (uuid.uuid, uuid.max_score)
            if uuid.signature in _res:
                _res[uuid.signature]["uuids"].append(t)
            else:
                _res[uuid.signature] = {
                    "uuids": [t],
                    "number": uuid.number,
                    "installs": uuid.installs,
                    "url": utils.make_url_for_signature(
                        uuid.signature, buildid, sbid, channel, product
                    ),
                }
        res = sorted(
            _res.items(),
            key=lambda p: (-p[1]["number"], -p[1]["installs"], p[0].lower()),
        )
        return res

    @staticmethod
    def get_uuids_from_buildid_no_score(buildid, product, channel):
        sbid = buildid
        buildid = utils.get_build_date(buildid)
        uuids = db.session.query(
            UUID.uuid, Signature.signature, Stats.number, Stats.installs
        ).select_from(UUID)
        uuids = uuids.join(Signature).join(Build)
        uuids = uuids.join(
            Stats, db.and_(Signature.id == Stats.signatureid, Build.id == Stats.buildid)
        )
        uuids = (
            uuids.filter(
                Build.buildid == buildid,
                Build.product == product,
                Build.channel == channel,
                UUID.useless.is_(True),
                UUID.analyzed.is_(True),
            )
            .distinct(UUID.id)
            .order_by(UUID.id)
        )

        _res = {}
        for uuid in uuids:
            if uuid.signature in _res:
                _res[uuid.signature]["uuids"].append(uuid.uuid)
            else:
                _res[uuid.signature] = {
                    "uuids": [uuid.uuid],
                    "number": uuid.number,
                    "installs": uuid.installs,
                    "url": utils.make_url_for_signature(
                        uuid.signature, buildid, sbid, channel, product
                    ),
                }
        res = sorted(
            _res.items(),
            key=lambda p: (-p[1]["number"], -p[1]["installs"], p[0].lower()),
        )
        return res

    @staticmethod
    def clean(date, channel):
        date = datetime(date.year, date.month, date.day)
        date += relativedelta(days=config.get_ndays())
        db.session.query(UUID).filter(
            UUID.buildid <= date, UUID.channel == channel
        ).delete()
        db.session.commit()

    @staticmethod
    def get_id(uuid):
        return db.session.query(UUID.id).filter(UUID.uuid == uuid).first()[0]

    @staticmethod
    def is_stackhash_existing(stackhash, buildid, channel, product, java):
        if java:
            r = (
                db.session.query(UUID.id)
                .join(Build)
                .filter(
                    UUID.jstackhash == stackhash,
                    Build.buildid == buildid,
                    Build.channel == channel,
                    Build.product == product,
                )
                .first()
            )
        else:
            r = (
                db.session.query(UUID.id)
                .join(Build)
                .filter(
                    UUID.stackhash == stackhash,
                    Build.buildid == buildid,
                    Build.channel == channel,
                    Build.product == product,
                )
                .first()
            )
        return r is not None

    @staticmethod
    def get_buildids_from_pc(product, channel):
        bids = db.session.query(UUID.id, Build.buildid).select_from(UUID).join(Build)
        bids = (
            bids.filter(
                Build.product == product,
                Build.channel == channel,
                UUID.useless.is_(False),
                UUID.analyzed.is_(True),
            )
            .distinct(Build.buildid)
            .order_by(Build.buildid.desc())
        )
        res = [utils.get_buildid(bid.buildid) for bid in bids]
        return res

    @staticmethod
    def get_buildids(no_score=False):
        bids = (
            db.session.query(
                UUID.id, Build.product, Build.channel, Build.buildid, Build.version
            )
            .select_from(UUID)
            .join(Build)
        )
        bids = (
            bids.filter(UUID.useless.is_(no_score), UUID.analyzed.is_(True))
            .distinct(Build.product, Build.channel, Build.buildid)
            .order_by(Build.buildid.desc())
        )
        res = {}
        for bid in bids:
            b = utils.get_buildid(bid.buildid)
            if bid.product in res:
                r = res[bid.product]
                if bid.channel in r:
                    r[bid.channel].append([b, bid.version])
                else:
                    r[bid.channel] = [[b, bid.version]]
            else:
                res[bid.product] = {bid.channel: [[b, bid.version]]}
        return res


class Score(db.Model):
    __tablename__ = "scores"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    changesetid = db.Column(
        db.Integer, db.ForeignKey("changesets.id", ondelete="CASCADE")
    )
    crashstackid = db.Column(
        db.Integer, db.ForeignKey("crashstack.id", ondelete="CASCADE")
    )
    score = db.Column(db.Integer)

    def __init__(self, changesetid, crashstackid, score):
        self.changesetid = changesetid
        self.crashstackid = crashstackid
        self.score = score

    @staticmethod
    def set(data):
        for changesetid, crashstackid, score in data:
            db.session.add(Score(changesetid, crashstackid, score))
        db.session.commit()

    @staticmethod
    def get_by_score(score):
        qs = (
            db.session.query(Score, UUID.uuid)
            .select_from(Score)
            .join(CrashStack)
            .join(UUID)
        )
        qs = qs.filter(Score.score == score).distinct(UUID.id)
        res = [uuid for _, uuid in qs]
        return res


class CrashStack(db.Model):
    __tablename__ = "crashstack"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuidid = db.Column(db.Integer, db.ForeignKey("uuids.id", ondelete="CASCADE"))
    java = db.Column(db.Boolean)
    stackpos = db.Column(db.Integer)
    original = db.Column(db.String(512))
    module = db.Column(db.String(128))
    filename = db.Column(db.String(512))
    function = db.Column(db.Text)
    line = db.Column(db.Integer)
    node = db.Column(db.String(12))
    internal = db.Column(db.Boolean)

    def __init__(
        self,
        uuidid,
        stackpos,
        java,
        original,
        module,
        filename,
        function,
        line,
        node,
        internal,
    ):
        self.uuidid = uuidid
        self.stackpos = stackpos
        self.java = java
        self.original = original
        self.module = module
        self.filename = filename
        self.function = function
        self.line = line
        self.node = node
        self.internal = internal

    @staticmethod
    def delete(ids):
        db.session.query(CrashStack).filter(CrashStack.uuidid.in_(ids)).delete(
            synchronize_session=False
        )
        db.session.commit()

    @staticmethod
    def put_frames(uuid, frames, java, commit=True, channel=None):
        css = []
        uuidid = UUID.get_id(uuid)
        for frame in frames["frames"]:
            cs = CrashStack(
                uuidid,
                frame["stackpos"],
                java,
                frame["original"],
                frame["module"],
                frame["filename"],
                frame["function"],
                frame["line"],
                frame["node"],
                frame["internal"],
            )
            db.session.add(cs)
            css.append((cs, frame))

        db.session.commit()
        max_score = 0
        for cs, frame in css:
            csets = frame["changesets"]
            if csets:
                scores = Changeset.get_scores(
                    frame["filename"], frame["line"], csets, cs.id, channel=channel
                )
                if scores:
                    Score.set(scores)
                    scores = max(s for _, _, s in scores)
                    max_score = max(max_score, scores)
                else:
                    logger.warning(
                        "No scores for {} at line {} and changesets {} (uuid {})".format(
                            frame["filename"], frame["line"], csets, uuid
                        )
                    )

        UUID.set_max_score(uuidid, max_score)

    @staticmethod
    def get_by_uuid(uuid):
        uuid_info = UUID.get_bid_chan_by_uuid(uuid)
        if not uuid_info:
            return {}, {}

        uuidid = uuid_info["id"]
        repo_url = Mercurial.get_repo_url(uuid_info["channel"])
        is_java = uuid_info["java"]

        iframes = (
            db.session.query(
                CrashStack.stackpos,
                Node.node,
                Node.backedout,
                Node.pushdate,
                Node.bug,
                Node.id,
                Score.score,
            )
            .select_from(CrashStack)
            .join(Score)
            .join(Changeset)
            .join(Node)
        )
        iframes = iframes.filter(
            CrashStack.uuidid == uuidid, CrashStack.java.is_(is_java)
        ).order_by(CrashStack.stackpos, Node.id.desc())
        frames = (
            db.session.query(CrashStack)
            .filter(CrashStack.uuidid == uuidid, CrashStack.java.is_(is_java))
            .order_by(CrashStack.stackpos)
        )
        stack = []
        res = {"frames": stack}
        for frame in frames:
            url, filename = utils.get_file_url(
                repo_url, frame.filename, frame.node, frame.line, frame.original
            )
            stack.append(
                {
                    "stackpos": frame.stackpos,
                    "filename": filename,
                    "function": frame.function,
                    # The binary the frame is in (e.g. ``xul.dll``) -- Socorro puts it
                    # between the frame number and the function in the stack it pre-fills
                    # into a crash bug, so the bug comment (report_bug) needs it too.
                    "module": frame.module,
                    "changesets": OrderedDict(),
                    "line": frame.line,
                    "node": frame.node,
                    "original": frame.original,
                    "internal": frame.internal,
                    "url": url,
                }
            )

        for stackpos, node, bout, pdate, bugid, nodeid, score in iframes:
            stack[stackpos]["changesets"][node] = {
                "score": score,
                "backedout": bout,
                "pushdate": pdate,
                "bugid": bugid,
            }

        return res, uuid_info


class Dossier(db.Model):
    __tablename__ = "dossiers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuidid = db.Column(
        db.Integer,
        db.ForeignKey("uuids.id", ondelete="CASCADE"),
        index=True,
        unique=True,
    )
    schema_version = db.Column(db.Integer, default=DOSSIER_SCHEMA_VERSION)
    payload = db.Column(pg.JSONB)
    status = db.Column(AGENT_STATUS_TYPE, default="pending")
    worker_models = db.Column(pg.JSONB, default=list)
    seed_score = db.Column(db.Integer, nullable=True)
    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cache_read_tokens = db.Column(db.Integer, default=0)
    cost_usd = db.Column(db.Numeric(10, 4), nullable=True)
    created = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )
    updated = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now(),
    )

    __table_args__ = (db.UniqueConstraint("uuidid", name="uix_dossiers_uuidid"),)

    @staticmethod
    def upsert(
        uuid,
        payload=None,
        status=None,
        worker_models=None,
        seed_score=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cost_usd=None,
        commit=True,
    ):
        """Create or update a dossier in one atomic statement.

        WARNING: ``payload`` REPLACES the stored JSONB wholesale — it is not merged, EXCEPT for
        the keys in ``_STICKY_PAYLOAD_KEYS``. Any other key a previous write put there
        (``reap_attempts``, ``job_id``, ``error``) is gone unless the caller carries it forward.
        This is what made the reaper's recovery rate unmeasurable: a recovered run's finished
        payload dropped ``reap_attempts``, so the counter only ever survived on runs that FAILED,
        and "0 of 28 reaped dossiers reached done" was a tautology rather than a measurement."""
        uuidid = UUID.get_id(uuid)
        provided = {
            k: v
            for k, v in (
                ("payload", payload),
                ("status", status),
                ("worker_models", worker_models),
                ("seed_score", seed_score),
                ("input_tokens", input_tokens),
                ("output_tokens", output_tokens),
                ("cache_read_tokens", cache_read_tokens),
                ("cost_usd", cost_usd),
            )
            if v is not None
        }
        ins = pg.insert(Dossier).values(
            uuidid=uuidid, schema_version=DOSSIER_SCHEMA_VERSION, **provided
        )
        set_ = {**provided, "updated": db.func.now()}
        if payload is not None:
            set_["payload"] = Dossier._carry_sticky(ins)
        upd = ins.on_conflict_do_update(index_elements=["uuidid"], set_=set_)
        db.session.execute(upd)
        if commit:
            db.session.commit()

    # Payload keys that OUTLIVE a re-run of the same crash. Exactly one, and the argument for it
    # is a bug on file.
    #
    # `filed_bug` is the pair of idempotence keys that stop us posting to Bugzilla twice
    # (`already_filed`, per uuid; `already_commented`, per bug+signature). Both read it out of
    # THIS column. A successful re-run replaced the payload and took the record with it, so a
    # retriggered run could not see its own filing: on 2026-08-24 a retrigger of
    # `a4ac8c69-7b93-48e5-8e5d-9a1ae0260819` posted a second copy of its analysis onto bug
    # 2065072, which we had filed from that same crash four days earlier, and the component owner
    # replied "No need for it to post again."
    #
    # The asymmetry that hid it for so long: the REAPER path was always safe, because a crashed
    # run never reaches this write, so `filed_bug` survived exactly the case the guard was built
    # for. Only a re-run that SUCCEEDS destroyed it.
    #
    # And why the set is not larger: `reset_for_retrigger` deliberately POPS `reap_attempts`
    # ("an operator retrigger earns a fresh give-up budget") and `error` and `run_started`. Making
    # any of those sticky here would silently undo a deliberate clear. Stickiness is only correct
    # for a fact about the OUTSIDE WORLD -- something we did to Bugzilla cannot be un-done by
    # re-running the analysis.
    _STICKY_PAYLOAD_KEYS = ("filed_bug",)

    @staticmethod
    def _carry_sticky(ins):
        """The ON CONFLICT payload expression: sticky keys from the STORED row, everything else
        from the proposed one.

        ``jsonb_strip_nulls`` is what makes an absent key absent rather than an explicit
        ``null`` -- without it a first write would store ``{"filed_bug": null}`` and
        ``payload ? 'filed_bug'`` (``filed_bugs_since``, ``filed_bug_rows``) would start
        matching every dossier ever written. The proposed payload is on the RIGHT of ``||`` so a
        caller that genuinely supplies a new ``filed_bug`` still wins."""
        # `->` explicitly, not `payload[key]`: SQLAlchemy renders the subscript form, which is
        # jsonb subscripting and needs PG14+, and the value MUST come back as jsonb -- via
        # `->>` it would be text and `jsonb_build_object` would store the whole record as a
        # JSON string.
        args = []
        for key in Dossier._STICKY_PAYLOAD_KEYS:
            args.append(key)
            args.append(Dossier.payload.op("->", return_type=pg.JSONB)(key))
        carried = db.func.jsonb_strip_nulls(db.func.jsonb_build_object(*args))
        return carried.op("||", return_type=pg.JSONB)(ins.excluded.payload)

    @staticmethod
    def set_status(uuid, status, error=None, commit=True):
        """Set a dossier's status. When ``error`` is given (the failure path), the short
        reason is stashed in the JSONB ``payload`` (key ``error``) so it OUTLIVES the
        ephemeral worker-log buffer and can surface in the tasks view — the row has no
        dedicated error column, and there is no migration framework here. Reassigns
        payload to a new dict so SQLAlchemy flags the column dirty (see ``set_job_id``)."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return
        d = db.session.query(Dossier).filter(Dossier.uuidid == uuidid).first()
        if d is None:
            return
        d.status = status
        d.updated = db.func.now()
        if error is not None:
            payload = dict(d.payload or {})
            payload["error"] = str(error)[:1000]
            d.payload = payload
        if commit:
            db.session.commit()

    @staticmethod
    def set_job_id(uuid, job_id, commit=True):
        """Record the RQ job id (in payload) while a triage runs, so the tasks view's
        retrigger can stop the in-flight job. Best-effort: no-op if the row isn't there.
        Reassigns payload to a new dict so SQLAlchemy flags the JSONB column dirty."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return
        d = db.session.query(Dossier).filter(Dossier.uuidid == uuidid).first()
        if d is None:
            return
        payload = dict(d.payload or {})
        payload["job_id"] = job_id
        # Stamp when THIS attempt started. `created` is the row's first-ever ingest and
        # `reset_for_retrigger` deliberately leaves it alone, so on a retriggered crash it
        # can be days old -- the tasks view computed duration from it and showed a run that
        # started 20 minutes ago as "29h running". Written here because this is the first
        # thing after `claim_running`, so it marks the start of the attempt that owns the
        # row, and it is rewritten on every attempt.
        payload["run_started"] = datetime.now(timezone.utc).isoformat()
        d.payload = payload
        if commit:
            db.session.commit()

    @staticmethod
    def merge_payload(uuid, values, commit=True):
        """MERGE ``values`` into the JSONB ``payload``, keeping every other key —
        the opposite of ``upsert``, which replaces the payload wholesale. For the
        failure paths, which have forensics to record (the agent's raw final text, the
        turn count) on a row whose existing keys — ``error``, ``job_id``,
        ``reap_attempts`` — are exactly what makes the failure diagnosable. A ``None``
        value is skipped, not stored, so a caller can pass an optional field
        unconditionally. Best-effort: no-op if the row isn't there. Reassigns payload
        to a new dict so SQLAlchemy flags the JSONB column dirty (see ``set_job_id``).

        Joins rather than going through ``UUID.get_id``, which does NOT return None for
        an unknown uuid — it subscripts ``.first()`` and raises ``TypeError``. Callers
        that guard on ``if uuidid is None`` are therefore guarding against nothing, and
        this one is reached from the failure path, where the row is the least certain to
        exist. One query instead of two, and the no-op is real."""
        d = (
            db.session.query(Dossier)
            .join(UUID, Dossier.uuidid == UUID.id)
            .filter(UUID.uuid == uuid)
            .first()
        )
        if d is None:
            return
        payload = dict(d.payload or {})
        payload.update({k: v for k, v in values.items() if v is not None})
        d.payload = payload
        if commit:
            db.session.commit()

    @staticmethod
    def reset_for_retrigger(uuid, commit=True):
        """Mark a dossier ``pending`` (dropping any recorded job_id) so a forced retrigger
        can re-claim it via the atomic ``claim_running`` -- ``done``/``error``/fresh
        ``running`` are otherwise not claimable. Routing the retrigger back through the
        same atomic claim is what collapses two concurrent retriggers of one uuid into a
        single run (exactly one wins pending->running): no double-pay. Also clears the
        reaper attempt counter so an operator retrigger earns a fresh give-up budget.

        Clears ``error`` for the same reason: an operator asked to start this run over, so
        the PREVIOUS failure is no longer this row's state. Left in, it outlives the run
        that produced it — ``upsert`` only drops it on success, by replacing the payload
        wholesale — and the tasks view renders ``error`` for any status, so a queued,
        perfectly healthy retrigger displays the failure it was meant to repair until the
        moment it finishes. During a bulk recovery that reads as an ongoing outage.

        Note this is cleared HERE and not filtered in the view: ``set_status(uuid,
        "pending", error=...)`` on the transient-retry path is a pending row whose error
        is current and worth showing. The difference is not the status, it is whether the
        failure still describes what the row is doing."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return
        d = db.session.query(Dossier).filter(Dossier.uuidid == uuidid).first()
        if d is None:
            return
        d.status = "pending"
        d.updated = datetime.now(timezone.utc)
        payload = dict(d.payload or {})
        payload.pop("job_id", None)
        payload.pop("reap_attempts", None)
        payload.pop("error", None)
        # And the previous attempt's start, or a row that was queued seconds ago renders
        # the elapsed time of the run it is replacing — the same lie, from the other end,
        # as timing from `created`. The next attempt stamps a fresh one at `set_job_id`.
        payload.pop("run_started", None)
        d.payload = payload
        if commit:
            db.session.commit()

    @staticmethod
    def bump_reap_attempts(uuid, commit=True):
        """Increment (in the JSONB ``payload``, no migration) the count of times the
        reaper has re-enqueued this stuck dossier, and return the new count. Lets the
        reaper GIVE UP on a crash that keeps orphaning instead of re-enqueuing it
        forever. Reset by ``reset_for_retrigger`` (an operator retrigger earns a fresh
        budget). Returns 0 if the row is absent.

        This bookkeeping write MUST NOT refresh ``updated``. ``updated`` is the liveness
        clock the re-enqueued run reads back (``skip_triage``, then ``claim_running``) to
        decide whether the orphan is still owned by a live worker — so stamping it here
        made the reaper announce "this dossier is alive as of now" a fraction of a second
        before scheduling the retry, and every retry then skipped ITSELF as a duplicate.
        Measured cost of that: 0 recoveries out of 28 reaped dossiers, each one burning
        its attempts and settling to ``error``.

        ``updated`` carries ``onupdate=now()``, so simply not assigning it is NOT enough —
        an ORM flush of the dirty row re-stamps it anyway. Hence the explicit Core UPDATE
        that writes the column back to its own value."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return 0
        d = db.session.query(Dossier).filter(Dossier.uuidid == uuidid).first()
        if d is None:
            return 0
        payload = dict(d.payload or {})
        n = int(payload.get("reap_attempts", 0) or 0) + 1
        payload["reap_attempts"] = n
        db.session.execute(
            db.update(Dossier)
            .where(Dossier.uuidid == uuidid)
            .values(payload=payload, updated=Dossier.updated)
        )
        db.session.expire(d)  # the row changed under the identity map
        if commit:
            db.session.commit()
        return n

    @staticmethod
    def get_reap_attempts(uuid):
        """How many times the reaper has re-enqueued this dossier (0 if never, or if the row
        is absent). Read just before a run's settling write so the count can be carried into
        the FINISHED payload — see the caller in ``run_evidence_agent``, and the warning on
        ``upsert`` about the payload being replaced wholesale.

        Like the other read helpers here this leaves its transaction open for the session to
        close, so don't call it immediately before something long-blocking."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return 0
        row = (
            db.session.query(Dossier.payload)
            .filter(Dossier.uuidid == uuidid)
            .first()
        )
        if row is None:
            return 0
        return int((row[0] or {}).get("reap_attempts", 0) or 0)

    @staticmethod
    def heartbeat(uuid):
        """Stamp ``updated`` on a RUNNING dossier: "this run was still alive just now".

        Without a heartbeat ``updated`` records when the run STARTED, so an abandoned dossier
        cannot say whether it died after 2 minutes or after 29 — and that is exactly the case
        that is hard to diagnose, because RQ's SIGKILL at ``job_timeout`` beats the error
        handler, leaving ``error`` and ``cost_usd`` NULL with no other trace.

        Guarded on ``status='running'``, so it can never resurrect a dossier that has already
        settled to ``done``/``error``, and one UPDATE with no prior SELECT so it cannot race
        with the run's own writes. Returns True iff a row was stamped.

        NOTE this changes what the orphan reaper measures FROM: the stale window now starts at
        the last beat rather than at the run's start, so a run that dies late is reaped later.
        That is the correct semantics (``updated`` finally means "last known alive"), and it is
        what would let ``_STALE_BUFFER_S`` be decoupled from ``job_timeout`` and cut right down
        — a dead run becomes detectable in a few missed beats instead of 35 minutes."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return False
        res = db.session.execute(
            db.update(Dossier)
            .where(Dossier.uuidid == uuidid, Dossier.status == "running")
            .values(updated=db.func.now())
        )
        db.session.commit()
        return res.rowcount > 0

    @staticmethod
    def add_usage(
        uuid,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cost_usd=None,
        commit=True,
    ):
        uuidid = UUID.get_id(uuid)
        q = db.session.query(Dossier).filter(Dossier.uuidid == uuidid)
        upd = {
            "input_tokens": Dossier.input_tokens + input_tokens,
            "output_tokens": Dossier.output_tokens + output_tokens,
            "cache_read_tokens": Dossier.cache_read_tokens + cache_read_tokens,
            "updated": db.func.now(),
        }
        if cost_usd is not None:
            upd["cost_usd"] = cost_usd
        q.update(upd, synchronize_session=False)
        if commit:
            db.session.commit()

    @staticmethod
    def get_by_uuid(uuid):
        return (
            db.session.query(Dossier)
            .join(UUID, Dossier.uuidid == UUID.id)
            .filter(UUID.uuid == uuid)
            .first()
        )

    @staticmethod
    def get_pending(limit=1):
        return (
            db.session.query(Dossier)
            .filter(Dossier.status == "pending")
            .order_by(Dossier.created)
            .limit(limit)
            .all()
        )

    @staticmethod
    def skip_triage(uuid, stale_after_s, own_job_id=None):
        """Whether triage of ``uuid`` should be SKIPPED: it is already ``done``/``error``,
        or a run is genuinely in progress (``running`` and ``updated`` within
        ``stale_after_s``). A ``running`` dossier OLDER than that is an orphan — its
        worker died mid-run (e.g. a Heroku dyno restart, which SIGKILLs before the
        exception handler can mark ``error``) — so it is NOT skipped and gets retried;
        ``pending`` retries too. ``updated`` is the run's heartbeat (stamped at the claim,
        then every couple of minutes for as long as the run lives), so a ``running`` row
        that has not been stamped within the window is dead, not merely slow.

        NOTE this makes ``updated`` load-bearing for recovery, not just for reporting: any
        write that refreshes it on an orphan tells the retry "someone is already on it"
        and the retry skips itself. See ``bump_reap_attempts``.

        ``own_job_id`` mirrors the arm in ``claim_running`` and has to be here too, or that
        one is unreachable for every ordinary (non-forced) job: this early-out runs ~30
        lines before the claim, so a deploy-killed ingestion run would still skip itself
        here and still wait out the reaper. Same bound for the same reason — the row must
        have gone quiet for ``_OWN_JOB_RECLAIM_AFTER_S``, so a horse that is still beating
        is never treated as ours to take back."""
        d = Dossier.get_by_uuid(uuid)
        if d is None:
            return False
        if d.status in ("done", "error"):
            return True
        if d.status == "running":
            upd = d.updated
            if upd is None:
                return True  # unknown age -> assume in progress
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            quiet_s = (datetime.now(timezone.utc) - upd).total_seconds()
            if own_job_id and quiet_s >= _OWN_JOB_RECLAIM_AFTER_S:
                owner = (getattr(d, "payload", None) or {}).get("job_id")
                if str(owner or "") == str(own_job_id):
                    # Our own dead attempt: re-run it now instead of waiting for the reaper.
                    return False
            return quiet_s < stale_after_s
        return False  # pending -> run

    @staticmethod
    def claim_running(uuid, stale_after_s, own_job_id=None):
        """Atomically claim ``uuid`` for a run: set ``status='running'`` + ``updated=now()``
        iff no dossier exists yet, or it is ``pending``, or a STALE ``running`` orphan
        (``updated`` older than ``stale_after_s``), or the ``running`` row is owned by
        ``own_job_id`` — THIS job's own previous attempt. Returns True iff THIS caller won
        the claim, so with several agentworkers exactly one runs a given uuid — no
        double-pay. ``done``/``error``/a FRESH ``running`` owned by ANOTHER job are not
        claimable -> False. A single atomic Postgres ``INSERT .. ON CONFLICT DO UPDATE ..
        WHERE .. RETURNING`` (no check-then-set race between the skip decision and marking
        it running).

        The ``own_job_id`` arm exists because without it RQ's retry-after-a-dead-worker is
        a GUARANTEED no-op for these jobs. When Heroku SIGKILLs a worker mid-run (every
        deploy does), RQ requeues the job under the SAME id, but the dossier is still
        ``running`` with a heartbeat from seconds ago — so the retry looks at a row that
        appears freshly owned, refuses the claim, and exits "claimed by another worker;
        skipping" in a couple of seconds. It then has to wait out the full
        ``job_timeout + buffer`` before the reaper will touch it. Measured 2026-08-08: 4 of
        4 deploy-killed runs self-skipped in 2-11s, and the crashes sat dead for 35-50
        minutes each while three agentworkers idled.

        THE ID ALONE IS NOT ENOUGH, and assuming it was is how the first version of this
        was wrong. It is NOT true that RQ only retries a job whose work-horse is dead: the
        abandoned-execution path keys on the WORKER PARENT's execution heartbeat, and the
        parent can die while its forked horse runs on (``teardown`` never kills the horse).
        The registry entry then expires after ~90s and another worker's
        ``StartedJobRegistry.cleanup`` calls ``job.retry(...)`` under the SAME id. This was
        reproduced on the pinned rq 2.10.0 against a real Redis: SIGKILL only the parent,
        and a second execution of the same job id starts while the first is still running.
        Three ways the parent can stop beating while the horse lives here: an unprotected
        Redis call in ``monitor_work_horse`` escaping to "found an unhandled exception,
        quitting" (``worker.black_hole``'s comment records seeing exactly that line in
        prod); a Redis failover blocking the parent for minutes while the horse talks only
        to Anthropic/hg/Postgres (the connection is built with no ``socket_timeout``); and
        the dyno swapping under the Node CLI, which starves the parent's monitor loop on
        precisely the biggest runs. Granting the claim on the id alone would then put two
        ~20-minute, ~$3 runs on one crash — both upserting the dossier, both reaching
        ``_autofile``, which can file two Bugzilla bugs for one crash.

        So the arm is ANDed with the dossier's own heartbeat going quiet for
        ``_OWN_JOB_RECLAIM_AFTER_S``. A live run stamps ``updated`` every
        ``_HEARTBEAT_INTERVAL_S`` and therefore can never look that stale, so a horse that
        is still working cannot be displaced no matter what Redis believes; a genuinely
        dead one is recovered in 4 minutes instead of 35.

        A retrigger — the case where someone else legitimately owns an in-flight run —
        cancels the old job and enqueues a NEW id, and ``reset_for_retrigger`` pops
        ``job_id`` outright, so this arm cannot fire there at all."""
        uuidid = UUID.get_id(uuid)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
        claimable = [
            Dossier.status == "pending",
            and_(Dossier.status == "running", Dossier.updated < cutoff),
        ]
        if own_job_id:
            own_cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=_OWN_JOB_RECLAIM_AFTER_S
            )
            claimable.append(
                and_(
                    Dossier.status == "running",
                    Dossier.updated < own_cutoff,
                    Dossier.payload["job_id"].astext == str(own_job_id),
                )
            )
        ins = pg.insert(Dossier).values(
            uuidid=uuidid, schema_version=DOSSIER_SCHEMA_VERSION, status="running"
        )
        stmt = ins.on_conflict_do_update(
            index_elements=["uuidid"],
            set_={"status": "running", "updated": db.func.now()},
            where=or_(*claimable),
        ).returning(Dossier.id)
        won = db.session.execute(stmt).first() is not None
        db.session.commit()
        return won

    @staticmethod
    def list_tasks(limit=500):
        """Recent triage runs (newest first) for the tasks/monitoring view: the uuid
        string + signature + verdict alongside each dossier's status/timestamps/cost/
        tokens. Returns raw rows; the view layer derives duration/stalled/aggregates."""
        return (
            db.session.query(
                UUID.uuid, Signature.signature, Dossier.status,
                # WHICH CHANNEL, because with more than one the ops view could not answer "is
                # that stalled run beta or nightly?" at all -- and the two have different costs,
                # different filing policies and different expected volumes.
                Build.channel.label("channel"), Build.version.label("version"),
                Dossier.created, Dossier.updated, Dossier.cost_usd,
                Dossier.input_tokens, Dossier.output_tokens, Dossier.cache_read_tokens,
                Dossier.worker_models, Verdict.verdict, Verdict.confidence,
                # Failure reason (stashed by set_status on the error path); NULL otherwise.
                Dossier.payload["error"].astext.label("error"),
                # When the CURRENT attempt started (``set_job_id``). `created` is the
                # row's first ingest and survives a retrigger, so it is the wrong clock
                # for a re-run; the view prefers this and falls back to `created` for
                # rows written before it existed.
                Dossier.payload["run_started"].astext.label("run_started"),
                # What the autofiler did, if anything (``record_filed_bug``): the bug id and
                # whether it opened one or commented on an existing bug. NULL for every run
                # before filing was armed, and for anything it declined to file.
                Dossier.payload["filed_bug"]["bug"].astext.label("filed_bug"),
                Dossier.payload["filed_bug"]["mode"].astext.label("filed_mode"),
                Dossier.payload["filed_bug"]["needinfo"].astext.label("filed_needinfo"),
                # The requestee we WANTED and could not set: either BMO refused it during
                # the create (``needinfo_dropped``, the bug was filed anyway) or the PUT on
                # an existing bug failed (``needinfo_failed``). Without this a filing where
                # nobody was asked renders exactly like one where nobody needed to be, and
                # the whole point of the bug is to put it in front of a person.
                func.coalesce(
                    Dossier.payload["filed_bug"]["needinfo_dropped"].astext,
                    Dossier.payload["filed_bug"]["needinfo_failed"].astext,
                ).label("filed_needinfo_missed"),
            )
            .select_from(Dossier)
            .join(UUID, Dossier.uuidid == UUID.id)
            .outerjoin(Signature, UUID.signatureid == Signature.id)
            # OUTER, like the signature join: `uuids.buildid` is a nullable FK and a row without
            # a build must still appear in the ops view -- an invisible stalled run is the exact
            # failure this view exists to catch.
            .outerjoin(Build, Build.id == UUID.buildid)
            # Verdict is upserted 1:1 on uuidid (Verdict.set) and its dossierid is not
            # populated, so join on uuidid -- joining on dossierid would drop every
            # verdict. Dossier is also 1:1 per uuid, so this can't multiply rows.
            .outerjoin(Verdict, Verdict.uuidid == UUID.id)
            # NEWEST ACTIVITY first, not newest crash. `created` is INSERT-only
            # (`server_default`) and `reset_for_retrigger` leaves it alone, so a re-run keeps
            # its original timestamp: a retrigger of a 4-day-old crash sorted 4 days down the
            # page, and one older than `limit` rows vanished from this view entirely. That is
            # how a duplicate comment on bug 2065072 was invisible here while a component
            # owner was reading it on Bugzilla. `updated` carries `onupdate`, so it is the last
            # time anything happened to this run.
            #
            # NOT `run_started`, which is the semantically perfect clock and the one the view
            # already prefers for DURATION: it is text in the JSONB payload, so ordering on it
            # needs a `::timestamptz` cast, and one malformed value would 500 the page you open
            # during an outage. `updated` is a real column and cannot fail.
            .order_by(Dossier.updated.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get_stale_running(stale_after_s):
        """UUIDs whose dossier is stuck ``running`` past ``stale_after_s`` — orphaned by
        a dead worker. The reaper re-enqueues these so they self-heal instead of
        blocking that crash forever (and wasting the partial run's cost for nothing)."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
        rows = (
            db.session.query(UUID.uuid)
            .select_from(Dossier)
            .join(UUID, Dossier.uuidid == UUID.id)
            .filter(Dossier.status == "running", Dossier.updated < cutoff)
            .all()
        )
        return [r.uuid for r in rows]

    @staticmethod
    def get_stale_pending(stale_after_s):
        """UUIDs stuck ``pending`` past ``stale_after_s``. A dossier only becomes pending
        via reset_for_retrigger (a tasks-view retrigger); if the forced job it enqueued is
        then lost — e.g. a Heroku dyno restart kills the worker before pickup — nothing
        else requeues it, so it would sit pending forever. The reaper re-enqueues these
        (forced) so a retrigger self-heals across a restart."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
        rows = (
            db.session.query(UUID.uuid)
            .select_from(Dossier)
            .join(UUID, Dossier.uuidid == UUID.id)
            .filter(Dossier.status == "pending", Dossier.updated < cutoff)
            .all()
        )
        return [r.uuid for r in rows]

    @staticmethod
    def mark_action_applied(uuid, index, result_id, applied_at=None, commit=True):
        """Record the apply/replay outcome of one recorded action (#12).

        Recorded actions live inside ``payload["actions"]`` (a list written by the
        orchestrator, hackbot ``ActionsRecorder`` shape). This stamps
        ``applied_at``/``result_id`` on the action at ``index`` so the human-confirmed
        apply step is idempotent (a re-apply is a no-op) and auditable. Reassigns the
        whole ``payload`` dict so SQLAlchemy flags the JSONB column dirty (it is a
        plain ``pg.JSONB``, not a ``MutableDict``). No-op when the row/index is absent.
        """
        d = Dossier.get_by_uuid(uuid)
        if d is None or not d.payload:
            return False
        payload = dict(d.payload)
        actions = list(payload.get("actions") or [])
        if index < 0 or index >= len(actions):
            return False
        action = dict(actions[index])
        action["applied_at"] = applied_at or datetime.now(pytz.utc).isoformat()
        action["result_id"] = result_id
        actions[index] = action
        payload["actions"] = actions
        d.payload = payload
        db.session.add(d)
        if commit:
            db.session.commit()
        return True

    @staticmethod
    def record_filed_bug(uuid, info, commit=True):
        """Stamp ``payload["filed_bug"]`` with what the autofiler did for this crash.

        The audit trail for the only unattended Bugzilla write, and the per-uuid
        idempotence key: the reaper re-runs a crashed run, and without this a run that
        filed and then died would file again on recovery. Same whole-dict reassignment as
        ``mark_action_applied`` — ``payload`` is a plain JSONB column, not a MutableDict."""
        d = Dossier.get_by_uuid(uuid)
        if d is None or not d.payload:
            return False
        payload = dict(d.payload)
        payload["filed_bug"] = info
        d.payload = payload
        db.session.add(d)
        if commit:
            db.session.commit()
        return True

    @staticmethod
    def already_filed(uuid):
        """The recorded ``filed_bug`` for this crash, or ``None``. Fails CLOSED (returns a
        truthy sentinel) on a DB error so a lookup failure can never authorise a re-file."""
        try:
            d = Dossier.get_by_uuid(uuid)
        except Exception:                                  # pragma: no cover - defensive
            return {"skipped": "filed-bug lookup failed"}
        return (d.payload or {}).get("filed_bug") if d is not None else None

    @staticmethod
    def already_commented(bug_id, signature):
        """``{"uuid": ...}`` when we have ALREADY put an analysis of *signature* on *bug_id*,
        else ``None`` — the gate that stops one bug collecting the same wall of text twice.

        ``already_filed`` is keyed on the uuid, and that is not the same question. One
        (signature, build) is split into as many proto-signature clusters as its stacks have
        distinct frame lists, each of which is analysed and filed independently, so N clusters
        on one signature produce N comments on the one bug they all resolve to. Bug 2062934 got
        two 80 SECONDS apart from two crashes on the SAME machine whose stacks differed only by
        six frames of recursion depth; 2064537 and 2063862 each got a second comment on a bug we
        had just filed ourselves. Three of the 31 bugs we have commented on.

        Keyed on (bug, signature), not on the bug alone, because a SECOND signature landing on
        the same bug is real information — that is the one-defect-many-signatures case, and it
        can only happen when the bug already lists the other signature.

        Counts our own filings too (``mode="new_bug"``), which is where two of the three came
        from: having just filed the bug, the next cluster finds it via
        ``_open_bugs_for_signature`` and comments on it.

        Fails CLOSED like ``already_filed`` — a lookup we cannot do returns a truthy sentinel,
        so a DB failure produces silence rather than a duplicate."""
        if not bug_id or not signature:
            return None
        fb = Dossier.payload["filed_bug"]
        try:
            row = (
                db.session.query(UUID.uuid)
                .select_from(Dossier)
                .join(UUID, Dossier.uuidid == UUID.id)
                .filter(
                    # `filed` matters: a SKIPPED filing is recorded under the same key (see
                    # `filed_bug_rows`), and a skip must not block the comment it declined
                    # to make.
                    fb["filed"].astext == "true",
                    fb["bug"].astext == str(bug_id),
                    fb["signature"].astext == signature,
                )
                .order_by(Dossier.id)
                .first()
            )
        except Exception:                                  # pragma: no cover - defensive
            return {"skipped": "prior-comment lookup failed"}
        return {"uuid": row.uuid} if row else None

    @staticmethod
    def already_filed_for_signature(signature, channel=None):
        """``{"uuid", "bug"}`` when we have ALREADY FILED A BUG for *signature*, else ``None``.

        THE GUARD THAT SURVIVES THE TARGET BUG BEING CLOSED, which none of the others do.
        ``_open_bugs_for_signature`` filters ``resolution: "---"``, so a bug WE filed from
        nightly and a human then closed is invisible to a later run: ``existing`` is empty, the
        ``comment_on_existing`` branch never fires, ``_bug_for_this_regression`` is never asked,
        and ``already_commented`` is only consulted once a venue has been CHOSEN — so it is dead
        on that path. ``_fixed_after_build_bug`` catches only the subset RESOLVED FIXED after the
        crash's build was produced.

        MEASURED ON OUR OWN FILINGS. Of the 58 parseable signatures behind the 60 bugs the canary
        filed since 2026-08-05, 18 also crash on Firefox beta+aurora in the last 21 days. For 11
        our bug is still OPEN (the existing dedup sees it and skips). For 7 it is CLOSED with no
        other open bug covering the signature; re-running ``_fixed_after_build_bug`` catches 3
        (all FIXED after the beta build) and MISSES 4 — 2060922 DUPLICATE, 2061726 INVALID,
        2063364 INVALID, 2064066 WORKSFORME. So **4 of 18 = 22.2% (95% CI 9.0-45.2%)** of
        nightly-filed signatures that also crash on beta would have collected a second Clouseau
        bug, and those four resolutions are exactly the ones where a duplicate is worst.

        IT ALSO CLOSES A DISCLOSURE CASE, for free. The venue lookup is deliberately
        UNAUTHENTICATED (``bugzilla_apply._open_bugs_for_signature``: "we must not reason about a
        security bug we can only see because the filing account can"), so a RESTRICTED bug we
        filed from nightly is invisible to it — and a later run whose own dossier does not trip
        ``sensitive.is_withheld`` (a different report, possibly a different fault address) would
        file a PUBLIC bug on that signature. This query never asks BMO, so it sees the restricted
        bug. Do not "fix" the venue lookup by authenticating it; that trade was made on purpose.

        ``channel`` SCOPES IT, and defaults to every channel. The cross-channel question is the
        one this is for; passing a channel is how nightly keeps its existing behaviour
        byte-identical while beta gains the guard (on nightly this is the unshipped half of plan
        #17's defect A — 5 of 7 duplicate targets were our OWN earlier filings).

        Fails CLOSED like its siblings: a lookup we cannot do returns a truthy sentinel, so a DB
        failure produces silence rather than a duplicate."""
        if not signature:
            return None
        fb = Dossier.payload["filed_bug"]
        try:
            q = (
                db.session.query(UUID.uuid, fb["bug"].astext.label("bug"))
                .select_from(Dossier)
                .join(UUID, Dossier.uuidid == UUID.id)
                .filter(
                    # Same `filed` term as `already_commented`: a recorded SKIP must not read as
                    # a filing.
                    fb["filed"].astext == "true",
                    fb["signature"].astext == signature,
                )
            )
            if channel:
                # OUTER join, and the channel test admits a row whose build is UNKNOWN. This is
                # a DEDUP guard, so it must fail toward SKIPPING: a missed match is a duplicate
                # bug on BMO, which this repo treats as the unrecoverable outcome, while a
                # spurious match costs one filing the next crash on the signature will make
                # again. (`uuids.buildid` is a nullable FK; `update.put_crashes` skips a crash
                # whose buildid has no row, so in production it is never NULL -- this is about
                # which way to be wrong, not about a case we expect.)
                q = q.outerjoin(Build, Build.id == UUID.buildid).filter(
                    or_(Build.channel == channel, Build.id.is_(None))
                )
            row = q.order_by(Dossier.id).first()
        except Exception:                                  # pragma: no cover - defensive
            return {"skipped": "prior-filing lookup failed"}
        return {"uuid": row.uuid, "bug": row.bug} if row else None

    @staticmethod
    def filed_bugs_since(when, channel=None):
        """How many bugs the autofiler has FILED since *when* — the daily-cap counter.

        TWO FIXES IN ONE SIGNATURE, both of which only bite once there is more than one channel.

        ``filed`` is now required. This counted every row with a ``filed_bug`` KEY, and a SKIP is
        recorded under that same key (see ``filed_bug_rows``, which filters on the flag for
        exactly this reason). Beta ships at ``comment_on_existing: skip`` and 58-59% of beta
        signatures have an open bug, so the moment beta is armed the skips would have started
        eating nightly's cap — a global filing stop caused by declining to file.

        ``channel`` makes the cap per channel. Beta's selections are 48% concentrated in a 4-day
        post-merge burst, which is exactly when a freshly uplifted regression is worth filing, so
        one shared cap of 10 would let that burst spend nightly's budget. A row whose payload
        predates the channel key is counted for EVERY channel rather than none: under-counting a
        cap is the direction that files too much.

        ``Build.channel`` rather than the payload, so pre-existing rows need no backfill."""
        fb = Dossier.payload["filed_bug"]
        q = (
            db.session.query(func.count(Dossier.id))
            .select_from(Dossier)
            .filter(fb["filed"].astext == "true", Dossier.updated >= when)
        )
        if channel:
            q = (
                q.join(UUID, Dossier.uuidid == UUID.id)
                .join(Build, Build.id == UUID.buildid)
                .filter(Build.channel == channel)
            )
        return q.scalar() or 0

    @staticmethod
    def filed_bug_rows():
        """``[{uuid, filed_bug, dossier}]`` for every dossier a bug was actually filed from,
        oldest first — the input to ``feedback.refresh``.

        Returns the whole payload rather than plucking JSONB paths because the caller wants
        three unrelated parts of it (what was filed, which changeset was named, which
        archetypes fired) and the row count is in the tens. ``filed_bug`` is also written for
        the SKIPPED cases, so filter on its ``filed`` flag rather than on the key existing."""
        qs = (
            db.session.query(UUID.uuid, Dossier.payload)
            .select_from(Dossier)
            .join(UUID, UUID.id == Dossier.uuidid)
            .filter(Dossier.payload.has_key("filed_bug"))  # noqa: W601 - JSONB ? operator
            .order_by(Dossier.updated)
        )
        out = []
        for uuid, payload in qs:
            payload = payload or {}
            filed = payload.get("filed_bug") or {}
            if not filed.get("filed"):
                continue
            out.append({"uuid": uuid, "filed_bug": filed,
                        "dossier": payload.get("dossier") or {}})
        return out


class Verdict(db.Model):
    __tablename__ = "verdicts"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuidid = db.Column(
        db.Integer,
        db.ForeignKey("uuids.id", ondelete="CASCADE"),
        index=True,
        unique=True,
    )
    dossierid = db.Column(
        db.Integer, db.ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=True
    )
    verdict = db.Column(VERDICT_TYPE)
    confidence = db.Column(db.Integer, nullable=True)
    principal_model = db.Column(db.String(64))
    rationale = db.Column(db.Text)
    evidence = db.Column(pg.JSONB, default=list)
    effort = db.Column(db.String(16), nullable=True)
    created = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    __table_args__ = (db.UniqueConstraint("uuidid", name="uix_verdicts_uuidid"),)

    @staticmethod
    def set(
        uuid,
        verdict,
        confidence=None,
        principal_model=None,
        rationale=None,
        evidence=None,
        effort=None,
        dossierid=None,
        commit=True,
    ):
        uuidid = UUID.get_id(uuid)
        provided = {
            k: v
            for k, v in (
                ("verdict", verdict),
                ("confidence", confidence),
                ("principal_model", principal_model),
                ("rationale", rationale),
                ("evidence", evidence),
                ("effort", effort),
                ("dossierid", dossierid),
            )
            if v is not None
        }
        ins = pg.insert(Verdict).values(uuidid=uuidid, **provided)
        upd = ins.on_conflict_do_update(index_elements=["uuidid"], set_=provided)
        db.session.execute(upd)
        if commit:
            db.session.commit()

    @staticmethod
    def get_by_uuid(uuid):
        return (
            db.session.query(Verdict)
            .join(UUID, Verdict.uuidid == UUID.id)
            .filter(UUID.uuid == uuid)
            .first()
        )

    @staticmethod
    def get_for_build(buildid, product, channel):
        bdate = utils.get_build_date(buildid)
        return (
            db.session.query(Verdict)
            .select_from(Verdict)
            .join(UUID, Verdict.uuidid == UUID.id)
            .join(Build, UUID.buildid == Build.id)
            .filter(
                Build.buildid == bdate,
                Build.product == product,
                Build.channel == channel,
            )
            .all()
        )

    @staticmethod
    def map_for_build(buildid, product, channel):
        """``{uuid -> {"verdict", "confidence"}}`` for one build — lets reports.html tag
        which scored crashes the agent found a culprit/lead for, so the interesting ones
        are spottable from the index instead of clicking each. Empty when the agent
        hasn't run (so the index is unchanged without it)."""
        bdate = utils.get_build_date(buildid)
        rows = (
            db.session.query(UUID.uuid, Verdict.verdict, Verdict.confidence)
            .select_from(Verdict)
            .join(UUID, Verdict.uuidid == UUID.id)
            .join(Build, UUID.buildid == Build.id)
            .filter(
                Build.buildid == bdate,
                Build.product == product,
                Build.channel == channel,
            )
            .all()
        )
        return {r.uuid: {"verdict": r.verdict, "confidence": r.confidence} for r in rows}

    @staticmethod
    def get_evidence(uuid):
        """Read the persisted verdict + dossier + recorded actions for one UUID (#12).

        Returns a plain dict the UI/API render (never an ORM row), or ``None`` when
        no verdict row exists (panel hidden / apply route 404s). ``dossier`` is the
        #03 dossier sub-object of ``Dossier.payload``; ``actions`` is the recorded
        ``[{type, params, reasoning[, applied_at, result_id]}]`` list the orchestrator
        stored under ``payload["actions"]``. Read-only.
        """
        row = (
            db.session.query(Verdict, Dossier)
            .select_from(Verdict)
            .join(UUID, Verdict.uuidid == UUID.id)
            .outerjoin(Dossier, Dossier.uuidid == UUID.id)
            .filter(UUID.uuid == uuid)
            .first()
        )
        if row is None:
            return None
        v, d = row
        payload = (d.payload if d is not None else None) or {}
        return {
            "uuid": uuid,
            "verdict": v.verdict,
            "confidence": v.confidence,
            "principal_model": v.principal_model,
            "rationale": v.rationale,
            "evidence": v.evidence or [],
            "effort": v.effort,
            "dossier": payload.get("dossier") or {},
            "actions": payload.get("actions") or [],
            "over_budget": bool(payload.get("over_budget")),
            "status": d.status if d is not None else None,
            "cost_usd": (
                float(d.cost_usd)
                if (d is not None and d.cost_usd is not None)
                else None
            ),
        }


class Archetype(db.Model):
    """A recurring crash SHAPE and what a reviewer taught us to check when we see it.

    THE FEEDBACK LOOP THIS EXISTS FOR. Bug 2062119 was filed naming a changeset from 2022 as
    the regressor of a shutdown-phase null deref. Jens Stutte replied that it was the wrong
    changeset, found the real origin himself (bug 1412726 converted `gJarHandler` to a
    `StaticRefPtr` cleared by `ClearOnShutdown`, decoupling nulling from destruction) and wrote
    the patches -- then said: "maybe a general 'is a singleton involved that may not have a
    good/complete shutdown handling?'". That is a reusable investigation rule, and it should not
    have to be rediscovered per crash.

    ROWS, NOT CODE, because these accumulate from feedback: a reviewer's correction arrives
    weeks after the deploy that would have carried it, and the point is to add one the day it is
    learned. The precedent for the SHAPE is `_looks_pref_flip` + the LINKED-CAUSE prompt block,
    which encode exactly this kind of rule (learned from bug 2056116) but are hardcoded.

    WHAT A ROW MAY AND MAY NOT DO, because a row is not reviewed the way a patch is:

    * ``guidance`` is injected into the agent's brief as a HINT and labelled as one. It is
      never evidence, never a citation, and never a gate. The grounding rule still applies --
      the agent must cite real code for anything it concludes -- so the worst a wrong row can do
      is waste effort or suggest a dead end. It cannot file a bug or move a rung by itself.
    * ``matcher`` is DECLARATIVE, not code: lists of regexes over named crash fields plus a
      couple of scalars (see ``matches``). A rule cannot run arbitrary logic, and a pattern that
      does not compile disables its own row rather than breaking a run.

    Which rows fired is recorded on the dossier and copied onto ``Feedback``, so "did this rule
    help?" is answerable from the outcomes rather than from intuition."""

    __tablename__ = "archetypes"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    slug = db.Column(db.String(64), unique=True, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    matcher = db.Column(pg.JSONB, nullable=False, default=dict)
    guidance = db.Column(db.Text, nullable=False)
    # The bug that taught us this, so a row is never anonymous folklore.
    source_bug = db.Column(db.Integer, nullable=True)
    created = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )
    updated = db.Column(
        db.DateTime(timezone=True), nullable=False,
        server_default=db.func.now(), onupdate=db.func.now(),
    )

    # A pathological pattern would hang a worker inside `re` with no timeout available, so
    # patterns are length-capped. Rows are ops-authored, not user input, but a run must not be
    # lossable to a typo.
    MAX_PATTERN = 200

    @staticmethod
    def _any_match(patterns, text_):
        if not patterns:
            return True
        if not text_:
            return False
        for pattern in patterns:
            if not isinstance(pattern, str) or len(pattern) > Archetype.MAX_PATTERN:
                continue
            try:
                if re.search(pattern, text_, re.I):
                    return True
            except re.error:
                continue
        return False

    def matches(self, facts):
        """Does this archetype apply to a crash? ``facts`` is ``{signature, stack, crash_type,
        fault_address, shutdown_progress, moz_crash_reason}``.

        AND across the keys a row specifies, OR within each key's list, and an unspecified key
        is not a constraint. Deliberately boring: a rule an operator cannot predict the firing
        of is worse than no rule.

        THREE NON-REGEX CONDITIONS, because what they test is not text and no regex expresses
        it. All three read the same way: missing, unparseable or unknown does NOT satisfy them.

        * ``max_fault_address`` -- "a small address" is a null base plus a field offset. Not an
          n=1 bound: over 96 nightly in-shutdown EXCEPTION_ACCESS_VIOLATION_READ reports
          (2026-05-21..08-21) 72 of the 96 are small -- {0x0: 50, 0x28: 13, 0x8: 2, 0x1c: 2,
          0x470: 2, 0x10: 1, 0x14: 1, 0x80: 1} -- and the next value up is 0x80000, two orders
          of magnitude away.
        * ``require_shutdown_progress`` -- Socorro's ``shutdown_progress`` annotation is set,
          i.e. the process really had begun shutting down. Its recall cost is NOT measurable on
          the 96-report corpus above, which is selected on this very field and where the
          condition is therefore a tautology; measured separately instead, over 3 months of
          Firefox-nightly SuperSearch, the reports that carry one of this row's stack tokens in
          ``proto_signature`` with ``shutdown_progress`` unset, no ``moz_crash_reason`` and a
          small address number ONE -- an ``IPCError-browser | ShutDownKill`` at
          EXCEPTION_BREAKPOINT, i.e. a content process killed on purpose, which dereferenced
          nothing.
        * ``no_moz_crash_reason`` -- ``moz_crash_reason`` is EMPTY, i.e. Socorro has no record
          of a deliberate abort, so something really was dereferenced. An ABSENT key fails this
          and not merely a non-empty one: a caller that never looked up the field has not
          established that nothing aborted, and getting that backwards fails silently.

        Both new keys came from `shutdown-singleton` (crashclouseau/archetypes.py), which had
        neither and therefore asserted a cleared-singleton mechanism on 23 crashes per 1051
        nightly reports of which 21 had aborted on purpose and 3 were not in shutdown at all.

        WHAT THIS MUST NOT EAT, and why there is deliberately no ``min_fault_address``: a
        genuine read at EXACTLY 0x0 during shutdown is the majority shape, not an artefact --
        50 of those 96 crashes fault at 0x0, every one with a recorded memory access and none
        with a ``moz_crash_reason``. e23bec95-9350-40c7-80d3-827d20260531
        (``MOZ_StripRelativeComponents``, ``movzx eax, byte [r9]``) and
        032c9db1-f5c5-49a8-80ba-0c0500260616 (``URLQueryStringStripper::ManageObservers``,
        whose stack literally reads ``KillClearOnShutdown`` -- the archetype's own mechanism)
        both fault at 0x0 and both must keep matching."""
        m = self.matcher or {}
        if not self._any_match(m.get("signature"), (facts or {}).get("signature")):
            return False
        if not self._any_match(m.get("stack"), (facts or {}).get("stack")):
            return False
        if not self._any_match(m.get("crash_type"), (facts or {}).get("crash_type")):
            return False
        limit = m.get("max_fault_address")
        if limit is not None:
            try:
                if int(str((facts or {}).get("fault_address") or ""), 16) > int(limit):
                    return False
            except (TypeError, ValueError):
                return False
        if m.get("require_shutdown_progress"):
            if not str((facts or {}).get("shutdown_progress") or "").strip():
                return False
        if m.get("no_moz_crash_reason"):
            # An ABSENT key fails, not just a non-empty one. "Nobody looked" is not "there was
            # no abort", and reading it the other way round would quietly restore the old
            # behaviour for any caller (or fixture) built before the field existed.
            if "moz_crash_reason" not in (facts or {}):
                return False
            if str((facts or {}).get("moz_crash_reason") or "").strip():
                return False
        return True

    @staticmethod
    def for_crash(facts):
        """``[{slug, title, guidance}, ...]`` for the enabled archetypes matching this crash.

        Never raises: an unreachable table (or one that predates this feature) must degrade to
        "no hints", not lose the run."""
        try:
            rows = (
                db.session.query(Archetype)
                .filter(Archetype.enabled.is_(True))
                .order_by(Archetype.slug)
                .all()
            )
        except Exception as exc:                            # pragma: no cover - defensive
            logger.warning("archetypes: lookup failed (%s); continuing with none", exc)
            db.session.rollback()
            return []
        out = []
        for row in rows:
            try:
                if row.matches(facts):
                    out.append({"slug": row.slug, "title": row.title,
                                "guidance": row.guidance})
            except Exception:                               # pragma: no cover - defensive
                logger.warning("archetypes: %s failed to match", row.slug, exc_info=True)
        return out

    @staticmethod
    def upsert(slug, title, guidance, matcher, source_bug=None, enabled=True, commit_=True):
        """Create or update one archetype by slug. Used by the seeder and by hand."""
        row = db.session.query(Archetype).filter(Archetype.slug == slug).one_or_none()
        if row is None:
            row = Archetype(slug=slug)
            db.session.add(row)
        row.title = title
        row.guidance = guidance
        row.matcher = matcher
        row.source_bug = source_bug
        row.enabled = enabled
        if commit_:
            db.session.commit()
        return row


class Feedback(db.Model):
    """What actually happened to a bug the pipeline filed.

    The other half of the loop, and the half that makes the first half honest: without it, an
    archetype is a guess nobody can score. ``Dossier.payload["filed_bug"]`` records what we DID;
    nothing has ever recorded what came of it.

    ``regressed_by`` is why this is cheap rather than a labelling project. When a reviewer
    corrects the attribution they set BMO's own field -- bug 2062119 carries
    ``regressed_by: [1412726]`` against the ``1768581`` we named -- so "were we right?" is a
    machine comparison on a bug we already know the id of, not an inference from prose."""

    __tablename__ = "feedback"

    # What we claimed, at filing time
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    bug_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    uuid = db.Column(db.String(36), nullable=True, index=True)
    named_bug = db.Column(db.Integer, nullable=True)
    named_node = db.Column(db.String(40), nullable=True)
    # Which archetypes fired on the run that produced it — the join that scores a rule.
    archetypes = db.Column(pg.JSONB, nullable=False, default=list)

    # What Bugzilla says now
    status = db.Column(db.String(32), nullable=True)
    resolution = db.Column(db.String(32), nullable=True)
    dupe_of = db.Column(db.Integer, nullable=True)
    regressed_by = db.Column(pg.JSONB, nullable=False, default=list)
    # correct | wrong | unconfirmed | crash_invalid | unknown — see `classify`
    attribution = db.Column(db.String(16), nullable=False, default="unknown")

    filed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    checked_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        server_default=db.func.now(), onupdate=db.func.now(),
    )

    @staticmethod
    def classify(resolution, named_bug, regressed_by, claimed=None, independent_comment=None):
        """The verdict on OUR verdict.

        Five states, and the middle ones are the ones worth separating: a bug can be a real crash
        we were useful about while still naming the wrong changeset, which is exactly bug 2062119
        and exactly what the worth-investigating pivot says is an acceptable outcome. Collapsing
        it into "wrong" would make the pipeline look worse than it is; collapsing it into
        "correct" would hide the attribution problem that is genuinely there.

        ``unknown`` covers not-yet-triaged and, deliberately, a bug nobody has set
        ``regressed_by`` on: silence is not agreement.

        ``unconfirmed`` is what keeps the loop honest now that the FILER sets ``regressed_by``
        itself (``bugzilla_apply._link_regressed_by``). A field that agrees with us because we
        wrote it is not a reviewer agreeing with us, and scoring it ``correct`` would have every
        archetype reading 100% from the day it first fired — the exact self-congratulation this
        table exists to prevent. ``claimed`` is what we set at filing time; only a value somebody
        else put there counts. A reviewer who REPLACES ours still lands in ``wrong``, which is the
        half of the loop that improves anything: writing the field invites that correction, where
        prose in a comment could be ignored silently.

        THE SECOND ROUTE INTO THE SAME TRAP DOES NOT GO THROUGH ``claimed`` AT ALL: somebody else
        can copy the field out of our own comment. Measured over the 52 filings on 2026-08-21, 15
        carry a ``regressed_by`` we did not set, and 10 of the 15 were set by ONE release-
        management account (dmeehan@mozilla.com, seven of them inside 2h06 on 08-17) answering
        BugBot's ":calixte, since this bug is a regression, could you fill (if possible) the
        regressed_by field?". On bugs 2061969 and 2061691 that is the whole story: no human other
        than the filer has ever written on either — 2061691's only non-filer analysis is 2560
        characters posted by ``hackbot@mozilla.tld``, which is us. Scoring those ``correct`` reads
        our own prose back as an endorsement.

        ``independent_comment`` is the third input and it is deliberately a question about the
        BUG, not about the setter: ``True``/``False`` = ``feedback._independent_reviewers`` looked
        and somebody who is neither the filer nor machinery did / did not write on it; ``None`` =
        nobody looked, which leaves every historical row and every caller that does not pay for
        the lookup scored exactly as before. Unlike ``candidate_in_pushlog_window``'s tri-state,
        silence here means "unchecked", not "no".

        WHY THE BUG AND NOT THE SETTER — the counter-example that kills the tighter predicate: on
        bug 2062806 ryanvm set ``regressed_by`` and never commented, but hzhao had already written
        "Confirming the mechanism — this is correct" and landed the backout. "The setter did not
        comment" eats the single cleanest confirmed fix of the 52. There is no length floor on
        "substantive" either: bug 2063862's only independent comment is 42 characters ("This will
        be fixed by backout bug 2059597."), and a threshold tuned to exclude it would be fit on
        the one case it was invented for. Of the 14 rows that score ``correct`` today this moves
        exactly two — 2061969 and 2061691 — and leaves all five genuine adjudications alone
        (2062219 dveditz, 2062806 ryanvm/hzhao, 2063892 abienner, 2062052 aborovova, 2061975
        dtownsend) along with both contradictions, 2062119 (jstutte replaced ours -> ``wrong``)
        and 2064137 (jdemooij removed ours -> ``crash_invalid``)."""
        if (resolution or "").upper() in ("INVALID", "WORKSFORME", "INCOMPLETE"):
            return "crash_invalid"
        known = [int(b) for b in (regressed_by or []) if str(b).isdigit()]
        if not known:
            return "unknown"
        ours = [int(b) for b in (claimed or []) if str(b).isdigit()]
        if named_bug is not None and int(named_bug) in known:
            if int(named_bug) in ours or independent_comment is False:
                return "unconfirmed"
            return "correct"
        return "wrong"

    @staticmethod
    def record(bug_id, claimed_regressed_by=None, independent_comment=None, **fields):
        """Create or refresh one row by bug id; recomputes ``attribution``.

        ``claimed_regressed_by`` (what the filer itself set on the bug, from
        ``Dossier.payload['filed_bug']``) is an input to the verdict and is deliberately NOT
        stored: it is a fact about our filing, which the dossier already holds, and a new column
        on a long-lived table would need a migration this project has no framework for. Every
        caller reads the dossier anyway, so it is always available where it is needed. The same
        goes for ``independent_comment`` (did anybody but us and the machinery write on the bug —
        ``feedback._independent_reviewers``): it is re-derived on every refresh, because a bug
        nobody had commented on last week may have been adjudicated since, and ``_ensure_tables``
        creates missing TABLES and never missing COLUMNS, so a new column here would silently not
        exist in prod."""
        row = db.session.query(Feedback).filter(Feedback.bug_id == bug_id).one_or_none()
        if row is None:
            row = Feedback(bug_id=bug_id)
            db.session.add(row)
        for key, value in fields.items():
            if hasattr(row, key):
                setattr(row, key, value)
        row.attribution = Feedback.classify(
            row.resolution, row.named_bug, row.regressed_by, claimed_regressed_by,
            independent_comment)
        row.checked_at = datetime.now(timezone.utc)
        db.session.commit()
        return row

    @staticmethod
    def scoreboard(channel=None):
        """``{attribution -> count}`` plus per-archetype tallies, for the page and the CLI.

        ``channel`` STRATIFIES IT, joining through ``uuid`` -> ``uuids`` -> ``builds`` rather
        than needing a column here (this table predates having more than one channel, and there
        is no migration mechanism for adding a column to an existing table -- see
        ``models.create``). Without stratification the scoreboard pools two populations and the
        rate it prints describes neither: beta files under a different policy
        (``comment_on_existing``), against a signature population that is 84% long-lived where
        nightly's is not, so a shared "N% correct" would be read as being about both and be
        about neither. The same denominator argument as ``sigage.hardware_noise``'s.

        A row whose uuid is unknown (or NULL) drops out of a channel-scoped call and stays in the
        unscoped one, which is the honest direction: it cannot be attributed."""
        out = {"total": 0, "by_attribution": {}, "by_archetype": {}}
        q = db.session.query(Feedback)
        if channel:
            q = (
                q.join(UUID, UUID.uuid == Feedback.uuid)
                .join(Build, Build.id == UUID.buildid)
                .filter(Build.channel == channel)
            )
        for row in q.all():
            out["total"] += 1
            out["by_attribution"][row.attribution] = (
                out["by_attribution"].get(row.attribution, 0) + 1)
            for slug in row.archetypes or []:
                tally = out["by_archetype"].setdefault(
                    slug, {"filed": 0, "correct": 0, "wrong": 0, "unconfirmed": 0,
                           "crash_invalid": 0})
                tally["filed"] += 1
                if row.attribution in tally:
                    tally[row.attribution] += 1
        return out


# The hand-set vocabulary for ``ReviewNote.error_class``. Every value fits
# ``ReviewNote.error_class``'s declared width; ``tests/test_reviewnote.py`` pins that, which
# is the only way to catch it — CI runs on sqlite, which does not enforce VARCHAR length at
# all, so a too-long value is a Postgres-only ``StringDataRightTruncation`` inside a commit.
# The trap is not hypothetical: the first sketch of this feature wanted a state called
# ``needinfo_returned`` (17 chars) in ``Feedback.attribution``, a ``db.String(16)``.
ERROR_CLASSES = (
    "wrong_regressor",     # real crash, wrong changeset named (bug 2062119)
    "wrong_mechanism",     # right area, wrong causal story (bug 2065373, bug 2061691)
    "wrong_component",     # routed to a team that has no idea why they got it (bug 2061973)
    "not_a_regression",    # pre-existing defect, not caused by the candidate (bug 2062335)
    "duplicate",           # already tracked elsewhere (bug 2060922)
    "unchecked_claim",     # checkable against a source the run already held, not checked
    "not_a_defect",        # hardware, corrupt report, invalid
    "endorsed",            # they agreed. NOT a correction, and the common case
    "off_topic",           # not a reaction to our filing at all
)

# ``author_kind``: who wrote it, as a TOTAL function of (author, text) — see
# ``ReviewNote.classify_author``.
AUTHOR_KINDS = ("human", "agent", "automation")

# Accounts whose comments on our filings are machinery: BugBot's "could you fill the
# regressed_by field", pulsebot's "Pushed by", phabricator's uplift forms, the github
# mirror. 96 of the 186 non-ours comments on the 52 filings are automation and BugBot alone
# wrote 57 of them, so this is not a tidiness rule — unfiltered, two thirds of the corpus is
# noise nobody will ever read.
_AUTOMATION_ACCOUNTS = frozenset({
    "release-mgmt-account-bot@mozilla.tld",
    "pulsebot@bmo.tld",
    "phab-bot@bmo.tld",
    "github-automation@bmo.tld",
    "bugzilla@mozilla.org",
})

# ...and the counter-example that stops the obvious generalisation. "Filter to human
# authors" is right for the NEEDINFO channel (17 of 18 needinfos aimed at us were mass
# sweeps by release-mgmt-account-bot) and WRONG here: two of the accounts reacting to our
# filings are themselves agents, and 3 of their 4 comments flatly refute us — bug 2062335
# ("the attribution to bug 2011452 is not [right] -- this is a pre-existing defect"), bug
# 2061973 ("that routing looks incorrect"), bug 2060922 ("the same defect already tracked in
# bug 1990812"). A human-only filter would eat the sharpest corrections in the panel.
_AGENT_ACCOUNTS = frozenset({
    "hackbot@mozilla.tld",
    "firefoxmanagerdev@gmail.com",
})

# Machine-generated prose posted under a HUMAN account — 33 of the 186, so the account list
# above is not enough on its own. BMO writes the duplicate notice and the attachment header;
# release engineers post a bare landing URL and nothing else.
#
# The attachment rule is deliberately narrow, and this is the measured part: dropping every
# "Created attachment" comment eats 12 of the 18 in the panel, whose bodies are the patch's
# commit message — including jstutte's on bug 2062119, which explains the true mechanism the
# pipeline had missed. So only the BARE two-line form (header + title, nothing else) counts
# as boilerplate.
_BOILERPLATE = (
    re.compile(r"^\s*\*\*\*\s+(?:Bug \d+ has been marked as a duplicate of this bug"
               r"|This bug has been marked as a duplicate of bug \d+)\.?\s+\*\*\*\s*$"),
    re.compile(r"^\s*https?://hg\.mozilla\.org/\S+\s*$"),
    re.compile(r"^\s*Created attachment \d+\s*\n[^\n]*\s*$"),
)


def _fit_column(model, column, value):
    """Clamp *value* to the width its own column declares, reading the width off the column.

    The general form of a specific trap. A Python string longer than a ``db.String(n)`` is a
    ``StringDataRightTruncation`` on Postgres inside an unguarded ``db.session.commit()`` —
    which, in a scheduled job, kills the tick — and CI cannot see it because sqlite ignores
    VARCHAR lengths entirely. Checking each literal by hand is the version of this fix that
    rots; asking the column is the version that does not. A clamped value is still a usable
    label, and it can never abort the sweep."""
    if value is None:
        return None
    value = str(value)
    length = getattr(model.__table__.c[column].type, "length", None)
    return value if not length or len(value) <= length else value[:length]


class ReviewNote(db.Model):
    """What a reviewer SAID about a bug we filed. One row per comment, hand-labelled.

    ``Feedback`` reads the structured half of a review — ``resolution`` and ``regressed_by``,
    a field a reviewer either sets or does not. This is the other half, and it is where every
    real correction has actually lived: nothing in this repo could read a rebuttal comment, so
    every improvement in the 2026-08-21 overfitting audit came from a human reading Bugzilla by
    hand. Bug 2065373 is the case in point — jstutte corrected three separate claims in prose,
    set no field at all, and the pipeline's own record of that bug is still "NEW, no
    regressed_by, nothing happened".

    Four rules, each of which cost a measurement:

    * **It is a corpus, not a verdict.** Nothing here is written to ``Feedback.attribution``.
      That column is the causal verdict and feeds ``scoreboard()["by_archetype"]``; two of the
      filings that get notes already carry a reviewer-set ``regressed_by`` (2061975 ``[2023197]``
      set by dtownsend, 2063892 ``[2058982]`` set by dmeehan) and a second state written over
      them would destroy the only verdicts the table has. Two values in one column cannot both
      be set. ``human_replied`` is therefore DERIVED (``ReviewNote.replied``), never stored --
      which is also forced: ``_ensure_tables`` creates missing TABLES, never missing COLUMNS, so
      a new column on ``feedback`` would silently not exist in prod.

    * **A reply is not a correction.** The predicate "somebody who is not us commented" fires
      on 43 of the 52 filings (18 of the 27 still open), and among them are outright
      endorsements: bug 2060920 (docfaraday, "Seems like an easy enough fix... I'll probably do
      it all in one go") and bug 2063892 (abienner, "I have a fix almost ready"). Only the
      hand-set ``error_class`` asserts that we were wrong.

    * **Our own comments are identified by their BODY, not their author.** The filer posts as
      cdenizet, who is also a real reviewer on these bugs. Measured on the panel: 8 cdenizet
      comments sit past comment 0 across 7 filings, and 6 of them are the filer commenting
      again — an author-email rule mislabels all 8. The body marker
      ``Crash report: https://crash-stats.mozilla.org/report/index/`` splits them exactly:
      58 marker comments, all cdenizet's, none by anyone else. The residual two are honest --
      one BMO duplicate notice (caught by ``_BOILERPLATE``) and one genuine human note the
      operator wrote on bug 2063003, which is a human reply and is kept as one.

    * **Only bugs we CREATED.** ``bugzilla_apply`` sets ``filed: True`` for
      ``mode == "comment_on_existing"`` too, so the filed set includes bugs that are not ours.
      The one such bug in the public record, 2057980, would inject 29 rows -- 24% of the
      corpus -- of Thunderbird contributors discussing their own bug. The gate is an allowlist
      (``feedback._NOTE_MODES``), so an unrecognised future mode is skipped and COUNTED rather
      than silently trusted.

    ``comment_id`` is BMO's own id and is unique, so the sweep is idempotent for free and a
    re-fetch can never duplicate a row or overwrite a hand label."""

    __tablename__ = "reviewnote"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    bug_id = db.Column(db.Integer, nullable=False, index=True)
    comment_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    comment_no = db.Column(db.Integer, nullable=False, default=0)
    author = db.Column(db.String(128), nullable=False, default="")
    # human | agent | automation
    author_kind = db.Column(db.String(12), nullable=False, default="human")
    # This comment's author also put a needinfo on the account that FILED the bug. High
    # precision and very low recall by construction: 18 of the 52 filings ever carried a
    # needinfo aimed at us and 17 were mass sweeps by release-mgmt-account-bot (4 on
    # 2026-08-06, 13 on 2026-08-10), leaving exactly one human -- jstutte on bug 2065373, the
    # review that started the audit. Kept as a PRIORITY hint on a note, never as a channel of
    # its own: all 18 arrived with a comment from the same author within ten minutes (0
    # orphans), so the comment sweep already sees every one of them.
    needinfo = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=True)
    body = db.Column(db.Text, nullable=False, default="")
    # HAND-SET (`bin/feedback.py --label`). The only field that may assert a correction.
    error_class = db.Column(db.String(32), nullable=True, index=True)
    label_note = db.Column(db.Text, nullable=True)
    labelled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    seen_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now())

    @staticmethod
    def classify_author(author, text):
        """``human`` | ``agent`` | ``automation`` — a TOTAL function, defaulting to ``human``.

        Defaults to ``human`` deliberately: an unknown account is a person until proven
        otherwise, so a new bot costs noise (visible, fixable by adding it to the list) while
        the opposite default would silently swallow a new reviewer."""
        author = (author or "").strip().lower()
        text = text or ""
        if author in _AGENT_ACCOUNTS:
            return "agent"
        if author in _AUTOMATION_ACCOUNTS:
            return "automation"
        for pattern in _BOILERPLATE:
            try:
                if pattern.match(text):
                    return "automation"
            except Exception:                               # pragma: no cover - defensive
                continue
        return "human"

    @staticmethod
    def record(bug_id, comment_id, commit=True, **fields):
        """Insert one note. Returns ``(row, created)``; an id already stored is UNTOUCHED.

        Never re-writes: ``error_class`` and ``label_note`` are hand-set, and a six-hourly job
        that re-reads the same comment must not be able to erase a label somebody spent
        judgement on. A comment edited on BMO therefore keeps its original text here, which is
        the right trade — the label was made against the text we hold."""
        row = (db.session.query(ReviewNote)
               .filter(ReviewNote.comment_id == int(comment_id)).one_or_none())
        if row is not None:
            return row, False
        row = ReviewNote(bug_id=int(bug_id), comment_id=int(comment_id))
        for key, value in fields.items():
            if not hasattr(row, key):
                continue
            setattr(row, key, _fit_column(ReviewNote, key, value)
                    if isinstance(value, str) else value)
        db.session.add(row)
        if commit:
            db.session.commit()
        return row, True

    @staticmethod
    def label(comment_id, error_class, note=None):
        """Hand-set the verdict on one note. Refuses a value outside ``ERROR_CLASSES``.

        A vocabulary check rather than a free-text column: the point of the corpus is to be
        counted, and a typo'd class is a row that silently never appears in any tally."""
        if error_class is not None and error_class not in ERROR_CLASSES:
            raise ValueError("error_class must be one of {}, got {!r}".format(
                ", ".join(ERROR_CLASSES), error_class))
        row = (db.session.query(ReviewNote)
               .filter(ReviewNote.comment_id == int(comment_id)).one_or_none())
        if row is None:
            return None
        row.error_class = _fit_column(ReviewNote, "error_class", error_class)
        row.label_note = note if note is not None else row.label_note
        row.labelled_at = datetime.now(timezone.utc)
        db.session.commit()
        return row

    @staticmethod
    def replied():
        """``{bug_id: {notes, authors, needinfo, labels}}`` — the DERIVED ``human_replied``.

        A bug is in this dict iff somebody who is not the filer wrote something that is not
        machinery on it. That is all it claims. Whether they were CORRECTING us is
        ``labels``, and only a human puts a value there."""
        out = {}
        rows = (db.session.query(ReviewNote)
                .filter(ReviewNote.author_kind != "automation")
                .order_by(ReviewNote.bug_id, ReviewNote.comment_no).all())
        for row in rows:
            e = out.setdefault(row.bug_id, {"notes": 0, "authors": [], "needinfo": False,
                                            "labels": {}})
            e["notes"] += 1
            if row.author not in e["authors"]:
                e["authors"].append(row.author)
            e["needinfo"] = e["needinfo"] or bool(row.needinfo)
            if row.error_class:
                e["labels"][row.error_class] = e["labels"].get(row.error_class, 0) + 1
        return out

    @staticmethod
    def corpus(unlabelled_only=False, limit=None):
        """The rows a human should read next, needinfo'd ones first."""
        q = db.session.query(ReviewNote).filter(ReviewNote.author_kind != "automation")
        if unlabelled_only:
            q = q.filter(ReviewNote.error_class.is_(None))
        q = q.order_by(ReviewNote.needinfo.desc(), ReviewNote.bug_id, ReviewNote.comment_no)
        return q.limit(limit).all() if limit else q.all()


def commit():
    db.session.commit()


# Named-enum values added after the initial deploy. Postgres does NOT alter an
# existing named enum on create_all(), and there is no migration framework here, so
# each new value must be added explicitly (idempotently) at startup — see
# _ensure_enum_values(). Fresh DBs and the full create.py recreate get them from the
# db.Enum(...) definitions directly; only long-lived Postgres DBs need this.
_ENUM_ADDITIONS = {"VERDICT_TYPE": ("lead",)}


def _ensure_enum_values():
    """Bring a long-lived Postgres DB's named enums up to date with _ENUM_ADDITIONS.
    A no-op on non-Postgres (db.Enum renders as VARCHAR, so create_all already carries
    every value). For each value: first check pg_enum with a plain SELECT (allowed even
    for a restricted DML-only runtime role) and SKIP the DDL when it already exists — so
    a hardened role whose migrations run separately isn't hit by ``ALTER TYPE`` on every
    startup. Only a genuinely-missing value triggers the (AUTOCOMMIT, so it never trips
    the in-transaction restriction) ADD VALUE, and any failure is logged, NOT raised, so
    it can never abort init/bootstrap. Values are hard-coded literals (never user input)."""
    engine = db.engine
    if engine.dialect.name != "postgresql":
        return
    for enum_name, values in _ENUM_ADDITIONS.items():
        for value in values:
            try:
                with engine.connect() as conn:
                    exists = conn.execute(
                        text(
                            "SELECT 1 FROM pg_enum e JOIN pg_type t "
                            "ON t.oid = e.enumtypid "
                            "WHERE lower(t.typname) = lower(:n) AND e.enumlabel = :v"
                        ),
                        {"n": enum_name, "v": value},
                    ).first()
                    if exists:
                        continue
                    conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                    conn.execute(
                        text('ALTER TYPE "{}" ADD VALUE IF NOT EXISTS \'{}\''.format(
                            enum_name, value))
                    )
            except Exception as exc:
                logger.warning(
                    "could not ensure enum %s value %r (add it manually if the DB is "
                    "missing it): %s", enum_name, value, exc
                )


def create():
    engine = db.engine
    fresh = not inspect(engine).has_table("lastdate")
    if fresh:
        db.create_all()
        db.session.commit()
    # Idempotently add post-deploy enum values to a long-lived DB (no-op when fresh,
    # since create_all just built the enums from their current definitions).
    _ensure_enum_values()
    _ensure_tables()
    return fresh


# Tables added after the initial deploy. `create()` only calls `create_all()` on a FRESH
# database, so a long-lived one would never grow a new table and every read of it would fail at
# runtime — the same gap `_ensure_enum_values` exists to close for enum values.
class ChannelDaily(db.Model):
    """One row per (product, channel, PROCESSED day): the channel's reports and distinct
    installations that day. The DENOMINATOR, and it is the whole point of the table.

    Nightly's distinct-install count fell from a median 860/day in 2026-06 to 462/day in 2026-08 —
    a gradual ~45% ramp, not a step. Over that period a signature whose per-install rate held
    constant lost half its raw crash count, and one whose raw count merely held steady DOUBLED in
    rate. Any trend statistic on raw counts is measuring the user base at least as much as the
    code, which is why `sigtrend` divides by this and never compares counts directly.

    ``day`` is Socorro's ``date``, i.e. ``processed_crash.date_processed``. That choice is what
    makes the series causal: a row for day D contains exactly what was visible on D, so reading
    days <= D reproduces what a run on D could have known, with no late arrivals leaking in."""

    __tablename__ = "chandaily"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    product = db.Column(PRODUCT_TYPE, nullable=False)
    channel = db.Column(CHANNEL_TYPE, nullable=False)
    day = db.Column(db.Date, nullable=False)
    reports = db.Column(db.Integer, nullable=False, default=0)
    installs = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint("product", "channel", "day", name="chandaily_day"),
    )

    @staticmethod
    def upsert(product, channel, day, reports, installs, commit=True):
        try:
            ins = pg.insert(ChannelDaily).values(
                product=product, channel=channel, day=day,
                reports=reports, installs=installs,
            )
            db.session.execute(ins.on_conflict_do_update(
                constraint="chandaily_day",
                set_=dict(reports=ins.excluded.reports, installs=ins.excluded.installs),
            ))
            if commit:
                db.session.commit()
            return True
        except Exception:
            logger.error("Cannot upsert the channel daily row", exc_info=True)
            db.session.rollback()
            return False

    @staticmethod
    def series(product, channel, start, end):
        """``{day: (reports, installs)}`` for ``start <= day <= end``."""
        try:
            rows = (
                db.session.query(ChannelDaily.day, ChannelDaily.reports,
                                 ChannelDaily.installs)
                .filter(ChannelDaily.product == product,
                        ChannelDaily.channel == channel,
                        ChannelDaily.day >= start, ChannelDaily.day <= end)
                .all()
            )
            return {d: (r, i) for d, r, i in rows}
        except Exception:
            logger.error("Cannot read the channel daily series", exc_info=True)
            db.session.rollback()
            return {}

    @staticmethod
    def known_days(product, channel, start, end):
        """The days already collected — so a backfill only fetches the gaps."""
        return set(ChannelDaily.series(product, channel, start, end))

    @staticmethod
    def prune(days=90):
        return _prune_daily(ChannelDaily, days)


class SignatureDaily(db.Model):
    """One row per (product, channel, PROCESSED day, signature): that signature's reports and
    distinct installations that day.

    DISTINCT INSTALLATIONS ARE THE POINT, and reports are kept only so a human surface can quote
    both. One machine has produced 81,843 of 86,196 reports in a past measurement here, and 7 of
    the 59 loudest nightly signatures came from a single installation — so a rate built on reports
    measures one bad machine as loudly as a real regression. Measured over 12 matched thresholds on
    294,422 replayed (signature, run-day) rows, the install-based test beat the report-based one
    every time with non-overlapping confidence intervals (relative risk 16.9 vs 11.0 at the same
    window, firing half as often).

    Why this exists at all, when ``stats`` already holds per-(signature, build) counts: ``stats`` is
    indexed by BUILD and only for the pairs the selector KEPT, and the deployed window is 21 days.
    The trend statistic needs a per-DAY series for EVERY active signature over ~63 days, and the
    length is not a nicety — replayed at the honest standard (credit only at 3-30 days of lead), a
    14-day baseline reaches 6 of 57 human cases, 28 days reaches 10, and 56 days reaches 16."""

    __tablename__ = "sigdaily"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    product = db.Column(PRODUCT_TYPE, nullable=False)
    channel = db.Column(CHANNEL_TYPE, nullable=False)
    day = db.Column(db.Date, nullable=False)
    signature = db.Column(db.String(512), nullable=False)
    reports = db.Column(db.Integer, nullable=False, default=0)
    installs = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint("product", "channel", "day", "signature",
                            name="sigdaily_row"),
        db.Index("sigdaily_lookup_idx", "product", "channel", "signature", "day"),
    )

    # Same 65535-bind-parameter ceiling as `Selection.record_many`: ~300 signatures a day x 6
    # columns is nowhere near it, but a backfill upserts many days at once.
    _CHUNK = 1000

    @staticmethod
    def record_day(product, channel, day, rows, commit=True):
        """Upsert ``{signature: (reports, installs)}`` for one day. Never raises."""
        if not rows:
            return 0
        try:
            values = [
                {"product": product, "channel": channel, "day": day,
                 "signature": sgn[:512], "reports": rep, "installs": ins}
                for sgn, (rep, ins) in rows.items()
            ]
            written = 0
            for start in range(0, len(values), SignatureDaily._CHUNK):
                chunk = values[start:start + SignatureDaily._CHUNK]
                ins_stmt = pg.insert(SignatureDaily).values(chunk)
                db.session.execute(ins_stmt.on_conflict_do_update(
                    constraint="sigdaily_row",
                    set_=dict(reports=ins_stmt.excluded.reports,
                              installs=ins_stmt.excluded.installs),
                ))
                written += len(chunk)
            if commit:
                db.session.commit()
            return written
        except Exception:
            logger.error("Cannot record the signature daily rows", exc_info=True)
            db.session.rollback()
            return 0

    @staticmethod
    def series(product, channel, signature, start, end):
        """``{day: (reports, installs)}`` for one signature over an inclusive day range.

        A day with no row is a day with no crash, and the caller must read it as zero rather
        than as missing — which is only sound because the collector writes a ChannelDaily row
        for every day it visits, so "no signature row" and "never collected" are told apart
        there rather than here."""
        try:
            rows = (
                db.session.query(SignatureDaily.day, SignatureDaily.reports,
                                 SignatureDaily.installs)
                .filter(SignatureDaily.product == product,
                        SignatureDaily.channel == channel,
                        SignatureDaily.signature == signature[:512],
                        SignatureDaily.day >= start, SignatureDaily.day <= end)
                .all()
            )
            return {d: (r, i) for d, r, i in rows}
        except Exception:
            logger.error("Cannot read the signature daily series", exc_info=True)
            db.session.rollback()
            return {}

    @staticmethod
    def prune(days=90):
        return _prune_daily(SignatureDaily, days)


def _prune_daily(model, days):
    """Drop rows older than ``days``. These tables are a rolling window, not an archive."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        n = (db.session.query(model)
             .filter(model.day < cutoff)
             .delete(synchronize_session=False))
        db.session.commit()
        return n
    except Exception:
        logger.error("Cannot prune %s", model.__tablename__, exc_info=True)
        db.session.rollback()
        return 0


_ADDED_TABLES = ("archetypes", "chandaily", "feedback", "reviewnote",
                 "selection", "sigdaily", "sweepmarks")


def _ensure_tables():
    """Create post-deploy tables that a long-lived DB is missing. Idempotent, and never raises.

    Checks with `inspect` first and only then issues the DDL, so a hardened DML-only role whose
    migrations run separately is not hit by a CREATE on every startup. A failure is logged, not
    raised: a missing feedback table must not stop the pipeline triaging crashes — every reader
    of these tables already degrades to "no data"."""
    try:
        existing = set(inspect(db.engine).get_table_names())
    except Exception as exc:                                # pragma: no cover - defensive
        logger.warning("could not list tables to add %s: %s", _ADDED_TABLES, exc)
        return
    missing = [db.Model.metadata.tables[name]
               for name in _ADDED_TABLES
               if name not in existing and name in db.Model.metadata.tables]
    if not missing:
        return
    try:
        db.Model.metadata.create_all(bind=db.engine, tables=missing, checkfirst=True)
        db.session.commit()
        logger.info("created missing tables: %s", [t.name for t in missing])
    except Exception as exc:                                # pragma: no cover - defensive
        db.session.rollback()
        logger.warning("could not create tables %s (create them manually): %s",
                       [t.name for t in missing], exc)


def clear():
    db.drop_all()
    db.session.commit()
