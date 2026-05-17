# SparkVM Phase 1

## 1. What was implemented

Phase 1 establishes the SDK package structure and public API for one-shot SparkVM execution.

Implemented components:

- New `sparkvm/` package with required modules:
  - `__init__.py`
  - `vm.py`
  - `process.py`
  - `api.py`
  - `image.py`
  - `job.py`
  - `result.py`
  - `disk.py`
  - `config.py`
  - `setup.py`
  - `errors.py`
  - `runtimes/__init__.py`
  - `runtimes/python.py`
- Public exports in `sparkvm.__init__`:
  - `SparkVM`
  - `VMJob`
  - `VMJobs`
  - `VMResult`
  - `SparkVMError`
- `SparkVM` constructor with product-facing parameters only:
  - `vcpu`, `memory`, `timeout`, `runtime`, `home_dir`
- Config system:
  - `SparkVMConfig` dataclass
  - memory parsing to MiB with support for `int`, `M/MiB`, and `G/GiB` inputs
  - managed default directories under `~/.sparkvm`
- Exception hierarchy for config, setup, process/API, guest execution, and cleanup failures.
- Job models:
  - `VMJob` dataclass
  - `VMJob.python(...)`
  - file path validation (no absolute paths, no `..`, no empty names)
  - `VMJobs` container with `add`, `add_python`, iteration, and length helpers.
- Result model:
  - `VMResult` dataclass
  - `passed` property.
- Phase-appropriate behavior in `SparkVM.run(job)`:
  - validates `VMJob` input
  - raises: `NotImplementedError("SparkVM.run execution is implemented in Phase 3.")`

## 2. Why the SDK hides kernel/rootfs/binary paths from users

SparkVM intentionally hides `kernel_image`, `rootfs_image`, `socket_path`, and `firecracker_bin` from normal usage to keep the API safe and stable:

- Reduces user error from low-level VM wiring and host-specific paths.
- Allows SparkVM to enforce consistent managed assets under `~/.sparkvm`.
- Keeps runtime upgrades and image rotations backwards-compatible.
- Makes the user experience match the product goal: submit a job, get a result.

## 3. Why SparkVM is one-shot instead of persistent

SparkVM is one-shot by design in this SDK path:

- Better isolation: each job starts from a clean VM state.
- Predictable cleanup: VM resources are always torn down after completion.
- Lower operational complexity for users: no VM lifecycle bookkeeping.
- Easier error containment: crashes or hangs are scoped to a single run.

This design matches the intended API:

```python
vm = SparkVM(vcpu=2, memory="4G", timeout=100)
job = VMJob.python("print('hello from SparkVM')")
result = vm.run(job)
```

## 4. What remains for Phase 2 and Phase 3

### Phase 2 (artifact/setup and job disk plumbing)

- Implement managed setup workflows in `setup.py`:
  - install/verify Firecracker binary
  - install/verify runtime images under `~/.sparkvm/images`
  - host readiness checks (KVM, disk pressure, permissions)
- Implement `disk.py` helpers:
  - create ext4 image
  - mount/copy job files/env/metadata
  - unmount and cleanup
- Define job payload conventions consumed by runtime init scripts.

### Phase 3 (end-to-end execution)

- Implement Firecracker process lifecycle in `process.py`.
- Implement Firecracker UDS API calls in `api.py`.
- Implement one-shot orchestration in `SparkVM.run`:
  - create ephemeral VM workspace
  - boot microVM
  - run guest init/job
  - collect stdout/stderr/exit code/timeout/OOM signals
  - build `VMResult`
  - guarantee cleanup and robust error mapping.
- Add integration tests for one-shot execution behavior and failure modes.
