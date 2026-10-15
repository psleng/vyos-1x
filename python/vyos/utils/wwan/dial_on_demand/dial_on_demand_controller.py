import argparse
import asyncio
import signal
import logging
import subprocess
import re
import vyos.utils.wwan.dial_on_demand.idle_wwan_watcher as idle_wwan_watcher
import vyos.utils.wwan.dial_on_demand.wwan_nft_reconnect as wwan_nft_reconnect
import vyos.utils.wwan.dial_on_demand.install_wwan_nft_rules as nft_rules
from vyos.utils.wwan.dial_on_demand._client import open_client_with_retry

logger = logging.getLogger(__name__)
# Do NOT embed %(asctime)s here: journald already timestamps every line (the
# unit logs to the journal) in the system's local timezone, like every other
# VyOS service. A service process runs with TZ unset (UTC), so a Python-rendered
# asctime would show UTC and look "wrong" next to journald's local timestamp.
logging.basicConfig(level=logging.INFO,
                    format="%(module)s: %(message)s")

def cleanup(interface='wwan0'):
    pattern = re.compile(rf"^table\s+(\S+)\s+({re.escape(interface)}+_raw_\d+)$")
    result = subprocess.run(
        ["nft", "list", "tables"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        family = m.group(1)
        table = m.group(2)
        subprocess.run(
            ["nft", "delete", "table", family, table],
            check=True
        )

async def run(interface='wwan0', timeout=30, connect_timeout=30):
    # Open ONE WWAN D-Bus client for the whole pipeline and hand it to every
    # stage, instead of each stage opening and closing its own. open_client_with_
    # retry() waits for the FSM manager (igos-wwan-manager) to claim its bus name,
    # so the single connect also absorbs the boot race. The client is closed once
    # here when the pipeline ends.
    client = await open_client_with_retry(interface)
    try:
        # Apply the dial-on-demand connection mode ONCE on the shared client.
        # (Each stage used to re-apply it on its own client, and every apply
        # makes the FSM fire a RECONFIGURE that bounces a connected bearer.)

        logger.info("Starting idle wwan watcher program.")
        await idle_wwan_watcher.main(interface=interface, timeout=timeout,
                                     client=client)
        logger.info("Finished idle wwan watcher program.")
        logger.info("Starting installation of nft rules.")
        await nft_rules.generate_nft_rules(interface=interface)
        logger.info("Finished installing nft rules.")
        logger.info("Starting nfqueue monitor.")
        await wwan_nft_reconnect.main(interface=interface,
                                     connect_timeout=connect_timeout,
                                     client=client)
        logger.info("Finished nfqueue monitor.")
    except asyncio.CancelledError:
        raise
    finally:
        await client.close()
        logger.info("Finished service successfully!")


async def main(interface='wwan0', timeout=30, connect_timeout=30, loop=None):
    """Run the dial-on-demand pipeline and clean up nft state on stop."""
    if loop is None:
        loop = asyncio.get_event_loop()
    current_task = asyncio.create_task(run(interface=interface, timeout=timeout, connect_timeout=connect_timeout))
    def handle_sigterm():
        logger.info("Received signal termination. Stopping service...")
        current_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, handle_sigterm)

    try:
        await current_task
    except asyncio.CancelledError:
        logger.info("Received a termination signal.")
    finally:
        # Always remove leftover nftables tables for this interface.
        cleanup(interface=interface)
        loop.remove_signal_handler(signal.SIGTERM)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="nfqueue-bound-monitor", description="binds wwanX to nfqueue", epilog="Test")
    parser.add_argument('params',help="colon separated parameter string")
    args = parser.parse_args()
    params = args.params
    items = params.split(':')
    if len(items) > 3:
        raise ValueError("Too many arguments for this function")
    interface, timeout, connect_timeout = items

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(interface=interface,timeout=timeout,connect_timeout=connect_timeout, loop=loop))
    finally:
        loop.close()
