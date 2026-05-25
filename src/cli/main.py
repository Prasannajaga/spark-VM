"""SparkVM command line interface."""

from __future__ import annotations

import argparse
import json
import sys

from cli.cleanup import run_cleanup_command, run_reset_command
from sparkvm.errors import SparkVMError
from cli.setup import (
    doctor_status,
    format_doctor_report,
    get_sparkvm_paths,
    run_setup_command,
)
from sparkvm.rollouts import Rollouts
from sparkvm.workers import Workers


def parse_env_vars(pairs: list[str] | None) -> dict[str, str]:
    if not pairs:
        return {}
    env: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SparkVMError(f"Invalid --env value {pair!r}. Expected KEY=VALUE.")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise SparkVMError(f"Invalid --env value {pair!r}. KEY cannot be empty.")
        env[key] = value
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sparkvm", description="SparkVM setup and diagnostics")
    parser.add_argument(
        "--home-dir",
        default=None,
        help="Override SparkVM home directory (default: $SPARKVM_HOME if set, otherwise invoking user's ~/.sparkvm)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Show SparkVM host and asset diagnostics")

    setup_parser = subparsers.add_parser("setup", help="Install/verify managed SparkVM assets")
    setup_parser.add_argument("--force", action="store_true", help="Reinstall managed assets")
    setup_parser.add_argument("--owner", default=None, help="User that should own files under SparkVM home after setup")

    rollout_parser = subparsers.add_parser("rollout", help="Rollout operations")
    rollout_subparsers = rollout_parser.add_subparsers(dest="rollout_command", required=True)
    rollout_create = rollout_subparsers.add_parser("create", help="Create Dockerfile-backed rollout")
    rollout_create.add_argument("--name", required=True, help="Rollout name")
    rollout_create.add_argument("--runtime", default="Dockerfile", help="Runtime value (must be Dockerfile)")
    rollout_create.add_argument(
        "--dockerfile",
        default="Dockerfile",
        help="Dockerfile path (absolute path or relative to current working directory)",
    )
    rollout_create.add_argument(
        "--delete-on-success",
        action="store_true",
        help="Delete rollout artifacts after a passed run",
    )
    rollout_subparsers.add_parser("list", help="List rollouts")
    rollout_view = rollout_subparsers.add_parser("view", help="View one rollout by id")
    rollout_view.add_argument("rollout_id", help="Rollout id")

    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup rollouts and/or preserved failed worker folders")
    cleanup_parser.add_argument("target", choices=["rollouts", "workers", "all"], help="Cleanup target")
    cleanup_parser.add_argument("--force", action="store_true", help="Skip confirmation prompt before deleting files")

    reset_parser = subparsers.add_parser("reset", help="Delete all data inside SparkVM home directory")
    reset_parser.add_argument("--force", action="store_true", help="Skip confirmation prompt before deleting files")

    workers_parser = subparsers.add_parser("workers", help="Run and inspect workers")
    workers_subparsers = workers_parser.add_subparsers(dest="workers_command", required=True)

    workers_subparsers.add_parser("list", help="List preserved workers")
    workers_run = workers_subparsers.add_parser("run", help="Run a rollout id")
    workers_run.add_argument("rollout_id", help="Rollout id")
    workers_run.add_argument("--vcpu", type=int, default=2, help="vCPU count")
    workers_run.add_argument("--memory", default="2G", help="Memory (e.g. 2G)")
    workers_run.add_argument("--disk", default="4G", help="Execution disk (e.g. 4G)")
    workers_run.add_argument("--timeout", type=float, default=60.0, help="Timeout seconds")
    workers_run.add_argument("--network", action="store_true", help="Enable network")
    workers_run.add_argument(
        "--env",
        action="append",
        default=None,
        help="Environment variable KEY=VALUE (repeatable)",
    )

    workers_view = workers_subparsers.add_parser("view", help="View worker details/log")
    workers_view.add_argument("vm_id", help="Worker vm id (e.g. vm-02e67edfc7a0)")
    workers_view.add_argument("--tail", type=int, default=None, help="Show only last N log lines")
    workers_view.add_argument("--live", action="store_true", help="Stream firecracker.log updates live")
    workers_view.add_argument("--result", action="store_true", help="Print result.json for the worker")
    workers_view.add_argument("--failure", action="store_true", help="Print failure.json for the worker")
    workers_view.add_argument("--results", action="store_true", help="Print sanitized worker result logs")
    workers_view.add_argument("--path", action="store_true", help="Print worker directory path")

    worker_parser = subparsers.add_parser("worker", help="Internal worker execution")
    worker_subparsers = worker_parser.add_subparsers(dest="worker_command", required=True)
    worker_run = worker_subparsers.add_parser("run", help="Run one worker id")
    worker_run.add_argument("worker_id", help="Worker id")

    subparsers.add_parser("start", help="Start rollout scheduler loop")

    return parser


def run_doctor(home_dir: str | None) -> int:
    paths = get_sparkvm_paths(home_dir)
    status = doctor_status(paths)
    print(format_doctor_report(status))
    return 0


def run_rollout_create(
    home_dir: str | None,
    *,
    name: str,
    runtime: str,
    dockerfile: str,
    delete_on_success: bool,
) -> int:
    manager = Rollouts(home_dir=home_dir)
    rollout = manager.create(
        name=name,
        runtime=runtime,
        dockerfile=dockerfile,
        deleteOnSuccess=delete_on_success,
    )
    print(json.dumps(rollout.to_metadata_entry(), indent=2, sort_keys=True))
    return 0


def run_rollout_execute(
    home_dir: str | None,
    *,
    rollout_id: str,
    vcpu: int,
    memory: str,
    disk: str,
    timeout: float,
    network: bool,
    env_pairs: list[str] | None,
) -> int:
    if home_dir is not None:
        # CLI-level home override remains available via SPARKVM_HOME.
        import os

        os.environ["SPARKVM_HOME"] = home_dir
    env = parse_env_vars(env_pairs)
    from sparkvm.vm import SparkVM

    vm = SparkVM(vcpu=vcpu, memory=memory, disk=disk, timeout=timeout, network=network, env=env)
    result = vm.run(rollout_id)
    print(
        json.dumps(
            {
                "rollout_id": result.rollout_id,
                "vm_id": result.vm_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "passed": result.passed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def run_rollout_list(home_dir: str | None) -> int:
    manager = Rollouts(home_dir=home_dir)
    rollouts = manager.list()
    payload = [item.to_metadata_entry() for item in rollouts]
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_rollout_view(home_dir: str | None, rollout_id: str) -> int:
    manager = Rollouts(home_dir=home_dir)
    rollout = manager.get_by_id(rollout_id)
    print(json.dumps(rollout.to_metadata_entry(), indent=2, sort_keys=True))
    return 0


def run_workers_list(home_dir: str | None) -> int:
    workers = Workers(home_dir=home_dir)
    items = workers.list()
    if not items:
        print("No preserved workers found.")
        return 0

    headers = ["VM ID", "Rollout ID", "Status", "Exit Code", "Error Type", "Duration", "Created At"]
    rows: list[list[str]] = []
    for item in items:
        rows.append(
            [
                item.vm_id,
                item.rollout_id or "-",
                item.status,
                str(item.exit_code) if item.exit_code is not None else "-",
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


def run_workers_view(
    home_dir: str | None,
    vm_id: str,
    *,
    tail: int | None,
    live: bool,
    show_result: bool,
    show_failure: bool,
    show_results: bool,
    show_path: bool,
) -> int:
    workers = Workers(home_dir=home_dir)
    if live and (show_result or show_failure or show_results or show_path):
        raise SparkVMError("--live can only be used with default log view.")

    if show_path:
        print(workers.path(vm_id))
        return 0

    if show_result:
        payload = workers.result_json(vm_id)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if show_failure:
        payload = workers.failure_json(vm_id)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if show_results:
        text = workers.results_text(vm_id)
        if not text:
            print("No extracted result logs found. Check firecracker.log and failure.json.")
        else:
            print(text)
        return 0

    if live:
        try:
            for chunk in workers.stream_log(vm_id, tail=tail):
                print(chunk, end="", flush=True)
        except KeyboardInterrupt:
            return 0
        return 0

    print(workers.log_text(vm_id, tail=tail))
    return 0


def run_start_scheduler(home_dir: str | None) -> int:
    from sparkvm.scheduler import Scheduler

    scheduler = Scheduler(home_dir=home_dir)
    try:
        scheduler.start_loop()
    except KeyboardInterrupt:
        return 0
    return 0


def run_worker_execute(home_dir: str | None, worker_id: str) -> int:
    from sparkvm.worker_runner import WorkerRunner

    runner = WorkerRunner(worker_id, home_dir=home_dir)
    return runner.run()


def _extract_internal_worker_run(argv: list[str]) -> tuple[str | None, str | None]:
    if "__worker-run" not in argv:
        return (None, None)
    idx = argv.index("__worker-run")
    if idx + 1 >= len(argv):
        raise SparkVMError("Missing worker id for internal command __worker-run.")
    worker_id = argv[idx + 1]
    home_dir: str | None = None
    if "--home-dir" in argv:
        home_idx = argv.index("--home-dir")
        if home_idx + 1 < len(argv):
            home_dir = argv[home_idx + 1]
    return (home_dir, worker_id)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    internal_home_dir, internal_worker_id = _extract_internal_worker_run(raw_argv)
    if internal_worker_id is not None:
        if internal_home_dir is not None:
            import os

            os.environ["SPARKVM_HOME"] = internal_home_dir
        return run_worker_execute(internal_home_dir, internal_worker_id)

    # Normalize `sparkvm rollout <id>` -> `sparkvm rollout view <id>`.
    normalized_argv = list(raw_argv)
    if (
        len(normalized_argv) >= 2
        and normalized_argv[0] == "rollout"
        and not normalized_argv[1].startswith("-")
        and normalized_argv[1] not in {"create", "list", "view"}
    ):
        normalized_argv.insert(1, "view")

    parser = build_parser()
    args = parser.parse_args(normalized_argv)

    try:
        if args.home_dir is not None:
            import os

            os.environ["SPARKVM_HOME"] = args.home_dir

        if args.command == "doctor":
            return run_doctor(args.home_dir)

        if args.command == "setup":
            return run_setup_command(args.home_dir, args.force, owner=args.owner)

        if args.command == "rollout":
            if args.rollout_command == "create":
                return run_rollout_create(
                    args.home_dir,
                    name=args.name,
                    runtime=args.runtime,
                    dockerfile=args.dockerfile,
                    delete_on_success=args.delete_on_success,
                )
            if args.rollout_command == "list":
                return run_rollout_list(args.home_dir)
            if args.rollout_command == "view":
                return run_rollout_view(args.home_dir, args.rollout_id)
            parser.error(f"Unknown rollout command: {args.rollout_command}")
            return 2

        if args.command == "cleanup":
            return run_cleanup_command(args.home_dir, args.target, args.force)

        if args.command == "reset":
            return run_reset_command(args.home_dir, args.force)

        if args.command == "workers":
            if args.workers_command == "list":
                return run_workers_list(args.home_dir)
            if args.workers_command == "run":
                return run_rollout_execute(
                    args.home_dir,
                    rollout_id=args.rollout_id,
                    vcpu=args.vcpu,
                    memory=args.memory,
                    disk=args.disk,
                    timeout=args.timeout,
                    network=args.network,
                    env_pairs=args.env,
                )
            if args.workers_command == "view":
                return run_workers_view(
                    args.home_dir,
                    args.vm_id,
                    tail=args.tail,
                    live=args.live,
                    show_result=args.result,
                    show_failure=args.failure,
                    show_results=args.results,
                    show_path=args.path,
                )
            parser.error(f"Unknown workers command: {args.workers_command}")
            return 2

        if args.command == "start":
            return run_start_scheduler(args.home_dir)

        if args.command == "worker":
            if args.worker_command == "run":
                return run_worker_execute(args.home_dir, args.worker_id)
            parser.error(f"Unknown worker command: {args.worker_command}")
            return 2

        parser.error(f"Unknown command: {args.command}")
        return 2
    except SparkVMError as exc:
        print(f"sparkvm error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
