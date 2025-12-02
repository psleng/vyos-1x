import importlib.util
import asyncio

spec = importlib.util.spec_from_file_location(
    'modem_service',
    '/home/jpasqualoni/software/vyos-1x/python/vyos/ifconfig/interfaces_wwan_ModemConnectionDbusService.py',
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ModemConnectionStateService = mod.ModemConnectionStateService
ModemConnectionService = mod.ModemConnectionService


def test_state_to_action_mapping():
    svc = ModemConnectionStateService(device_name='', wwan_interface='')
    assert svc._map_modem_state_to_action(9) == 'connect'
    assert svc._map_modem_state_to_action(8) == 'disconnect'
    assert svc._map_modem_state_to_action(10) == 'none'
    assert svc._map_modem_state_to_action(0) == 'none' or svc._map_modem_state_to_action(0) is None


def test_state_to_transition_mapping():
    svc = ModemConnectionStateService(device_name='', wwan_interface='')
    assert svc._map_modem_state_to_transition(9) == 'to_connect'
    assert svc._map_modem_state_to_transition(10) == 'to_activated'
    assert svc._map_modem_state_to_transition(3) == 'to_disconnected'
    assert svc._map_modem_state_to_transition(0) == 'to_unavailable'


def test_on_enter_handlers_schedule_actions():
    # Ensure on_enter_connect schedules an _action_connect task and on_enter_deactivating schedules disconnect
    svc = ModemConnectionStateService(device_name='', wwan_interface='')

    # attach a fake connection service with overrides to avoid actual DBus calls
    conn = ModemConnectionService()
    async def fake_connect(self, modem_path, apn):
        await asyncio.sleep(0)
        return '/org/freedesktop/ModemManager1/Bearer/123'
    async def fake_disconnect(self, modem_path):
        await asyncio.sleep(0)
        return None
    conn._connect_override = lambda self, path, apn: fake_connect(self, path, apn)
    conn._simple_disconnect = fake_disconnect
    svc.attach_connection_service(conn)

    # call handlers directly
    svc.modem_path = '/org/freedesktop/ModemManager1/Modem/1'
    # run on_enter_connect
    mod.on_enter_connect(svc)
    # wait a tiny bit for scheduled tasks
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.sleep(0.05))
    # after connect, service should be connected
    assert conn._connected is True or conn._bearer_path != ''

    # run on_enter_deactivating
    mod.on_enter_deactivating(svc)
    loop.run_until_complete(asyncio.sleep(0.05))
    assert conn._connected is False
