"""Task-package validation and scaffolding.

The manifest intentionally starts as a small, human-readable YAML subset. The
full schema will become versioned before the standalone server is released;
validation here is deliberately dependency-free so task authors can install
the CLI without a large framework.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

SUPPORTED_KINDS = frozenset({"agent", "game", "icpc", "interactive", "ioai", "model", "optimization", "output-only"})
# These are the task kinds executed by the built-in scorer sandbox.
SCORER_KINDS = frozenset({"ioai", "output-only"})
MODEL_KINDS = frozenset({"model"})
OPTIMIZATION_KINDS = frozenset({"optimization"})
CLASSIC_KINDS = frozenset({"icpc"})
INTERACTIVE_KINDS = frozenset({"interactive"})
PLUGIN_KINDS = frozenset({"agent", "game"})
BUILTIN_KINDS = SCORER_KINDS | MODEL_KINDS | OPTIMIZATION_KINDS | CLASSIC_KINDS | INTERACTIVE_KINDS | PLUGIN_KINDS
MANIFEST_VERSION = 1
MODEL_MANIFEST_VERSION = 2
CLASSIC_LANGUAGES = frozenset({"python", "py", "c", "cpp", "c++", "c++17", "gnu++17", "g++", "rust", "rs"})
MAX_MODEL_ASSET_BYTES = 10_000_000
MAX_MODEL_CODE_BYTES = 1_000_000
MAX_MODEL_PREDICTION_BYTES = 64_000_000
MIN_MODEL_TIME_MS = 1_000
MAX_MODEL_TIME_MS = 3_600_000
MIN_MODEL_MEMORY_MB = 64
MAX_MODEL_MEMORY_MB = 16_384
_KIND_RE = re.compile(r"^\s*kind\s*:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class TaskValidation:
    path: Path
    kind: str | None
    errors: tuple[str, ...]
    settings: dict[str, Any] = dataclass_field(default_factory=dict)

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


def _manifest_list(manifest: str, name: str) -> tuple[str, ...]:
    """Read a small YAML scalar/list field without a YAML dependency."""

    value = _manifest_field(manifest, name)
    if not value:
        return ()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return tuple(item.strip().strip("\"'") for item in value.split(",") if item.strip())


def _scorer_settings(manifest: str) -> dict[str, Any]:
    """Return generic-scorer settings that affect registration or scheduling."""

    settings: dict[str, Any] = {}
    for name in ("runtime", "scoring", "network", "resource_class"):
        value = _manifest_field(manifest, name)
        if value:
            settings[name] = value
    capabilities = _manifest_list(manifest, "required_capabilities")
    if capabilities:
        settings["required_capabilities"] = capabilities
    return settings


def _validate_scorer_manifest(root: Path, manifest: str, errors: list[str]) -> dict[str, Any]:
    """Validate the executable and scheduling contract for IOAI/output tasks."""

    packaged = root / "scorer" / "metrics.py"
    legacy = root / "metrics.py"
    if packaged.is_file() and legacy.is_file():
        errors.append("provide either scorer/metrics.py or legacy metrics.py, not both")
    elif not packaged.is_file() and not legacy.is_file():
        errors.append("missing scorer/metrics.py (or legacy root metrics.py)")
    else:
        scorer_path = "scorer/metrics.py" if packaged.is_file() else "metrics.py"
        expected_entrypoint = "scorer.metrics:evaluate" if packaged.is_file() else "metrics:evaluate"
        declared_entrypoint = _manifest_field(manifest, "scoring")
        if declared_entrypoint and declared_entrypoint != expected_entrypoint:
            errors.append(f"scoring must be {expected_entrypoint}")
        _validate_python_functions(root, scorer_path, "scorer", ("evaluate",), errors)

    network = _manifest_field(manifest, "network")
    if network and network.lower() != "disabled":
        errors.append("generic scorer tasks must declare network: disabled")
    if _manifest_field(manifest, "feedback") is not None:
        errors.append("feedback is platform policy; do not declare it in a Judge task manifest")

    resource_class = _manifest_field(manifest, "resource_class")
    if resource_class and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,49}", resource_class):
        errors.append("resource_class must contain only letters, numbers, '.', '_', ':', or '-'")
    capabilities = _manifest_list(manifest, "required_capabilities")
    if len(capabilities) > 32 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", item) for item in capabilities):
        errors.append("required_capabilities must contain at most 32 valid capability labels")
    return _scorer_settings(manifest)


def _validate_relative_path(value: str, label: str, errors: list[str]) -> None:
    path = Path(value)
    if path.is_absolute() or "\\" in value or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        errors.append(f"{label} must stay inside the task directory")


def _validate_python_functions(root: Path, relative: str, label: str, names: tuple[str, ...], errors: list[str]) -> None:
    path = root / relative
    if not path.is_file():
        return
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        errors.append(f"{label} has invalid Python syntax: {exc}")
        return
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in names:
        if name not in defined:
            errors.append(f"{label} must define {name}()")


def _validate_model_asset(
    root: Path,
    value: str,
    field: str,
    errors: list[str],
    *,
    directory: str,
) -> None:
    """Validate a model asset's path, visibility boundary, and size."""

    _validate_relative_path(value, field, errors)
    asset_path = (root / value).resolve()
    task_root = root.resolve()
    if asset_path == task_root or task_root not in asset_path.parents:
        errors.append(f"{field} must stay inside the task directory")
        return
    if not asset_path.as_posix().startswith(f"{(task_root / directory).resolve().as_posix()}/"):
        errors.append(f"{field} must be stored under {directory}/")
    if not asset_path.is_file():
        errors.append(f"model task needs {field}: {value}")
    elif asset_path.stat().st_size > MAX_MODEL_ASSET_BYTES:
        errors.append(f"model asset {value} exceeds {MAX_MODEL_ASSET_BYTES} bytes")


def _validate_model_code(root: Path, relative: str, label: str, names: tuple[str, ...], errors: list[str]) -> None:
    path = (root / relative).resolve()
    task_root = root.resolve()
    if path == task_root or task_root not in path.parents:
        errors.append(f"{label} must stay inside the task directory")
        return
    if not path.is_file():
        errors.append(f"model task needs {relative}")
        return
    if path.stat().st_size > MAX_MODEL_CODE_BYTES:
        errors.append(f"model code {relative} exceeds {MAX_MODEL_CODE_BYTES} bytes")
        return
    _validate_python_functions(root, relative, label, names, errors)


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
    """Validate the strict v2 train/model/predict contract."""

    legacy_fields = {
        "public_dataset", "hidden_dataset", "hidden_labels_dataset", "submission_mode",
        "submission_language", "prediction_output", "prediction_max_bytes", "official_split",
        "scoring", "scoring_code", "metric", "direction", "aggregation",
    }
    for field in sorted(legacy_fields):
        if _manifest_field(manifest, field) is not None:
            errors.append(f"legacy model field {field} is not supported")
    if _manifest_field(manifest, "model_contract") != "train_predict_v2":
        errors.append("model tasks must declare model_contract: train_predict_v2")

    if _manifest_field(manifest, "runner") not in {"model"}:
        errors.append("model tasks must declare runner: model")
    if (_manifest_field(manifest, "runtime") or "") != "python-3.13-ml-v1":
        errors.append("model tasks must use runtime: python-3.13-ml-v1")
    if (_manifest_field(manifest, "evaluation") or "") != "evaluator:evaluate":
        errors.append("model tasks must declare evaluation: evaluator:evaluate")

    entrypoint = _manifest_field(manifest, "submission_entrypoint") or "submission.py"
    _validate_relative_path(entrypoint, "submission_entrypoint", errors)
    for field in ("time_limit_ms", "training_time_limit_ms", "prediction_time_limit_ms", "evaluator_time_limit_ms", "memory_limit_mb", "model_max_bytes"):
        _validate_positive_field(manifest, field, errors)
    model_limit = _manifest_field(manifest, "model_max_bytes")
    if model_limit is not None:
        try:
            if not 1_024 <= int(model_limit) <= MAX_MODEL_PREDICTION_BYTES:
                raise ValueError
        except ValueError:
            errors.append(f"model_max_bytes must be between 1024 and {MAX_MODEL_PREDICTION_BYTES}")
    try:
        total = int(_manifest_field(manifest, "time_limit_ms") or "0")
        training = int(_manifest_field(manifest, "training_time_limit_ms") or "0")
        prediction = int(_manifest_field(manifest, "prediction_time_limit_ms") or "0")
        evaluator = int(_manifest_field(manifest, "evaluator_time_limit_ms") or "0")
        memory = int(_manifest_field(manifest, "memory_limit_mb") or "0")
        if not MIN_MODEL_TIME_MS <= total <= 3_600_000:
            errors.append("time_limit_ms must be between 1000 and 3600000")
        for value, label in ((training, "training_time_limit_ms"), (prediction, "prediction_time_limit_ms"), (evaluator, "evaluator_time_limit_ms")):
            if not MIN_MODEL_TIME_MS <= value <= 3_600_000:
                errors.append(f"{label} must be between 1000 and 3600000")
        if not MIN_MODEL_MEMORY_MB <= memory <= MAX_MODEL_MEMORY_MB:
            errors.append(f"memory_limit_mb must be between {MIN_MODEL_MEMORY_MB} and {MAX_MODEL_MEMORY_MB}")
    except ValueError:
        pass

    for field in ("training_dataset", "private_test_dataset", "private_labels_dataset"):
        value = _manifest_field(manifest, field)
        if not value:
            errors.append(f"model tasks must declare {field}")
            continue
        directory = "public/datasets" if field == "training_dataset" else "private/datasets"
        _validate_model_asset(root, value, field, errors, directory=directory)

    public_test = _manifest_field(manifest, "public_test_dataset")
    public_labels = _manifest_field(manifest, "public_labels_dataset")
    if bool(public_test) != bool(public_labels):
        errors.append("public_test_dataset and public_labels_dataset must be declared together")
    for field in ("public_test_dataset", "public_labels_dataset"):
        value = _manifest_field(manifest, field)
        if value:
            _validate_model_asset(root, value, field, errors, directory="public/datasets")

    baseline_enabled = _manifest_bool(manifest, "baseline_enabled", bool(_manifest_field(manifest, "baseline_entrypoint")))
    if baseline_enabled:
        baseline_entrypoint = _manifest_field(manifest, "baseline_entrypoint") or "private/baseline.py"
        _validate_model_code(root, baseline_entrypoint, "baseline entrypoint", ("train", "predict"), errors)
    post_enabled = _manifest_bool(manifest, "post_competition_enabled")
    if post_enabled:
        for field in ("post_training_dataset", "post_test_dataset", "post_labels_dataset", "post_training_time_limit_ms", "post_prediction_time_limit_ms", "post_evaluator_time_limit_ms", "post_evaluator_entrypoint"):
            value = _manifest_field(manifest, field)
            if not value:
                errors.append(f"post-competition model tasks must declare {field}")
                continue
            if field.endswith("_dataset"):
                directory = {
                    "post_training_dataset": "private/post/training",
                    "post_test_dataset": "private/post/test",
                    "post_labels_dataset": "private/post/labels",
                }[field]
                _validate_model_asset(root, value, field, errors, directory=directory)
            elif field == "post_evaluator_entrypoint":
                _validate_model_code(root, value, "post evaluator", ("evaluate",), errors)
            else:
                _validate_positive_field(manifest, field, errors)

    try:
        total = int(_manifest_field(manifest, "time_limit_ms") or "0")
        training = int(_manifest_field(manifest, "training_time_limit_ms") or "0")
        prediction = int(_manifest_field(manifest, "prediction_time_limit_ms") or "0")
        evaluator = int(_manifest_field(manifest, "evaluator_time_limit_ms") or "0")
        solutions = 2 if baseline_enabled else 1
        live_splits = 2 if public_test else 1
        live_budget = solutions * (training + live_splits * prediction + live_splits * evaluator) + 5_000
        if total < live_budget:
            errors.append("time_limit_ms must cover all live model phases")
        if live_budget > MAX_MODEL_TIME_MS:
            errors.append("live model evaluation budget exceeds 3600000 ms")
        if post_enabled:
            post_training = int(_manifest_field(manifest, "post_training_time_limit_ms") or "0")
            post_prediction = int(_manifest_field(manifest, "post_prediction_time_limit_ms") or "0")
            post_evaluator = int(_manifest_field(manifest, "post_evaluator_time_limit_ms") or "0")
            post_budget = solutions * (post_training + post_prediction + post_evaluator) + 5_000
            if total < post_budget:
                errors.append("time_limit_ms must cover all post-competition model phases")
            if post_budget > MAX_MODEL_TIME_MS:
                errors.append("post-competition model evaluation budget exceeds 3600000 ms")
    except ValueError:
        pass


def _validate_optimization_manifest(root: Path, manifest: str, errors: list[str]) -> None:
    """Validate the bounded code-plus-objective optimization contract."""

    if _manifest_field(manifest, "runner") not in {"optimization"}:
        errors.append("optimization tasks must declare runner: optimization")
    language = (_manifest_field(manifest, "language") or "").lower()
    if language not in CLASSIC_LANGUAGES:
        errors.append("optimization tasks must declare language: python, c, cpp, or rust")
    for field in ("time_limit_ms", "memory_limit_mb", "output_limit_bytes"):
        _validate_positive_field(manifest, field, errors)
    try:
        time_limit = int(_manifest_field(manifest, "time_limit_ms") or "0")
        memory_limit = int(_manifest_field(manifest, "memory_limit_mb") or "0")
        output_limit = int(_manifest_field(manifest, "output_limit_bytes") or "0")
        if not 100 <= time_limit <= 15_000:
            errors.append("optimization time_limit_ms must be between 100 and 15000")
        if not 64 <= memory_limit <= 4_096:
            errors.append("optimization memory_limit_mb must be between 64 and 4096")
        if not 1_024 <= output_limit <= 64 * 1024 * 1024:
            errors.append("optimization output_limit_bytes must be between 1024 and 67108864")
    except ValueError:
        pass

    if (_manifest_field(manifest, "evaluation") or "") != "evaluator:evaluate":
        errors.append("optimization tasks must declare evaluation: evaluator:evaluate")
    direction = (_manifest_field(manifest, "objective_direction") or "").lower()
    if direction not in {"maximize", "minimize"}:
        errors.append("optimization tasks must declare objective_direction: maximize or minimize")
    score_mode = (_manifest_field(manifest, "score_mode") or "").lower()
    if score_mode not in {"checker_score", "baseline_ratio"}:
        errors.append("optimization tasks must declare score_mode: checker_score or baseline_ratio")
    aggregation = (_manifest_field(manifest, "aggregation") or "").lower()
    if aggregation not in {"mean", "minimum", "geometric_mean"}:
        errors.append("optimization tasks must declare aggregation: mean, minimum, or geometric_mean")

    evaluator_entrypoint = _manifest_field(manifest, "evaluator_entrypoint") or "private/evaluator.py"
    _validate_relative_path(evaluator_entrypoint, "evaluator_entrypoint", errors)
    evaluator_path = root / evaluator_entrypoint
    if not evaluator_path.is_file():
        errors.append(f"optimization tasks need {evaluator_entrypoint}")
    elif evaluator_path.stat().st_size > MAX_MODEL_CODE_BYTES:
        errors.append(f"optimization evaluator exceeds {MAX_MODEL_CODE_BYTES} bytes")
    else:
        _validate_python_functions(root, evaluator_entrypoint, "optimization evaluator", ("evaluate",), errors)

    inputs = sorted((root / "tests").rglob("*.in")) if (root / "tests").is_dir() else []
    if not inputs:
        errors.append("optimization tasks need tests/*.in files")
    if len(inputs) > 100:
        errors.append("optimization tasks support at most 100 test inputs")
    for input_path in inputs:
        if input_path.stat().st_size > 1_000_000:
            errors.append(f"optimization input {input_path.relative_to(root)} exceeds 1 MB")

    baseline_enabled = _manifest_bool(manifest, "baseline_enabled")
    baseline_entrypoint = _manifest_field(manifest, "baseline_entrypoint")
    if score_mode == "baseline_ratio" and not baseline_enabled:
        errors.append("baseline_ratio scoring requires baseline_enabled: true")
    if baseline_enabled:
        baseline_entrypoint = baseline_entrypoint or "private/baseline.py"
        _validate_relative_path(baseline_entrypoint, "baseline_entrypoint", errors)
        baseline_path = root / baseline_entrypoint
        if not baseline_path.is_file():
            errors.append(f"optimization tasks need {baseline_entrypoint}")
        elif baseline_path.stat().st_size > MAX_MODEL_CODE_BYTES:
            errors.append(f"optimization baseline exceeds {MAX_MODEL_CODE_BYTES} bytes")


def validate_task(path: str | Path) -> TaskValidation:
    root = Path(path).expanduser().resolve()
    errors: list[str] = []
    manifest_path = root / "judge.yaml"
    kind: str | None = None
    settings: dict[str, Any] = {}

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
    expected_version = MODEL_MANIFEST_VERSION if kind == "model" else MANIFEST_VERSION
    if version is None:
        errors.append(f"judge.yaml must declare version: {expected_version}")
    elif version != str(expected_version):
        errors.append(f"unsupported judge.yaml version {version!r}; choose {expected_version}")
    if kind in OPTIMIZATION_KINDS:
        _validate_optimization_manifest(root, manifest_text, errors)
    elif kind in CLASSIC_KINDS:
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
        if not (root / "evaluator.py").is_file():
            errors.append("missing evaluator.py")
        elif (root / "evaluator.py").stat().st_size > MAX_MODEL_CODE_BYTES:
            errors.append(f"model code evaluator.py exceeds {MAX_MODEL_CODE_BYTES} bytes")
        else:
            _validate_python_functions(root, "evaluator.py", "evaluator", ("evaluate",), errors)
    elif kind in SCORER_KINDS:
        settings = _validate_scorer_manifest(root, manifest_text, errors)
    if not (root / "public").is_dir():
        errors.append("missing public/ task-data directory")
    if not (root / "private").is_dir():
        errors.append("missing private/ hidden-assets directory")

    return TaskValidation(root, kind, tuple(errors), settings)


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
    elif normalized_kind in OPTIMIZATION_KINDS:
        (root / "public" / "instances").mkdir(parents=True, exist_ok=True)
        (root / "judge.yaml").write_text(
            """# Brunost Judge optimization task manifest
version: 1
kind: optimization
runner: optimization
language: python
time_limit_ms: 2000
memory_limit_mb: 512
output_limit_bytes: 1048576
network: disabled
evaluation: evaluator:evaluate
objective_direction: maximize
score_mode: baseline_ratio
aggregation: mean
evaluator_entrypoint: private/evaluator.py
baseline_enabled: true
baseline_entrypoint: private/baseline.py
""",
            encoding="utf-8",
        )
        (root / "private" / "evaluator.py").write_text(
            '''"""Trusted feasibility and objective evaluator."""


def evaluate(input_path: str, output_path: str) -> dict:
    capacity = int(open(input_path, encoding="utf-8").read().strip())
    value = int(open(output_path, encoding="utf-8").read().strip())
    return {"feasible": 0 <= value <= capacity, "objective": value}
''',
            encoding="utf-8",
        )
        (root / "private" / "baseline.py").write_text(
            '''"""Reference solution for the example optimization task."""


import sys


print(sys.stdin.read().strip())
''',
            encoding="utf-8",
        )
        (root / "tests" / "example.in").write_text("10\n", encoding="utf-8")
        (root / "public" / "instances" / "example.in").write_text("10\n", encoding="utf-8")
    elif normalized_kind == "model":
        (root / "public" / "datasets").mkdir(parents=True, exist_ok=True)
        (root / "private" / "datasets").mkdir(parents=True, exist_ok=True)
        (root / "public" / "datasets" / "training.csv").write_text("feature,label\n1,0\n", encoding="utf-8")
        (root / "private" / "datasets" / "test.csv").write_text("feature\n2\n", encoding="utf-8")
        (root / "private" / "datasets" / "labels.csv").write_text("label\n1\n", encoding="utf-8")
        (root / "judge.yaml").write_text(
            """# Brunost Judge model task manifest (train_predict_v2)
version: 2
kind: model
runner: model
model_contract: train_predict_v2
runtime: python-3.13-ml-v1
evaluation: evaluator:evaluate
network: disabled
time_limit_ms: 150000
training_time_limit_ms: 120000
prediction_time_limit_ms: 10000
evaluator_time_limit_ms: 10000
memory_limit_mb: 2048
model_max_bytes: 64000000
training_dataset: public/datasets/training.csv
private_test_dataset: private/datasets/test.csv
private_labels_dataset: private/datasets/labels.csv
submission_entrypoint: submission.py
baseline_enabled: false
post_competition_enabled: false
""",
            encoding="utf-8",
        )
        (root / "evaluator.py").write_text(
            '''"""Official split evaluator. It must return one numeric score."""


def evaluate(predictions_path: str, labels_path: str) -> float:
    _ = predictions_path, labels_path
    return 0.0
''',
            encoding="utf-8",
        )
        (root / "submission.example.py").write_text(
            '''"""Participant module contract."""


def train(train_dataset: str, model_path: str) -> None:
    with open(model_path, "wb") as model:
        model.write(b"replace with a trained model")


def predict(model_path: str, test_dataset: str, predictions_path: str) -> None:
    _ = model_path, test_dataset
    with open(predictions_path, "w", encoding="utf-8") as predictions:
        predictions.write("prediction\\n")
''',
            encoding="utf-8",
        )
    else:
        (root / "scorer").mkdir(parents=True, exist_ok=True)
        runner = "\nrunner: python" if normalized_kind in PLUGIN_KINDS else ""
        (root / "judge.yaml").write_text(
            f"""# Brunost Judge task manifest\nversion: 1\nkind: {normalized_kind}{runner}\nruntime: python-3.13\nscoring: scorer.metrics:evaluate\nnetwork: disabled\n\n# Add resource_class and required_capabilities before publishing an official task.\n""",
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
    if normalized_kind != "model":
        (root / "public" / "README.md").write_text("Put contestant-visible data here.\n", encoding="utf-8")
    (root / "private" / ".gitkeep").write_text("", encoding="utf-8")
    (root / "tests" / "test_task.py").write_text(
        """# Add deterministic scorer tests here.\n""",
        encoding="utf-8",
    )
    return root
