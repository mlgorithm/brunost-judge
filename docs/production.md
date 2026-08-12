# Production profile

The standalone repository is designed so the reference API can be replaced by a
production control plane without changing task packages or the SDK contract.

Before an official contest, operators must provide:

- Postgres or another durable store adapter instead of local SQLite;
- object storage for submissions, task packages, and bounded artifacts;
- isolated worker hosts using gVisor/Kata/Firecracker or an equivalent boundary;
- immutable, digest-pinned judge and runtime images;
- private worker queues and signed result callbacks;
- backups, monitoring, alerting, and a rehearsed restore/failover plan;
- a second failure domain for multi-country availability.

The Docker Compose files are a reference installation and intentionally do not
pretend that a shared Docker socket is a sufficient high-stakes isolation
boundary. Official IOAI/IOI use should pass a supervised security and capacity
certification before the profile is enabled.
