# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import six
from . import update
from .models import CrashStack, UUID


def reset(uuids):
    if not uuids:
        return
    if isinstance(uuids, six.string_types):
        uuids = [uuids]
    ids = UUID.reset(uuids)
    CrashStack.delete(ids)
    update.analyze_reports()
