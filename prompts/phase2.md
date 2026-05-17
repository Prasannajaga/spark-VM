# SparkVM Phase 2

## 1. What setup installs/builds

Phase 2 adds managed setup so SparkVM owns host-side runtime assets under `~/.sparkvm`.

Implemented managed layout:

- `~/.sparkvm/bin/firecracker`
- `~/.sparkvm/images/vmlinux`
- `~/.sparkvm/images/python-3.12-rootfs.ext4`
- `~/.sparkvm/work/`
- `~/.sparkvm/cache/`

Implemented setup behaviors:

- Host checks:
  - Linux-only host guard.
  - CPU architecture guard for `x86_64` and `aarch64`.
  - `/dev/kvm` existence and access checks.
- Host tool checks:
  - Required: `curl`, `tar`, `dd`, `mkfs.ext4`, `mount`, `umount`.
  - Optional for base setup: `docker`.
  - Required for Python runtime build: `docker`.
- Firecracker installation:
  - Pinned version constant (`v1.15.1`), no dynamic `latest`.
  - Downloads official release tarball for host architecture.
  - Extracts binary and installs to `~/.sparkvm/bin/firecracker`.
  - Marks executable.
- Kernel installation:
  - Downloads managed kernel to `~/.sparkvm/images/vmlinux` using pinned URL constants per architecture.

## 2. Why runtime images are internal SDK-managed assets

Runtime images are SDK-managed so SparkVM can provide a predictable, versioned execution environment:

- SparkVM controls compatibility between Firecracker, boot args, and runtime rootfs format.
- SDK upgrades can evolve internals without changing user call sites.
- Team-wide reproducibility improves because everyone uses the same managed layout and image conventions.

## 3. Why users should not pass kernel/rootfs manually

Not exposing kernel/rootfs arguments in the public constructor avoids unstable, low-level integration details in normal SDK usage:

- Prevents path/config drift across machines.
- Reduces user errors from mismatched kernel/rootfs combinations.
- Keeps the API focused on job execution intent instead of VM plumbing.

In SparkVM, kernel/rootfs paths are resolved internally from `SparkVMConfig` and setup-managed paths.

## 4. How Python runtime setup works

`sparkvm setup python` does the following:

1. Runs base setup checks and installs (directories, OS/arch, KVM, host tools, Firecracker, kernel).
2. Requires Docker and pulls `python:3.12-slim`.
3. Creates a temporary container and exports its filesystem.
4. Extracts the filesystem into a temporary working directory.
5. Verifies `python3` exists in the exported rootfs.
6. Writes SparkVM guest `/init` script into the exported rootfs and sets it executable.
7. Builds an ext4 disk image.
8. Mounts the image as a loop device and copies the exported rootfs into it.
9. Unmounts and writes final artifact to `~/.sparkvm/images/python-3.12-rootfs.ext4`.

The runtime init script is stored in `sparkvm/runtimes/python.py` as `INIT_TEMPLATE`.

## 5. Known limitations

- Setup/build steps rely on external host tools and Docker availability.
- Rootfs image assembly uses loopback mount/umount and requires host permissions.
- Rootfs build is Linux-host specific and not supported on non-Linux systems.
- Phase 2 builds artifacts only; one-shot VM execution orchestration remains Phase 3.
