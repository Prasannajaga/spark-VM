# SparkVM Phase 3

## 1. Rollouts API

Rollouts are persisted execution packages with two modes:

- `script`
- `repo`

`Rollouts.create(...)` stores:

- normalized `runtime`
- optional `setup_cmd`
- required `run_cmd`
- mode-specific defaults for `disk_mb`

## 2. Script mode

- requires `files`
- writes `run.sh` at rollout root
- runs from `/job`
- default disk size: 1024 MB

## 3. Repo mode

- requires `source`
- local source must contain `.git`
- git URL sources are cloned
- writes `run.sh` (and optional `setup.sh`)
- runs from `/job/repo`
- default disk size: 4096 MB

## 4. VM execution contract

`SparkVM.run(rollout)`:

- loads rollout runtime
- resolves managed runtime image + kernel
- attaches rollout ext4 as `/dev/vdb`
- collects setup/run phase outputs from `/job/results`
- returns `VMResult`

Workers are cleaned for normal setup/run outcomes and preserved only for infrastructure failures.

## 5. Runtime requirement

SparkVM does not auto-create runtime images at execution time. Missing runtime images produce a clear `sparkvm dockify` instruction.
