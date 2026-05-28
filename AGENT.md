## SparkVM Core

- This repository is SparkVM.
- SparkVM is a Firecracker-based microVM runner designed for executing high-density agent rollouts.
- SparkVM only supports Dockerfile-backed rollouts.
- The Dockerfile is the source of truth for setup and execution.
- Rollout and worker states are backed by a local SQLite database for robust, ACID-compliant indexing.
- Machine resources and admission controls are dynamic, tracking available CPU, memory, and disk using policies.
- The project core must not be changed.

## Core Flow

- Create a rollout from a Dockerfile runtime.
- Build a Docker image from the Dockerfile.
- Convert the Docker image filesystem into an ext4 image.
- Store the ext4 image as the rollout artifact.
- Persist rollout metadata in the SQLite database and create a local `rollout.json` in the rollout directory.
- Create a SparkVM instance with VM resource configuration.
- Run the rollout inside Firecracker using `rollout.id` (either directly or via the background scheduler queue).
- Attach dynamic network TAP devices and inject per-worker network environment configs (when network is enabled).
- Extract results (stdouts, stderrs, exit codes, OOM checks) after the VM exits.
- Delete successful workers (including rootfs/execution disks) while preserving failed and timeout workers for diagnostics.

## Canonical Python API

The 4 core public exports at the package root level of `sparkvm` are `Rollouts`, `SparkVM`, `SparkScheduler`, and `MachineConfig`.

- **Rollout Management (SQLite Backed)**:

  ```python
  from sparkvm import Rollouts

  rollouts = Rollouts()
  rollout = rollouts.create(
      name="my-agent",
      runtime="Dockerfile",
      deleteOnSuccess=False,
      dockerfile="/abs/path/simplegithub.Dockerfile",
  )
  ```

- **One-Shot VM Execution**:

  ```python
  from sparkvm import SparkVM

  vm = SparkVM(
      vcpu=2,
      memory="2G",
      disk="4G",
      timeout=60.0,
      network=True,
      env=runtime_env,
  )
  result = vm.run(rollout.id)
  print(result.status, result.exit_code, result.passed)
  ```

- **Scheduler-Managed Queue Execution**:

  ```python
  from sparkvm import SparkScheduler, MachineConfig

  # Configure machine resource policy constraints
  MachineConfig.set_policy(poll_interval=2.0, max_concurrent_vms=10)

  # Process the execution queue
  scheduler = SparkScheduler()
  summary = scheduler.tick()  # Executes one scheduling cycle
  print(summary["tick_id"], summary["spawned"])
  ```

## SparkVM Core Rules

### 1. Runtime & API Boundaries

- **Strict Dockerfile Runtime**: The guest execution setup and instructions must always be defined entirely within the Dockerfile. Do not support any other runtime modes, and do not split logic between host commands and the Dockerfile.
- **Canonical API Access**: The `Rollouts` and `SparkVM` classes must only be invoked via their canonical interfaces. No other parameters (such as `command`, `script`, `files`, `repo`, `source`, `path`, `mode`, `image_name`, `setup_cmd`, or `run_cmd`) are allowed.
- **Constructor Configuration**: SparkVM must be configured via constructor arguments only (`vcpu`, `memory`, `disk`, `timeout`, `network`, `env`). These arguments must be strictly validated (e.g. positive integer cores, valid size strings like `"2G"`, positive timeout).

### 2. Rollout Build Rules

- **Build-Time Isolation**: Rollout creation (`Rollouts.create()`) is strictly a build-time operation. It must validate the Dockerfile, build the image, export its filesystem to an ext4 disk, inject the `/init` harness, and persist the rollout metadata.
- **No Side Effects**: Rollout creation must NEVER start a Firecracker daemon, provision networks, execute guest code, or instantiate worker resources.
- **Image Reuse**: When creating a rollout, check for and reuse an existing ext4 rollout image under the same name to avoid redundant builds.
- **Artifact Storage**: Store the finalized static ext4 rootfs image under `~/.sparkvm/images/` and record the rollout in both `rollout.json` and the SQLite database.

### 3. VM Execution & Workers

- **Execution-Time Isolation**: VM runs (`vm.run()`) must accept only `rollout.id` and launch from the static rollout image without rebuilds or mutations to the original image.
- **Unique Workers**: Every execution must run inside a private worker directory under `~/.sparkvm/workers/<worker_id>/` containing a writable rootfs copy, a writable execution disk, sockets, logs, and state files (`worker.json`, `result.json` / `failure.json`).
- **Worker Cleanups**: Successful worker directories must be completely deleted from the host immediately after a passed run.
- **Failure Diagnostics**: Failed and timed-out workers must be preserved in their entirety to allow post-mortem debugging.

### 4. Database & State Management

- **Central SQLite Index**: A local SQLite database under `~/.sparkvm/` serves as the primary operational state registry. It tracks all tables: `rollouts`, `workers`, `runtime_images`, `reservations`, `machine_policy`, and `events`.
- **Atomic Operations**: All database writes and status transitions must be fully atomic and ACID-compliant. Partially written or corrupted entries are strictly forbidden.
- **File Sync**: Maintain consistency by synchronizing database records with the local `rollout.json` and `worker.json` filesystem files.

### 5. Network & Disk Controls

- **Isolated Networks**: If network support is disabled, no TAP devices, NAT rules, or Firecracker network cards may be configured. If enabled, assign a unique TAP network device per worker using isolated subnets and route NAT cleanly, releasing all resources upon completion. Network logic is centralized strictly inside `src/sparkvm/machine/network.py`.
- **Disk Safe Handling**: Mount, loop device setup, sparse space allocations, and unmount steps must live strictly in `src/sparkvm/machine/disk.py`. Never mutate the reference rollout ext4 image during runtime execution.

### 6. Results, Diagnostics & Cleanup

- **Strict Success Conditions**: A run is only considered `passed` if the Firecracker VM exited normally, the guest process succeeded with exit code `0`, and guest results (stdout, stderr, exit code, status) were successfully extracted from the guest's `/job/results/` directory.
- **Result Failure Behavior**: If result extraction fails, the worker is marked `failed` and preserved.
- **Delete on Success Policy**: The `deleteOnSuccess` option, when enabled, removes the primary rollout rootfs images only after a successfully completed pass.
- **Timeouts Handling**: In the event of a timeout, immediately terminate the Firecracker subprocess, flag the status as `timeout`, write `failure.json`, and preserve the worker directory.

### 7. Security, Testing & Verification

- **No Host Execution**: Guest setup or run commands must never run directly on the host operating system.
- **Sensitive Env Redaction**: Treat environment variables as highly sensitive. Automatically redact and scrub sensitive env values from stdout/stderr logs, worker files, and results.
- **Test Enforcement**: Standardized tests must enforce architecture compliance, verify Dockerfile-only limits, reject disallowed APIs, check TAP configs, and validate metadata recovery from SQLite.
- **Fail-Fast Error Handling**: Silence in error handling is strictly prohibited. Critical failures must trigger immediate, descriptive exceptions.

## Package Structure & Module Responsibilities

The codebase inside `src/` is structured as a modular package hierarchy under `sparkvm/`:

### 1. `src/sparkvm/core/` (System Foundation)

- `config.py`: Centralized config validation, resolvability of `SPARKVM_HOME` defaults (or sudo invoking user's home), and fail-fast validation routines.
- `constants.py`: Fixed defaults (vcpu, memory, network, defaults), regex patterns, required host binaries, and fixed filesystem names.
- `errors.py`: Exception class hierarchy representing infrastructure, guest, and configuration errors (e.g. `GuestPanicError`, `JobTimeoutError`).
- `fsops.py`: Atomic file writing helper (`write_json_atomic`), safely handling nested directories.
- `logger.py`: Central thread-safe custom logger using `threading.RLock`, supporting `logfmt` and structured `JSON` formatted events.
- `utils.py`: Reusable utilities (ISOs, timing, shell quoting, memory parsers).

### 2. `src/sparkvm/storage/` (ACID-Compliant State Management)

- `db.py`: Establishes the thread-safe connection to the SQLite database.
- `schema.sql`: Contains the canonical SQLite schema defining tables for `rollouts`, `workers`, `runtime_images`, `reservations`, `machine_policy`, and `events`.
- `migrations.py`: Handles automatic sqlite file creation and structure migrations.
- `query_builder.py`: Query assembly interface for safe parameterized database writes/reads.
- `repositories.py`: Contains Repository classes (`RolloutRepository`, `WorkerRepository`, `ReservationRepository`, `MachinePolicyRepository`, `EventRepository`) separating raw query construction from orchestration rules.
- `state_store.py`: Backward compatibility mapper translating SQLite database state to and from legacy JSON file structures.

### 3. `src/sparkvm/machine/` (Infrastructure Logic)

- `disk.py`: Dedicated ext4 loop setup, sparse file allocation, directory mounts/unmounts, and guest VM log/result extraction.
- `image.py`: Manages resolution of runtime templates.
- `image_builder.py`: Parses Dockerfiles, runs `docker build`, extracts Docker filesystem exports, constructs ext4 images, and injects the `/init` harness.
- `machine_config.py`: Facilitates machine resource safety overrides (e.g. max memory overheads).
- `network.py`: Safe allocation of unique IPs, MACs, guest routing/NAT, and automated TAP setup/cleanup.

### 4. `src/sparkvm/firecracker/` (Virtualization Interface)

- `api.py`: REST API wrapper talking directly over Unix socket endpoints with the Firecracker REST server.
- `process.py`: Subprocess management, polling, and signal control for launching the Firecracker background daemon.

### 5. `src/sparkvm/api/` (Public Boundaries)

- `vm.py`: Orchestrates execution flow stages, resources capability checking, boots Firecracker, monitors runtime heartbeats, tail/redacts secrets from logs, and applies worker completion/cleanup rules.
- `rollouts.py`: Manages validation and triggers the builder to generate rollout images, ensuring rollouts under existing names are reused without rebuilding.
- `workers.py`: Supports diagnostics querying, status transitions, and log tailing.

### 6. `src/sparkvm/cli/` (Command Line App)

- `main.py`: Entry point for `sparkvm` commands (doctor, start, rollout, workers, setup).
- `setup.py`: Diagnostics, managed Firecracker binary downloads, kernel installs, KVM device link setup, and DB schema creation.
- `cleanup.py` / `runtimes.py`: Utility subcommands for cleanups and template checks.

## Conflict Behavior

- If a user request conflicts with these rules, stop.
- Respond with:

  This request conflicts with the workspace instructions.

  SparkVM only supports Dockerfile-backed rollouts:

  rollouts.create(name, runtime="Dockerfile", deleteOnSuccess=False, dockerfile="/abs/path/simplegithub.Dockerfile")

  vm = SparkVM(
  vcpu=2,
  memory="2G",
  disk="4G",
  timeout=60.0,
  network=True,
  env=runtime_env,
  )

  vm.run(rollout.id)

  I will not implement script, repo, file, command, image-name, setup_cmd, or run_cmd modes unless the workspace instructions are changed.
- Do not continue implementation after reporting the conflict.

## Implementation Priorities

- Prefer simplicity over feature expansion.
- Prefer architectural consistency over backward compatibility.
- Prefer Dockerfile mode over adding new modes.
- Prefer explicit metadata over hidden behavior.
- Prefer preserving diagnostics over deleting failure data.
- Prefer small focused changes over broad rewrites.

## Final Instruction

- Follow the project core strictly.
- Do not reinterpret it.
- Do not bypass it.
- Do not partially implement around it.
- Do not silently modify the architecture.
- SparkVM is only a Firecracker microVM runner for Dockerfile-backed rollouts.
