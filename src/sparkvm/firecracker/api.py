"""Unix socket Firecracker API client."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sparkvm.errors import FirecrackerAPIError


@dataclass(frozen=True)
class FirecrackerAPIResponse:
    status_code: int
    body: str


class FirecrackerAPIClient:
    def __init__(self, socket_path: Path, timeout_sec: float = 10.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_sec = timeout_sec

    def put(self, path: str, payload: dict[str, Any]) -> FirecrackerAPIResponse:
        return self._request("PUT", path, payload)

    def get(self, path: str) -> FirecrackerAPIResponse:
        return self._request("GET", path, None)

    def attach_network(
        self,
        *,
        iface_id: str = "eth0",
        host_dev_name: str,
        guest_mac: str,
    ) -> FirecrackerAPIResponse:
        return self.put(
            f"/network-interfaces/{iface_id}",
            {
                "iface_id": iface_id,
                "host_dev_name": host_dev_name,
                "guest_mac": guest_mac,
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> FirecrackerAPIResponse:
        if not path.startswith("/"):
            raise ValueError("Firecracker API path must start with '/'.")

        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8") + body

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_sec)
        try:
            sock.connect(str(self.socket_path))
            sock.sendall(request)
            response = _recv_until(sock, b"\r\n\r\n")
            header_end = response.find(b"\r\n\r\n")
            if header_end == -1:
                raise FirecrackerAPIError(f"Malformed HTTP response from Firecracker API for {method} {path}.")

            header_bytes = response[:header_end]
            body_bytes = response[header_end + 4 :]
            status_line, headers = _parse_http_response_head(header_bytes)
            status_code = _parse_status_code(status_line, method=method, path=path)

            if status_code == 204:
                body_text = ""
            else:
                content_length = _parse_content_length(headers)
                if content_length is None:
                    raise FirecrackerAPIError(
                        f"Firecracker API malformed response for {method} {path}: missing Content-Length."
                    )
                if len(body_bytes) < content_length:
                    body_bytes.extend(_recv_exact(sock, content_length - len(body_bytes)))
                body_bytes = body_bytes[:content_length]
                body_text = body_bytes.decode("utf-8", errors="replace")

            if status_code < 200 or status_code >= 300:
                rendered = (
                    f"Firecracker API {method} {path} failed with {status_line}."
                    if not body_text
                    else f"Firecracker API {method} {path} failed with {status_line}: {body_text}"
                )
                raise FirecrackerAPIError(rendered)

            return FirecrackerAPIResponse(status_code=status_code, body=body_text)
        except socket.timeout as exc:
            raise FirecrackerAPIError(f"Firecracker API request timed out for {method} {path}") from exc
        except OSError as exc:
            raise FirecrackerAPIError(f"Firecracker API socket error for {method} {path}: {exc}") from exc
        finally:
            sock.close()


def _recv_until(sock: socket.socket, marker: bytes) -> bytearray:
    buf = bytearray()
    while marker not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
    return buf


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    remaining = size
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = sock.recv(min(65536, remaining))
        if not chunk:
            raise FirecrackerAPIError("Unexpected EOF while reading Firecracker API response body.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_http_response_head(header_bytes: bytes) -> tuple[str, dict[str, str]]:
    header_text = header_bytes.decode("utf-8", errors="replace")
    lines = header_text.split("\r\n")
    if not lines or not lines[0]:
        raise FirecrackerAPIError("Missing HTTP status line in Firecracker API response.")
    status_line = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return status_line, headers


def _parse_status_code(status_line: str, *, method: str, path: str) -> int:
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise FirecrackerAPIError(f"Invalid status line from Firecracker API for {method} {path}: {status_line}")
    return int(parts[1])


def _parse_content_length(headers: dict[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


__all__ = ["FirecrackerAPIClient", "FirecrackerAPIResponse"]
