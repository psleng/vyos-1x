import vyos.utils.wwan.dial_on_demand.idle_wwan_watcher as idle_wwan_watcher
import vyos.utils.wwan.dial_on_demand.wwan_nft_reconnect2 as wwan_nft_reconnect
import argparse
import asyncio
from vyos.utils.wwan.wwan_client import (  # noqa: E402
    WWANClient,
    WWANError,
    WWANConfigError,
    WWANConnectionError,
)


async def main(interface='wwan0', timeout=30, connect_timeout=30, loop=None):
    """"""
    print("Start idle wwan watcher task")
    client = WWANClient()
    await client.open()
    await client.add_interface(int(interface[4:]))
    config = await client.set_configuration(int(interface[4:]), {
            "connection_mode": "dial-on-demand",
            "primary_sim_slot": 1,
        })

    idle_task = await idle_wwan_watcher.main(interface=interface, timeout=timeout, connect_timeout=connect_timeout, client=client)
    print("Start wwan reconnect task")
    reconnect_task = await wwan_nft_reconnect.main(interface=interface,timeout=timeout, connect_timeout=connect_timeout, loop=loop, client=client)
    print(await client.get_status(int(interface[4:])))
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="nfqueue-bound-monitor", description="binds wwanX to nfqueue", epilog="Test")
    parser.add_argument('interface',nargs="?", default="wwan0")
    args = parser.parse_args()

    print(args.interface)
    interface = args.interface
    timeout = 30
    connect_timeout = 30
    if ":" in args.interface:
        interface = args.interface.split(":")[0]
        timeout = args.interface.split(":")[1]
        connect_timeout = args.interface.split(":")[2]

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(interface=interface,timeout=timeout,connect_timeout=connect_timeout,loop=loop))
    finally:
        loop.close()
    #asyncio.run(main(interface=interface,timeout=timeout,connect_timeout=connect_timeout))
    #asyncio.run(idle_wwan_watcher.main(interface=interface, timeout=timeout, connect_timeout=connect_timeout))
