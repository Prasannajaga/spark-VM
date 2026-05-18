# SparkVM Phase 4

## 1. What changed

Phase 4 moves SparkVM to an explicit runtime image strategy:

- `sparkvm setup` now initializes only base assets.
- Runtime rootfs images are created explicitly via `sparkvm dockify <docker-image>`.
- VM execution resolves pre-converted runtime images from `~/.sparkvm/images`.

## 2. Setup behavior

`sparkvm setup` now:

1. Creates managed directories under `~/.sparkvm`.
2. Installs/verifies Firecracker binary.
3. Installs/verifies kernel image (`vmlinux`).
4. Initializes rollout metadata.
5. Runs doctor checks.

It no longer:

- builds Debian minbase rootfs
- runs `debootstrap`
- pulls Docker images

Legacy `sparkvm setup python` now prints a deprecation message that points to `sparkvm dockify`.

## 3. Runtime conversion (`dockify`)

`sparkvm dockify <docker-image>`:

1. Optionally pulls Docker image.
2. Creates and exports a container filesystem.
3. Injects SparkVM `/init`.
4. Validates rootfs basics.
5. Builds ext4 image under `~/.sparkvm/images/<runtime>.ext4`.
6. Writes runtime metadata at `~/.sparkvm/images/<runtime>.json`.

Runtime names are normalized (for example `python:3.12-slim` -> `python-3.12-slim`).

## 4. VM execution model

`SparkVM.run(rollout_id)` now:

- loads rollout runtime
- resolves runtime ext4 + kernel from managed images
- never auto-pulls Docker images
- never auto-converts images during execution
- preserves worker directories only for infrastructure failures
- cleans workers for setup/run successes and failures

## 5. Runtime management CLI

New commands:

- `sparkvm runtimes list`
- `sparkvm runtimes inspect <runtime>`
- `sparkvm runtimes delete <runtime>`

## 6. Rollouts model updates

Rollouts now store `runtime` (normalized) rather than `base_image` in rollout metadata.

Defaults:

- script mode disk: 1024 MB
- repo mode disk: 4096 MB

## 7. Doctor updates

`sparkvm doctor` now reports:

- SparkVM home/layout
- Firecracker presence and version
- kernel presence
- KVM access
- Docker/mount tool availability
- available runtime images

If no runtimes exist, doctor suggests:

```text
sparkvm dockify python:3.12-slim
```

## 8. Networking status

Phase 4 left networking unimplemented; Phase 5 introduces TAP/NAT networking, runtime-only network/env injection files on the execution disk, and secret scrubbing for preserved infrastructure-failure workers.
