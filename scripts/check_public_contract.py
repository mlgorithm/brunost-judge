"""Check version, schema, examples, and Markdown invariants for a release.

This intentionally uses only the standard library and the package's own task
validator so it can run in every development and CI environment.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from brunost_judge.task import SUPPORTED_KINDS, validate_task
from brunost_judge.version import __version__

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
YAML_FENCE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)
INLINE_YAML_COMMENT = re.compile(r"^\s*[A-Za-z0-9_-]+\s*:\s*[^#\n]+\s+#")


def fail(message: str) -> None:
    raise SystemExit(f"public contract check failed: {message}")


def check_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dynamic = project.get("project", {}).get("dynamic", [])
    if "version" not in dynamic:
        fail("pyproject.toml must derive the project version dynamically")
    configured = project.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {})
    if configured.get("attr") != "brunost_judge.version.__version__":
        fail("pyproject.toml must use brunost_judge.version.__version__")
    package_init = (ROOT / "src" / "brunost_judge" / "__init__.py").read_text(encoding="utf-8")
    if "from brunost_judge.version import __version__" not in package_init:
        fail("package __version__ must re-export the canonical version")
    if re.search(r"^__version__\s*=", package_init, re.MULTILINE):
        fail("package __init__ must not define a second version")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.+-][A-Za-z0-9.-]+)?", __version__):
        fail(f"version is not release-shaped: {__version__!r}")


def check_schemas() -> None:
    schema_dir = ROOT / "schemas"
    names = {"task-manifest.schema.json", "task-manifest-v1.schema.json", "task-manifest-v2.schema.json"}
    missing = names - {path.name for path in schema_dir.glob("*.json")}
    if missing:
        fail(f"task schema directory is missing: {', '.join(sorted(missing))}")
    for name in sorted(names):
        try:
            payload = json.loads((schema_dir / name).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"{name} is not valid JSON: {exc}")
        if payload.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            fail(f"{name} must declare Draft 2020-12")
        if not payload.get("$id"):
            fail(f"{name} must declare a stable $id")


def check_docs() -> None:
    documents = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for fence in YAML_FENCE.findall(text):
            for number, line in enumerate(fence.splitlines(), start=1):
                if INLINE_YAML_COMMENT.match(line):
                    fail(f"{document.relative_to(ROOT)} YAML snippet line {number} has an inline comment unsupported by the flat manifest parser")
        for target in MARKDOWN_LINK.findall(text):
            target = target.strip().strip("<>")
            if not target or target.startswith(("#", "mailto:")) or "://" in target:
                continue
            local_target = target.partition("#")[0]
            if local_target and not (document.parent / local_target).resolve().exists():
                fail(f"broken local Markdown link in {document.relative_to(ROOT)}: {target}")

    type_matrix = (ROOT / "docs" / "task-types.md").read_text(encoding="utf-8")
    missing = [kind for kind in sorted(SUPPORTED_KINDS) if f"`{kind}`" not in type_matrix]
    if missing:
        fail(f"task type matrix omits supported kinds: {', '.join(missing)}")


def check_examples() -> None:
    expected = ("deterministic-sum", "model-basics", "optimization-basics", "quiz-basics")
    for name in expected:
        validation = validate_task(ROOT / "examples" / name)
        if not validation.valid:
            fail(f"example {name} does not validate: {'; '.join(validation.errors)}")


def main() -> int:
    check_version()
    check_schemas()
    check_docs()
    check_examples()
    print(f"public contract check passed for Brunost Judge {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
