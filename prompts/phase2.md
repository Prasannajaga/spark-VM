# SparkVM Phase 2

## 1. Setup asset model

Phase 2 now defines SparkVM managed setup assets under `~/.sparkvm`:

- `bin/firecracker`
- `images/vmlinux`
- `rollouts/metadata.json`
- `workers/`
- `cache/`

Setup is base-only; runtime rootfs creation is moved to `sparkvm dockify`.

## 2. Why runtime images are explicit

Runtime conversion is explicit so VM execution stays deterministic:

- no hidden Docker pulls during `run()`
- no runtime conversion during VM startup
- clear separation between host provisioning and execution

## 3. Runtime image naming

Docker image names are normalized into runtime IDs:

- `python:3.12-slim` -> `python-3.12-slim`
- `ghcr.io/org/image:tag` -> `ghcr.io-org-image-tag`

Runtime artifacts live at:

- `~/.sparkvm/images/<runtime>.ext4`
- `~/.sparkvm/images/<runtime>.json`

## 4. Dockify flow

`sparkvm dockify` exports a Docker rootfs, injects SparkVM `/init`, validates basic tools, builds ext4, and writes metadata.

Because ext4 mount/umount is required, dockify may need root privileges.

## 5. Current limitation

Networking is not yet implemented for guest VMs, so internet-dependent commands may fail unless dependencies are pre-baked or vendored.
