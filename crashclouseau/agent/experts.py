# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Area-experts selection (#15 phase 2).

Given the ranked candidate changesets (from ``orchestrator.build_seed``) and their
authors (from local ``models.Node.authors_for`` — migration-proof, no network), pick a
small set of developers who recently worked in the crashing area: someone to ASK, not
necessarily the cause. Pure/deterministic and DB-free (the caller does the query), so
it unit-tests trivially. Skips noise-flagged candidates, backed-out changesets, bot
authors (``_is_bot``, judged on the ADDRESS -- see its docstring for the year-of-m-c panel
that killed the substring form) and the empty author; de-dupes and caps.
"""
from __future__ import annotations

import re

# An automated committer we must never needinfo, read off the ADDRESS: the domain, and the
# SHAPE of the local part. `.moz.tools`/`.gserviceaccount.com`/`.tld`/`.test` are service
# domains as a CLASS, not a roster of accounts; `.tld` is BMO's (update-bot@bmo.tld,
# release-mgmt-account-bot@mozilla.tld are real logins) and is reachable only from the
# filing path, which is why it scores 0 on the hg panel below and is still here.
# ``mozilla.bugs`` is BMO's pseudo-domain for accounts that are not mailboxes, the same
# category as ``.tld`` — and it only reaches this function from the BUG side, which is why a
# panel of mozilla-central AUTHOR strings could never have found it. Measured over the people
# on 1,200 recent prod bugs: exactly three accounts use it, in 382 appearances, and all three
# are automation (``intermittent-bug-filer`` "Treeherder Bug Filer", ``telemetry-probes``,
# ``wptsync``). The first two were being offered as needinfo requestees.
_BOT_DOMAIN_SUFFIXES = (".moz.tools", ".gserviceaccount.com", ".tld", ".test")
# Exact, not a suffix: there is one such domain and a suffix would also eat ``notmozilla.bugs``.
_BOT_DOMAIN_EXACT = frozenset({"mozilla.bugs"})
_BOT_LOCAL_SUFFIXES = ("bot", "bots", "bld", "bumper", "autoroller", "sync", "-ci")
_BOT_LOCAL_EXACT = frozenset({"bot", "noreply", "no-reply", "nobody", "cron", "autoland"})
# GitHub writes a user's privacy address as ``<numeric-id>+<login>@users.noreply.github.com``
# and an app's as ``<login>[bot]@...``; strip the id and fold ``[bot]`` so the local part is
# the identity and nothing else.
_GH_LOCAL_ID = re.compile(r"^\d+\+")


def _is_bot(email: str, name: str = "", nick: str = "") -> bool:
    """True when ``email`` is a SERVICE rather than a mailbox. ``name``/``nick`` are kept
    because both call sites pass them positionally, and are deliberately unused: a
    display-name-token clause was swept and added 0 true positives over 1,642 authors.

    Measured 2026-08-21 over a YEAR of mozilla-central (2025-08-21..2026-08-22, 2,056 pushes,
    68,518 changesets, 1,646 distinct hg author strings parsed with the shipped
    ``hgauthors.analyze_author``). The panel is 1,642 of those: four triples are dropped as
    neither. ``Claude <noreply@anthropic.com>`` (1 changeset) is a tool a human ran, and it is
    the ONE row whose label decides the 0 FP below -- this rule calls it a bot, so counting it
    as a human would make that 1. The other three are ``mail@max-inden.de`` spellings, one of
    them "mxinden-bot", of a human both rules already call human. 603 of the 1,642 touch a
    ``config.get_extensions`` source file, in 25,876 changesets -- the only authors that can
    ever be scored onto a crash frame. 14 automation identities exist in that year, hand-
    labelled from a broad bot|bld|sync|autorol|roller|l10n|cron|release+|nobody|bump|pontoon|
    treescript|sheriff|taskcluster|ci|automation|servo|noreply|updater|tagging|merge|nightly|
    daemon|robot|[bot] scan of every author string:

        rule                                             TP    FP   FN
        substrings over email+name+nick (what was here)   8   138    6
        the same, minus ``noreply``                       5     0    9
        anchoring ``bot@`` to ``^bot@``/``@bot.``         7   138    7
        this                                             14     0    0

    ANCHORING WAS THE OBVIOUS FIX AND IT IS A TRAP: it keeps all 138 false positives, which
    never came from ``bot@`` at all, and it additionally loses ``Updatebot
    <updatebot@mozilla.com>``, which lands 121 source changesets a year in vendored media and
    crypto code (security/nss, gfx/harfbuzz, third_party/aom, media/libvpx, media/libopus; 18
    of the 121 in media/libopus or media/libdav1d) -- i.e. a real crash class. Every one of
    the 138 came from ONE marker, ``noreply``, matching GitHub's per-user privacy address,
    which is a HUMAN's: 138 of 1,642 m-c authors (8.4%) and 32 of 603 source authors (5.3%)
    were being deleted -- Keith Cirkel (252 changesets, 202 of them source, 27 touching
    dom/base/Document.cpp), mcarare (342), Mugurell (274), AndiAJ (220), Reem H (154), Alice
    Boxhall, Jan-Ivar Bruaroey.

    On the population where this function can change anything -- a 713-crash nightly replay
    over the 26 build ids of our own filings, of which 115 have an in-window NON-merge author
    on a crashing-thread frame file -- the old rule fired on 4 of 115 and ALL FOUR were Keith
    Cirkel, while the one bot that reaches a crash-stack file, ``Lando <lando@lando.moz.tools>``,
    reaches 7 of the same 115 and was not caught. Lando's 552 changesets split into 230 merges,
    which ``models.py`` already drops, and 322 non-merge "Bug NNNNN: apply code formatting via
    Lando" -- bug-numbered reformatting of nsDocShell.cpp, gfxFont.cpp and MediaEncoder.cpp,
    which nothing drops. The rule's sign was inverted on its own target population. (Prod
    agrees, from a figure this panel cannot recompute: the untracked note find_authors.md S1,
    2026-08-12, counted 49 "Lando" suggestions -- from 5 distinct changesets -- in the dossiers
    table, 46 for lando@lando.moz.tools and 3 for lando@lando.test.)

    MUST STAY EXCLUDED, all verified True: ffxbld@mozilla.com, ffxbld@lando.moz.tools,
    updatebot@mozilla.com, release+landoscript@mozilla.com, l10n-bumper@mozilla.com,
    lando@lando.moz.tools, lando@lando.test, blink-w3c-test-autoroller@chromium.org,
    luci-bisection@appspot.gserviceaccount.com, crash@system.gserviceaccount.com,
    wpt-pr-bot / dependabot[bot] / github-actions[bot] / servo-wpt-sync (all
    @users.noreply.github.com), update-bot@bmo.tld, release-mgmt-account-bot@mozilla.tld, and
    ``moz-wptsync-bot <wptsync@mozilla.com>`` -- which the OLD rule let straight through even
    though ``report_bug._match_author`` names it as the hazard it refuses a fourth key over.
    MUST NOT BE EATEN, all verified False: keithamus@ and jan-ivar@users.noreply.github.com,
    48995920+mcarare@, 95208+alice@, botond@mozilla.com "Botond Ballo" (64 source changesets
    a year, and the needinfo requestee on our own bug 2064600), abotella@igalia.com "Andreu
    Botella", anirudh@Anirudhs-MacBook-Air.local (a broken git config, a human), and
    sync@example.org, which only the ``local != s`` clause below saves.

    Per clause over the year corpus (bots caught / humans eaten): suffix ``bot`` 4/0,
    .gserviceaccount.com 2/0, .moz.tools 2/0, suffix ``bld`` 2/0, suffix ``sync`` 2/0, .test
    1/0, ``release+`` 1/0, suffix ``autoroller`` 1/0. The other ten (.tld and the exact locals
    bot/noreply/no-reply/nobody/cron/autoland, suffixes ``bots``/``bumper``/``-ci``) score 0/0
    here and are kept as SHAPES rather than as the account roster they replace -- an exact
    ``bot@`` is the one case the old ``bot@`` marker caught that a suffix would not.
    seabld/tbirdbld ride suffix ``bld``: comm-central is not on this panel and
    ``config.products`` is not a kill switch.

    RESIDUAL, disclosed and not fixed: a human whose local part ENDS in "bot" (``talbot@``,
    ``abbot@``) is still eaten, exactly as before. 0 of 1,642 m-c authors in a year are such
    a person, and requiring a separator before "bot" would cost ``updatebot`` and
    ``dependabot``, which are real. Cost if it happens: one absent name in "Suggested contacts"
    and one unset needinfo flag -- ``_create_bug_keeping_the_bug`` already files the bug
    without one."""
    local, _, domain = (email or "").lower().partition("@")
    local = _GH_LOCAL_ID.sub("", local).replace("[bot]", "bot")
    if domain.endswith(_BOT_DOMAIN_SUFFIXES) or domain in _BOT_DOMAIN_EXACT:
        return True
    if local in _BOT_LOCAL_EXACT or local.startswith("release+"):
        return True
    # A SUFFIX, never the whole local part: ``wptsync@`` is a service, ``sync@`` is a person.
    return any(local.endswith(s) and local != s for s in _BOT_LOCAL_SUFFIXES)


def area_experts(candidates, authors_by_node, *, max_experts=3):
    """Return up to ``max_experts`` area-expert dicts (``AreaExpert`` shape) built from
    the authors of the top NON-noise, non-backed-out candidate changesets.

    ``candidates`` is build_seed's ranked list ([{node, score, bug, backedout, noise}]);
    ``authors_by_node`` maps a node hash -> {email, real, nick, ...}. De-duped by author
    identity (email, else name, else nick), so the same dev isn't listed twice."""
    experts: list[dict] = []
    seen: set[str] = set()
    for c in candidates:
        if c.get("noise") or c.get("backedout"):
            continue
        info = authors_by_node.get(c.get("node"))
        if not info:
            continue
        email = info.get("email", "")
        name = info.get("real", "")
        nick = info.get("nick", "")
        ident = (email or name or nick).lower()
        if not ident or ident in seen or _is_bot(email, name, nick):
            continue
        seen.add(ident)
        bug = c.get("bug")
        experts.append({
            "name": name,
            "email": email,
            "nick": nick,
            "node": c.get("node", ""),
            "bug": bug,
            "reason": "authored candidate {}{}".format(
                c.get("node", ""), " (bug {})".format(bug) if bug else ""
            ),
        })
        if len(experts) >= max_experts:
            break
    return experts
