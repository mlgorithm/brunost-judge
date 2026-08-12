# Scheduling and provider adapters

`brunost_judge.scheduler.CapabilityScheduler` is the provider-independent
selection layer. It filters workers by queue, resource class, required
capabilities, drain state, and preferred region, then chooses the least-loaded
compatible worker deterministically.

`brunost_judge.adapters` provides normalized launch plans for:

- Docker/local hosts
- Kubernetes Jobs
- Slurm `sbatch`
- OpenStack server launches

The adapters deliberately return plans instead of making provider API calls
from the control plane. A deployment can add credentials, admission policy,
quotas, and provider-specific retries in a separate launcher service.

This keeps the public judge portable while allowing a country deployment to use
NREC/OpenStack, Kubernetes, Slurm, or bare metal without changing task APIs.
