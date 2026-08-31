# Local Judge and worker smoke test

This walkthrough tests the standalone Judge only. It does not start the
Brunost Platform UI, user accounts, contests, submissions pages, or
leaderboards. The Platform is a separate consumer of the Judge API.

The test uses three terminal windows to simulate a control plane, a worker
node, and an operator submitting an evaluation. Everything uses temporary
SQLite and filesystem-artifact storage on the local machine.

## 1. Install the Judge

From the repository checkout, set this once in every terminal below:

```bash
export BRUNOST_JUDGE_REPO=/path/to/brunost-judge
cd "$BRUNOST_JUDGE_REPO"
python3 -m venv .venv-local
source .venv-local/bin/activate
python -m pip install -e '.[server]'

export BRUNOST_TEST_ROOT="$(mktemp -d /tmp/brunost-live.XXXXXX)"
export BRUNOST_JUDGE_API_TOKEN="local-admin-token"
export BRUNOST_JUDGE_REQUIRE_API_TOKEN=true
export BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN=true
export BRUNOST_JUDGE_CLUSTER_ID="local-test"
export BRUNOST_JUDGE_ARTIFACT_ROOT="$BRUNOST_TEST_ROOT/artifacts"
export BRUNOST_JUDGE_SANDBOX_MODE=process

mkdir -p "$BRUNOST_JUDGE_ARTIFACT_ROOT"
echo "Copy this path for the other terminals: $BRUNOST_TEST_ROOT"

brunost server \
  --host 127.0.0.1 \
  --port 8799 \
  --database "$BRUNOST_TEST_ROOT/judge.db"
```

Leave the server running. The `process` sandbox is for local development only;
it executes the scorer in the worker process. Production workers should use
Docker with gVisor/Kata/Firecracker or another certified isolation runtime.

If this worker will deliver callbacks to a Platform Kit application, also set
the same signing secret in the worker environment and in the platform:

```bash
export BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET=local-callback-secret
```

The Platform Kit rejects unsigned callbacks. A temporary callback receiver
outage is retried by the remote worker without terminating the worker loop.

## 2. Issue and consume a worker token

In Terminal 2, use the path printed by Terminal 1:

```bash
cd "$BRUNOST_JUDGE_REPO"
source .venv-local/bin/activate

export BRUNOST_TEST_ROOT="/tmp/PASTE-THE-PATH-HERE"
export BRUNOST_JUDGE_URL="http://127.0.0.1:8799"
export BRUNOST_JUDGE_API_TOKEN="local-admin-token"

JOIN_JSON="$(brunost cluster issue-node-token \
  --url "$BRUNOST_JUDGE_URL" \
  --token "$BRUNOST_JUDGE_API_TOKEN" \
  --node-id local-cpu-1 \
  --worker-id local-cpu-1 \
  --queue default \
  --resource-class cpu)"

export BRUNOST_JUDGE_JOIN_TOKEN="$(printf '%s' "$JOIN_JSON" | \
  python -c 'import json,sys; print(json.load(sys.stdin)["join_token"])')"

brunost node join \
  --url "$BRUNOST_JUDGE_URL" \
  --join-token "$BRUNOST_JUDGE_JOIN_TOKEN" \
  --output "$BRUNOST_TEST_ROOT/node.json"
```

The join token is short-lived and single-use. A `401` response from
`/v1/nodes/enroll` means that the token is wrong, expired, or already used;
issue a new token instead of retrying the old one.

## 3. Start the worker

In Terminal 3:

```bash
cd "$BRUNOST_JUDGE_REPO"
source .venv-local/bin/activate

export BRUNOST_JUDGE_SANDBOX_MODE=process
export BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS=true

brunost worker \
  --config "/tmp/PASTE-THE-PATH-HERE/node.json" \
  --poll-seconds 0.2
```

The worker connects outbound to the Judge. It does not need an inbound port.
It heartbeats, claims compatible work, downloads immutable artifacts, runs the
task, and posts the result.

## 4. Submit an artifact-backed evaluation

In a fourth terminal, with the same virtual environment and test-root path:

```bash
cd "$BRUNOST_JUDGE_REPO"
source .venv-local/bin/activate

export BRUNOST_TEST_ROOT="/tmp/PASTE-THE-PATH-HERE"
export BRUNOST_JUDGE_URL="http://127.0.0.1:8799"
export BRUNOST_JUDGE_API_TOKEN="local-admin-token"

python - <<'PY'
import json
import os
import time
from pathlib import Path

from brunost_judge.sdk import JudgeClient

root = Path(os.environ["BRUNOST_TEST_ROOT"])
client = JudgeClient(
    os.environ["BRUNOST_JUDGE_URL"],
    token=os.environ["BRUNOST_JUDGE_API_TOKEN"],
)

task = client.upload_artifact(Path("examples/ioai-cpu"))
client.register_task(
    task_ref="live/ioai-cpu-v1",
    artifact_id=task["artifact_id"],
    kind="ioai",
)

submission = root / "submission"
submission.mkdir(exist_ok=True)
(submission / "answer.txt").write_text("brunost\n", encoding="utf-8")
uploaded = client.upload_artifact(submission)

execution = client.submit(
    task_ref="live/ioai-cpu-v1",
    submission_artifact_id=uploaded["artifact_id"],
    idempotency_key="local-worker-test-1",
    queue="default",
    resource_class="cpu",
)
print("submitted:", execution["execution_id"])

for _ in range(30):
    result = client.get_execution(execution["execution_id"])
    print(result["status"], result.get("score"))
    if result["status"] in {"completed", "failed", "cancelled"}:
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "completed" and result.get("score") == 1.0 else 1)
    time.sleep(0.25)

raise SystemExit("timed out waiting for the worker")
PY
```

The successful result is:

```text
completed 1.0
```

The server log should contain a `200 OK` claim, artifact downloads, and a
`200 OK` finish. Repeated `204 No Content` responses from `/claim` are normal:
they mean the worker is healthy but the queue has no compatible work. The
submitted evaluation must use `queue: default` and `resource_class: cpu` for
this worker.

## 5. Verify and stop

```bash
brunost node doctor \
  --config "/tmp/PASTE-THE-PATH-HERE/node.json"
```

Press `Ctrl+C` in the worker and server terminals when finished. The temporary
test directory can then be removed.

## What this test covers

- Judge API health and authentication.
- One-time worker enrollment and scoped worker credentials.
- Worker heartbeats and queue/resource-class scheduling.
- Content-addressed task and submission artifacts.
- Worker-side artifact download and task-digest verification.
- Execution, scoring, and result persistence.

It does not test Platform users, contest registration, UI pages, leaderboard
policy, or Platform callbacks. Those are covered by the Platform Kit and its
integration flow.

For the shorter artifact-first control-plane canary, use
`BRUNOST_JUDGE_API_TOKEN=... scripts/canary.sh`. Unlike the manual path above,
it never submits mutable filesystem paths and is suitable for verifying that a
separate API and worker can share only the artifact backend.
