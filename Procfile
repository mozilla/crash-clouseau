web: gunicorn -b 0.0.0.0:$PORT crashclouseau:app
worker: python -m crashclouseau.worker
clock: python bin/schedule.py