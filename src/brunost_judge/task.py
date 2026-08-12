"""Task-package validation and scaffolding.

The manifest intentionally starts as a small, human-readable YAML subset. The
full schema will become versioned before the standalone server is released;
validation here is deliberately dependency-free so task authors can install
the CLI without a large framework.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_KINDS = {"agent", "game", "icpc", "interactive", "ioai", "ioi", "model", "output-only"}
_KIND_RE = re.compile(r"^\s*kind\s*:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class TaskValidation:
    path: Path
    kind: str | None
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def _manifest_kind(manifest: str) -> str | None:
    match = _KIND_RE.search(manifest)
    return match.group(1).lower() if match else None


def validate_task(path: str | Path) -> TaskValidation:
    root = Path(path).expanduser().resolve()
    errors: list[str] = []
    manifest_path = root / "judge.yaml"
    kind: str | None = None

    if not root.is_dir():
        return TaskValidation(root, None, (f"task directory does not exist: {root}",))
    if not manifest_path.is_file():
        errors.append("missing judge.yaml")
    else:
        kind = _manifest_kind(manifest_path.read_text(encoding="utf-8"))
        if kind is None:
            errors.append("judge.yaml must declare a kind")
        elif kind not in SUPPORTED_KINDS:
            errors.append(f"unsupported task kind {kind!r}; choose one of {sorted(SUPPORTED_KINDS)}")

    if not (root / "scorer" / "metrics.py").is_file() and not (root / "metrics.py").is_file():
        errors.append("missing scorer/metrics.py (or legacy root metrics.py)")
    if not (root / "public").is_dir():
        errors.append("missing public/ task-data directory")
    if not (root / "private").is_dir():
        errors.append("missing private/ hidden-assets directory")

    return TaskValidation(root, kind, tuple(errors))


def task_digest(path: str | Path) -> str:
    """Return a stable SHA-256 digest for a task package.

    File names and bytes are both covered, in sorted order, so operators can
    pin exactly the package a worker is expected to execute.
    """
    root = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(root).as_posix().encode("utf-8")
        content = item.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def scaffold_task(path: str | Path, kind: str, *, force: bool = False) -> Path:
    root = Path(path).expanduser().resolve()
    normalized_kind = kind.lower()
    if normalized_kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported task kind {kind!r}; choose one of {sorted(SUPPORTED_KINDS)}")
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"refusing to overwrite non-empty directory: {root}")

    (root / "scorer").mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(parents=True, exist_ok=True)
    (root / "private").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "judge.yaml").write_text(
        f"""# Brunost Judge task manifest\nversion: 1\nkind: {normalized_kind}\nruntime: python-3.13\nscoring: scorer.metrics:evaluate\nnetwork: disabled\n\n# Add resource_profile and feedback policy before publishing an official task.\n""",
        encoding="utf-8",
    )
    (root / "scorer" / "metrics.py").write_text(
        '''"""Task scorer. Hidden assets are available under assets_path/private."""\n\n\ndef evaluate(submission_path: str, assets_path: str) -> dict[str, float]:\n    # Replace this example with deterministic task scoring.\n    _ = submission_path, assets_path\n    return {"public": 0.0}\n''',
        encoding="utf-8",
    )
    (root / "public" / "README.md").write_text("Put contestant-visible data here.\n", encoding="utf-8")
    (root / "private" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "tests" / "test_task.py").write_text(
        """# Add deterministic scorer tests here.\n""",
        encoding="utf-8",
    )
    return root
