# ModemConnectionService - A D-Bus service for managing modem connections
# TODO : Service File to come
# TODO: Run file in separate terminal to start the service for now and interact with busctl commands

from dbus_next.service import ServiceInterface, method, signal, dbus_property
from dbus_next.aio import MessageBus
from dbus_next import BusType, DBusError, PropertyAccess, message
from dbus_next.signature import Variant
import asyncio, logging, sys
import signal as py_signal
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

class ModemConnectionService(ServiceInterface):
    def __init__(self):
        super().__init__('com.perle.ModemConnectionService.Interface')
        self._connected = False
        self._modem_path = ''
        self._bearer_path = ''
        self._bus = None
        self._auto_task = None
        self._stop_auto = False

    @signal()
    def Connected(self, bearer_path: 's', modem_id: 'i', interface: 's') -> None: # type: ignore
        """Signal emitted when the modem is connected.
        bearer_path: object path for the bearer
        modem_id: numeric modem id from ModemManager
        interface: wwan interface name (eg. 'wwan0')
        """

    @signal()
    def Disconnected(self, modem_id: 'i', interface: 's') -> None: # type: ignore
        """Signal emitted when the modem is disconnected."""

    @signal()
    def Error(self, message: 's', modem_id: 'i') -> None: # type: ignore
        """Signal emitted on errors related to modem connection."""

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
        # Use existing connected bus if available to avoid reconnecting repeatedly
        bus = self._bus
        if bus is None:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            # do not assign it to self._bus here; attach_bus handles persistent assignment

        try:
            bearer = await self._simple_connect(bus, modem_path, apn)
        finally:
            # If we created a temporary bus (not self._bus), disconnect it
            if bus is not self._bus:
                bus.disconnect()
                await bus.wait_for_disconnect()
        if bearer is None:
            logger.error("Failed to connect to modem; emitting Error signal")
            try:
                # best-effort to include modem id
                modem_id = None
                try:
                    modem_id = int(modem_path.rstrip('/').split('/')[-1])
                except Exception:
                    modem_id = -1
                self.Error("Failed to connect to modem", modem_id)
            except Exception:
                logger.exception("Failed to emit Error signal")
            raise DBusError("org.freedesktop.ModemManager1.Error.ConnectionFailed", "Failed to connect to the modem.")

        self._bearer_path = bearer
        self._connected = True
        logger.debug(f"Connected to modem at {modem_path}, bearer path: {bearer}")
        try:
            # include modem id and interface if possible
            modem_id = -1
            iface = ''
            try:
                modem_id = int(modem_path.rstrip('/').split('/')[-1])
            except Exception:
                modem_id = -1
            try:
                iface = await self._get_modem_interface(bus, modem_path)
            except Exception:
                iface = ''
            self.Connected(self._bearer_path, modem_id, iface)
        except Exception:
            logger.exception("Failed to emit Connected signal")

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

        # Retrieve extra params if available
        config = self.get_connection_params(modem_path)
        sim_slot = config.get('sim_slot', -1) if config else -1
        pdp_type = config.get('pdp_type', '') if config else ''

        connection_settings = {
            'apn': Variant('s', apn),
            'sim-slot': Variant('i', sim_slot),
            'pdp-type': Variant('s', pdp_type)
        }

        try:
            bearer = await simple_interface.call_connect(connection_settings)
            logger.debug(f"Connection bearer: {bearer}")
            return bearer
        except Exception as e:
            logger.exception("Failed to connect via ModemManager.Simple.Connect")
            return None

    @method()
    async def Disconnect(self, modem_path: 's'): # type: ignore
        """Disconnect from the modem."""
        logger.debug(f"Disconnecting from modem at {modem_path}")
        bus = self._bus
        temp_bus = False
        if bus is None:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            temp_bus = True

        try:
            await self._simple_disconnect(bus, modem_path)
        finally:
            if temp_bus:
                bus.disconnect()
                await bus.wait_for_disconnect()

        logger.debug(f"Disconnected from modem at {modem_path}")
        self._bearer_path = ''
        self._connected = False
        try:
            modem_id = -1
            iface = ''
            try:
                modem_id = int(modem_path.rstrip('/').split('/')[-1])
            except Exception:
                modem_id = -1
            try:
                iface = await self._get_modem_interface(bus, modem_path)
            except Exception:
                iface = ''
            self.Disconnected(modem_id, iface)
        except Exception:
            logger.exception("Failed to emit Disconnected signal")

    async def _get_modem_interface(self, bus: MessageBus, modem_path: str):
        """Try to read the Modem.Ports property to get the wwan interface name (last port entry)."""
        try:
            proxy_object = bus.get_proxy_object(
                'org.freedesktop.ModemManager1',
                modem_path,
                await bus.introspect('org.freedesktop.ModemManager1', modem_path)
            )
            props_iface = proxy_object.get_interface('org.freedesktop.DBus.Properties')
            ports = await props_iface.call_get('org.freedesktop.ModemManager1.Modem', 'Ports')
            ports = ports.value
            return ports[-1][0]
        except Exception:
            return ''

    @method()
    def SetConnectionParams(self, modem_path: 's', apn: 's', username: 's', password: 's') -> 'b': # type: ignore
        """Store APN, auth, sim slot, and PDP type for a modem path so the auto-connect loop can use it."""
        if not hasattr(self, '_configs'):
            self._configs = {}
        # Accept sim_slot and pdp_type as explicit arguments
        import inspect
        frame = inspect.currentframe()
        args, _, _, values = inspect.getargvalues(frame)
        sim_slot = values.get('sim_slot', -1)
        pdp_type = values.get('pdp_type', '')
        self._configs[modem_path] = {
            'apn': apn,
            'username': username,
            'password': password,
            'sim_slot': sim_slot,
            'pdp_type': pdp_type
        }
        logger.debug(f"SetConnectionParams for {modem_path}: {self._configs[modem_path]}")
        return True

    def get_connection_params(self, modem_path: str):
        if not hasattr(self, '_configs'):
            self._configs = {}
        return self._configs.get(modem_path, None)

    @method()
    def SetRetryPolicy(self, modem_path: 's', max_retries: 'i', base_delay: 'd', max_delay: 'd') -> 'b': # type: ignore
        """Set retry/backoff policy for a given modem path.

        base_delay: starting delay in seconds (float)
        max_delay: maximum delay in seconds
        max_retries: maximum attempts (-1 for infinite)
        """
        if not hasattr(self, '_retry_policies'):
            self._retry_policies = {}
        self._retry_policies[modem_path] = {'max_retries': int(max_retries), 'base_delay': float(base_delay), 'max_delay': float(max_delay)}
        logger.debug(f"SetRetryPolicy for {modem_path}: {self._retry_policies[modem_path]}")
        return True

    def get_retry_policy(self, modem_path: str):
        if not hasattr(self, '_retry_policies'):
            self._retry_policies = {}
        return self._retry_policies.get(modem_path, None)

    async def _simple_disconnect(self, bus: MessageBus, modem_path: str):
        logger.debug(f"Disconnecting from modem at {modem_path}")
        proxy_object = bus.get_proxy_object(
            'org.freedesktop.ModemManager1',
            modem_path,
            await bus.introspect('org.freedesktop.ModemManager1', modem_path)
        )
        simple_interface = proxy_object.get_interface('org.freedesktop.ModemManager1.Modem.Simple')

        # ModemManager Simple.Disconnect takes the bearer object path, may raise on failure
        try:
            await simple_interface.call_disconnect(self._bearer_path)
        except Exception:
            logger.exception("Error while disconnecting bearer")

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

def state_change_handler(message: message.Message):
    """Handle state change signals from the modem."""
    logger.debug(f"State change signal received: {message}")


# FSM on_enter handlers -------------------------------------------------
def on_enter_unavailable(self):
    logger.debug("Entering Unavailable state")
    # nothing specific
    # mark for tests
    try:
        self._last_on_enter = 'Unavailable'
    except Exception:
        pass

def on_enter_disconnected(self):
    logger.debug("Entering Disconnected state")
    # when disconnected, schedule attempt to connect
    try:
        asyncio.create_task(self._action_connect())
    except Exception:
        logger.exception("Failed to schedule connect from on_enter_disconnected")
    try:
        self._last_on_enter = 'Disconnected'
    except Exception:
        pass

def on_enter_prepare(self):
    logger.debug("Entering Prepare state")
    # prepare resources if needed
    try:
        self._last_on_enter = 'Prepare'
    except Exception:
        pass

def on_enter_wait_for_sim(self):
    logger.debug("Entering Wait for SIM state")
    try:
        self._last_on_enter = 'Wait for SIM'
    except Exception:
        pass

def on_enter_unlock(self):
    logger.debug("Entering Unlock state")
    try:
        self._last_on_enter = 'Unlock'
    except Exception:
        pass

def on_enter_wait_for_ready(self):
    logger.debug("Entering Wait for Ready state")
    try:
        self._last_on_enter = 'Wait for Ready'
    except Exception:
        pass

def on_enter_initial_eps_bearer(self):
    logger.debug("Entering Initial EPS Bearer state")
    try:
        self._last_on_enter = 'Initial EPS Bearer'
    except Exception:
        pass

def on_enter_connect(self):
    logger.debug("Entering Connect state: scheduling connect action")
    try:
        asyncio.create_task(self._action_connect())
    except Exception:
        logger.exception("Failed to schedule connect from on_enter_connect")
    try:
        self._last_on_enter = 'Connect'
    except Exception:
        pass

def on_enter_last(self):
    logger.debug("Entering Last state")
    try:
        self._last_on_enter = 'Last'
    except Exception:
        pass

def on_enter_activated(self):
    logger.debug("Entering Activated state")
    # no-op: keep connection until signalled otherwise
    try:
        self._last_on_enter = 'Activated'
    except Exception:
        pass

def on_enter_deactivating(self):
    logger.debug("Entering Deactivating state: scheduling disconnect action")
    try:
        asyncio.create_task(self._action_disconnect())
    except Exception:
        logger.exception("Failed to schedule disconnect from on_enter_deactivating")
    try:
        self._last_on_enter = 'Deactivating'
    except Exception:
        pass

def on_enter_failed(self):
    logger.debug("Entering Failed state")
    # optionally schedule recovery
    try:
        self._last_on_enter = 'Failed'
    except Exception:
        pass


state_machine = [
    {
        "name" : "Unavailable",
        "next_states": {
            'to_disconnected': 'Disconnected',
            'to_prepare': 'Prepare'
        },
        "on_enter": on_enter_unavailable,
        "on_exit": None
    },
    {
        "name" : "Need Authentication",
        "next_states": {
            'to_prepare': 'Prepare',
        },
        "on_enter": None,
        "on_exit": None
    },
    {
        "name" : "Prepare",
        "next_states": {
            'to_wait_for_sim': 'Wait for SIM'
        },
        "on_enter": on_enter_prepare,
        "on_exit": None
    },
    {
        "name" : "Wait for SIM",
        "next_states": {
            'to_unlock': 'Unlock'
        },
        "on_enter": on_enter_wait_for_sim,
        "on_exit": None
    },
    {
        "name" : "Unlock",
        "next_states": {
            'to_wait_for_ready': 'Wait for Ready',
            'to_need_auth': 'Need Authentication'
        },
        "on_enter": on_enter_unlock,
        "on_exit": None
    },
    {
        "name" : "Wait for Ready",
        "next_states": {
            'to_initial_eps_bearer': 'Initial EPS Bearer'
        },
        "on_enter": on_enter_wait_for_ready,
        "on_exit": None
    },
    {
        "name" : "Initial EPS Bearer",
        "next_states": {
            'to_connect': 'Connect'
        },
        "on_enter": on_enter_initial_eps_bearer,
        "on_exit": None
    },
    {
        "name": "Connect",
        "next_states": {
            'to_last': 'Last'
        },
        "on_enter": on_enter_connect,
        "on_exit": None
    },
    {
        "name": "Last",
        "next_states": {
            'to_activated': 'Activated'
        },
        "on_enter": on_enter_last,
        "on_exit": None
    },
    {
        "name" : "Activated",
        "next_states": {
            'to_deactivating': 'Deactivating'
        },
        "on_enter": on_enter_activated,
        "on_exit": None
    },
    {
        "name": "Deactivating",
        "next_states": {
            'to_disconnected': 'Disconnected'
        },
        "on_enter": on_enter_deactivating,
        "on_exit": None
    },
    {
        "name": "Failed",
        "next_states": {
            'to_unavailable': 'Unavailable',
            'to_disconnected': 'Disconnected'
        },
        "on_enter": on_enter_failed,
        "on_exit": None
    }
]

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

    async def attach_modem_signals(self, bus: MessageBus):
        """Subscribe to ModemManager signals for this modem and map to FSM transitions."""
        if not self.modem_path:
            logger.warning("No modem path to attach signals to")
            return

        introspection = await bus.introspect('org.freedesktop.ModemManager1', self.modem_path)
        proxy = bus.get_proxy_object('org.freedesktop.ModemManager1', self.modem_path, introspection)
        props_iface = proxy.get_interface('org.freedesktop.DBus.Properties')

        # listen for PropertiesChanged signals on the Modem interface
        def on_props_changed(interface_name, changed_props, invalidated_props):
            # delegate to testable handler
            try:
                self.handle_properties_changed(interface_name, changed_props)
            except Exception:
                logger.exception("Error while handling PropertiesChanged")

        # dbus_next signals require converting method signature; use add_message_handler as a fallback
        props_iface.on_properties_changed(on_props_changed)

    def handle_properties_changed(self, interface_name, changed_props):
        """Process a PropertiesChanged payload (dictionary) and drive the FSM transitions.

        This is exposed so unit tests and integration harnesses can directly simulate ModemManager
        state updates without relying on a running system bus.
        """
        logger.debug(f"PropertiesChanged for {interface_name}: {changed_props}")
        for k, v in changed_props.items():
            val = v.value if isinstance(v, Variant) else v
            if k == 'State':
                transition = self._map_modem_state_to_transition(val)
                if transition:
                    logger.debug(f"Modem state changed to {val}; transitioning FSM '{transition}'")
                    try:
                        self.state_machine.process_event(transition)
                    except Exception:
                        logger.exception("FSM transition failed on State change")
                else:
                    logger.debug(f"No mapped FSM transition for modem state {val}")

    def attach_connection_service(self, conn_service: 'ModemConnectionService'):
        """Attach the ModemConnectionService instance so state callbacks can call Connect/Disconnect."""
        self.connection_service = conn_service

    def _map_modem_state_to_action(self, modem_state_value: int):
        """Map ModemManager numeric state to an actionable name handled by `_do_action`.

        Return values: 'connect', 'disconnect', 'none'
        """
        mapping = {
            0: 'none',   # failed
            3: 'none',   # disabled -> no immediate action
            6: 'none',   # searching
            7: 'none',   # registered
            9: 'connect',# connecting -> ensure connection is in progress
            10: 'none',  # connected -> no action required
            8: 'disconnect', # disconnecting
        }
        return mapping.get(modem_state_value, None)

    async def _do_action(self, action: str):
        """Perform an async action based on the action name."""
        if not hasattr(self, 'connection_service') or self.connection_service is None:
            logger.debug("No connection service attached; cannot perform action")
            return
        if action == 'connect':
            await self._action_connect()
        elif action == 'disconnect':
            await self._action_disconnect()
        else:
            logger.debug(f"Action '{action}' has no handler")

    async def _action_connect(self):
        logger.debug(f"_action_connect called for modem {self.modem_path}")
        params = None
        try:
            params = self.connection_service.get_connection_params(self.modem_path)
        except Exception:
            logger.exception("Failed to get connection params")
        apn = params['apn'] if params and 'apn' in params else ''
        # Apply retry/backoff policy
        policy = None
        try:
            policy = self.connection_service.get_retry_policy(self.modem_path)
        except Exception:
            logger.exception("Failed to get retry policy")

        max_retries = policy['max_retries'] if policy and 'max_retries' in policy else 5
        base_delay = policy['base_delay'] if policy and 'base_delay' in policy else 1.0
        max_delay = policy['max_delay'] if policy and 'max_delay' in policy else 60.0

        attempt = 0
        while True:
            attempt += 1
            try:
                # Allow tests to inject an override coroutine on the connection_service
                connect_override = getattr(self.connection_service, '_connect_override', None)
                if connect_override is not None:
                    bearer = await connect_override(self.connection_service, self.modem_path, apn)
                else:
                    # call underlying coroutine if method is decorated by dbus_next
                    connect_func = getattr(self.connection_service.Connect, '__wrapped__', None)
                    if connect_func is not None:
                        bearer = await connect_func(self.connection_service, self.modem_path, apn)
                    else:
                        bearer = await self.connection_service.Connect(self.modem_path, apn)

                logger.debug(f"_action_connect succeeded on attempt {attempt}, bearer: {bearer}")
                break
            except DBusError as dbe:
                logger.error(f"_action_connect DBusError on attempt {attempt}: {dbe}")
            except Exception:
                logger.exception(f"Exception in _action_connect attempt {attempt}")

            if max_retries >= 0 and attempt >= max_retries:
                logger.error(f"Max retries reached ({max_retries}) for modem {self.modem_path}")
                # emit an error signal via connection service if possible
                try:
                    modem_id = -1
                    try:
                        modem_id = int(self.modem_path.rstrip('/').split('/')[-1])
                    except Exception:
                        modem_id = -1
                    self.connection_service.Error(f"Max retries reached for {self.modem_path}", modem_id)
                except Exception:
                    logger.exception("Failed to emit error signal after retries exhausted")
                # mark as failed for testability and possible recovery logic
                try:
                    self._failed = True
                    self._last_on_enter = 'Failed'
                except Exception:
                    pass
                break

            # exponential backoff with jitter
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            # add jitter between 0.5x and 1.5x
            import random
            jitter = random.uniform(0.5, 1.5)
            sleep_time = delay * jitter
            logger.debug(f"Retrying in {sleep_time:.1f}s (attempt {attempt})")
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                logger.debug("_action_connect cancelled during backoff")
                break

    async def _action_disconnect(self):
        logger.debug(f"_action_disconnect called for modem {self.modem_path}")
        try:
            await self.connection_service.Disconnect(self.modem_path)
        except Exception:
            logger.exception("Exception in _action_disconnect")

    def _map_modem_state_to_transition(self, modem_state_value: int):
        """Return the FSM transition name for a given ModemManager numeric state.

        ModemManager defines states (from ModemManager docs):
         0: failed
         1: unknown
         2: disabling
         3: disabled
         4: enabling
         5: enabled
         6: searching
         7: registered
         8: disconnecting
         9: connecting
         10: connected

        Map these to FSM transitions where applicable.
        """
        mapping = {
            0: 'to_unavailable',   # failed -> Unavailable
            3: 'to_disconnected',   # disabled -> Disconnected
            6: 'to_prepare',   # searching -> Prepare/Wait for SIM
            7: 'to_wait_for_ready',   # registered -> Wait for Ready
            9: 'to_connect',   # connecting -> Connect
            10: 'to_activated',  # connected -> Activated
            8: 'to_deactivating',   # disconnecting -> Deactivating
        }
        return mapping.get(modem_state_value, None)

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
        # Start the FSM. In this simple implementation we just let the FSM run
        # No-op here; higher-level code should drive transitions or hook signals
        return

    # helper to start background monitoring/auto-connect
    async def start_auto_connect(self, service: ModemConnectionService, interval: int = 10):
        """Background task that ensures the modem stays connected.

        The service may provide per-modem connection params via SetConnectionParams().
        """
        logger.debug("Starting auto-connect background task")
        self._stop_auto = False
        while not self._stop_auto:
            try:
                # If not connected, attempt to connect
                if not service.connected:
                    if self.modem_path:
                        try:
                            params = service.get_connection_params(self.modem_path)
                            apn = params['apn'] if params and 'apn' in params else ''
                            bearer = await service.Connect(self.modem_path, apn)
                            logger.debug(f"Auto-connected bearer: {bearer}")
                        except DBusError as dbe:
                            logger.error(f"Auto-connect DBusError: {dbe}")
                    else:
                        logger.debug("No modem path resolved for auto-connect yet")
                # sleep for a bit before checking again
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Exception in auto-connect loop")

        logger.debug("Auto-connect background task stopped")

    def stop_auto_connect(self):
        self._stop_auto = True


# Main function to run the service
async def main():

    # TODO : hard coded device names and interfaces for testing, this will be replaced with a proper configuration
    m1 = ModemConnectionStateService(device_name='/sys/devices/pci0000:00/0000:00:05.0/usb3/3-3', wwan_interface='wwan0')
    m2 = ModemConnectionStateService(device_name='/sys/devices/pci0000:00/0000:00:05.0/usb3/3-2', wwan_interface='wwan1')
    await m1.initialize()
    await m2.initialize()
    # TODO : make this into a dbus service. get the main loop running on its own while interacting on the dbus

    # Connect to the system or session bus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    # Request a unique name on the bus
    await bus.request_name('com.perle.ModemConnectionService')

    # Export the service interface on a specific object path
    interface = ModemConnectionService()
    bus.export('/com/perle/ModemConnectionService', interface)
    # Keep a reference to the bus so methods can reuse it
    interface._bus = bus

    # Initialize and export the state service for each modem
    state_service_1 = ModemConnectionStateService(device_name=m1.device_name, wwan_interface=m1.wwan_interface)
    await state_service_1.initialize()
    # Export state service under its own path
    bus.export(f'/com/perle/ModemConnectionStateService{state_service_1.modem_id}', state_service_1)
    # give the state service access to the connection service so it can perform actions
    state_service_1.attach_connection_service(interface)

    # Attach modem signals so the state service can react to ModemManager events
    try:
        await state_service_1.attach_modem_signals(bus)
    except Exception:
        logger.exception("Failed to attach modem signals")

    # Start an example auto-connect background task for demonstration (APN to be configured)
    apn = 'internet'
    state_service_1._auto_task = asyncio.create_task(state_service_1.start_auto_connect(interface, interval=15))

    # second modem
    state_service_2 = ModemConnectionStateService(device_name=m2.device_name, wwan_interface=m2.wwan_interface)
    await state_service_2.initialize()
    bus.export(f'/com/perle/ModemConnectionStateService{state_service_2.modem_id}', state_service_2)
    try:
        await state_service_2.attach_modem_signals(bus)
    except Exception:
        logger.exception("Failed to attach modem signals for modem 2")
    state_service_2.attach_connection_service(interface)
    state_service_2._auto_task = asyncio.create_task(state_service_2.start_auto_connect(interface, interval=15))

    #add match rules to listen to signals

    bus.add_message_handler(message_handler)

    stop_event = asyncio.Future()  # Run forever
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(py_signal.SIGINT, stop_event.set_result, None)  # Handle Ctrl+C
    loop.add_signal_handler(py_signal.SIGTERM, stop_event.set_result, None)  # Handle termination signal
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
        # cancel background auto task(s)
        try:
            if state_service_1._auto_task:
                state_service_1._auto_task.cancel()
                await state_service_1._auto_task
            if state_service_2._auto_task:
                state_service_2._auto_task.cancel()
                await state_service_2._auto_task
        except Exception:
            logger.exception("Error stopping auto-connect task")
        bus.disconnect()
        await bus.wait_for_disconnect()
        logger.debug("D-Bus service stopped.")

# Run the asyncio event loop
if __name__ == "__main__":
    asyncio.run(main())

## all calls will fail if modem is in a failed state i.e NO SIM
