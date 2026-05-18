# SparkVM

SparkVM runs rollouts inside Firecracker microVMs using pre-converted runtime rootfs images.

## Managed layout

```text
~/.sparkvm/
├── bin/
│   └── firecracker
├── images/
│   ├── vmlinux
│   ├── python-3.12-slim.ext4
│   ├── python-3.12-slim.json
│   ├── ubuntu-24.04.ext4
│   └── ubuntu-24.04.json
├── rollouts/
│   ├── metadata.json
│   └── rollout-*/
├── workers/
└── cache/
```

## Setup

`sparkvm setup` now does only base host initialization:

1. Creates SparkVM directories under `~/.sparkvm`.
2. Installs/verifies Firecracker at `~/.sparkvm/bin/firecracker`.
3. Installs/verifies kernel image at `~/.sparkvm/images/vmlinux`.
4. Initializes `~/.sparkvm/rollouts/metadata.json`.

It does **not** build Debian rootfs images and does **not** run Docker/debootstrap.

Legacy command behavior:

```bash
sparkvm setup python
```

prints:

```text
Language-specific setup is no longer required. Use `sparkvm dockify <docker-image>`.
```

## Build runtimes with dockify

Convert Docker images into SparkVM runtime ext4 images:

```bash
sparkvm dockify python:3.12-slim
sparkvm dockify node:22-slim
sparkvm dockify ubuntu:24.04 --name ubuntu-24.04
sparkvm dockify ghcr.io/org/custom:latest --size-mb 4096
```

`dockify` writes:

- `~/.sparkvm/images/<runtime>.ext4`
- `~/.sparkvm/images/<runtime>.json`

`dockify` now prefers non-root conversion via `mkfs.ext4 -d`. If your host `mkfs.ext4` lacks `-d` support, run with sudo (mount fallback) and pin ownership:

```bash
sudo sparkvm dockify python:3.12-slim --home-dir /home/<user>/.sparkvm --owner <user>
```

## Runtime CLI

```bash
sparkvm runtimes list
sparkvm runtimes inspect python-3.12-slim
sparkvm runtimes delete python-3.12-slim --force
```

## Python usage

```python
from sparkvm import SparkVM, Rollouts

rollouts = Rollouts()
rollout = rollouts.create(
    name="hello",
    mode="script",
    runtime="python-3.12-slim",
    files={"main.py": "print('hello')"},
    run_cmd="python3 /job/main.py",
)

result = SparkVM(runtime="python-3.12-slim").run(rollout.id)
print(result.exit_code, result.stdout)
```

Custom Ubuntu runtime:

```python
rollout = rollouts.create(
    name="shell",
    mode="script",
    runtime="ubuntu-24.04",
    files={"hello.sh": "echo hello"},
    run_cmd="sh /job/hello.sh",
)

result = SparkVM(runtime="ubuntu-24.04").run(rollout.id)
```

## Runtime resolution behavior

- Runtime names are normalized (`python:3.12-slim` -> `python-3.12-slim`).
- SparkVM never auto-pulls or auto-converts Docker images during `run()`.
- Missing runtime image raises a clear error with a `sparkvm dockify ...` hint.

## Networking status

Guest networking is not implemented yet.

If `setup_cmd` needs internet (for example `apt`, `pip`, `npm`), it may fail unless dependencies are already present in the dockified runtime or included in the rollout.
