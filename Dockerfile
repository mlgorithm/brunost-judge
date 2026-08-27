FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRUNOST_JUDGE_DB=/data/judge.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# The API image is also the production control-plane image.  Production uses
# PostgreSQL (and may use S3-compatible artifact storage), so the image must
# include the production extras rather than only the HTTP server dependencies.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[production]" \
    && useradd --system --uid 10001 --create-home brunost \
    && mkdir --parents /data \
    && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/readyz', timeout=3)"

ENTRYPOINT ["brunost"]
CMD ["server", "--host", "0.0.0.0", "--port", "8787"]
