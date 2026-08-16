"""Test-only defaults for the intentionally unauthenticated local app fixture."""

import os

os.environ.setdefault("BRUNOST_JUDGE_ALLOW_ANONYMOUS_API", "true")
