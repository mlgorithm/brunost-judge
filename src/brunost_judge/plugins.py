"""Public runner-plugin SDK.

The implementation is dependency-free and is also available inside the
evaluator image as :mod:`grader.plugins`.
"""

from grader.plugins import (
    PLUGIN_KINDS,
    PLUGIN_PROTOCOL_VERSION,
    RunnerContext,
    RunnerPlugin,
    RunnerRegistry,
    default_registry,
    normalize_plugin_result,
)

__all__ = [
    "PLUGIN_KINDS",
    "PLUGIN_PROTOCOL_VERSION",
    "RunnerContext",
    "RunnerPlugin",
    "RunnerRegistry",
    "default_registry",
    "normalize_plugin_result",
]
