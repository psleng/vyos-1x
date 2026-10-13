import time
import argparse
import subprocess
import re
import asyncio
import logging

from vyos.utils.wwan.dial_on_demand._client import open_client_with_retry

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
    # When the controller hands us a shared client we must NOT close it -- the
    # owner (dial_on_demand_controller.run) closes it once for the whole
    # pipeline. Only a client we open ourselves (standalone invocation) is ours
    # to close.
    owns_client = client is None
    try:
        if client is None:
            client = await open_client_with_retry(interface)
        await idler(interface=interface, timeout=timeout, client=client)
    except asyncio.CancelledError:
        raise
    finally:
        if owns_client and client is not None:
            await client.close()


async def idler(interface='wwan0', timeout=30, client=None):

    logger.info(f"Given interface: {interface}")
    logger.info(f"Given idle-timeout: {timeout}")
    timeout = int(timeout)

    ifnum = int(interface[4:])

    # The idle watcher only has a job when the bearer is CONNECTED: wait for it
    # to go idle, then disconnect it so dial-on-demand can re-arm. If the bearer
    # is already down there is nothing to idle-out -- return immediately so the
    # caller arms the dial trigger right away.
    #
    # Watching tx_bytes while the bearer is down is actively harmful: traffic the
    # router pushes at a DOWN wwan interface (e.g. forwarded pings after an eth
    # failover) still increments the netdev tx counter even though the modem
    # drops it, so the idle timer would keep resetting and the bearer would never
    # be (re)dialed -- exactly the "pings keep it from coming up" symptom.
    if client is not None and await client.get_bearer_status(ifnum) != "connected":
        logger.info("Bearer already disconnected; not idling, arm dial trigger now")
        return

    last_tx = get_tx_bytes(interface)
    last_active = int(time.time())

    # poll
    while True:
        await asyncio.sleep(timeout)

        # If the bearer dropped out from under us (idle elsewhere, signal loss,
        # etc.), stop idling and let the dial trigger be re-armed instead of
        # waiting on tx that will never settle while packets hit the down link.
        if client is not None and await client.get_bearer_status(ifnum) != "connected":
            logger.info("Bearer no longer connected; arming dial trigger")
            break
        tx = get_tx_bytes(interface)
        logger.info(f"tx vs last tx: {tx}, {last_tx}")
        if last_tx != tx:
            logger.info("Still detecting packets")
            logger.info(f"tx vs last tx: {tx}, {last_tx}")
            last_tx = tx
            last_active = int(time.time())
            continue

        now = int(time.time())
        idle = now - last_active
        if idle > timeout:
            logger.info(f"idled for: {timeout}")
            await client.disconnect_bearer(int(interface[4:]))
            wait_disconnect = await client.get_bearer_status(ifnum)
            logger.info(f"Have we successfully disconnected? {wait_disconnect}")
            break

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
