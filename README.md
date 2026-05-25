# SparkVM

SparkVM is a Firecracker microVM runner for Dockerfile rollouts usefull for agents long running task and inspired by composer-2 Async RL.  

## Canonical Python API

```python
from sparkvm import SparkVM
from sparkvm.rollouts import Rollouts

rollout = Rollouts().create(
    name="my-agent",
    runtime="Dockerfile",
    deleteOnSuccess=False,
)

runtime_env = {"OPENAI_API_KEY": "..."}

vm = SparkVM(
    vcpu=2,
    memory="2G",
    disk="4G",
    timeout=60.0,
    network=True,
    env=runtime_env,
)

result = vm.run(rollout.id)
print(result.status, result.exit_code)
```

## CLI Usage (All Available Args)

```bash
# Global option (available on every command)
sparkvm [--home-dir <path>] <command> ...

# Setup / diagnostics
sparkvm setup [--force] [--owner <user>]
sparkvm doctor
sparkvm rollout list
sparkvm start
sparkvm cleanup {rollouts|workers|all} [--force]

# Rollouts
sparkvm rollout create \
  --name <name> \
  [--runtime Dockerfile] \
  [--dockerfile Dockerfile] \
  [--delete-on-success]
sparkvm rollout view <rollout-id>
sparkvm rollout <rollout-id>   # alias for: sparkvm rollout view <rollout-id>

# Workers
sparkvm workers run <rollout-id> \
  [--vcpu 2] \
  [--memory 2G] \
  [--disk 4G] \
  [--timeout 60.0] \
  [--network] \
  [--env KEY=VALUE --env KEY2=VALUE2]
sparkvm workers list
sparkvm workers view <worker-id>
# Optional worker view flags:
#   [--tail <n>] [--live] [--result] [--failure] [--results] [--path]
```
