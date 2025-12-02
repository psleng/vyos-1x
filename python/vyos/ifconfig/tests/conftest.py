import importlib.util
import pytest

# A FakeMessageBus and helpers used to patch the service module and dbus_next
class FakeSimple:
    async def call_connect(self, settings):
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


# Autouse fixture: patches the module's MessageBus and dbus_next.aio.MessageBus
@pytest.fixture(autouse=True)
def fake_message_bus(monkeypatch):
    # patch dbus_next.aio.MessageBus if available
    try:
        import dbus_next.aio as _aio
        monkeypatch.setattr(_aio, 'MessageBus', lambda *a, **k: FakeBus())
    except Exception:
        # dbus_next not available in test env; ignore
        pass

    # Also patch the local module when tests import it by path
    def patch_module(module):
        if hasattr(module, 'MessageBus'):
            setattr(module, 'MessageBus', lambda *a, **k: FakeBus())

    # provide helper to tests
    return patch_module
