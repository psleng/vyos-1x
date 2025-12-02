import importlib.util
import asyncio

# Load the service module by absolute path so we can patch its MessageBus cleanly
spec = importlib.util.spec_from_file_location(
    'modem_service',
    '/home/jpasqualoni/software/vyos-1x/python/vyos/ifconfig/interfaces_wwan_ModemConnectionDbusService.py'
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ModemConnectionService = mod.ModemConnectionService


class FakeSimple:
    async def call_connect(self, settings):
        # return a fake bearer path as ModemManager would
        return '/org/freedesktop/ModemManager1/Bearer/99'

    async def call_disconnect(self, bearer):
        return None


class FakeProxy:
    def get_interface(self, name):
        return FakeSimple()


class FakeBus:
    async def connect(self):
        return self

    def get_proxy_object(self, *args, **kwargs):
        return FakeProxy()

    async def introspect(self, *args, **kwargs):
        return None

    def disconnect(self):
        pass

    async def wait_for_disconnect(self):
        pass


# Override the MessageBus used by the module with our FakeBus factory
mod.MessageBus = lambda *a, **k: FakeBus()


svc = ModemConnectionService()


async def run():
    bearer = await svc.Connect.__wrapped__(svc, '/org/freedesktop/ModemManager1/Modem/1', 'internet')
    assert svc._connected is True
    assert svc._bearer_path == '/org/freedesktop/ModemManager1/Bearer/99'
    await svc.Disconnect.__wrapped__(svc, '/org/freedesktop/ModemManager1/Modem/1')
    assert svc._connected is False


asyncio.run(run())
