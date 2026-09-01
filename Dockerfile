# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Keep this argument digest-pinned for release builds. Local developers can
# override it with --build-arg PYTHON_IMAGE=python:3.13-slim when deliberately
# testing a newer base; the committed default is the reproducible release base.
ARG PYTHON_IMAGE=python:3.13.15-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRUNOST_JUDGE_DB=/data/judge.db \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# The lock is part of the release input. Do not replace these commands with a
# floating pip install: uv verifies the production graph in uv.lock.
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra production --no-install-project --no-cache
COPY src ./src
RUN uv sync --locked --no-dev --extra production --no-cache \
    && rm --force /bin/uv /bin/uvx \
    && useradd --system --uid 10001 --create-home brunost \
    && mkdir --parents /data \
    && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/readyz', timeout=3)"

ENTRYPOINT ["brunost"]
CMD ["server", "--host", "0.0.0.0", "--port", "8787"]
