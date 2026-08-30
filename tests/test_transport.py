from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from brunost_judge.transport import ResponseTooLarge, SafeHttpTransport


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "/target")
        self.end_headers()

    def log_message(self, *_args):
        return


def test_transport_does_not_follow_redirects():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = SafeHttpTransport()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/start")
        with pytest.raises(urllib.error.HTTPError, match="redirects are disabled"):
            transport.open(request, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_transport_bounds_real_response_reads():
    class Response:
        def __init__(self):
            self.headers = {"Content-Length": "5"}

        def read(self, amount):
            assert amount == 4
            return b"12345"

    with pytest.raises(ResponseTooLarge):
        SafeHttpTransport(max_response_bytes=3, max_artifact_response_bytes=3).read_json(Response())


def test_transport_requires_a_complete_client_certificate_pair():
    with pytest.raises(ValueError, match="certificate and client key"):
        SafeHttpTransport(client_cert_file="client.pem")
