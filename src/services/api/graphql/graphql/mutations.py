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

from importlib import import_module

# used below by func_sig
from typing import Any, Dict, Optional  # pylint: disable=W0611 # noqa: F401
from graphql import GraphQLResolveInfo  # pylint: disable=W0611 # noqa: F401

from ariadne import ObjectType, convert_camel_case_to_snake
from fastapi.concurrency import run_in_threadpool
from makefun import with_signature

from vyos.opmode import Error as OpModeError

from ...background import BackgroundOpError, BackgroundOpFailed
from ...config_lock import config_lock
from ...session import SessionState
from ..libs import key_auth
from ..session.session import Session
from ..session.errors.op_mode_errors import op_mode_err_msg, op_mode_err_code
from .background_operations import background_op_manager

mutation = ObjectType('Mutation')

# Mutations that run long and never use the shared config session: system image
# add/delete/rename/set-default (in-tree and Perle) and cellular modem firmware
# flash/archive/cache/check (wwan_firmware.py). Holding the config lock for
# them would block every API commit for minutes.
_CONFIG_LOCK_EXEMPT = ('SystemImage', 'WwanFirmware')


def uses_config_lock(class_name: str) -> bool:
    """Whether a mutation must hold the API config lock.

    Any mutation may stage or commit changes in the shared config session
    (op-mode scripts run in-process and can reach it via SessionState), so
    the lock is the default; see _CONFIG_LOCK_EXEMPT for the exceptions.
    """
    return not any(marker in class_name for marker in _CONFIG_LOCK_EXEMPT)


def op_mode_error_response(e: OpModeError) -> dict:
    typename = type(e).__name__
    msg = str(e)
    return {
        'success': False,
        'errors': ['op_mode_error'],
        'op_mode_error': {
            'name': f'{typename}',
            'message': msg if msg else op_mode_err_msg.get(typename, 'Unknown'),
            'vyos_code': op_mode_err_code.get(typename, 9999),
        },
    }


def run_background_mutation(execute, data: dict) -> dict:
    """Run a mutation as a background operation; return its normal response.

    A failed mutation raises BackgroundOpFailed carrying that response, so
    the operation record shows `failed` with the response as its result.
    """
    try:
        result = execute()
    except OpModeError as e:
        response = op_mode_error_response(e)
        raise BackgroundOpFailed(response['op_mode_error']['message'], response)
    except Exception as error:
        response = {'success': False, 'errors': [repr(error)]}
        raise BackgroundOpFailed(repr(error), response)
    return {'success': True, 'data': {**data, 'result': result}}


def make_mutation_resolver(mutation_name, class_name, session_func):
    """Dynamically generate a resolver for the mutation named in the
    schema by 'mutation_name'.

    Dynamic generation is provided using the package 'makefun' (via the
    decorator 'with_signature'), which provides signature-preserving
    function wrappers; it provides several improvements over, say,
    functools.wraps.

    :raise Exception:
        raising ConfigErrors, or internal errors
    """

    func_base_name = convert_camel_case_to_snake(class_name)
    resolver_name = f'resolve_{func_base_name}'
    func_sig = '(obj: Any, info: GraphQLResolveInfo, data: Optional[Dict]=None)'
    state = SessionState()
    locked = uses_config_lock(class_name)

    @mutation.field(mutation_name)
    @with_signature(func_sig, func_name=resolver_name)
    async def func_impl(*args, **kwargs):
        try:
            data, auth_error = key_auth.authorize(
                kwargs.get('info'), kwargs.get('data')
            )
            if auth_error is not None:
                return {'success': False, 'errors': [auth_error]}

            # Never pass the background flag on to the mutation itself
            in_background = bool(data.pop('in_background', False))

            session = state.session

            # one may override the session functions with a local subclass
            try:
                mod = import_module(f'api.graphql.session.override.{func_base_name}')
                klass = getattr(mod, class_name)
            except ImportError:
                # otherwise, dynamically generate subclass to invoke subclass
                # name based functions
                klass = type(class_name, (Session,), {})

            # Run in the threadpool so the event loop stays free; the config
            # lock is taken inside the worker thread, never on the loop
            def execute(data):
                k = klass(session, data)
                method = getattr(k, session_func)
                if locked:
                    with config_lock:
                        return method()
                return method()

            if in_background:
                job_data = dict(data)
                try:
                    record = background_op_manager.start(
                        lambda: run_background_mutation(
                            lambda: execute(job_data), job_data
                        ),
                        name=mutation_name,
                    )
                except BackgroundOpError as e:
                    return {'success': False, 'errors': [str(e)]}
                return {'success': True, 'data': data, 'operation': record.model_dump()}

            result = await run_in_threadpool(execute, data)
            data['result'] = result

            return {'success': True, 'data': data}
        except OpModeError as e:
            return op_mode_error_response(e)
        except Exception as error:
            return {'success': False, 'errors': [repr(error)]}

    return func_impl


def make_config_session_mutation_resolver(mutation_name):
    return make_mutation_resolver(
        mutation_name, mutation_name, convert_camel_case_to_snake(mutation_name)
    )


def make_gen_op_mutation_resolver(mutation_name):
    return make_mutation_resolver(mutation_name, mutation_name, 'gen_op_mutation')


def make_composite_mutation_resolver(mutation_name):
    return make_mutation_resolver(
        mutation_name, mutation_name, convert_camel_case_to_snake(mutation_name)
    )
