# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""``create.create()`` must not silently drop a populated database.

    DATABASE_URL=sqlite:// uv run python -m unittest tests.test_create_guard

Its first statement was an unconditional ``models.clear()`` -> ``db.drop_all()``. The module has
no caller anywhere, so it has never fired; that is a property of the callers, not of the code.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import create                                          # noqa: E402


class CreateGuardTest(unittest.TestCase):
    def _patch(self, tables):
        insp = mock.Mock()
        insp.get_table_names.return_value = tables
        return mock.patch.object(create, "sa_inspect", return_value=insp)

    def test_refuses_when_tables_exist(self):
        with self._patch(["builds", "uuids", "dossiers"]), \
                mock.patch.object(create.models, "clear") as clear, \
                mock.patch.object(create.models, "create") as mk:
            create.create()
        clear.assert_not_called()
        mk.assert_not_called()

    def test_force_overrides_the_guard(self):
        with self._patch(["builds"]), \
                mock.patch.object(create.models, "clear") as clear, \
                mock.patch.object(create.models, "create", return_value=False):
            create.create(force=True)
        clear.assert_called_once()

    def test_a_fresh_database_is_not_blocked(self):
        with self._patch([]), \
                mock.patch.object(create.models, "clear") as clear, \
                mock.patch.object(create.models, "create", return_value=False):
            create.create()
        clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
