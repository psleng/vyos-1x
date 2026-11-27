"""Entry point for ``dial-on-demand@<wwanN>:<idle>:<connect>:<flush>.service``.

Argument parsing and signal handling only; the long-running state machine
lives in ``DemandDaemon`` (docs/igos-wwan-dial-on-demand-design.md §10).
"""

import argparse
import asyncio
import logging
import re
import signal
import sys

from vyos.utils.wwan.dial_on_demand.dial_daemon import DemandDaemon

logger = logging.getLogger(__name__)
# Do NOT embed %(asctime)s here: journald already timestamps every line (the
# unit logs to the journal) in the system's local timezone, like every other
# VyOS service. A service process runs with TZ unset (UTC), so a Python-rendered
# asctime would show UTC and look "wrong" next to journald's local timestamp.
logging.basicConfig(level=logging.INFO,
                    format="%(module)s: %(message)s")

_INTERFACE_RE = re.compile(r'^wwan\d+$')


def parse_params(params):
    """Parse the ``wwanN[:idle[:connect[:flush]]]`` instance string."""
    items = params.split(':')
    if len(items) > 4:
        raise ValueError(f'Too many fields in instance name {params!r}')
    interface = items[0]
    if not _INTERFACE_RE.match(interface):
        raise ValueError(f'Invalid WWAN interface {interface!r}')
    idle_timeout = int(items[1]) if len(items) > 1 else 30
    connect_timeout = int(items[2]) if len(items) > 2 else 30
    # flush-timeout is optional so an older 3-field instance name still starts.
    flush_timeout = int(items[3]) if len(items) > 3 else 60
    return interface, idle_timeout, connect_timeout, flush_timeout


async def main(interface='wwan0', timeout=30, connect_timeout=30,
               flush_timeout=60):
    """Run the dial-on-demand daemon until SIGTERM.  Returns the exit code."""
    daemon = DemandDaemon(interface=interface, idle_timeout=timeout,
                          connect_timeout=connect_timeout,
                          flush_timeout=flush_timeout)
    loop = asyncio.get_running_loop()

    def handle_signal(signum):
        logger.info('Received %s; stopping dial-on-demand on %s',
                    signal.Signals(signum).name, interface)
        daemon.request_stop()

    loop.add_signal_handler(signal.SIGTERM, handle_signal, signal.SIGTERM)
    try:
        return await daemon.run()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="dial-on-demand",
        description="WWAN dial-on-demand daemon")
    parser.add_argument('params',
                        help="instance string wwanN[:idle[:connect[:flush]]]")
    args = parser.parse_args()
    try:
        iface, idle, connect, flush = parse_params(args.params)
    except ValueError as exc:
        parser.error(str(exc))

    sys.exit(asyncio.run(main(interface=iface, timeout=idle,
                              connect_timeout=connect,
                              flush_timeout=flush)))
