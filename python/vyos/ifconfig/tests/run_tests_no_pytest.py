"""Minimal test runner for the ifconfig tests when pytest isn't available.
It patches dbus_next.aio.MessageBus to a FakeBus and then imports and runs test_* functions
from each test module in this directory.
"""
import importlib.util
import inspect
import asyncio
import sys
import pathlib

HERE = pathlib.Path(__file__).parent

# Fake bus used by tests
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


# Patch dbus_next.aio.MessageBus if dbus_next is present
try:
    import dbus_next.aio as _aio
    _aio.MessageBus = lambda *a, **k: FakeBus()
    print('Patched dbus_next.aio.MessageBus')
except Exception:
    print('dbus_next not available to patch (or patch failed)')


def run_tests_in_file(path):
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # find callable test_* functions and run them
    for name, obj in inspect.getmembers(mod, inspect.isfunction):
        if name.startswith('test_'):
            print(f'Running {path.name}::{name}()')
            # if coroutine function, run with asyncio
            if inspect.iscoroutinefunction(obj):
                asyncio.run(obj())
            else:
                # call directly; functions may call asyncio.run internally
                obj()


if __name__ == '__main__':
    tests = sorted(HERE.glob('test_modem_service*.py'))
    failures = 0
    for t in tests:
        try:
            run_tests_in_file(t)
        except AssertionError as e:
            print(f'Assertion failed in {t.name}: {e}')
            failures += 1
        except Exception as e:
            print(f'Error while running {t.name}: {e}')
            failures += 1

    if failures:
        print(f'Finished with {failures} failures')
        sys.exit(1)
    else:
        print('All tests passed')
        sys.exit(0)
