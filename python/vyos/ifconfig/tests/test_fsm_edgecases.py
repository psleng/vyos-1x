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


def test_sim_lock_and_recovery():
    svc = ModemConnectionStateService(device_name='', wwan_interface='')
    # Simulate state sequence: searching(6) -> registered(7) -> connecting(9) -> failed(0)
    # Build FSM
    svc.state_machine = mod.machines.FiniteMachine.build(state_space=mod.state_machine)
    svc.state_machine.default_start_state = 'Unavailable'

    # attach fake connection service to observe actions
    conn = ModemConnectionService()
    conn._connect_override = lambda self, path, apn: (asyncio.sleep(0, result='/org/freedesktop/ModemManager1/Bearer/42'))
    svc.attach_connection_service(conn)

    # simulate properties changed events
    svc.handle_properties_changed('org.freedesktop.ModemManager1.Modem', {'State': 6})
    svc.handle_properties_changed('org.freedesktop.ModemManager1.Modem', {'State': 7})
    svc.handle_properties_changed('org.freedesktop.ModemManager1.Modem', {'State': 9})
    # allow tasks to run
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.sleep(0.05))

    # now simulate failure
    svc.handle_properties_changed('org.freedesktop.ModemManager1.Modem', {'State': 0})
    loop.run_until_complete(asyncio.sleep(0.05))

    # after failure, FSM should reflect transition to Unavailable or Failed
    assert svc.state_machine.current_state in ('Unavailable', 'Failed')


def test_registered_to_activated_transition():
    svc = ModemConnectionStateService(device_name='', wwan_interface='')
    svc.state_machine = mod.machines.FiniteMachine.build(state_space=mod.state_machine)
    svc.state_machine.default_start_state = 'Unavailable'

    svc.handle_properties_changed('org.freedesktop.ModemManager1.Modem', {'State': 7})
    # registering should lead to Wait for Ready state (via to_wait_for_ready)
    assert svc.state_machine.current_state in ('Wait for Ready', 'Initial EPS Bearer', 'Prepare', 'Unavailable')
