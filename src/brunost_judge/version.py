"""Release version exposed by every public Judge surface.

``pyproject.toml`` reads this attribute when building a distribution, so the
package metadata, SDK, HTTP API, and worker user agent cannot drift apart.
"""

__version__ = "1.3.1"
