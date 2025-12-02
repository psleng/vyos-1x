import importlib.util


def load_module():
    spec = importlib.util.spec_from_file_location(
        'modem_service',
        '/home/jpasqualoni/software/vyos-1x/python/vyos/ifconfig/interfaces_wwan_ModemConnectionDbusService.py',
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_map_and_attach():
    mod = load_module()
    ModemConnectionStateService = mod.ModemConnectionStateService
    ModemConnectionService = mod.ModemConnectionService

    svc = ModemConnectionStateService(device_name='', wwan_interface='')
    assert svc._map_modem_state_to_action(9) == 'connect'
    assert svc._map_modem_state_to_action(8) == 'disconnect'
    assert svc._map_modem_state_to_action(999) is None

    conn = ModemConnectionService()
    svc.attach_connection_service(conn)
    assert hasattr(svc, 'connection_service') and svc.connection_service is conn


def test_set_get_connection_params():
    mod = load_module()
    ModemConnectionService = mod.ModemConnectionService
    svc = ModemConnectionService()
    # dbus_next @method decorator wraps the function; call the underlying implementation
    ok = svc.SetConnectionParams.__wrapped__(svc, '/org/freedesktop/ModemManager1/Modem/0', 'internet', 'u', 'p')
    assert ok is True
    params = svc.get_connection_params('/org/freedesktop/ModemManager1/Modem/0')
    assert params['apn'] == 'internet'
    assert params['username'] == 'u'
    assert params['password'] == 'p'
