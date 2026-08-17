import time
import argparse
import subprocess
import re
import asyncio
import logging

from vyos.utils.wwan.wwan_client import (  # noqa: E402
    WWANClient
)

logger = logging.getLogger(__name__)

def get_tx_bytes(iface):
    try:
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes", 'r') as f:
            return f.read().strip()
    except IOError:
        return "0"

def get_modem_mapping():
    result = subprocess.run(["mmcli", "-L"], capture_output=True, text=True)
    modems = re.findall(r'Modem/(\d+)', result.stdout)

    mapping = {}

    for modem_index in modems:
        info = subprocess.run(
            ["mmcli", "-m", modem_index],
            capture_output=True,
            text=True
        ).stdout

        ports = re.findall(r'(wwan\d+)\s+\(net\)', info)
        for port in ports:
            mapping[port] = int(modem_index)

    return mapping

def is_modem_connected(modem_id):
    "check if modem is connected"
    """Check modem connection status via mmcli key-value output."""
    result = subprocess.run(
        ["mmcli", "--output-keyvalue", "-m", modem_id],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if line.strip().startswith("modem.generic.state"):
            return "connected" in line.lower()
    return False

async def main(interface='wwan0',timeout=30, client=None):
    try:
        if client is None:
            client = WWANClient()
            await client.open()
            await client.add_interface(int(interface[4:]))
            await client.set_configuration(int(interface[4:]), {
                    "connection_mode": "dial-on-demand",
                    "primary_sim_slot": 1,
                })
        await idler(interface=interface, timeout=timeout, client=client)
    except asyncio.CancelledError:
        raise
    finally:
        if client:
            await client.close()


async def idler(interface='wwan0',timeout=30, client=None):

    logger.info(f"Given interface: {interface}")
    logger.info(f"Given idle-timeout: {timeout}")
    timeout = int(timeout)

    last_tx = get_tx_bytes(interface)
    last_active = int(time.time())

    #poll

    while True:
        await asyncio.sleep(timeout)
        tx = get_tx_bytes(interface)
        if last_tx != tx:
            logger.info("Still detecting packets")
            logger.info(f"tx vs last tx: {tx}, {last_tx}")
            last_tx = tx
            last_active = int(time.time())
        else:
            now = int(time.time())
            idle = now - last_active
            if idle > timeout:
                logger.info(f"idled for: {timeout}")
                await client.disconnect_bearer(int(interface[4:]))
                wait_disconnect = await client.wait_for_bearer(int(interface[4:]), "disconnected", timeout=60)
                logger.info(f"Have we successfully disconnected? {wait_disconnect}")
                last_tx = 0
                last_active = int(time.time())
                break
    if client:
        await client.close()

if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="idle-wwan-watcher", description="Watches wwan for inactivity", epilog="Test")
    parser.add_argument('params',help="colon separated parameter string")
    args = parser.parse_args()
    params = args.params
    items = params.split(':')
    if len(items) > 2:
        raise ValueError("Too many arguments for this function")
    interface, timeout = items

    asyncio.run(main(interface=interface, timeout=timeout))
