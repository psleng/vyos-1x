# Copyright (C) 2026 Perle Systems Limited
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library.  If not, see <http://www.gnu.org/licenses/>.

"""Root-only local API socket: key-less auth and trusted acting user."""

import asyncio
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/services'))

try:
    import httpx
    from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI
    from starlette.requests import Request
except ImportError:
    httpx = None

if httpx is not None:
    from api import caller as api_caller
    from api.background import BackgroundOpManager
    from api.caller import Caller, LocalSocketMarker, caller_scope, get_caller
    from api.graphql.libs import key_auth
    from api.rest.models import ApiModel
    from api.rest.routers import MultipartRoute, auth_required
    from api.session import SessionState

KEY = 'test-key'


def make_request(local, headers=None):
    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/graphql',
        'headers': [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }
    if local:
        scope[api_caller.SCOPE_LOCAL] = True
    return Request(scope)


@unittest.skipIf(httpx is None, 'needs fastapi and httpx')
class TestCallerHelpers(unittest.TestCase):
    def test_find_key_id(self):
        keys = [{'id': 'gui', 'key': 'a'}, {'id': 'ops', 'key': 'b'}]
        self.assertEqual(api_caller.find_key_id(keys, 'b'), 'ops')
        for bad in ('c', '', None, 1):
            self.assertIsNone(api_caller.find_key_id(keys, bad))
        self.assertIsNone(api_caller.find_key_id([], 'a'))

    def test_identity_headers_are_sanitized(self):
        good = api_caller.local_caller(
            {'x-igos-user': 'alice@example.com', 'x-igos-via': 'gui'}
        )
        self.assertEqual((good.user, good.via), ('alice@example.com', 'gui'))
        bad = api_caller.local_caller({'x-igos-user': 'a|b', 'x-igos-via': 'GUI\n'})
        self.assertEqual((bad.user, bad.via), (None, None))

    def test_commit_env_only_for_local_caller_with_user(self):
        env = {'COMMIT_VIA': 'vyos-http-api'}
        with caller_scope(Caller(local=True, user='alice', via='gui')):
            with api_caller.commit_actor_env(env):
                self.assertEqual(env['COMMIT_USER'], 'alice')
                self.assertEqual(env['COMMIT_VIA'], 'gui')
        self.assertEqual(env, {'COMMIT_VIA': 'vyos-http-api'})

        for caller in (None, Caller(key_id='myid'), Caller(local=True)):
            with caller_scope(caller), api_caller.commit_actor_env(env):
                self.assertEqual(env, {'COMMIT_VIA': 'vyos-http-api'})

    def test_local_socket_is_root_only_and_replaces_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'api-local.sock')
            with patch.object(api_caller.os, 'chown'):
                first = api_caller.bind_local_socket(path)
                first.close()
                second = api_caller.bind_local_socket(path)
            try:
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertEqual(second.family, socket.AF_UNIX)
            finally:
                second.close()


@unittest.skipIf(httpx is None, 'needs fastapi and httpx')
class TestRestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = SessionState()
        state.keys = [{'id': 'myid', 'key': KEY}]

        router = APIRouter(
            route_class=MultipartRoute, dependencies=[Depends(auth_required)]
        )

        def probe(data: ApiModel, background_tasks: BackgroundTasks):
            # sync endpoint: runs in the threadpool, as the real ones do
            c = get_caller()
            seen = {}
            background_tasks.add_task(lambda: seen.setdefault('bg', get_caller()))
            cls.background_seen = seen
            return {'local': c.local, 'key_id': c.key_id, 'user': c.user, 'via': c.via}

        router.post('/probe')(probe)
        cls.app = FastAPI()
        cls.app.include_router(router)

    def post(self, local, **kwargs):
        app = LocalSocketMarker(self.app) if local else self.app

        async def go():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url='http://api'
            ) as client:
                return await client.post('/probe', **kwargs)

        return asyncio.run(go())

    def test_nginx_socket_still_requires_key(self):
        self.assertEqual(self.post(False, json={}).status_code, 401)
        self.assertEqual(self.post(False, json={'key': 'wrong'}).status_code, 401)
        response = self.post(False, json={'key': KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['key_id'], 'myid')

    def test_nginx_socket_ignores_identity_headers(self):
        response = self.post(
            False,
            json={'key': KEY},
            headers={'X-Igos-User': 'mallory', 'X-Igos-Via': 'gui'},
        )
        self.assertEqual(
            response.json(),
            {'local': False, 'key_id': 'myid', 'user': None, 'via': None},
        )

    def test_local_socket_needs_no_key_and_trusts_identity(self):
        response = self.post(
            True, json={}, headers={'X-Igos-User': 'alice', 'X-Igos-Via': 'gui'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'local': True, 'key_id': None, 'user': 'alice', 'via': 'gui'},
        )
        self.assertEqual(self.background_seen['bg'].user, 'alice')

    def test_local_socket_form_request_without_key(self):
        response = self.post(True, data={'data': '{"op": "show", "path": ["version"]}'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['local'])

    def test_nginx_form_request_without_key_rejected(self):
        response = self.post(
            False, data={'data': '{"op": "show", "path": ["version"]}'}
        )
        self.assertEqual(response.status_code, 401)


@unittest.skipIf(httpx is None, 'needs fastapi and httpx')
class TestGraphQLAuthorize(unittest.TestCase):
    def setUp(self):
        state = SessionState()
        self.saved = (state.auth_type, state.keys)
        state.keys = [{'id': 'myid', 'key': KEY}]
        self.addCleanup(self.restore)

    def restore(self):
        state = SessionState()
        state.auth_type, state.keys = self.saved

    def authorize(self, auth_type, local, data, user=None, headers=None):
        SessionState().auth_type = auth_type
        info = SimpleNamespace(
            context={'request': make_request(local, headers), 'user': user}
        )
        with caller_scope(None):
            data, err = key_auth.authorize(info, data)
            return data, err, get_caller()

    def test_key_mode_via_nginx(self):
        data, err, caller = self.authorize('key', False, {'key': KEY, 'path': ['x']})
        self.assertIsNone(err)
        self.assertEqual(data, {'path': ['x']})
        self.assertEqual(caller.key_id, 'myid')
        for data in ({'path': ['x']}, None, {'key': 'wrong'}):
            self.assertEqual(self.authorize('key', False, data)[1], 'invalid API key')

    def test_token_mode_via_nginx_needs_user(self):
        self.assertEqual(self.authorize('token', False, None)[1], 'not authenticated')
        _, err, caller = self.authorize('token', False, None, user='bob')
        self.assertIsNone(err)
        self.assertEqual(caller.user, 'bob')

    def test_local_socket_needs_no_key_or_token(self):
        for auth_type in ('key', 'token'):
            data, err, caller = self.authorize(
                auth_type,
                True,
                {'key': 'ignored', 'path': ['x']},
                headers={'X-Igos-User': 'alice', 'X-Igos-Via': 'cloud'},
            )
            self.assertIsNone(err)
            self.assertEqual(data, {'path': ['x']})
            self.assertEqual(
                (caller.local, caller.user, caller.via), (True, 'alice', 'cloud')
            )


@unittest.skipIf(httpx is None, 'needs fastapi and httpx')
class TestBackgroundKeepsCaller(unittest.TestCase):
    def test_start_runs_with_callers_context(self):
        manager = BackgroundOpManager()
        seen = []
        done = threading.Event()

        def job():
            seen.append(get_caller())
            done.set()

        with caller_scope(Caller(local=True, user='alice')):
            manager.start(job)
        self.assertTrue(done.wait(5))
        self.assertEqual(seen[0].user, 'alice')

    def test_queued_jobs_keep_their_own_caller(self):
        manager = BackgroundOpManager()
        tasks = BackgroundTasks()
        seen = []
        for user in ('alice', 'bob'):
            with caller_scope(Caller(local=True, user=user)):
                manager.enqueue(tasks, lambda: seen.append(get_caller().user))
        with caller_scope(None):
            manager.drain()
        self.assertEqual(seen, ['alice', 'bob'])


class TestCommitLogUser(unittest.TestCase):
    def get_user(self, login, commit_user):
        from vyos.config_mgmt import ConfigMgmt

        env = {'COMMIT_USER': commit_user} if commit_user else {}
        getlogin = (
            (lambda: login) if login else (lambda: (_ for _ in ()).throw(OSError()))
        )
        with patch.dict(os.environ, env, clear=False), patch('os.getlogin', getlogin):
            if not commit_user:
                os.environ.pop('COMMIT_USER', None)
            return ConfigMgmt._get_user()

    def test_commit_user_used_without_login_terminal(self):
        self.assertEqual(self.get_user(None, 'alice'), 'alice')

    def test_interactive_login_cannot_be_overridden(self):
        self.assertEqual(self.get_user('vyos', 'alice'), 'vyos')


if __name__ == '__main__':
    unittest.main()
