"""Execute one phase of a v2 ML train/predict submission."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import sys
from pathlib import Path


def _load_module(entrypoint: str):
    path = Path(entrypoint).resolve()
    spec = importlib.util.spec_from_file_location(f"brunost_submission_{os.getpid()}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("submission module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("entrypoint")
    parser.add_argument("phase", choices=("train", "predict"))
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    expected = 2 if args.phase == "train" else 3
    if len(args.paths) != expected:
        raise RuntimeError(f"{args.phase} phase expects {expected} path arguments")
    module = _load_module(args.entrypoint)
    if args.phase == "train":
        function = getattr(module, "train", None)
        if not callable(function):
            raise RuntimeError("submission.py must define train(train_dataset, model_path)")
        function(*args.paths[:2])
    else:
        function = getattr(module, "predict", None)
        if not callable(function):
            raise RuntimeError("submission.py must define predict(model_path, test_dataset, predictions_path)")
        function(*args.paths)
    return 0


if __name__ == "__main__":
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - the harness reports a structured failure
        print(f"model phase failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
