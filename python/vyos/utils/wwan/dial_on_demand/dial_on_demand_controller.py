import vyos.utils.wwan.dial_on_demand.idle_wwan_watcher as idle_wwan_watcher
import vyos.utils.wwan.dial_on_demand.wwan_nft_reconnect2 as wwan_nft_reconnect
import vyos.utils.wwan.dial_on_demand.install_wwan_nft_rules as nft_rules
import argparse
import asyncio


async def main(interface='wwan0', timeout=30, connect_timeout=30):
    """"""
    print("Start idle wwan watcher task")
    idle_task = await idle_wwan_watcher.main(interface=interface, timeout=timeout, connect_timeout=connect_timeout)

    nft_rules.generate_nft_rules(interface)
    print("Start wwan reconnect task")
    reconnect_task = await wwan_nft_reconnect.main(interface=interface,timeout=timeout, connect_timeout=connect_timeout)



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

    asyncio.run(main(interface=interface,timeout=timeout,connect_timeout=connect_timeout))
