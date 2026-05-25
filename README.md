# SparkVM

SparkVM is a Firecracker microVM runner for Dockerfile rollouts usefull for agents long running task and inspired by composer-2 Async RL.

### why sparkvm ?

sparkvm will allocate and manage the agent rollouts efficiently by allocating available system to each microVM based on the host machines so you can freely run the agent rollouts without any hesitations

you dont need big kubernetes cluster for triggering 1000 of rollouts anymore sparkvm will do that for you
just deploy it on your machine it will track and run the VM efficiently

check how that will be implemented

you can use SDK and cli as well both supporte

## Setup SparkVM

Use this when you are preparing a machine for SparkVM for the first time:

```bash
sparkvm setup
```

What `sparkvm setup` does:

1. Creates SparkVM directories under your home (`~/.sparkvm` by default): `bin`, `images`, `rollouts`, `workers`, `scheduler`, `cache`.
2. Validates host requirements: Linux host, supported arch (`x86_64` or `aarch64`), and required setup tools.
3. Installs the managed Firecracker binary into `~/.sparkvm/bin/firecracker` when needed.
4. Creates `~/.sparkvm/bin/kvm` symlink pointing to `/dev/kvm`.
5. Downloads the managed kernel image to `~/.sparkvm/images/vmlinux` when needed.
6. Initializes the SQLite DB and default machine policy.
7. Migrates old rollout metadata into SQLite when legacy data exists.

If you run it again:

1. It is mostly safe and idempotent.
2. Existing managed assets are reused.
3. Use `--force` to reinstall/re-download managed assets.

Useful setup flags:

1. `sparkvm setup --force`
2. `sparkvm setup --owner <user>` (requires root, then chowns SparkVM home recursively)

To wipe everything and start fresh:

```bash
sparkvm reset
```

What `sparkvm reset` does:

1. Prompts for confirmation unless `--force` is provided.
2. Unmounts mounted paths under worker folders first.
3. Deletes everything inside SparkVM home (`~/.sparkvm` by default), including DB state, rollouts, workers, images, binaries, kernel, logs, and cache.
4. Recreates only an empty SparkVM home directory.

## Canonical Python API

```python
from sparkvm import Rollouts, SparkVM, SparkScheduler, MachineConfig

rollout = Rollouts().create(
    name="my-agent",
    runtime="Dockerfile",
    dockerfile="Dockerfile",
    deleteOnSuccess=False,
)

# Option A: run immediately (single rollout execution)
vm = SparkVM(vcpu=2, memory="2G", disk="4G", timeout=60.0, network=True, env={})
result = vm.run(rollout.id)
print(result.status, result.exit_code, result.passed)

# Option B: scheduler-managed queue execution
MachineConfig.set_policy(poll_interval=2.0)
scheduler = SparkScheduler()
summary = scheduler.tick()  # one scheduling cycle
print(summary["tick_id"], summary["spawned"])
```

## CLI Usage (All Available Args)

```bash
# Global option (available on every command)
sparkvm [--home-dir <path>] <command> ...

# Setup / diagnostics
sparkvm setup [--force] [--owner <user>]
sparkvm doctor
sparkvm start
sparkvm cleanup {rollouts|workers|all} [--force]
sparkvm reset [--force]

# Rollouts
sparkvm rollout create \
  --name <name> \
  [--dockerfile Dockerfile] \
  [--delete-on-success] \
  [--vcpu 2] \
  [--memory 2G] \
  [--disk 4G] \
  [--timeout 60.0] \
  [--network | --no-network] \
  [--env KEY=VALUE --env KEY2=VALUE2]
sparkvm rollout list
sparkvm rollout view <rollout-id>
sparkvm rollout <rollout-id>   # alias for: sparkvm rollout view <rollout-id>

# Workers
sparkvm workers run <rollout-id> \
  [--vcpu 2] \
  [--memory 2G] \
  [--disk 4G] \
  [--timeout 60.0] \
  [--network | --no-network] \
  [--env KEY=VALUE --env KEY2=VALUE2]
sparkvm workers list
sparkvm workers view <worker-id> \
  [--tail <n>] [--live] [--result] [--failure] [--results] [--path]
```
