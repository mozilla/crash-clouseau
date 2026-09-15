# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Which module answers ``(product, channel, buildid) -> revision`` for a product.

Buildhub has no Fenix at all -- ``source.product`` over its 1.79M documents is firefox,
thunderbird, devedition, fennec and flowstate; a full-text ``fenix`` query returns 0 -- so the
contract the whole pipeline hangs on (``update.update_builds`` -> ``models.Build.put_data``,
``tools.get_changeset``, the pushlog helpers) is served by ``tcindex`` for Fenix and by
``buildhub`` for everything else. ``for_product("Firefox") is buildhub``: the desktop path is
the identical function object, not a wrapper, which is what keeps it byte-for-byte unchanged.

The tempting one-line fix -- ``buildhub.PRODS["Fenix"] = "firefox"`` -- is WRONG for the build
SET even though it is right for a single revision: Fenix does not build on every push desktop
does (46 fenix-nightly builds against 47 firefox-nightly buildids over 23 days), and borrowing
Firefox's ordering for the previous build collapsed one changeset window from 173 changesets
to 2 (plans/16 §2.5). The set comes from the TaskCluster index; the revision may come from
Buildhub queried AS firefox at the Fenix buildid, because the buildid IS the push timestamp.
"""

from . import buildhub

# Socorro product names whose builds come from the TaskCluster index.
TC_PRODUCTS = frozenset({"Fenix"})


def for_product(product):
    """The build-source module for *product*: ``tcindex`` for the products it serves,
    ``buildhub`` otherwise. Both expose ``get``, ``get_rev_from``, ``get_two_last`` and
    ``get_enclosing_builds`` with the same signatures and return shapes."""
    if product in TC_PRODUCTS:
        from . import tcindex
        return tcindex
    return buildhub
