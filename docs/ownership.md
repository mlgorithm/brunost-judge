# System ownership boundary

Brunost Judge is an execution service, not an LMS. A country can use it
standalone, but it must provide a small control-plane application around it.

| Capability | Owner | Judge sees |
|---|---|---|
| Users, login, roles, organizations, countries | Platform/LMS | Opaque `user_id` and organization metadata |
| Email, invitations, password reset, notifications | Platform/LMS | Nothing; receives signed result events only |
| Contest registration, eligibility, deadlines, appeals | Platform/LMS | Opaque contest/task references and policy metadata |
| Task statements, translations, contest UI | Platform/LMS | Immutable task package reference/digest |
| Submission intake and object-storage upload | Platform/LMS or country adapter | A scoped artifact reference, never user credentials |
| Queueing, worker leases, sandbox execution, scoring | Judge | Execution request and immutable task/package data |
| Execution logs, metrics, artifacts, result callbacks | Judge | Execution record keyed by platform idempotency key |
| Leaderboard calculation, visibility, freeze/reveal, medals | Platform/LMS | Public/private score fields and execution status |
| Emailing results and certificates | Platform/LMS | No email addresses or notification policy |
| Worker health, capacity, backups, restore, failover | Judge operator | No end-user identity data |

## Request and result flow

1. The platform authenticates the student and applies contest policy.
2. The platform uploads the submission and creates a stable idempotency key.
3. The platform sends the judge a task reference/digest, scoped artifact
   reference, resource class, and callback URL/token.
4. The judge schedules and runs the task in an isolated worker.
5. The judge sends a signed, idempotent result callback.
6. The platform stores the result against its user/submission/contest records,
   applies leaderboard rules, and sends any email or UI notification.

The judge must not add `/users`, `/leaderboards`, `/email`, or `/contestants`
endpoints. If a country wants a turnkey installation, its adapter can provide
those features while using the judge API as the execution layer.

The platform remains authoritative for identity and policy. The judge remains
authoritative for whether and how an execution ran, its score/metrics, and its
worker-side evidence.
