```bash


#!/bin/sh -> means the script runs with POSIX shell.
set +e  # do not exit immediately when a command fails


# this function manages help shutdown if panic or failure 
# since we convert the docker to ext4 image the 
# docker sometime dont support so we try to shutdown using fallback commands
shutdown_vm() {
  sync
  if command -v poweroff >/dev/null 2>&1; then
    poweroff -f
  fi
  if command -v halt >/dev/null 2>&1; then
    halt -f
  fi
  if command -v reboot >/dev/null 2>&1; then
    reboot -f
  fi
  if command -v busybox >/dev/null 2>&1; then
    busybox poweroff -f
    busybox reboot -f
  fi
  if [ -w /proc/sysrq-trigger ]; then
    echo s > /proc/sysrq-trigger || true
    echo u > /proc/sysrq-trigger || true
    echo o > /proc/sysrq-trigger || true
    sleep 1
    echo b > /proc/sysrq-trigger || true
  fi
  echo "SparkVM: no shutdown command found" > /dev/console
  while true; do sleep 3600; done
}


# mounting kernel file systems 
# /proc = process kernel info 
# /sys = device/kernel sysfs
# /dev = device nodes like /dev/vda, /dev/vdb, /dev/console 
mount -t proc proc /proc
mount -t sysfs sysfs /sys

mount -t devtmpfs devtmpfs /dev || true
# dev is where we mount our rollouts code 

# we make tmp directory for write access because rootfs should 
# always be read-only
mkdir -p /tmp /run /var/tmp
mount -t tmpfs tmpfs /tmp || true
mount -t tmpfs tmpfs /run || true
mount -t tmpfs tmpfs /var/tmp || true
# so rootsfs = read-only
# /job  = agent writeable area 
# tmpfs = temporary runtime writes

# prevent disk not found fallback to shutdowwn 
if ! mount /dev/vdb /job; then
  echo "SparkVM: failed to mount /dev/vdb at /job" > /dev/console
  shutdown_vm
fi


# rootsfs always lives in /dev/vda
# rollouts.ext4 our rollout disk mounted at /dev/vdb 
mkdir -p /job
mount /dev/vdb /job

mkdir -p /job/results
# expected job directory layout:
# /job/
#   setup.sh
#   run.sh
#   main.py or repo/
#   rollout.json
#   .sparkvm/
#     env.sh
#     network.env
#   results/ -> all outputs goes here 


print_phase_logs() {
  phase="$1"
  out_file="/job/results/${phase}.stdout.log"
  err_file="/job/results/${phase}.stderr.log"

  if [ -s "$out_file" ]; then
    echo "SparkVM: ${phase} stdout begin" > /dev/console
    cat "$out_file" > /dev/console
    echo "SparkVM: ${phase} stdout end" > /dev/console
  fi

  if [ -s "$err_file" ]; then
    echo "SparkVM: ${phase} stderr begin" > /dev/console
    cat "$err_file" > /dev/console
    echo "SparkVM: ${phase} stderr end" > /dev/console
  fi
}

if ! command -v ip >/dev/null 2>&1; then
  echo "SparkVM: ip command missing; network unavailable" > /dev/console
fi

# network on tap config to the VM
# This makes external calls possible if host TAP/NAT was set up correctly.
if [ -f /job/.sparkvm/network.env ]; then
  . /job/.sparkvm/network.env

  if [ "$SPARKVM_NET_ENABLED" = "1" ]; then
    ip link set eth0 up
    ip addr add "$SPARKVM_GUEST_CIDR" dev eth0
    ip route add default via "$SPARKVM_HOST_IP" dev eth0
    echo "nameserver ${SPARKVM_DNS:-1.1.1.1}" > /etc/resolv.conf
  fi
fi

# load the passed env 
# here set -a means every variable loaded becomes
# exported to child process like 
# job/setup.sh & run.sh 
if [ -f /job/.sparkvm/env.sh ]; then
  set -a
  . /job/.sparkvm/env.sh
  set +a
fi

cd /job

# later we move into /job 
# start executing the setup followed by run.sh 
# setup will includes dependency installation 
# run.sh will run your main script  
if [ -f /job/setup.sh ]; then
  echo "SparkVM: running setup.sh" > /dev/console
  sh /job/setup.sh > /job/results/setup.stdout.log 2> /job/results/setup.stderr.log
  setup_code=$?
  echo "SparkVM: setup.sh exit code=${setup_code}" > /dev/console
  print_phase_logs "setup"
else
  setup_code=0
fi

# setup captures:
# stdout -> /job/results/setup.stdout.log
# stderr -> /job/results/setup.stderr.log
# exit   -> setup_code


echo "$setup_code" > /job/results/setup.exit_code

# do not run the main command if setup failed.
if [ "$setup_code" -ne 0 ]; then
  echo "$setup_code" > /job/results/final_exit_code
  shutdown_vm
fi

echo "SparkVM: running run.sh" > /dev/console
sh /job/run.sh > /job/results/run.stdout.log 2> /job/results/run.stderr.log
run_code=$?

# run captures:
# stdout -> /job/results/run.stdout.log
# stderr -> /job/results/run.stderr.log
# exit   -> run_code

echo "SparkVM: run.sh exit code=${run_code}" > /dev/console
print_phase_logs "run"

echo "$run_code" > /job/results/run.exit_code
echo "$run_code" > /job/results/final_exit_code


shutdown_vm

# On VM exits: 
# rollout.ext4 reads
# /job/results/setup.stdout.log
# /job/results/setup.stderr.log
# /job/results/setup.exit_code
# /job/results/run.stdout.log
# /job/results/run.stderr.log
# /job/results/run.exit_code
# /job/results/final_exit_code

```


