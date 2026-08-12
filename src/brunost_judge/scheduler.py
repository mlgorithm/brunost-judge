"""Capability-aware scheduling primitives.

The control plane can use this module with SQLite, PostgreSQL, or an external
queue.  It intentionally has no infrastructure dependency: provider adapters
turn the selected worker into a Docker, Kubernetes, Slurm, or OpenStack job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class WorkerAdvertisement:
    worker_id: str
    capabilities: frozenset[str] = frozenset()
    queues: frozenset[str] = frozenset({"default"})
    resource_classes: frozenset[str] = frozenset({"cpu"})
    region: str | None = None
    active_leases: int = 0
    draining: bool = False
    last_seen: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class SchedulingRequest:
    queue: str = "default"
    resource_class: str = "cpu"
    required_capabilities: frozenset[str] = frozenset()
    preferred_region: str | None = None
    tenant: str = "default"


class CapabilityScheduler:
    """Deterministic worker selection with capability and load filtering."""

    def __init__(self) -> None:
        self._workers: dict[str, WorkerAdvertisement] = {}
        self._tenant_assignments: dict[str, int] = {}

    def register(self, worker: WorkerAdvertisement) -> None:
        self._workers[worker.worker_id] = worker

    def remove(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)

    def workers(self) -> tuple[WorkerAdvertisement, ...]:
        return tuple(sorted(self._workers.values(), key=lambda item: item.worker_id))

    def candidates(self, request: SchedulingRequest) -> tuple[WorkerAdvertisement, ...]:
        return tuple(
            worker
            for worker in self._workers.values()
            if not worker.draining
            and request.queue in worker.queues
            and request.resource_class in worker.resource_classes
            and request.required_capabilities.issubset(worker.capabilities)
        )

    def choose(self, request: SchedulingRequest) -> WorkerAdvertisement | None:
        candidates = self.candidates(request)
        if not candidates:
            return None
        selected = min(
            candidates,
            key=lambda worker: (
                0 if request.preferred_region and worker.region == request.preferred_region else 1,
                worker.active_leases,
                self._tenant_assignments.get(request.tenant, 0),
                worker.worker_id,
            ),
        )
        self._tenant_assignments[request.tenant] = self._tenant_assignments.get(request.tenant, 0) + 1
        return selected
