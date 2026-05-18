from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.firecracker import FirecrackerAPIClient
from sparkvm.errors import FirecrackerAPIError


class _FakeUnixSocket:
    def __init__(self, recv_items: list[bytes | Exception]) -> None:
        self.recv_items = list(recv_items)
        self.closed = False
        self.sent = b""
        self.timeout: float | None = None
        self.connected_to: str | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, path: str) -> None:
        self.connected_to = path

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _size: int) -> bytes:
        if not self.recv_items:
            return b""
        item = self.recv_items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class FirecrackerAPIClientTest(unittest.TestCase):
    def test_request_204_no_content_returns_without_waiting_for_eof(self) -> None:
        fake_sock = _FakeUnixSocket(
            [
                b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n",
                AssertionError("recv called unexpectedly after complete 204 response"),
            ]
        )
        client = FirecrackerAPIClient(Path("/tmp/fc.sock"), timeout_sec=10.0)

        with patch("sparkvm.firecracker.api.socket.socket", return_value=fake_sock):
            response = client.put("/boot-source", {"kernel_image_path": "/vmlinux", "boot_args": "console=ttyS0"})

        self.assertEqual(204, response.status_code)
        self.assertEqual("", response.body)
        self.assertTrue(fake_sock.closed)

    def test_request_200_reads_exact_content_length(self) -> None:
        fake_sock = _FakeUnixSocket(
            [
                b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhe",
                b"llo",
                AssertionError("recv called unexpectedly after full response body"),
            ]
        )
        client = FirecrackerAPIClient(Path("/tmp/fc.sock"), timeout_sec=10.0)

        with patch("sparkvm.firecracker.api.socket.socket", return_value=fake_sock):
            response = client.get("/machine-config")

        self.assertEqual(200, response.status_code)
        self.assertEqual("hello", response.body)
        self.assertTrue(fake_sock.closed)

    def test_request_non_2xx_raises_firecracker_api_error(self) -> None:
        fake_sock = _FakeUnixSocket(
            [
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 11\r\n\r\nbad request",
            ]
        )
        client = FirecrackerAPIClient(Path("/tmp/fc.sock"), timeout_sec=10.0)

        with patch("sparkvm.firecracker.api.socket.socket", return_value=fake_sock):
            with self.assertRaises(FirecrackerAPIError) as ctx:
                client.put("/boot-source", {"bad": "payload"})

        msg = str(ctx.exception)
        self.assertIn("PUT /boot-source", msg)
        self.assertIn("HTTP/1.1 400 Bad Request", msg)
        self.assertIn("bad request", msg)
        self.assertTrue(fake_sock.closed)

    def test_request_timeout_raises_firecracker_api_error(self) -> None:
        fake_sock = _FakeUnixSocket([socket.timeout("timed out")])
        client = FirecrackerAPIClient(Path("/tmp/fc.sock"), timeout_sec=10.0)

        with patch("sparkvm.firecracker.api.socket.socket", return_value=fake_sock):
            with self.assertRaises(FirecrackerAPIError) as ctx:
                client.get("/machine-config")

        self.assertIn("Firecracker API request timed out for GET /machine-config", str(ctx.exception))
        self.assertTrue(fake_sock.closed)


if __name__ == "__main__":
    unittest.main()
