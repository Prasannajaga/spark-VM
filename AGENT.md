## SparkVM Core

- This repository is SparkVM.
- SparkVM is a Firecracker-based microVM runner.
- SparkVM only supports Dockerfile-backed rollouts.
- The Dockerfile is the source of truth for execution.
- The project core must not be changed.

## Core Flow

- Create a rollout from a Dockerfile runtime.
- Build a Docker image from the Dockerfile.
- Convert the Docker image filesystem into an ext4 image.
- Store the ext4 image as the rollout artifact.
- Create a SparkVM instance with VM resources.
- Run the rollout inside Firecracker using rollout.id.
- Extract results after the VM exits.
- Delete successful workers.
- Preserve failed workers.

## Canonical Python API

- The only supported rollout creation API is:

  rollout = rollouts.create(
  name="my-agent",
  runtime="Dockerfile",
  deleteOnSuccess=False,
  dockerfile="/abs/path/simplegithub.Dockerfile",
  )
- The only supported VM configuration API is:

  vm = SparkVM(
  vcpu=2,
  memory="2G",
  disk="4G",
  timeout=60.0,
  network=True,
  env=runtime_env,
  )
- The only supported run API is:

  vm.run(rollout.id)

## Strict Runtime Rule

- Runtime must always be Dockerfile.
- The Dockerfile owns all setup and execution behavior.
- Do not add any other runtime type.
- Do not split setup or run logic between Dockerfile and host-side commands.

## Forbidden Rollout APIs

- Do not support:

  - rollouts.create(command="...")
  - rollouts.create(script="...")
  - rollouts.create(files=[...])
  - rollouts.create(repo="...")
  - rollouts.create(source="...")
  - rollouts.create(path="...")
  - rollouts.create(mode="repo")
  - rollouts.create(mode="script")
  - rollouts.create(mode="file")
  - rollouts.create(mode="command")
  - rollouts.create(image_name="...")
  - rollouts.create(setup_cmd="...")
  - rollouts.create(run_cmd="...")

## Forbidden Run APIs

- Do not support:

  - vm.run(command="...")
  - vm.run(script="...")
  - vm.run(path="...")
  - vm.run(source="...")
  - vm.run(repo="...")
  - vm.run(runtime="...")
  - vm.run(setup_cmd="...")
  - vm.run(run_cmd="...")

## Rollout Creation Rules

- rollouts.create() is build-time only.
- rollouts.create() must:

  - validate the Dockerfile
  - build the Docker image
  - export or extract the Docker image filesystem
  - create an ext4 filesystem image
  - inject /init
  - persist rollout metadata
  - store the ext4 image under SparkVM home
- rollouts.create() must not:

  - launch Firecracker
  - run guest commands
  - configure networking
  - create workers
  - execute arbitrary host scripts

## VM Run Rules

- vm.run() is execution-time only.
- vm.run() must accept only rollout.id.
- vm.run() must load rollout metadata from disk.
- vm.run() must use the stored rollout ext4 image.
- vm.run() must not rebuild the Docker image.
- vm.run() must not mutate the original rollout image.

## SparkVM Constructor Rules

- SparkVM must be configured through constructor arguments only.
- Supported fields are:

  - vcpu
  - memory
  - disk
  - timeout
  - network
  - env
- vcpu must be a positive integer.
- memory must be validated.
- disk must be validated.
- timeout must be a positive number.
- network must be a boolean.
- env must be treated as sensitive.

## Worker Rules

- Every VM run must create a unique worker.
- Worker directories must live under:

  ~/.sparkvm/workers/<worker_id>/
- Worker metadata must be recoverable from disk.
- Valid worker statuses are:

  - running
  - passed
  - failed
  - timeout
- Successful workers must be deleted.
- Failed workers must be preserved.
- Timeout workers must be preserved.

## Worker Files

- Each worker directory should contain:
  - rootfs.ext4
  - execution.ext4
  - firecracker.sock
  - firecracker.log
  - worker.json
  - result.json
  - failure.json

## Metadata Rules

- Rollout metadata must include:

  - id
  - name
  - runtime
  - image_path
  - deleteOnSuccess
  - created_at
- Worker metadata must include:

  - id
  - rollout_id
  - vcpu
  - memory
  - disk
  - timeout
  - network
  - status
  - created_at
  - completed_at
- Metadata writes should be atomic.
- Do not leave partial metadata files.
- Do not rely only on in-memory runtime state.

## Result Rules

- Guest results must be extracted after VM exit.
- Expected guest result files:

  - /job/results/run.stdout.log
  - /job/results/run.stderr.log
  - /job/results/exit_code
  - /job/results/status.json
- A run must not be marked passed unless:

  - the VM completed successfully
  - the guest command succeeded
  - result extraction succeeded
- If result extraction fails, mark the worker failed.
- Preserve the worker directory when result extraction fails.

## Cleanup Rules

- On passed run:

  - mark worker as passed
  - write result.json
  - delete worker directory
  - delete rollout artifacts only if deleteOnSuccess is true
- On failed run:

  - mark worker as failed
  - write failure.json when possible
  - preserve worker directory
  - preserve logs
  - preserve execution disk
  - do not delete rollout image
- On timeout:

  - terminate Firecracker
  - mark worker as timeout
  - write failure.json when possible
  - preserve worker directory
  - do not delete rollout image
- deleteOnSuccess applies only after passed execution.

## Network Rules

- If network is false:

  - do not create TAP devices
  - do not configure NAT
  - do not attach a Firecracker network interface
- If network is true:

  - create isolated per-worker network config
  - configure TAP
  - configure routing or NAT when needed
  - inject guest network metadata
  - clean up network state after execution
- Network logic must live in network.py.
- Do not scatter iproute2 or iptables logic across vm.py.

## Disk Rules

- Disk logic must live in disk.py.
- The rollout ext4 image is the root filesystem source.
- The original rollout image must never be mutated during vm.run().
- Each worker must get its own disk copy or reflink.
- The execution disk must be writable.
- Disk cleanup must remove temporary mounts and loop devices.

## Layer Responsibilities

- config.py:

  - SparkVM home path resolution
  - environment overrides
  - directory paths
  - config validation
- constants.py:

  - default values
  - supported runtime values
  - required host tools
  - fixed filenames
- rollouts.py:

  - rollout creation
  - rollout validation
  - rollout metadata
  - rollout lookup
  - rollout deletion
  - image builder calls
- image_builder.py:

  - Dockerfile validation
  - docker build
  - image inspection
  - filesystem export
  - ext4 creation
  - /init injection
  - temporary cleanup
- vm.py:

  - SparkVM constructor
  - vm.run(rollout.id)
  - worker setup
  - Firecracker lifecycle
  - timeout handling
  - result extraction
  - cleanup policy
- workers.py:

  - worker metadata
  - worker status transitions
  - worker lookup
  - failed worker preservation
- network.py:

  - TAP creation
  - IP allocation
  - NAT
  - routing
  - guest network metadata
  - network cleanup
- disk.py:

  - ext4 file creation
  - sparse files
  - disk copying
  - execution disk preparation
  - safe mount and unmount helpers
- firecracker/client.py:

  - Firecracker socket API client
  - boot source config
  - machine config
  - drive attachment
  - network attachment
  - VM start
- cli/main.py:

  - CLI parsing
  - calls to public SparkVM APIs only
- cli/setup.py:

  - host diagnostics
  - Firecracker setup
  - kernel setup
  - directory scaffolding
  - host tool checks

## IMPLEMENTATION CONSTRAINTS

- These rules are project-wide and mandatory across all modules.
- Each concern must have one owner module. Do not duplicate logic across layers.
- Keep API modules as API boundaries only; keep parsing/normalization/helpers in shared utility modules.
- Keep constants/defaults in `core/constants.py` only.
- Keep reusable helper logic in `core/utils.py` only.
- Repository/storage modules may persist and fetch data, but must not define domain semantics, defaults, or normalization rules.
- Orchestration/API/CLI modules must consume shared APIs/utilities; they must not re-implement shared logic locally.
- If a helper or constant is needed in multiple modules, move it to the owning shared module instead of copying.
- When refactoring, update all call sites project-wide to the shared owner; do not leave mixed patterns.
 

## Testing Rules

- Tests must protect the architecture.
- Tests must verify Dockerfile-only runtime.
- Tests must reject unsupported APIs.
- Tests must verify rollout metadata behavior.
- Tests must verify image build behavior.
- Tests must verify vm.run(rollout.id).
- Tests must verify vm.run() does not rebuild images.
- Tests must verify successful workers are deleted.
- Tests must verify failed workers are preserved.
- Tests must verify timeout workers are preserved.
- Tests must verify deleteOnSuccess only applies after passed runs.
- Tests must verify network=false creates no TAP device.
- Tests must verify network=true uses centralized network setup.
- Tests must verify metadata can be recovered from disk.

## Security Rules

- Do not execute rollout commands on the host.
- Do not run Dockerfile-defined commands on the host outside Docker build/export mechanics.
- Do not expose host secrets unless explicitly passed through env.
- Treat env values as sensitive.
- Redact env values from logs when possible.
- Do not print secret env values.
- Do not preserve sensitive env files in successful worker directories.
- Avoid privileged operations outside setup, disk, and network layers.

## Error Handling Rules

- Errors must be explicit.
- Do not swallow critical failures.
- Do not mark a worker passed unless all required steps succeeded.
- Write useful diagnostics to failure.json when possible.
- Preserve failed worker data.
- Preserve timeout worker data.
- Preserve result extraction failure data.

## Failure Phases

- Use clear failure phases such as:
  - rollout_validation
  - docker_build
  - docker_export
  - ext4_creation
  - metadata_write
  - worker_prepare
  - disk_prepare
  - network_prepare
  - firecracker_start
  - firecracker_config
  - vm_boot
  - guest_run
  - timeout
  - result_extract
  - cleanup

## Conflict Behavior

- If a user request conflicts with these rules, stop.
- Respond with:

  This request conflicts with the workspace instructions.

  SparkVM only supports Dockerfile-backed rollouts:

  rollouts.create(name, runtime="Dockerfile", deleteOnSuccess=False,   dockerfile="/abs/path/simplegithub.Dockerfile")

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

## Core Invariants

- A rollout is created from a Dockerfile.
- A rollout has one generated ext4 image.
- The ext4 image is created during rollout creation.
- VM execution starts from rollout.id.
- VM configuration lives in the SparkVM constructor.
- Runtime env is passed through SparkVM env.
- The Dockerfile owns execution logic.
- Failed workers are preserved.
- Successful workers are cleaned.
- deleteOnSuccess deletes rollout artifacts only after passed execution.
- Unsupported runtime modes are rejected.
- CLI mirrors the Python API.
- Tests protect the architecture.

## Final Instruction

- Follow the project core strictly.
- Do not reinterpret it.
- Do not bypass it.
- Do not partially implement around it.
- Do not silently modify the architecture.
- SparkVM is only a Firecracker microVM runner for Dockerfile-backed rollouts.
