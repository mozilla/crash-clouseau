# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import functools
import re
from collections import Counter
from jinja2 import Environment, FileSystemLoader
from libmozdata import socorro
from libmozdata.bugzilla import Bugzilla, BugzillaUser
from libmozdata.hgmozilla import Mercurial
from . import net
from urllib.parse import parse_qs, urlencode, urlparse
from . import buginfo, config, models, utils
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
    bzh = {"X-Bugzilla-API-Key": config.get_bugzilla_token()}
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
# Process cache behind `_bug_meta`: a bug's product::component and the people on it are
# both stable, and the preview is a hot render path (every crashstack view, not just a
# filing). bug_id -> the raw bug dict, or {} for one we are not allowed to read.
_BUG_CACHE: dict = {}


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


def changeset_links(node, channel, git_commit=""):
    """``[<node>](hg) ([gh](github))`` -- the changeset hash itself links to hg, with a short
    ``(gh)`` for the GitHub counterpart, since Firefox lives in both forges after the
    hg->git migration.

    ``git_commit`` is supplied by the CALLER (persisted on the candidate by
    ``orchestrator._resolve_candidate_git_commit``); this function makes no network call,
    because resolving an hg rev to a git sha costs 8-13s at hg's ``json-rev`` and this runs on
    every page render. No sha -> no ``(gh)``; no repo for the channel -> a bare hash. Never a
    dead link, and never a slow one."""
    if not node:
        return ""
    repo_url = Mercurial.get_repo_url(channel) if channel else ""
    if not repo_url:
        return node
    out = "[{}]({}/rev/{})".format(node, repo_url, node)
    if git_commit:
        out += " ([gh]({}))".format(_GITHUB_COMMIT_URL.format(git_commit))
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
        elif kind == "ref":
            # The catch-all kind (see ``schema.RefCitation``) — something read through the
            # source/history tools. Without a branch here it is silently absent from the
            # filed bug, which is the same "the evidence exists but the page doesn't say
            # so" failure the kind was added to stop.
            # ``filename`` has to LOOK like a repo path before it can build a /file/ URL.
            # A ``ref`` is the catch-all kind, so the model sometimes puts a label there
            # ("hg-changeset-metadata"), and /file/<node>/<label> is a 404 in a list whose
            # whole purpose is letting a human check the analysis without leaving the bug.
            # Falling through to /rev/<node> gives them a page that exists.
            if repo_url and c.get("node") and "/" in (c.get("filename") or ""):
                url = "{}/file/{}/{}".format(repo_url, c["node"], c["filename"])
                label = c["filename"]
                if c.get("line"):
                    url += "#l{}".format(c["line"])
                    label += ":{}".format(c["line"])
            elif repo_url and c.get("node"):
                url = "{}/rev/{}".format(repo_url, c["node"])
                label = c["node"][:12]
            elif str(c.get("permalink") or "").startswith(("https://", "http://")):
                url = c["permalink"]
                label = c.get("filename") or c.get("symbol_id") or url
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
    related_bugs=None,
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
    7. why this is a new bug rather than a comment on ``related_bugs``, when there are any;
    8. the needinfo ask.

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
        build_related_bugs_note(related_bugs),
        needinfo,
    ]
    return "\n\n".join(s for s in sections if s)


def build_related_bugs_note(related_bugs):
    """Why this is a NEW bug when ``related_bugs`` are open on the same signature, or ``""``.

    The filer only ever skips past an open bug because that bug predates the suspected
    regressor (``bugzilla_apply._bug_for_this_regression``), so say so, in the bug itself,
    where the triager deciding whether to duplicate it can see the reasoning and overrule it.
    An unexplained second bug on a live signature just looks like a broken deduplicator."""
    bugs = [b for b in (related_bugs or []) if b]
    if not bugs:
        return ""
    return (
        "Filed as a new bug rather than a comment on {} — {} open on this signature, but "
        "{} filed before the changeset above landed, so {} cannot be about this regression. "
        "Please duplicate if that is wrong.".format(
            ", ".join("bug {}".format(b) for b in bugs),
            "which is" if len(bugs) == 1 else "which are",
            "it was" if len(bugs) == 1 else "they were",
            "it" if len(bugs) == 1 else "they",
        )
    )


def _bug_meta(bugids):
    """``{bug_id (int) -> bug dict}`` for the bugs among ``bugids``, ``{}`` for one we
    cannot read.

    ONE fetch behind both things the preview asks a regressor bug: where to file
    (``product``/``component``) and who to ask (``assigned_to``/``creator``). They used to
    be two requests for the same bug -- and this path runs on every crashstack page view,
    not just on a filing, so BMO's rate limiter is a real ceiling here (it answered 429 to
    a few hundred reads while this was being written).

    Unreadable is recorded as ``{}`` and cached: a security bug does not become readable
    later, and re-asking on every preview spends a request to learn nothing. It always
    arrives as ABSENCE rather than as an error, which is what makes one batched read able to
    answer "and if that bug is private, try another". Measured anonymously: a mixed batch
    (``id=2043188,2042379``) returns 200 carrying only 2042379, with no ``faults`` key, and
    -- the case that matters, because rung 2 asks for exactly one bug -- the restricted id
    ALONE also returns ``200 {"bugs":[]}`` in this query form. (``GET /rest/bug/2043188``,
    the path form, is the one that answers 401/code 102; libmozdata does not use it.)

    Best-effort: never raises."""
    want, out = [], {}
    for b in bugids:
        try:
            bid = int(b)
        except (TypeError, ValueError):
            continue
        if bid in _BUG_CACHE:
            out[bid] = _BUG_CACHE[bid]
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
            # ``assigned_to``/``creator``, NOT ``assigned_to_detail``. The detail hash is
            # emitted as a companion of the BASE field -- Bugzilla's `_bug_to_hash` sets
            # `assigned_to_detail` inside `if (filter_wants $params, 'assigned_to')` -- and
            # `assigned_to_detail` is not a token `filter_wants` recognises: its prefix
            # branch matches the literal `assigned_to.`, with a dot, not an underscore. Ask
            # for the companion alone and the field simply never appears, which reads as
            # "unreadable bug" and would silently kill rungs 2 and 3 while product/component
            # (whose tokens are exact) kept working. Confirmed on the wire too: a request
            # naming `creator` and not `creator_detail` came back carrying creator_detail.
            include_fields=["id", "product", "component", "assigned_to", "creator"],
            bughandler=handler,
            bugdata=got,
        ).get_data().wait()
    except Exception:
        # We could not ASK. Unlike "unreadable", this must NOT be cached as an empty answer:
        # one BMO blip would otherwise blind this process to that bug for its whole life.
        logger.warning("bug preview: bug metadata lookup failed", exc_info=True)
        return out
    for bid in want:
        bug = got.get(bid) or {}
        _BUG_CACHE[bid] = bug
        out[bid] = bug
    return out


def _bugs_product_component(bugids):
    """``{bug_id (int) -> (product, component)}`` for the READABLE bugs among ``bugids``.
    A security bug the token can't read is simply absent, which is what triggers the
    author-patches fallback below. Cached + best-effort (never raises)."""
    out = {}
    for bid, bug in _bug_meta(bugids).items():
        pc = (bug.get("product"), bug.get("component"))
        if pc[0] and pc[1]:
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
                # Tally in recent_bugs_by_author's NEWEST-FIRST order (not the cache's
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
        detail = "Suspected regressor: {}".format(
            changeset_links(c["node"], channel, c.get("git_commit") or "")
        )
        if c.get("bug"):
            detail += " (bug {})".format(c["bug"])
        author = (c.get("author") or "").strip()
        if author:
            detail += " by {}".format(author)
        lines.append(detail + ".")
    return "\n\n".join(lines) if lines else None


_USER_CACHE: dict = {}   # email -> {"exists", "nick"}

# A Bugzilla ``real_name`` is a plain name plus annotations in brackets: "Andreas Farre
# [:farre]", and often more than one and not always last — "[:jandem] (PTO until Monday)",
# "Foo Bar [:foo] ⌚UTC+1". Strip every bracketed group WHEREVER it appears, not just a nick
# tag at the end, or an author with a trailing note never matches their own hg name.
_BZ_ANNOTATION = re.compile(r"[\[(][^\])]*[\])]")


def _norm_name(name):
    """A person's display name, normalised for comparison: bracketed annotations removed,
    whitespace collapsed, casefolded. ``""`` for anything unusable, and ``""`` NEVER matches
    ``""`` at the call sites -- an absent name must not make two strangers equal.

    Stripping annotations cannot merge two people: what remains still has to be an EXACT
    full-name match, and prod's own near-miss (``farre@mozilla.com`` "Andreas Farre" vs
    ``sfarre@mozilla.com`` "Simon Farre") differs in the part no annotation touches."""
    return re.sub(r"\s+", " ", _BZ_ANNOTATION.sub(" ", name or "")).strip().casefold()


def _bugzilla_user(email):
    """What Bugzilla knows about the login ``email``: ``{"exists": bool, "nick": str}``.

    ``exists`` is the field the old nick-only lookup threw away, and it is the one that
    matters. It is NOT ``bool(nick)`` -- plenty of real accounts have no nick -- but whether
    ``/rest/user`` returned a user at all. BMO validates a needinfo requestee while CREATING
    a bug and rejects the WHOLE post with code 51 for an unknown one, so a requestee that is
    not an account costs the entire filing, not just the needinfo. That is not theoretical:
    crash f6fe186b's hg author is ``farre@mozilla.com``, which is nobody on BMO (the account
    is ``afarre@mozilla.com``), and the create came back 404.

    ``permissive`` is what makes the distinction trustworthy. Passing a ``fault_user_handler``
    makes libmozdata send it, so BMO answers 200 with the unknown name in ``faults`` instead
    of erroring -- which means a missing user is now distinguishable from a network blip.
    Without it, both arrive as an exception and we would drop a perfectly good needinfo every
    time BMO hiccups. ``exists`` is therefore only False when BMO SAID so.

    Anonymous, like every other read here: ``/rest/user?names=`` answers without an API key.
    (``match=`` does not -- BMO replies 505, "Logged-out users cannot use the match argument"
    -- which is why account resolution goes through BUG metadata and not a user search.)

    Cached + best-effort (never raises)."""
    if not email:
        return {"exists": False, "nick": ""}
    if email in _USER_CACHE:
        return _USER_CACHE[email]
    got: dict = {}

    def handler(user, data):
        data["user"] = user

    def fault(f, data):
        data["fault"] = f

    try:
        # NB: BugzillaUser fires the query in its constructor (Connection.exec_queries) and
        # is drained by .wait() -- it has NO get_data() (that lives on the sibling Bugzilla
        # class). The handlers run during wait() and fill ``got``.
        BugzillaUser(
            user_names=[email],
            include_fields=["name", "nick"],
            user_handler=handler,
            fault_user_handler=fault,
            user_data=got,
        ).wait()
    except Exception as exc:
        # Could not ASK. Not the same as "no such user": leave the address usable and let
        # the create's own fallback carry the risk.
        logger.info("bug preview: bugzilla user lookup failed for %s: %s", email, exc)
        return {"exists": True, "nick": "", "unverified": True}
    user = got.get("user")
    out = {"exists": user is not None, "nick": ((user or {}).get("nick") or "").strip()}
    _USER_CACHE[email] = out
    return out


def _bug_people(bugids):
    """``{bug_id -> [{"email", "real", "nick"}, ...]}``, assignee before creator, for the
    READABLE bugs among ``bugids`` -- a bug we cannot read yields an empty list, which is
    exactly the "then try another one" signal.

    ``nobody@mozilla.org`` is skipped: it is the unassigned placeholder, not a person.
    Shares ``_bug_meta``'s single fetch and cache; never raises."""
    out = {}
    for bid, bug in _bug_meta(bugids).items():
        people = []
        for key in ("assigned_to_detail", "creator_detail"):
            d = bug.get(key) or {}
            mail = (d.get("email") or d.get("name") or "").strip()
            if not mail or mail.startswith("nobody@"):
                continue
            people.append({"email": mail,
                           "real": (d.get("real_name") or "").strip(),
                           "nick": (d.get("nick") or "").strip()})
        out[bid] = people
    return out


def _match_author(people, name, email=""):
    """The entry in ``people`` that IS the hg author, or ``None``.

    Three keys, each an EXACT comparison, strongest first. The strictness is the point:
    prod's hgauthors holds ``farre@mozilla.com`` "Andreas Farre" AND ``sfarre@mozilla.com``
    "Simon Farre", and needinfo-ing the wrong human is worse than needinfo-ing nobody.

    1. the same address. Conclusive.
    2. the same display name, annotations stripped. Carries most of the weight.
    3. the Bugzilla nick equals the hg address's local part -- ``longsonr@gmail.com`` is
       "Robert Longson [:longsonr]", and hg records that author's name as the bare
       ``longsonr``, so key 2 cannot see them.

    Measured over 189 recent (bug, hg author) pairs where the bug was readable: key 2 alone
    identifies 59%, adding key 3 takes it to 65%, and in ZERO cases did a weaker key point at
    a different person than a stronger one. A fourth key -- local part equal across
    DIFFERENT domains -- would reach 74%, and is deliberately not here: 10 of the 17 it adds
    are ``moz-wptsync-bot``, which we must never ask to investigate a crash, and across
    domains a bare local part is weak evidence that two addresses are one human."""
    want_email = (email or "").strip().casefold()
    want_name = _norm_name(name)
    want_local = want_email.split("@")[0]
    people = people or []
    for p in people:
        if want_email and (p.get("email") or "").strip().casefold() == want_email:
            return p
    for p in people:
        if want_name and _norm_name(p.get("real")) == want_name:
            return p
    for p in people:
        if want_local and (p.get("nick") or "").strip().casefold() == want_local:
            return p
    return None


def _needinfo_account(candidate, channel, email, name):
    """The Bugzilla LOGIN to put in the needinfo flag: ``{"email", "nick"}``, or ``{}``.

    An hg commit address is not a Bugzilla account. Usually it happens to be one; when it is
    not, BMO rejects the whole bug (see ``_bugzilla_user``). So ask the bugs instead --
    deliberately the same ladder, and the same fallback, as ``resolve_product_component``
    just above, because it is the same problem:

    1. the hg author's own address, when BMO says it IS an account (the common case, one
       cheap lookup, and no bug read at all);
    2. the REGRESSOR bug's assignee or creator whose real name is the author's -- the bug the
       changeset landed for knows the person's account even when hg does not;
    3. the same over the author's other recent patches' bugs, which is what answers "and if
       the regressor bug is private, find one that isn't": a restricted bug just vanishes
       from a batched read, and the author's other landings are almost always public.
    4. ``{}`` -- then we file with no flag rather than filing no bug.

    Step 3 also runs when the regressor bug is perfectly readable but nobody on it matches
    (an unassigned bug filed by a triager is ordinary), which is a deliberate widening of
    "if it is private": one batched request, and the alternative is a needinfo we could have
    resolved and didn't.

    Cost, precisely, because this runs on every crashstack page view and not only when a bug
    is filed: ``build_bug_preview`` calls ``resolve_product_component`` first, through the
    same ``_bug_meta`` cache. Step 2 is therefore always free -- that function reads the
    regressor bug first thing. Step 3 is free exactly when the regressor bug was UNREADABLE,
    because p/c then fell back to the author's recent bugs and cached them, i.e. free in the
    private-bug case this exists for. It costs one batched read in the other case (bug
    readable, nobody on it matched)."""
    if not email and not name:
        return {}
    user = _bugzilla_user(email)
    if user.get("exists") and not user.get("unverified"):
        return {"email": email, "nick": user.get("nick", "")}

    c = candidate or {}
    # `nodes.bug` is -1, not NULL, when the commit message carries no bug number (2555 of
    # 20372 prod nodes), so "is there a bug" has to be a >0 test rather than a truth test.
    try:
        bug = int(c.get("bug") or 0)
    except (TypeError, ValueError):
        bug = 0
    if bug > 0:
        hit = _match_author(_bug_people([bug]).get(bug), name, email)
        if hit:
            return {"email": hit["email"], "nick": hit["nick"]}

    if email:
        try:
            others = models.Node.recent_bugs_by_author(email, channel)
        except Exception:
            others = []
        others = [b for b in others if b != bug]
        if others:
            people = _bug_people(others)
            # recent_bugs_by_author is newest-first; keep that order so the account we pick
            # comes from the author's most recent work.
            for b in others:
                hit = _match_author(people.get(b), name, email)
                if hit:
                    return {"email": hit["email"], "nick": hit["nick"]}
    # Last: an address we could not CHECK (BMO would not answer the user lookup) beats no
    # needinfo at all, but only after the bug-verified rungs have had their turn -- a
    # name-matched account is better evidence than an unverified guess. If it turns out not
    # to be a login, `_create_bug_keeping_the_bug` drops the flag and still files the bug.
    if user.get("unverified") and email:
        return {"email": email, "nick": ""}
    return {}


def _needinfo_person(candidate, channel):
    """The person to needinfo for the suspected regressor: its AUTHOR, as
    ``{nick, name, email, account}``. ``{}`` when the author is unknown.

    ``email``/``name`` are the MERCURIAL identity -- from the local hgauthor record for the
    candidate node, else the candidate's author display string (``Real Name <email>``).
    ``account`` is the verified BUGZILLA login (``_needinfo_account``), which is a different
    thing and often a different address, and it is the only one of the two safe to put in a
    flag. ``nick`` is that account's Bugzilla handle, so a ``:nick`` needinfo reaches the
    right person; it is empty when no account resolved, and the prose then falls back to the
    plain name."""
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
    if not email:
        # hg's own ``user`` field, resolved once per run by the orchestrator and stored on
        # the candidate. Without it the needinfo is usually absent: `Node.authors_for` is
        # empty for most candidates and the model writes ``author`` as a bare display name
        # ("Jon Coppeard"), so only 3 of 12 recent rung-70 leads resolved an address.
        email = (c.get("author_email") or "").strip()
    if not name and author:
        name = author.split("<", 1)[0].strip()
    if not (email or name):
        return {}
    account = _needinfo_account(c, channel, email, name)
    return {"nick": account.get("nick", ""), "name": name, "email": email,
            "account": account.get("email", "")}


def _needinfo_line(person):
    """The needinfo we'd request -- ``:nick, can you have a look please?`` -- for ``person``
    (a ``{nick, name, email, account}`` dict). Prefer the IRC nick, then the name, then the
    email. ``None`` when no usable identity is available.

    Deliberately still written when no ACCOUNT resolved and no flag will be set: naming the
    human in the prose is most of the value, and a triager who reads "Andreas Farre, can you
    have a look please?" can set the flag in one click. Silence would throw that away too."""
    person = person or {}
    nick = (person.get("nick") or "").strip()
    if nick:
        return ":{}, can you have a look please?".format(nick)
    who = (person.get("name") or person.get("email") or "").strip()
    if who:
        return "{}, can you have a look please?".format(who)
    return None


def _bug_version(channel):
    """The Bugzilla ``version`` FIELD value for a crash on ``channel`` — not the Firefox
    version string. A nightly crash is ``Trunk`` (verified present and active in Core,
    Firefox, Toolkit, DevTools and WebExtensions, i.e. every product
    ``resolve_product_component`` can return); anything else falls back to ``unspecified``,
    which exists in every product, rather than guessing at a "Firefox NNN" value that may
    not be active there. Bugzilla REJECTS a ``create_bug`` without this field."""
    return "Trunk" if (channel or "").lower() == "nightly" else "unspecified"


def build_bug_preview(uuid_info, stack, dossier, related_bugs=None):
    """The "bug we'd file" preview for the crashstack panel, and the payload the automatic
    filer posts: ``{title, comment, product, component, version, type, keywords,
    cf_crash_signature, blocked, needinfo, needinfo_email}``.

    ``comment`` is the whole bug opener as ONE comment (``build_bug_comment``) -- the
    stack, the crash reason, the volume, the analysis and the needinfo ask together, the
    way a triager reads a hand-filed crash bug. ``needinfo_email`` is the requestee the
    flag needs (the rendered ``needinfo`` line only carries a display nick).
    product/component are best-effort from the regressor (``resolve_product_component``).
    Returns ``None`` when there is no candidate regressor to file a bug against.

    ``related_bugs`` are open bugs on this signature that the automatic filer decided NOT to
    comment on because they predate the suspected regressor; passing them puts the reason in
    the bug. The page preview passes none — it does not know, because deciding needs a
    Bugzilla search and a changeset's landing date, neither of which belongs in a render.

    The metadata below ``component`` is what a hand-filed crash bug carries and what
    ``create_bug`` needs to be accepted at all: ``version``/``type`` are MANDATORY on BMO,
    and ``keywords``/``cf_crash_signature``/``blocked`` mirror what the hand-draft path sets
    in ``improve`` -- so the preview shows the whole bug rather than the parts a filer would
    then have to supply by hand.

    Deliberately NOT set: ``regressed_by``. It is the field that would assert "this crash was
    caused by that bug" as structured, tooling-visible data, and the pipeline is not accurate
    enough to claim it unattended -- the blind second opinion refutes ~74% of leads, and the
    corrected instrument puts only ~28% of them on the true regressor. The suspected regressor
    is stated in the comment prose, where a human can weigh it, and ``blocked`` records the
    association without asserting causation."""
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
            related_bugs=related_bugs,
        ),
        "product": product,
        "component": component,
        # --- metadata a create_bug needs / a hand-filed crash bug carries ---
        "version": _bug_version(channel),
        "type": "defect",
        # `regression` alongside `crash`: the whole pipeline only looks inside a build's
        # pushlog window, so every candidate it names is a suspected regression.
        "keywords": ["crash", "regression"],
        # Bugzilla's crash-signature field, same `[@ ...]` syntax as the title. This is what
        # makes the bug show up against the signature in Socorro and in BMO's crash queries.
        "cf_crash_signature": "[@ {}]".format((uuid_info.get("signature") or "").strip()),
        # Mirrors `improve`: the crash bug blocks the `clouseau` tracking bug, plus the
        # suspected regressor's own bug when we know it. An alias and a bug id are both
        # accepted here. Association, NOT a causal claim (see the docstring on regressed_by).
        "blocked": (
            ["clouseau", candidate["bug"]] if candidate.get("bug") else ["clouseau"]
        ),
        "needinfo": _needinfo_line(person),
        # The VERIFIED Bugzilla login, not the hg commit address -- BMO rejects a whole
        # create for an unknown requestee, so an unresolved account means no flag (and the
        # prose above still names the person).
        "needinfo_email": (person or {}).get("account") or "",
    }
