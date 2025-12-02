import asyncio
import importlib.util


def load_module():
    spec = importlib.util.spec_from_file_location(
        'modem_service',
        '/home/jpasqualoni/software/vyos-1x/python/vyos/ifconfig/interfaces_wwan_ModemConnectionDbusService.py',
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_connect_disconnect_monkeypatch():
    mod = load_module()
    ModemConnectionService = mod.ModemConnectionService

    svc = ModemConnectionService()

    async def fake_simple_connect(bus, modem_path, apn):
        await asyncio.sleep(0)
        return '/org/freedesktop/ModemManager1/Bearer/0'

    async def fake_simple_disconnect(bus, modem_path):
        await asyncio.sleep(0)
        return None

    # monkeypatch the internal methods
    svc._simple_connect = fake_simple_connect
    svc._simple_disconnect = fake_simple_disconnect

    async def run_test():
        bearer = await svc.Connect.__wrapped__(svc, '/org/freedesktop/ModemManager1/Modem/0', 'internet')
        assert svc._connected is True
        assert svc._bearer_path == '/org/freedesktop/ModemManager1/Bearer/0'
        await svc.Disconnect.__wrapped__(svc, '/org/freedesktop/ModemManager1/Modem/0')
        assert svc._connected is False
        assert svc._bearer_path == ''

    asyncio.run(run_test())
