import vyos.utils.wwan.dial_on_demand.idle_wwan_watcher as idle_wwan_watcher
import vyos.utils.wwan.dial_on_demand.wwan_nft_reconnect as wwan_nft_reconnect
import vyos.utils.wwan.dial_on_demand.install_wwan_nft_rules as nft_rules
import subprocess
import re
import argparse
import asyncio
import signal
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(module)s: %(message)s")

def cleanup(interface='wwan0'):
    pattern = re.compile(rf"^table\s+(\S+)\s+({interface}+_raw_\d+)$")
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
    try:
        await idle_wwan_watcher.main(interface=interface, timeout=timeout)
        await nft_rules.generate_nft_rules(interface=interface)
        await wwan_nft_reconnect.main(interface=interface, connect_timeout=connect_timeout)
    except asyncio.CancelledError:
        raise
    finally:
        logger.info("Finished service successfully!")


async def main(interface='wwan0', timeout=30, connect_timeout=30):
    """"""
    current_task = asyncio.create_task(run(interface=interface, timeout=timeout, connect_timeout=connect_timeout))
    def handle_sigterm():
        logger.info("Received signal termination. Stopping service...")
        current_task.cancel()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_sigterm)

    try:
        await current_task

    except asyncio.CancelledError:
        pass
    finally:
        # should always cleanup
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

    asyncio.run(main(interface=interface,timeout=timeout,connect_timeout=connect_timeout))
