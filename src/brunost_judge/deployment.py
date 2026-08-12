"""Generated, dependency-light country deployment bundle."""

from __future__ import annotations

from pathlib import Path

CONTROL_PLANE_COMPOSE = """services:
  judge-postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: [\"CMD-SHELL\", \"pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}\"]
      interval: 5s
      timeout: 5s
      retries: 12
    volumes:
      - judge-postgres-data:/var/lib/postgresql/data
    restart: unless-stopped

  judge-api:
    image: ${BRUNOST_JUDGE_IMAGE}
    command: [\"brunost\", \"server\", \"--host\", \"0.0.0.0\", \"--port\", \"8787\"]
    environment:
      BRUNOST_JUDGE_DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@judge-postgres:5432/${POSTGRES_DB}
      BRUNOST_JUDGE_API_TOKEN: ${BRUNOST_JUDGE_API_TOKEN}
      BRUNOST_JUDGE_REQUIRE_API_TOKEN: \"true\"
      BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN: \"true\"
      BRUNOST_JUDGE_CLUSTER_ID: ${BRUNOST_JUDGE_CLUSTER_ID}
      BRUNOST_JUDGE_ARTIFACT_ROOT: /var/lib/brunost/artifacts
      BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET: ${BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET}
      BRUNOST_JUDGE_ENV: production
      BRUNOST_JUDGE_REQUIRE_HTTPS_CALLBACKS: "true"
      BRUNOST_JUDGE_CALLBACK_HOSTS: ${BRUNOST_JUDGE_CALLBACK_HOSTS:?set the platform callback hostname allowlist}
    ports:
      - \"8787:8787\"
    volumes:
      - judge-artifacts:/var/lib/brunost/artifacts
    depends_on:
      judge-postgres:
        condition: service_healthy
    restart: unless-stopped

volumes:
  judge-postgres-data:
  judge-artifacts:
"""

WORKER_COMPOSE = """services:
  docker-socket-proxy:
    image: ${BRUNOST_DOCKER_SOCKET_PROXY_IMAGE:?set BRUNOST_DOCKER_SOCKET_PROXY_IMAGE to a digest-pinned proxy image}
    environment:
      CONTAINERS: "1"
      IMAGES: "1"
      INFO: "1"
      VERSION: "1"
      POST: "1"
      NETWORKS: "0"
      VOLUMES: "0"
      EXEC: "0"
      AUTH: "0"
      SECRETS: "0"
      SWARM: "0"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    restart: unless-stopped
  judge-worker:
    image: ${BRUNOST_JUDGE_IMAGE}
    command: [\"brunost\", \"worker\", \"--config\", \"/etc/brunost/node.json\"]
    environment:
      BRUNOST_JUDGE_SANDBOX_MODE: ${BRUNOST_JUDGE_SANDBOX_MODE:-docker}
      BRUNOST_JUDGE_SANDBOX_IMAGE: ${BRUNOST_JUDGE_SANDBOX_IMAGE}
      BRUNOST_JUDGE_SANDBOX_RUNTIME: ${BRUNOST_JUDGE_SANDBOX_RUNTIME:-runsc}
      BRUNOST_JUDGE_REQUIRE_SECCOMP: \"true\"
      BRUNOST_JUDGE_SANDBOX_SECCOMP: ${BRUNOST_JUDGE_SANDBOX_SECCOMP:-/etc/docker/seccomp/brunost-seccomp.json}
      BRUNOST_JUDGE_ENV: production
      BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS: \"true\"
      DOCKER_HOST: tcp://docker-socket-proxy:2375
    depends_on:
      - docker-socket-proxy
    volumes:
      - ${BRUNOST_NODE_CONFIG:-./brunost-node.json}:/etc/brunost/node.json:ro
      - ${BRUNOST_TASK_ROOT:-./tasks}:/tasks:ro
      - ${BRUNOST_SUBMISSION_ROOT:-./submissions}: /submissions
    restart: unless-stopped
"""

WORKER_COMPOSE = WORKER_COMPOSE.replace(": /submissions", ":/submissions")

RUNBOOK = """# Country cluster runbook

1. Start the control plane on Node 1:

   `docker compose --env-file .env -f docker-compose.control.yml up -d`

2. Issue one join token per worker from Node 1:

`brunost cluster issue-node-token --url https://judge.example --node-id node-2`

3. On each worker, run `brunost node join` and then:

   `docker compose --env-file worker.env -f docker-compose.worker.yml up -d`

4. Run `brunost node doctor --config /etc/brunost/node.json`.
5. Upload task and submission bundles with `brunost artifact upload`.
6. Run the CPU canary before enabling contest traffic.

The generated compose files are a reference deployment. Put the API behind
TLS, replace the callback hostname placeholder before startup, keep the global
API token on the control plane only, and use a replicated PostgreSQL/object-
storage service for an official contest.
"""


def render_country_bundle(root: str | Path, *, force: bool = False) -> list[Path]:
    """Write the operator-facing three-node bundle and return its files."""

    destination = Path(root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    files = {
        "docker-compose.control.yml": CONTROL_PLANE_COMPOSE,
        "docker-compose.worker.yml": WORKER_COMPOSE,
        "RUNBOOK.md": RUNBOOK,
        "worker.env.example": "BRUNOST_JUDGE_IMAGE=ghcr.io/mlgorithm/brunost-judge@sha256:<64-hex-digest>\nBRUNOST_JUDGE_SANDBOX_IMAGE=ghcr.io/brunost/judge-runtime@sha256:<64-hex-digest>\nBRUNOST_DOCKER_SOCKET_PROXY_IMAGE=tecnativa/docker-socket-proxy@sha256:<64-hex-digest>\nBRUNOST_NODE_CONFIG=/etc/brunost/node.json\n",
    }
    written: list[Path] = []
    for name, content in files.items():
        path = destination / name
        if path.exists() and not force:
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
