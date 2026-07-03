# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import redis
from rq import Worker, Queue, suspension
from .logger import logger
from . import config


listen = ["high", "default", "low", "agent"]
redis_url = os.getenv("REDIS_URL", config.get_redis())
# The ssl_* kwargs are only valid for SSL connections (rediss://), e.g. the
# Heroku Redis add-on; passing them to a plain redis:// connection (local
# Docker) makes redis-py raise, so only set them when the URL is SSL.
if redis_url.startswith("rediss://"):
    conn = redis.from_url(redis_url, ssl_cert_reqs=None, ssl_check_hostname=False)
else:
    conn = redis.from_url(redis_url)
__QUEUE = None


def black_hole(job, *exc_info):
    args = job.args
    func = job.func_name
    logger.error(("Job for call {}{} failed").format(func, args))
    job.cancel()
    return False


def get_queue(name="low"):
    global __QUEUE
    if __QUEUE is None:
        __QUEUE = {n: Queue(n, connection=conn, default_timeout=6000) for n in listen}
    return __QUEUE[name]


def suspend():
    suspension.suspend(conn)


def resume():
    suspension.resume(conn)


if __name__ == "__main__":
    worker = Worker(
        [Queue(name, connection=conn) for name in listen],
        exception_handlers=[black_hole],
        connection=conn,
    )
    worker.work()
