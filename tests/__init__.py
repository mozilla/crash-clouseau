import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import mailmap  # noqa: E402

# Disable mailmap fetches unless a test overrides the cache.
mailmap._cache.update(entries={}, until=float("inf"))

from unittest import mock  # noqa: E402

from crashclouseau.agent import venue_judge  # noqa: E402

# Prevent model calls unless a test overrides this mock.
REAL_VENUE_CHECK = venue_judge.check
mock.patch.object(venue_judge, "check", return_value=None).start()
