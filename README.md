# SparkVM

SparkVM runs persistent rollouts inside Firecracker VMs.

## Current execution model

- No direct script execution API.
- No direct script CLI execution path.
- All execution goes through rollout IDs.
- `Rollouts.create()` is repo-only.
- Runtime resources are configured at execution time with `RunConfig`.

## CLI usage

```bash
sparkvm setup
sparkvm cleanup all
sparkvm cleanup rollouts
sparkvm cleanup workers
sparkvm reset
sparkvm workers list
sparkvm workers view <vm-id>
sparkvm workers delete <vm-id>
```

## Python usage

```python
from sparkvm import RunConfig, SparkVM
from sparkvm.rollouts import Rollouts

rollout = Rollouts().create(
    name="version-3",
    source="/path/to/local/git/repo",  # or git URL
    ref="main",                        # optional
    setup_cmd="pip install -r requirements.txt",  # optional
    run_cmd="python3 main.py",
    delete_on_success=False,
)

runtime_env = {"OPENAI_API_KEY": "..."}

result = SparkVM().run(
    rollout.id,
    config=RunConfig(
        vcpu=2,
        memory="2G",
        disk="4G",
        timeout=300,
        runtime="sparkvm-debian-minbase",
        network=True,
        env=runtime_env,
    ),
)

print(result.exit_code, result.status)
```

## Rollouts.create contract

`Rollouts.create()` supports:

- `name` (required)
- `source` (required): local git repo path or git URL
- `run_cmd` (required)
- `setup_cmd` (optional)
- `ref` (optional)
- `delete_on_success` (optional, default `False`)

Unsupported:

- `mode`
- `files`
- `command`
- `disk_mb`
- direct script rollouts

## deleteOnSuccess behavior

Rollout metadata stores:

```json
{
  "deleteOnSuccess": false
}
```

If `deleteOnSuccess` is `true`, SparkVM deletes the rollout directory and removes it from `rollouts/metadata.json` only when `VMResult.passed` is `true`.

SparkVM does not delete the rollout on:

- setup failure
- run failure
- infrastructure failure
- timeout
- OOM
