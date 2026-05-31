import json
import os
import queue
import select
import socket
import ssl
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

import requests


DEFAULT_URL = "https://api.github.com/repos/python/cpython"
DEFAULT_MIN_REMAINING_BUDGET_SEC = 2.0
DEFAULT_RUN_DEFAULT_STACK = "0"
MAX_RESPONSE_PREVIEW_BYTES = 2048
RESPONSE_HEADERS_TO_LOG = {
    "content-length",
    "content-type",
    "date",
    "server",
    "x-github-request-id",
}


def request_with_default_stack(url: str, timeout: float) -> dict[str, object]:
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return {
            "status": "pass",
            "elapsed_ms": elapsed_ms(started),
            "http_status": response.status_code,
            "repository": data.get("full_name"),
            "stars": data.get("stargazers_count"),
        }
    except Exception as exc:
        return {
            "status": "fail",
            "elapsed_ms": elapsed_ms(started),
            "error": repr(exc),
        }


def run_probe_with_timeout(
    label: str,
    timeout: float,
    func: Callable[[], dict[str, object]],
) -> dict[str, object]:
    result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
    started = time.monotonic()

    def run() -> None:
        try:
            result_queue.put(("pass", func()))
        except Exception as exc:
            result_queue.put(("fail", exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    try:
        status, payload = result_queue.get(timeout=timeout)
    except queue.Empty:
        return {
            "status": "fail",
            "elapsed_ms": elapsed_ms(started),
            "error": f"{label} timed out after {timeout:.1f}s",
        }

    if status == "fail":
        return {
            "status": "fail",
            "elapsed_ms": elapsed_ms(started),
            "error": repr(payload),
        }

    return payload  # type: ignore[return-value]


def getaddrinfo_with_timeout(
    host: str,
    port: int,
    family: socket.AddressFamily,
    timeout: float,
) -> tuple[list[tuple] | None, dict[str, object] | None]:
    result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
    started = time.monotonic()

    def resolve() -> None:
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
            result_queue.put(("pass", infos))
        except Exception as exc:
            result_queue.put(("fail", exc))

    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()

    try:
        status, payload = result_queue.get(timeout=timeout)
    except queue.Empty:
        return None, {
            "status": "fail",
            "elapsed_ms": elapsed_ms(started),
            "error": f"getaddrinfo timed out after {timeout:.1f}s",
        }

    if status == "fail":
        return None, {
            "status": "fail",
            "elapsed_ms": elapsed_ms(started),
            "error": f"getaddrinfo failed: {payload!r}",
        }

    return payload, None  # type: ignore[return-value]


def unique_addresses(infos: list[tuple]) -> list[str]:
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def dns_family_result(
    host: str,
    port: int,
    family: socket.AddressFamily,
    timeout: float,
) -> dict[str, object]:
    infos, error = getaddrinfo_with_timeout(host, port, family, timeout)
    if error is not None:
        return error
    return {
        "status": "pass",
        "addresses": unique_addresses(infos or []),
    }


def dns_probe(url: str, timeout: float) -> dict[str, object]:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return {"status": "fail", "error": f"URL has no host: {url}"}

    port = parsed.port or 443
    return {
        "host": host,
        "af_unspec": dns_family_result(host, port, socket.AF_UNSPEC, timeout),
        "af_inet": dns_family_result(host, port, socket.AF_INET, timeout),
        "af_inet6": dns_family_result(host, port, socket.AF_INET6, timeout),
    }


def probe_tcp_connect_family(url: str, family: socket.AddressFamily, timeout: float) -> dict[str, object]:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return {"status": "fail", "error": f"URL has no host: {url}"}

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    started = time.monotonic()
    addresses: list[str] = []
    errors: list[str] = []

    infos, error = getaddrinfo_with_timeout(host, port, family, timeout)
    if error is not None:
        error["family"] = family.name
        return error

    deadline = started + timeout
    for _, socktype, proto, _, sockaddr in infos or []:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append(f"connect probe exceeded {timeout:.1f}s budget")
            break
        address = str(sockaddr[0])
        if address in addresses:
            continue
        addresses.append(address)

        with socket.socket(family, socktype, proto) as sock:
            sock.setblocking(False)
            connect_error = sock.connect_ex(sockaddr)
            if connect_error == 0:
                return {
                    "status": "pass",
                    "elapsed_ms": elapsed_ms(started),
                    "address": address,
                    "port": port,
                }

            _, writable, exceptional = select.select([], [sock], [sock], max(0.1, remaining))
            if not writable and not exceptional:
                errors.append(f"{address}:{port}: TCP connect timed out after {remaining:.1f}s")
                continue

            so_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if so_error == 0:
                return {
                    "status": "pass",
                    "elapsed_ms": elapsed_ms(started),
                    "address": address,
                    "port": port,
                }
            errors.append(f"{address}:{port}: TCP connect failed errno={so_error}")

    return {
        "status": "fail",
        "elapsed_ms": elapsed_ms(started),
        "addresses": addresses,
        "errors": errors,
    }


def probe_https_family(url: str, family: socket.AddressFamily, timeout: float) -> dict[str, object]:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return {"status": "fail", "error": f"URL has no host: {url}"}
    if parsed.scheme != "https":
        return {"status": "skip", "error": f"family probe only supports https URLs: {url}"}

    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    started = time.monotonic()
    addresses: list[str] = []
    errors: list[str] = []
    attempts: list[dict[str, object]] = []

    infos, error = getaddrinfo_with_timeout(host, port, family, timeout)
    if error is not None:
        error["family"] = family.name
        return error

    deadline = started + timeout
    for _, socktype, proto, _, sockaddr in infos or []:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append(f"probe exceeded {timeout:.1f}s budget")
            break
        address = str(sockaddr[0])
        if address in addresses:
            continue
        addresses.append(address)
        try:
            result = run_https_exchange(
                host=host,
                path=path,
                family=family,
                socktype=socktype,
                proto=proto,
                sockaddr=sockaddr,
                address=address,
                deadline=deadline,
                started=started,
            )
            if result["status"] == "pass":
                return result
            attempts.append(result)
            errors.append(f"{address}: {result.get('error', result)!r}")
        except Exception as exc:
            attempts.append({"status": "fail", "address": address, "error": repr(exc)})
            errors.append(f"{address}: {exc!r}")

    return {
        "status": "fail",
        "elapsed_ms": elapsed_ms(started),
        "addresses": addresses,
        "attempts": attempts,
        "errors": errors,
    }


def run_https_exchange(
    *,
    host: str,
    path: str,
    family: socket.AddressFamily,
    socktype: int,
    proto: int,
    sockaddr: tuple,
    address: str,
    deadline: float,
    started: float,
) -> dict[str, object]:
    timings: dict[str, int] = {}
    tcp_started = time.monotonic()
    with socket.socket(family, socktype, proto) as raw_sock:
        raw_sock.setblocking(False)
        if not wait_for_tcp_connect(raw_sock, sockaddr, deadline):
            return {
                "status": "fail",
                "elapsed_ms": elapsed_ms(started),
                "address": address,
                "stage": "tcp_connect",
                "timings_ms": timings,
                "error": "TCP connect timed out or failed",
            }
        timings["tcp_connect"] = elapsed_ms(tcp_started)

        context_started = time.monotonic()
        context = ssl.create_default_context()
        timings["ssl_context"] = elapsed_ms(context_started)

        tls_sock = context.wrap_socket(
            raw_sock,
            server_hostname=host,
            do_handshake_on_connect=False,
        )
        try:
            handshake_started = time.monotonic()
            if not wait_for_tls_handshake(tls_sock, deadline):
                return {
                    "status": "fail",
                    "elapsed_ms": elapsed_ms(started),
                    "address": address,
                    "stage": "tls_handshake",
                    "timings_ms": timings,
                    "error": "TLS handshake timed out",
                }
            timings["tls_handshake"] = elapsed_ms(handshake_started)

            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "User-Agent: sparkvm-network-probe\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            send_started = time.monotonic()
            if not wait_for_tls_send(tls_sock, request, deadline):
                return {
                    "status": "fail",
                    "elapsed_ms": elapsed_ms(started),
                    "address": address,
                    "stage": "tls_send",
                    "timings_ms": timings,
                    "error": "TLS request send timed out",
                }
            timings["tls_send"] = elapsed_ms(send_started)

            read_started = time.monotonic()
            response = wait_for_tls_recv(tls_sock, deadline)
            if response is None:
                return {
                    "status": "fail",
                    "elapsed_ms": elapsed_ms(started),
                    "address": address,
                    "stage": "tls_recv",
                    "timings_ms": timings,
                    "error": "TLS response read timed out",
                }
            timings["tls_recv"] = elapsed_ms(read_started)
            decoded = response.decode("iso-8859-1", errors="replace")
            status_line = decoded.splitlines()[0] if decoded.splitlines() else ""
            headers, body_preview = parse_http_response_preview(response)
            return {
                "status": "pass",
                "elapsed_ms": elapsed_ms(started),
                "address": address,
                "http_status_line": status_line,
                "response_headers": headers,
                "response_preview": body_preview,
                "timings_ms": timings,
            }
        finally:
            tls_sock.close()


def wait_for_tcp_connect(sock: socket.socket, sockaddr: tuple, deadline: float) -> bool:
    connect_error = sock.connect_ex(sockaddr)
    if connect_error == 0:
        return True
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _, writable, exceptional = select.select([], [sock], [sock], min(remaining, 0.5))
        if not writable and not exceptional:
            continue
        return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0


def wait_for_tls_handshake(tls_sock: ssl.SSLSocket, deadline: float) -> bool:
    while True:
        try:
            tls_sock.do_handshake()
            return True
        except ssl.SSLWantReadError:
            if not wait_for_socket(tls_sock, read=True, deadline=deadline):
                return False
        except ssl.SSLWantWriteError:
            if not wait_for_socket(tls_sock, read=False, deadline=deadline):
                return False


def wait_for_tls_send(tls_sock: ssl.SSLSocket, payload: bytes, deadline: float) -> bool:
    offset = 0
    while offset < len(payload):
        try:
            offset += tls_sock.send(payload[offset:])
        except ssl.SSLWantReadError:
            if not wait_for_socket(tls_sock, read=True, deadline=deadline):
                return False
        except ssl.SSLWantWriteError:
            if not wait_for_socket(tls_sock, read=False, deadline=deadline):
                return False
    return True


def wait_for_tls_recv(tls_sock: ssl.SSLSocket, deadline: float) -> bytes | None:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = tls_sock.recv(4096)
            if chunk:
                chunks.append(chunk)
                if sum(len(part) for part in chunks) >= MAX_RESPONSE_PREVIEW_BYTES:
                    return b"".join(chunks)[:MAX_RESPONSE_PREVIEW_BYTES]
            return b"".join(chunks)
        except ssl.SSLWantReadError:
            if not wait_for_socket(tls_sock, read=True, deadline=deadline):
                return None
        except ssl.SSLWantWriteError:
            if not wait_for_socket(tls_sock, read=False, deadline=deadline):
                return None


def wait_for_socket(sock: socket.socket, *, read: bool, deadline: float) -> bool:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if read:
            readable, _, exceptional = select.select([sock], [], [sock], min(remaining, 0.5))
            if readable:
                return True
        else:
            _, writable, exceptional = select.select([], [sock], [sock], min(remaining, 0.5))
            if writable:
                return True
        if exceptional:
            return False


def parse_http_response_preview(response: bytes) -> tuple[dict[str, str], str]:
    header_bytes, _, body = response.partition(b"\r\n\r\n")
    header_lines = header_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in RESPONSE_HEADERS_TO_LOG:
            headers[key] = value.strip()
    return headers, body.decode("utf-8", errors="replace")


def env_truthy(name: str, default: str = "0") -> bool:
    value = os.getenv(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def print_result(name: str, payload: dict[str, object]) -> None:
    print(f"{name}={json.dumps(payload, sort_keys=True)}", flush=True)


def print_line(line: str) -> None:
    print(line, flush=True)


def resolve_probe_timeout() -> float:
    explicit = os.getenv("NETWORK_APP_TIMEOUT", "").strip()
    if explicit:
        return max(0.5, float(explicit))

    run_timeout_raw = os.getenv("SPARKVM_RUN_TIMEOUT_SEC", "").strip()
    try:
        run_timeout = float(run_timeout_raw)
    except ValueError:
        run_timeout = 30.0

    # The app runs DNS, IPv4, and IPv6 probes. Keep enough budget
    # before the guest-side timeout command kills the process.
    return max(1.0, min(5.0, (run_timeout - DEFAULT_MIN_REMAINING_BUDGET_SEC) / 3.0))


def main() -> None:
    url = os.getenv("NETWORK_APP_URL", DEFAULT_URL).strip() or DEFAULT_URL
    timeout = resolve_probe_timeout()
    run_default_stack = env_truthy("NETWORK_APP_RUN_DEFAULT", DEFAULT_RUN_DEFAULT_STACK)

    print_result(
        "NETWORK_PROBE_BEGIN",
        {
            "url": url,
            "per_probe_timeout_sec": timeout,
            "run_default_stack": run_default_stack,
        },
    )

    dns_result = dns_probe(url, timeout)
    print_result("NETWORK_DNS_RESULT", dns_result)

    ipv4_tcp_result = run_probe_with_timeout(
        "IPv4 TCP connect probe",
        timeout + 0.5,
        lambda: probe_tcp_connect_family(url, socket.AF_INET, timeout),
    )
    print_result("NETWORK_IPV4_TCP_RESULT", ipv4_tcp_result)

    ipv4_result = run_probe_with_timeout(
        "IPv4 HTTPS probe",
        timeout + 0.5,
        lambda: probe_https_family(url, socket.AF_INET, timeout),
    )
    print_result("NETWORK_IPV4_RESULT", ipv4_result)

    ipv6_tcp_result = run_probe_with_timeout(
        "IPv6 TCP connect probe",
        timeout + 0.5,
        lambda: probe_tcp_connect_family(url, socket.AF_INET6, timeout),
    )
    print_result("NETWORK_IPV6_TCP_RESULT", ipv6_tcp_result)

    ipv6_result = run_probe_with_timeout(
        "IPv6 HTTPS probe",
        timeout + 0.5,
        lambda: probe_https_family(url, socket.AF_INET6, timeout),
    )
    print_result("NETWORK_IPV6_RESULT", ipv6_result)

    if run_default_stack:
        default_result = run_probe_with_timeout(
            "default requests probe",
            timeout + 0.5,
            lambda: request_with_default_stack(url, timeout),
        )
    else:
        default_result = {
            "status": "skip",
            "reason": "set NETWORK_APP_RUN_DEFAULT=1 to run the normal requests stack",
        }
    print_result("NETWORK_DEFAULT_RESULT", default_result)

    if (
        ipv4_result["status"] == "pass"
        or ipv6_result["status"] == "pass"
        or default_result["status"] == "pass"
    ):
        print_line("NETWORK_APP_RESULT=PASS")
        return

    print_line("NETWORK_APP_RESULT=FAIL")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
