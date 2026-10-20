# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Read-only report of hot-engine suppressions and hold-out verdicts.

    uv run python bin/hot_engine_audit.py                 # everything recorded
    uv run python bin/hot_engine_audit.py --days 30       # the last 30 days
    uv run python bin/hot_engine_audit.py --no-bugzilla   # skip Bugzilla lookups
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashclouseau import hot_engine_audit  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--no-bugzilla", action="store_true")
    args = parser.parse_args()
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)) if args.days else None
    rep = hot_engine_audit.report(since=since, with_bugzilla=not args.no_bugzilla)
    print(hot_engine_audit.format_report(rep))


if __name__ == "__main__":
    main()
