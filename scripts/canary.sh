#!/usr/bin/env bash
set -euo pipefail

task_path="${BRUNOST_JUDGE_CANARY_TASK:-examples/ioai-cpu}"
submission_path="${BRUNOST_JUDGE_CANARY_SUBMISSION:-local-submissions/canary}"
mkdir -p "$submission_path"
exec brunost canary \
  --url "${BRUNOST_JUDGE_URL:-http://127.0.0.1:8787}" \
  --token "${BRUNOST_JUDGE_API_TOKEN:-}" \
  --task-path "$task_path" \
  --submission "$submission_path" \
  --task-ref "${BRUNOST_JUDGE_CANARY_TASK_REF:-canary/ioai-cpu-v1}" \
  --timeout "${BRUNOST_JUDGE_CANARY_TIMEOUT_SECONDS:-180}"
