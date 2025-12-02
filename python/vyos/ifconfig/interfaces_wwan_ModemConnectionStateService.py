from dbus_next.service import ServiceInterface
from dbus_next.aio import MessageBus
from dbus_next import BusType
from dbus_next.signature import Variant
from automaton import machines

import asyncio, logging, sys
from typing import Literal


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler(sys.stdout) # TODO: change this to other stream
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

def _log_properties(properties, indent=0):
    """Recursively log properties with proper indentation, unwrapping Variant types."""
    for key, value in properties.items():
        value = value.value if isinstance(value, Variant) else value
        if isinstance(value, dict):
            logger.debug(" " * indent + f"{key}:")
            _log_properties(value, indent + 4)
        elif isinstance(value, list):
            logger.debug(" " * indent + f"{key}: [")
            for item in value:
                item = item.value if isinstance(item, Variant) else item
                if isinstance(item, dict):
                    _log_properties(item, indent + 4)
                else:
                    logger.debug(" " * (indent + 4) + f"{item}")
            logger.debug(" " * indent + "]")
        else:
            logger.debug(" " * indent + f"{key}: {value}")

state_machine = [
    {
        "name" : "Unavailable",
        "next_states": {
            'next': 'Disconnected'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name": "Disconnected",
        "next_states": {
            'next': 'Prepare'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Need Authentication",
        "next_states": {
            'next': 'Prepare',
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Prepare",
        "next_states": {
            'next': 'Wait for SIM'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Wait for SIM",
        "next_states": {
            'next': 'Unlock'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Unlock",
        "next_states": {
            'next': 'Wait for Ready',
            'next2': 'Need Authentication'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Wait for Ready",
        "next_states": {
            'next': 'Initial EPS Bearer'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Initial EPS Bearer",
        "next_states": {
            'next': 'Connect'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name": "Connect",
        "next_states": {
            'next': 'Last'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name": "Last",
        "next_states": {
            'next': 'Activated'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Activated",
        "next_states": {
            'next': 'Deactivating'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name": "Deactivating",
        "next_states": {
            'next': 'Disconnected'
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name": "Failed",
        "next_states": {
            'next': 'Unavailable',
            'next2': 'Disconnected'
        },
        "on_enter": None,
        "on_exit": None
    }
]

# state machine function definitions:
def on_enter_unavailable(self):
    logger.debug("Entering Unavailable state")
    self.state_machine.transition('next')

def on_enter_disconnected(self):
    logger.debug("Entering Disconnected state")
    self.state_machine.transition('next')

def on_enter_prepare(self):
    logger.debug("Entering Prepare state")

    self.state_machine.transition('next')

def on_enter_wait_for_sim(self):
    logger.debug("Entering Wait for SIM state")
    self.state_machine.transition('next')

def on_enter_unlock(self):
    logger.debug("Entering Unlock state")
    self.state_machine.transition('next')

def on_enter_wait_for_ready(self):
    logger.debug("Entering Wait for Ready state")
    self.state_machine.transition('next')

def on_enter_initial_eps_bearer(self):
    logger.debug("Entering Initial EPS Bearer state")
    self.state_machine.transition('next')

def on_enter_connect(self):

    logger.debug("Entering Connect state")
    self.state_machine.transition('next')

def on_enter_last(self):
    logger.debug("Entering Last state")
    self.state_machine.transition('next')

def on_enter_activated(self):
    logger.debug("Entering Activated state")
    # Stay in Activated state until a disconnect is requested

def on_enter_deactivating(self):
    logger.debug("Entering Deactivating state")
    self.state_machine.transition('next')

def on_enter_failed(self):
    logger.debug("Entering Failed state")
    self.state_machine.transition('next')

def on_exit_state(self):
    logger.debug(f"Exiting {self.state_machine.current_state.name} state")
    self.state_machine.transition('next')

class ModemConnectionStateService(ServiceInterface):
    """
    Top layer abstraction for handling modem connections.
    This service interfaces with ModemConnectionService and ModemManager.
    """

    # This service manages the state of modem connections using a finite state machine.
    # it takes the device name and interface name as an argument to initialize the service. TODO : providing one of device name or interface name will be enough
    # it will resolve the modem path using the device name or interface name.
    # TODO : it will read the vyos configuration corresponding to the modem given to manage the connection
    def __init__(self, device_name: str, wwan_interface: str):
        super().__init__('com.perle.ModemConnectionStateService.Interface')
        self.device_name = device_name
        self.wwan_interface = wwan_interface
        self.modem_id = -1
        self.modem_path = ""
        self.state_machine = None

    async def initialize(self):
        self.modem_id = await self.resolve_modem_path(self.device_name, 'device')
        self.modem_path = f"/org/freedesktop/ModemManager1/Modem/{self.modem_id}"
        self.state_machine = machines.FiniteMachine.build(state_space=state_machine)
        self.state_machine.default_start_state = 'Unavailable'
        logger.debug(self.state_machine.pformat())
        logger.debug(f"Initialized ModemConnectionStateService with modem path: {self.modem_path}")
        logger.debug(f"Initialized ModemConnectionStateService with modem id: {self.modem_id}")

    async def get_available_modems(self):
        """
        Retrieve a list of available modems.
        This method will query the ModemManager for available modems and make a list of dictionaries containing the interface name, device name, and modem path.
        """
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await bus.introspect('org.freedesktop.ModemManager1', '/org/freedesktop/ModemManager1')
        proxy_object = bus.get_proxy_object('org.freedesktop.ModemManager1', '/org/freedesktop/ModemManager1', introspection)
        modem_manager_interface = proxy_object.get_interface('org.freedesktop.DBus.ObjectManager')

        managed_objects = await modem_manager_interface.call_get_managed_objects()
        modems = []
        for path, interfaces in managed_objects.items():
            '''
            ports output example:
                Ports: [
                    ['cdc-wdm1', 6]
                    ['ttyUSB3', 9]
                    ['ttyUSB4', 5]
                    ['ttyUSB5', 3]
                    ['ttyUSB6', 3]
                    ['ttyUSB7', 9]
                    ['wwan1', 2] <-- we want the 'wwan1' name only and not the port number
            '''
            port = interfaces['org.freedesktop.ModemManager1.Modem']['Ports'].value[-1][0]
            # [-1] is selecting the last item which is the wwanN name, - [0] is selecting the name instead of the port number

            device = interfaces['org.freedesktop.ModemManager1.Modem']['Device'].value
            logger.debug(f"port: {port}")
            logger.debug(f"Modem Path: {path}")
            logger.debug(f"Device: {device}")
            modems.append({'path': path, 'device': device, 'interface':port})
        logger.debug(f"Available modems: {modems}")
        return modems

    async def resolve_modem_path(self, name: str, method: Literal["path", "device", "interface"]):
        '''
        this method takes any of:
            modem path,
            device name,
            or wwan interface name
        and returns the corresponding modem path.
        '''
        available_modems = await self.get_available_modems()
        matching_modem = next((modem for modem in available_modems if modem[method] == name), None)
        logger.debug(f"Resolved {method} : {name} TO {matching_modem}")
        return matching_modem['path'] if matching_modem else None


    def main_loop(self):
        """
        Main loop for the service.
        This will run the state machine and handle transitions.
        """
        pass

async def main():
    # TODO : hard coded device names and interfaces for testing, this will be replaced with a proper configuration
    m1 = ModemConnectionStateService(device_name='/sys/devices/pci0000:00/0000:00:05.0/usb3/3-3', wwan_interface='wwan0')
    m2 = ModemConnectionStateService(device_name='/sys/devices/pci0000:00/0000:00:05.0/usb3/3-2', wwan_interface='wwan1')
    await m1.initialize()
    await m2.initialize()
    # TODO : make this into a dbus service. get the main loop running on its own while interacting on the dbus

if __name__ == "__main__":
    asyncio.run(main())
