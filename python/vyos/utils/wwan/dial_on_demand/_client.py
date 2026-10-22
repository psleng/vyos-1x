import asyncio
import logging

from vyos.utils.wwan.wwan_client import (
    WWANClient,
    WWANConnectionError,
)

logger = logging.getLogger(__name__)


async def open_client_with_retry(interface, connect_timeout=20.0, poll=0.5):
    """Open a WWANClient and register the interface, retrying until the WWAN FSM
    manager (igos-wwan-manager) has claimed its D-Bus name.

    The dial-on-demand service can be scheduled before the manager has finished
    claiming ``com.igos.IgosModemManager`` on the bus -- ``systemctl start``
    returns while the manager is still booting. ``open()`` / ``add_interface()``
    raise ``WWANConnectionError`` in that window, so retry the whole connect up
    to ``connect_timeout`` seconds instead of letting the controller exit
    non-zero (mirrors interfaces_wwan.py ``_apply_via_dbus``).

    Returns an opened, interface-registered WWANClient. Raises
    ``WWANConnectionError`` if the manager never becomes reachable before the
    deadline.
    """
    ifnum = int(interface[4:])
    deadline = asyncio.get_event_loop().time() + connect_timeout
    while True:
        client = WWANClient()
        try:
            await client.open()
            await client.add_interface(ifnum)
            return client
        except WWANConnectionError as exc:
            await client.close()
            if asyncio.get_event_loop().time() >= deadline:
                raise
            logger.info(
                'WWAN manager not ready yet (%s); retrying connect for %s',
                exc, interface)
            await asyncio.sleep(poll)
