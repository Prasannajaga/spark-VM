# SparkVM

SparkVM is a Firecracker wrapper that talks directly to the Firecracker API to initialize and manage microVMs for agent execution.

It provides a complete single-agent rollout lifecycle: isolated playgrounds, network restrictions, memory snapshot checkpoints, and fast restore so agents can resume exactly where they left off.

it is inspired by composer-2 technical report this is where it all started: 
[composer-2 article on X](https://x.com/jaga_prasanna/status/2054872261166080226?s=20)

## Project architecture 

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

## Cli usage 

`sparkvm setup` it creates SparkVM directories under `~/.sparkvm`. firecracker bin at `~/.sparkvm/bin/firecracker`.kernel image at `~/.sparkvm/images/vmlinux`.

```bash
sparkvm dockify <image-name>
# Convert a Docker image into a SparkVM runtime ext4 image

sparkvm runtimes 
# List, inspect, and delete runtime images

sparkvm cleanup all | rollouts | workers 
# Cleanup rollouts and/or preserved failed worker folders

sparkvm reset 
# delete all files under ~/.sparkvm 

sparkvm workers list | view | delete 
# Inspect and manage preserved failed worker attempts

sparkvm recycle 
# re-execute the failed rollouts 

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

Runtime env + networking:

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
    keep_rootfs_on_failure=False,
    keep_disk_on_failure=False,
)
```

`env` values are runtime-scoped: SparkVM writes them only to the temporary execution disk for the active run, scrubs them from preserved worker artifacts, and never stores raw values in rollout/result metadata. True in-memory secret delivery requires a future vsock guest-agent path.

Failure preservation defaults are metadata-first and disk-lightweight: SparkVM preserves `firecracker.log`, worker metadata (`result.json` / `failure.json`), and sanitized `results/`, while deleting per-run `rootfs.ext4` and `rollout.ext4` unless explicitly enabled with `keep_rootfs_on_failure=True` and/or `keep_disk_on_failure=True`.

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



## Download Firecracker binary

install using this 

```bash
cd ~/coding/coderoll

ARCH="$(uname -m)"
release_url="https://github.com/firecracker-microvm/firecracker/releases"
latest=$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' ${release_url}/latest)")

curl -L "${release_url}/download/${latest}/firecracker-${latest}-${ARCH}.tgz" | tar -xz

mv "release-${latest}-${ARCH}/firecracker-${latest}-${ARCH}" firecracker
chmod +x firecracker

sudo mv firecracker /usr/local/bin/firecracker

```

make sure you check 

```bash
firecracker --version

ls -l /dev/kvm # firecracker needs KVM so 

```


## Kernel workflow


This is kernel level code which runs the firecracker VM process 

```text
Host:
  creates workers/<vm-id>/rootfs.ext4 as a writable copy of images/<runtime>.ext4
  creates rollout.ext4
  copies rollout files
  writes .sparkvm/env.sh if env provided
  writes .sparkvm/redact.sed if env provided and redaction rules are available
  writes .sparkvm/network.env if network enabled
  attaches rootfs as /dev/vda
  attaches rollout disk as /dev/vdb
  starts VM

Guest /init:
  mounts proc/sys/dev
  mounts tmpfs dirs
  mounts /dev/vdb -> /job
  configures network
  sources env
  runs setup.sh
  runs run.sh
  writes results
  powers off

Host:
  mounts rollout.ext4
  reads results
  returns VMResult
  never copies results back into rollouts/<rollout-id>/
  deletes worker on normal completion


```
