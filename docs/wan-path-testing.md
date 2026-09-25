# WAN path testing

`/usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py` provides passive interface readiness,
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

## Interface status

Checks whether `wwan0` is administratively up, has an address, and has a usable
route.

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method interface-status
```

## Ping

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method ping --primary-target 1.1.1.1
```

## TCP

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method tcp --primary-target 1.1.1.1 --port 443
```

## DNS

IPv4 (`A`) query:

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method dns --primary-target 1.1.1.1 \
  --name example.com --record-type A
```

IPv6 (`AAAA`) query:

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method dns --primary-target 2606:4700:4700::1111 \
  --name example.com --record-type AAAA
```

## HTTP

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method http --primary-target http://example.com/
```

## HTTPS

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method https --primary-target https://example.com/
```

## Continuous monitoring

Use `--watch` to repeat a test. Two targets can be tested concurrently with
`--secondary-target`.

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py wwan0 \
  --method ping --primary-target 1.1.1.1 --secondary-target 8.8.8.8 \
  --watch --interval 10 --timeout 15 --policy any \
  --failure-threshold 3 --recovery-threshold 2
```

`--port` is required for TCP and defaults to 53 for DNS. DNS requires `--name`;
`--record-type` defaults to A. HTTP/HTTPS ports are specified in the URL.
`--timeout` defaults to 15 seconds. DNS uses only the Python standard library,
so it needs no additional package. Omit `--watch` to run a single round.

Omit `--watch` for one round of active tests; the exit code is 0 if the aggregate round passes,
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

## Multiple targets and WAN failover

Multiple targets prevent one unreachable destination from falsely declaring the
WAN down. With `--policy any`, one successful target is enough for the round to
pass. With `--policy all`, every target must pass.

Example with two ping targets and a 10-second interval:

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py <primary-wan> \
  --method ping \
  --primary-target 1.1.1.1 \
  --secondary-target 8.8.8.8 \
  --watch --interval 10 --policy any \
  --failure-threshold 3 --recovery-threshold 2
```

The `any` policy behaves as follows:

```text
10:00:00  Target 1 FAIL   Target 2 FAIL  -> failed round 1
10:00:10  Target 1 FAIL   Target 2 PASS  -> passing round; failure count resets
10:00:20  Target 1 FAIL   Target 2 PASS  -> WAN remains UP
```

To require every target to pass, use:

```sh
--policy all
```

After three consecutive failed rounds, the primary WAN is considered DOWN. To
change the runtime route preference to cellular, add the cellular interface:

```sh
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py <primary-wan> \
  --method ping --primary-target 1.1.1.1 --secondary-target 8.8.8.8 \
  --watch --interval 10 --policy any \
  --failure-threshold 3 --recovery-threshold 2 \
  --failover-interface wwan0
```

When failover is enabled, the primary route uses metric `10` and the cellular
route uses metric `220` while the primary is healthy. Gateways are discovered
from the active default route for each interface. Explicit `--primary-gateway`
and `--cellular-gateway` options can override discovery. After three failed rounds,
the metrics are reversed. After two successful recovery rounds, the primary
route is preferred again. These are runtime route changes and are not saved to
the VyOS configuration.

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
sudo python3 /usr/lib/python3/dist-packages/vyos/utils/wan/wan_testing.py eth0 \
  --method ping --primary-target 1.1.1.1 --watch \
  --failover-interface wwan0 --failure-threshold 3 --recovery-threshold 2
```

This changes the active kernel routes with `ip route change`; it is not
persistent across reboot or interface reconfiguration.

HTTP implementation reference: [curl options](https://curl.se/docs/manpage.html).
