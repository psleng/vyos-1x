# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Interface readiness, WAN probes, and opt-in default-route failover."""

import errno
import json
import math
import secrets
import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit


@dataclass(frozen=True)
class InterfaceStatus:
    interface: str
    passed: bool
    reason: str
    admin_up: bool | None = None
    link_state: str = 'UNKNOWN'
    address_ready: bool | None = None
    route_ready: bool | None = None


def _read_ip(arguments, timeout):
    output = subprocess.run(
        ['ip', '-json', *arguments], capture_output=True, text=True,
        check=True, timeout=timeout,
    )
    records = json.loads(output.stdout)
    if not isinstance(records, list) or any(
        not isinstance(record, dict) for record in records
    ):
        raise ValueError('Expected a list of iproute2 objects')
    return records


def _usable_address(address, family):
    if address.get('family') != ('inet' if family == 4 else 'inet6'):
        return False
    if address.get('scope') != 'global':
        return False
    flags = address.get('flags', [])
    if any(address.get(flag) or flag in flags for flag in ('tentative', 'dadfailed')):
        return False
    if address.get('valid_life_time') == 0:
        return False
    value = ip_address(address['local'])
    return value.version == family and not (
        value.is_unspecified or value.is_loopback or value.is_link_local
        or value.is_multicast
    )


def _usable_route(route, interface, family):
    if route.get('type', 'unicast') != 'unicast':
        return False
    if route.get('expires') == 0:
        return False
    if set(route.get('flags', [])) & {'dead', 'linkdown'}:
        return False
    # iproute2 emits inline ECMP members in nexthops. A different live
    # interface must never make the monitored interface pass.
    return any(
        hop.get('dev') == interface
        and not set(hop.get('flags', [])) & {'dead', 'linkdown'}
        for hop in route.get('nexthops', [route])
    )


def check_interface_status(interface, *, family=4, table='main', timeout=2.0):
    """Check link, address and a usable unicast route for one IP family and table.

    UP is required administratively. UNKNOWN operstate is accepted only for
    point-to-point devices, whose drivers may not report carrier state.
    Supply the VRF/policy table explicitly; routes in other tables do not count.
    A passing result establishes local readiness, not Internet reachability.
    Read failures return a failed result with reason ``check_error``.
    """
    if not isinstance(interface, str) or not interface or interface.startswith('-'):
        raise ValueError('An interface name is required')
    if family not in (4, 6):
        raise ValueError('family must be 4 or 6')
    table = str(table)
    if not table or table.startswith('-') or table in ('all', 'unspec', '0'):
        raise ValueError('A specific routing table is required')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')

    details = {}

    def result(reason):
        return InterfaceStatus(interface, reason == 'up', reason, **details)

    try:
        links = _read_ip(['address', 'show', 'dev', interface], timeout)
        link = next((item for item in links if item.get('ifname') == interface), None)
        if link is None:
            return result('interface_missing')
        flags = set(link.get('flags', []))
        details['admin_up'] = 'UP' in flags
        state = link.get('operstate', 'UNKNOWN')
        if (state == 'UNKNOWN' and {'UP', 'LOWER_UP'} <= flags
                and 'NO-CARRIER' not in flags):
            state = 'UP'
        details['link_state'] = state
        link_ready = 'NO-CARRIER' not in flags and (
            state == 'UP' or (state == 'UNKNOWN' and 'POINTOPOINT' in flags)
        )
        details['address_ready'] = any(
            _usable_address(a, family) for a in link.get('addr_info', [])
        )
        routes = _read_ip([f'-{family}', 'route', 'show', 'table', table], timeout)
        details['route_ready'] = any(
            _usable_route(route, interface, family) for route in routes
        )
        if not details['admin_up']:
            return result('admin_down')
        if not link_ready:
            return result('link_down')
        if not details['address_ready']:
            return result('no_address')
        if not details['route_ready']:
            return result('no_route')
        return result('up')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError,
            AttributeError):
        return result('check_error')


def discover_interfaces(timeout=2.0):
    """Return physical and virtual interfaces, including down interfaces.

    Discovery covers the current network namespace and does not infer WAN
    roles. Interfaces in other routing tables still need an explicit table.
    Loopback and internal PIM register interfaces are included.
    """
    links = _read_ip(['link', 'show'], timeout)
    names = set()
    for link in links:
        name = link.get('ifname')
        if not isinstance(name, str) or not name:
            raise ValueError('Interface discovery returned an invalid name')
        names.add(name)
    return sorted(names)


@dataclass(frozen=True)
class ProbeResult:
    interface: str
    method: str
    target: str
    passed: bool
    reason: str


def _validate_probe(interface, timeout):
    if (not isinstance(interface, str) or not interface
            or interface.startswith('-') or '\0' in interface):
        raise ValueError('An interface name is required')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')


def _validate_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('port must be an integer from 1 to 65535')


def _bound_socket(interface, target, kind, timeout):
    """Fail closed if device binding is unavailable or not permitted."""
    family = socket.AF_INET if ip_address(target).version == 4 else socket.AF_INET6
    sock = socket.socket(family, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        interface.encode() + b'\0')
        sock.settimeout(timeout)
        return sock
    except Exception:
        sock.close()
        raise


def check_ping(interface, target, *, timeout=15.0):
    """Send one ICMP echo using iputils ping, with a numeric destination."""
    _validate_probe(interface, timeout)
    address = ip_address(target)
    try:
        completed = subprocess.run(
            ['ping', f'-{address.version}', '-n', '-I', interface,
             '-c', '1', '-W', str(timeout), str(address)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        reason = {0: 'echo_reply', 1: 'no_reply'}.get(completed.returncode, 'check_error')
    except subprocess.TimeoutExpired:
        reason = 'timeout'
    except (OSError, subprocess.SubprocessError):
        reason = 'check_error'
    return ProbeResult(interface, 'ping', str(target), reason == 'echo_reply', reason)


def check_tcp(interface, target, port, *, timeout=15.0):
    """Connect to an IP/port. Refusal (RST) also establishes path reachability."""
    _validate_probe(interface, timeout)
    target = str(ip_address(target))
    _validate_port(port)
    try:
        with _bound_socket(interface, target, socket.SOCK_STREAM, timeout) as sock:
            code = sock.connect_ex((target, port))
        reason = {0: 'connected', errno.ECONNREFUSED: 'refused',
                  errno.ETIMEDOUT: 'timeout', errno.EAGAIN: 'timeout'}.get(
                      code, 'connect_failed')
    except TimeoutError:
        reason = 'timeout'
    except (OSError, AttributeError):
        reason = 'check_error'
    return ProbeResult(interface, 'tcp', f'{target}:{port}',
                       reason in ('connected', 'refused'), reason)


_DNS_TYPES = {'A': 1, 'NS': 2, 'CNAME': 5, 'SOA': 6, 'PTR': 12,
              'MX': 15, 'TXT': 16, 'AAAA': 28, 'SRV': 33, 'CAA': 257}
_DNS_RCODES = {0: 'response', 1: 'formerr', 2: 'servfail', 3: 'nxdomain',
               4: 'notimp', 5: 'refused'}


def _dns_question(name, record_type, query_id):
    if not isinstance(name, str) or not name:
        raise ValueError('A DNS query name is required')
    try:
        labels = [label.encode('idna') for label in name.rstrip('.').split('.')]
    except UnicodeError as error:
        raise ValueError('Invalid DNS query name') from error
    if (not labels or any(not label or len(label) > 63 for label in labels)
            or sum(len(label) + 1 for label in labels) + 1 > 255):
        raise ValueError('Invalid DNS query name')
    if not isinstance(record_type, str):
        raise TypeError('DNS record type must be a string')
    record_type = record_type.upper()
    try:
        query_type = (_DNS_TYPES[record_type] if not record_type.isdecimal()
                      else int(record_type))
    except (KeyError, ValueError) as error:
        raise ValueError('Unsupported DNS record type') from error
    if not 1 <= query_type <= 65535:
        raise ValueError('Invalid DNS record type')
    encoded_name = b''.join(bytes([len(label)]) + label for label in labels) + b'\0'
    header = struct.pack('!HHHHHH', query_id, 0x0100, 1, 0, 0, 0)
    return header + encoded_name + struct.pack('!HH', query_type, 1), query_type


def _dns_read_name(message, offset):
    labels = []
    next_offset = None
    visited = set()
    while True:
        if offset >= len(message) or offset in visited:
            raise ValueError('Invalid DNS name')
        visited.add(offset)
        length = message[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(message):
                raise ValueError('Truncated DNS pointer')
            if next_offset is None:
                next_offset = offset + 2
            offset = ((length & 0x3F) << 8) | message[offset + 1]
            continue
        if length & 0xC0 or length > 63:
            raise ValueError('Invalid DNS label')
        offset += 1
        if length == 0:
            break
        if offset + length > len(message):
            raise ValueError('Truncated DNS label')
        labels.append(message[offset:offset + length].lower())
        offset += length
    return labels, offset if next_offset is None else next_offset


def _dns_response(message, query_id, expected_name, query_type):
    if len(message) < 12:
        raise ValueError('Truncated DNS response')
    response_id, flags, questions, _, _, _ = struct.unpack('!HHHHHH', message[:12])
    if (response_id != query_id or not flags & 0x8000
            or flags & 0x7800 or questions != 1):
        raise ValueError('Mismatched DNS response')
    labels, offset = _dns_read_name(message, 12)
    if offset + 4 > len(message):
        raise ValueError('Truncated DNS question')
    response_type, response_class = struct.unpack('!HH', message[offset:offset + 4])
    if labels != expected_name or response_type != query_type or response_class != 1:
        raise ValueError('Mismatched DNS question')
    return _DNS_RCODES.get(flags & 0x000F, f'rcode_{flags & 0x000F}'), bool(flags & 0x0200)


def _recv_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise OSError('DNS TCP connection closed early')
        data.extend(chunk)
    return bytes(data)


def check_dns(interface, server, name, *, port=53, record_type='A', timeout=15.0):
    """Send a DNS query through an interface; any valid response proves connectivity."""
    _validate_probe(interface, timeout)
    server = str(ip_address(server))
    _validate_port(port)
    query_id = secrets.randbelow(65536)
    query, query_type = _dns_question(name, record_type, query_id)
    expected_name = [label.encode('idna').lower()
                     for label in name.rstrip('.').split('.')]
    deadline = time.monotonic() + timeout
    try:
        with _bound_socket(interface, server, socket.SOCK_DGRAM, timeout) as sock:
            sock.connect((server, port))
            sock.sendall(query)
            response = sock.recv(65535)
        reason, truncated = _dns_response(
            response, query_id, expected_name, query_type)
        if truncated:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with _bound_socket(interface, server, socket.SOCK_STREAM, remaining) as sock:
                sock.connect((server, port))
                sock.sendall(struct.pack('!H', len(query)) + query)
                length = struct.unpack('!H', _recv_exact(sock, 2))[0]
                response = _recv_exact(sock, length)
            reason, truncated = _dns_response(
                response, query_id, expected_name, query_type)
            if truncated:
                raise ValueError('Truncated DNS TCP response')
    except TimeoutError:
        reason = 'timeout'
    except (OSError, AttributeError, UnicodeError, ValueError, struct.error):
        reason = 'check_error'
    return ProbeResult(interface, 'dns', f'{server}/{name}',
                       reason not in ('timeout', 'check_error'), reason)


def check_http(interface, url, *, timeout=15.0):
    """GET a URL; any completed HTTP response (including 4xx/5xx) proves reachability.

    TLS verification stays enabled. Redirects are not followed. Hostnames use
    the system resolver; use a separate DNS probe to test WAN-specific DNS.
    """
    _validate_probe(interface, timeout)
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('An HTTP/HTTPS URL without credentials is required')
    if parsed.port is not None:
        _validate_port(parsed.port)
    try:
        completed = subprocess.run(
            ['curl', '--disable', '--silent', '--show-error', '--noproxy', '*',
             '--interface', f'if!{interface}', '--proto', '=http,https',
             '--request', 'GET', '--max-time', str(timeout),
             '--output', '/dev/null', '--write-out', '%{http_code}', '--url', url],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        code = completed.stdout.strip()
        if completed.returncode == 0 and code.isdigit() and 100 <= int(code) <= 599:
            reason = f'http_{code}'
            passed = True
        else:
            reason = 'timeout' if completed.returncode == 28 else 'request_failed'
            passed = False
    except subprocess.TimeoutExpired:
        reason, passed = 'timeout', False
    except (OSError, subprocess.SubprocessError):
        reason, passed = 'check_error', False
    return ProbeResult(interface, parsed.scheme, url, passed, reason)


@dataclass(frozen=True)
class PathTest:
    method: str
    target: str
    port: int | None = None
    name: str | None = None
    timeout: float = 15.0
    record_type: str = 'A'

    def __post_init__(self):
        _validate_probe('validation', self.timeout)
        if self.method in ('ping', 'tcp', 'dns'):
            ip_address(self.target)
            if self.method == 'tcp':
                _validate_port(self.port)
            if self.method == 'dns':
                _validate_port(53 if self.port is None else self.port)
                _dns_question(self.name, self.record_type, 0)
        elif self.method in ('http', 'https'):
            parsed = urlsplit(self.target)
            if (parsed.scheme != self.method or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError('An HTTP/HTTPS URL matching the method without credentials is required')
            if parsed.port is not None:
                _validate_port(parsed.port)
        else:
            raise ValueError(f'Unknown test method: {self.method}')

    def run(self, interface):
        if self.method == 'ping':
            return check_ping(interface, self.target, timeout=self.timeout)
        if self.method == 'tcp':
            return check_tcp(interface, self.target, self.port, timeout=self.timeout)
        if self.method == 'dns':
            return check_dns(interface, self.target, self.name,
                             port=53 if self.port is None else self.port,
                             record_type=self.record_type, timeout=self.timeout)
        if self.method in ('http', 'https'):
            if urlsplit(self.target).scheme != self.method:
                raise ValueError('URL scheme must match the test method')
            return check_http(interface, self.target, timeout=self.timeout)
        raise ValueError(f'Unknown test method: {self.method}')


def run_path_tests(interface, tests):
    """Run up to 32 mixed tests concurrently so one timeout cannot delay peers."""
    tests = tuple(tests)
    if not 1 <= len(tests) <= 32:
        raise ValueError('Configure between 1 and 32 path tests')
    with ThreadPoolExecutor(max_workers=len(tests)) as executor:
        return list(executor.map(lambda test: test.run(interface), tests))


@dataclass
class PathMonitor:
    """Count consecutive aggregate rounds, not failures of individual targets.

    State starts unknown (None); recovery_threshold successes establish UP.
    The caller schedules rounds and decides whether/how to change routes.
    """
    policy: str = 'any'
    failure_threshold: int = 3
    recovery_threshold: int = 2
    up: bool | None = None
    failures: int = 0
    successes: int = 0

    def __post_init__(self):
        if self.policy not in ('any', 'all'):
            raise ValueError('policy must be any or all')
        for threshold in (self.failure_threshold, self.recovery_threshold):
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
                raise ValueError('Thresholds must be positive integers')

    def update(self, results, *, interface_ready=True):
        results = tuple(results)
        if not results:
            raise ValueError('At least one test result is required')
        passed = interface_ready and (any if self.policy == 'any' else all)(
            result.passed for result in results)
        if passed:
            self.failures = 0
            self.successes = min(self.successes + 1, self.recovery_threshold)
            if self.successes >= self.recovery_threshold:
                self.up = True
        else:
            self.successes = 0
            self.failures = min(self.failures + 1, self.failure_threshold)
            if self.failures >= self.failure_threshold:
                self.up = False
        return self.up


class RouteFailover:
    """Switch default-route preference after monitored WAN state changes.

    Route changes are deliberately opt-in: callers must construct this class
    and pass transitions from :class:`PathMonitor`.  Metrics are changed at
    runtime only and are not written to the VyOS configuration.
    """

    def __init__(self, primary, cellular, *, primary_metric=10,
                 cellular_metric=220, runner=None):
        for name in (primary, cellular):
            if (not isinstance(name, str) or not name or name.startswith('-')
                    or '\0' in name):
                raise ValueError('Interface names are required')
        if primary == cellular:
            raise ValueError('Primary and cellular interfaces must differ')
        for metric in (primary_metric, cellular_metric):
            if isinstance(metric, bool) or not isinstance(metric, int) or metric < 0:
                raise ValueError('Route metrics must be non-negative integers')
        self.primary = primary
        self.cellular = cellular
        self.primary_metric = primary_metric
        self.cellular_metric = cellular_metric
        self.runner = runner or subprocess.run
        self.active = None

    def _set_metric(self, interface, metric):
        self.runner(
            ['ip', 'route', 'change', 'default', 'dev', interface,
             'metric', str(metric)], check=True, capture_output=True,
            text=True)

    def apply(self, primary_up):
        """Apply a route preference only when the preferred WAN changes."""
        desired = self.primary if primary_up else self.cellular
        if desired == self.active:
            return False
        if primary_up:
            self._set_metric(self.primary, self.primary_metric)
            self._set_metric(self.cellular, self.cellular_metric)
        else:
            self._set_metric(self.primary, self.cellular_metric)
            self._set_metric(self.cellular, self.primary_metric)
        self.active = desired
        return True


def main(argv=None):
    """Check interface readiness or run configured active path tests."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Check WAN interface status and connectivity')
    parser.add_argument('interfaces', nargs='*',
                        help='Physical or virtual interface names; omit for automatic discovery')
    parser.add_argument('--family', type=int, choices=(4, 6), default=4)
    parser.add_argument('--table', default='main', help='Routing table name or ID')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--test-config', help='JSON file containing a list of PathTest objects')
    source.add_argument('--method', choices=('interface-status', 'ping', 'tcp', 'dns', 'http', 'https'),
                        help='Test type for direct target options')
    parser.add_argument('--primary-target', help='Target IP, DNS server IP, or HTTP/HTTPS URL')
    parser.add_argument('--secondary-target', help='Optional second target, tested concurrently')
    parser.add_argument('--port', type=int, help='TCP port (required) or DNS port (default: 53)')
    parser.add_argument('--name', help='Name to resolve for DNS tests')
    parser.add_argument('--record-type', help='DNS record type (default: A)')
    parser.add_argument('--timeout', type=float, help='Timeout per direct test in seconds (default: 15)')
    parser.add_argument('--watch', action='store_true', help='Repeat path tests until interrupted')
    parser.add_argument('--interval', type=int, choices=range(10, 61), default=10,
                        metavar='10-60', help='Seconds between rounds (default: 10)')
    parser.add_argument('--policy', choices=('any', 'all'), default='any')
    parser.add_argument('--failure-threshold', type=int, default=3)
    parser.add_argument('--recovery-threshold', type=int, default=2)
    parser.add_argument('--failover-interface',
                        help='Cellular interface for opt-in default-route failover')
    parser.add_argument('--primary-route-metric', type=int, default=10)
    parser.add_argument('--cellular-route-metric', type=int, default=220)
    args = parser.parse_args(argv)
    direct_options = (args.primary_target, args.secondary_target, args.port,
                      args.name, args.record_type, args.timeout)
    if not args.method and not args.test_config:
        parser.error('--method or --test-config is required')
    if args.method == 'interface-status':
        if any(value is not None for value in direct_options):
            parser.error('interface-status cannot be combined with probe options')
        if args.watch:
            parser.error('interface-status cannot be used with --watch')
        args.method = None
    if not args.method and any(value is not None for value in direct_options):
        parser.error('Direct target and probe options require --method and cannot be used with --test-config')
    if args.watch and not (args.test_config or args.method):
        parser.error('--watch requires --method or --test-config')
    if args.test_config or args.method:
        if len(args.interfaces) != 1:
            parser.error('Path tests require exactly one explicit interface')
        try:
            if args.test_config:
                with open(args.test_config) as config_file:
                    config = json.load(config_file)
                if not isinstance(config, list) or not 1 <= len(config) <= 32:
                    raise ValueError('Configure between 1 and 32 path tests')
                tests = [PathTest(**item) for item in config]
            else:
                if not args.primary_target:
                    raise ValueError('--method requires --primary-target')
                if args.port is not None and args.method not in ('tcp', 'dns'):
                    raise ValueError('--port is only supported for TCP and DNS')
                if args.method == 'tcp' and args.port is None:
                    raise ValueError('TCP tests require --port')
                if args.method == 'dns' and not args.name:
                    raise ValueError('DNS tests require --name')
                if args.method != 'dns' and (args.name is not None or args.record_type is not None):
                    raise ValueError('--name and --record-type are only supported for DNS')
                targets = [args.primary_target]
                if args.secondary_target is not None:
                    targets.append(args.secondary_target)
                tests = [PathTest(args.method, target, port=args.port, name=args.name,
                                  timeout=15.0 if args.timeout is None else args.timeout,
                                  record_type='A' if args.record_type is None else args.record_type)
                         for target in targets]
            monitor = PathMonitor(args.policy, args.failure_threshold,
                                  args.recovery_threshold)
            if args.failover_interface and not args.watch:
                parser.error('--failover-interface requires --watch')
            failover = (RouteFailover(args.interfaces[0], args.failover_interface,
                                       primary_metric=args.primary_route_metric,
                                       cellular_metric=args.cellular_route_metric)
                        if args.failover_interface else None)
            return (_monitor_cli(args, tests, monitor, failover)
                    if failover is not None else _monitor_cli(args, tests, monitor))
        except (OSError, ValueError, TypeError) as error:
            parser.error(str(error))
    interfaces = args.interfaces
    if not interfaces:
        try:
            interfaces = discover_interfaces()
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
            print(f'Interface discovery failed: {error}', file=sys.stderr)
            return 1
        if not interfaces:
            print('No interfaces found.', file=sys.stderr)
            return 1
    failed = False
    print('Interface status')
    print('INTERFACE  ADMIN  LINK             ADDRESS  ROUTE    RESULT  REASON')
    def label(value):
        return 'UNKNOWN' if value is None else ('YES' if value else 'NO')
    for interface in interfaces:
        try:
            status = check_interface_status(interface, family=args.family, table=args.table)
        except ValueError as error:
            parser.error(str(error))
        failed |= not status.passed
        print(f'{interface:<10} {label(status.admin_up):<6} '
              f'{status.link_state:<16} {label(status.address_ready):<8} '
              f'{label(status.route_ready):<8} '
              f'{"PASS" if status.passed else "FAIL":<7} {status.reason}')
    return int(failed)


def _format_path_test_table(interface, state, policy, failures, successes,
                            results, timestamp=None):
    """Format one monitoring round as a plain-text table."""
    timestamp = timestamp or time.strftime('%Y-%m-%d %H:%M:%S')
    headers = ('TIME', 'INTERFACE', 'STATE', 'POLICY', 'FAILURES', 'SUCCESSES',
               'METHOD', 'TARGET', 'RESULT', 'REASON')
    rows = [
        (timestamp, interface, state, policy, str(failures), str(successes),
         result.method.upper(), result.target,
         'PASS' if result.passed else 'FAIL', result.reason)
        for result in results
    ]
    widths = [max(len(header), *(len(row[index]) for row in rows))
              for index, header in enumerate(headers)]

    def format_row(row):
        return '  '.join(value.ljust(widths[index])
                         for index, value in enumerate(row)).rstrip()

    return '\n'.join((format_row(headers),
                      format_row(tuple('-' * width for width in widths)),
                      *(format_row(row) for row in rows)))


def _monitor_cli(args, tests, monitor, failover=None):

    interface = args.interfaces[0]
    try:
        while True:
            started = time.monotonic()
            status = check_interface_status(interface, family=args.family, table=args.table)
            if status.passed:
                results = run_path_tests(interface, tests)
            else:
                results = [ProbeResult(interface, test.method, test.target,
                                       False, status.reason) for test in tests]
            up = monitor.update(results, interface_ready=status.passed)
            if failover is not None and up is not None:
                failover.apply(up)
            state = 'UNKNOWN' if up is None else ('UP' if up else 'DOWN')
            print(_format_path_test_table(
                interface, state, args.policy, monitor.failures,
                monitor.successes, results), flush=True)
            if not args.watch:
                passed = (any if args.policy == 'any' else all)(r.passed for r in results)
                return int(not passed)
            # Never overlap rounds when a probe takes longer than the interval.
            time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
