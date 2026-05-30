# SparkVM Network App Example

This example validates SparkVM CNI networking (`network=True`) from inside a microVM by
running guest-side Python code that makes real outbound HTTPS API calls.

## What it checks

- guest code can reach external APIs over HTTPS
- DNS resolution works well enough for outbound requests
- the TAP/NAT path is usable by the actual application process, not just `ip route`
- forced IPv4 HTTPS and forced IPv6 HTTPS are reported separately

By default it prints a timeout-protected DNS snapshot, then performs separate
socket-level HTTPS probes with `AF_INET` and `AF_INET6`. This makes it clear
whether IPv4 works while IPv6 or NAT64 is still failing.

If you want to point it at your own service, set `NETWORK_APP_URL` before
starting the VM. You can also override the per-probe timeout with
`NETWORK_APP_TIMEOUT`; otherwise the app derives a small per-probe timeout from
`SPARKVM_RUN_TIMEOUT_SEC` and flushes each result as soon as it is available.
The normal Python `requests` stack is skipped by default because it can block
before family-specific probes print useful diagnostics; set
`NETWORK_APP_RUN_DEFAULT=1` if you want to run it after the IPv4/IPv6 probes.

## Run

```bash
sparkvm setup
sparkvm network doctor
uv run python examples/network-app/run_example.py
```

The guest app prints a JSON request report and ends with:

- `NETWORK_PROBE_BEGIN=...` with URL and per-probe timeout
- `NETWORK_DNS_RESULT=...` for Python `getaddrinfo` results by address family
- `NETWORK_IPV4_TCP_RESULT=...` for forced IPv4 TCP connect
- `NETWORK_IPV4_RESULT=...` for forced IPv4 HTTPS
- `NETWORK_IPV6_TCP_RESULT=...` for forced IPv6 TCP connect
- `NETWORK_IPV6_RESULT=...` for forced IPv6 HTTPS
- `NETWORK_DEFAULT_RESULT=...` for normal Python `requests`, skipped unless enabled
- `NETWORK_APP_RESULT=PASS` on success
- `NETWORK_APP_RESULT=FAIL` on failure
