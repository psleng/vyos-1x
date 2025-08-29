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
import inspect

# used below by func_sig
from typing import Any, Dict, Optional  # pylint: disable=W0611 # noqa: F401
from graphql import GraphQLResolveInfo  # pylint: disable=W0611 # noqa: F401

from ariadne import ObjectType, convert_camel_case_to_snake
from makefun import with_signature

from vyos.configsession import ConfigSession
from vyos.opmode import Error as OpModeError

from ...session import SessionState
from ..libs import key_auth
from ..session.session import Session
from ..session.errors.op_mode_errors import op_mode_err_code, op_mode_err_msg
import asyncio

subscription = ObjectType('Subscription')

def make_subscription_resolver(subscription_name, class_name, session_func):
    """Dynamically generate a resolver for the subscription named in the
    schema by 'subscription_name'.

    Dynamic generation is provided using the package 'makefun' (via the
    decorator 'with_signature'), which provides signature-preserving
    function wrappers; it provides several improvements over, say,
    functools.wraps.

    :raise Exception:
        raising ConfigErrors, or internal errors
    """

    func_base_name = convert_camel_case_to_snake(class_name)
    resolver_name = f'resolve_subscribe_{func_base_name}'
    func_sig = '(obj: Any, info: GraphQLResolveInfo, data: Optional[Dict]=None)'
    state = SessionState()

    @subscription.field(subscription_name)
    @with_signature(func_sig, func_name=resolver_name)
    async def resolver_impl(*args, **kwargs):
        try:
            # Pass-through for the payload from the generator.
            payload = args[0] if args else kwargs.get('obj')

            # Extract the actual data for the result field
            if isinstance(payload, dict) and 'success' in payload and payload['success']:
                if 'data' in payload:
                    return {
                        'data': {'result': payload['data']}
                    }
            return payload
        except Exception as error:
            return {'success': False, 'errors': [repr(error)]}

    async def generator_impl(obj, info, **kwargs):
        try:
            auth_type = state.auth_type

            data = kwargs.get('data')
            if data is None:
                data = kwargs

            if auth_type == 'key':
                auth_data = data.copy()
                key = auth_data.get('key')
                if not key:
                    yield {'success': False, 'errors': ['API key is missing']}

                auth = key_auth.auth_required(key)
                if auth is None:
                    yield {'success': False, 'errors': ['invalid API key']}

                # We are finished with the 'key' entry, and may remove so as to
                # pass the rest of data (if any) to function.
                if 'key' in data:
                    del data['key']

            elif auth_type == 'token':
                info = kwargs.get('info')
                user = info.context.get('user')
                if user is None:
                    error = info.context.get('error')
                    if error is not None:
                        yield {'success': False, 'errors': [error]}
                    yield {'success': False, 'errors': ['not authenticated']}
            else:
                pass

            session = state.session

            # one may override the session functions with a local subclass
            try:
                mod = import_module(f'api.graphql.session.override.{func_base_name}')
                klass = getattr(mod, class_name)
            except ImportError:
                # otherwise, dynamically generate subclass to invoke subclass
                # name based functions
                klass = type(class_name, (Session,), {})
            k = klass(session, data)
            method = getattr(k, session_func)
            result = method()

            # Handle both async and sync generators
            if inspect.isasyncgen(result):
                async for item in result:
                    yield {'success': True, 'data': item}
            elif inspect.isgenerator(result):
                for item in result:
                    yield {'success': True, 'data': item}
            else:
                # For non-generator results, yield once
                yield {'success': True, 'data': result}
        except OpModeError as e:
            typename = type(e).__name__
            msg = str(e)
            yield {
                'success': False,
                'errors': ['op_mode_error'],
                'op_mode_error': {
                    'name': f'{typename}',
                    'message': msg if msg else op_mode_err_msg.get(typename, 'Unknown'),
                    'vyos_code': op_mode_err_code.get(typename, 9999),
                },
            }
        except Exception as error:
            yield {'success': False, 'errors': [repr(error)]}

    return generator_impl, resolver_impl



def make_gen_op_subscription_resolver(subscription_name):
    return make_subscription_resolver(
        subscription_name, subscription_name, "gen_op_subscription"
    )


def bind_subscription_extensions(subscription_obj, type_defs):
    """Handle binding of subscription extensions from schema."""
    from graphql import parse, TypeExtensionNode

    ast = parse(type_defs)
    subscription_bindings = {
        'genopsubscription': make_gen_op_subscription_resolver,
    }

    for definition in ast.definitions:
        if isinstance(definition, TypeExtensionNode) and definition.name.value == 'Subscription':
            for field in definition.fields:
                for directive in field.directives:
                    if directive.name.value in subscription_bindings:
                        generator, resolver = subscription_bindings[directive.name.value](field.name.value)
                        subscription_obj.set_field(field.name.value, resolver)
                        subscription_obj.set_source(field.name.value, generator)
