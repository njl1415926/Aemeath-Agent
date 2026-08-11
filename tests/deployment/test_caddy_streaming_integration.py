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

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
RAW_BODY_LIMIT = 128 * 1024


class _StreamingBackend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    allow_terminal = threading.Event()
    terminal_written = threading.Event()
    disconnect_seen = threading.Event()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/stream":
            self._read_body()
            self._start_stream()
            self._write_chunk(b'event: response.delta\ndata: {"delta":"first"}\n\n')
            self.allow_terminal.wait(timeout=5)
            self._write_chunk(b": heartbeat\n\n")
            self._write_chunk(
                b'event: turn.completed\ndata: {"status":"completed"}\n\n'
            )
            self.terminal_written.set()
            self._finish_chunks()
            return
        if self.path == "/v1/disconnect":
            self._read_body()
            self._start_stream()
            self._write_chunk(b'event: response.delta\ndata: {"delta":"first"}\n\n')
            payload = b": keepalive " + (b"x" * 65536) + b"\n\n"
            for _index in range(100):
                try:
                    self._write_chunk(payload)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.disconnect_seen.set()
                    return
                time.sleep(0.02)
            return
        self._read_body()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _finish_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connection(port: int) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(
        "localhost",
        port,
        timeout=5,
        context=ssl._create_unverified_context(),
    )


def _post(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    connection = _connection(port)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _selected_alpn(port: int, protocol: str) -> str | None:
    context = ssl._create_unverified_context()
    context.set_alpn_protocols([protocol])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname="localhost") as tls:
            return tls.selected_alpn_protocol()


def _wait_until_ready(*, process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 20
    last_status: int | None = None
    last_error_type: str | None = None
    while True:
        try:
            last_status = _post(port, "/health", b"")[0]
            if last_status == 204:
                return
        except OSError as exc:
            last_error_type = type(exc).__name__
        if process.poll() is not None or time.monotonic() >= deadline:
            raise AssertionError(
                "Caddy did not become ready "
                f"(exit={process.poll()}, status={last_status}, error={last_error_type})"
            )
        time.sleep(0.05)


def test_caddy_sse_flush_heartbeat_disconnect_protocols_and_body_limit(
    tmp_path: Path,
) -> None:
    caddy_binary = os.environ.get("AEMEATH_TEST_CADDY_BINARY")
    if not caddy_binary:
        pytest.skip("set AEMEATH_TEST_CADDY_BINARY for the pinned Caddy integration test")
    backend_port = _free_port()
    backend = ThreadingHTTPServer(("127.0.0.1", backend_port), _StreamingBackend)
    proxy_port = _free_port()
    test_caddyfile = tmp_path / "Caddyfile"
    test_caddyfile.write_text(
        (ROOT / "deploy" / "Caddyfile")
        .read_text(encoding="utf-8")
        .replace(
            "{\n",
            "{\n\tadmin 127.0.0.1:0\n\tskip_install_trust\n",
            1,
        ),
        encoding="utf-8",
    )
    backend.daemon_threads = True
    _StreamingBackend.allow_terminal.clear()
    _StreamingBackend.terminal_written.clear()
    _StreamingBackend.disconnect_seen.clear()
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
        [caddy_binary, "run", "--config", str(test_caddyfile)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stream_connection: http.client.HTTPSConnection | None = None
    disconnect_connection: http.client.HTTPSConnection | None = None
    try:
        _wait_until_ready(process=process, port=proxy_port)
        assert _selected_alpn(proxy_port, "http/1.1") == "http/1.1"
        assert _selected_alpn(proxy_port, "h2") == "h2"
        assert _post(proxy_port, "/v1/body", b"x" * RAW_BODY_LIMIT)[0] == 204
        assert _post(proxy_port, "/v1/body", b"x" * (RAW_BODY_LIMIT + 1))[0] == 413

        stream_connection = _connection(proxy_port)
        stream_connection.request("POST", "/v1/stream", body=b"{}")
        stream_response = stream_connection.getresponse()
        first_frame = b"".join(stream_response.readline() for _index in range(3))
        assert stream_response.status == 200
        assert stream_response.getheader("Content-Type") == "text/event-stream"
        assert b"event: response.delta" in first_frame
        assert not _StreamingBackend.terminal_written.is_set()
        _StreamingBackend.allow_terminal.set()
        remaining = stream_response.read()
        assert b": heartbeat\n\n" in remaining
        assert b"event: turn.completed" in remaining
        stream_connection.close()
        stream_connection = None

        _StreamingBackend.allow_terminal.clear()
        _StreamingBackend.terminal_written.clear()
        with httpx.Client(http2=True, verify=False, timeout=5, trust_env=False) as client:
            with client.stream(
                "POST",
                f"https://localhost:{proxy_port}/v1/stream",
                content=b"{}",
            ) as h2_response:
                h2_lines = h2_response.iter_lines()
                first_h2_frame = [next(h2_lines), next(h2_lines), next(h2_lines)]
                assert h2_response.http_version == "HTTP/2"
                assert h2_response.headers["content-type"] == "text/event-stream"
                assert first_h2_frame[0] == "event: response.delta"
                assert not _StreamingBackend.terminal_written.is_set()
                _StreamingBackend.allow_terminal.set()
                remaining_h2 = "\n".join(h2_lines)
                assert ": heartbeat" in remaining_h2
                assert "event: turn.completed" in remaining_h2

        disconnect_connection = _connection(proxy_port)
        disconnect_connection.request("POST", "/v1/disconnect", body=b"{}")
        disconnect_response = disconnect_connection.getresponse()
        assert b"event: response.delta" in b"".join(
            disconnect_response.readline() for _index in range(3)
        )
        disconnect_connection.close()
        disconnect_connection = None
        assert _StreamingBackend.disconnect_seen.wait(timeout=5)
    finally:
        _StreamingBackend.allow_terminal.set()
        if stream_connection is not None:
            stream_connection.close()
        if disconnect_connection is not None:
            disconnect_connection.close()
        process.terminate()
        process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        backend_thread.join(timeout=5)
