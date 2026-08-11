import vyos.utils.wwan.dial_on_demand.idle_wwan_watcher as idle_wwan_watcher
import vyos.utils.wwan.dial_on_demand.wwan_nft_reconnect as wwan_nft_reconnect
import vyos.utils.wwan.dial_on_demand.install_wwan_nft_rules as nft_rules
import subprocess
import re
import argparse
import asyncio
import signal

def cleanup(interface='wwan0'):
    pattern = re.compile(r"^table\s+(\S+)\s+(wwan\d+_raw_\d+)$")
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

async def shutdown_raise(shutdown_event):
    await shutdown_event.wait()
    raise asyncio.CancelledError()


async def main(interface='wwan0', timeout=30, connect_timeout=30):
    """"""
    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(shutdown_raise(shutdown_event))
            tg.create_task(idle_wwan_watcher.main(interface=interface, timeout=timeout))
            tg.create_task(nft_rules.generate_nft_rules(interface))
            tg.create_task(wwan_nft_reconnect.main(interface=interface, connect_timeout=connect_timeout))
    except asyncio.CancelledError:
        pass
    finally:
        # should always cleanup
        cleanup(interface=interface)



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
