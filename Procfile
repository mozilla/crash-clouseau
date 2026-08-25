release: python bin/release.py
web: gunicorn -b 0.0.0.0:$PORT --timeout 60 crashclouseau:app
worker: QUEUES="high default low" python -m crashclouseau.worker
agentworker: QUEUES="agent" python -m crashclouseau.worker
clock: python bin/schedule.py