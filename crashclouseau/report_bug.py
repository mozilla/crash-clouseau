# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio
import functools
import re
from collections import Counter
from jinja2 import Environment, FileSystemLoader
import libmozdata.config
from libmozdata.bugzilla import Bugzilla
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
_EMAIL_RE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
# Process cache: product::component of a bug is stable, and the preview is a hot render
# path. bug_id -> (product, component) | None (None = looked up, unreadable/absent).
_PC_CACHE: dict = {}


def build_stack_comment(uuid, stack, max_frames=_MAX_PREVIEW_FRAMES):
    """Recreate, locally (no network), the crash-report comment Socorro pre-fills into its
    'report a bug' link: the crash-report URL followed by the top ``max_frames`` frames of
    the crashing thread. Sourced from the frames we already hold
    (``models.CrashStack.get_by_uuid``), so it matches the Socorro format without the
    round-trip."""
    frames = (stack or {}).get("frames") or []
    top = frames[:max_frames]
    lines = [
        "Crash report: https://crash-stats.mozilla.org/report/index/{}".format(uuid),
        "",
        "Top {} frames of crashing thread:".format(len(top)),
        "",
    ]
    for f in top:
        fn = (f.get("function") or "").strip()
        fname = (f.get("filename") or "").strip()
        line = f.get("line")
        loc = "{}:{}".format(fname, line) if (fname and line and line > 0) else fname
        desc = "  ".join(x for x in (fn, loc) if x) or (f.get("original") or "").strip()
        lines.append("{}  {}".format(f.get("stackpos"), desc).rstrip())
    return "\n".join(lines)


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


def build_bug_preview(uuid_info, stack, candidate):
    """The informative "bug we'd file" preview for the crashstack panel:
    ``{title, comment, product, component}``. The comment is recreated locally
    (``build_stack_comment``); product/component are best-effort from the regressor
    (``resolve_product_component``). Returns ``None`` when there is no candidate regressor
    to file a bug against (nothing to preview)."""
    if not candidate or not candidate.get("node"):
        return None
    product, component = resolve_product_component(candidate, uuid_info.get("channel"))
    return {
        # Match Socorro's crash-bug summary verbatim: "Crash in [@ signature]". The
        # ``[@ ...]`` is Bugzilla's crash-signature syntax, so an identical title keeps
        # these bugs searchable/dedupable alongside Socorro-filed ones.
        "title": "Crash in [@ {}]".format((uuid_info.get("signature") or "").strip()),
        "comment": build_stack_comment(uuid_info.get("uuid", ""), stack),
        "product": product,
        "component": component,
    }
