#!/usr/bin/env python
# Retire one or more channel labels: delete every row they own, in one transaction.
#
#   heroku run -a crash-clouseau-augmented -- python bin/retire_channel.py esr115 esr140
#   heroku run -a crash-clouseau-augmented -- python bin/retire_channel.py esr115 esr140 --yes
#
# Without --yes it only reports what it would delete. Refuses a label INGEST_CHANNELS or
# AGENT_CHANNELS still names (drop it from both first). See crashclouseau/retire.py and
# DEPLOY.md, "Retiring a line".
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from crashclouseau import retire  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("labels", nargs="+", help="channel labels to retire, e.g. esr115 esr140")
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args(argv)
    try:
        before, after = retire.retire(args.labels, execute=args.yes)
    except ValueError as exc:
        print("retire:", exc, file=sys.stderr)
        return 2
    verb = "deleted" if args.yes else "would delete"
    for table, n in before.items():
        left = "" if after is None else "  (left: {})".format(after[table])
        print("{:<10} {:>6} rows {}{}".format(table, n, verb, left))
    if after is None:
        print("dry run; add --yes to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
