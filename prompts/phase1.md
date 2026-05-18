# SparkVM Phase 1

## 1. SDK foundation

Phase 1 establishes the package structure and public APIs:

- `SparkVM`
- `Rollouts`
- `VMResult`
- exception hierarchy
- managed config defaults under `~/.sparkvm`

## 2. One-shot model

SparkVM is one-shot by design:

- each run starts with a fresh VM worker directory
- execution disk is per-run
- cleanup is automatic for non-infrastructure outcomes

## 3. Configuration model

Public constructor stays high-level:

- `vcpu`
- `memory`
- `timeout`
- `runtime`
- `home_dir`

Kernel/rootfs host paths are managed internally.

## 4. Forward phases

Later phases add:

- base setup (`sparkvm setup`)
- runtime conversion (`sparkvm dockify`)
- rollout execution plumbing with Firecracker
- runtime/worker CLI management
