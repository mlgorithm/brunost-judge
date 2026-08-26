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

SUPPORTED_KINDS = frozenset({"agent", "game", "icpc", "interactive", "ioai", "model", "output-only"})
# These are the task kinds executed by the built-in scorer sandbox.
SCORER_KINDS = frozenset({"ioai", "model", "output-only"})
CLASSIC_KINDS = frozenset({"icpc"})
INTERACTIVE_KINDS = frozenset({"interactive"})
PLUGIN_KINDS = frozenset({"agent", "game"})
BUILTIN_KINDS = SCORER_KINDS | CLASSIC_KINDS | INTERACTIVE_KINDS | PLUGIN_KINDS
MANIFEST_VERSION = 1
CLASSIC_LANGUAGES = frozenset({"python", "py", "c", "cpp", "c++", "c++17", "gnu++17", "g++", "rust", "rs"})
MODEL_SUBMISSION_MODES = frozenset({"scorer", "python_code"})
_KIND_RE = re.compile(r"^\s*kind\s*:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", re.MULTILINE)


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


def _manifest_field(manifest: str, name: str) -> str | None:
    for match in _FIELD_RE.finditer(manifest):
        if match.group(1).lower() == name.lower():
            return match.group(2).strip().strip('"').strip("'")
    return None


def _validate_relative_path(value: str, label: str, errors: list[str]) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{label} must stay inside the task directory")


def _validate_positive_field(manifest: str, name: str, errors: list[str]) -> None:
    value = _manifest_field(manifest, name)
    if value is None:
        return
    try:
        if int(value) < 1:
            raise ValueError
    except ValueError:
        errors.append(f"{name} must be a positive integer")


def _manifest_bool(manifest: str, name: str, default: bool = False) -> bool:
    value = _manifest_field(manifest, name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _validate_model_manifest(root: Path, manifest: str, errors: list[str]) -> None:
    """Validate the explicit model-training contract when one is declared.

    Older model packages remain scorer-backed and are intentionally accepted
    without these fields. New Python training packages opt into the stricter
    lifecycle with ``submission_mode: python_code``.
    """

    submission_mode = (_manifest_field(manifest, "submission_mode") or "scorer").lower()
    if submission_mode not in MODEL_SUBMISSION_MODES:
        errors.append("model submission_mode must be scorer or python_code")
        return
    if submission_mode != "python_code":
        return

    if _manifest_field(manifest, "runner") not in {"model"}:
        errors.append("python_code model tasks must declare runner: model")
    if (_manifest_field(manifest, "official_split") or "private").lower() != "private":
        errors.append("model official_split must be private")
    if (_manifest_field(manifest, "submission_language") or "python").lower() not in {"python", "py"}:
        errors.append("python_code model submissions must use Python")

    entrypoint = _manifest_field(manifest, "submission_entrypoint") or "submission.py"
    _validate_relative_path(entrypoint, "submission_entrypoint", errors)
    output = _manifest_field(manifest, "prediction_output") or "predictions.csv"
    _validate_relative_path(output, "prediction_output", errors)
    for field in ("time_limit_ms", "training_time_limit_ms", "memory_limit_mb"):
        _validate_positive_field(manifest, field, errors)
    try:
        total = int(_manifest_field(manifest, "time_limit_ms") or "0")
        training = int(_manifest_field(manifest, "training_time_limit_ms") or str(total))
        if total and training > total:
            errors.append("training_time_limit_ms must not exceed time_limit_ms")
    except ValueError:
        pass

    for field in ("public_dataset", "hidden_dataset", "hidden_labels_dataset"):
        value = _manifest_field(manifest, field)
        if not value:
            errors.append(f"python_code model tasks must declare {field}")
            continue
        _validate_relative_path(value, field, errors)
        if not (root / value).is_file():
            errors.append(f"model task needs {value}")

    baseline_enabled = _manifest_bool(manifest, "baseline_enabled", bool(_manifest_field(manifest, "baseline_entrypoint")))
    if baseline_enabled:
        if (_manifest_field(manifest, "baseline_language") or "python").lower() not in {"python", "py"}:
            errors.append("python_code model baselines must use Python")
        baseline_entrypoint = _manifest_field(manifest, "baseline_entrypoint") or "private/baseline.py"
        _validate_relative_path(baseline_entrypoint, "baseline_entrypoint", errors)
        if not (root / baseline_entrypoint).is_file():
            errors.append(f"model task needs {baseline_entrypoint}")
    direction = (_manifest_field(manifest, "direction") or "maximize").lower()
    if direction not in {"maximize", "minimize"}:
        errors.append("model direction must be maximize or minimize")
    aggregation = (_manifest_field(manifest, "aggregation") or "mean").lower()
    if aggregation not in {"mean", "weighted_mean"}:
        errors.append("model aggregation must be mean or weighted_mean")


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

    manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else ""
    version = _manifest_field(manifest_text, "version")
    if version is None:
        errors.append("judge.yaml must declare version: 1")
    elif version != str(MANIFEST_VERSION):
        errors.append(f"unsupported judge.yaml version {version!r}; choose {MANIFEST_VERSION}")
    if kind in CLASSIC_KINDS:
        if _manifest_field(manifest_text, "runner") not in {None, "classic"}:
            errors.append("classic tasks must declare runner: classic")
        language = (_manifest_field(manifest_text, "language") or "").lower()
        if language not in CLASSIC_LANGUAGES:
            errors.append("classic tasks must declare language: python, c, cpp, or rust")
        for field in ("time_limit_ms", "memory_limit_mb", "output_limit_bytes"):
            _validate_positive_field(manifest_text, field, errors)
        answer_source = (_manifest_field(manifest_text, "answer_source") or "answer_key").lower()
        if answer_source not in {"answer_key", "reference"}:
            errors.append("answer_source must be answer_key or reference")
        if answer_source == "reference":
            reference_language = (_manifest_field(manifest_text, "reference_language") or language).lower()
            if reference_language not in CLASSIC_LANGUAGES:
                errors.append("reference_language must be python, c, cpp, or rust")
            reference_entrypoint = _manifest_field(manifest_text, "reference_entrypoint") or "private/reference.py"
            _validate_relative_path(reference_entrypoint, "reference_entrypoint", errors)
            if not (root / reference_entrypoint).is_file():
                errors.append(f"classic reference task needs {reference_entrypoint}")
        entrypoint = _manifest_field(manifest_text, "entrypoint")
        if entrypoint:
            _validate_relative_path(entrypoint, "entrypoint", errors)
        inputs = sorted((root / "tests").rglob("*.in")) if (root / "tests").is_dir() else []
        if not inputs:
            errors.append("classic tasks need tests/*.in files")
        for input_path in inputs:
            if answer_source == "answer_key" and not (input_path.with_suffix(".ans").is_file() or input_path.with_suffix(".out").is_file()):
                errors.append(f"test {input_path.relative_to(root)} is missing .ans or .out")
        scoring_mode = (_manifest_field(manifest_text, "scoring_mode") or "all_or_nothing").lower()
        if scoring_mode not in {"all_or_nothing", "percentage"}:
            errors.append("scoring_mode must be all_or_nothing or percentage")
        if (root / "subtasks.json").is_file():
            errors.append("subtasks.json is not supported; choose scoring_mode: percentage instead")
    elif kind in INTERACTIVE_KINDS:
        if _manifest_field(manifest_text, "runner") not in {None, "classic"}:
            errors.append("interactive tasks must declare runner: classic")
        language = (_manifest_field(manifest_text, "language") or "").lower()
        if language not in CLASSIC_LANGUAGES:
            errors.append("interactive tasks must declare language: python, c, cpp, or rust")
        for field in ("time_limit_ms", "memory_limit_mb", "output_limit_bytes"):
            _validate_positive_field(manifest_text, field, errors)
        interactor = _manifest_field(manifest_text, "interactor") or "interactor.py"
        _validate_relative_path(interactor, "interactor", errors)
        if not (root / interactor).is_file():
            errors.append("interactive tasks need interactor.py")
        if not (root / "tests").is_dir() or not list((root / "tests").rglob("*.in")):
            errors.append("interactive tasks need tests/*.in files")
    elif kind in PLUGIN_KINDS:
        if _manifest_field(manifest_text, "runner") not in {None, "python"}:
            errors.append("agent/game tasks must declare runner: python")
        entrypoint = _manifest_field(manifest_text, "entrypoint") or "runner.py"
        _validate_relative_path(entrypoint, "entrypoint", errors)
        if not (root / entrypoint).is_file():
            errors.append(f"{kind} tasks need {entrypoint}")
    elif kind == "model":
        _validate_model_manifest(root, manifest_text, errors)
        if not (root / "scorer" / "metrics.py").is_file() and not (root / "metrics.py").is_file():
            errors.append("missing scorer/metrics.py (or legacy root metrics.py)")
    elif not (root / "scorer" / "metrics.py").is_file() and not (root / "metrics.py").is_file():
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
        relative_parts = item.relative_to(root).parts
        if any(part == "__pycache__" or part.endswith(".pyc") for part in relative_parts):
            continue
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
    if normalized_kind in CLASSIC_KINDS:
        manifest = (
            f"""# Brunost Judge classic task manifest\nversion: 1\nkind: {normalized_kind}\nrunner: classic\nlanguage: python\ntime_limit_ms: 2000\nmemory_limit_mb: 512\noutput_limit_bytes: 1048576\nnetwork: disabled\n"""
        )
        (root / "judge.yaml").write_text(manifest, encoding="utf-8")
        (root / "tests" / "README.md").write_text(
            "Add matching .in/.ans files here, then choose scoring_mode: all_or_nothing or percentage.\n",
            encoding="utf-8",
        )
    else:
        runner = "\nrunner: python" if normalized_kind in PLUGIN_KINDS else ""
        (root / "judge.yaml").write_text(
            f"""# Brunost Judge task manifest\nversion: 1\nkind: {normalized_kind}{runner}\nruntime: python-3.13\nscoring: scorer.metrics:evaluate\nnetwork: disabled\n\n# Add resource_profile and feedback policy before publishing an official task.\n""",
            encoding="utf-8",
        )
        (root / "scorer" / "metrics.py").write_text(
            '''"""Task scorer. Hidden assets are available under assets_path/private."""\n\n\ndef evaluate(submission_path: str, assets_path: str) -> dict[str, float]:\n    # Replace this example with deterministic task scoring.\n    _ = submission_path, assets_path\n    return {"public": 0.0}\n''',
            encoding="utf-8",
        )
        if normalized_kind in PLUGIN_KINDS:
            (root / "runner.py").write_text(
                '''"""Reference agent/game runner. Return a canonical result mapping."""\n\n\ndef run(context: dict) -> dict:\n    _ = context\n    return {"status": "completed", "score": 0.0, "metrics": {}}\n''',
                encoding="utf-8",
            )
    (root / "public" / "README.md").write_text("Put contestant-visible data here.\n", encoding="utf-8")
    (root / "private" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "tests" / "test_task.py").write_text(
        """# Add deterministic scorer tests here.\n""",
        encoding="utf-8",
    )
    return root
