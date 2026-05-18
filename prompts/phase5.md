# SparkVM Phase 5

## 1. Public API

`SparkVM` now supports:

- `network: bool = False`
- `env: dict[str, str] | None = None`

Example:

```python
import os
from sparkvm import SparkVM

vm = SparkVM(
    runtime="python-3.12-slim",
    vcpu=2,
    memory="2G",
    timeout=300,
    network=True,
    env={"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]},
)
```

## 2. Runtime env injection model

- Env keys are validated with `^[A-Za-z_][A-Za-z0-9_]*$`.
- Env values must be strings.
- Env values are stored only in-memory on the `SparkVM` instance.
- During a VM run, SparkVM writes runtime-only `/job/.sparkvm/env.sh` into the temporary execution disk.
- `/init` sources `/job/.sparkvm/env.sh` before `setup.sh` and `run.sh`.
- Env values are not written to rollout metadata and not written to `failure.json`.

## 3. Networking model

- `network=True` creates a per-VM TAP device and configures NAT/forwarding with `iptables`.
- Guest network config is written to runtime-only `/job/.sparkvm/network.env`.
- `/init` configures `eth0` from `network.env`.
- Firecracker network interface attach is done before `InstanceStart`.

Phase-5 privilege requirement:

- Root or `CAP_NET_ADMIN` is required for TAP + `iptables` setup.
- If missing, SparkVM raises:
  `Network setup requires root/CAP_NET_ADMIN for TAP and iptables. Run with sudo or configure capabilities.`

## 4. Secret handling

- Env values never persist to rollouts (`~/.sparkvm/rollouts`).
- Env values never persist to runtime image assets (`~/.sparkvm/images`).
- On infrastructure failure, SparkVM attempts to scrub `/job/.sparkvm/env.sh` from `rollout.ext4`.
- If scrub fails, SparkVM deletes `rollout.ext4` to avoid preserving secrets.
- `failure.json` stores only env keys, never values.

## 5. Doctor and dockify updates

- `sparkvm doctor` reports network host tool availability (`ip`, `iptables`, `sysctl`) and privilege status.
- `sparkvm network doctor` aliases the same diagnostics.
- `sparkvm dockify` metadata now records runtime validation details and warns when `ip` is missing:
  `Runtime image does not contain ip command. network=True will fail unless installed.`

## 6. Known limitation

SparkVM cannot prevent secret leakage if user-provided `setup.sh`/`run.sh` prints environment variables to stdout/stderr.
