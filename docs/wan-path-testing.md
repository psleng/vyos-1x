# WAN path testing

`python/vyos/utils/wan/wan_testing.py` provides passive interface readiness,
active probes, and a stateful health monitor. Route failover is opt-in and
changes only runtime default-route metrics; it does not modify the VyOS
configuration.

## Checks

| Method | Success condition |
| --- | --- |
| Interface status | Administratively up, usable link state, usable address, and at least one live unicast route through the interface in the selected table/family |
| Ping | One ICMP echo reply from the configured IPv4/IPv6 address, using `ping` |
| TCP | Connection established or refused (RST) by the configured IP/port |
| DNS | Any valid response from the configured DNS server IP; truncated UDP responses retry over TCP |
| HTTP/HTTPS | Completed GET transaction with an HTTP response, including redirects and 4xx/5xx; HTTPS also requires valid TLS verification |

Interface status already existed. It verifies local readiness, not Internet
reachability. Its route check accepts any usable unicast route, including a
connected route; it does not require a default route or a route to a probe target.

Active probes bind to the selected Linux interface. A binding error fails the
probe rather than falling back to another interface. Run with the Linux
permissions needed for socket device binding and ICMP. HTTP uses curl with proxy
and curlrc settings disabled and does not follow redirects. Hostname URLs use
the system resolver, which may query through another interface; use a separate
DNS probe to validate DNS through the monitored WAN. Ping/TCP targets and DNS
server addresses must be IP literals. Link-local destinations requiring zone
identifiers are not intended as Internet reachability targets.

`--table` and `--family` select the passive readiness check. Active traffic uses
normal Linux routing rules subject to device binding; the tool does not install
policy routes or enter a VRF/network namespace. Ensure the intended routes and
rules exist before using it on a policy-routed WAN.

## Direct options (no JSON file)

Use one method with one or two targets. Both targets share the port, DNS query,
and timeout settings and are tested concurrently; primary/secondary identify
their order, not a sequential fallback.

```sh
sudo python3 python/vyos/utils/wan/wan_testing.py wwan0 \
  --method ping --primary-target 1.1.1.1 --secondary-target 8.8.8.8 \
  --watch --interval 10 --timeout 15 --policy any \
  --failure-threshold 3 --recovery-threshold 2
```

Other methods:

```sh
sudo python3 python/vyos/utils/wan/wan_testing.py wwan0 \
  --method tcp --primary-target 1.1.1.1 --port 443

sudo python3 python/vyos/utils/wan/wan_testing.py wwan0 \
  --method dns --primary-target 1.1.1.1 --secondary-target 8.8.8.8 \
  --name example.com --record-type A

sudo python3 python/vyos/utils/wan/wan_testing.py wwan0 \
  --method http --primary-target http://example.com/

sudo python3 python/vyos/utils/wan/wan_testing.py wwan0 \
  --method https --primary-target https://example.com/
```

`--port` is required for TCP and defaults to 53 for DNS. DNS requires `--name`;
`--record-type` defaults to A. HTTP/HTTPS ports are specified in the URL.
`--timeout` defaults to 15 seconds. DNS uses only the Python standard library,
so it needs no additional package. Omit `--watch` to run a single round.

Direct probe options and `--test-config` cannot be combined. Use JSON when
mixing methods or specifying different settings per target.

## Two-target ping example using JSON

Save this as `/tmp/wan-tests.json`:

```json
[
  {"method": "ping", "target": "1.1.1.1"},
  {"method": "ping", "target": "8.8.8.8"}
]
```

From the repository checkout on VyOS:

```sh
sudo python3 python/vyos/utils/wan/wan_testing.py eth0 \
  --test-config /tmp/wan-tests.json --watch --interval 10 \
  --policy any --failure-threshold 3 --recovery-threshold 2
```

Omit both `--test-config` and `--method` for the original interface-status output. Omit `--watch`
for one round of active tests; the exit code is 0 if the aggregate round passes,
1 if it fails, and 2 for invalid configuration. Watch mode prints one result
table per round; Ctrl-C exits with code 130.

The `any` policy passes a round if at least one target passes; `all` requires
every test to pass. Local interface readiness is also required. Three consecutive
failed rounds mark the WAN DOWN; two consecutive successful rounds mark it UP.
An opposite result resets the consecutive counter. Initial state is UNKNOWN
until one threshold is reached. Thus, a failed target alongside a passing target
does not mark an established UP interface DOWN under `any`.

Intervals are configurable from 10 through 60 seconds; each probe defaults to a
15-second timeout. Probes in a round run concurrently (up to 32). Rounds do not
overlap: if a round exceeds the interval, the next starts when it finishes.
Thresholds count rounds rather than individual probe attempts, so they do not
guarantee a fixed elapsed failover time.

## Mixed tests

```json
[
  {"method": "ping", "target": "1.1.1.1", "timeout": 5},
  {"method": "tcp", "target": "1.1.1.1", "port": 443},
  {"method": "dns", "target": "1.1.1.1", "name": "example.com", "record_type": "A"},
  {"method": "http", "target": "http://example.com/"},
  {"method": "https", "target": "https://example.com/"}
]
```

DNS port defaults to 53; `record_type` defaults to A and can be AAAA. Any valid
DNS response, including NXDOMAIN, SERVFAIL, or REFUSED, proves query connectivity.
TCP refusal counts as success because this checks path reachability, not whether
the application is accepting connections. HTTP status errors count as responses;
there is currently no expected-status or response-body matching option.

The Python API exposes `check_interface_status`, `check_ping`, `check_tcp`,
`check_dns`, `check_http`, `PathTest`, `run_path_tests`, and `PathMonitor`.
An external routing controller can consume transitions from `PathMonitor.update`
to implement its own failover/failback policy while continuing primary-WAN tests.

For a direct, opt-in primary-to-cellular failover monitor, add
`--failover-interface wwan0` to a `--watch` command. The primary interface is
demoted after the failure threshold and restored after the recovery threshold:

```sh
sudo python3 python/vyos/utils/wan/wan_testing.py eth0 \
  --method ping --primary-target 1.1.1.1 --watch \
  --failover-interface wwan0 --failure-threshold 3 --recovery-threshold 2
```

This changes the active kernel routes with `ip route change`; it is not
persistent across reboot or interface reconfiguration.

HTTP implementation reference: [curl options](https://curl.se/docs/manpage.html).
