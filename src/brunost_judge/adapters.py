"""Provider-neutral execution adapter specifications.

Adapters return a normalized launch plan.  A deployment-specific launcher can
submit that plan to its provider and report lifecycle events back to Brunost.
No adapter silently executes untrusted code from the control plane process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LaunchRequest:
    execution_id: str
    image: str
    command: tuple[str, ...]
    environment: dict[str, str] = field(default_factory=dict)
    cpu_cores: float = 1
    memory_mb: int = 512
    gpu_count: int = 0
    network: str = "disabled"
    queue: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LaunchPlan:
    provider: str
    execution_id: str
    payload: dict[str, Any]


class ExecutionAdapter(Protocol):
    provider: str

    def plan(self, request: LaunchRequest) -> LaunchPlan: ...


class DockerAdapter:
    provider = "docker"

    def plan(self, request: LaunchRequest) -> LaunchPlan:
        args = ["docker", "run", "--rm", "--network", "none", "--cpus", str(request.cpu_cores), "--memory", f"{request.memory_mb}m"]
        if request.network != "disabled":
            args[4] = request.network
        args.extend([request.image, *request.command])
        return LaunchPlan(self.provider, request.execution_id, {"argv": args, "env": request.environment})


class KubernetesAdapter:
    provider = "kubernetes"

    def __init__(self, namespace: str = "brunost") -> None:
        self.namespace = namespace

    def plan(self, request: LaunchRequest) -> LaunchPlan:
        return LaunchPlan(self.provider, request.execution_id, {
            "namespace": self.namespace,
            "job_name": f"brunost-{request.execution_id}",
            "image": request.image,
            "command": list(request.command),
            "env": request.environment,
            "resources": {
                "requests": {"cpu": str(request.cpu_cores), "memory": f"{request.memory_mb}Mi"},
                "limits": {"cpu": str(request.cpu_cores), "memory": f"{request.memory_mb}Mi", "nvidia.com/gpu": request.gpu_count},
            },
            "network_policy": request.network,
        })


class SlurmAdapter:
    provider = "slurm"

    def __init__(self, partition: str = "cpu") -> None:
        self.partition = partition

    def plan(self, request: LaunchRequest) -> LaunchPlan:
        return LaunchPlan(self.provider, request.execution_id, {
            "argv": ["sbatch", "--parsable", "--partition", self.partition, "--cpus-per-task", str(max(1, int(request.cpu_cores))), "--mem", str(request.memory_mb), *request.command],
            "image": request.image,
            "environment": request.environment,
            "gpus": request.gpu_count,
        })


class OpenStackAdapter:
    provider = "openstack"

    def __init__(self, flavor: str = "m1.small", network: str | None = None) -> None:
        self.flavor = flavor
        self.network = network

    def plan(self, request: LaunchRequest) -> LaunchPlan:
        return LaunchPlan(self.provider, request.execution_id, {
            "server_name": f"brunost-{request.execution_id}",
            "flavor": self.flavor,
            "network": self.network,
            "image": request.image,
            "command": list(request.command),
            "environment": request.environment,
            "metadata": {"brunost-execution-id": request.execution_id, **request.metadata},
        })
