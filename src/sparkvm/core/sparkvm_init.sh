#!/bin/sh
set +e

log_console() {
  sparkvm_log_level="$1"
  shift

  if [ -e /dev/console ]; then
    printf '%s\n' "SparkVM: [${sparkvm_log_level}] $*" > /dev/console
  else
    printf '%s\n' "SparkVM: [${sparkvm_log_level}] $*" >&2
  fi
}

log_fn_start() {
  log_console "INFO" "fn=$1 status=start"
}

log_fn_end() {
  sparkvm_fn_name="$1"
  sparkvm_fn_rc="$2"

  if [ "$sparkvm_fn_rc" -eq 0 ]; then
    log_console "INFO" "fn=${sparkvm_fn_name} status=success"
  else
    log_console "ERROR" "fn=${sparkvm_fn_name} status=failed rc=${sparkvm_fn_rc}"
  fi

  return "$sparkvm_fn_rc"
}

shutdown_vm() {
  log_fn_start "shutdown_vm"
  sync

  # Try userland shutdown commands in background so we never block forever
  # on "System halted" states that don't fully terminate the microVM.
  if command -v poweroff >/dev/null 2>&1; then
    poweroff -f >/dev/null 2>&1 &
    log_console "INFO" "fn=shutdown_vm action=poweroff_launched"
  fi
  if command -v halt >/dev/null 2>&1; then
    halt -f >/dev/null 2>&1 &
    log_console "INFO" "fn=shutdown_vm action=halt_launched"
  fi
  if command -v reboot >/dev/null 2>&1; then
    reboot -f >/dev/null 2>&1 &
    log_console "INFO" "fn=shutdown_vm action=reboot_launched"
  fi
  if command -v busybox >/dev/null 2>&1; then
    busybox poweroff -f >/dev/null 2>&1 &
    busybox reboot -f >/dev/null 2>&1 &
    log_console "INFO" "fn=shutdown_vm action=busybox_shutdown_launched"
  fi

  # Give userland commands a brief chance, then force-kernel shutdown/reset.
  sleep 1
  if [ -w /proc/sysrq-trigger ]; then
    echo s > /proc/sysrq-trigger || true
    echo u > /proc/sysrq-trigger || true
    echo o > /proc/sysrq-trigger || true
    sleep 1
    echo b > /proc/sysrq-trigger || true
    log_console "INFO" "fn=shutdown_vm action=sysrq_shutdown_triggered"
  fi

  log_console "ERROR" "fn=shutdown_vm status=failed reason=no_shutdown_command_succeeded"
  while true; do sleep 3600; done
}

prepare_linux_runtime() {
  log_fn_start "prepare_linux_runtime"
  sparkvm_fn_rc=0

  mkdir -p /proc /sys /dev /dev/pts || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mkdir_core_dirs rc=$?"
    sparkvm_fn_rc=1
  }

  mountpoint -q /proc || mount -t proc proc /proc || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mount_proc rc=$?"
    sparkvm_fn_rc=1
  }
  mountpoint -q /sys || mount -t sysfs sysfs /sys || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mount_sys rc=$?"
    sparkvm_fn_rc=1
  }
  mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mount_dev rc=$?"
    sparkvm_fn_rc=1
  }

  mkdir -p /dev/pts || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mkdir_dev_pts rc=$?"
    sparkvm_fn_rc=1
  }
  mountpoint -q /dev/pts || mount -t devpts devpts /dev/pts || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mount_dev_pts rc=$?"
    sparkvm_fn_rc=1
  }

  ln -sf /proc/self/fd /dev/fd || true
  ln -sf /proc/self/fd/0 /dev/stdin || true
  ln -sf /proc/self/fd/1 /dev/stdout || true
  ln -sf /proc/self/fd/2 /dev/stderr || true
  ln -sf /proc/kcore /dev/core 2>/dev/null || true

  mkdir -p /tmp /run /var/tmp || {
    log_console "ERROR" "fn=prepare_linux_runtime action=mkdir_tmp_dirs rc=$?"
    sparkvm_fn_rc=1
  }
  mountpoint -q /tmp || mount -t tmpfs tmpfs /tmp || true
  mountpoint -q /run || mount -t tmpfs tmpfs /run || true
  mountpoint -q /var/tmp || mount -t tmpfs tmpfs /var/tmp || true

  export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  export DEBIAN_FRONTEND=noninteractive
  export TZ=Etc/UTC

  log_fn_end "prepare_linux_runtime" "$sparkvm_fn_rc"
  return "$?"
}

mount_job_disk() {
  log_fn_start "mount_job_disk"

  mkdir -p /job || {
    log_console "ERROR" "fn=mount_job_disk action=mkdir_job rc=$?"
    log_fn_end "mount_job_disk" 1
    return "$?"
  }

  if ! mount /dev/vdb /job; then
    log_console "ERROR" "fn=mount_job_disk action=mount_dev_vdb rc=$?"
    shutdown_vm
  fi

  mkdir -p /job/results || {
    log_console "ERROR" "fn=mount_job_disk action=mkdir_job_results rc=$?"
    log_fn_end "mount_job_disk" 1
    return "$?"
  }

  log_fn_end "mount_job_disk" 0
  return "$?"
}

configure_network() {
  log_fn_start "configure_network"

  if [ ! -f /job/.sparkvm/network.env ]; then
    log_console "INFO" "fn=configure_network reason=network_env_missing"
    log_fn_end "configure_network" 0
    return "$?"
  fi

  . /job/.sparkvm/network.env

  if [ "${SPARKVM_NET_ENABLED:-0}" != "1" ]; then
    log_console "INFO" "fn=configure_network reason=network_disabled"
    log_fn_end "configure_network" 0
    return "$?"
  fi

  if ! command -v ip >/dev/null 2>&1; then
    log_console "ERROR" "fn=configure_network reason=ip_command_missing"
    log_fn_end "configure_network" 1
    return "$?"
  fi

  sparkvm_fn_rc=0

  ip link set lo up || {
    log_console "ERROR" "fn=configure_network action=link_lo_up rc=$?"
    sparkvm_fn_rc=1
  }
  ip link set "${SPARKVM_GUEST_IFACE:-eth0}" up || {
    log_console "ERROR" "fn=configure_network action=link_guest_up iface=${SPARKVM_GUEST_IFACE:-eth0} rc=$?"
    sparkvm_fn_rc=1
  }
  ip addr flush dev "${SPARKVM_GUEST_IFACE:-eth0}" || true
  ip addr add "$SPARKVM_GUEST_CIDR" dev "${SPARKVM_GUEST_IFACE:-eth0}" || {
    log_console "ERROR" "fn=configure_network action=addr_add cidr=$SPARKVM_GUEST_CIDR iface=${SPARKVM_GUEST_IFACE:-eth0} rc=$?"
    sparkvm_fn_rc=1
  }

  if [ -n "${SPARKVM_GATEWAY:-}" ]; then
    ip route add default via "$SPARKVM_GATEWAY" dev "${SPARKVM_GUEST_IFACE:-eth0}" || {
      log_console "ERROR" "fn=configure_network action=route_default gateway=$SPARKVM_GATEWAY rc=$?"
      sparkvm_fn_rc=1
    }
  fi

  mkdir -p /etc || {
    log_console "ERROR" "fn=configure_network action=mkdir_etc rc=$?"
    sparkvm_fn_rc=1
  }

  dns_servers_raw="${SPARKVM_DNS_SERVERS:-${SPARKVM_DNS:-1.1.1.1}}"
  if [ -z "$dns_servers_raw" ]; then
    dns_servers_raw="1.1.1.1"
  fi

  : > /etc/resolv.conf || {
    log_console "ERROR" "fn=configure_network action=truncate_resolv_conf rc=$?"
    sparkvm_fn_rc=1
  }

  wrote_dns=0
  old_ifs="$IFS"
  IFS=','
  for dns_server in $dns_servers_raw; do
    if [ -z "$dns_server" ]; then
      continue
    fi
    echo "nameserver $dns_server" >> /etc/resolv.conf || {
      log_console "ERROR" "fn=configure_network action=append_resolver dns=${dns_server} rc=$?"
      sparkvm_fn_rc=1
    }
    wrote_dns=1
  done
  IFS="$old_ifs"

  if [ "$wrote_dns" -eq 0 ]; then
    echo "nameserver 1.1.1.1" >> /etc/resolv.conf || {
      log_console "ERROR" "fn=configure_network action=append_default_resolver rc=$?"
      sparkvm_fn_rc=1
    }
  fi

  # Keep DNS failure latency bounded for dynamic workloads.
  echo "options timeout:2 attempts:2" >> /etc/resolv.conf || {
    log_console "ERROR" "fn=configure_network action=append_resolver_options rc=$?"
    sparkvm_fn_rc=1
  }

  log_console "INFO" "fn=configure_network dns_servers=${dns_servers_raw}"

  log_fn_end "configure_network" "$sparkvm_fn_rc"
  return "$?"
}

load_runtime_env() {
  log_fn_start "load_runtime_env"
  sparkvm_fn_rc=0

  if [ -f /job/.sparkvm/runtime.env ]; then
    set -a
    . /job/.sparkvm/runtime.env || {
      log_console "ERROR" "fn=load_runtime_env action=source_runtime_env rc=$?"
      sparkvm_fn_rc=1
    }
    set +a
  fi

  if [ -f /job/.sparkvm/env.sh ]; then
    set -a
    . /job/.sparkvm/env.sh || {
      log_console "ERROR" "fn=load_runtime_env action=source_env_sh rc=$?"
      sparkvm_fn_rc=1
    }
    set +a
  fi

  log_fn_end "load_runtime_env" "$sparkvm_fn_rc"
  return "$?"
}

run_with_timeout() {
  timeout_sec="$1"
  script="$2"
  out_file="$3"
  err_file="$4"

  log_fn_start "run_with_timeout"
  log_console "INFO" "fn=run_with_timeout script=${script} timeout_sec=${timeout_sec}"

  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout_sec" sh "$script" > "$out_file" 2> "$err_file"
    sparkvm_fn_rc=$?
    log_fn_end "run_with_timeout" "$sparkvm_fn_rc"
    return "$?"
  fi

  log_console "INFO" "fn=run_with_timeout reason=timeout_command_missing"
  sh "$script" > "$out_file" 2> "$err_file"
  sparkvm_fn_rc=$?
  log_fn_end "run_with_timeout" "$sparkvm_fn_rc"
  return "$?"
}

redact_to_console() {
  file="$1"
  log_fn_start "redact_to_console"

  if [ ! -s "$file" ]; then
    log_console "INFO" "fn=redact_to_console reason=empty_file file=$file"
    log_fn_end "redact_to_console" 0
    return "$?"
  fi

  if [ -f /job/.sparkvm/redact.sed ] && command -v sed >/dev/null 2>&1; then
    sed -f /job/.sparkvm/redact.sed "$file" > /dev/console
    sparkvm_fn_rc=$?
    log_fn_end "redact_to_console" "$sparkvm_fn_rc"
    return "$?"
  fi

  if [ -f /job/.sparkvm/env.sh ]; then
    log_console "INFO" "fn=redact_to_console reason=redaction_unavailable_runtime_env_present"
    log_fn_end "redact_to_console" 0
    return "$?"
  fi

  cat "$file" > /dev/console
  sparkvm_fn_rc=$?
  log_fn_end "redact_to_console" "$sparkvm_fn_rc"
  return "$?"
}

print_phase_logs() {
  phase="$1"
  out_file="/job/results/${phase}.stdout.log"
  err_file="/job/results/${phase}.stderr.log"

  log_fn_start "print_phase_logs"
  log_console "INFO" "fn=print_phase_logs phase=${phase}"

  if [ -s "$out_file" ]; then
    log_console "INFO" "phase=${phase} stream=stdout begin"
    redact_to_console "$out_file"
    log_console "INFO" "phase=${phase} stream=stdout end"
  fi

  if [ -s "$err_file" ]; then
    log_console "INFO" "phase=${phase} stream=stderr begin"
    redact_to_console "$err_file"
    log_console "INFO" "phase=${phase} stream=stderr end"
  fi

  log_fn_end "print_phase_logs" 0
  return "$?"
}

run_phase() {
  phase="$1"
  script="$2"
  timeout_sec="$3"
  out_file="/job/results/${phase}.stdout.log"
  err_file="/job/results/${phase}.stderr.log"

  log_fn_start "run_phase"
  log_console "INFO" "fn=run_phase phase=${phase} script=${script} timeout_sec=${timeout_sec}"

  run_with_timeout "$timeout_sec" "$script" "$out_file" "$err_file"
  code=$?

  if [ "$code" -eq 124 ]; then
    log_console "ERROR" "fn=run_phase phase=${phase} reason=timeout timeout_sec=${timeout_sec}"
  fi

  echo "$code" > "/job/results/${phase}.exit_code"
  print_phase_logs "$phase"

  if [ "$code" -eq 0 ]; then
    log_console "INFO" "fn=run_phase phase=${phase} status=success"
  else
    log_console "ERROR" "fn=run_phase phase=${phase} status=failed rc=${code}"
  fi

  log_fn_end "run_phase" "$code"
  return "$?"
}

collect_network_diagnostics() {
  log_fn_start "collect_network_diagnostics"

  if [ "${SPARKVM_NET_ENABLED:-0}" != "1" ]; then
    log_console "INFO" "fn=collect_network_diagnostics reason=network_disabled"
    log_fn_end "collect_network_diagnostics" 0
    return "$?"
  fi

  out_file="/job/results/network.stdout.log"
  err_file="/job/results/network.stderr.log"
  : > "$out_file"
  : > "$err_file"

  log_console "INFO" "fn=collect_network_diagnostics status=begin"
  if command -v ip >/dev/null 2>&1; then
    ip addr > /dev/console 2>&1 || true
    ip route > /dev/console 2>&1 || true
  else
    log_console "ERROR" "fn=collect_network_diagnostics reason=ip_command_missing"
  fi
  cat /etc/resolv.conf > /dev/console 2>&1 || true
  log_console "INFO" "fn=collect_network_diagnostics status=end"

  {
    if command -v ip >/dev/null 2>&1; then
      echo "[network] ip addr"
      ip addr
      echo ""
      echo "[network] ip route"
      ip route
      echo ""
    else
      echo "[network] ip command missing"
    fi
    echo "[network] /etc/resolv.conf"
    cat /etc/resolv.conf
  } > "$out_file" 2> "$err_file" || true

  log_fn_end "collect_network_diagnostics" 0
  return "$?"
}

log_console "INFO" "sparkvm_init status=start"

prepare_linux_runtime
mount_job_disk
load_runtime_env
configure_network
collect_network_diagnostics

cd /job || {
  log_console "ERROR" "sparkvm_init action=cd_job rc=$?"
  echo 1 > /job/results/final_exit_code
  shutdown_vm
}

if [ "${SPARKVM_RUN_SETUP_IN_GUEST:-0}" = "1" ] && [ -f /job/setup.sh ]; then
  run_phase "setup" "/job/setup.sh" "${SPARKVM_SETUP_TIMEOUT_SEC:-300}"
  setup_code=$?
else
  setup_code=0
  echo 0 > /job/results/setup.exit_code
  log_console "INFO" "setup phase skipped"
fi

if [ "$setup_code" -ne 0 ]; then
  log_console "ERROR" "sparkvm_init phase=setup status=failed rc=${setup_code}"
  echo "$setup_code" > /job/results/final_exit_code
  shutdown_vm
fi

if [ ! -f /job/run.sh ]; then
  log_console "ERROR" "sparkvm_init reason=missing_run_script path=/job/run.sh"
  echo "missing /job/run.sh" > /job/results/run.stderr.log
  echo 127 > /job/results/run.exit_code
  echo 127 > /job/results/final_exit_code
  shutdown_vm
fi

run_phase "run" "/job/run.sh" "${SPARKVM_RUN_TIMEOUT_SEC:-300}"
run_code=$?

echo "$run_code" > /job/results/final_exit_code
if [ "$run_code" -eq 0 ]; then
  log_console "INFO" "sparkvm_init phase=run status=success"
else
  log_console "ERROR" "sparkvm_init phase=run status=failed rc=${run_code}"
fi

shutdown_vm
