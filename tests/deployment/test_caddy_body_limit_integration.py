from __future__ import annotations

import http.client
import os
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RAW_BODY_LIMIT = 128 * 1024


class _BodySink(BaseHTTPRequestHandler):
    forwarded_for_values: list[str | None] = []

    def do_POST(self) -> None:  # noqa: N802
        self.forwarded_for_values.append(self.headers.get("X-Forwarded-For"))
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            _read_chunked(self)
        else:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _read_chunked(handler: _BodySink) -> None:
    while True:
        size_line = handler.rfile.readline().split(b";", 1)[0].strip()
        if not size_line:
            return
        size = int(size_line, 16)
        if size == 0:
            handler.rfile.readline()
            return
        handler.rfile.read(size)
        handler.rfile.read(2)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    body: bytes | list[bytes],
    *,
    chunked: bool,
    forwarded_for: str | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    connection = http.client.HTTPSConnection(
        "localhost",
        port,
        timeout=5,
        context=ssl._create_unverified_context(),
    )
    try:
        headers = {"Content-Type": "application/octet-stream"}
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
        connection.request(
            "POST",
            "/",
            body=body,
            headers=headers,
            encode_chunked=chunked,
        )
        response = connection.getresponse()
        response.read()
        return response.status, response.getheaders()
    finally:
        connection.close()


def test_caddy_rejects_content_length_and_chunked_bodies_over_128_kib(tmp_path: Path):
    caddy_binary = os.environ.get("AEMEATH_TEST_CADDY_BINARY")
    if not caddy_binary:
        pytest.skip("set AEMEATH_TEST_CADDY_BINARY for the pinned Caddy integration test")
    backend_port = _free_port()
    proxy_port = _free_port()
    backend = ThreadingHTTPServer(("127.0.0.1", backend_port), _BodySink)
    _BodySink.forwarded_for_values.clear()
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    environment = os.environ.copy()
    environment.update(
        {
            "AEMEATH_DOMAIN": f"https://localhost:{proxy_port}",
            "AEMEATH_API_UPSTREAM": f"127.0.0.1:{backend_port}",
            "XDG_DATA_HOME": str(tmp_path / "caddy-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "caddy-config"),
        }
    )
    process = subprocess.Popen(
        [caddy_binary, "run", "--config", str(ROOT / "deploy" / "Caddyfile")],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 8
        while True:
            try:
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=0.2):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("Caddy did not become ready")
                time.sleep(0.05)

        status, response_headers = _request(
            proxy_port,
            b"x" * RAW_BODY_LIMIT,
            chunked=False,
            forwarded_for="198.51.100.5, 203.0.113.9",
        )
        assert status == 204
        assert "alt-svc" not in {name.lower() for name, _value in response_headers}
        assert _request(
            proxy_port, b"x" * (RAW_BODY_LIMIT + 1), chunked=False
        )[0] == 413
        assert _request(proxy_port, [b"x" * RAW_BODY_LIMIT], chunked=True)[0] == 204
        assert _request(
            proxy_port, [b"x" * (RAW_BODY_LIMIT + 1)], chunked=True
        )[0] == 413
        assert _BodySink.forwarded_for_values[0] == "127.0.0.1"
    finally:
        process.terminate()
        process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        backend_thread.join(timeout=5)
