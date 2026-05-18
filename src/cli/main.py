"""SparkVM command line interface."""

from __future__ import annotations

import argparse
import json
import sys

from cli.cleanup import run_cleanup_command, run_reset_command
from cli.runtimes import (
    run_dockify_command,
    run_runtimes_delete_command,
    run_runtimes_inspect_command,
    run_runtimes_list_command,
)
from sparkvm.errors import SparkVMError
from cli.setup import (
    doctor_status,
    format_doctor_report,
    get_sparkvm_paths,
    run_setup_command,
)
from sparkvm.workers import Workers


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparkvm", description="SparkVM setup and diagnostics")
    parser.add_argument(
        "--home-dir",
        default=None,
        help="Override SparkVM home directory (default: $SPARKVM_HOME if set, otherwise invoking user's ~/.sparkvm)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Show SparkVM host and asset diagnostics")

    setup_parser = subparsers.add_parser("setup", help="Install/verify managed SparkVM assets")
    setup_parser.add_argument("runtime", nargs="?", help="Deprecated setup target")
    setup_parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall managed assets even when they already exist",
    )
    setup_parser.add_argument(
        "--owner",
        default=None,
        help="User that should own files under SparkVM home after setup (useful with sudo).",
    )

    dockify_parser = subparsers.add_parser("dockify", help="Convert a Docker image into a SparkVM runtime ext4 image")
    dockify_parser.add_argument("docker_image", help="Docker image reference (e.g. python:3.12-slim)")
    dockify_parser.add_argument("--name", default=None, help="Optional runtime name override")
    dockify_parser.add_argument("--size-mb", type=int, default=2048, help="Runtime ext4 size in MiB (default: 2048)")
    dockify_parser.add_argument("--force", action="store_true", help="Overwrite existing runtime image if present")
    dockify_parser.add_argument("--pull", dest="pull", action="store_true", help="Pull image before conversion")
    dockify_parser.add_argument("--no-pull", dest="pull", action="store_false", help="Skip image pull")
    dockify_parser.set_defaults(pull=True)
    dockify_parser.add_argument(
        "--owner",
        default=None,
        help="User that should own generated runtime files (useful with sudo).",
    )

    runtimes_parser = subparsers.add_parser("runtimes", help="List, inspect, and delete runtime images")
    runtimes_subparsers = runtimes_parser.add_subparsers(dest="runtimes_command", required=True)
    runtimes_subparsers.add_parser("list", help="List available runtime images")

    runtimes_inspect = runtimes_subparsers.add_parser("inspect", help="Show runtime metadata JSON")
    runtimes_inspect.add_argument("runtime", help="Runtime name or docker image name")

    runtimes_delete = runtimes_subparsers.add_parser("delete", help="Delete runtime image and metadata")
    runtimes_delete.add_argument("runtime", help="Runtime name or docker image name")
    runtimes_delete.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup rollouts and/or preserved failed worker folders")
    cleanup_parser.add_argument("target", choices=["rollouts", "workers", "all"], help="Cleanup target")
    cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt before deleting files",
    )

    reset_parser = subparsers.add_parser("reset", help="Delete all files under SparkVM home directory")
    reset_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt before deleting files",
    )

    workers_parser = subparsers.add_parser("workers", help="Inspect and manage preserved failed worker attempts")
    workers_subparsers = workers_parser.add_subparsers(dest="workers_command", required=True)

    workers_subparsers.add_parser("list", help="List preserved workers")

    workers_view = workers_subparsers.add_parser("view", help="View worker details/log")
    workers_view.add_argument("vm_id", help="Worker vm id (e.g. vm-02e67edfc7a0)")
    workers_view.add_argument("--tail", type=int, default=None, help="Show only last N log lines")
    workers_view.add_argument("--failure", action="store_true", help="Print failure.json for the worker")
    workers_view.add_argument("--path", action="store_true", help="Print worker directory path")

    workers_delete = workers_subparsers.add_parser("delete", help="Delete one preserved worker")
    workers_delete.add_argument("vm_id", help="Worker vm id (e.g. vm-02e67edfc7a0)")
    workers_delete.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    return parser


def _run_doctor(home_dir: str | None) -> int:
    paths = get_sparkvm_paths(home_dir)
    status = doctor_status(paths)
    print(format_doctor_report(status))
    return 0


def _run_workers_list(home_dir: str | None) -> int:
    workers = Workers(home_dir=home_dir)
    items = workers.list()
    if not items:
        print("No preserved workers found.")
        return 0

    headers = ["VM ID", "Rollout ID", "Status", "Error Type", "Duration", "Created At"]
    rows: list[list[str]] = []
    for item in items:
        rows.append(
            [
                item.vm_id,
                item.rollout_id or "-",
                item.status,
                item.error_type or "-",
                str(item.duration_ms) if item.duration_ms is not None else "-",
                item.created_at or "-",
            ]
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))

    header_line = " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    sep_line = "-+-".join("-" * widths[i] for i in range(len(headers)))
    print(header_line)
    print(sep_line)
    for row in rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return 0


def _run_workers_view(
    home_dir: str | None,
    vm_id: str,
    *,
    tail: int | None,
    show_failure: bool,
    show_path: bool,
) -> int:
    workers = Workers(home_dir=home_dir)
    if show_path:
        print(workers.path(vm_id))
        return 0

    if show_failure:
        payload = workers.failure_json(vm_id)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(workers.log_text(vm_id, tail=tail))
    return 0


def _run_workers_delete(home_dir: str | None, vm_id: str, *, force: bool) -> int:
    if not force:
        response = input(f"Delete worker {vm_id}? [y/N] ").strip().lower()
        if response not in {"y", "yes"}:
            print("Aborted.")
            return 0

    workers = Workers(home_dir=home_dir)
    workers.delete_by_id(vm_id, force=force)
    print(f"Deleted worker: {vm_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            return _run_doctor(args.home_dir)

        if args.command == "setup":
            return run_setup_command(args.home_dir, args.runtime, args.force, owner=args.owner)

        if args.command == "dockify":
            return run_dockify_command(
                args.home_dir,
                args.docker_image,
                name=args.name,
                size_mb=args.size_mb,
                force=args.force,
                pull=args.pull,
                owner=args.owner,
            )

        if args.command == "runtimes":
            if args.runtimes_command == "list":
                return run_runtimes_list_command(args.home_dir)
            if args.runtimes_command == "inspect":
                return run_runtimes_inspect_command(args.home_dir, args.runtime)
            if args.runtimes_command == "delete":
                return run_runtimes_delete_command(args.home_dir, args.runtime, force=args.force)
            parser.error(f"Unknown runtimes command: {args.runtimes_command}")
            return 2

        if args.command == "cleanup":
            return run_cleanup_command(args.home_dir, args.target, args.force)

        if args.command == "reset":
            return run_reset_command(args.home_dir, args.force)

        if args.command == "workers":
            if args.workers_command == "list":
                return _run_workers_list(args.home_dir)
            if args.workers_command == "view":
                return _run_workers_view(
                    args.home_dir,
                    args.vm_id,
                    tail=args.tail,
                    show_failure=args.failure,
                    show_path=args.path,
                )
            if args.workers_command == "delete":
                return _run_workers_delete(args.home_dir, args.vm_id, force=args.force)
            parser.error(f"Unknown workers command: {args.workers_command}")
            return 2

        parser.error(f"Unknown command: {args.command}")
        return 2
    except SparkVMError as exc:
        print(f"sparkvm error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
