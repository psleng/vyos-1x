# ModemConnectionService - A D-Bus service for managing modem connections
# TODO : Service File to come
# TODO: Run file in separate terminal to start the service for now and interact with busctl commands

from dbus_next.service import ServiceInterface, method, signal, dbus_property
from dbus_next.aio import MessageBus
from dbus_next import BusType, DBusError, PropertyAccess, message
from dbus_next.signature import Variant
import asyncio, logging, sys, signal

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

class ModemConnectionService(ServiceInterface):
    def __init__(self):
        super().__init__('com.perle.ModemConnectionService.Interface')
        self._connected = False
        self._modem_path = ''
        self._bearer_path = ''

    @dbus_property()
    def modem_path(self) -> 's': # type: ignore
        """Get the current modem path."""
        return self._modem_path

    @modem_path.setter
    def modem_path(self, value: 's'): # type: ignore
        """Set the modem path."""
        logger.debug(f"Setting modem path to: {value}")
        self._modem_path = value

    @dbus_property(access=PropertyAccess.READ)
    def bearer_path(self) -> 's': # type: ignore
        """Get the current bearer path."""
        return self._bearer_path

    @dbus_property(access=PropertyAccess.READ)
    def connected(self) -> 'b': # type: ignore
        """Check if the modem is connected."""
        return self._connected

    @method()
    async def Connect(self, modem_path: 's', apn: 's') -> 's': # type: ignore
        """Connect to the modem with the provided parameters."""
        logger.debug(f"Connecting to modem at {modem_path} with APN {apn}")
        self._modem_path = modem_path

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        bearer = await self._simple_connect(bus, modem_path, apn)
        bus.disconnect()
        await bus.wait_for_disconnect()
        if bearer is None:
            raise DBusError("org.freedesktop.ModemManager1.Error.ConnectionFailed", "Failed to connect to the modem.")

        self._bearer_path = bearer
        self._connected = True
        logger.debug(f"Connected to modem at {modem_path}, bearer path: {bearer}")

        # Emit a signal to indicate connection status
        # self.emit_signal('Connected', self._bearer_path) :TODO write signal emitter

        return self._bearer_path

    async def _simple_connect(self, bus: MessageBus, modem_path: str, apn: str):
        proxy_object = bus.get_proxy_object(
            'org.freedesktop.ModemManager1',
            modem_path,
            await bus.introspect('org.freedesktop.ModemManager1', modem_path)
        )
        simple_interface = proxy_object.get_interface('org.freedesktop.ModemManager1.Modem.Simple')

        connection_settings = { # TODO : more config option handling
        'apn': Variant('s', apn)  # 's' indicates a string type
        }

        bearer = await simple_interface.call_connect(connection_settings) # this returns the bearer path
        logger.debug(f"Connection bearer: {bearer}")
        return bearer

    @method()
    async def Disconnect(self, modem_path: 's'): # type: ignore
        """Disconnect from the modem."""
        logger.debug(f"Disconnecting from modem at {modem_path}")

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        await self._simple_disconnect(bus, modem_path)
        bus.disconnect()
        await bus.wait_for_disconnect()

        logger.debug(f"Disconnected from modem at {modem_path}")
        self._bearer_path = ''
        self._connected = False

    async def _simple_disconnect(self, bus: MessageBus, modem_path: str):
        logger.debug(f"Disconnecting from modem at {modem_path}")
        proxy_object = bus.get_proxy_object(
            'org.freedesktop.ModemManager1',
            modem_path,
            await bus.introspect('org.freedesktop.ModemManager1', modem_path)
        )
        simple_interface = proxy_object.get_interface('org.freedesktop.ModemManager1.Modem.Simple')

        #disconnect has no return. Assume that it will always disconnect from the bearer path given
        await simple_interface.call_disconnect(self._bearer_path)

    @method()
    async def GetModemInfo(self, modem_path: 's') -> 'a{sv}': # type: ignore
        """Get information about the modem."""
        logger.debug(f"Getting modem info for {modem_path}")
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        proxy_object = bus.get_proxy_object(
            'org.freedesktop.ModemManager1',
            modem_path,
            await bus.introspect('org.freedesktop.ModemManager1', modem_path)
        )
        simple_interface = proxy_object.get_interface('org.freedesktop.ModemManager1.Modem.Simple')

        # Retrieve all properties of the modem
        properties = await simple_interface.call_get_status()
        _log_properties(properties)

        bus.disconnect()
        await bus.wait_for_disconnect()

        return properties

def message_handler(message: message.Message): # TODO: Can do more with this handler, like logging or processing signals
    """Handle incoming D-Bus messages."""
    logger.debug(f"Received message: {message}")


# Main function to run the service
async def main():
    # Connect to the system or session bus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Request a unique name on the bus
    await bus.request_name('com.perle.ModemConnectionService')

    # Export the service interface on a specific object path
    interface = ModemConnectionService()
    bus.export('/com/perle/ModemConnectionService', interface)

    #add match rules to listen to signals

    bus.add_message_handler(message_handler)

    stop_event = asyncio.Future()  # Run forever
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set_result, None)  # Handle Ctrl+C
    loop.add_signal_handler(signal.SIGTERM, stop_event.set_result, None)  # Handle termination signal
    try:
        logger.debug("D-Bus service is running...")
        await stop_event # This will block until the event is set
    except Exception:
        pass # Handle any exceptions that may occur
    finally:
        # await bus.wait_for_disconnect()
        # Unexport the service and disconnect the bus
        logger.debug("D-Bus service is stopping...")
        bus.unexport('/com/perle/ModemConnectionService')
        bus.disconnect()
        await bus.wait_for_disconnect()
        logger.debug("D-Bus service stopped.")

# Run the asyncio event loop
if __name__ == "__main__":
    asyncio.run(main())

## all calls will fail if modem is in a failed state i.e NO SIM
