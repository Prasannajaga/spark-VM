# SparkVM

SparkVM is a Firecracker microVM runner for Dockerfile-backed rollouts.

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

## Canonical CLI

```bash
sparkvm setup
sparkvm rollout create --name my-agent --runtime Dockerfile
sparkvm run <rollout-id> --vcpu 2 --memory 2G --disk 4G --timeout 60 --network
```

## Notes

- SparkVM supports only Dockerfile-backed rollouts.
- `rollout create` builds the Docker image, exports filesystem, injects `/init`, and stores rollout ext4 artifacts.
- `sparkvm run` and `SparkVM.run()` execute by rollout id only.
