import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import mailmap  # noqa: E402

# Disable mailmap fetches unless a test overrides the cache.
mailmap._cache.update(entries={}, until=float("inf"))
