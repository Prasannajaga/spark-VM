from __future__ import annotations

import argparse
import os
import signal
import sys
from typing import NoReturn

from sparkVM.firecracker_vm import FirecrackerVM


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize and start a Firecracker microVM")
    parser.add_argument("--kernel-image", required=True, help="Absolute path to vmlinux")
    parser.add_argument("--rootfs", required=True, help="Absolute path to rootfs.ext4")
    parser.add_argument("--firecracker-bin", default="firecracker", help="Firecracker binary path")
    parser.add_argument("--socket-path", default="/tmp/firecracker.socket", help="API socket path")
    parser.add_argument("--vcpu-count", type=int, default=1, help="vCPU count")
    parser.add_argument("--mem-size-mib", type=int, default=256, help="Memory size in MiB")
    parser.add_argument("--smt", action="store_true", help="Enable SMT")
    parser.add_argument("--track-dirty-pages", action="store_true", help="Enable dirty page tracking")
    parser.add_argument("--boot-args", default="console=ttyS0 reboot=k panic=1 pci=off", help="Kernel boot args")
    parser.add_argument("--init-path", default="", help="Optional guest init path, e.g. /init")
    parser.add_argument("--init-script-file", default="", help="Host path of custom init script to copy into rootfs")
    parser.add_argument("--startup-timeout-sec", type=float, default=5.0, help="Timeout waiting for Firecracker API socket")
    parser.add_argument("--request-timeout-sec", type=float, default=3.0, help="Timeout for each Firecracker API request")
    parser.add_argument("--rootfs-read-only", action="store_true", help="Attach rootfs as read-only")
    parser.add_argument("--job-disk", default="", help="Optional path to job disk image")
    parser.add_argument("--job-drive-id", default="job", help="Drive ID for optional job disk")
    parser.add_argument("--job-read-only", action="store_true", help="Attach job disk as read-only")
    parser.add_argument("--host-dev-name", default="", help="Optional TAP device name for networking")
    parser.add_argument("--guest-mac", default="", help="Optional guest MAC address")
    parser.add_argument("--wait-timeout-sec", type=float, default=0.0, help="Wait for exit timeout; <=0 means wait forever")
    return parser.parse_args()


def fatal(msg: str) -> NoReturn:
    raise SystemExit(msg)


def main() -> None:
    args = parse_args()

    if bool(args.host_dev_name) != bool(args.guest_mac):
        fatal("Both --host-dev-name and --guest-mac must be provided together")
    if args.vcpu_count <= 0:
        fatal("--vcpu-count must be > 0")
    if args.mem_size_mib <= 0:
        fatal("--mem-size-mib must be > 0")
    if args.startup_timeout_sec <= 0:
        fatal("--startup-timeout-sec must be > 0")
    if args.request_timeout_sec <= 0:
        fatal("--request-timeout-sec must be > 0")
    if args.init_script_file and not os.path.exists(args.init_script_file):
        fatal(f"init script file does not exist: {args.init_script_file}")
    if args.init_script_file and args.rootfs_read_only:
        fatal("Cannot modify rootfs when --rootfs-read-only is set")

    vm = FirecrackerVM(
        firecracker_bin=args.firecracker_bin,
        socket_path=args.socket_path,
        kernel_image_path=args.kernel_image,
        boot_args=args.boot_args,
        startup_timeout=args.startup_timeout_sec,
        request_timeout=args.request_timeout_sec,
    )

    init_path = args.init_path
    if args.init_script_file and not init_path:
        init_path = "/init"

    if init_path:
        vm.set_init_path(init_path)

    if args.init_script_file:
        print(f"installing init script from {args.init_script_file} into rootfs at {init_path}")
        vm.install_init_script_from_file(args.rootfs, args.init_script_file, guest_path=init_path)

    def _signal_handler(signum: int, _frame: object) -> None:
        print(f"received signal {signum}, cleaning up vm")
        vm.cleanup()
        sys.exit(130)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        print("creating firecracker process")
        vm.create()

        print("configuring cpu/memory")
        vm.configure_cpu_memory(
            vcpu_count=args.vcpu_count,
            mem_size_mib=args.mem_size_mib,
            smt=args.smt,
            track_dirty_pages=args.track_dirty_pages,
        )

        print("attaching rootfs")
        vm.attach_rootfs(args.rootfs, read_only=args.rootfs_read_only)

        if args.job_disk:
            print("attaching job disk")
            vm.attach_job_disk(
                args.job_disk,
                drive_id=args.job_drive_id,
                read_only=args.job_read_only,
            )

        if args.host_dev_name:
            print("attaching network")
            vm.attach_network(
                host_dev_name=args.host_dev_name,
                guest_mac=args.guest_mac,
            )

        print("starting vm")
        vm.start()

        timeout = args.wait_timeout_sec if args.wait_timeout_sec > 0 else None
        print("waiting for vm exit")
        code = vm.wait_for_exit(timeout=timeout)
        print(f"vm exited with code={code}")
    finally:
        vm.cleanup()
        print("cleanup done")


if __name__ == "__main__":
    main()
