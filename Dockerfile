# Stage 0: self-signed cert for local HTTPS (dev only)
FROM alpine

RUN apk update && apk add openssl
RUN openssl req \
    -newkey rsa:4096 -nodes -sha256 -keyout server.key \
    -x509 -days 365 -out server.crt \
    -subj "/C=FR/ST=Paris/L=Paris/O=Crash-Stop/OU=Crash/CN=crash-stop.org"

# Stage 1: application image
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV DATABASE_URL=postgresql://clouseau:passwd@postgres:5432/clouseau
ENV REDIS_URL=redis://queue
ENV PORT=8081
ENV PYTHONPATH=.
ENV PYTHONUNBUFFERED=1
# Use the image's Python instead of letting uv download its own.
ENV UV_PYTHON_PREFERENCE=only-system
# Keep the venv out of /code: docker-compose bind-mounts the source over /code
# at runtime, which would otherwise shadow a venv built there.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /code
COPY pyproject.toml uv.lock /code/
RUN uv sync --locked --no-default-groups

WORKDIR /

COPY --from=0 server.* /
ADD Procfile .
RUN sed -i 's/gunicorn/gunicorn --reload --reload-extra-file static --reload-extra-file templates --certfile=\/server.crt --keyfile=\/server.key/g' Procfile

WORKDIR /code

EXPOSE 8081
