# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
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


from typing import Optional

from ...caller import Caller
from ...caller import find_key_id
from ...caller import is_local
from ...caller import local_caller
from ...caller import set_caller
from ...session import SessionState


def check_auth(key_list, key):
    return find_key_id(key_list, key)


def auth_required(key):
    state = SessionState()
    api_keys = None
    api_keys = state.keys
    key_id = check_auth(api_keys, key)
    state.id = key_id
    return key_id


def authorize(info, data: Optional[dict]) -> tuple[dict, Optional[str]]:
    """Authenticate a GraphQL resolver call; return (data, error).

    Local-socket requests (api.caller) need no key or token. Otherwise the
    configured auth type applies. On success any 'key' entry is removed from
    the returned data and the per-request caller is set.
    """
    state = SessionState()
    data = data if data is not None else {}
    request = info.context.get('request') if info is not None else None

    if request is not None and is_local(request.scope):
        data.pop('key', None)
        set_caller(local_caller(request.headers))
        return data, None

    if state.auth_type == 'key':
        key_id = auth_required(data.get('key'))
        if key_id is None:
            return data, 'invalid API key'
        data.pop('key', None)
        set_caller(Caller(key_id=key_id))
        return data, None

    if state.auth_type == 'token':
        user = info.context.get('user')
        if user is None:
            return data, info.context.get('error') or 'not authenticated'
        set_caller(Caller(user=user))
        return data, None

    return data, None
