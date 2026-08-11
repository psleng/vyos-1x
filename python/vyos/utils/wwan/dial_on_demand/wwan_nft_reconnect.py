#!/usr/bin/env python3
#
# NFQUEUE-based on-demand WWAN trigger for Debian Bookworm
#
# When outbound traffic to wwan0 is detected and the modem is not connected,
# this script uses ModemManager to bring it up on demand.

from netfilterqueue import NetfilterQueue
import subprocess
import re
import asyncio
import argparse

from vyos.utils.wwan.wwan_client import (  # noqa: E402
    WWANClient
)

COOLDOWN = 60           # seconds between connection attempts
CONNECT_TIMEOUT = 20    # seconds to wait for modem to connect

shutdown_event = asyncio.Event()

packet_queue = asyncio.Queue(maxsize=1024)
modem_ready_event = asyncio.Event()
dhcp_success_event = asyncio.Event()
renew_dhcp_event = asyncio.Event()
dhcp_done_event = asyncio.Event()
accept_packet_event = asyncio.Event()

# async function wrapper for subprocess commands with a timeout
async def run_cmd(*cmd, timeout=CONNECT_TIMEOUT):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)

    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "timeout"
    #print(stdout.decode())
    return proc.returncode, stdout.decode(), stderr.decode()

# returns dict {interface: modem_id}

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

# async function to delete the nft netfilter queue rules
async def teardown_nftables(queue, interface='wwan0'):
    nft_table = f'{interface}_raw_{queue}'
    proc = await asyncio.create_subprocess_exec("nft", "delete", "table", "inet", nft_table,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await proc.communicate()
    except:
        proc.kill()
        await proc.communicate()
        return -1, "", "exception"
    #print(stdout.decode())
    if proc.returncode == 0:
        return True
    return False


def event_loop(loop=None):
    if loop is None:
        return asyncio.get_event_loop()
    else:
        return loop
# This function acts as the packet consumer.

# This acts as the packet producer
class PacketHandler:
    def __init__(self, interface='wwan0', connect_timeout=30, loop=None, client=None):
        self.interface = interface
        self.connect_timeout = connect_timeout
        self.modem_id = get_all_wwan_options()[self.interface]
        self.queue_num = get_interface_queue_num(self.interface)
        self.client = client
        self.tasks = set()
        self.shutdown = False
        self.close_client_needed = False
        if client is None:
            self.close_client_needed = True
        if loop is None:
            loop = event_loop(loop)
        self.loop = loop
        self.modem_bringup_started = False

    # close all the tasks
    async def cleanup_tasks(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.client is None:
            return
        if self.close_client_needed:
            await self.client.close()

    # setup
    async def setup(self):
        print('setup called')
        if self.client is None:
            client = WWANClient()
            await client.open()
            print(client)
            setup = await client.add_interface(int(self.interface[4:]))
            print(setup)
            config = await client.set_configuration(int(self.interface[4:]), {
                "connection_mode": "dial-on-demand",
                "primary_sim_slot": 1,
            })
            self.client = client
        return self

    async def packet_consumer(self, queue_num):

        await modem_ready_event.wait()

        while not packet_queue.empty():
            pkt = await packet_queue.get()
            print(f"Packet received and accepting from queue: {pkt}")
            pkt.accept()

        await teardown_nftables(queue_num, self.interface)

        self.loop.call_soon_threadsafe(shutdown_event.set)


    # packet handler wrapper
    def handle_packet(self, packet):
        if self.shutdown:
            packet.accept()
            return
        packet.retain()
        task = self.loop.create_task(self.handle_packet_async(packet))
        self.tasks.add(task)

    # async packet handler
    async def handle_packet_async(self, packet):
        print(f"Received packet: {packet}")

        #if await modem_connected(self.modem_id):
        print(f"Modem id is: {self.modem_id}")

        if shutdown_event.is_set() or modem_ready_event.is_set():
            print(f"Packet received and accepting from callback: {packet}")
            packet.accept()
            return
        # enqueue the packet into an async queue (the async queue is not thread safe)
        try:
            self.loop.call_soon_threadsafe(packet_queue.put_nowait, packet)
            print(f"packet queued: {packet}")
        except asyncio.QueueFull:
            print("queue full → dropping")
            packet.drop()
            return
        except Exception:
            packet.drop()
            return


        # first case if modem is already connected, set the modem event to ready if it's not already set

        # second case if modem is not connected, bring up the modem ONLY if it hasn't been attempted already
        # in this case, we also bring up the modem_ready_event
        #if not modem_bringup_started:
        #    modem_bringup_started = True
        # replace the bring_modem_up, wait_for_modem for appropriate functions
        if self.client is None:
            return
        #print(self.connect_timeout)
        #async with WWANClient() as client:

        #print(await self.client.get_bearer_status(int(self.interface[4:])))
        #print(await self.client.get_status(int(self.interface[4:])))
        if await self.client.get_bearer_status(int(self.interface[4:])) == "connected" and not modem_ready_event.is_set():
            print("If modem is connected, set modem_ready_event")
            self.loop.call_soon_threadsafe(modem_ready_event.set)
            return

        if not self.modem_bringup_started:
            self.modem_bringup_started = True
            if await self.client.connect_bearer(int(self.interface[4:])) == 'accepted':
                print('connect_bearer started')
                result = await self.client.wait_for_bearer(int(self.interface[4:]), "connected", timeout=float(self.connect_timeout))
                print("result is: ", result)
                if result == True:
                    self.loop.call_soon_threadsafe(modem_ready_event.set)
                    #await renew_dhcp(self.interface)
        #if not dhcp_success_event.is_set() and modem_bringup_started:
        #    success, logs = await renew_dhcp("wwan0", timeout=15)
        #    if success:
        #        print("DHCP lease acquired (DHCPACK seen)")
        #        loop.call_soon_threadsafe(dhcp_success_event.set)
        #    else:
        #        print("Timeout or failed to get DHCPACK")


# renew dhcp to assign ip again

async def renew_dhcp(interface):
    #Release any old DHCP lease and request a new one.
    print(f"[WWAN] Renewing DHCP lease on {interface}...", flush=True)
    code, out, err = await run_cmd("dhclient", "-r", interface)
    code, out, err = await run_cmd("dhclient", "-v", interface)
    if code == 0:
        print(f"[WWAN] DHCP renewal complete on {interface}.", flush=True)
        print(f"DHCP success output: {out}")
    else:
        print(f"[WWAN] DHCP renewal failed on {interface}.", flush=True)
        print(f"DHCP error output: {err}")

    code, out, err = await run_cmd("dhclient", "-r", interface)
    code, out, err = await run_cmd("dhclient", "-v", interface)

    #loop.call_soon_threadsafe(dhcp_success_event.set)

    return True


# -------------------- Main --------------------


def get_all_wwan_options():
    "Map the modem index to the corresponding wwan"
    result = subprocess.run(["cli-shell-api", "showConfig", "--show-active-only", "--show-commands"], capture_output=True, text=True)
    result = result.stdout.replace("'", "")
    wwan_to_check = get_modem_mapping()
    final_wwan = {}
    if "load-balancing" in result:
        values = re.findall(r"inbound-interface\s+(\S+)", result)

        if 'any' in values:
            final_wwan = wwan_to_check
        else:
            ignore_wwan = {}
            for i in values:
                print(i)
                if "wwan" in i:
                    ignore_wwan.append(i)
            final_wwan = {k: v for k, v in wwan_to_check.items() if k in ignore_wwan}
    else:
        final_wwan = wwan_to_check
    return final_wwan

# for the interface, get the netfilter queue number
def get_interface_queue_num(interface):

    result = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True, check=True)
    matches = re.findall(rf"table\s+\S+\s+({interface}_raw_\d+)", result.stdout)
    pattern = r'oifname\s+"(?P<oifname>\S+)"\s+queue\s+to\s+(?P<queue>\d+)'
    for tables in matches:
        table_info = subprocess.run(['nft', 'list', 'table', 'inet', tables], capture_output=True, text=True, check=True)

        wwan_matches = re.finditer(pattern, table_info.stdout)
        for m in wwan_matches:
           if m.group("oifname") == interface:
               return int(m.group("queue"))
    return None

def start_nfqueue(nfqueue):
    nfqueue.run(block=False)


async def main(interface='wwan0', connect_timeout=30, loop=None, client=None):

    queue_num = get_interface_queue_num(interface)
    modem_index = get_all_wwan_options()
    if modem_index.get(interface) != None:
        modem_index = modem_index.get(interface)
    else:
        return

    print(connect_timeout)

    print(queue_num)
    print(modem_index)
    if queue_num is None:
        return

    nfqueue = NetfilterQueue()
    fd = nfqueue.get_fd()
    loop = event_loop(loop)

    try:
        handler = PacketHandler(interface, connect_timeout, loop=loop, client=client)
        if client is None:
            await handler.setup()
        # Bind to queue number 1
        nfqueue.bind(queue_num, handler.handle_packet)

        #nfqueue.run()
        # Use file descriptor in an async way
        fd = nfqueue.get_fd()
        #print(f"Listening on NFQueue fd {fd}")

        loop.add_reader(fd, start_nfqueue, nfqueue)

        print("Worker task set")
        worker_task = loop.create_task(handler.packet_consumer(queue_num))

        # We wait for the worker task to tell us to stop.
        print('worker task created')
        await shutdown_event.wait()
        print('finished tasks and closing dbus')

        #await handler.removed_event.wait()
        #loop.run_forever()
    finally:
        handler.shutdown = True
        loop.remove_reader(fd)
        worker_task.cancel()
        await handler.cleanup_tasks()
        nfqueue.unbind()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="nfqueue-bound-monitor", description="binds wwanX to nfqueue", epilog="Test")
    parser.add_argument('params',help="colon separated parameter string")
    args = parser.parse_args()
    params = args.params
    items = params.split(':')
    if len(items) > 2:
        raise ValueError("Too many arguments for this function")
    interface, timeout = items

    loop = event_loop(None)
    try:
        loop.run_until_complete(main(interface=interface, connect_timeout=timeout, loop=loop))
    finally:
        loop.close()
