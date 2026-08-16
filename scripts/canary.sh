#!/usr/bin/env bash
set -euo pipefail

task_path="${BRUNOST_JUDGE_CANARY_TASK:-examples/ioi-sum}"
submission_path="${BRUNOST_JUDGE_CANARY_SUBMISSION:-examples/canary-ioi-sum}"
canary_args=(
  --url "${BRUNOST_JUDGE_URL:-http://127.0.0.1:8787}"
  --token "${BRUNOST_JUDGE_API_TOKEN:-}"
  --task-path "$task_path"
  --submission "$submission_path"
  --task-ref "${BRUNOST_JUDGE_CANARY_TASK_REF:-canary/ioi-sum-v1}"
  --timeout "${BRUNOST_JUDGE_CANARY_TIMEOUT_SECONDS:-180}"
)
if [[ -n "${BRUNOST_JUDGE_CANARY_CALLBACK_URL:-}" ]]; then
  canary_args+=(--callback-url "$BRUNOST_JUDGE_CANARY_CALLBACK_URL")
fi
if [[ -n "${BRUNOST_JUDGE_CANARY_CALLBACK_TOKEN:-}" ]]; then
  canary_args+=(--callback-token "$BRUNOST_JUDGE_CANARY_CALLBACK_TOKEN")
fi
exec brunost canary "${canary_args[@]}"
