# Standalone deployment

Brunost Judge can be used without the NOKI/Brunost platform.

## Local development

```bash
uv run --extra server brunost init ./my-judge
uv run --extra server brunost task new ioai ./my-judge/tasks/demo
uv run --extra server brunost task validate ./my-judge/tasks/demo
```

For the bundled reference services:

```bash
mkdir -p local-submissions
docker compose up --build
```

The API is available at `http://127.0.0.1:8787`, with interactive docs at
`/docs` and a small operator landing page at `/console`. Register a task through
`POST /v1/tasks`, submit an execution through `POST /v1/executions`, and poll the
execution or receive a callback.

Set `BRUNOST_JUDGE_API_TOKEN`, `BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET`, and
`POSTGRES_PASSWORD` to non-default values before exposing the API beyond a local
test. The Compose profile uses PostgreSQL; a direct `brunost server` invocation
still defaults to SQLite for development.

Workers can be split by queue and hardware:

```bash
brunost worker --queue default --resource-class cpu --worker-id cpu-1
brunost worker --queue ioai-gpu --resource-class gpu --worker-id gpu-1
```

The API exposes `/v1/stats` and `/v1/executions` for a lightweight operator
view; the `/console` page uses the API token stored in the current browser.

## Existing platform integration

An LMS should use the SDK or the JSON API. It should send a stable idempotency
key for every submission attempt and treat the callback/result as idempotent.
The LMS remains authoritative for users, contests, leaderboards, appeals, and
visibility policy.
