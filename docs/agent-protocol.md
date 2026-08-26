# Agent protocol v1

The bundled agent runtime uses one child process per seat and newline-delimited
UTF-8 JSON over stdin/stdout. The protocol is intentionally dependency-free so
agents can be written in any language available in the evaluator image.

## Lifecycle

The judge sends an `init` message first:

```json
{"type":"init","protocol_version":1,"agent_id":"red","seat":0,"seed":19,"metadata":{}}
```

The agent must answer:

```json
{"type":"ready"}
```

For each turn, the judge sends:

```json
{"type":"turn","turn":1,"state":{"round":1},"seed":19,"agent_id":"red","seat":0}
```

The agent answers with an action of any JSON type:

```json
{"type":"action","action":{"move":"left"}}
```

The judge sends `{"type":"shutdown"}` when the match ends normally. Agents
must flush each response and must never write diagnostics to stdout; stderr is
discarded by the runtime.

## Ordering and simultaneous turns

`AgentRuntime.step(state)` requests actions in ascending seat order. A trusted
referee can use `AgentRuntime.step(state, simultaneous=True)` to send one turn
to all seats concurrently. Returned actions are still assembled in ascending
seat order, so scoring remains deterministic while response latency is not
observable between seats.

Unknown fields are allowed for additive extensions. Message types, required
fields, protocol version, and framing are validated by
`brunost_judge.agent_protocol` and `grader.agent_protocol`.

## Limits and telemetry

Each seat has startup, per-request, total-runtime, message-size, turn-count,
memory, file-size, open-file, and stderr-output limits. Agent processes receive
an allowlisted environment containing runtime basics and Brunost protocol
variables; worker credentials and unrelated host secrets are not inherited.
The runtime terminates stalled or crashed processes and exposes bounded timing
and stderr diagnostics through `runtime.metrics()`. Host-level
Docker/gVisor/Kata isolation remains required for production execution; the
Python runtime limits are a second boundary, not a replacement for the
container policy.

## Local validation

Agent bundles default to `agent.py`, or can declare a shell-free argv in
`agent.yaml`/`agent.json`. Validate an artifact statically or launch its
startup handshake:

```bash
brunost agent validate ./agents/red
brunost agent validate ./agents/red --smoke
brunost agent protocol
brunost match run ./games/closest-number \
  --agent steady=./agents/steady \
  --agent round-robin=./agents/round-robin
```

The reference game and agents under `examples/games/closest-number` and
`examples/agents` provide a complete minimal match.
