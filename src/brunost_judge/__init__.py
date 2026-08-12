"""Public, platform-independent Brunost Judge API.

The compatibility ``grader`` package remains importable for task packages that
were authored before the standalone repository was extracted.
"""

from brunost_judge.task import task_digest
from grader.harness import normalize_result, run

__all__ = ["normalize_result", "run", "task_digest"]
__version__ = "0.3.0"
