# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import functools
import re
from collections import Counter
from jinja2 import Environment, FileSystemLoader
import libmozdata.config
from libmozdata import socorro
from libmozdata.bugzilla import Bugzilla, BugzillaUser
from libmozdata.hgmozilla import Mercurial
from . import net
from urllib.parse import parse_qs, urlencode, urlparse
from . import buginfo, models, utils
from .logger import logger


def findall(p, s):
    """Yields all the positions of
    the pattern p in the string s."""
    i = s.find(p)
    while i != -1:
        yield i
        i = s.find(p, i + 1)


def get_bz_query(data):
    """Get the Bugzilla query inside the Socorro web page"""
    needle = 'href="https://bugzilla.mozilla.org/enter_bug.cgi?'
    for i in findall(needle, data):
        j = data.index('"', i + len(needle))
        if j != -1:
            bz_url = data[i + len('href="'):j]
            bz_url = bz_url.replace("&amp;", "&")
            bz_url = bz_url.replace("&lt;", "<")
            bz_url = bz_url.replace("&gt;", ">")
            bz_url = bz_url.replace("&quot;", '"')
            bz_url = bz_url.replace("&apos;", "'")
            if "keywords=crash" in bz_url:
                query = parse_qs(urlparse(bz_url).query)
                return query
    return {}


def improve(query, bzdata, bugid):
    """Improve the Bugzilla query we found with other useful info"""
    if "bugs" in bzdata and len(bzdata["bugs"]) == 1:
        bzdata = bzdata["bugs"][0]
        query["product"] = bzdata["product"]
        query["component"] = bzdata["component"]
        query["keywords"] = "{},regression".format(query["keywords"][0])
        query["blocked"] = "clouseau,{}".format(bugid)
        return bzdata["assigned_to"]
    return ""


def get_stats(data, buildid):
    """Get crash stats from Socorro to put in the bug report"""
    res = {}
    for i in data["facets"]["build_id"]:
        count = i["count"]
        facets = i["facets"]
        it = len(facets["install_time"])
        if it == 100:
            it = facets["cardinality_install_time"]["value"]
        res[i["term"]] = {"count": count, "installs": it}

    if len(res) == 1:
        return True, res[buildid]
    else:
        count = 0
        installs = 0
        for v in res.values():
            count += v["count"]
            installs += v["installs"]
        return False, {"count": count, "installs": installs}


def finalize_comment(bzquery, first, stats, info, changeset, bugid, evidence_summary=None):
    """Finalize the comment to put in the bug report.

    ``evidence_summary`` (#12) is the principal's evidence lines. It is appended to
    the drafted comment ONLY when provided, so the ``bug.txt`` render — and thus the
    drafted comment when omitted — is byte-identical to before."""
    comment = bzquery["comment"][0]
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("bug.txt")
    channel = info["channel"]
    url = Mercurial.get_repo_url(channel)
    url = "{}/rev?node={}".format(url, changeset)
    if channel == "nightly":
        version = "nightly {}".format(utils.get_major(info["version"]))
    else:
        version = info["version"]

    comment = template.render(
        socorro_comment=comment,
        count=stats["count"],
        installs=stats["installs"],
        version=version,
        buildid=info["buildid"],
        bugid=bugid,
        changeset_url=url,
        first=first,
    )
    comment = comment.replace("\\n", "\n")
    if evidence_summary:
        comment = comment.rstrip("\n") + "\n\n" + evidence_summary + "\n"
    bzquery["comment"] = comment
    bzurl = "https://bugzilla.mozilla.org/enter_bug.cgi"
    return bzurl + "?" + urlencode(bzquery, True)


async def get_info_helper(uuid, changeset, evidence_summary=None):
    info = models.UUID.get_info(uuid)
    bugid = models.Node.get_bugid(changeset, info["channel"])
    sgn = info["signature"]
    bzw, bugsdata = buginfo.get_bugs(sgn, wait=False)

    cs = "https://crash-stats.mozilla.org/report/index/" + uuid
    bz = "https://bugzilla.mozilla.org/rest/bug"
    bzh = {"X-Bugzilla-API-Key": libmozdata.config.get("Bugzilla", "token", "")}
    bzq = {"id": bugid, "include_fields": ["product", "component", "assigned_to"]}
    cs_api = "https://crash-stats.mozilla.org/api/SuperSearch/"
    cs_api_q = {
        "signature": "=" + info["signature"],
        "build_id": ">=" + info["buildid"],
        "product": info["product"],
        "release_channel": info["channel"],
        "_aggs.build_id": ["install_time", "_cardinality.install_time"],
        "_results_number": 0,
        "_facets": "release_channel",
        "_facets_size": 100,
    }

    loop = asyncio.get_running_loop()
    f1 = loop.run_in_executor(None, functools.partial(net.get, cs))
    if bugid:
        f2 = loop.run_in_executor(
            None, functools.partial(net.get, bz, headers=bzh, params=bzq)
        )
    f3 = loop.run_in_executor(
        None, functools.partial(net.get, cs_api, params=cs_api_q)
    )
    r1 = await f1
    if bugid:
        r2 = await f2
    r3 = await f3
    bzquery = get_bz_query(r1.text)
    first, stats = get_stats(r3.json(), int(info["buildid"]))
    bzdata = r2.json() if bugid else {}
    ni = improve(bzquery, bzdata, bugid)
    url = finalize_comment(
        bzquery, first, stats, info, changeset, bugid, evidence_summary=evidence_summary
    )

    bzw.wait()

    return url, ni, sgn, bugsdata


def get_info(uuid, changeset, evidence_summary=None):
    """Get the info (comment and Bugzilla stuff) to put in the bug report"""
    return asyncio.run(
        get_info_helper(uuid, changeset, evidence_summary=evidence_summary)
    )


# --------------------------------------------------------------------------- #
# Local bug preview (#12, evaluation phase). The eventual flow files a bug (with the
# stack), posts the Clouseau comment, and needinfos the area-expert AUTOMATICALLY, so the
# UI is only informative. This recreates the crash-report comment WITHOUT the Socorro
# round-trip (we already have the stack + signature) and resolves the target
# product::component from the regressor. All best-effort — never raises into the caller.
# --------------------------------------------------------------------------- #
_MAX_PREVIEW_FRAMES = 10
# Socorro truncates a frame's function to this width (with a trailing "...") in the stack
# it pre-fills into a crash bug; matching it keeps a heavily-templated C++/Rust signature
# from turning one frame into three wrapped lines.
_MAX_FUNCTION_CHARS = 80
# Code references appended after the analysis. Capped so a citation-heavy verdict cannot
# bury the prose under a wall of links.
_MAX_CODE_REFS = 6
_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
# Process cache: product::component of a bug is stable, and the preview is a hot render
# path. bug_id -> (product, component) | None (None = looked up, unreadable/absent).
_PC_CACHE: dict = {}


def _fenced(text):
    """``text`` in a markdown fenced block. BMO renders comments as markdown
    (``class="comment-text markdown-body"``), so an unfenced C++/Rust stack gets mangled:
    ``_`` becomes emphasis, ``*`` a list, ``<T>`` swallowed as a tag."""
    return "```\n{}\n```".format(text.strip("\n"))


def build_frames_block(stack, max_frames=_MAX_PREVIEW_FRAMES):
    """The ``Top N frames:`` section, in the format Socorro pre-fills into a crash bug:
    ``<stackpos>  <module>  <function>  <file>:<line>``, fenced. Sourced from the frames we
    already hold (``models.CrashStack.get_by_uuid``), so no Socorro round-trip is needed."""
    frames = (stack or {}).get("frames") or []
    top = frames[:max_frames]
    lines = []
    for f in top:
        fn = (f.get("function") or "").strip()
        if len(fn) > _MAX_FUNCTION_CHARS:
            fn = fn[:_MAX_FUNCTION_CHARS] + "..."
        module = (f.get("module") or "").strip()
        fname = (f.get("filename") or "").strip()
        line = f.get("line")
        loc = "{}:{}".format(fname, line) if (fname and line and line > 0) else fname
        desc = "  ".join(x for x in (module, fn, loc) if x)
        if not desc:
            desc = (f.get("original") or "").strip()
        lines.append("{}  {}".format(f.get("stackpos"), desc).rstrip())
    return "Top {} frames:\n{}".format(len(top), _fenced("\n".join(lines)))


def build_reason_block(details):
    """The crash-reason section, or ``None`` when Socorro gave us nothing.

    A ``MOZ_CRASH``/Rust panic carries a human-written ``moz_crash_reason`` and gets the
    ``MOZ_CRASH Reason:`` heading a hand-filed crash bug uses. Anything else (a segv, an
    access violation) has only the OS-level ``reason``, which is worth stating together
    with the faulting ``address`` -- for a null deref that pair *is* the diagnosis."""
    details = details or {}
    moz = (details.get("moz_crash_reason") or "").strip()
    if moz:
        return "MOZ_CRASH Reason:\n{}".format(_fenced(moz))
    reason = (details.get("reason") or "").strip()
    if not reason:
        return None
    address = (details.get("address") or "").strip()
    body = "{} at {}".format(reason, address) if address else reason
    return "Crash Reason:\n{}".format(_fenced(body))


def build_stats_sentence(first, stats, uuid_info):
    """One sentence on how much this signature is crashing -- deliberately the same
    phrasing as the ``bug.txt`` draft template, so the automatic and hand-drafted comments
    read alike. ``first`` is Socorro's "only this buildid" flag (see ``get_stats``).
    ``None`` when we have no counts."""
    stats = stats or {}
    count = stats.get("count")
    if not count:
        return None
    installs = stats.get("installs") or 0
    if count == 1:
        what = "There is 1 crash"
    elif installs == 1:
        what = "There are {} crashes (from 1 installation)".format(count)
    else:
        what = "There are {} crashes (from {} installations)".format(count, installs)
    # Same "where" wording as bug.txt: a nightly is named by its channel + major version
    # ("nightly 155"), a release/beta by its full version.
    channel = (uuid_info or {}).get("channel") or ""
    version = (uuid_info or {}).get("version") or ""
    if channel == "nightly":
        where = "nightly {}".format(utils.get_major(version)) if version else "nightly"
    else:
        where = version or channel
    buildid = utils.get_buildid((uuid_info or {}).get("buildid"))
    return "{}{} {}with buildid {}.".format(
        what,
        " in {}".format(where) if where else "",
        "" if first else "starting ",
        buildid,
    )


_GITHUB_COMMIT_URL = "https://github.com/mozilla-firefox/firefox/commit/{}"
# hg rev -> git commit ("" = looked up, no counterpart). The mapping is immutable and the
# comment is re-rendered on every page view, so one lookup per node per dyno is plenty.
_GIT_HASH_CACHE: dict = {}


def hg_to_git(node, channel):
    """The git commit for an hg rev, from hg's own ``json-rev`` -- which accepts a SHORT rev
    and returns a ``git_commit`` field. (lando's ``hg2git`` would need the full 40-char hash;
    ``Node.node`` only stores 12, which is why ``inspector.git2hg`` goes the other way.)
    ``""`` when unresolvable. Cached + best-effort."""
    if not node:
        return ""
    if node in _GIT_HASH_CACHE:
        return _GIT_HASH_CACHE[node]
    git = ""
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if repo_url:
        try:
            r = net.get("{}/json-rev/{}".format(repo_url, node), allow_redirects=True)
            r.raise_for_status()
            git = (r.json() or {}).get("git_commit") or ""
        except Exception:
            logger.warning(
                "bug preview: hg->git lookup failed for %s", node, exc_info=True
            )
    _GIT_HASH_CACHE[node] = git
    return git


def changeset_links(node, channel):
    """``[<node>](hg) ([gh](github))`` -- the changeset hash itself links to hg, with a short
    ``(gh)`` for the GitHub counterpart, since Firefox lives in both forges after the
    hg->git migration. The ``(gh)`` is dropped when the git counterpart can't be resolved,
    and the hash left bare when the channel has no repo -- never a dead link."""
    if not node:
        return ""
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if not repo_url:
        return node
    out = "[{}]({}/rev/{})".format(node, repo_url, node)
    git = hg_to_git(node, channel)
    if git:
        out += " ([gh]({}))".format(_GITHUB_COMMIT_URL.format(git))
    return out


def build_code_references(verdict, channel, max_refs=_MAX_CODE_REFS):
    """Markdown links to the code the verdict cites -- searchfox permalinks for symbols,
    hg file links (at the candidate's own revision) for the lines its patch touched. This
    is what makes the analysis checkable without leaving the bug. ``None`` when the verdict
    cites nothing linkable."""
    verdict = verdict or {}
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    cites = []
    for claim in ("mechanism", "consistency"):
        cites.extend((verdict.get(claim) or {}).get("citations") or [])
    refs, seen = [], set()
    for c in cites:
        if len(refs) >= max_refs:
            break
        kind, url, label = c.get("kind"), "", ""
        if kind == "searchfox" and c.get("permalink"):
            url = c["permalink"]
            label = c.get("symbol_id") or c.get("filename") or url
        elif kind == "diff_line" and repo_url and c.get("filename") and c.get("node"):
            url = "{}/file/{}/{}".format(repo_url, c["node"], c["filename"])
            label = c["filename"]
            if c.get("line"):
                url += "#l{}".format(c["line"])
                label += ":{}".format(c["line"])
        if not url or url in seen:
            continue
        seen.add(url)
        refs.append("- [{}]({})".format(label, url))
    return "Code references:\n{}".format("\n".join(refs)) if refs else None


_REASON_COLUMNS = ["moz_crash_reason", "reason", "address"]
# Process caches: the comment is rendered on every page view of a culprit/lead crash, and
# neither of these moves in a way that matters for a preview (a crash's reason is
# immutable; the counts only creep up). uuid -> value; keeps the render to one fetch per
# uuid per dyno, preserving the "no Socorro round-trip on the hot path" property.
_REASON_CACHE: dict = {}
_STATS_CACHE: dict = {}


def fetch_crash_reason(uuid):
    """``{moz_crash_reason, reason, address}`` for ONE crash, from Socorro. Cached +
    best-effort: ``{}`` on failure, which simply omits the reason section."""
    if uuid in _REASON_CACHE:
        return _REASON_CACHE[uuid]
    got: dict = {}

    def handler(json, data):
        hits = json.get("hits") or []
        if hits:
            data.update(hits[0])

    try:
        socorro.SuperSearch(
            params={"uuid": uuid, "_columns": _REASON_COLUMNS, "_results_number": 1},
            handler=handler,
            handlerdata=got,
        ).wait()
    except Exception:
        logger.warning("bug preview: crash-reason lookup failed", exc_info=True)
    _REASON_CACHE[uuid] = got
    return got


def fetch_signature_stats(uuid, info):
    """``(first, {count, installs})`` for this signature at/after this buildid -- the same
    Socorro aggregation the hand-drafted ``bug.txt`` comment uses, so both comments quote
    the same numbers. ``(True, {})`` when unavailable. Cached + best-effort."""
    if uuid in _STATS_CACHE:
        return _STATS_CACHE[uuid]
    buildid = utils.get_buildid(info.get("buildid"))
    out = (True, {})
    got: dict = {}

    def handler(json, data):
        data.update(json)

    try:
        socorro.SuperSearch(
            params={
                "signature": "=" + (info.get("signature") or ""),
                "build_id": ">=" + str(buildid),
                "product": info.get("product"),
                "release_channel": info.get("channel"),
                "_aggs.build_id": ["install_time", "_cardinality.install_time"],
                "_results_number": 0,
                "_facets": "release_channel",
                "_facets_size": 100,
            },
            handler=handler,
            handlerdata=got,
        ).wait()
        out = get_stats(got, int(buildid))
    except Exception:
        logger.warning("bug preview: signature stats lookup failed", exc_info=True)
    _STATS_CACHE[uuid] = out
    return out


def build_bug_comment(
    uuid_info,
    stack,
    dossier,
    details=None,
    stats=None,
    first=True,
    version=None,
    needinfo=None,
    max_frames=_MAX_PREVIEW_FRAMES,
):
    """The SINGLE comment the filed bug opens with, in the shape a triager expects from a
    hand-filed crash bug (cf. bug 2057432 comment 0):

    1. the crash-report link;
    2. the crash reason (``MOZ_CRASH Reason:`` for a panic, else the OS reason + address);
    3. the top ``max_frames`` frames, fenced, with the module column;
    4. one sentence on how much this signature is crashing;
    5. the Clouseau analysis + suspected regressor;
    6. searchfox/hg links for the code the analysis cites;
    7. the needinfo ask.

    Sections with no data are dropped, never emitted empty."""
    uuid = (uuid_info or {}).get("uuid", "")
    channel = (uuid_info or {}).get("channel")
    info = dict(uuid_info or {})
    if version:
        info["version"] = version
    sections = [
        "Crash report: https://crash-stats.mozilla.org/report/index/{}".format(uuid),
        build_reason_block(details),
        build_frames_block(stack, max_frames=max_frames),
        build_stats_sentence(first, stats, info),
        _explanation_comment(
            (dossier or {}).get("verdict"), (dossier or {}).get("candidate"), channel
        ),
        build_code_references((dossier or {}).get("verdict"), channel),
        needinfo,
    ]
    return "\n\n".join(s for s in sections if s)


def _bugs_product_component(bugids):
    """``{bug_id (int) -> (product, component)}`` for the READABLE bugs among ``bugids``.
    A security bug the token can't read is simply absent (Bugzilla returns no
    product/component for it), which is what triggers the author-patches fallback below.
    Cached + best-effort (never raises)."""
    want, out = [], {}
    for b in bugids:
        try:
            bid = int(b)
        except (TypeError, ValueError):
            continue
        if bid in _PC_CACHE:
            if _PC_CACHE[bid]:
                out[bid] = _PC_CACHE[bid]
        elif bid not in want:
            want.append(bid)
    if not want:
        return out
    got: dict = {}

    def handler(bug, data):
        data[int(bug["id"])] = bug

    try:
        Bugzilla(
            bugids=[str(b) for b in want],
            include_fields=["id", "product", "component"],
            bughandler=handler,
            bugdata=got,
        ).get_data().wait()
    except Exception:
        logger.warning("bug preview: product/component lookup failed", exc_info=True)
        return out
    for bid in want:
        bug = got.get(bid) or {}
        pc = (bug.get("product"), bug.get("component"))
        pc = pc if (pc[0] and pc[1]) else None
        _PC_CACHE[bid] = pc
        if pc:
            out[bid] = pc
    return out


def _first_email(author):
    """Best-effort email from an ``hg`` author display string (``Real Name <email>`` or a
    bare address)."""
    if not author:
        return ""
    m = _EMAIL_RE.search(author)
    if m:
        return m.group(1)
    author = author.strip()
    return author if ("@" in author and " " not in author) else ""


def resolve_product_component(candidate, channel):
    """``(product, component)`` for the bug we would file, best-effort + never raises:

    1. the REGRESSOR bug's own product::component;
    2. if that bug is unreadable (e.g. a security regressor bug), the MOST FREQUENT
       product::component across the regressor author's recent patches' bugs;
    3. ``(None, None)`` when neither resolves.
    """
    if not candidate:
        return None, None
    try:
        bug = candidate.get("bug")
        if bug:
            pc = _bugs_product_component([bug]).get(int(bug))
            if pc:
                return pc
        node = candidate.get("node")
        info = models.Node.authors_for([node], channel).get(node, {}) if node else {}
        email = info.get("email") or _first_email(candidate.get("author"))
        if email:
            bugs = models.Node.recent_bugs_by_author(email, channel)
            pcs = _bugs_product_component(bugs)
            if pcs:
                # Tally in recent_bugs_by_author's NEWEST-FIRST order (not _PC_CACHE's
                # cache-hits-first dict order): Counter.most_common breaks a count tie by
                # first-seen, so this deterministically favours the author's most RECENT
                # patch, independent of unrelated prior cache state.
                ordered = [pcs[b] for b in bugs if b in pcs]
                return Counter(ordered).most_common(1)[0][0]
    except Exception:
        logger.warning("bug preview: could not resolve product/component", exc_info=True)
    return None, None


def _explanation_comment(verdict, candidate, channel=None):
    """The Clouseau analysis comment we'd post to the filed bug: the crash mechanism (and,
    when present, why it is consistent with the crash) plus the suspected regressor -- the
    latter carrying an hg and a GitHub link when ``channel`` tells us which repo it is in.
    ``None`` when there is nothing substantive to say."""
    verdict = verdict or {}
    lines = []
    mech = ((verdict.get("mechanism") or {}).get("statement") or "").strip()
    cons = ((verdict.get("consistency") or {}).get("statement") or "").strip()
    conf = verdict.get("confidence") or ""
    if mech:
        lines.append("Clouseau analysis{}: {}".format(
            " (confidence {})".format(conf) if conf else "", mech))
    if cons:
        lines.append(cons)
    c = candidate or {}
    if c.get("node"):
        detail = "Suspected regressor: {}".format(changeset_links(c["node"], channel))
        if c.get("bug"):
            detail += " (bug {})".format(c["bug"])
        author = (c.get("author") or "").strip()
        if author:
            detail += " by {}".format(author)
        lines.append(detail + ".")
    return "\n\n".join(lines) if lines else None


_NICK_CACHE: dict = {}   # email -> Bugzilla nick ("" = looked up, none/unresolvable)


def _bugzilla_nick(email):
    """The BUGZILLA IRC nick for a user (e.g. ``stransky``), looked up by login/email via
    the Bugzilla user API (``/rest/user``). This is the Bugzilla handle -- distinct from the
    hg-commit nick -- so a ``:nick`` needinfo actually reaches the right account. ``""`` when
    unknown/unresolvable. Cached + best-effort (never raises)."""
    if not email:
        return ""
    if email in _NICK_CACHE:
        return _NICK_CACHE[email]
    got: dict = {}

    def handler(user, data):
        data["nick"] = (user.get("nick") or "").strip()

    try:
        # NB: BugzillaUser fires the query in its constructor (Connection.exec_queries) and
        # is drained by .wait() -- it has NO get_data() (that lives on the sibling Bugzilla
        # class). The handler runs during wait() and fills ``got``.
        BugzillaUser(
            user_names=[email],
            include_fields=["name", "nick"],
            user_handler=handler,
            user_data=got,
        ).wait()
    except Exception as exc:
        # An unresolvable author email is a routine 400 (user not found / not visible), so
        # log it concisely rather than with a full traceback -- the nick just stays empty.
        logger.info("bug preview: bugzilla nick lookup failed for %s: %s", email, exc)
    nick = got.get("nick", "")
    _NICK_CACHE[email] = nick
    return nick


def _needinfo_person(candidate, channel):
    """The person to needinfo for the suspected regressor: its AUTHOR, identified by their
    BUGZILLA nick (e.g. ``:stransky``) looked up from the author's email via the Bugzilla
    user API. The email/name come from the local hgauthor record for the candidate node,
    else the candidate's author display string (``Real Name <email>``). Falls back to the
    name/email when no Bugzilla nick resolves. ``{}`` when the author is unknown."""
    c = candidate or {}
    email = name = ""
    node = c.get("node")
    if node:
        try:
            info = models.Node.authors_for([node], channel).get(node) or {}
        except Exception:
            info = {}
        email = (info.get("email") or "").strip()
        name = (info.get("real") or "").strip()
    author = (c.get("author") or "").strip()
    if not email:
        email = _first_email(author)
    if not name and author:
        name = author.split("<", 1)[0].strip()
    if not (email or name):
        return {}
    return {"nick": _bugzilla_nick(email), "name": name, "email": email}


def _needinfo_line(person):
    """The needinfo we'd request -- ``:nick, can you have a look please?`` -- for ``person``
    (a ``{nick, name, email}`` dict). Prefer the IRC nick, then the name, then the email.
    ``None`` when no usable identity is available."""
    person = person or {}
    nick = (person.get("nick") or "").strip()
    if nick:
        return ":{}, can you have a look please?".format(nick)
    who = (person.get("name") or person.get("email") or "").strip()
    if who:
        return "{}, can you have a look please?".format(who)
    return None


def build_bug_preview(uuid_info, stack, dossier):
    """The "bug we'd file" preview for the crashstack panel, and the payload the automatic
    filer posts: ``{title, comment, product, component, needinfo, needinfo_email}``.

    ``comment`` is the whole bug opener as ONE comment (``build_bug_comment``) -- the
    stack, the crash reason, the volume, the analysis and the needinfo ask together, the
    way a triager reads a hand-filed crash bug. ``needinfo_email`` is the requestee the
    flag needs (the rendered ``needinfo`` line only carries a display nick).
    product/component are best-effort from the regressor (``resolve_product_component``).
    Returns ``None`` when there is no candidate regressor to file a bug against."""
    dossier = dossier or {}
    candidate = dossier.get("candidate")
    if not candidate or not candidate.get("node"):
        return None
    channel = uuid_info.get("channel")
    uuid = uuid_info.get("uuid", "")
    product, component = resolve_product_component(candidate, channel)
    person = _needinfo_person(candidate, channel)
    # Version lives on the build row, not on the page's uuid_info; best-effort, and the
    # stats sentence simply omits it when unavailable.
    version = uuid_info.get("version")
    if not version:
        try:
            version = models.UUID.get_info(uuid).get("version")
        except Exception:
            version = None
    first, stats = fetch_signature_stats(uuid, uuid_info)
    return {
        # Match Socorro's crash-bug summary verbatim: "Crash in [@ signature]". The
        # ``[@ ...]`` is Bugzilla's crash-signature syntax, so an identical title keeps
        # these bugs searchable/dedupable alongside Socorro-filed ones.
        "title": "Crash in [@ {}]".format((uuid_info.get("signature") or "").strip()),
        "comment": build_bug_comment(
            uuid_info,
            stack,
            dossier,
            details=fetch_crash_reason(uuid),
            stats=stats,
            first=first,
            version=version,
            needinfo=_needinfo_line(person),
        ),
        "product": product,
        "component": component,
        "needinfo": _needinfo_line(person),
        "needinfo_email": (person or {}).get("email") or "",
    }
