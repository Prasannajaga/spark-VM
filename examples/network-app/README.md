# SparkVM Network App Example

This example validates SparkVM CNI networking (`network=True`) from inside a microVM by
running guest-side Python code that makes real outbound HTTPS API calls.

## What it checks

- guest code can reach external APIs over HTTPS
- DNS resolution works well enough for outbound requests
- the TAP/NAT path is usable by the actual application process, not just `ip route`

By default it tries a few public JSON endpoints and reports which ones succeeded.
The example passes if at least one outbound API call succeeds.
It also stops after the first success and automatically keeps its request budget
inside `SPARKVM_RUN_TIMEOUT_SEC` when SparkVM provides one.

If you want to point it at your own service, set `NETWORK_APP_URLS` to a comma-separated
list of URLs before starting the VM. You can also override timing with
`NETWORK_APP_TIMEOUT` and `NETWORK_APP_BUDGET`.

## Run

```bash
sparkvm setup
sparkvm network doctor
uv run python examples/network-app/run_example.py
```

The guest app prints a JSON request report and ends with:

- `NETWORK_APP_RESULT=PASS` on success
- `NETWORK_APP_RESULT=FAIL` on failure
