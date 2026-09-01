# Zero-code node onboarding

Brunost Judge supports a country operator adding worker nodes without writing
integration code. The control plane issues a short-lived, single-use join
token. The node exchanges that token for a scoped worker credential, writes a
small local configuration file, and then connects outbound to the control
plane over HTTPS.

## Control-plane setup

Generate a private cluster environment on the first server:

```bash
brunost cluster init /srv/brunost
```

The command also generates `docker-compose.control.yml`,
`docker-compose.worker.yml`, `worker.env.example`, and `RUNBOOK.md`. The first
file starts PostgreSQL and the Judge API; the worker file is used on Nodes 2 and
3 after enrollment. The worker Compose file requires
`BRUNOST_JUDGE_WORKER_IMAGE`, a digest-pinned image built from
[`Dockerfile.worker`](../Dockerfile.worker). Do not point this setting at
`BRUNOST_JUDGE_IMAGE`: that is the smaller API/control-plane image and
intentionally has no Docker CLI.

Load the generated `.env` into the API deployment and start the API with a
shared PostgreSQL database. The generated configuration includes a cluster ID,
random API token, callback signing secret, and database password. Keep the file
private.

Issue one token per node:

```bash
brunost cluster issue-node-token \
  --url https://judge.country.example \
  --token "$BRUNOST_JUDGE_API_TOKEN" \
  --node-id country-node-2 \
  --worker-id country-node-2-cpu \
  --capability runtime:docker \
  --region north
```

The command prints a join token. It expires in 15 minutes by default and can
only be consumed once.

## Worker-node setup

On the new node, run the official Brunost image or installed CLI:

```bash
brunost node join \
  --url https://judge.country.example \
  --join-token '<one-time-token>' \
  --output /etc/brunost/node.json \
  --path-map /srv/brunost/tasks=/tasks \
  --path-map /srv/brunost/submissions=/submissions
```

The command detects the node hostname and basic CPU, Docker, and NVIDIA GPU
capabilities, registers the resulting resource classes, and saves the worker
credential with mode `0600`. Additional hints can be supplied with repeated
`--capability` or `--resource-class` flags. No source code or API integration is
required.

Start the worker agent using the saved configuration:

```bash
brunost worker --config /etc/brunost/node.json
```

For production, run that command as a container or systemd service. The agent
heartbeats, claims only work matching its registered queues/resource classes,
executes in the configured sandbox, and submits a signed result. The worker
credential cannot list tasks, issue enrollment tokens, or control another
worker. The generated worker container runs as UID `10001`, with a read-only
root filesystem, `no-new-privileges`, all Linux capabilities dropped, and a
bounded `/tmp` tmpfs. Before mounting a config produced by `node join`, give
that UID read access while retaining owner-only permissions:

```bash
sudo chown 10001:10001 /etc/brunost/node.json
sudo chmod 0600 /etc/brunost/node.json
```

## Verify the node

```bash
brunost node doctor --config /etc/brunost/node.json
```

The check verifies the public API health endpoint and the worker's scoped
credential. A node may be drained from the operator API before maintenance;
queued jobs remain durable and can be picked up by another worker.

If a node credential is lost, revoke it immediately:

```bash
brunost node revoke --url https://judge.country.example \
  --token "$BRUNOST_JUDGE_API_TOKEN" --worker-id country-node-2-cpu
```

## Three-node country layout

For a small country deployment, run the API/control plane against shared
PostgreSQL on the first node and enroll the other two as CPU/GPU workers. For
high availability, run API replicas on all three nodes and keep the database,
queue, and object storage replicated. Use a separate worker failure domain when
untrusted code must never share a host with control-plane services.

Workers need a shared task/submission mount (or an object-storage synchronizer)
only when using legacy filesystem paths. The portable path is to upload bundles
to the control plane artifact store:

```bash
brunost artifact upload ./tasks/ioai-example --url https://judge.country.example
brunost artifact upload ./submissions/student-1 --url https://judge.country.example
```

Then register the returned task artifact and submit with its artifact ID. The
worker downloads and verifies the content-addressed archive automatically; no
shared filesystem or path map is required. The generated bundle mounts the
artifact root persistently, so it can later be replaced by an S3/MinIO adapter
without changing task packages.
