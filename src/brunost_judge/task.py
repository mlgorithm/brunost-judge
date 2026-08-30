"""Task-package validation and scaffolding.

The manifest intentionally starts as a small, human-readable YAML subset. The
full schema will become versioned before the standalone server is released;
validation here is deliberately dependency-free so task authors can install
the CLI without a large framework.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

SUPPORTED_KINDS = frozenset(
    {"agent", "coding", "game", "icpc", "interactive", "ioai", "model", "optimization", "output-only", "quiz"}
)
# These are the task kinds executed by the built-in scorer sandbox.
SCORER_KINDS = frozenset({"ioai", "output-only"})
MODEL_KINDS = frozenset({"model"})
OPTIMIZATION_KINDS = frozenset({"optimization"})
QUIZ_KINDS = frozenset({"quiz"})
# ``icpc`` is retained for existing packages. New integrations should use the
# task-family name rather than a contest-format label.
CLASSIC_KINDS = frozenset({"coding", "icpc"})
INTERACTIVE_KINDS = frozenset({"interactive"})
PLUGIN_KINDS = frozenset({"agent", "game"})
BUILTIN_KINDS = SCORER_KINDS | MODEL_KINDS | OPTIMIZATION_KINDS | QUIZ_KINDS | CLASSIC_KINDS | INTERACTIVE_KINDS | PLUGIN_KINDS
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
DEFAULT_CLASSIC_TIME_LIMIT_MS = 2_000
DEFAULT_CLASSIC_MEMORY_MB = 512
DEFAULT_CLASSIC_OUTPUT_LIMIT_BYTES = 1 << 20
MAX_CLASSIC_TIME_LIMIT_MS = 60_000
MAX_CLASSIC_MEMORY_MB = 4_096
MAX_CLASSIC_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
MAX_CLASSIC_TESTS = 200
MAX_CLASSIC_TEST_INPUT_BYTES = 32 * 1024 * 1024
MAX_CLASSIC_TOTAL_TEST_BYTES = 512 * 1024 * 1024
MAX_CLASSIC_CODE_BYTES = 1_000_000
MAX_CLASSIC_WALL_TIME_MS = 3_600_000
CLASSIC_COMPILE_BUDGET_MS = 30_000
CLASSIC_SETUP_BUDGET_MS = 5_000
MAX_QUIZ_QUESTIONS = 500
MAX_QUIZ_CHOICES = 100
MAX_QUIZ_ID_CHARS = 64
MAX_QUIZ_TEXT_CHARS = 20_000
MAX_QUIZ_ANSWER_CHARS = 4_096
MAX_QUIZ_ANSWERS_PER_TEXT = 100
MAX_QUIZ_POINTS = 1_000_000
MAX_QUIZ_KEY_BYTES = 4 * 1024 * 1024
MAX_QUIZ_SUBMISSION_BYTES = 1 * 1024 * 1024
QUIZ_TYPES = frozenset({"single_choice", "multiple_choice", "free_text"})
QUIZ_TEXT_NORMALIZATIONS = frozenset(
    {"exact", "trim", "casefold_trim", "collapse_whitespace", "casefold_collapse_whitespace"}
)
BROWSER_ONLY_RUNTIME_MARKERS = ("browser", "cheerpx", "pyodide", "wasm", "webassembly")
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


def is_browser_only_runtime(runtime: str) -> bool:
    """Return whether a runtime belongs to Premium's local browser Lab."""

    normalized = runtime.strip().lower()
    return any(marker in normalized for marker in BROWSER_ONLY_RUNTIME_MARKERS)


def _scheduling_settings(manifest: str, *, default_runtime: str | None = None) -> dict[str, Any]:
    """Return task-owned settings that affect registration or worker selection."""

    settings: dict[str, Any] = {}
    for name in ("runtime", "scoring", "network", "resource_class"):
        value = _manifest_field(manifest, name)
        if value:
            settings[name] = value
    if default_runtime and "runtime" not in settings:
        settings["runtime"] = default_runtime
    capabilities = list(_manifest_list(manifest, "required_capabilities"))
    if settings.get("runtime") == "python-3.13-ml-v1":
        # ML jobs must only land on workers that have the ML runtime image.
        # Older workers do not advertise this capability and therefore remain
        # eligible for classic Python tasks without becoming unsafe fallbacks.
        capabilities.append("runtime:python-3.13-ml-v1")
    if capabilities:
        settings["required_capabilities"] = list(dict.fromkeys(capabilities))
    return settings


def _validate_scheduling_labels(manifest: str, errors: list[str]) -> None:
    runtime = _manifest_field(manifest, "runtime")
    if runtime and is_browser_only_runtime(runtime):
        errors.append("browser-only runtimes must remain in Premium Lab and cannot be Judge task runtimes")
    resource_class = _manifest_field(manifest, "resource_class")
    if resource_class and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,49}", resource_class):
        errors.append("resource_class must contain only letters, numbers, '.', '_', ':', or '-'")
    capabilities = _manifest_list(manifest, "required_capabilities")
    if len(capabilities) > 32 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", item) for item in capabilities):
        errors.append("required_capabilities must contain at most 32 valid capability labels")


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

    _validate_scheduling_labels(manifest, errors)
    return _scheduling_settings(manifest)


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


def _manifest_positive_int(manifest: str, name: str, default: int, errors: list[str]) -> int | None:
    """Read a positive integer while retaining the validator's useful errors."""

    value = _manifest_field(manifest, name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        errors.append(f"{name} must be a positive integer")
        return None
    if parsed < 1:
        errors.append(f"{name} must be a positive integer")
        return None
    return parsed


def _is_private_path(value: str) -> bool:
    return Path(value).parts[:1] == ("private",)


def _validate_classic_tests(
    root: Path,
    *,
    answer_source: str,
    output_limit_bytes: int | None,
    errors: list[str],
) -> tuple[Path, ...]:
    """Validate answer pairing and bound the data a classic runner will read."""

    tests_root = root / "tests"
    inputs = tuple(sorted(tests_root.rglob("*.in"))) if tests_root.is_dir() else ()
    if not inputs:
        errors.append("classic tasks need tests/*.in files")
        return ()
    if len(inputs) > MAX_CLASSIC_TESTS:
        errors.append(f"classic tasks support at most {MAX_CLASSIC_TESTS} test inputs")

    total_bytes = 0
    expected_outputs: set[Path] = set()
    for input_path in inputs:
        relative = input_path.relative_to(root)
        try:
            input_size = input_path.stat().st_size
        except OSError as exc:
            errors.append(f"could not inspect test {relative}: {exc}")
            continue
        total_bytes += input_size
        if input_size > MAX_CLASSIC_TEST_INPUT_BYTES:
            errors.append(f"classic input {relative} exceeds {MAX_CLASSIC_TEST_INPUT_BYTES} bytes")

        answers = tuple(candidate for candidate in (input_path.with_suffix(".ans"), input_path.with_suffix(".out")) if candidate.is_file())
        expected_outputs.update((input_path.with_suffix(".ans"), input_path.with_suffix(".out")))
        if answer_source == "answer_key":
            if not answers:
                errors.append(f"test {relative} is missing .ans or .out")
                continue
            if len(answers) > 1:
                errors.append(f"test {relative} has both .ans and .out; provide exactly one")
                continue
            answer_size = answers[0].stat().st_size
            total_bytes += answer_size
            if output_limit_bytes is not None and answer_size > output_limit_bytes:
                errors.append(f"answer {answers[0].relative_to(root)} exceeds output_limit_bytes")
        elif answers:
            errors.append(f"reference task test {relative} must not include .ans or .out")

    for answer_path in sorted(
        item for suffix in ("*.ans", "*.out") for item in tests_root.rglob(suffix)
    ):
        if answer_path not in expected_outputs:
            errors.append(f"orphan answer file {answer_path.relative_to(root)} has no matching .in")
    if total_bytes > MAX_CLASSIC_TOTAL_TEST_BYTES:
        errors.append(f"classic tests exceed {MAX_CLASSIC_TOTAL_TEST_BYTES} bytes in total")
    return inputs


def _classic_settings(root: Path, manifest: str, errors: list[str]) -> dict[str, Any]:
    """Validate the deterministic coding execution envelope and settings."""

    settings = _scheduling_settings(manifest, default_runtime="python-3.13")
    network = _manifest_field(manifest, "network")
    if network and network.lower() != "disabled":
        errors.append("classic tasks must declare network: disabled when network is specified")
    settings["network"] = "disabled"
    _validate_scheduling_labels(manifest, errors)

    time_limit = _manifest_positive_int(manifest, "time_limit_ms", DEFAULT_CLASSIC_TIME_LIMIT_MS, errors)
    memory_limit = _manifest_positive_int(manifest, "memory_limit_mb", DEFAULT_CLASSIC_MEMORY_MB, errors)
    output_limit = _manifest_positive_int(manifest, "output_limit_bytes", DEFAULT_CLASSIC_OUTPUT_LIMIT_BYTES, errors)
    if time_limit is not None and not 100 <= time_limit <= MAX_CLASSIC_TIME_LIMIT_MS:
        errors.append(f"classic time_limit_ms must be between 100 and {MAX_CLASSIC_TIME_LIMIT_MS}")
    if memory_limit is not None and not 64 <= memory_limit <= MAX_CLASSIC_MEMORY_MB:
        errors.append(f"classic memory_limit_mb must be between 64 and {MAX_CLASSIC_MEMORY_MB}")
    if output_limit is not None and not 1_024 <= output_limit <= MAX_CLASSIC_OUTPUT_LIMIT_BYTES:
        errors.append(f"classic output_limit_bytes must be between 1024 and {MAX_CLASSIC_OUTPUT_LIMIT_BYTES}")

    answer_source = (_manifest_field(manifest, "answer_source") or "answer_key").lower()
    inputs = _validate_classic_tests(
        root,
        answer_source=answer_source,
        output_limit_bytes=output_limit,
        errors=errors,
    )
    if time_limit is not None and inputs:
        reference_multiplier = 2 if answer_source == "reference" else 1
        wall_time_ms = CLASSIC_SETUP_BUDGET_MS + CLASSIC_COMPILE_BUDGET_MS * reference_multiplier
        wall_time_ms += len(inputs) * time_limit * reference_multiplier
        if wall_time_ms > MAX_CLASSIC_WALL_TIME_MS:
            errors.append(f"classic evaluation budget exceeds {MAX_CLASSIC_WALL_TIME_MS} ms")
        else:
            settings["execution_timeout_seconds"] = max(1, (wall_time_ms + 999) // 1_000)
    if time_limit is not None:
        settings["time_limit_ms"] = time_limit
    if memory_limit is not None:
        settings["memory_limit_mb"] = memory_limit
    if output_limit is not None:
        settings["output_limit_bytes"] = output_limit
    settings["answer_source"] = answer_source
    settings["scoring_mode"] = (_manifest_field(manifest, "scoring_mode") or "all_or_nothing").lower()
    settings["evaluator"] = "grader.classic:run_classic"
    return settings


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


def _load_bounded_json(path: Path, label: str, max_bytes: int, errors: list[str]) -> Any | None:
    """Load task-authored JSON without accepting oversized or non-finite data."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        errors.append(f"could not inspect {label}: {exc}")
        return None
    if size > max_bytes:
        errors.append(f"{label} exceeds {max_bytes} bytes")
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            errors.append(f"{label} exceeds {max_bytes} bytes")
            return None
        return json.loads(
            raw.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=reject_duplicates
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        errors.append(f"{label} is not valid JSON: {exc}")
        return None


def _validate_quiz_manifest(root: Path, manifest: str, errors: list[str]) -> dict[str, Any]:
    """Validate the private answer-key contract for deterministic quiz tasks."""

    if _manifest_field(manifest, "runner") != "quiz":
        errors.append("quiz tasks must declare runner: quiz")

    scoring_mode = (_manifest_field(manifest, "scoring_mode") or "weighted").lower()
    if scoring_mode not in {"weighted", "all_or_nothing"}:
        errors.append("quiz scoring_mode must be weighted or all_or_nothing")
    text_normalization = (_manifest_field(manifest, "free_text_normalization") or "casefold_trim").lower()
    if text_normalization not in QUIZ_TEXT_NORMALIZATIONS:
        errors.append(
            "free_text_normalization must be exact, trim, casefold_trim, collapse_whitespace, "
            "or casefold_collapse_whitespace"
        )

    answer_key = _manifest_field(manifest, "answer_key") or "private/questions.json"
    _validate_relative_path(answer_key, "answer_key", errors)
    if not _is_private_path(answer_key):
        errors.append("answer_key must be stored under private/")
    answer_key_path = (root / answer_key).resolve()
    task_root = root.resolve()
    private_root = (root / "private").resolve()
    if answer_key_path == task_root or task_root not in answer_key_path.parents:
        errors.append("answer_key must stay inside the task directory")
        return _quiz_settings(manifest, scoring_mode, text_normalization)
    if private_root != answer_key_path and private_root not in answer_key_path.parents:
        errors.append("answer_key must stay under private/")
        return _quiz_settings(manifest, scoring_mode, text_normalization)
    if not answer_key_path.is_file():
        errors.append(f"quiz tasks need {answer_key}")
        return _quiz_settings(manifest, scoring_mode, text_normalization)

    payload = _load_bounded_json(answer_key_path, "quiz answer_key", MAX_QUIZ_KEY_BYTES, errors)
    if payload is None:
        return _quiz_settings(manifest, scoring_mode, text_normalization)
    if not isinstance(payload, dict) or set(payload) - {"questions", "title", "description"}:
        errors.append("quiz answer_key must be an object containing only questions, title, and description")
        return _quiz_settings(manifest, scoring_mode, text_normalization)
    questions = payload.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_QUIZ_QUESTIONS:
        errors.append(f"quiz answer_key questions must contain 1 to {MAX_QUIZ_QUESTIONS} items")
        return _quiz_settings(manifest, scoring_mode, text_normalization)
    if isinstance(payload.get("title"), str) and len(payload["title"]) > MAX_QUIZ_TEXT_CHARS:
        errors.append(f"quiz title exceeds {MAX_QUIZ_TEXT_CHARS} characters")
    if isinstance(payload.get("description"), str) and len(payload["description"]) > MAX_QUIZ_TEXT_CHARS:
        errors.append(f"quiz description exceeds {MAX_QUIZ_TEXT_CHARS} characters")

    ids: set[str] = set()
    total_points = 0.0
    allowed_question_fields = {"id", "type", "prompt", "choices", "points", "answer", "accepted_answers"}
    for index, question in enumerate(questions, start=1):
        prefix = f"quiz question {index}"
        if not isinstance(question, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(question) - allowed_question_fields:
            errors.append(f"{prefix} contains unsupported fields")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not re.fullmatch(
            rf"[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_QUIZ_ID_CHARS - 1}}}", question_id
        ):
            errors.append(f"{prefix} id must be a unique safe string of at most {MAX_QUIZ_ID_CHARS} characters")
        elif question_id in ids:
            errors.append(f"duplicate quiz question id {question_id!r}")
        else:
            ids.add(question_id)
        question_type = question.get("type")
        if question_type not in QUIZ_TYPES:
            errors.append(f"{prefix} type must be single_choice, multiple_choice, or free_text")
        if "prompt" in question and (not isinstance(question["prompt"], str) or len(question["prompt"]) > MAX_QUIZ_TEXT_CHARS):
            errors.append(f"{prefix} prompt must be text of at most {MAX_QUIZ_TEXT_CHARS} characters")
        points = question.get("points", 1)
        if isinstance(points, bool) or not isinstance(points, (int, float)) or not 0 < points <= MAX_QUIZ_POINTS:
            errors.append(f"{prefix} points must be a finite number in (0, {MAX_QUIZ_POINTS}]")
        else:
            total_points += float(points)

        choices = question.get("choices")
        choice_ids: set[str] = set()
        if question_type in {"single_choice", "multiple_choice"}:
            if not isinstance(choices, list) or not 2 <= len(choices) <= MAX_QUIZ_CHOICES:
                errors.append(f"{prefix} choices must contain 2 to {MAX_QUIZ_CHOICES} items")
            else:
                for choice_index, choice in enumerate(choices, start=1):
                    if not isinstance(choice, dict) or set(choice) - {"id", "text"}:
                        errors.append(f"{prefix} choice {choice_index} must contain only id and text")
                        continue
                    choice_id = choice.get("id")
                    if not isinstance(choice_id, str) or not re.fullmatch(
                        rf"[A-Za-z0-9][A-Za-z0-9_.:-]{{0,{MAX_QUIZ_ID_CHARS - 1}}}", choice_id
                    ):
                        errors.append(f"{prefix} choice ids must be safe strings")
                    elif choice_id in choice_ids:
                        errors.append(f"{prefix} contains duplicate choice id {choice_id!r}")
                    else:
                        choice_ids.add(choice_id)
                    if not isinstance(choice.get("text"), str) or len(choice["text"]) > MAX_QUIZ_TEXT_CHARS:
                        errors.append(f"{prefix} choice text must be at most {MAX_QUIZ_TEXT_CHARS} characters")
        elif choices is not None:
            errors.append(f"{prefix} free_text questions must not declare choices")

        if question_type == "single_choice":
            if "accepted_answers" in question:
                errors.append(f"{prefix} single_choice questions must use answer")
            answer = question.get("answer")
            if not isinstance(answer, str) or answer not in choice_ids:
                errors.append(f"{prefix} answer must name one of its choices")
        elif question_type == "multiple_choice":
            if "accepted_answers" in question:
                errors.append(f"{prefix} multiple_choice questions must use answer")
            answer = question.get("answer")
            if (
                not isinstance(answer, list)
                or not answer
                or len(answer) > MAX_QUIZ_CHOICES
                or any(not isinstance(item, str) or item not in choice_ids for item in answer)
                or len(set(answer)) != len(answer)
            ):
                errors.append(f"{prefix} answer must be a non-empty list of unique choice ids")
        elif question_type == "free_text":
            if "answer" in question and "accepted_answers" in question:
                errors.append(f"{prefix} free_text questions must use either answer or accepted_answers, not both")
            accepted = question.get("accepted_answers", question.get("answer"))
            if isinstance(accepted, str):
                accepted = [accepted]
            if (
                not isinstance(accepted, list)
                or not 1 <= len(accepted) <= MAX_QUIZ_ANSWERS_PER_TEXT
                or any(not isinstance(item, str) or len(item) > MAX_QUIZ_ANSWER_CHARS for item in accepted)
            ):
                errors.append(
                    f"{prefix} accepted_answers must contain 1 to {MAX_QUIZ_ANSWERS_PER_TEXT} short text answers"
                )
    if total_points > MAX_QUIZ_POINTS:
        errors.append(f"quiz points total exceeds {MAX_QUIZ_POINTS}")
    return _quiz_settings(manifest, scoring_mode, text_normalization)


def _quiz_settings(manifest: str, scoring_mode: str, text_normalization: str) -> dict[str, Any]:
    settings = _scheduling_settings(manifest, default_runtime="python-3.13")
    settings.update(
        {
            "network": "disabled",
            "scoring_mode": scoring_mode,
            "free_text_normalization": text_normalization,
            "evaluator": "grader.quiz:run_quiz",
        }
    )
    return settings


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
    elif kind in QUIZ_KINDS:
        settings = _validate_quiz_manifest(root, manifest_text, errors)
    elif kind in CLASSIC_KINDS:
        if _manifest_field(manifest_text, "runner") not in {None, "classic"}:
            errors.append("classic tasks must declare runner: classic")
        language = (_manifest_field(manifest_text, "language") or "").lower()
        if language not in CLASSIC_LANGUAGES:
            errors.append("classic tasks must declare language: python, c, cpp, or rust")
        answer_source = (_manifest_field(manifest_text, "answer_source") or "answer_key").lower()
        if answer_source not in {"answer_key", "reference"}:
            errors.append("answer_source must be answer_key or reference")
        if answer_source == "reference":
            reference_language = (_manifest_field(manifest_text, "reference_language") or language).lower()
            if reference_language not in CLASSIC_LANGUAGES:
                errors.append("reference_language must be python, c, cpp, or rust")
            reference_entrypoint = _manifest_field(manifest_text, "reference_entrypoint") or "private/reference.py"
            _validate_relative_path(reference_entrypoint, "reference_entrypoint", errors)
            if not _is_private_path(reference_entrypoint):
                errors.append("reference_entrypoint must be stored under private/")
            reference_path = root / reference_entrypoint
            if not reference_path.is_file():
                errors.append(f"classic reference task needs {reference_entrypoint}")
            elif reference_path.stat().st_size > MAX_CLASSIC_CODE_BYTES:
                errors.append(f"classic reference exceeds {MAX_CLASSIC_CODE_BYTES} bytes")
            elif reference_language in {"python", "py"}:
                _validate_python_functions(root, reference_entrypoint, "classic reference", (), errors)
        entrypoint = _manifest_field(manifest_text, "entrypoint")
        if entrypoint:
            _validate_relative_path(entrypoint, "entrypoint", errors)
        scoring_mode = (_manifest_field(manifest_text, "scoring_mode") or "all_or_nothing").lower()
        if scoring_mode not in {"all_or_nothing", "percentage"}:
            errors.append("scoring_mode must be all_or_nothing or percentage")
        if (root / "subtasks.json").is_file():
            errors.append("subtasks.json is not supported; choose scoring_mode: percentage instead")
        checker_path = root / "checker.py"
        if checker_path.exists() and not checker_path.is_file():
            errors.append("checker.py must be a regular file")
        elif checker_path.is_file():
            if checker_path.stat().st_size > MAX_CLASSIC_CODE_BYTES:
                errors.append(f"checker exceeds {MAX_CLASSIC_CODE_BYTES} bytes")
            else:
                _validate_python_functions(root, "checker.py", "checker", ("check",), errors)
        settings = _classic_settings(root, manifest_text, errors)
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
        # Model tasks must retain their ML runtime when registered through the
        # artifact API.  Without this, the server falls back to the classic
        # Python runtime and the worker cannot execute train/predict safely.
        settings = _scheduling_settings(manifest_text, default_runtime="python-3.13-ml-v1")
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
            f"""# Brunost Judge classic task manifest\nversion: 1\nkind: {normalized_kind}\nrunner: classic\nlanguage: python\nruntime: python-3.13\ntime_limit_ms: 2000\nmemory_limit_mb: 512\noutput_limit_bytes: 1048576\nnetwork: disabled\nscoring_mode: all_or_nothing\n"""
        )
        (root / "judge.yaml").write_text(manifest, encoding="utf-8")
        (root / "tests" / "README.md").write_text(
            "Add matching .in/.ans files here, then choose scoring_mode: all_or_nothing or percentage.\n",
            encoding="utf-8",
        )
    elif normalized_kind in QUIZ_KINDS:
        (root / "judge.yaml").write_text(
            """# Brunost Judge quiz task manifest
version: 1
kind: quiz
runner: quiz
answer_key: private/questions.json
scoring_mode: weighted
free_text_normalization: casefold_trim
network: disabled
""",
            encoding="utf-8",
        )
        (root / "private" / "questions.json").write_text(
            """{
  "questions": [
    {
      "id": "example",
      "type": "single_choice",
      "prompt": "Replace this example question.",
      "choices": [
        {"id": "a", "text": "First answer"},
        {"id": "b", "text": "Second answer"}
      ],
      "answer": "a",
      "points": 1
    }
  ]
}
""",
            encoding="utf-8",
        )
        (root / "public" / "README.md").write_text(
            "Publish question text and choices here if contestants should see them. Keep answers in private/questions.json.\n",
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
