"""Regression checks for release-image inputs and worker isolation policy."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_api_and_worker_images_use_digest_pinned_bases_and_locked_dependencies():
    for name in ("Dockerfile", "Dockerfile.worker"):
        dockerfile = (ROOT / name).read_text(encoding="utf-8")
        assert "# syntax=docker/dockerfile:1.7@sha256:" in dockerfile
        assert "ARG PYTHON_IMAGE=python:3.13.15-slim@sha256:" in dockerfile
        assert "ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.26@sha256:" in dockerfile
        assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
        assert "uv sync --locked --no-dev --extra production" in dockerfile
        assert 'pip install --no-cache-dir ".[production]"' not in dockerfile

    worker = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "ARG DOCKER_CLI_IMAGE=docker:27.5.1-cli@sha256:" in worker
    assert 'COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker' in worker
    assert "USER 10001:10001" in worker


def test_production_overlay_keeps_the_docker_worker_hardened():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    base_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    worker = compose.split("  judge-worker:\n", 1)[1]
    assert "dockerfile: Dockerfile.worker" in worker
    assert 'user: "10001:10001"' in worker
    assert "read_only: true" in worker
    assert "/tmp:rw,noexec,nosuid,size=${BRUNOST_JUDGE_WORKER_TMPFS_SIZE:-256m}" in base_compose
    assert "no-new-privileges:true" in worker
    assert "cap_drop:\n      - ALL" in worker
    assert "pids_limit: ${BRUNOST_JUDGE_WORKER_PIDS_LIMIT:-512}" in worker
