# SparkVM Phase 5

## 1. Runtime config ownership

`RunConfig` owns execution-time resources:

- `vcpu`
- `memory`
- `disk`
- `timeout`
- `runtime`
- `network`
- `env`

Rollouts do not own CPU/RAM/DISK/network.

## 2. Global resource policy

`SparkVM.run` and recycle flows must respect the global resource cap policy before starting VMs:

```json
{
  "resource_policy": {
    "max_vm_cpu_percent": 80,
    "max_vm_memory_percent": 80,
    "max_vm_disk_percent": 80,
    "min_host_cpu_percent": 20,
    "min_host_memory_percent": 20,
    "min_host_disk_percent": 20
  }
}
```

## 3. Execution model remains rollout-first

- direct script execution is intentionally unsupported
- all runs are rollout ID based
- failed worker retries target rollout-backed workers only
