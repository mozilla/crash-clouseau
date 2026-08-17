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
    args = parser.parse_args()

    models.create()
    if args.show:
        _print(models.Feedback.scoreboard())
        return
    summary = feedback.refresh()
    print("refreshed {} of {} filed bugs ({} readable on BMO)".format(
        summary["updated"], summary["filed"], summary["fetched"]))
    _print(summary)


if __name__ == "__main__":
    main()
