

## rollouts


rollouts is the job that VM execute we create the rollout and persist 

for example 


```bash
sparkvm rollouts list

sparkvm rollouts <id>

sparkvm rollouts delete <id>

```

all the rollouts will be stored under .sparkvm/bin/rollouts for the sparkvm to use it when runing VM 

you can delete , write and list 


## workers

workers is the each execution of the rollouts for untrusted code we mount 
ext4 for agent action and changes 

```bash

sparkvm workers list 

sparkvm workers view <id>

```

you can use CLI and sdk for listing and managing your workers 
note: if the rollouts execution is sucesfull workers will be cleared to prevent to storage overload 

if failed it will persist and we can refer the last changes so later we can resume it from where it left 

```bash

sparkvm workers recycle 

```
this commend will scan the workers directory for the failed rollouts 
and execute it again if the system have CPU/RAM/DISK free so the agent can pick and run




## System design

```mermaid
flowchart TD
    subgraph Cluster["Kubernetes Cluster"]
        CP["Control Plane<br/>API Server / Scheduler / Controllers"]

        subgraph Node1["Worker Node 1<br/>16 vCPU / 16 GiB RAM / 500 GB"]
            N1Reserve["Host + kubelet + container runtime<br/>Reserve: 2 vCPU / 2 GiB / 50 GB"]

            subgraph N1ExecutorPod["Executor Pod<br/>Request/Limit: 14 vCPU / 14 GiB / 450 GB usable disk"]
                N1Agent["Worker Agent<br/>~1 vCPU / ~1 GiB"]
                N1FC1["Firecracker Process job-101"]
                N1FC2["Firecracker Process job-102"]
                N1FC3["Firecracker Process job-103"]

                N1VM1["MicroVM job-101<br/>2 vCPU / 2 GiB RAM / 10 GB disk"]
                N1VM2["MicroVM job-102<br/>2 vCPU / 2 GiB RAM / 10 GB disk"]
                N1VM3["MicroVM job-103<br/>2 vCPU / 2 GiB RAM / 10 GB disk"]

                N1Agent --> N1FC1 --> N1VM1
                N1Agent --> N1FC2 --> N1VM2
                N1Agent --> N1FC3 --> N1VM3
            end
        end

        subgraph Node2["Worker Node 2<br/>16 vCPU / 16 GiB RAM / 500 GB"]
            N2Reserve["Host + kubelet + container runtime<br/>Reserve: 2 vCPU / 2 GiB / 50 GB"]

            subgraph N2ExecutorPod["Executor Pod<br/>Request/Limit: 14 vCPU / 14 GiB / 450 GB usable disk"]
                N2Agent["Worker Agent<br/>~1 vCPU / ~1 GiB"]
                N2FC1["Firecracker Process job-201"]
                N2FC2["Firecracker Process job-202"]

                N2VM1["MicroVM job-201<br/>4 vCPU / 4 GiB RAM / 20 GB disk"]
                N2VM2["MicroVM job-202<br/>4 vCPU / 4 GiB RAM / 20 GB disk"]

                N2Agent --> N2FC1 --> N2VM1
                N2Agent --> N2FC2 --> N2VM2
            end
        end

        subgraph Node3["Worker Node 3<br/>16 vCPU / 16 GiB RAM / 500 GB"]
            N3Reserve["Host + kubelet + container runtime<br/>Reserve: 2 vCPU / 2 GiB / 50 GB"]

            subgraph N3ExecutorPod["Executor Pod<br/>Request/Limit: 14 vCPU / 14 GiB / 450 GB usable disk"]
                N3Agent["Worker Agent<br/>~1 vCPU / ~1 GiB"]
                N3FC1["Firecracker Process job-301"]
                N3FC2["Firecracker Process job-302"]
                N3FC3["Firecracker Process job-303"]
                N3FC4["Firecracker Process job-304"]

                N3VM1["MicroVM job-301<br/>1 vCPU / 512 MiB RAM / 2 GB disk"]
                N3VM2["MicroVM job-302<br/>1 vCPU / 512 MiB RAM / 2 GB disk"]
                N3VM3["MicroVM job-303<br/>1 vCPU / 512 MiB RAM / 2 GB disk"]
                N3VM4["MicroVM job-304<br/>1 vCPU / 512 MiB RAM / 2 GB disk"]

                N3Agent --> N3FC1 --> N3VM1
                N3Agent --> N3FC2 --> N3VM2
                N3Agent --> N3FC3 --> N3VM3
                N3Agent --> N3FC4 --> N3VM4
            end
        end
    end

    CP --> Node1
    CP --> Node2
    CP --> Node3

```

### A workflow of Case A

```mermaid
flowchart TD
    User["User submits code"] --> API["API Service Pod<br/>1 vCPU / 1 GiB"]
    API --> Queue["Job Queue"]
    Queue --> ExecutorPod["Executor Pod on Worker Node<br/>14 vCPU / 14 GiB"]

    ExecutorPod --> CapacityCheck{"Enough pod capacity?"}

    CapacityCheck -->|Yes| CreateDisk["Create job disk<br/>Example: 10 GB"]
    CapacityCheck -->|No| Wait["Wait for running VM to finish"]

    CreateDisk --> StartFC["Start Firecracker process<br/>inside executor pod cgroup"]
    StartFC --> ConfigureVM["Configure VM specs<br/>2 vCPU / 2 GiB RAM"]
    ConfigureVM --> StartVM["Start MicroVM"]
    StartVM --> RunScript["Guest /init runs user script"]
    RunScript --> WriteOutput["Write output.log + exit_code"]
    WriteOutput --> ShutdownVM["VM shuts down"]
    ShutdownVM --> CollectResult["Executor collects result"]
    CollectResult --> Cleanup["Cleanup Firecracker process + disk"]
    Cleanup --> Queue
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