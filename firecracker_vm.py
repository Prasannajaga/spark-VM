from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from typing import Any, BinaryIO, Mapping


class FirecrackerAPIError(RuntimeError):
    pass


DEFAULT_HELLO_INIT_SCRIPT = """#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
echo "hello world" > /dev/console
poweroff -f
"""


class FirecrackerVM:
    def __init__(
        self,
        *,
        firecracker_bin: str = "firecracker",
        socket_path: str = "/tmp/firecracker.socket",
        kernel_image_path: str,
        boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off",
        startup_timeout: float = 5.0,
        request_timeout: float = 3.0,
    ) -> None:
        self.firecracker_bin = firecracker_bin
        self.socket_path = socket_path
        self.kernel_image_path = kernel_image_path
        self.boot_args = boot_args
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_path = f"{self.socket_path}.log"
        self._log_file: BinaryIO | None = None

    def set_init_path(self, init_path: str) -> None:
        if not init_path.startswith("/"):
            raise ValueError("init_path must be an absolute guest path, e.g. /init")
        if " init=" in f" {self.boot_args} ":
            raise ValueError("boot_args already contains init=...")
        self.boot_args = f"{self.boot_args} init={init_path}"

    def install_init_script(
        self,
        rootfs_path: str,
        script_content: str,
        *,
        guest_path: str = "/init",
        debugfs_bin: str = "/usr/sbin/debugfs",
    ) -> None:
        if not guest_path.startswith("/"):
            raise ValueError("guest_path must be an absolute path, e.g. /init")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(script_content)
            tmp_path = tmp.name
        os.chmod(tmp_path, 0o755)
        try:
            self._debugfs(rootfs_path, f"rm {guest_path}", debugfs_bin=debugfs_bin, check=False)
            write_result = self._debugfs(
                rootfs_path,
                f"write {tmp_path} {guest_path}",
                debugfs_bin=debugfs_bin,
                check=False,
            )
            if write_result.returncode != 0:
                raise RuntimeError(
                    "failed to copy init script into rootfs:\n"
                    f"{write_result.stderr}\n{write_result.stdout}"
                )
            mode_result = self._debugfs(
                rootfs_path,
                f"sif {guest_path} mode 0100755",
                debugfs_bin=debugfs_bin,
                check=False,
            )
            if mode_result.returncode != 0:
                raise RuntimeError(
                    "failed to set init script executable mode:\n"
                    f"{mode_result.stderr}\n{mode_result.stdout}"
                )
        finally:
            os.unlink(tmp_path)

    def install_init_script_from_file(
        self,
        rootfs_path: str,
        host_script_path: str,
        *,
        guest_path: str = "/init",
        debugfs_bin: str = "/usr/sbin/debugfs",
    ) -> None:
        with open(host_script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.install_init_script(
            rootfs_path,
            content,
            guest_path=guest_path,
            debugfs_bin=debugfs_bin,
        )

    def install_hello_init_script(
        self,
        rootfs_path: str,
        *,
        guest_path: str = "/init",
        debugfs_bin: str = "/usr/sbin/debugfs",
    ) -> None:
        self.install_init_script(
            rootfs_path,
            DEFAULT_HELLO_INIT_SCRIPT,
            guest_path=guest_path,
            debugfs_bin=debugfs_bin,
        )

    def create(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("Firecracker process is already running")
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self._log_file = open(self._log_path, "wb")
        self._proc = subprocess.Popen(
            [self.firecracker_bin, "--api-sock", self.socket_path],
            stdin=subprocess.DEVNULL,
            stdout=self._log_file,
            stderr=self._log_file,
        )
        self._wait_for_socket()
        self._put(
            "/boot-source",
            {
                "kernel_image_path": self.kernel_image_path,
                "boot_args": self.boot_args,
            },
        )

    def configure_cpu_memory(
        self,
        *,
        vcpu_count: int = 1,
        mem_size_mib: int = 256,
        smt: bool = False,
        track_dirty_pages: bool = False,
    ) -> None:
        self._put(
            "/machine-config",
            {
                "vcpu_count": vcpu_count,
                "mem_size_mib": mem_size_mib,
                "smt": smt,
                "track_dirty_pages": track_dirty_pages,
            },
        )

    def attach_rootfs(self, path_on_host: str, *, read_only: bool = False) -> None:
        self._put(
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": path_on_host,
                "is_root_device": True,
                "is_read_only": read_only,
            },
        )

    def attach_job_disk(
        self,
        path_on_host: str,
        *,
        drive_id: str = "job",
        read_only: bool = False,
    ) -> None:
        self._put(
            f"/drives/{drive_id}",
            {
                "drive_id": drive_id,
                "path_on_host": path_on_host,
                "is_root_device": False,
                "is_read_only": read_only,
            },
        )

    def attach_network(
        self,
        *,
        iface_id: str = "eth0",
        host_dev_name: str,
        guest_mac: str,
    ) -> None:
        self._put(
            f"/network-interfaces/{iface_id}",
            {
                "iface_id": iface_id,
                "host_dev_name": host_dev_name,
                "guest_mac": guest_mac,
            },
        )

    def start(self) -> None:
        self._put("/actions", {"action_type": "InstanceStart"})

    def wait_for_exit(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise RuntimeError("Firecracker process is not running")
        return self._proc.wait(timeout=timeout)

    def kill(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=3)

    def cleanup(self) -> None:
        self.kill()
        self._proc = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

    def _wait_for_socket(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    "Firecracker exited before API socket became ready.\n"
                    f"{self._read_log_tail()}"
                )
            if os.path.exists(self.socket_path):
                try:
                    self._request("GET", "/")
                    return
                except OSError:
                    pass
                except FirecrackerAPIError:
                    return
            time.sleep(0.05)
        raise TimeoutError(
            f"Timed out waiting for Firecracker socket: {self.socket_path}\n"
            f"{self._read_log_tail()}"
        )

    def _put(self, path: str, payload: Mapping[str, Any]) -> bytes:
        return self._request("PUT", path, payload)

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> bytes:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8") + body
        response = bytearray()
        expected_total: int | None = None
        header_end: int | None = None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.request_timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(request)
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    if response:
                        break
                    raise
                if not chunk:
                    break
                response.extend(chunk)
                if header_end is None:
                    header_sep = bytes(response).find(b"\r\n\r\n")
                    if header_sep != -1:
                        header_end = header_sep + 4
                        headers_text = bytes(response[:header_sep]).decode("latin1", errors="replace")
                        content_length: int | None = None
                        for line in headers_text.split("\r\n"):
                            if line.lower().startswith("content-length:"):
                                raw = line.split(":", 1)[1].strip()
                                try:
                                    content_length = int(raw)
                                except ValueError:
                                    content_length = None
                                break
                        if content_length is not None:
                            expected_total = header_end + content_length
                if expected_total is not None and len(response) >= expected_total:
                    break
        finally:
            sock.close()
        status_line = bytes(response).split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        if not (status_line.startswith("HTTP/1.1 2") or status_line.startswith("HTTP/1.0 2")):
            raise FirecrackerAPIError(f"{status_line}\n{bytes(response).decode('utf-8', errors='replace')}")
        return bytes(response)

    def _read_log_tail(self, max_bytes: int = 8192) -> str:
        if self._log_file is not None:
            self._log_file.flush()
        if not os.path.exists(self._log_path):
            return f"(no firecracker log found at {self._log_path})"
        with open(self._log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start, os.SEEK_SET)
            data = f.read()
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return f"(firecracker log is empty at {self._log_path})"
        return f"firecracker log tail ({self._log_path}):\n{text}"

    @staticmethod
    def _debugfs(
        rootfs_path: str,
        command: str,
        *,
        debugfs_bin: str,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [debugfs_bin, "-w", "-R", command, rootfs_path],
            check=check,
            text=True,
            capture_output=True,
        )
