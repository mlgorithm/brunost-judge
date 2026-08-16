"""Brunost grader core — platform-independent scoring and classic judging.

No imports from the Brunost backend (`app.*`) are permitted here: this package is the
extraction seam (ADR-0010) and must stay self-contained.
"""

from grader.harness import normalize_result, run

__all__ = ["normalize_result", "run"]
