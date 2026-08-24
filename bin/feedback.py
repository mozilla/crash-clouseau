# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Pull the outcome of every bug the pipeline filed, and score the archetypes against them.

    uv run python bin/feedback.py            # refresh from Bugzilla, then print the scoreboard
    uv run python bin/feedback.py --show     # print what is already stored, no network

Read-only against Bugzilla (public bugs, unauthenticated). Safe to run on a schedule.
"""
import argparse

from crashclouseau import feedback, models


def _print_notes(board):
    """The reviewer corpus: who reacted to a filing of ours, and what we have labelled.

    A REPLY IS NOT A CORRECTION, which is why this block is separate from the scoreboard
    above and why nothing here touches `attribution`. The predicate "somebody who is not us
    commented" fires on 43 of the 52 filings, and two of those are endorsements (bug 2060920,
    "Seems like an easy enough fix"; bug 2063892, "I have a fix almost ready"). Only the
    hand-set `error_class` claims we were wrong — everything else is a reading list."""
    try:
        replied = models.ReviewNote.replied()
        rows = models.ReviewNote.corpus()
    except Exception as exc:
        print("\nreviewer notes: unavailable ({})".format(exc))
        return
    notes = board.get("notes") or {}
    if notes:
        print("\nlast sweep: {} eligible, {} unchanged, {} fetched, {} new of {} read "
              "({} ours, {} automation), {} skipped (not our bug), {} failed".format(
                  notes.get("eligible", 0), notes.get("unchanged", 0), notes.get("scanned", 0),
                  notes.get("new", 0), notes.get("seen", 0), notes.get("ours", 0),
                  notes.get("automation", 0), notes.get("skipped_mode", 0),
                  notes.get("failed", 0)))
    labelled = sum(1 for r in rows if r.error_class)
    print("\nreviewer notes: {} on {} filings ({} labelled, {} to read)".format(
        len(rows), len(replied), labelled, len(rows) - labelled))
    by_class = {}
    for r in rows:
        if r.error_class:
            by_class[r.error_class] = by_class.get(r.error_class, 0) + 1
    if by_class:
        print("\nhand-set error classes:")
        for name, n in sorted(by_class.items(), key=lambda kv: (-kv[1], kv[0])):
            print("  {:<18} {}".format(name, n))
    unread = [r for r in rows if not r.error_class]
    if unread:
        print("\nunlabelled, needinfo'd first:")
        for r in unread[:20]:
            first = " ".join((r.body or "").split())[:88]
            print("  bug {:<8} c{:<3} {:<10} {:<28} {}{}".format(
                r.bug_id, r.comment_no, r.author_kind, (r.author or "")[:28],
                "[ni] " if r.needinfo else "", first))
        if len(unread) > 20:
            print("  ... and {} more".format(len(unread) - 20))


def _print(board):
    total = board.get("total", 0)
    print("\nfiled bugs tracked: {}".format(total))
    by = board.get("by_attribution") or {}
    # `unknown` is the honest majority early on: most filings are simply not triaged yet, and a
    # scoreboard that hid it would read as a precision figure it is not. `unconfirmed` is the
    # same caution about a `regressed_by` the filer set itself — nobody has agreed with it yet.
    for key in ("correct", "wrong", "unconfirmed", "crash_invalid", "unknown"):
        if key in by:
            print("  {:<14} {}".format(key, by[key]))
    adjudicated = by.get("correct", 0) + by.get("wrong", 0)
    if adjudicated:
        print("  attribution correct: {}/{} adjudicated".format(
            by.get("correct", 0), adjudicated))
    arch = board.get("by_archetype") or {}
    if arch:
        print("\narchetypes that fired on a filed bug:")
        for slug, t in sorted(arch.items()):
            print("  {:<24} filed {:<4} correct {:<4} wrong {:<4} unconfirmed {:<4} "
                  "invalid {}".format(slug, t["filed"], t["correct"], t["wrong"],
                                      t["unconfirmed"], t["crash_invalid"]))
    else:
        print("\nno archetype has fired on a filed bug yet")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true",
                        help="print the stored scoreboard without querying Bugzilla")
    parser.add_argument("--label", metavar="COMMENT_ID=ERROR_CLASS",
                        help="hand-set the verdict on one reviewer note; one of: {}".format(
                            ", ".join(models.ERROR_CLASSES)))
    parser.add_argument("--note", help="free-text rationale to store with --label")
    args = parser.parse_args()

    models.create()
    if args.label:
        try:
            comment_id, _, error_class = args.label.partition("=")
            row = models.ReviewNote.label(int(comment_id), error_class or None, args.note)
        except ValueError as exc:
            print(exc)
            return
        print("labelled comment {} -> {}".format(comment_id, error_class)
              if row else "no such comment id: {}".format(comment_id))
        return
    if args.show:
        board = models.Feedback.scoreboard()
        _print(board)
        _print_notes(board)
        return
    summary = feedback.refresh()
    print("refreshed {} of {} filed bugs ({} readable on BMO)".format(
        summary["updated"], summary["filed"], summary["fetched"]))
    # Printed, not buried in the summary dict: a bug we filed PUBLIC that we can no longer read
    # means a human restricted it, which is the ground truth for a missed security filing. Bug
    # 2065051 was exactly that, and the only way we found out was a reviewer mentioning it.
    if summary.get("unreadable"):
        print("\n  NO LONGER READABLE BY THIS ACCOUNT: {}".format(
            ", ".join(str(b) for b in summary["unreadable"])))
        print("  Each is either a bug a human RESTRICTED after we filed it public (check whether "
              "the\n  security gate should have caught it -- see crashclouseau/sensitive.py) or a "
              "BMO read failure.")
    _print(summary)
    _print_notes(summary)


if __name__ == "__main__":
    main()
