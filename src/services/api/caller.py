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

"""Per-request caller identity for the HTTP API.

Requests arrive on one of two Unix sockets:

- ``/run/api.sock`` — published by nginx; every request needs an API key
  (or a GraphQL token in token mode).
- ``/run/api-local.sock`` — root-only (mode 0600), never published by nginx.
  Only root processes on the unit (the GUI, the cloud agent) can connect, so
  the socket permission is the authorization and no key is needed. Only here
  may the caller assert the acting user (``X-Igos-User``) and channel
  (``X-Igos-Via``) for commit-log attribution.

The caller is kept in a ContextVar, not on the shared SessionState, so
concurrent requests cannot see each other's identity.
"""

import hmac
import os
import re
import socket
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

LOCAL_SOCKET = '/run/api-local.sock'
SCOPE_LOCAL = 'igos.api_local'

USER_HEADER = 'x-igos-user'
VIA_HEADER = 'x-igos-via'

# The commit log uses '|' as field separator; keep values to a safe charset
_USER_RE = re.compile(r'[A-Za-z0-9._@+-]{1,64}')
_VIA_RE = re.compile(r'[a-z0-9-]{1,16}')


@dataclass(frozen=True)
class Caller:
    local: bool = False
    key_id: Optional[str] = None
    user: Optional[str] = None
    via: Optional[str] = None


_caller: ContextVar[Optional[Caller]] = ContextVar('igos_api_caller', default=None)


def get_caller() -> Optional[Caller]:
    return _caller.get()


def set_caller(caller: Optional[Caller]) -> None:
    _caller.set(caller)


@contextmanager
def caller_scope(caller: Optional[Caller]):
    token = _caller.set(caller)
    try:
        yield
    finally:
        _caller.reset(token)


class LocalSocketMarker:
    """ASGI wrapper for the local listener: marks every request as local"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') in ('http', 'websocket'):
            scope = dict(scope)
            scope[SCOPE_LOCAL] = True
        await self.app(scope, receive, send)


def is_local(scope) -> bool:
    return bool(scope and scope.get(SCOPE_LOCAL))


def _clean(value: Optional[str], pattern: re.Pattern) -> Optional[str]:
    if value and pattern.fullmatch(value):
        return value
    return None


def local_caller(headers) -> Caller:
    """Caller for a local-socket request; invalid identity headers are dropped"""
    return Caller(
        local=True,
        user=_clean(headers.get(USER_HEADER), _USER_RE),
        via=_clean(headers.get(VIA_HEADER), _VIA_RE),
    )


def find_key_id(key_list, key) -> Optional[str]:
    """Return the id of the configured key equal to `key` (constant time)"""
    if not key_list or not isinstance(key, str) or not key:
        return None
    offered = key.encode()
    key_id = None
    for k in key_list:
        if hmac.compare_digest(k['key'].encode(), offered):
            key_id = k['id']
    return key_id


@contextmanager
def commit_actor_env(env):
    """Expose the acting user to the commit-log hook for one commit.

    The API's config session environment is the process environment, which
    the commit engine and its post-commit hooks inherit. Callers hold the API
    config lock, so commits never overlap.
    """
    caller = get_caller()
    values = {}
    if caller is not None and caller.local and caller.user:
        values['COMMIT_USER'] = caller.user
        if caller.via:
            values['COMMIT_VIA'] = caller.via
    saved = {name: env.get(name) for name in values}
    env.update(values)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                env.pop(name, None)
            else:
                env[name] = old


def bind_local_socket(path: str = LOCAL_SOCKET) -> socket.socket:
    """Create the root-only listening socket for local callers"""
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old_umask)
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)
    sock.set_inheritable(True)
    return sock
