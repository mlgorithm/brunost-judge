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
