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

from crashclouseau import report_bug  # noqa: E402

# Disable product lookups unless a test overrides this mock.
REAL_PRODUCT_COMPONENTS = report_bug._product_components
mock.patch.object(report_bug, "_product_components", return_value=None).start()

# Prevent model calls unless a test overrides this mock.
REAL_VENUE_CHECK = venue_judge.check
mock.patch.object(venue_judge, "check", return_value=None).start()

from crashclouseau.agent import spike_escalation  # noqa: E402

# Disable spike backout checks unless a test restores them.
REAL_CULPRIT_BACKOUT = spike_escalation._culprit_backout
mock.patch.object(spike_escalation, "_culprit_backout", return_value=None).start()

# Disable release version-step scans unless a test restores them.
REAL_VERSION_SCAN = spike_escalation._version_scan
mock.patch.object(spike_escalation, "_version_scan", return_value=None).start()
