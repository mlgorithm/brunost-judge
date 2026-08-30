"""Redirect-free, bounded HTTP transport used by the Judge SDK."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_RESPONSE_BYTES = 128 * 1024 * 1024
_ORIGINAL_URLOPEN = urllib.request.urlopen


class ResponseTooLarge(RuntimeError):
    """Raised when a Judge response exceeds the client safety bound."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward bearer-authenticated requests to another location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, code, "redirects are disabled", headers, fp)


def _ssl_context(
    ca_file: str | Path | None,
    client_cert_file: str | Path | None,
    client_key_file: str | Path | None,
) -> ssl.SSLContext:
    if bool(client_cert_file) != bool(client_key_file):
        raise ValueError("client certificate and client key must be configured together")
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    if client_cert_file and client_key_file:
        context.load_cert_chain(str(client_cert_file), str(client_key_file))
    return context


class SafeHttpTransport:
    """A small urllib wrapper with no redirects and bounded reads."""

    def __init__(
        self,
        *,
        ca_file: str | Path | None = None,
        client_cert_file: str | Path | None = None,
        client_key_file: str | Path | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_artifact_response_bytes: int = DEFAULT_MAX_ARTIFACT_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes <= 0 or max_artifact_response_bytes <= 0:
            raise ValueError("response size limits must be positive")
        if max_artifact_response_bytes < max_response_bytes:
            raise ValueError("artifact response limit must be at least the JSON response limit")
        self.max_response_bytes = int(max_response_bytes)
        self.max_artifact_response_bytes = int(max_artifact_response_bytes)
        context = _ssl_context(ca_file, client_cert_file, client_key_file)
        self._opener = urllib.request.build_opener(NoRedirectHandler, urllib.request.HTTPSHandler(context=context))

    def open(self, request: urllib.request.Request, *, timeout: float):
        # Preserve the conventional urllib monkeypatch seam used by SDK
        # consumers and tests. Production uses the private opener above.
        if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
            return urllib.request.urlopen(request, timeout=timeout)
        return self._opener.open(request, timeout=timeout)

    @staticmethod
    def read(response: Any, *, max_bytes: int) -> bytes:
        content_length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise ResponseTooLarge(f"HTTP response exceeds {max_bytes} bytes")
            except ValueError:
                pass
        if getattr(response, "headers", None) is None:
            # Keep compatibility with simple response doubles used by SDK
            # integrations; real HTTPResponse objects expose headers and use
            # the bounded read below.
            body = response.read()
        else:
            body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ResponseTooLarge(f"HTTP response exceeds {max_bytes} bytes")
        return body

    def read_json(self, response: Any) -> bytes:
        return self.read(response, max_bytes=self.max_response_bytes)

    def read_artifact(self, response: Any) -> bytes:
        return self.read(response, max_bytes=self.max_artifact_response_bytes)
