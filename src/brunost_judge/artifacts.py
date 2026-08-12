"""Content-addressed task and submission bundles.

The control plane stores bundles under a durable artifact root. Workers fetch
them over the authenticated API, which removes the requirement that every
country node share a filesystem. Archives are extracted with traversal and
symlink checks before being passed to the sandbox.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any


class ArtifactError(ValueError):
    """Raised when an artifact is invalid or unsafe to extract."""


def _safe_id(value: str) -> str:
    if not value or len(value) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
        raise ArtifactError("artifact_id contains unsupported characters")
    return value


def artifact_id(data: bytes) -> str:
    """Return the immutable SHA-256 identifier for an archive."""

    return hashlib.sha256(data).hexdigest()


def pack_directory(path: str | Path) -> bytes:
    """Create a deterministic gzip tar bundle from a directory."""

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ArtifactError(f"artifact source is not a directory: {root}")
    tar_output = io.BytesIO()
    with tarfile.open(fileobj=tar_output, mode="w") as archive:
        for item in sorted(root.rglob("*")):
            if item.is_symlink():
                raise ArtifactError(f"symlinks are not allowed in artifacts: {item}")
            relative = item.relative_to(root).as_posix()
            info = archive.gettarinfo(str(item), arcname=relative)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if item.is_file():
                with item.open("rb") as source:
                    archive.addfile(info, source)
            else:
                archive.addfile(info)
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        compressed.write(tar_output.getvalue())
    return output.getvalue()


def safe_extract(data: bytes, destination: str | Path) -> Path:
    """Extract a bundle while rejecting traversal, links, and special files."""

    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        members = archive.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise ArtifactError("artifact contains a path traversal")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ArtifactError("artifact contains an unsupported link or special file")
        for member in members:
            target = root / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ArtifactError("artifact member could not be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    return root


class ArtifactStore:
    """Small filesystem backend; object-storage adapters can share this API."""

    def __init__(self, root: str | Path = "artifacts", *, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, artifact: str) -> Path:
        value = _safe_id(artifact)
        return self.root / value[:2] / f"{value}.tar.gz"

    def put(self, data: bytes, *, expected_id: str | None = None) -> dict[str, object]:
        if len(data) > self.max_bytes:
            raise ArtifactError(f"artifact exceeds {self.max_bytes} bytes")
        identifier = artifact_id(data)
        if expected_id and _safe_id(expected_id) != identifier:
            raise ArtifactError("artifact_id does not match the uploaded bytes")
        target = self.path(identifier)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(f".tmp-{os.getpid()}")
            temporary.write_bytes(data)
            temporary.replace(target)
        return {"artifact_id": identifier, "size_bytes": len(data), "sha256": identifier}

    def get(self, identifier: str) -> bytes:
        target = self.path(identifier)
        if not target.is_file():
            raise FileNotFoundError(identifier)
        data = target.read_bytes()
        if artifact_id(data) != _safe_id(identifier):
            raise ArtifactError("artifact checksum mismatch")
        return data

    def materialize(self, identifier: str) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
        temporary = tempfile.TemporaryDirectory(prefix="brunost-artifact-")
        try:
            return safe_extract(self.get(identifier), temporary.name), temporary
        except Exception:
            temporary.cleanup()
            raise


class S3ArtifactStore(ArtifactStore):
    """Content-addressed artifact store backed by S3-compatible object storage."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        max_bytes: int = 512 * 1024 * 1024,
        prefix: str = "brunost/artifacts",
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised in production images
            raise ArtifactError("S3 artifact storage requires the brunost-judge production extra") from exc
        if not bucket.strip():
            raise ArtifactError("S3 artifact bucket is required")
        session = boto3.session.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
        )
        self.client: Any = session.client("s3", endpoint_url=endpoint_url)
        self.bucket = bucket
        self.max_bytes = max(1, int(max_bytes))
        self.prefix = prefix.strip("/")

    def _key(self, identifier: str) -> str:
        return f"{self.prefix}/{identifier[:2]}/{identifier}.tar.gz"

    def path(self, artifact: str) -> str:
        return f"s3://{self.bucket}/{self._key(_safe_id(artifact))}"

    def put(self, data: bytes, *, expected_id: str | None = None) -> dict[str, object]:
        if len(data) > self.max_bytes:
            raise ArtifactError(f"artifact exceeds {self.max_bytes} bytes")
        identifier = artifact_id(data)
        if expected_id and _safe_id(expected_id) != identifier:
            raise ArtifactError("artifact_id does not match the uploaded bytes")
        self.client.put_object(Bucket=self.bucket, Key=self._key(identifier), Body=data, ContentType="application/gzip")
        return {"artifact_id": identifier, "size_bytes": len(data), "sha256": identifier}

    def get(self, identifier: str) -> bytes:
        safe_identifier = _safe_id(identifier)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(safe_identifier))
        except Exception as exc:
            raise FileNotFoundError(identifier) from exc
        body = response["Body"]
        try:
            data = body.read()
        finally:
            body.close()
        if artifact_id(data) != safe_identifier:
            raise ArtifactError("artifact checksum mismatch")
        return data


def artifact_store_from_environment() -> ArtifactStore:
    """Build the local or S3 artifact backend selected by deployment config."""
    backend = os.environ.get("BRUNOST_JUDGE_ARTIFACT_BACKEND", "filesystem").strip().lower()
    max_bytes = int(os.environ.get("BRUNOST_JUDGE_ARTIFACT_MAX_BYTES", str(512 * 1024 * 1024)))
    if backend in {"s3", "object", "object-storage"}:
        return S3ArtifactStore(
            os.environ.get("BRUNOST_JUDGE_ARTIFACT_BUCKET", ""),
            endpoint_url=os.environ.get("BRUNOST_JUDGE_ARTIFACT_ENDPOINT"),
            region_name=os.environ.get("BRUNOST_JUDGE_ARTIFACT_REGION"),
            access_key=os.environ.get("BRUNOST_JUDGE_ARTIFACT_ACCESS_KEY"),
            secret_key=os.environ.get("BRUNOST_JUDGE_ARTIFACT_SECRET_KEY"),
            max_bytes=max_bytes,
            prefix=os.environ.get("BRUNOST_JUDGE_ARTIFACT_PREFIX", "brunost/artifacts"),
        )
    return ArtifactStore(os.environ.get("BRUNOST_JUDGE_ARTIFACT_ROOT", "artifacts"), max_bytes=max_bytes)
