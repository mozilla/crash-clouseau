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
from urllib.parse import parse_qs, quote, urlencode, urlparse
from . import buginfo, config, models, sensitive, utils
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


def improve(query, bzdata, bugid, product=None):
    """Improve the Bugzilla query we found with other useful info.

    The regressor bug's product::component is adopted only when it belongs to the crashing
    application. A mozilla-central changeset written for a Thunderbird bug would otherwise
    retarget this draft at ``MailNews Core``, overriding the product Socorro itself pre-filled
    from the crash — see ``config.get_other_app_products``."""
    if "bugs" in bzdata and len(bzdata["bugs"]) == 1:
        bzdata = bzdata["bugs"][0]
        pc = (bzdata.get("product"), bzdata.get("component"))
        if all(pc) and pc[0] not in config.get_other_app_products(product):
            query["product"], query["component"] = pc
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
    ni = improve(bzquery, bzdata, bugid, product=info.get("product"))
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
# Skeptic findings shown in the bug. A run emits ~6; the cap is there so a verbose review cannot
# push the needinfo ask off the bottom of what anyone reads, and unverifiable entries sort first
# so the cap can never drop an open question in favour of a confirmation.
_MAX_SKEPTIC_ITEMS = 8
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


def build_frames_block(stack, max_frames=_MAX_PREVIEW_FRAMES, details=None):
    """The ``Top N frames:`` section, in the format Socorro pre-fills into a crash bug:
    ``<stackpos>  <module>  <function>  <file>:<line>``, fenced. Sourced from the frames we
    already hold (``models.CrashStack.get_by_uuid``), so no Socorro round-trip is needed.

    NAMES THE THREAD on a hang. Nothing crashed there: a watchdog killed the process because the
    main thread stopped making progress, so these frames are what the main thread is WAITING on
    and they read outside-in. Bug 2064436 was filed showing the watchdog thread's seven frames of
    boilerplate under a bare "Top 9 frames:", which told the reviewer nothing about which thread he
    was looking at — `inspector.thread_for_analysis` now picks the hung thread, and this says so."""
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
    what = ("frames of the hung main thread (nothing crashed here — a watchdog killed the "
            "process; these are what the main thread is waiting on)"
            if (details or {}).get("report_type") == "hang" else "frames")
    return "Top {} {}:\n{}".format(len(top), what, _fenced("\n".join(lines)))


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


# `report_type` so the frames block can say WHICH thread it is showing: on a hang the frames
# are the hung main thread, not a crashing one, and a reader who assumes otherwise reads the
# stack backwards. Bug 2064436 — see `inspector.thread_for_analysis`.
_REASON_COLUMNS = ["moz_crash_reason", "reason", "address", "report_type"]
# Process caches: the comment is rendered on every page view of a culprit/lead crash, and
# neither of these moves in a way that matters for a preview (a crash's reason is
# immutable; the counts only creep up). uuid -> value; keeps the render to one fetch per
# uuid per dyno, preserving the "no Socorro round-trip on the hot path" property.
_REASON_CACHE: dict = {}
_STATS_CACHE: dict = {}


def _day_str(buildid):
    """``YYYY-MM-DD`` from a 14-char buildid, for a SuperSearch ``date`` bound."""
    bid = str(buildid)
    return "{}-{}-{}".format(bid[0:4], bid[4:6], bid[6:8])


# The report date a Socorro uuid ends with (``...0260726`` -> 2026-07-26).
_UUID_DAY_RE = re.compile(r"(\d{2})(\d{2})(\d{2})$")


def _uuid_day(uuid):
    """``YYYY-MM-DD`` from a uuid's trailing ``YYMMDD``, or ``None``.

    Needed for the same reason as the ``date`` bound in ``fetch_signature_stats``:
    SuperSearch with no ``date`` searches only the last ~8 days, so looking a crash up by
    uuid alone answers NOTHING once it is older than that (measured 2026-08-11: a
    2026-07-26 uuid returned 0 hits bare and 1 hit with the anchor). Before the nightly
    window widened, every analysed crash was days old and this could not bite."""
    match = _UUID_DAY_RE.search(uuid or "")
    if match is None:
        return None
    year, month, day = match.groups()
    if not ("01" <= month <= "12" and "01" <= day <= "31"):
        return None
    return "20{}-{}-{}".format(year, month, day)


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

    params = {"uuid": uuid, "_columns": _REASON_COLUMNS, "_results_number": 1}
    day = _uuid_day(uuid)
    if day is not None:
        params["date"] = ">=" + day

    try:
        socorro.SuperSearch(
            params=params,
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
                # Anchored at the build, because SuperSearch with NO `date` silently
                # returns only the last ~8 days of REPORT dates (measured 2026-08-11:
                # a bare nightly query answered 2026-08-04..08-11 and nothing older).
                # A build whose crashes arrived longer ago than that answered `total=0`,
                # get_stats fell to its summing branch, and the filed bug said "0 crashes
                # on 0 installations" -- a needinfo about a crash it claims never
                # happened. Unreachable while every analysed build was <=5 days old;
                # reachable as soon as the nightly window widened to 21 days.
                "date": ">=" + _day_str(buildid),
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


# How far above the crash-population rate a signature's hardware-error share has to be before
# the filed bug mentions it. 2x is a low bar on purpose: this paragraph never suppresses
# anything (`_apply_bit_flip_gate` does that, at bugbot's much higher thresholds), it only tells
# the reader something they would otherwise have to go and measure. Timothy Nikkel, on bug
# 2064600: "I always look for these two things in crash reports."
#
# Reused by `_cpu_spread_sentence` for the CPU-model concentration, against
# `sigage.POPULATION_TOP_CPU_SHARE_MEDIAN` (0.32) rather than against a rate, so that sentence
# appears at a top share >= 0.64 -- 58 of 200 sampled nightly signatures, 29% (measured
# 2026-08-21). Reusing the constant instead of adding a second one is the point: the alternative
# was a fresh tunable with nothing behind it, and this one at least says what the two rates say,
# "twice the population". It suppresses nothing either way.
_HARDWARE_NOTE_LIFT = 2.0


def build_signature_age_note(corroborations, buildid=None):
    """One sentence saying when this signature first appeared, or ``""``.

    ONSET ANCHORING, which is what the bugs a human files have and ours did not. Of the archive's
    `Core :: JavaScript*` FIXED crash bugs that named a regressor, 12 of 12 also said which build
    the signature started in; and when the signature was old, 7 of 7 stopped naming a regressor and
    got actionable another way. Nine of our ten filings into that component accused a changeset
    that landed 283 to 3205 days after the signature was already crashing, and all four owner
    refutations attacked the analysis rather than the report. This is the line that lets the
    recipient make that call in one glance — or say "that is just a signature change", the way
    peterv did on bug 1898399.

    FROM THE UNBOUNDED CLOCK (``sigage.first_seen_ever``), never the windowed one. On the first
    prod day after `86f6799`, five of the seven dossiers whose windowed clock said "0 days old"
    sat on signatures 1098 to 2255 days old; printing that number would be printing the error.
    The disagreement is stated too, briefly, because a reader who checks crash-stats will see the
    truncated date and conclude WE are wrong.

    ``buildid`` is the crash's own build, used only so that "the build above" is said when the
    signature really did start in THIS build and not merely within a day of it.

    Costs nothing: ``_record_signature_age_facts`` already put all of this in ``corroborations``."""
    from crashclouseau import sigage

    c = corroborations or {}
    ever = c.get("signature_first_seen_ever")
    age_ever = c.get("signature_age_days_ever")
    windowed = c.get("signature_first_seen_windowed")
    age_win = c.get("signature_age_days_windowed")
    drift = c.get("signature_clock_drift_days")

    if c.get("signature_rename_suspected") and ever and drift is not None:
        # Never a novelty claim, only ever the withdrawal of one -- see `sigage.age_facts`.
        return ("Socorro first recorded this signature in build {} ({}), but crash-stats holds "
                "reports of it on builds up to {:.0f} days older than that, which means older "
                "crashes were re-signatured onto this name. The name is new; the crash may not "
                "be.".format(ever, sigage.buildid_day(ever), abs(drift)))
    if ever and age_ever is not None:
        note = ("This signature is {}: its first report anywhere is in build {} ({}), {}."
                .format("new" if age_ever <= sigage.NEW_SIGNATURE_DAYS else "not new",
                        ever, sigage.buildid_day(ever),
                        "the build above" if str(ever) == str(buildid)
                        else "less than a day before the build above" if age_ever < 1
                        else "{:.0f} days before the build above".format(age_ever)))
        disagree = drift is not None and drift >= sigage.CLOCK_DISAGREEMENT_DAYS
        if windowed and age_win is not None and disagree:
            note += (" (A 364-day crash-stats search only reaches {}, because Socorro's search "
                     "index keeps about six months — which is why the signature looks much newer "
                     "there.)".format(sigage.buildid_day(windowed)))
        return note
    if windowed and age_win is not None and str(windowed) == str(buildid):
        # No row in `SignatureFirstDate`. Two plain facts and a hedge, rather than an inference:
        # the table has no row mainly when a signature is newer than the daily cron that fills it
        # (measured: 7.6% of dossiers analyse a crash within an hour of its signature's first
        # report ever) or when the signature has only ever produced one report.
        return ("Crash-stats has no report of this signature on any earlier build, and Socorro's "
                "all-time signature index has no entry for it yet — as far as we can tell it is "
                "new with this build.")
    return ""


def build_stale_signature_note(corroborations):
    """One sentence saying the signature's onset runs AGAINST the changeset below, or ``""``.

    `build_signature_age_note` above says how old the signature is. This says what that number
    was USED for: ``_apply_signature_age_gate`` compared the signature's first report against the
    CANDIDATE's landing date, found the crash older, and lowered our own confidence one rung for
    it. Until this, that comparison had ONE writer and ZERO readers outside tests —
    ``stale_signature_clamped`` reached no prompt, no chip and no bug comment.

    THE PAIR IS THE POINT. A lead clamped for 202 days of staleness and then re-inflated by the
    blind second opinion ships at p_worth 0.7234, byte-identical to a clean rung-70 lead, and the
    recipient is told neither half — not that the timing evidence ran against the changeset, nor
    that the confidence they are reading rests on an independent blind agreement rather than on
    the first analysis. That is the bug-2065373 lesson (:jstutte, 2026-08-21): every claim he
    corrected was checkable against something the run already held and printed nowhere.

    SAYING IT IS NOT A REASON TO WITHHOLD THE REPORT, and the sentence must not read like one. Of
    the 11 stale filings of 2026-08, 4 were acted on (36%) against 9 of 39 fresh ones (23%), and
    bug 2062219 — 202.5 days stale, clamped, re-inflated, filed at 97% — is a topcrash, RESOLVED
    FIXED, with ``regressed_by`` set by :dveditz to exactly the bug of the changeset Clouseau
    named.

    WHAT IT MUST NOT SAY: "this changeset is not the cause", and the claim is scoped to the
    SIGNATURE for a measured reason rather than a stylistic one. The SAME POST that carries this
    paragraph sets ``regressed_by=[the candidate's bug]`` and ``keywords=[crash, regression]``
    whenever the candidate came from the build's pushlog window (``build_bug_preview``,
    report_bug.py:1328/1341) — so a sentence saying the changeset "cannot be what INTRODUCED this
    crash" contradicts our own structured field inside one filing, and on the counter-example it
    contradicts the human too: on bug 2062219 :dveditz kept the ``regressed_by`` we asserted and
    renamed the bug "(regression from bug 2043000)". SIGNATURE REUSE is why the gate downweights
    instead of abstaining — an old signature can acquire a new cause, and a rare crash can be
    made frequent by a change that did not create it. Landing late disproves the ORIGIN OF THE
    SIGNATURE, not relevance and not causation for this report, so the note states the dates and
    leaves the call to the reader, in the same voice as the crashstack.html chip.

    Three shapes, because all three get filed: flagged-only (strong-evidence is flagged and not
    moved), clamped, and clamped-then-restored. Rounds the gap the same way the chip does
    (half-to-even) so the two surfaces never print different numbers for one fact. Costs nothing:
    ``_apply_signature_age_gate`` already put every value in ``corroborations``."""
    c = corroborations or {}
    days = c.get("candidate_landed_after_first_seen_days")
    if not c.get("stale_signature") or days is None:
        return ""
    # NAME THE BUILD the gap was measured from. The paragraph immediately above this one
    # (`build_signature_age_note`) prints an age from ``signature_first_seen_ever``, Socorro's
    # all-time table; this gate measures from ``signature_first_seen_buildid``, the 364-day
    # SuperSearch answer, and the two are DOCUMENTED as disagreeing (`sigage.age_facts`,
    # `signature_clock_drift_days`, and the measured "five of the seven dossiers whose windowed
    # clock said 0 days old sat on signatures 1098 to 2255 days old" in the note above). Without
    # the buildid the filed bug prints two unexplained day counts a paragraph apart -- "3207 days
    # before the build above" and "202 days before the changeset" -- for what a reader takes to
    # be one fact. The crashstack chip already names the build for exactly this reason.
    seen = c.get("signature_first_seen_buildid")
    where = " in build {},".format(seen) if seen else ""
    note = (
        "Timing check: this signature was already being reported{} {:.0f} days before the "
        "changeset named below landed, so that changeset cannot be what INTRODUCED this "
        "SIGNATURE. It may still be relevant, and it may still be the cause of the crash in "
        "this particular report — an old signature can acquire a new cause, and a rare crash "
        "can be made frequent by a change that did not create it — but the signature's own "
        "history is not evidence that it is.".format(where, days)
    )
    if not c.get("stale_signature_clamped"):
        return note
    if c.get("second_opinion_boosted"):
        return note + (
            " Clouseau lowered its own confidence one step for that, and then put it back: an "
            "independent blind re-analysis, given none of the reasoning above, still agreed the "
            "changeset can cause this crash. The confidence stated below is that restored one.")
    return note + " Clouseau lowered its own confidence one step for that."


def build_hardware_note(corroborations):
    """One paragraph on how much of this signature is hardware error, or ``""``.

    WHAT BUG 2064600 SHOULD HAVE SAID. We filed a display-list crash at 97% worth-investigating;
    Timothy Nikkel answered twenty minutes later with two facts about the signature that we had
    not looked up and he checks by hand every time — 50% of its crashes carry a bit-flip
    probability, and many of the rest are on the known-defective Raptor Lake CPU. Both were a
    single Socorro aggregation away, and printing them is worth more than the paragraph costs:
    it is the first thing the recipient will want to know, and if we are wrong about the crash
    it is also the fastest route to finding that out.

    Costs NOTHING to produce. ``_apply_bit_flip_gate`` already measured all of this and left it
    in ``corroborations`` (see ``sigage.hardware_noise``); a bug that gets filed at all is by
    construction one whose shares were UNDER the suppression thresholds, so this is the residual
    doubt rather than a contradiction of the filing.

    Says explicitly whether THIS report is implicated, because that is the distinction the
    numbers otherwise hide and the one that decides what the reader should do next: on bug
    2064600 the signature was mostly hardware — 60% bit flips and 20% Raptor Lake on Firefox
    nightly, re-measured 2026-08-21 over 5 reports — and the triaged report was clean, which is
    precisely the case Nikkel called "the next step". (The 71% quoted here before came from all
    products and all channels over 180 days, 29.3% + 42.6% of 331 reports: the denominator
    `sigage.hardware_noise` refuses, because it suppresses bug 2062219, FIXED.)

    THE CPU-MODEL SPREAD rides in the same paragraph when it clears `_HARDWARE_NOTE_LIFT`, so
    the human reads the number the crash brief gave the model (`triage._cpu_spread_line`). On
    bug 2065373 it is the WHOLE note — 58 of 58 reports on one AMD model, `broken_cpu_rate` 0.0,
    no bit flip — which is why it has to be able to stand alone: the signature's only unusual
    property is the one nothing else printed."""
    c = corroborations or {}
    sample = c.get("signature_hardware_sample")
    flip = c.get("signature_bit_flip_rate")
    cpu = c.get("signature_broken_cpu_rate")
    if not sample or (flip is None and cpu is None):
        return ""
    from crashclouseau import sigage

    parts = []
    if flip is not None and flip >= _HARDWARE_NOTE_LIFT * sigage.POPULATION_BIT_FLIP_RATE:
        parts.append("{:.0f}% carry a possible-bit-flip annotation (crash population: "
                     "{:.0f}%)".format(100 * flip, 100 * sigage.POPULATION_BIT_FLIP_RATE))
    if cpu is not None and cpu >= _HARDWARE_NOTE_LIFT * sigage.POPULATION_BROKEN_CPU_RATE:
        parts.append("{:.0f}% come from the known-buggy Intel Raptor Lake (family 6 model 183 "
                     "stepping 1, bug 1975808; crash population: {:.0f}%)".format(
                         100 * cpu, 100 * sigage.POPULATION_BROKEN_CPU_RATE))
    # Whether the crash we actually analysed is one of the suspect ones. Both flags are recorded
    # for every verdict, so an absent one means "not established" and is left unsaid.
    cpu_known = "report_on_broken_cpu" in c
    clean = cpu_known and not c["report_on_broken_cpu"]
    clean = clean and c.get("possible_bit_flip_confidence") is None
    tail = (" The report analysed above is not one of them — it has no bit-flip annotation and "
            "did not come from one of those CPUs — so if this signature is worth anything, it "
            "is worth it on crashes like that one." if clean else "")
    note = ""
    if parts:
        note = ("Hardware-error share of this signature: of its {} reports on this channel over "
                "the last year, {}.{}".format(sample, "; and ".join(parts), tail))
    spread = _cpu_spread_sentence(c)
    if spread:
        note = (note + " " + spread) if note else spread
    return note


def _cpu_spread_sentence(corroborations):
    """The CPU-model concentration for the filed bug, when it is unusual, or ``""``.

    THE SAME NUMBER THE CRASH BRIEF GAVE THE MODEL (`triage._cpu_spread_line`), stated with the
    same 0.32 population median, because a bug comment and a prompt disagreeing about a figure
    is how a reviewer stops trusting both. :jstutte asked for exactly this on bug 2065373 —
    "could clouseau do some OS / install distribution checks on the socorro data?" — after a
    filing whose 58 reports all came from one AMD model and whose comment 0 said nothing about
    it.

    A SCOPE HINT, NEVER AN ACCUSATION, and the wording is the whole safeguard: 13% of nightly
    signatures sit on a single model, and of the 20 one-model signatures with >=20 reports in
    the background sample 8 carry a real Firefox bug. Every attempt to turn this into a
    suppression threshold ate a FIXED filing (`orchestrator._signature_is_mostly_hardware`), so
    the sentence has to carry its base rate or it becomes that suppressor in the reader's head.

    AND IT SAYS NOTHING UNDER `sigage.POPULATION_TOP_CPU_SHARE_MIN_REPORTS`. Unlike the two rate
    sentences this one has no sample floor of its own, and without one it would print on almost
    every filing for free: a one-report signature has one `cpu_info` string, so its share is
    1.00 by construction and clears any lift. Of the 27 filings among the canary's 52 whose
    share clears `_HARDWARE_NOTE_LIFT`, 19 have fewer than 5 reports and 17 have exactly one
    (measured 2026-08-21) -- so the floor is the difference between a fact about the population
    and a sentence in every bug comment saying 100% of one report is one model."""
    from crashclouseau import sigage

    c = corroborations or {}
    share = c.get("signature_top_cpu_share")
    seen = c.get("signature_cpu_reports")
    terms = c.get("signature_cpu_terms")
    if share is None or not seen or not terms:
        return ""
    if seen < sigage.POPULATION_TOP_CPU_SHARE_MIN_REPORTS:
        return ""
    if share < _HARDWARE_NOTE_LIFT * sigage.POPULATION_TOP_CPU_SHARE_MEDIAN:
        return ""
    return ("CPU-model spread: {:.0f}% of the {} reports that carry a cpu_info string are on "
            "{}{}. The median Firefox-nightly signature sits at {:.0f}% and 13% of them are on "
            "a single model, so this is a hint about SCOPE — a driver, a distribution, an "
            "instruction set — and not a hardware verdict.".format(
                100 * share, seen, c.get("signature_top_cpu_term") or "one model",
                ", the only model seen" if terms == 1
                else ", one of {} models seen".format(terms),
                100 * sigage.POPULATION_TOP_CPU_SHARE_MEDIAN))


def build_exposer_note(corroborations):
    """One paragraph saying the fault address is freed/poisoned memory and the changeset below
    may only have EXPOSED an older defect, or ``""``.

    ``_classify_exposer`` has written ``exposer_suspected`` / ``exposer_signals`` /
    ``exposer_strong`` onto every dossier since 2026-07-22 and NOTHING has ever read them: not a
    prompt, not a bug comment, and not the UI either — ``crashstack.html`` renders 16 named
    corroboration keys and ``exposer_*`` is not among them, so they only ever rode the persisted
    JSONB. This is the first thing the recipient of a poison-fault filing needs, and it costs
    nothing: the classifier already measured it.

    IT IS THE HALF THAT SURVIVES THE RUNG CHANGE. ``_classify_exposer`` used to answer a poison
    fault by clamping the verdict to ``medium`` (50), under ``autofile.min_confidence`` (70) —
    a silent suppression. It now lands ON that floor, so these crashes become ELIGIBLE to be
    filed (the stale-signature, absent-thread, backout and hardware gates below it can each
    still take the rung away), and the honesty that used to live in the clamp has to live in
    the text instead.
    spike/STRATEGY_REPORT.md:146 asked for exactly that pairing: "never auto-upgrade a
    proximity hit to 'culprit' when exposer corroborators fire — emit `lead` + needinfo the
    owner".

    GATED ON ``exposer_strong``, NOT ON ``exposer_suspected``, and that is the whole scope of
    it. The strong signal is the poison fault address: it has in-tree provenance
    (``orchestrator._POISON_BYTES``) and a measured base rate — 2,971 of 158,285 nightly
    reports with a parseable address over 89 days, 1.88%. The WEAK signals that also set
    ``exposer_suspected`` — ``failure_class=uaf``, a PHC free stack, a ``data_flow.operation``
    of free/uaf — are true of essentially every lifetime crash we file on, so keying the note
    on them would print a hedge paragraph onto filings where nothing specific was measured.

    WHAT IT MUST NOT SAY, and does not: that the changeset is innocent. Of the 289-bug study's
    86 exposers, 84 (98%) carry an accepted ``regressed_by``, against 196/203 (97%) for its
    non-exposers, so nominating an exposer is the accepted answer — and four of the five study
    bugs whose evidence quotes a poison literal (2000421, 1991950, 1980730, 2000425) are not
    exposers at all, but genuine regressors that happen to crash on freed memory. Hence a
    paragraph that asks the reader to PLACE the changeset, never one that apologises for it."""
    c = corroborations or {}
    if not c.get("exposer_strong"):
        return ""
    addr = ""
    for signal in c.get("exposer_signals") or []:
        for token in str(signal).split():
            if token.startswith("0x"):
                addr = token
                break
        if addr:
            break
    return (
        "The fault address{} is a run of one poison byte — the fill a freed or "
        "never-initialised allocation is stamped with — so this is a use-after-free or an "
        "uninitialised read, and the lifetime bug behind it may be older than the changeset "
        "below. Worth deciding which: did that changeset INTRODUCE the defect, or only EXPOSE "
        "it by changing timing, ordering or allocation? Both are useful answers, and an "
        "exposer is still recorded as `regressed_by`.".format(" " + addr if addr else "")
    )


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
    landing_unresolved=False,
    other_app_bugs=None,
    meta_bugs=None,
    max_frames=_MAX_PREVIEW_FRAMES,
):
    """The SINGLE comment the filed bug opens with, in the shape a triager expects from a
    hand-filed crash bug (cf. bug 2057432 comment 0):

    1. the crash-report link;
    2. the crash reason (``MOZ_CRASH Reason:`` for a panic, else the OS reason + address);
    3. the top ``max_frames`` frames, fenced, with the module column;
    4. one sentence on how much this signature is crashing;
    4a. when the signature first appeared, which is the onset anchor every hand-filed crash
        bug that names a regressor carries and ours did not;
    4b. whether that onset runs AGAINST the changeset in 5 — and, when it does, whether the
        blind second opinion is what put the stated confidence back;
    4c. how much of the signature is hardware error, when that is above background;
    4d. whether the fault address says this is freed/poisoned memory, in which case the
        changeset in 5 may only have EXPOSED a lifetime bug that is older than it;
    5. the Clouseau analysis + suspected regressor;
    6. searchfox/hg links for the code the analysis cites;
    7. why this is a new bug rather than a comment on ``related_bugs`` / ``other_app_bugs`` /
       ``meta_bugs``, when there are any — three different reasons, never merged;
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
        build_frames_block(stack, max_frames=max_frames, details=details),
        build_stats_sentence(first, stats, info),
        build_signature_age_note((dossier or {}).get("corroborations"), info.get("buildid")),
        build_stale_signature_note((dossier or {}).get("corroborations")),
        build_hardware_note((dossier or {}).get("corroborations")),
        build_exposer_note((dossier or {}).get("corroborations")),
        _explanation_comment(
            (dossier or {}).get("verdict"), (dossier or {}).get("candidate"), channel,
            corroborations=(dossier or {}).get("corroborations"),
        ),
        build_code_references((dossier or {}).get("verdict"), channel),
        build_skeptic_block(dossier),
        build_dissent_note(dossier),
        build_related_bugs_note(
            related_bugs, landing_unresolved=landing_unresolved,
            node=((dossier or {}).get("candidate") or {}).get("node")),
        build_other_app_bugs_note(other_app_bugs),
        build_meta_bugs_note(meta_bugs),
        needinfo,
        _PROVENANCE,
    ]
    return "\n\n".join(s for s in sections if s)


# Last line of every filed bug. One sentence, not a section: the reviewer of bug 2061961 had to
# INFER that a machine wrote the analysis ("the automated analysis suspected my patch"), and a
# reader who knows can discount it themselves without the prose hedging every clause. The
# invitation to resolve it is not politeness — an INVALID from someone who knows the code is a
# useful outcome, and the alternative is a stale needinfo nobody wants to be rude about.
_PROVENANCE = (
    "_Filed automatically by [Clouseau](https://github.com/mozilla/crash-clouseau), which "
    "analyses nightly crashes with an LLM. Nothing above was written or checked by a human. "
    "Please close it as INVALID if it is wrong — that is useful feedback, not a nuisance._\n\n"
    # THE ATTRIBUTION SENTENCE, and it is not politeness. :mccr8 on bug 2065051: the dupe "is
    # going to kind of cause a bit of a headache for the bug bounty process, because at a casual
    # glance it looks like we found it internally and thus won't pay a bounty, but of course we
    # only saw that crash because of the report." A machine filing reads as an internal discovery
    # unless it says otherwise, and the reporter loses the bounty to that impression.
    #
    # The rule behind it, stated once: a machine-filed bug should always be the duplicate SOURCE,
    # never the duplicate target. We have no claim to precedence over the human whose report is
    # the only reason we saw the crash at all.
    "_This is not an independent Mozilla discovery: the crash is known here only because "
    "somebody submitted the crash report linked at the top. If this duplicates an existing "
    "report, please resolve THIS bug as the duplicate and leave the credit with the earlier "
    "reporter._"
)


def build_related_bugs_note(related_bugs, landing_unresolved=False, node=None):
    """Why this is a NEW bug when ``related_bugs`` are open on the same signature, or ``""``.

    Two reasons, two sentences, because they are not the same claim. Normally the filer skips
    past an open bug because that bug predates the suspected regressor
    (``bugzilla_apply._bug_for_this_regression``). Under ``landing_unresolved`` it skipped past
    it because it could not resolve when the changeset landed at all, and the ordinary wording
    — "they were filed before the changeset above landed" — would then be asserting the very
    thing the run failed to establish. That is the defect :jstutte flagged on bug 2065373,
    where the filed bug stated as fact something checkable against data the run already held
    and had not checked; a reader who cannot tell a verdict from a blind spot cannot overrule
    either. ``node`` names the changeset when we have it, and is omitted rather than guessed.

    KNOWN GAP, and it is the same class of defect: the SAME comment can carry section 4b
    (``build_stale_signature_note``), which says the signature was already being reported "N
    days before the changeset named below landed" — a sentence that only exists because
    ``_apply_signature_age_gate`` DID resolve that landing date, from the seed's free
    ``candidate_pushdates`` map rather than from hg (orchestrator.py:1568). The filer's
    ``_candidate_landed`` has no access to that map and asks hg, so on the poisoned-cache path
    one bug can state the gap in 4b and disclaim all knowledge of it here. Rare — it needs a
    stale filing (~24% of filings), an open same-application bug (~18%) and an hg blip (~2%) at
    once, so of order one filing a year — but it is not hypothetical, and the fix is the
    recommendation's unshipped adjunct: give the filer the pushdate the run already has.

    Said in the bug itself, where the triager deciding whether to duplicate it can see the
    reasoning: an unexplained second bug on a live signature just looks like a broken
    deduplicator."""
    bugs = [b for b in (related_bugs or []) if b]
    if not bugs:
        return ""
    if landing_unresolved:
        return (
            "Filed as a new bug rather than a comment on {} — {} open on this signature, but "
            "we could not resolve when {} landed, so we could not tell whether {} about this "
            "regression. Please duplicate if {}.".format(
                ", ".join("bug {}".format(b) for b in bugs),
                "which is" if len(bugs) == 1 else "which are",
                "changeset {}".format(node) if node else "the changeset above",
                "it is" if len(bugs) == 1 else "they are",
                "it is" if len(bugs) == 1 else "one of them is",
            )
        )
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


def build_other_app_bugs_note(other_app_bugs):
    """Cross-reference the open bugs on this signature that belong to ANOTHER application built
    on Gecko, or ``""``.

    Same purpose as ``build_related_bugs_note`` and a different reason: those bugs predate the
    cause, these ones are somebody else's product (``bugzilla_apply._split_by_application``).
    Worth saying rather than silently dropping, because the shared signature IS a real link —
    bug 2057980 spent a day settling whether ``nsDocShellLoadState`` was Thunderbird's or
    Gecko's — and a second bug on a live signature otherwise just looks like a broken
    deduplicator.

    Rows are ``{"id", "product", ...}`` as the lookup returns them; the application is named
    through its product, which is what BMO actually gives us."""
    bugs = [b for b in (other_app_bugs or []) if (b or {}).get("id")]
    if not bugs:
        return ""
    products = sorted({b.get("product") for b in bugs if b.get("product")})
    return (
        "{} {} this signature too, but {} filed in {} — {} built on the same Gecko, which "
        "shares the signature and not this crash. Filed here rather than commented there; "
        "please duplicate if it is the same defect.".format(
            ", ".join("bug {}".format(b["id"]) for b in bugs),
            "references" if len(bugs) == 1 else "reference",
            "it is" if len(bugs) == 1 else "they are",
            ", ".join(products) or "another product",
            "another application" if len(products) < 2 else "other applications",
        )
    )


def build_meta_bugs_note(meta_bugs):
    """Cross-reference the open ``[meta]`` trackers on this signature, or ``""``.

    The third reason the filer files past an open bug (``bugzilla_apply._split_out_metas``),
    after "it predates the cause" and "it is another application's". A meta bug is a list of
    other bugs: an analysis posted into one sits among its dependencies instead of in front of
    anyone, and the needinfo goes to whoever owns the tracker. Named rather than dropped
    because the tracker IS the right place for the link — bug 1279293 tracks every
    ``IPCError-browser | ShutDownKill`` there is — just not the right place for the analysis.

    Rows are ``{"id", "keywords", ...}`` as ``_open_bugs_for_signature`` returns them."""
    bugs = [b for b in (meta_bugs or []) if (b or {}).get("id")]
    if not bugs:
        return ""
    return (
        "{} {} this signature too, but {} [meta] tracking {}, so an analysis posted there "
        "would sit among the dependencies rather than in front of anyone. Filed here instead; "
        "please add it to the tracker if it belongs.".format(
            ", ".join("bug {}".format(b["id"]) for b in bugs),
            "references" if len(bugs) == 1 else "reference",
            "it is a" if len(bugs) == 1 else "they are",
            "bug" if len(bugs) == 1 else "bugs",
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


# product -> BMO's own default security group, memoised for the process. Not a hardcoded map:
# measured on production BMO 2026-08-24, `Core` is `core-security` while `Firefox`, `Toolkit` and
# `DevTools` are all `firefox-core-security`. A wrong or inapplicable group makes BMO refuse the
# create outright (401, code 120, no bug), so a hardcoded `core-security` would have silently
# stopped filing every non-Core bug -- and it looks right, because `core-security` IS accepted in
# every product on the allizom clone, whose group configuration is not production's.
_SEC_GROUP_CACHE = {}


def security_group(product):
    """BMO's ``default_security_group`` for *product*, or ``None`` if it cannot be determined.

    ``None`` is a REFUSAL, not a default: ``autofile_bug`` declines to file rather than filing
    a memory-safety analysis publicly. That matches Treeherder's own behaviour -- its filer
    answers HTTP 400 "Cannot file security bug for product without default security group"
    instead of falling through -- and it is the direction the asymmetry demands. Note this is
    reachable for Fenix only if BMO returns the product anonymously; it did not on 2026-08-24,
    so Fenix support (plans/16) must fail closed here rather than fall through."""
    if not product:
        return None
    if product in _SEC_GROUP_CACHE:
        return _SEC_GROUP_CACHE[product]
    group = None
    try:
        # Same base as the writes, so the group is read from whichever Bugzilla the filing
        # will actually go to -- reading production's group and filing to allizom (or the
        # reverse) is the shape of mistake that files a bug into a group that does not exist.
        from crashclouseau.bugzilla_apply import _bz_rest
        # `_bz_rest()` is the BUG endpoint (".../rest/bug"), and `BUGZILLA_REST_URL` is set to
        # the same shape, so trim the suffix rather than assuming a base -- otherwise this reads
        # `/rest/bug/product/Core` and BMO answers 404 code 32614, which `security_group`'s
        # except-clause would quietly turn into "no group" and hence "do not file".
        base = re.sub(r"/bug/?$", "", _bz_rest())
        url = "{}/product/{}".format(base, quote(product))
        r = net.get(url, params={"include_fields": "name,default_security_group"}, timeout=30)
        for p in (r.json().get("products") or []):
            if (p.get("name") or "") == product:
                group = p.get("default_security_group") or None
    except Exception:                                       # noqa: BLE001
        logger.warning("autofile: could not read the security group for %r", product,
                       exc_info=True)
        group = None
    # A failure is NOT cached: unlike an unreadable bug (which stays unreadable), this is a
    # transient read of stable configuration, and caching None would make one blip disable
    # security filing for the life of the dyno.
    if group:
        _SEC_GROUP_CACHE[product] = group
    return group


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


def resolve_product_component(candidate, channel, product=None):
    """``(product, component)`` for the bug we would file, best-effort + never raises:

    1. the REGRESSOR bug's own product::component;
    2. if that bug is unreadable (e.g. a security regressor bug), the MOST FREQUENT
       product::component across the regressor author's recent patches' bugs;
    3. ``(None, None)`` when neither resolves.

    A pair belonging to another application built on Gecko is never returned, at either rung:
    a mozilla-central changeset is regularly written FOR a Thunderbird bug — that is what
    ``MailNews Core`` mostly is — and inheriting its component would drop a Firefox crash on
    Thunderbird's triage queue. ``product`` is the crash's own Socorro product; leaving it
    unset exempts nobody (``config.get_other_app_products``).

    Resolving to nothing is an acceptable outcome of that, not a failure to paper over:
    ``bugzilla_apply.autofile_bug`` refuses to file without a pair, which is this module's
    standing preference over filing into a component that has no idea why it got the bug."""
    if not candidate:
        return None, None
    foreign = config.get_other_app_products(product)
    try:
        bug = candidate.get("bug")
        if bug:
            pc = _bugs_product_component([bug]).get(int(bug))
            if pc and pc[0] in foreign:
                logger.info("bug preview: regressor bug %s lives in %s, another application's "
                            "product — not filing a %s crash there", bug, pc[0], product or "?")
                pc = None
            if pc:
                return pc
        node = candidate.get("node")
        info = models.Node.authors_for([node], channel).get(node, {}) if node else {}
        email = info.get("email") or _first_email(candidate.get("author"))
        if email:
            bugs = models.Node.recent_bugs_by_author(email, channel)
            pcs = {b: pc for b, pc in _bugs_product_component(bugs).items()
                   if pc[0] not in foreign}
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


def is_suspected_regression(corroborations):
    """May the filed bug call its candidate a REGRESSOR? Tri-state: ``True``/``False``/``None``.

    True only when the candidate came from the crash build's own pushlog window
    (``orchestrator._record_window_membership``), because that is the only recency evidence the
    pipeline ever has. ``None`` means nobody recorded it -- old dossiers, and offline runs -- and
    is treated as "no" by every caller: an unproven regression claim is the thing being fixed, so
    silence must not license it.

    Held to a deliberately narrow standard because of what the claim COSTS. It is not a turn of
    phrase: it sets the ``regression`` keyword that release management triages on, links the bug
    into a stranger's blocks list, and points a needinfo at the person named. On bug 2062119 all
    three fired for a changeset from 2022 while the run's own skeptic pass was recording "a
    pre-existing latent race, not a new regression".

    WHAT THE FLAG GATES, exactly, because the paragraph above is now one release out of date:
    this prose, the ``regression`` KEYWORD, and the ``regressed_by`` field
    (``build_bug_preview``'s ``keywords`` and ``link_regressor``). It does NOT gate the needinfo
    or the ``blocked`` list — both are unconditional now, and the regressor's own bug was removed
    from ``blocked`` when ``regressed_by`` replaced it. And it deliberately does NOT gate the
    calibrated "N% worth investigating" number, which is a measured decision rather than an
    omission. Keying the number on it too — so the caveat and the number in the same comment
    could not disagree — was tried on 2026-08-21 and refuted: on
    corpus_ship the out-of-window arm is 8/21 = 0.381, but 12 of those 21 are culprit-DELETED
    negatives that cannot score ``worth`` at all, the informative rows read 8/9 = 0.889 against
    26/26 (Fisher p = 0.257), and all 12 reported rung-70+ negatives are out-of-window, so the
    flag is a proxy for the corpus's own ``is_negative`` label. Bug 2062806 is the wrong-direction
    case: out-of-window, this caveat printed, and hzhao confirmed the mechanism and backed the
    named changeset out. See ``config.get_agent_calibration``."""
    return (corroborations or {}).get("candidate_in_pushlog_window")


def build_dissent_note(dossier):
    """What THIS RUN found that points against the analysis above, or ``""``.

    THE DEFECT THIS EXISTS FOR. Reviewing bug 2065373, :jstutte corrected three claims, and every
    one of them was checkable against a fact the run already held and had not checked. The general
    shape is measurable: on the 500-dossier prod snapshot of 2026-08-24, 27 of 496 runs carried
    `second_opinion_refuted` and in **27 of 27** the run's own skeptic had marked at least one
    claim `pass` -- 10 of them on a claim named `mechanism`. So "a pass sitting under a
    contradiction the run itself produced" is a ~5% population defect, and the reader of the bug
    could not see the contradiction at all: `second_opinion_refuted` reached crashstack.html and
    nothing else.

    WHY DISAGREEMENT AND NOT AGREEMENT. The mirror sentence -- "an independent blind review
    agreed" -- was measured and rejected. 17 of 17 filed bugs in the snapshot carry a
    corroborating second opinion, so the sentence is constant on the surface that would print it;
    worse, BOTH known-wrong filings carry one at confidence `high` with an SO mechanism restating
    the very claim a reviewer refuted (2065969, RESOLVED/INVALID, and 2065373 itself). Printing
    agreement there inflates authority against the one thing `_PROVENANCE` is written to invite.
    A refutation has the opposite profile: it is rare on this surface, it always points our way,
    and it is the strongest thing we know that we are currently withholding.

    Deliberately NOT listed here: `stale_signature` and `exposer_suspected`. Both already have
    their own note in this comment (`build_stale_signature_note`, `build_exposer_note`); a second
    copy grouped under a new heading is duplication, not disclosure. This block is for the
    contradictions that reach no other surface, and adding a flag to it is the one-line way to
    keep the next one from going quiet."""
    c = (dossier or {}).get("corroborations") or {}
    if not c.get("second_opinion_refuted"):
        return ""
    so = (dossier or {}).get("second_opinion") or {}
    line = ("- A second, independent analysis of this crash \u2014 a separate agent given the "
            "crash and the candidate changeset but **none** of the reasoning above \u2014 "
            "concluded that the candidate cannot explain it")
    conf = (so.get("confidence") or "").strip()
    if conf:
        line += " (its own confidence in that: {})".format(conf)
    why = (so.get("refutation") or "").strip()
    line += ". {}".format(why) if why else "."
    lines = [line]
    if c.get("second_opinion_clamped_strong") or c.get("second_opinion_downgraded_strong"):
        lines.append("- The confidence above was lowered a band because of it; it is not the "
                     "rung the primary analysis asked for.")
    return ("**This run also produced evidence against the analysis above.** It is reported "
            "rather than resolved, because the two passes disagree and neither was allowed to "
            "overrule the other:\n" + "\n".join(lines))


def build_skeptic_block(dossier, max_items=_MAX_SKEPTIC_ITEMS):
    """The skeptic pass's own findings, or ``""`` -- the caveats, in the bug, with the analysis.

    THE POINT OF THIS SECTION. Every claim below already existed, was already shown on
    crashstack.html, and was already dropped on the way to Bugzilla. Bug 2062119's run recorded
    "file_history ... shows no landings near 2026-08-08 touching the relevant lines ... This is a
    pre-existing latent race, not a new regression" and "all three seed changesets ... touch none
    of the crash-path files" -- and then filed a bug asserting a regressor at "confidence high".
    The reviewer's first reply disputed exactly that, and the feedback afterwards was to be more
    speculative about the unsure parts. The unsure parts were known; they just were not sent.

    ``unverifiable`` entries lead, because an open question is what a reader most needs and is
    the easiest thing for them to close. Statuses are printed verbatim rather than rewritten as
    prose: ``pass`` on a claim named ``no_recent_regressor`` means the skeptic CONFIRMED there is
    no recent regressor, which no automatic paraphrase gets right."""
    items = [s for s in (dossier or {}).get("skeptic") or [] if isinstance(s, dict)]
    if not items:
        return ""
    order = {"unverifiable": 0, "fail": 1, "pass": 2}
    items = sorted(items, key=lambda s: order.get(s.get("status"), 3))[:max_items]
    lines = []
    for item in items:
        note = (item.get("note") or "").strip()
        ref = (item.get("claim_ref") or "").strip() or "(unnamed check)"
        lines.append("- **{}** {}{}".format(
            item.get("status") or "?", ref, " — " + note if note else ""))
    return ("What the automated skeptic pass checked (its own words — a `pass` means the check "
            "succeeded, which is not always support for the conclusion):\n" + "\n".join(lines))


def _explanation_comment(verdict, candidate, channel=None, corroborations=None):
    """The Clouseau analysis comment we'd post to the filed bug: the crash mechanism (and,
    when present, why it is consistent with the crash) plus the candidate changeset -- the
    latter carrying an hg and a GitHub link when ``channel`` tells us which repo it is in.
    ``None`` when there is nothing substantive to say.

    Two things are deliberately NOT stated the way the pipeline stores them:

    * the rung. ``confidence high`` reads as "I am sure this is the cause"; the number the
      pipeline actually calibrated is ``p_worth_investigating``, and it was fit at PERSON level
      -- "worth someone's time", not "this changeset did it". crashstack.html has rendered it as
      "N% worth investigating" since the calibration landed; the bug said "confidence high".
    * the candidate. "Suspected regressor" is a causal claim, and one the pipeline earns only
      inside the build's pushlog window (``is_suspected_regression``). Outside it, the changeset
      is a place to start looking, and saying so is what invites the correction that makes these
      bugs work: on bug 2062119 the reviewer rejected the named changeset, found the real origin
      himself and attached two patches."""
    verdict = verdict or {}
    lines = []
    mech = ((verdict.get("mechanism") or {}).get("statement") or "").strip()
    cons = ((verdict.get("consistency") or {}).get("statement") or "").strip()
    if mech:
        lines.append("Clouseau analysis (automated{}). The mechanism below fits the evidence "
                     "but is not proven end-to-end:\n\n{}".format(
                         _worth_phrase(verdict), mech))
    if cons:
        lines.append(cons)
    c = candidate or {}
    if c.get("node"):
        link = changeset_links(c["node"], channel, c.get("git_commit") or "")
        if c.get("bug"):
            link += " (bug {})".format(c["bug"])
        author = (c.get("author") or "").strip()
        if author:
            link += " by {}".format(author)
        if is_suspected_regression(corroborations):
            lines.append("Suspected regressor: {}.".format(link))
        else:
            # Everything the pipeline has here is "this code is on the crash path", which is a
            # starting point and not an origin. Ask for the correction outright -- it is the
            # single most useful thing the reader can give back, and on bug 2062119 it is what
            # produced the fix.
            lines.append(
                "Starting point — NOT a suspected cause: {}.\n\nThis changeset did not land in "
                "this build's pushlog window, so there is no evidence here that the crash is a "
                "recent regression from it; it is named only as the closest thing found on the "
                "crash path. If you know where this actually comes from, that correction is the "
                "most useful thing you could leave on this bug.".format(link))
    return "\n\n".join(lines) if lines else None


def _worth_phrase(verdict):
    """`` — N% worth investigating`` from the calibrated probability, or ``""``.

    The rung name is deliberately not offered as a fallback: with no calibrated number the honest
    thing is to claim nothing, not to fall back on the word that caused the problem."""
    p = (verdict or {}).get("p_worth_investigating")
    try:
        pct = round(float(p) * 100)
    except (TypeError, ValueError):
        return ""
    return (", {}% worth investigating — a calibrated estimate that this is worth someone's "
            "time, not that the changeset below caused it".format(pct))


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


def _is_bot(email, name="", nick=""):
    """``agent.experts._is_bot``, imported on call. ``report_bug`` is pulled in by ``html``
    on the first web request, and ``crashclouseau/agent/__init__`` states that nothing in the
    agent package is imported at Flask-app startup; keeping that true costs one lazy import,
    the same shape and the same reason as ``orchestrator._crashing_area_experts``."""
    from .agent.experts import _is_bot as impl
    return impl(email, name, nick)


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
    domains a bare local part is weak evidence that two addresses are one human.

    That paragraph was right about the hazard and wrong about the defence, so the defence is
    now here: an AUTOMATION account is dropped from ``people`` before any key is tried. The
    old ``_BOT_MARKERS`` substring rule did not match ``moz-wptsync-bot <wptsync@mozilla.com>``
    at all, and the hole it did leave is one space wide -- against the five real Updatebot bugs
    in our own filing windows (2059609, 2059649, 2059934, 2059935, 2064449) the hg name
    "Updatebot" matches nobody, but "Update Bot" resolves key 2 to ``update-bot@bmo.tld``
    "Update Bot" and the bare local part ``update-bot`` resolves key 3 to the same account.
    Updatebot lands 121 source changesets a year in vendored media/crypto code (security/nss,
    gfx/harfbuzz, third_party/aom, media/libvpx, media/libopus), so that is a real crash class,
    not a hypothetical. Measured cost: 0 of the 51 needinfo requestees our filings have
    actually set is dropped by this filter."""
    want_email = (email or "").strip().casefold()
    want_name = _norm_name(name)
    want_local = want_email.split("@")[0]
    # Never let ANY key land on a robot -- see the paragraph above.
    people = [p for p in (people or [])
              if not _is_bot(p.get("email") or "", p.get("real") or "", p.get("nick") or "")]
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
    # Never ask a robot to investigate its own crash. STATE OF THE EVIDENCE, so nobody
    # re-derives it: no filing has done it yet. The 51 needinfos our filings have set are all
    # human (BMO creator=cdenizet, >=2026-08-05, "Crash in [@"; 0/51, 95% CI 0.0-7.0%) -- but
    # that panel is the resolved ACCOUNT and this line reads the HG AUTHOR, so the figure that
    # bounds it is the other one: of the 475 distinct in-window hg author triples across those
    # same 26 build windows this rule fires on 10, and all 10 are real automation. Rung 1
    # cannot reach a bot either -- all 14 automation identities in a year of mozilla-central
    # (ffxbld@ at mozilla.com and lando.moz.tools, updatebot@, wptsync@, release+landoscript@,
    # lando@ at lando.moz.tools and lando.test, blink-w3c-test-autoroller@, luci-bisection@
    # appspot and crash@system on gserviceaccount.com, and wpt-pr-bot@, dependabot[bot]@,
    # github-actions[bot]@, servo-wpt-sync@ on users.noreply.github.com) answer
    # ``{"exists": False}`` from ``_bugzilla_user``. What this line closes that nothing else
    # does is the LAST rung: when BMO cannot be reached ``_bugzilla_user`` says ``unverified``
    # and ``_needinfo_account`` hands back the raw hg address, bot or not. It also suppresses
    # the PROSE line, which ``_needinfo_line`` writes even when no account resolves. The rungs
    # that can reach a bot ACCOUNT are closed in ``_match_author``.
    if _is_bot(email, name, ""):
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


def build_bug_preview(uuid_info, stack, dossier, related_bugs=None, other_app_bugs=None,
                      landing_unresolved=False, meta_bugs=None):
    """The "bug we'd file" preview for the crashstack panel, and the payload the automatic
    filer posts: ``{title, comment, product, component, version, type, keywords,
    cf_crash_signature, blocked, needinfo, needinfo_email}``.

    ``comment`` is the whole bug opener as ONE comment (``build_bug_comment``) -- the
    stack, the crash reason, the volume, the analysis and the needinfo ask together, the
    way a triager reads a hand-filed crash bug. ``needinfo_email`` is the requestee the
    flag needs (the rendered ``needinfo`` line only carries a display nick).
    product/component are best-effort from the regressor (``resolve_product_component``).
    Returns ``None`` when there is no candidate regressor to file a bug against.

    ``related_bugs``, ``other_app_bugs`` and ``meta_bugs`` are open bugs on this signature that
    the automatic filer decided NOT to comment on — respectively because the changeset landed
    after they were filed (or, under ``landing_unresolved``, because we could not tell WHEN it
    landed, which is a different sentence and not the same claim), because they belong to
    another application built on Gecko, and because they are ``[meta]`` trackers; passing them
    puts the reason in the bug. The page preview passes none of them — it does not know, because
    deciding needs a Bugzilla search and a changeset's landing date, neither of which belongs in
    a render.

    The metadata below ``component`` is what a hand-filed crash bug carries and what
    ``create_bug`` needs to be accepted at all: ``version``/``type`` are MANDATORY on BMO, and
    ``keywords``/``cf_crash_signature`` mirror what the hand-draft path sets in ``improve`` -- so
    the preview shows the whole bug rather than the parts a filer would then have to supply by
    hand.

    The regressor is linked by ``regressed_by`` ALONE. ``blocked`` carries the ``clouseau``
    tracking bug and nothing else: the regressor's bug used to be added there as well, which
    asserted the same thing twice in two shapes, one of them backwards (a blocks entry says that
    bug cannot be closed until this crash is fixed).

    ``regressed_by`` asserts causation as structured, tooling-visible data, so it is set only
    under the same gate as the ``regression`` keyword (``link_regressor`` below). It was withheld
    entirely while the only precision figure was the ~28% of leads the corrected second-opinion
    instrument put on the true regressor -- measured across ALL leads, before the stale-signature
    and backout gates. The filings answer it directly now: of the 39 bugs filed at rung 70 up to
    2026-08-17, 12 have a ``regressed_by`` a human set, and 11 of those name the bug we named. The
    one that does not (bug 2062119, whose candidate landed in 2022) is exactly what this gate
    excludes."""
    dossier = dossier or {}
    candidate = dossier.get("candidate")
    if not candidate or not candidate.get("node"):
        return None
    channel = uuid_info.get("channel")
    uuid = uuid_info.get("uuid", "")
    product, component = resolve_product_component(
        candidate, channel, uuid_info.get("product"))
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
    suspected_regression = bool(is_suspected_regression(dossier.get("corroborations")))
    # May the bug make a STRUCTURED claim about the regressor at all: a candidate from outside
    # this build's pushlog window is named in the prose and nowhere else.
    link_regressor = bool(candidate.get("bug") and suspected_regression)
    # Does the crash report itself prove a memory-safety fault? Read from the PERSISTED flag the
    # deterministic gate wrote (`sensitive.py`), never recomputed and never from the model's
    # `failure_class` -- that label fires on 43 of 500 runs and on 2 of 11 new-bug filings, and
    # every human who read those bugs left them public, including one whose real fault address is
    # 0xffffffffffffffff (a hardware bit flip). The deterministic address fires on 1 of 57
    # filings, which is exactly the one a human restricted.
    withhold = sensitive.is_withheld(dossier.get("corroborations"))
    g = security_group(product) if withhold else None
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
            landing_unresolved=landing_unresolved,
            other_app_bugs=other_app_bugs,
            meta_bugs=meta_bugs,
        ),
        "product": product,
        "component": component,
        # --- metadata a create_bug needs / a hand-filed crash bug carries ---
        "version": _bug_version(channel),
        "type": "defect",
        # `regression` ONLY when the candidate actually came from this build's pushlog window.
        # The old justification here was "the whole pipeline only looks inside a build's pushlog
        # window, so every candidate it names is a suspected regression" — measured over the
        # first 22 filings, that held 3 times. The keyword drives release management's triage
        # and uplift decisions, so claiming it for a changeset from 2022 (bug 2062119) is not a
        # wording problem.
        "keywords": (["crash", "regression"] if suspected_regression else ["crash"]),
        # Bugzilla's crash-signature field, same `[@ ...]` syntax as the title. This is what
        # makes the bug show up against the signature in Socorro and in BMO's crash queries.
        "cf_crash_signature": "[@ {}]".format((uuid_info.get("signature") or "").strip()),
        # The `clouseau` tracking bug, and ONLY that. The regressor's own bug used to be added
        # here too, which said something we never meant: that the regressor bug cannot be closed
        # until this crash is fixed. It also put a crash bug in a stranger's blocks list — on bug
        # 2062119, a 2022 bug of Jens Stutte's that the run's own skeptic had ruled out.
        # `regressed_by` below is the relation that actually describes a regression.
        "blocked": ["clouseau"],
        # The regression relation BMO's own tooling reads, and the one a triager expects to find
        # on a regression bug. Gated on `link_regressor`, and a list because the field is one --
        # the pipeline only ever names a single changeset.
        "regressed_by": [candidate["bug"]] if link_regressor else [],
        "needinfo": _needinfo_line(person),
        # The VERIFIED Bugzilla login, not the hg commit address -- BMO rejects a whole
        # create for an unknown requestee, so an unresolved account means no flag (and the
        # prose above still names the person).
        "needinfo_email": (person or {}).get("account") or "",
        # SECURITY VENUE. :mccr8 on bug 2065051: "Bugs on poison crashes like that should always
        # be filed initially a security issue." `[]` on an ordinary crash, so nothing changes for
        # the 98% -- and a group NAME rather than a boolean because BMO has no
        # `is_security_issue` create parameter (it answers 400, code 53; Treeherder's own SERVER
        # translates its checkbox into `groups` from a per-product table). `security_group` reads
        # BMO's answer per product, because production's differ: Core is `core-security` while
        # Firefox/Toolkit/DevTools are `firefox-core-security`.
        #
        # An empty list when the crash IS memory-unsafe means the group could not be resolved,
        # and `autofile_bug` treats that as "do not file" -- see the check there. The distinction
        # cannot be drawn here, because a preview has no business refusing.
        "groups": [g] if (withhold and g) else [],
        # A requestee who cannot see a restricted bug makes Bugzilla reject the whole create
        # (Flag.pm's requestee-visibility rule), and our retry strips `flags` -- so the bug would
        # then be filed restricted with NO needinfo, i.e. the ask silently disappears. `cc` IS
        # honoured on create (unlike `blocks`), and `cclist_accessible` defaults true, so cc'ing
        # the requestee is what keeps the ask reachable. Only when we are actually restricting.
        "cc": ([(person or {}).get("account")]
               if withhold and (person or {}).get("account") else []),
    }
