# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime
from dateutil.relativedelta import relativedelta
import json
from libmozdata import utils as lmdutils
import pytz
import six
from sqlalchemy import inspect as sa_inspect
from . import config, java, models, update
from .logger import logger


def create(date=None, extra={}, hgauthors={}, force=False):
    """Clear the current database (if one), create a new one and add everything we need.

    REFUSES BY DEFAULT IF THE DATABASE ALREADY HAS TABLES, and that guard is the point of this
    paragraph. The first statement of this function used to be an unconditional
    ``models.clear()`` -> ``db.drop_all()``: every table, no confirmation, no dry run. Nothing
    calls this module today -- not the Procfile, not ``bin/``, not docker-compose, not the docs,
    not the tests -- so it has never fired, but "unreachable" is a property of the CALLERS, and
    one ``from crashclouseau import create`` in a console attached to prod is the whole distance
    between here and an empty database.

    ``force=True`` is the escape hatch for the intended use, a genuinely fresh database. It is a
    keyword with no default caller precisely so that dropping production requires typing the
    word.

    (Deleting this module outright is the other option and is tracked as plan #20 item 8. It is
    not a one-liner: ``update.py``'s unclamped ``start_date`` branch cites this file as its only
    caller, and the cascade reaches ``java.populate_java_files`` -- i.e. it is really a decision
    about whether Java support is kept.)
    """
    existing = sa_inspect(models.db.engine).get_table_names()
    if existing and not force:
        logger.error(
            "create() refuses to drop %d existing tables (%s...); pass force=True if you "
            "really mean to destroy this database",
            len(existing),
            ", ".join(sorted(existing)[:3]),
        )
        return
    models.clear()
    if not models.create():
        return
    if not date:
        date = pytz.utc.localize(datetime.utcnow())
    else:
        date = lmdutils.get_date_ymd(date)

    logger.info("Populate with java files: started.")
    try:
        java.populate_java_files()
    except Exception as e:
        logger.error(e, exc_info=True)
        return
    logger.info("Populate with java files: finished.")

    models.HGAuthor.put(hgauthors)

    start_date = date - relativedelta(days=config.get_ndays_of_data())
    logger.info("Create data for {}: started.".format(date))
    for chan in config.get_channels():
        update.put_filelog(chan, start_date=start_date, end_date=date)
        for prod in config.get_products():
            update.update_builds(start_date + relativedelta(days=1), chan, prod)

    if isinstance(extra, six.string_types):
        extra = json.loads(extra)

    for build in extra.get("builds", []):
        update.put_build(*build)

    logger.info("Create data for {}: finished.".format(date))

    update.update_all()
