# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from datetime import datetime
from dateutil.relativedelta import relativedelta
from libmozdata import utils as lmdutils
import pytz
from . import config, java, models, update
from .logger import logger


def create(date=None):
    models.clear()
    if not models.create():
        return
    if not date:
        date = pytz.utc.localize(datetime.utcnow())
    else:
        date = lmdutils.get_date_ymd(date)

    logger.info('Populate with java files: started.')
    try:
        java.populate_java_files()
    except Exception as e:
        logger.error(e, exc_info=True)
        return
    logger.info('Populate with java files: finished.')

    start_date = date - relativedelta(days=config.get_ndays_of_data())
    logger.info('Create data for {}: started.'.format(date))
    update.put_filelog(start_date=start_date, end_date=date)
    start_date += relativedelta(days=1)
    update.update_builds(start_date)
    logger.info('Create data for {}: finished.'.format(date))

    update.update_in_queue()
