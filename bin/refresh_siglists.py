# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Refresh the vendored copies of Socorro's signature skip lists.

    uv run python bin/refresh_siglists.py            # fetch, diff, write
    uv run python bin/refresh_siglists.py --check    # fetch, diff, exit 1 when stale, write nothing

``crashclouseau/sigfamily.py`` decides which frames of a signature identify the crash site by
asking whether siggen would have skipped them, using the two lists Socorro itself generates
signatures with (``socorro/signature/siglists/``). They change: nine commits in the year to
2026-09-18, one of which -- "Better triage for chromium sandbox CHECK failures", 2025-11-06 --
listed the ``LogMessage`` siblings but not the ``CheckLogMessage`` class Chromium added later,
which is how every sandbox CHECK failure moved onto one new name (bug 2073210). A stale copy
here means a frame Socorro now skips is treated as specific, or the reverse; neither breaks
anything, both make the family a little blinder. Re-run this when a filing turns out to be a
rename the lookup missed, and commit the result.

Writes nothing but the two files under ``config/siglists``; no database, no Bugzilla.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crashclouseau import net, sigfamily  # noqa: E402

UPSTREAM = ("https://raw.githubusercontent.com/mozilla-services/socorro/main/socorro/signature/"
            "siglists/")
FILES = {
    "irrelevant_signature_re.txt": sigfamily.IRRELEVANT_SIGLIST,
    "prefix_signature_re.txt": sigfamily.PREFIX_SIGLIST,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="report whether the vendored lists are stale; write nothing")
    args = parser.parse_args(argv)
    stale = 0
    for name, path in FILES.items():
        r = net.get(UPSTREAM + name, timeout=(10, 60))
        r.raise_for_status()
        new = r.text
        try:
            with open(path, encoding="utf-8") as fh:
                old = fh.read()
        except OSError:
            old = ""
        if new == old:
            print("{}: up to date ({} lines)".format(name, new.count("\n")))
            continue
        stale += 1
        added = sorted(set(new.splitlines()) - set(old.splitlines()))
        removed = sorted(set(old.splitlines()) - set(new.splitlines()))
        print("{}: {} line(s) added, {} removed".format(name, len(added), len(removed)))
        for line in added:
            print("  + " + line)
        for line in removed:
            print("  - " + line)
        if not args.check:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            print("  written to {}".format(os.path.relpath(path)))
    return 1 if (stale and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
