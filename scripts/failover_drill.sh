#!/usr/bin/env bash
# Operator-assisted worker-loss drill for the Compose reference deployment.
# Default mode prints the sequence. `--execute` requires an explicit confirmation
# and a canary execution ID, then performs the reversible worker restart.
set -euo pipefail

lease="${BRUNOST_JUDGE_LEASE_SECONDS:-30}"
execution_id="${BRUNOST_JUDGE_EXECUTION_ID:-}"
if [[ "${1:-}" == "--execute" ]]; then
  [[ "${BRUNOST_JUDGE_CONFIRM:-}" == "YES" ]] || { echo "Set BRUNOST_JUDGE_CONFIRM=YES to execute the drill" >&2; exit 2; }
  [[ -n "$execution_id" ]] || { echo "Set BRUNOST_JUDGE_EXECUTION_ID to the canary execution ID" >&2; exit 2; }
  service="${BRUNOST_JUDGE_WORKER_SERVICE:-judge-worker}"
  compose_file="${BRUNOST_JUDGE_COMPOSE_FILE:-docker-compose.yml}"
  echo "Stopping $service; this intentionally creates a worker-loss window."
  docker compose -f "$compose_file" stop "$service"
  sleep "$((lease + 5))"
  docker compose -f "$compose_file" up -d "$service"
  deadline=$((SECONDS + ${BRUNOST_JUDGE_FAILOVER_TIMEOUT_SECONDS:-180}))
  while (( SECONDS < deadline )); do
    payload="$(curl -fsS -H "Authorization: Bearer ${BRUNOST_JUDGE_API_TOKEN:-}" "${BRUNOST_JUDGE_URL:-http://127.0.0.1:8787}/v1/executions/$execution_id")"
    status="$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))')"
    if [[ "$status" == "completed" ]]; then echo "FAILOVER DRILL PASSED: $execution_id"; exit 0; fi
    if [[ "$status" == "failed" || "$status" == "canceled" ]]; then echo "FAILOVER DRILL FAILED: $status" >&2; exit 1; fi
    sleep 2
  done
  echo "FAILOVER DRILL TIMED OUT: $execution_id" >&2
  exit 1
fi
echo "1. Start worker A with --lease-seconds $lease."
echo "2. Run scripts/canary.sh and record the execution ID."
echo "3. Stop worker A: docker compose stop judge-worker."
echo "4. Wait $((lease + 5)) seconds for the lease to expire."
echo "5. Start worker B with the same database: docker compose up -d judge-worker."
echo "6. Poll the recorded execution; it must complete exactly once."
echo "7. Verify /v1/stats has no queued/running work and inspect callback receipts."
echo "FAILOVER DRILL PLAN READY (execution requires an operator window)."
