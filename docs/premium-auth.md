# Premium-to-judge authentication

Brunost Premium owns people and product identity. It authenticates users with
its own session/OIDC system, applies organization and contest permissions, and
then calls the open-source judge as a backend service. A browser should never
receive the judge admin token or a Premium service credential.

## Recommended setup

1. The judge operator configures the global admin token only on the judge
   control plane, preferably through `BRUNOST_JUDGE_API_TOKEN_FILE`.
2. The operator creates one credential for the Premium backend:

   ```http
   POST /v1/auth/service-credentials
   Authorization: Bearer <admin-token>
   Content-Type: application/json

   {"name":"brunost-premium","scopes":["judge:read","judge:write"]}
   ```

3. Premium stores the returned `token` in its own secret manager. The judge
   stores only a SHA-256 token digest and the scope/expiry metadata.
4. Premium sends `Authorization: Bearer <service-token>` for task registration,
   artifact upload, evaluation submission, and result reads.
5. Premium receives callbacks over HTTPS, validates the HMAC signature and
   event ID using the callback signing secret, and deduplicates event IDs.

Premium must not use a service credential to enroll nodes, drain workers,
revoke credentials, rotate the admin token, or read the audit log. Those are
operator actions and require `judge:admin`/the global admin token.

## Rotation

Create a replacement service credential, deploy it to Premium, verify traffic,
then revoke the old credential. Service credentials can have a bounded
`ttl_seconds` value. Admin token rotation is separate and uses the atomic
`BRUNOST_JUDGE_API_TOKEN_FILE` flow described in [the API guide](api.md#authentication).

## Boundary

The judge deliberately does not model Premium users, teams, courses, contest
ownership, or end-user roles. Premium maps an authorized user action to a
judge API request and records the user-facing audit trail in its own system;
the judge audit log records the service credential or operator that reached the
control plane.
