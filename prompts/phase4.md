# SparkVM Phase 4

## 1. What Changed

Phase 4 replaces language/runtime-specific rootfs management with one managed Debian minbase base image.

SparkVM now manages:

- `~/.sparkvm/bin/firecracker`
- `~/.sparkvm/images/vmlinux`
- `~/.sparkvm/images/debian-rootfs.ext4`

Rollouts now target:

- `base_image="debian-minbase"`

instead of runtime identifiers like `python-3.12`.

## 2. Why Debian Minbase

Debian minbase was chosen to provide:

- a small, predictable guest filesystem
- no language lock-in
- explicit user-controlled dependency installation through rollout `setup_cmd`
- cleaner path to future networking and package workflows

This makes SparkVM execution language-agnostic by default.

## 3. Why Docker Export Was Removed

Previous rootfs creation depended on exporting Docker images.

That approach was removed because it:

- tightly coupled setup to Docker availability
- encouraged language-specific base images
- increased setup complexity and artifact size

Phase 4 rootfs build now uses `debootstrap --variant=minbase` directly.

## 4. Why Language-Specific Rootfs Was Removed

Language-specific rootfs images (for example Python-only rootfs) were removed to avoid baked-in runtimes and to keep VM images minimal.

Dependency installation is now rollout-driven:

- `setup_cmd` (optional) installs/configures what the job needs
- `run_cmd` executes the workload

## 5. Setup Flow

`sparkvm setup` now performs:

1. Directory bootstrap under `~/.sparkvm`
2. Firecracker install/verification
3. Kernel install/verification
4. Debian minbase rootfs build/verification (`debian-rootfs.ext4`)
5. `/init` injection and executable permission
6. Rollout metadata initialization (`~/.sparkvm/rollouts/metadata.json`)

`setup python` is now deprecated behavior and prints guidance:

- Language-specific setup is no longer required.
- Use rollout `setup_cmd` instead.

## 6. Rollout Model Updates

Rollout schema now uses `base_image`:

- `base_image` defaults to `debian-minbase`
- `run_cmd` is required for both script and repo modes
- `setup_cmd` is optional

Mode behavior:

- `script` mode runs inside `/job`
- `repo` mode runs inside `/job/repo`

No `instructions.md` file is created by SparkVM.

## 7. Result Model and Execution

VM result collection remains phase-based:

- setup phase logs/exit code (if `setup.sh` exists)
- run phase logs/exit code
- final exit code

`VMResult` now includes base-image context (`base_image`) along with rollout and phase information.

## 8. Current Limitation (No Networking Yet)

Networking is intentionally not implemented in Phase 4.

Implications:

- `apt-get`, `pip`, `npm`, and other internet-dependent commands inside the VM may fail.
- This is expected behavior for now.

Current workaround:

1. vendor dependencies into repo/rollout
2. use offline/local artifacts
3. wait for networking phase

## 9. What Remains for Future Networking Phase

Planned follow-up includes:

- TAP attachment and guest interface configuration
- host-side NAT/forwarding setup
- DNS and outbound connectivity in guest VMs
- clearer network policy controls and diagnostics

Phase 4 intentionally does not fake networking support.
