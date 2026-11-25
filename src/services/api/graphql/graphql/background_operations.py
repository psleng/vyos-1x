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

"""Background operations for GraphQL mutations.

A mutation called with ``in_background: true`` is started here and returns an
operation record at once; clients poll the ``BackgroundOperations`` query.

This manager is separate from the REST one so that a long GraphQL job (image
install, modem firmware flash) never delays a REST ``in_background``
configure. Each operation runs in its own thread; mutations that touch the
config session still serialize on the API config lock.

Records are kept in memory only and are lost when the API restarts.
"""

from typing import Any, Dict, Optional  # pylint: disable=W0611 # noqa: F401

from graphql import GraphQLResolveInfo  # pylint: disable=W0611 # noqa: F401

from ...background import BackgroundOpManager
from ...session import SessionState
from ..libs import key_auth
from .queries import query

background_op_manager = BackgroundOpManager()


@query.field('BackgroundOperations')
def resolve_background_operations(
    obj: Any, info: GraphQLResolveInfo, data: Optional[Dict] = None
):
    state = SessionState()
    data = data or {}

    if state.auth_type == 'key':
        if key_auth.auth_required(data.get('key')) is None:
            return {'success': False, 'errors': ['invalid API key']}
    elif state.auth_type == 'token':
        if info.context.get('user') is None:
            error = info.context.get('error')
            return {'success': False, 'errors': [error or 'not authenticated']}

    op_id = data.get('op_id')
    if op_id:
        record = background_op_manager.get_record(op_id)
        records = [record] if record else []
    else:
        records = background_op_manager.get_records()

    return {
        'success': True,
        'data': {'operations': [record.model_dump() for record in records]},
    }
