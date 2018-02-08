# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from collections import defaultdict
from datetime import datetime
from dateutil.relativedelta import relativedelta
from libmozdata.hgmozilla import Mercurial
import sqlalchemy.dialects.postgresql as pg
import pytz
from . import app, config, db, utils


CHANNEL_TYPE = db.Enum(*utils.get_channels(), name='CHANNEL_TYPE')
PRODUCT_TYPE = db.Enum(*utils.get_products(), name='PRODUCT_TYPE')


class LastDate(db.Model):
    __tablename__ = 'lastdate'

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
            return d.mindate.astimezone(pytz.utc), d.maxdate.astimezone(pytz.utc)
        return None, None


class File(db.Model):
    __tablename__ = 'files'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(512), unique=True)

    def __init__(self, name):
        self.name = name

    @staticmethod
    def get_id(name):
        sel = db.select([db.literal(name)]).where(~db.exists([File.name]).where(File.name == name))
        ins = db.insert(File).from_select([File.name], sel).returning(File.id).cte('inserted')
        rs = db.session.query(File.id).filter(File.name == name).union_all(db.session.query('id').select_from(ins))
        id = rs.first()[0]
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
        m = db.session.query(File.name).filter(File.name.like('%/' + name)).first()
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
    __tablename__ = 'nodes'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    channel = db.Column(CHANNEL_TYPE)
    node = db.Column(db.String(12))
    pushdate = db.Column(db.DateTime(timezone=True))
    backedout = db.Column(db.Boolean)
    merge = db.Column(db.Boolean)
    bug = db.Column(db.Integer)

    def __init__(self, channel, info):
        self.channel = channel
        self.node = info['node']
        self.pushdate = info['date']
        self.backedout = info['backedout']
        self.merge = info['merge']
        self.bug = info['bug']

    @staticmethod
    def get_min_date(channel):
        m = db.session.query(db.func.min(Node.pushdate)).filter(Node.channel == channel).first()[0]
        return m.astimezone(pytz.utc)

    @staticmethod
    def get_bugid(node, channel):
        m = db.session.query(Node.bug).filter(Node.channel == channel,
                                              Node.node == node).first()[0]
        return m if m > 0 else 0

    @staticmethod
    def clean(date, channel):
        ndays_ago = date - relativedelta(days=config.get_ndays_of_data())
        db.session.query(Node).filter(Node.pushdate <= ndays_ago,
                                      Node.channel == channel).delete()
        db.session.commit()
        return LastDate.update(Node.get_min_date(channel), date, channel)

    @staticmethod
    def get_ids(revs, channel):
        res = {}
        if revs:
            qs = db.session.query(Node.id, Node.node).filter(Node.node.in_(list(revs)),
                                                             Node.channel == channel)
            for q in qs:
                res[q.node] = q.id
        return res

    @staticmethod
    def has_channel(channel):
        q = db.session.query(Node.channel).filter(Node.channel == channel).first()
        return bool(q)


class Changeset(db.Model):
    __tablename__ = 'changesets'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nodeid = db.Column(db.Integer, db.ForeignKey('nodes.id', ondelete='CASCADE'))
    fileid = db.Column(db.Integer, db.ForeignKey('files.id', ondelete='CASCADE'))
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
        q = q.filter(Node.node.in_(revs)).update({'analyzed': False,
                                                  'isnew': False,
                                                  'added_lines': [],
                                                  'deleted_lines': [],
                                                  'touched_lines': []}, synchronize_session='fetch')
        db.session.commit()

    @staticmethod
    def to_analyze(chgsets=[], channel=''):
        if not channel:
            fl = db.session.query(Changeset.nodeid,
                                  Node.node,
                                  Node.channel).join(Node)
            fl = fl.filter(Node.merge.is_(False),
                           Changeset.analyzed.is_(False)).distinct(Node.id).first()

            return (fl.nodeid, fl.node, fl.channel) if fl else (None, None, None)

        if not chgsets:
            return []

        chgsets = list(chgsets)
        fls = db.session.query(Changeset.id,
                               Node.id,
                               Node.node).join(Node)
        fls = fls.filter(Node.node.in_(chgsets),
                         Node.channel == channel,
                         Node.merge.is_(False),
                         Changeset.analyzed.is_(False)).distinct(Node.id)

        res = [(nodeid, node) for _, nodeid, node in fls]
        return res

    @staticmethod
    def add(chgsets, date, channel):
        if not chgsets:
            return None, None

        nodes = []
        files = set()
        for chgset in chgsets:
            node = Node(channel, chgset)
            db.session.add(node)
            nodes.append((node, chgset))
            files |= set(chgset['files'])
        db.session.commit()

        if files:
            ids = File.get_ids(files)
            for node, chgset in nodes:
                nodeid = node.id
                for f in chgset['files']:
                    c = Changeset(nodeid, ids[f])
                    db.session.add(c)
            db.session.commit()

        return Node.clean(date, channel)

    @staticmethod
    def add_analyzis(data, nodeid, channel, commit=True):
        db.session.query(Changeset).filter(Changeset.nodeid == nodeid).update({"analyzed": True})
        if data:
            chgs = db.session.query(Changeset,
                                    File.name).join(File).filter(Changeset.nodeid == nodeid)
            for chg, name in chgs:
                # if the filename is not in data,
                # then it means that the file has been deleted
                info = data.get(name)
                if info:
                    added = info.get('added')
                    if added:
                        chg.added_lines = added
                    deleted = info.get('deleted')
                    if deleted:
                        chg.deleted_lines = deleted
                    touched = info.get('touched')
                    if touched:
                        chg.touched_lines = touched
                    new = info.get('new')
                    if new:
                        chg.isnew = True
                db.session.add(chg)

        if commit:
            db.session.commit()

    @staticmethod
    def get(filenames, mindate, maxdate, channel):
        if not filenames:
            return None

        md, Md = LastDate.get(channel)
        if md is None:
            # not a valid channel
            return None

        if mindate < md or maxdate > Md:
            return None

        chgs = db.session.query(Changeset.id,
                                File.name,
                                Node.node).join(Node).join(File)
        chgs = chgs.filter(File.name.in_(filenames),
                           mindate <= Node.pushdate,
                           Node.pushdate <= maxdate,
                           Node.channel == channel)
        res = {}
        for _, fname, node in chgs:
            if fname not in res:
                res[fname] = []
            res[fname].append(node)
        return res

    @staticmethod
    def find(filenames, buildid, channel, ndays):
        mindate = buildid - relativedelta(days=ndays)
        return Changeset.get(filenames, mindate, buildid, channel)

    @staticmethod
    def get_scores(filename, line, chgsets, csid):
        chgs = db.session.query(Changeset).join(Node).join(File)
        chgs = chgs.filter(Node.node.in_(chgsets),
                           File.name == filename,
                           Changeset.analyzed.is_(True))
        res = []
        M = config.get_max_score()
        for chg in chgs:
            if chg.isnew:
                res.append((chg.id, csid, M))
            else:
                added = chg.added_lines
                deleted = chg.deleted_lines
                touched = chg.touched_lines
                sc = max(utils.get_line_score(line, touched),
                         utils.get_line_score(line, added))
                if sc < 5:
                    sc = max(sc, utils.get_line_score(line, deleted))
                res.append((chg.id, csid, sc))

        return res


class Build(db.Model):
    __tablename__ = 'builds'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    buildid = db.Column(db.DateTime(timezone=True))
    product = db.Column(PRODUCT_TYPE)
    channel = db.Column(CHANNEL_TYPE)
    version = db.Column(db.Integer)
    nodeid = db.Column(db.Integer, db.ForeignKey('nodes.id', ondelete='CASCADE'))
    __table_args__ = (db.UniqueConstraint('buildid', 'product', 'channel', name='uix_builds'), )

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
                    revs_c.add(k['revision'])
        for chan, r in revs.items():
            revs[chan] = Node.get_ids(r, chan)
        for prod, i in data.items():
            for chan, j in i.items():
                revs_c = revs[chan]
                for bid, k in j.items():
                    rev = k['revision']
                    if rev in revs_c:
                        version = k['version']
                        ins = pg.insert(Build).values(buildid=bid,
                                                      product=prod,
                                                      channel=chan,
                                                      version=version,
                                                      nodeid=revs_c[rev])
                        upd = ins.on_conflict_do_nothing()
                        db.session.execute(upd)
        db.session.commit()

    @staticmethod
    def get_id(bid, channel, product):
        q = db.session.query(Build.id).filter(Build.buildid == bid,
                                              Build.product == product,
                                              Build.channel == channel).first()
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
        q = db.session.query(Build.id, Node.node).join(Node)
        q = q.filter(Build.buildid == bid,
                     Build.product == product,
                     Build.channel == channel).first()
        if q:
            return q[1]
        return None


class Signature(db.Model):
    __tablename__ = 'signatures'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    signature = db.Column(db.String(512))

    def __init__(self, signature):
        self.signature = signature

    @staticmethod
    def get_id(signature):
        sel = db.select([db.literal(signature)]).where(~db.exists([Signature.signature]).where(Signature.signature == signature))
        ins = db.insert(Signature).from_select([Signature.signature], sel).returning(Signature.id).cte('inserted')
        rs = db.session.query(Signature.id).filter(Signature.signature == signature).union_all(db.session.query('id').select_from(ins))
        id = rs.first()[0]
        db.session.commit()
        return id


class Stats(db.Model):
    __tablename__ = 'stats'

    signatureid = db.Column(db.Integer, db.ForeignKey('signatures.id', ondelete='CASCADE'), primary_key=True)
    buildid = db.Column(db.Integer, db.ForeignKey('builds.id', ondelete='CASCADE'), primary_key=True)
    number = db.Column(db.Integer, default=0)

    def __init__(self, signatureid, buildid, number):
        self.signatureid = signatureid
        self.buildid = buildid
        self.number = number

    @staticmethod
    def add(signatureid, buildid, number, commit=True):
        ins = pg.insert(Stats).values(signatureid=signatureid, buildid=buildid, number=number)
        upd = ins.on_conflict_do_update(index_elements=['signatureid', 'buildid'],
                                        set_=dict(number=number))
        db.session.execute(upd)
        if commit:
            db.session.commit()


class UUID(db.Model):
    __tablename__ = 'uuids'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuid = db.Column(db.String(36), unique=True)
    buildid = db.Column(db.Integer, db.ForeignKey('builds.id', ondelete='CASCADE'))
    signatureid = db.Column(db.Integer, db.ForeignKey('signatures.id', ondelete='CASCADE'))
    protohash = db.Column(db.String(56))
    stackhash = db.Column(db.String(56))
    jstackhash = db.Column(db.String(56))
    analyzed = db.Column(db.Boolean, default=False)
    useless = db.Column(db.Boolean, default=False)
    max_score = db.Column(db.Integer, default=0)
    error = db.Column(db.Boolean, default=False)

    def __init__(self, uuid, signatureid,
                 protohash, buildid):
        self.uuid = uuid
        self.signatureid = signatureid
        self.protohash = protohash
        self.buildid = buildid

    @staticmethod
    def get_info(uuid):
        q = db.session.query(UUID.id,
                             Build.buildid,
                             Build.product,
                             Build.channel,
                             Build.version,
                             Signature.signature).join(Build).join(Signature)
        q = q.filter(UUID.uuid == uuid).first()

        return {'buildid': utils.get_buildid(q.buildid),
                'product': q.product,
                'channel': q.channel,
                'version': q.version,
                'signature': q.signature}

    @staticmethod
    def reset(uuids):
        qs = db.session.query(UUID.id).filter(UUID.uuid.in_(uuids))
        qs.update({'analyzed': False,
                   'useless': False,
                   'stackhash': '',
                   'jstackhash': ''}, synchronize_session='fetch')

        res = [q.id for q in qs]
        db.session.commit()

        return res

    @staticmethod
    def set_max_score(uuidid, score, commit=True):
        q = db.session.query(UUID).filter(UUID.id == uuidid)
        q.update({'max_score': score})
        if commit:
            db.session.commit()

    @staticmethod
    def set_error(uuid, commit=True):
        q = db.session.query(UUID).filter(UUID.uuid == uuid)
        q.update({'error': True})
        if commit:
            db.session.commit()

    @staticmethod
    def add(uuid, signatureid, proto, buildid, commit=True):
        ret = True
        protohash = utils.hash(proto)
        q = db.session.query(UUID).filter(UUID.signatureid == signatureid,
                                          UUID.protohash == protohash,
                                          UUID.buildid == buildid).first()
        ret = not bool(q)
        if ret:
            ins = pg.insert(UUID).values(uuid=uuid,
                                         signatureid=signatureid,
                                         protohash=protohash,
                                         buildid=buildid)
            upd = ins.on_conflict_do_update(index_elements=['uuid'],
                                            set_=dict(signatureid=signatureid,
                                                      protohash=protohash,
                                                      buildid=buildid))
            db.session.execute(upd)
            if commit:
                db.session.commit()

        return ret

    @staticmethod
    def add_stack_hash(uuid, sh, jsh, commit=True):
        q = db.session.query(UUID).filter(UUID.uuid == uuid)
        if sh:
            q.update({'stackhash': sh})
        elif jsh:
            q.update({'jstackhash': jsh})
        if commit:
            db.session.commit()

    @staticmethod
    def set_analyzed(uuid, useless, commit=True):
        q = db.session.query(UUID).filter(UUID.uuid == uuid)
        q.update({'useless': useless,
                  'analyzed': True})
        if commit:
            db.session.commit()

    @staticmethod
    def to_analyze():
        uuid = db.session.query(UUID.uuid,
                                Build.buildid,
                                Build.channel,
                                Node.node).join(Build).join(Node)
        uuid = uuid.filter(UUID.analyzed.is_(False)).first()
        return uuid

    @staticmethod
    def get_bid_chan(uuid):
        r = db.session.query(UUID.id, Build.buildid, Build.channel).join(Build)
        r = r.filter(UUID.uuid == uuid,
                     UUID.useless.is_(False)).first()
        return r.buildid.astimezone(pytz.utc), r.channel

    @staticmethod
    def get_bid_chan_by_id(uuidid):
        r = db.session.query(UUID.uuid,
                             Signature.signature,
                             Build.buildid,
                             Build.channel,
                             Node.node).join(Build).join(Node).join(Signature)
        r = r.filter(UUID.id == uuidid).first()
        if r:
            return {'uuid': r.uuid,
                    'signature': r.signature,
                    'buildid': r.buildid.astimezone(pytz.utc),
                    'channel': r.channel,
                    'node': r.node}
        return {}

    @staticmethod
    def get_bid_chan_by_uuid(uuid):
        r = db.session.query(UUID.id,
                             UUID.jstackhash,
                             Signature.signature,
                             Build.buildid,
                             Build.channel,
                             Node.node).join(Build).join(Node).join(Signature)
        r = r.filter(UUID.uuid == uuid,
                     UUID.useless.is_(False),
                     UUID.analyzed.is_(True)).first()
        if r:
            return {'uuid': uuid,
                    'id': r.id,
                    'signature': r.signature,
                    'buildid': r.buildid.astimezone(pytz.utc),
                    'channel': r.channel,
                    'java': bool(r.jstackhash),
                    'node': r.node}
        return {}

    @staticmethod
    def get_uuids_from_buildid(buildid, product, channel, score):
        sbid = buildid
        buildid = utils.get_build_date(buildid)
        if score == -1:
            uuids = db.session.query(UUID.uuid,
                                     UUID.max_score,
                                     Signature.signature,
                                     Stats.number)
            uuids = uuids.join(Signature).join(CrashStack).join(Build)
            uuids = uuids.join(Stats,
                               db.and_(Signature.id == Stats.signatureid,
                                       Build.id == Stats.buildid))
            uuids = uuids.filter(Build.buildid == buildid,
                                 Build.product == product,
                                 Build.channel == channel,
                                 UUID.useless.is_(False),
                                 UUID.analyzed.is_(True)).distinct(UUID.id).order_by(UUID.id)
        else:
            uuids = db.session.query(UUID.uuid,
                                     UUID.max_score,
                                     Signature.signature,
                                     Stats.number)
            uuids = uuids.join(Signature).join(CrashStack).join(Build)
            uuids = uuids.join(Stats,
                               db.and_(Signature.id == Stats.signatureid,
                                       Build.id == Stats.buildid))
            uuids = uuids.filter(Build.buildid == buildid,
                                 Build.product == product,
                                 Build.channel == channel,
                                 UUID.useless.is_(False),
                                 UUID.analyzed.is_(True),
                                 UUID.max_score == score).distinct(UUID.id)
        _res = {}
        for uuid in uuids:
            t = (uuid.uuid, uuid.max_score)
            if uuid.signature in _res:
                _res[uuid.signature]['uuids'].append(t)
            else:
                _res[uuid.signature] = {'uuids': [t],
                                        'number': uuid.number,
                                        'url': utils.make_url_for_signature(uuid.signature,
                                                                            buildid,
                                                                            sbid,
                                                                            channel,
                                                                            product)}
        res = sorted(_res.items(), key=lambda p: p[1]['number'], reverse=True)
        return res

    @staticmethod
    def clean(date, channel):
        date = datetime(date.year, date.month, date.day)
        date += relativedelta(days=config.get_ndays())
        db.session.query(UUID).filter(UUID.buildid <= date,
                                      UUID.channel == channel).delete()
        db.session.commit()

    @staticmethod
    def get_id(uuid):
        return db.session.query(UUID.id).filter(UUID.uuid == uuid).first()[0]

    @staticmethod
    def is_stackhash_existing(stackhash, java):
        if java:
            r = db.session.query(UUID.id).filter(UUID.jstackhash == stackhash).first()
        else:
            r = db.session.query(UUID.id).filter(UUID.stackhash == stackhash).first()
        return r is not None

    @staticmethod
    def get_buildids_from_pc(product, channel):
        bids = db.session.query(UUID.id, Build.buildid).join(Build)
        bids = bids.filter(Build.product == product,
                           Build.channel == channel,
                           UUID.useless.is_(False),
                           UUID.analyzed.is_(True)).distinct(Build.buildid).order_by(Build.buildid.desc())
        res = [utils.get_buildid(bid.buildid) for bid in bids]
        return res

    @staticmethod
    def get_buildids_from_channel(channel):
        bids = db.session.query(UUID.id, Build.product, Build.buildid).join(Build)
        bids = bids.filter(Build.channel == channel,
                           UUID.useless.is_(False),
                           UUID.analyzed.is_(True)).distinct(Build.product,
                                                             Build.buildid).order_by(Build.buildid.desc())
        res = {}
        for bid in bids:
            b = utils.get_buildid(bid.buildid)
            if bid.product in res:
                res[bid.product].append(b)
            else:
                res[bid.product] = [b]
        return res


class Score(db.Model):
    __tablename__ = 'scores'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    changesetid = db.Column(db.Integer, db.ForeignKey('changesets.id', ondelete='CASCADE'))
    crashstackid = db.Column(db.Integer, db.ForeignKey('crashstack.id', ondelete='CASCADE'))
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
        qs = db.session.query(Score, UUID.uuid).join(CrashStack).join(UUID)
        qs = qs.filter(Score.score == score).distinct(UUID.id)
        res = [uuid for _, uuid in qs]
        return res


class CrashStack(db.Model):
    __tablename__ = 'crashstack'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    uuidid = db.Column(db.Integer, db.ForeignKey('uuids.id', ondelete='CASCADE'))
    java = db.Column(db.Boolean)
    stackpos = db.Column(db.Integer)
    original = db.Column(db.String(512))
    module = db.Column(db.String(128))
    filename = db.Column(db.String(512))
    function = db.Column(db.Text)
    line = db.Column(db.Integer)
    node = db.Column(db.String(12))
    internal = db.Column(db.Boolean)

    def __init__(self, uuidid, stackpos, java,
                 original, module, filename, function,
                 line, node, internal):
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
        db.session.query(CrashStack).filter(CrashStack.uuidid.in_(ids)).delete(synchronize_session=False)
        db.session.commit()

    @staticmethod
    def put_frames(uuid, frames, java, commit=True):
        css = []
        uuidid = UUID.get_id(uuid)
        for frame in frames['frames']:
            cs = CrashStack(uuidid,
                            frame['stackpos'],
                            java,
                            frame['original'],
                            frame['module'],
                            frame['filename'],
                            frame['function'],
                            frame['line'],
                            frame['node'],
                            frame['internal'])
            db.session.add(cs)
            css.append((cs, frame))

        db.session.commit()
        max_score = 0
        for cs, frame in css:
            csets = frame['changesets']
            if csets:
                scores = Changeset.get_scores(frame['filename'],
                                              frame['line'],
                                              csets,
                                              cs.id)
                Score.set(scores)
                scores = max(s for _, _, s in scores)
                max_score = max(max_score, scores)

        UUID.set_max_score(uuidid, max_score)

    @staticmethod
    def get_by_uuid(uuid):
        uuid_info = UUID.get_bid_chan_by_uuid(uuid)
        if not uuid_info:
            return {}, {}

        uuidid = uuid_info['id']
        repo_url = Mercurial.get_repo_url(uuid_info['channel'])
        is_java = uuid_info['java']

        iframes = db.session.query(CrashStack.stackpos,
                                   Node.node,
                                   Node.backedout,
                                   Node.pushdate,
                                   Node.bug,
                                   Score.score).join(Score).join(Changeset).join(Node)
        iframes = iframes.filter(CrashStack.uuidid == uuidid,
                                 CrashStack.java.is_(is_java)).order_by(CrashStack.stackpos)
        frames = db.session.query(CrashStack).filter(CrashStack.uuidid == uuidid,
                                                     CrashStack.java.is_(is_java)).order_by(CrashStack.stackpos)
        stack = []
        res = {'frames': stack}
        for frame in frames:
            url, filename = utils.get_file_url(repo_url,
                                               frame.filename,
                                               frame.node,
                                               frame.line,
                                               frame.original)
            stack.append({'stackpos': frame.stackpos,
                          'filename': filename,
                          'function': frame.function,
                          'changesets': {},
                          'line': frame.line,
                          'node': frame.node,
                          'original': frame.original,
                          'internal': frame.internal,
                          'url': url})

        for stackpos, node, bout, pdate, bugid, score, in iframes:
            stack[stackpos]['changesets'][node] = {'score': score,
                                                   'backedout': bout,
                                                   'pushdate': pdate,
                                                   'bugid': bugid}

        return res, uuid_info


def commit():
    db.session.commit()


def create():
    engine = db.get_engine(app)
    if not engine.dialect.has_table(engine, 'lastdate'):
        db.create_all()
        db.session.commit()
        return True
    return False


def clear():
    db.drop_all()
    db.session.commit()
