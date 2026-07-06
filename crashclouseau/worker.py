# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import redis
from rq import Worker, Queue, suspension
from .logger import logger
from . import config


# Queues THIS worker process consumes, from $QUEUES (space-separated). Default = all,
# so a plain `python -m crashclouseau.worker` (local/tests) still drains everything.
# The Procfile splits them: the ingestion `worker` runs "high default low" and a
# dedicated `agentworker` runs "agent", so a ~20-min agent job never blocks ingestion.
listen = os.getenv("QUEUES", "high default low agent").split()
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
    # Build queues on demand for ANY name (cached per process) so ENQUEUING is
    # decoupled from what this process CONSUMES (`listen`): the ingestion worker must
    # still be able to enqueue onto "agent" even though it no longer drains it.
    global __QUEUE
    if __QUEUE is None:
        __QUEUE = {}
    if name not in __QUEUE:
        __QUEUE[name] = Queue(name, connection=conn, default_timeout=6000)
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
