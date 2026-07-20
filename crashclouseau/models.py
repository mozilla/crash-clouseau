# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from libmozdata.hgmozilla import Mercurial
import sqlalchemy.dialects.postgresql as pg
from sqlalchemy import and_, inspect, func, or_, text
import pytz
from . import config, db, utils
from .logger import logger


CHANNEL_TYPE = db.Enum(*config.get_channels(), name="CHANNEL_TYPE")
PRODUCT_TYPE = db.Enum(*config.get_products(), name="PRODUCT_TYPE")

# Evidence-agent persistence (#04). The dossier JSON content schema is owned by
# the dossier-builder sub-plan (#03); this layer stores the envelope + verdict.
DOSSIER_SCHEMA_VERSION = 1
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
    def reset(revs):
        q = db.session.query(Changeset).join(Node)
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
    def get_scores(filename, line, chgsets, csid):
        chgs = db.session.query(Changeset).select_from(Changeset).join(Node).join(File)
        chgs = chgs.filter(
            Node.node.in_(chgsets), File.name == filename, Changeset.analyzed.is_(True)
        )
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

        res = []
        major = 0
        for q in qs:
            if major == 0:
                major = utils.get_major(q.version)
            elif major != utils.get_major(q.version):
                break
            res.append(
                {
                    "buildid": utils.get_buildid(q.buildid),
                    "revision": q.node,
                    "version": q.version,
                }
            )

        if len(res) >= 2:
            return res

        return []

    @staticmethod
    def get_pushdate_before(buildid, channel, product):
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
        return qs.pushdate

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
        unknown/proto-less uuid (so it never blocks a first run)."""
        row = (
            db.session.query(UUID.signatureid, UUID.protohash)
            .filter(UUID.uuid == uuid)
            .first()
        )
        if not row or not row.protohash:
            return False
        q = (
            db.session.query(Dossier.id)
            .join(UUID, Dossier.uuidid == UUID.id)
            .filter(
                UUID.signatureid == row.signatureid,
                UUID.protohash == row.protohash,
                Dossier.status == "done",
            )
        )
        return db.session.query(q.exists()).scalar()

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
            uuid = uuid.filter(UUID.analyzed.is_(False)).first()
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
    def put_frames(uuid, frames, java, commit=True):
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
                    frame["filename"], frame["line"], csets, cs.id
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
        upd = ins.on_conflict_do_update(
            index_elements=["uuidid"], set_={**provided, "updated": db.func.now()}
        )
        db.session.execute(upd)
        if commit:
            db.session.commit()

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
        reaper attempt counter so an operator retrigger earns a fresh give-up budget."""
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
        d.payload = payload
        if commit:
            db.session.commit()

    @staticmethod
    def bump_reap_attempts(uuid, commit=True):
        """Increment (in the JSONB ``payload``, no migration) the count of times the
        reaper has re-enqueued this stuck dossier, and return the new count. Lets the
        reaper GIVE UP on a crash that keeps orphaning (e.g. OOMs on every run) instead
        of re-enqueuing it forever. Reset by ``reset_for_retrigger`` (an operator
        retrigger earns a fresh budget). Returns 0 if the row is absent."""
        uuidid = UUID.get_id(uuid)
        if uuidid is None:
            return 0
        d = db.session.query(Dossier).filter(Dossier.uuidid == uuidid).first()
        if d is None:
            return 0
        payload = dict(d.payload or {})
        n = int(payload.get("reap_attempts", 0) or 0) + 1
        payload["reap_attempts"] = n
        d.payload = payload
        d.updated = db.func.now()
        if commit:
            db.session.commit()
        return n

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
    def skip_triage(uuid, stale_after_s):
        """Whether triage of ``uuid`` should be SKIPPED: it is already ``done``/``error``,
        or a run is genuinely in progress (``running`` and ``updated`` within
        ``stale_after_s``). A ``running`` dossier OLDER than that is an orphan — its
        worker died mid-run (e.g. a Heroku dyno restart, which SIGKILLs before the
        exception handler can mark ``error``) — so it is NOT skipped and gets retried;
        ``pending`` retries too. ``updated`` is stamped at run start (no heartbeat) and
        RQ kills a run at its job timeout, so any ``running`` past the timeout is dead."""
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
            return (datetime.now(timezone.utc) - upd).total_seconds() < stale_after_s
        return False  # pending -> run

    @staticmethod
    def claim_running(uuid, stale_after_s):
        """Atomically claim ``uuid`` for a run: set ``status='running'`` + ``updated=now()``
        iff no dossier exists yet, or it is ``pending``, or a STALE ``running`` orphan
        (``updated`` older than ``stale_after_s``). Returns True iff THIS caller won the
        claim, so with several agentworkers exactly one runs a given uuid — no double-pay.
        ``done``/``error``/a FRESH ``running`` are not claimable -> False. A single atomic
        Postgres ``INSERT .. ON CONFLICT DO UPDATE .. WHERE .. RETURNING`` (no
        check-then-set race between the skip decision and marking it running)."""
        uuidid = UUID.get_id(uuid)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
        ins = pg.insert(Dossier).values(
            uuidid=uuidid, schema_version=DOSSIER_SCHEMA_VERSION, status="running"
        )
        stmt = ins.on_conflict_do_update(
            index_elements=["uuidid"],
            set_={"status": "running", "updated": db.func.now()},
            where=or_(
                Dossier.status == "pending",
                and_(Dossier.status == "running", Dossier.updated < cutoff),
            ),
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
                Dossier.created, Dossier.updated, Dossier.cost_usd,
                Dossier.input_tokens, Dossier.output_tokens, Dossier.cache_read_tokens,
                Dossier.worker_models, Verdict.verdict, Verdict.confidence,
                # Failure reason (stashed by set_status on the error path); NULL otherwise.
                Dossier.payload["error"].astext.label("error"),
            )
            .select_from(Dossier)
            .join(UUID, Dossier.uuidid == UUID.id)
            .outerjoin(Signature, UUID.signatureid == Signature.id)
            # Verdict is upserted 1:1 on uuidid (Verdict.set) and its dossierid is not
            # populated, so join on uuidid -- joining on dossierid would drop every
            # verdict. Dossier is also 1:1 per uuid, so this can't multiply rows.
            .outerjoin(Verdict, Verdict.uuidid == UUID.id)
            .order_by(Dossier.created.desc())
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
    return fresh


def clear():
    db.drop_all()
    db.session.commit()
