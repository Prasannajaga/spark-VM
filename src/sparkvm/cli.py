"""SparkVM command line interface."""

from __future__ import annotations

import argparse
import sys

from .errors import SparkVMError
from .setup import (
    doctor_status,
    format_doctor_report,
    get_sparkvm_paths,
    run_setup,
    run_setup_python,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparkvm", description="SparkVM setup and diagnostics")
    parser.add_argument(
        "--home-dir",
        default=None,
        help="Override SparkVM home directory (default: $SPARKVM_HOME if set, otherwise ~/.sparkvm)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Show SparkVM host and asset diagnostics")

    setup_parser = subparsers.add_parser("setup", help="Install/verify managed SparkVM assets")
    setup_parser.add_argument("runtime", nargs="?", choices=["python"], help="Optional runtime setup target")
    setup_parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall managed assets even when they already exist",
    )

    return parser


def _run_doctor(home_dir: str | None) -> int:
    paths = get_sparkvm_paths(home_dir)
    status = doctor_status(paths)
    print(format_doctor_report(status))
    return 0


def _run_setup(home_dir: str | None, runtime: str | None, force: bool) -> int:
    paths = get_sparkvm_paths(home_dir)
    print(f"Using SparkVM home: {paths.home_dir}", flush=True)
    progress = lambda message: print(f"[setup] {message}", flush=True)

    if runtime == "python":
        print("Running base setup checks and managed asset install...", flush=True)
        run_setup_python(paths, force=force, progress=progress)
        print(f"SparkVM setup complete: python runtime image ready at {paths.python_rootfs}")
        return 0

    print("Running base setup checks and managed asset install...", flush=True)
    run_setup(paths, force=force, progress=progress)
    print("SparkVM setup complete.")
    print(f"Firecracker: {paths.firecracker_bin}")
    print(f"Kernel: {paths.kernel_image}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            return _run_doctor(args.home_dir)

        if args.command == "setup":
            return _run_setup(args.home_dir, args.runtime, args.force)

        parser.error(f"Unknown command: {args.command}")
        return 2
    except SparkVMError as exc:
        print(f"sparkvm error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
