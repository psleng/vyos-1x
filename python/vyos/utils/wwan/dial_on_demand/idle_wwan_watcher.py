import time
import argparse
import subprocess
import re
import asyncio

from vyos.utils.wwan.wwan_client import (  # noqa: E402
    WWANClient
)

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

async def main(interface='wwan0',timeout=30, connect_timeout=30, client=None):


    print(interface)
    print(timeout)
    timeout = int(timeout)

    last_tx = get_tx_bytes(interface)
    last_active = int(time.time())

    modem_mapping = get_modem_mapping()
    close_client = False
    #poll
    if client is None:
        client = WWANClient()
        await client.open()
        await client.add_interface(int(interface[4:]))
        config = await client.set_configuration(int(interface[4:]), {
                "connection_mode": "dial-on-demand",
                "primary_sim_slot": 1,
            })
        close_client = True

    while True:
        time.sleep(timeout)
        tx = get_tx_bytes(interface)
        if last_tx != tx:
            print("idle-wwan-watcher: still detecting packets")
            print("tx vs last tx: ", tx, last_tx)
            last_tx = tx
            last_active = int(time.time())
        else:
            now = int(time.time())
            idle = now - last_active
            if idle > timeout:
                print(f"idle-wwan-watcher: idle for: {timeout}")
                disconnect = await client.disconnect_bearer(int(interface[4:]))
                print(disconnect)
                wait_disconnect = await client.wait_for_bearer(int(interface[4:]), "disconnected", timeout=60)
                print(wait_disconnect)
                print(await client.get_bearer_status(int(interface[4:])))
                last_tx = 0
                last_active = int(time.time())
                break
    if close_client:
        await client.close()

if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="idle-wwan-watcher", description="Watches wwan for inactivity", epilog="Test")
    parser.add_argument('interface',nargs="?", default="wwan0")
    parser.add_argument('timeout',nargs="?", default=60, type=int)
    args = parser.parse_args()
    interface = args.interface
    timeout = args.timeout
    if ":" in args.interface:
        interface = args.interface.split(":")[0]
        timeout = args.interface.split(":")[1]
        connect_timeout = args.interface.split(":")[2]

    asyncio.run(main(interface=interface, timeout=timeout, connect_timeout=connect_timeout))
