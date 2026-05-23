# SparkVM Phase 4

## 1. Rollout execution contract

`SparkVM.run(rollout_id, config=RunConfig(...))` is the only supported execution shape.

- Input: rollout ID
- Resources/runtime/network/env: `RunConfig`
- Output: `VMResult`

Direct script execution is intentionally not supported.

## 2. deleteOnSuccess

Rollout metadata stores camelCase `deleteOnSuccess`.
Python API uses snake_case `delete_on_success`.

A rollout is auto-deleted only when:

- `deleteOnSuccess == true`
- `VMResult.passed == true`

It is not auto-deleted for setup failure, run failure, infra failure, timeout, or OOM.

## 3. Recycle contract

`sparkvm recycle` remains a loop-based retry mechanism for failed rollout workers.
No one-shot recycle mode is added in this phase.
