# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The bug a REAL spike files: what it says and in which order.

Built next to ``report_bug`` (whose blocks it reuses: crash link, reason, frames, links, the
needinfo line, the provenance footer) rather than inside it, because the two bugs argue
differently. A culprit filing exists because a changeset was named and everything in it defends
that claim. A spike filing exists because of the VOLUME: the numbers come first and stand on
their own, the investigator's analysis follows as an offer, and when that analysis grounded
nothing it is left out rather than dressed up -- the bug is still correct without it.

Every number in the spike paragraph is the one ``spikes.judge_selection`` decided on, rendered by
the same ``spikes.describe`` the investigator's brief used, so a human and the model were shown
the same spike.
"""
from crashclouseau import config, report_bug, sigage, sigtrend, spikes, utils

_MAX_EVIDENCE = 8
_MAX_LIST = 6
_MAX_OTHER_REPORTS = 3


def spike_paragraph(brief):
    """**Filed because the volume spiked.** + the numbers, the rate, the version step and the
    signature's age -- the deliverable for this class of bug, in one paragraph."""
    spike = brief.get("spike") or {}
    sentence = brief.get("spike_sentence") or spikes.describe(
        spike, brief.get("channel"), brief.get("build_day"), brief.get("buildid"))
    if not sentence:
        return None
    parts = ["**Filed because this signature's crash volume spiked.** " + sentence]
    if spike.get("kind") == "build_day":
        window = "the loudest of the preceding build-days"
        if spike.get("history_days"):
            window = ("the loudest of the preceding build-days and of this signature's builds "
                      "over the {} days before".format(spike["history_days"]))
        parts.append(
            "That is {}{} (Clouseau's bar is {}x, and the Poisson excess of the day against "
            "that baseline is z = {}, bar {})."
            .format("{}x ".format(spike["ratio"]) if spike.get("ratio") else
                    "an appearance from zero over ", window,
                    config.get_spike("ratio", brief.get("product") or "Firefox",
                                     brief.get("channel") or "nightly"),
                    spike.get("z"), spike.get("z_min")))
    trend = brief.get("trend_sentence")
    if trend and spike.get("kind") != "rate":
        parts.append("Over the last week the rate reads the same way: " + trend)
    step = (brief.get("version_step") or {})
    if step.get("version"):
        # A SHARE of the version's own crash reports, stepped from the version's first days
        # (`sigage.version_rates`): a count per version would read a population shift as a rate.
        parts.append("By version, {} runs at {}x the share of {} from its first days.".format(
            step["version"], step.get("ratio"), step.get("from_version")))
    age = signature_age_sentence(brief)
    if age:
        parts.append(age)
    return " ".join(parts)


def is_new_signature(brief):
    """Is this spike the signature APPEARING -- ``0, 0, 0 -> N`` with no earlier report of it on
    this channel -- rather than an old signature getting loud?

    Decides the channel's ``summary_prefix``: release titles its filings ``[new in release]``,
    and that is TRUE of an appearance and false of a rise, so the mark follows the shape of the
    spike rather than the channel alone. The CHANNEL's first-seen clock is the right one, not
    the all-channel one: a crash that ran on nightly for a month and reaches release with a
    version is new IN RELEASE, which is what the mark says. New until a clock says otherwise --
    the baseline is the selector's own evidence, and a first-seen can only push the origin
    earlier; a lookup that failed cannot make an appearance look old. The signature's own build
    history (``spikes.build_history``, when the judgement carried it) is the first clock: any
    earlier build with a report of it, and this is a rise, not an appearance."""
    spike = brief.get("spike") or {}
    if spike.get("kind") != "build_day":
        return False
    # A NEW NAME IS NOT A NEW CRASH (`sigfamily`): an older name that stopped on this build
    # (a handoff predecessor), or a spelling that was already live before it, means the mark
    # would be false -- 2072770 was titled `[new in release]` over 756 reports of
    # `shutdownhang | CanEnterBaselineJIT`.
    if brief.get("signature_predecessors"):
        return False
    if any((s or {}).get("status") in ("older", "coexisting", "undecided")
           for s in brief.get("signature_siblings") or []):
        return False
    if any(int(b or 0) > 0 for b in (spike.get("baseline") or [])):
        return False
    if any(int((h or {}).get("count") or 0) > 0 for h in (spike.get("history") or [])):
        return False
    buildid = str(brief.get("buildid") or "")
    seen = brief.get("first_seen_channel")
    if buildid and seen and str(seen) < buildid:
        return False
    return True


def signature_age_sentence(brief):
    """When the signature first appeared: on this channel, and anywhere (the unbounded clock).
    On a RENAMED signature (`sigfamily`), what it was called before and how old THAT is."""
    channel = brief.get("channel") or "this channel"
    buildid = str(brief.get("buildid") or "")
    preds = brief.get("signature_predecessors") or []
    if preds:
        return _renamed_signature_sentence(brief, preds[0])
    ever = brief.get("first_seen_ever") or brief.get("first_seen")
    chan = brief.get("first_seen_channel")
    new_on_channel = bool(buildid and chan and str(chan) >= buildid)
    days = sigage.signature_age_days(ever, buildid) if ever else None
    if new_on_channel:
        if ever and days is not None and days >= 1:
            return ("This signature is new on {}: crash-stats has no report of it there before "
                    "this build. Its first report anywhere is in build {} ({}), {:.0f} days "
                    "earlier.".format(channel, ever, sigage.buildid_day(ever), days))
        return ("This signature is new on {}: crash-stats has no report of it there before this "
                "build.".format(channel))
    if not ever:
        if chan:
            return "Socorro first recorded this signature on {} in build {}.".format(channel, chan)
        return None
    if days is None:
        return "Socorro first recorded this signature in build {}.".format(ever)
    if str(ever) == buildid or days < 1:
        return ("This signature is new: crash-stats has no report of it on any build before "
                "this one.")
    return "This signature is {}: its first report is in build {} ({}), {:.0f} days before this build.".format(
        "new" if days <= sigage.NEW_SIGNATURE_DAYS else "not new", ever,
        sigage.buildid_day(ever), days)


def _renamed_signature_sentence(brief, top):
    """``This signature is a new name for an older crash: until build B it reported as `P`
    (...); `P` was first recorded in build F (day).``"""
    p = top.get("signature")
    fam = brief.get("signature_family") or {}
    build = fam.get("s_first_build")
    head = "This signature is a new name for an older crash: {}it reported as `{}`".format(
        "until build {} ({}) ".format(build, sigage.buildid_day(build)) if build else "", p)
    numbers = []
    if top.get("before") is not None and top.get("after") is not None:
        numbers.append("{} reports on the builds of the 28 days before, {} since".format(
            top["before"], top["after"]))
    if top.get("change"):
        numbers.append(top["change"])
    if numbers:
        head += " ({})".format("; ".join(numbers))
    ever = top.get("first_seen_ever") or brief.get("signature_family_first_seen_ever")
    if ever:
        head += "; `{}` was first recorded in build {} ({})".format(p, ever, sigage.buildid_day(ever))
    alignment = fam.get("alignment")
    if alignment == "date":
        head += (". The old name stopped on every live build at once, which is what a Socorro "
                 "skip-list change or a symbol gap does, not a code change")
    return head + "."


def _culprit_paragraph(findings, brief, author_display=None, link_regressor=False):
    c = findings.culprit
    if c is None or not c.node:
        return None
    channel = brief.get("channel")
    link = report_bug.changeset_links(c.node, channel)
    if c.bug:
        link += " (bug {})".format(c.bug)
    if author_display:
        link += " by {}".format(author_display)
    if link_regressor:
        head = "Suspected regressor ({} confidence): {}.".format(c.confidence, link)
    else:
        head = ("Starting point -- a candidate, NOT an established cause ({} confidence): "
                "{}.".format(c.confidence, link))
    return head + ("\n\n" + c.why if c.why else "")


def analysis_section(findings, brief, author_display=None, link_regressor=False,
                     grounded=True):
    """The investigator's account, or the one sentence that says why there is none."""
    if findings is None:
        return ("No automated analysis is attached: the investigation did not reach a "
                "conclusion. The volume above is the finding.")
    if not grounded:
        return ("No automated analysis is attached: the investigation consulted no source, "
                "history or crash-stats data, so its conclusions would be unverified. The volume "
                "above is the finding.")
    lines = []
    # No model name here (Calixte, 2026-09-08): the ordinary filer says "Clouseau analysis
    # (automated ...)" and the footer already says an LLM wrote it; the reader needs to know
    # a machine wrote this, not which one.
    head = ("Clouseau analysis (automated -- nothing below was checked by a human; a claim it "
            "could not ground is deliberately absent):")
    if findings.summary:
        lines.append(head + "\n\n" + findings.summary)
    else:
        lines.append(head)
    culprit = _culprit_paragraph(findings, brief, author_display, link_regressor)
    if culprit:
        lines.append(culprit)
    if findings.trigger_path:
        lines.append("Possible path to the crash: " + findings.trigger_path)
    evidence = [e for e in findings.evidence if e.claim][:_MAX_EVIDENCE]
    if evidence:
        lines.append("Checked:\n" + "\n".join(_evidence_line(e) for e in evidence))
    if findings.ruled_out:
        # "Alternatives", not "Ruled out": the prompt's evidence model reserves "ruled out" for
        # a direct contradiction and has each entry carry its own status word (disfavored, not
        # supported, unresolved), so the heading must not claim more than the entries do.
        lines.append("Alternatives considered:\n" + "\n".join(
            "- {}".format(x) for x in findings.ruled_out[:_MAX_LIST]))
    if findings.open_questions:
        lines.append("Worth checking first:\n" + "\n".join(
            "- {}".format(x) for x in findings.open_questions[:_MAX_LIST]))
    return "\n\n".join(lines)


def _evidence_line(e):
    """``- claim [inferred, medium confidence] (source)``. The kind is shown only when it is not
    a plain observation, so a list of observed facts reads as it always did."""
    kind = getattr(e, "kind", "") or ""
    confidence = getattr(e, "confidence", "") or ""
    tag = ""
    if kind == "inferred":
        tag = " [inferred{}]".format(", {} confidence".format(confidence) if confidence else "")
    elif kind == "derived":
        tag = " [derived]"
    return "- {}{}{}".format(e.claim, tag, " ({})".format(e.source) if e.source else "")


def other_reports_line(brief):
    others = [st.get("uuid") for st in (brief.get("stacks") or [])[1:] if st.get("uuid")]
    others = [u for u in others if u != brief.get("uuid")][:_MAX_OTHER_REPORTS]
    if not others:
        return None
    return "Reports with a different stack in the same spike: " + ", ".join(
        "https://crash-stats.mozilla.org/report/index/{}".format(u) for u in others)


def spike_bucket_title(brief, findings):
    """The title of a spike bug on a BUCKET-HOLDER signature (one an open ``[meta]`` holds), or
    ``""``: the deterministic hang title from the awaited thread (``hang.bucket_title``, the
    ``<work> blocks <pool> shutdown inside <call>`` shape :jstutte gave 2071528 and 2073426),
    else the investigator's summary cut to a sentence. Nothing usable means the spike is
    signature-level information and goes to the tracker as a comment instead (see
    ``spike_escalation.file_spike_bug``)."""
    work = _spike_awaited_work(brief)
    if work.get("title"):
        return str(work["title"])[:report_bug._MAX_BUCKET_TITLE]
    summary = getattr(findings, "summary", "") if findings is not None else ""
    sentence = report_bug._first_sentence(summary)
    return sentence[:report_bug._MAX_BUCKET_TITLE] if len(sentence) >= 20 else ""


def _spike_awaited_work(brief):
    """The awaited-work summary only when *brief* is actually a watchdog crash."""
    raw = (brief or {}).get("raw_crash") or {}
    if not is_hang((brief or {}).get("signature"), raw):
        return {}
    from crashclouseau import hang

    try:
        return hang.awaited_summary(raw) or {}
    except Exception:  # pragma: no cover - defensive
        return {}


def spike_bucket_key(brief):
    """The deterministic awaited-stack key used to deduplicate a hang bucket."""
    return str(_spike_awaited_work(brief).get("bucket") or "")


def venue_note(related_bugs=None, other_app_bugs=None, meta_bugs=None):
    """Why this is a new bug and not a comment, when open bugs exist on the signature. A
    ``[meta]`` on the signature is not a reason any more: a bug filed beside one is a BUCKET bug
    that opens with ``report_bug.build_bucket_opener`` (``meta_bugs`` is accepted and ignored
    so the callers need not change shape)."""
    notes = []
    if related_bugs:
        notes.append(
            "Open bug{} {} reference{} this signature; this was filed as a new bug because the "
            "channel's policy for spike reports is not to comment on existing bugs. If one of "
            "them is about this spike, please mark this one as its duplicate.".format(
                "s" if len(related_bugs) > 1 else "",
                ", ".join(str(b) for b in related_bugs),
                "" if len(related_bugs) > 1 else "s"))
    other = report_bug.build_other_app_bugs_note(other_app_bugs)
    if other:
        notes.append(other)
    return "\n\n".join(notes) if notes else None


def build_spike_comment(brief, findings, *, details=None, stack=None, person=None,
                        author_display=None, link_regressor=False, grounded=True,
                        related_bugs=None, other_app_bugs=None, meta_bugs=None,
                        as_comment=False, bucket_opener=None):
    """The whole opener (or the comment on an existing bug) as one markdown text. On a hang the
    awaited thread's stack follows the main thread's (``report_bug.build_awaited_work_block``,
    from the same minidump): the main thread's frames are the signature, the awaited thread is
    what the bug is about."""
    uuid = brief.get("uuid", "")
    channel = brief.get("channel")
    sections = [
        bucket_opener,
        "Crash report: https://crash-stats.mozilla.org/report/index/{}".format(uuid),
        other_reports_line(brief),
        report_bug.build_reason_block(details),
        report_bug.build_frames_block(stack, details=details) if stack else None,
        _awaited_block(brief),
        spike_paragraph(brief),
        analysis_section(findings, brief, author_display=author_display,
                         link_regressor=link_regressor, grounded=grounded),
        None if as_comment else venue_note(related_bugs, other_app_bugs, meta_bugs),
        report_bug._needinfo_line(person) if person else None,
        report_bug._provenance(channel),
    ]
    return report_bug._unbacktick_bug_refs("\n\n".join(s for s in sections if s))


def _awaited_block(brief):
    """The awaited thread of a hang, rendered from the brief's own processed crash."""
    raw = (brief or {}).get("raw_crash") or {}
    if not raw or not (brief or {}).get("is_hang"):
        return None
    from crashclouseau import hang

    try:
        work = hang.awaited_summary(raw)
    except Exception:  # pragma: no cover - defensive
        return None
    return report_bug.build_awaited_work_block({"hang_awaited_work": work}) if work else None


def build_spike_preview(brief, findings, *, product, component, person=None,
                        details=None, stack=None, link_regressor=False, grounded=True,
                        related_bugs=None, other_app_bugs=None, meta_bugs=None,
                        withhold=False, bucket_title=None):
    """The bug the spike filer posts: ``build_bug_preview``'s shape, for the spike.

    ``bucket_title`` makes it a BUCKET BUG on a signature an open ``[meta]`` holds
    (``spike_bucket_title``): titled for its cause, no ``cf_crash_signature``, blocking the
    tracker, opening with why (the same shape as the ordinary filer's, see
    ``report_bug.build_bug_preview``)."""
    channel = brief.get("channel")
    policy = config.get_agent_autofile(channel)
    signature = (brief.get("signature") or "").strip()
    meta_ids = [int(b["id"]) for b in (meta_bugs or []) if (b or {}).get("id")]
    bucket = bool(meta_ids and bucket_title)
    opener = report_bug.build_bucket_opener(meta_bugs, signature) if bucket else None
    bucket_key = spike_bucket_key(brief) if bucket else ""
    author_display = report_bug._person_display(person) if person else None
    culprit = findings.culprit if findings is not None else None
    regression = bool(culprit and culprit.node and grounded and culprit.confidence != "low")
    group = report_bug.security_group(product) if withhold else None
    account = (person or {}).get("account") or ""
    # The structured causal claim only at HIGH confidence, on a candidate that is in the window:
    # the ordinary filer earned this field at rung 70 with 11 of 12 human-confirmed.
    regressed_by = []
    if culprit and culprit.bug and link_regressor and culprit.confidence == "high":
        regressed_by = [culprit.bug]
    # The channel's mark (release: "[new in release]") when the spike IS an appearance -- an
    # all-zero baseline and no earlier report on the channel -- and no prefix when an old
    # signature got loud, where the mark would be false. See `is_new_signature`.
    prefix = policy.get("summary_prefix") or "" if is_new_signature(brief) else ""
    return {
        "title": bucket_title if bucket else report_bug.bug_title(signature, prefix=prefix),
        "comment": build_spike_comment(
            brief, findings, details=details, stack=stack, person=person,
            author_display=author_display, link_regressor=link_regressor, grounded=grounded,
            related_bugs=related_bugs, other_app_bugs=other_app_bugs, meta_bugs=meta_bugs,
            bucket_opener=opener),
        "product": product,
        "component": component,
        "version": report_bug._bug_version(channel),
        "type": "defect",
        # `regression` when the investigator grounded a candidate at medium or better; the bare
        # volume step is not asserted as one -- an OS update spikes a signature too.
        "keywords": ["crash", "regression"] if regression else ["crash"],
        "cf_crash_signature": "" if bucket else "[@ {}]".format(signature),
        "blocked": ["clouseau"] + (meta_ids if bucket else []),
        "bucket": {"meta_bugs": meta_ids, "key": bucket_key} if bucket else None,
        "regressed_by": regressed_by,
        "needinfo": report_bug._needinfo_line(person) if person else None,
        "needinfo_email": account,
        # Apply the channel's tracking policy to spike bugs too.
        "tracking_flag": (report_bug._tracking_flag(brief.get("version"), channel)
                          if policy.get("nominate_tracking") else None),
        # State the crash's own train; the filer adds other live trains reported by Socorro.
        "status_flags": ({own: "affected"} if (own := report_bug._status_flag(
            brief.get("version"), channel)) else {}),
        "groups": [group] if (withhold and group) else [],
        "cc": [account] if (withhold and account) else [],
    }


def trend_sentence(facts):
    """``sigtrend.describe`` when the rate is rising, else ``None``."""
    if not facts or not sigtrend.is_rising(facts):
        return None
    return sigtrend.describe(facts)


def is_hang(signature, raw_crash=None):
    raw = raw_crash or {}
    return utils.is_watchdog_crash(
        signature=signature, report_type=raw.get("report_type"),
        moz_crash_reason=raw.get("moz_crash_reason"))
