import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import mailmap  # noqa: E402

# Disable mailmap fetches unless a test overrides the cache.
mailmap._cache.update(entries={}, until=float("inf"))

from unittest import mock  # noqa: E402

from crashclouseau import bugcomponents, bugzilla_apply  # noqa: E402
from crashclouseau.agent import venue_judge  # noqa: E402

# Disable hidden-duplicate lookups unless a test overrides this mock.
REAL_HIDDEN_DUPLICATE_BUG = bugzilla_apply._hidden_duplicate_bug
mock.patch.object(bugzilla_apply, "_hidden_duplicate_bug", return_value=None).start()

# Prevent GitHub file-history calls unless a test overrides this mock.
REAL_PATH_HISTORIES = bugcomponents._path_histories
mock.patch.object(bugcomponents, "_path_histories", return_value=None).start()

# Prevent model calls unless a test overrides this mock.
REAL_VENUE_CHECK = venue_judge.check
mock.patch.object(venue_judge, "check", return_value=None).start()
