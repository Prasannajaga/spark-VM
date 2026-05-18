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
)
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

