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

"""GraphQL background operations (`in_background` + `BackgroundOperations`).

Builds the real executable schema from the generator's mutation template for
a fake op-mode mutation and drives it the way a client does.
"""

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/services'))

try:
    import ariadne
    from fastapi import BackgroundTasks
except ImportError:
    ariadne = None

if ariadne is not None:
    from vyos import opmode
    from api.background import BackgroundOpError, BackgroundOpManager
    from api.background import BackgroundOpStatus
    from api.session import SessionState
    from api.graphql.generate import schema_from_op_mode
    from api.graphql.graphql.directives import directives_dict
    from api.graphql.graphql.errors import op_mode_error
    from api.graphql.graphql.mutations import mutation
    from api.graphql.graphql.queries import query
    from api.graphql.session.session import Session

SCHEMA_DIR = ROOT / 'src/services/api/graphql/graphql/schema'
KEY = 'test-key'

MUTATION = """
mutation ($data: AddThingDemoInput) {
  AddThingDemo(data: $data) {
    success
    errors
    data { result }
    operation { op_id name status result error }
  }
}
"""

POLL = """
query ($data: BackgroundOperationsInput) {
  BackgroundOperations(data: $data) {
    success
    errors
    data { operations { op_id name status result error } }
  }
}
"""

TIMESTAMPS = """
query ($data: BackgroundOperationsInput) {
  BackgroundOperations(data: $data) {
    data { operations { created_at started_at finished_at } }
  }
}
"""


def add_thing(name: str):
    """Fake op-mode mutation; the schema is generated from this signature"""


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@unittest.skipIf(ariadne is None, 'needs ariadne and fastapi')
class TestGraphQLBackground(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        type_defs = [
            (SCHEMA_DIR / 'schema.graphql').read_text(),
            (SCHEMA_DIR / 'background_operations.graphql').read_text(),
            schema_from_op_mode.create_error_schema(),
            schema_from_op_mode.create_schema('add_thing', 'demo', add_thing, {}),
        ]
        cls.schema = ariadne.make_executable_schema(
            type_defs,
            query,
            op_mode_error,
            mutation,
            ariadne.snake_case_fallback_resolvers,
            directives=directives_dict,
        )
        state = SessionState()
        state.auth_type = 'key'
        state.keys = [{'id': 'test', 'key': KEY}]

    def setUp(self):
        self.calls = []
        self.behavior = lambda data: f'added {data["name"]}'

        def gen_op_mutation(session):
            self.calls.append(dict(session._data))
            return self.behavior(session._data)

        patcher = patch.object(Session, 'gen_op_mutation', gen_op_mutation)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_query(self, document, data):
        ok, response = asyncio.run(
            ariadne.graphql(
                self.schema,
                {'query': document, 'variables': {'data': data}},
                context_value={},
            )
        )
        self.assertTrue(ok, response)
        self.assertNotIn('errors', response, response)
        return response['data']

    def poll(self, op_id, key=KEY):
        result = self.run_query(POLL, {'key': key, 'op_id': op_id})
        return result['BackgroundOperations']

    def operation(self, op_id):
        operations = self.poll(op_id)['data']['operations']
        self.assertEqual(len(operations), 1)
        return operations[0]

    def test_background_returns_at_once_and_poll_shows_result(self):
        release = threading.Event()

        def slow(data):
            release.wait(5)
            return f'added {data["name"]}'

        self.behavior = slow
        response = self.run_query(
            MUTATION, {'key': KEY, 'name': 'x', 'in_background': True}
        )['AddThingDemo']

        self.assertTrue(response['success'])
        op = response['operation']
        self.assertEqual(op['name'], 'AddThingDemo')
        self.assertIn(op['status'], ('queued', 'running'))
        self.assertIsNone(response['data']['result'])
        self.assertIn(self.operation(op['op_id'])['status'], ('queued', 'running'))

        release.set()
        self.assertTrue(
            wait_until(lambda: self.operation(op['op_id'])['status'] == 'succeeded')
        )
        record = self.operation(op['op_id'])
        self.assertIsNone(record['error'])
        self.assertEqual(
            record['result'],
            {'success': True, 'data': {'name': 'x', 'result': 'added x'}},
        )
        self.assertEqual(self.calls, [{'name': 'x'}])

    def test_background_op_mode_error_is_kept(self):
        def fail(data):
            raise opmode.UnconfiguredSubsystem('modem not present')

        self.behavior = fail
        response = self.run_query(
            MUTATION, {'key': KEY, 'name': 'x', 'in_background': True}
        )['AddThingDemo']
        op_id = response['operation']['op_id']

        self.assertTrue(wait_until(lambda: self.operation(op_id)['status'] == 'failed'))
        record = self.operation(op_id)
        self.assertEqual(record['error'], 'modem not present')
        self.assertFalse(record['result']['success'])
        self.assertEqual(record['result']['errors'], ['op_mode_error'])
        self.assertEqual(
            record['result']['op_mode_error']['name'], 'UnconfiguredSubsystem'
        )

    def test_synchronous_call_unchanged(self):
        response = self.run_query(
            MUTATION, {'key': KEY, 'name': 'y', 'in_background': False}
        )['AddThingDemo']

        self.assertTrue(response['success'])
        self.assertEqual(response['data']['result'], 'added y')
        self.assertIsNone(response['operation'])
        self.assertEqual(self.calls, [{'name': 'y'}])

    def test_invalid_key_rejected(self):
        response = self.run_query(
            MUTATION, {'key': 'wrong', 'name': 'x', 'in_background': True}
        )['AddThingDemo']
        self.assertFalse(response['success'])
        self.assertIsNone(response['operation'])
        self.assertEqual(self.calls, [])

        poll = self.poll(None, key='wrong')
        self.assertFalse(poll['success'])
        self.assertEqual(poll['errors'], ['invalid API key'])

    def test_poll_unknown_id_and_list(self):
        self.assertEqual(self.poll('no-such-id')['data']['operations'], [])

        response = self.run_query(
            MUTATION, {'key': KEY, 'name': 'z', 'in_background': True}
        )['AddThingDemo']
        op_id = response['operation']['op_id']
        operations = self.poll(None)['data']['operations']
        self.assertIn(op_id, [op['op_id'] for op in operations])

    def test_timestamps_after_2038(self):
        after_2038 = 2**31 + 100
        with patch('api.background._ts', return_value=after_2038):
            response = self.run_query(
                MUTATION, {'key': KEY, 'name': 'x', 'in_background': True}
            )['AddThingDemo']
            op_id = response['operation']['op_id']
            self.assertTrue(
                wait_until(lambda: self.operation(op_id)['status'] == 'succeeded')
            )

        result = self.run_query(TIMESTAMPS, {'key': KEY, 'op_id': op_id})
        (record,) = result['BackgroundOperations']['data']['operations']
        self.assertEqual(record['created_at'], after_2038)
        self.assertEqual(record['finished_at'], after_2038)


@unittest.skipIf(ariadne is None, 'needs ariadne and fastapi')
class TestBackgroundOpManager(unittest.TestCase):
    def test_start_does_not_serialize_operations(self):
        manager = BackgroundOpManager()
        release = threading.Event()
        slow = manager.start(lambda: release.wait(5), name='slow')
        fast = manager.start(lambda: 'done', name='fast')

        self.assertTrue(
            wait_until(
                lambda: (
                    manager.get_record(fast.op_id).status
                    == BackgroundOpStatus.succeeded
                )
            )
        )
        self.assertEqual(
            manager.get_record(slow.op_id).status, BackgroundOpStatus.running
        )
        release.set()
        self.assertTrue(
            wait_until(lambda: manager.get_record(slow.op_id).status.is_completed)
        )

    def test_start_limits_active_operations(self):
        manager = BackgroundOpManager(max_queue_size=1)
        release = threading.Event()
        first = manager.start(lambda: release.wait(5))

        with self.assertRaises(BackgroundOpError):
            manager.start(lambda: None)

        release.set()
        self.assertTrue(
            wait_until(lambda: manager.get_record(first.op_id).status.is_completed)
        )
        manager.start(lambda: None)

    def test_enqueue_still_runs_in_order(self):
        manager = BackgroundOpManager()
        order = []
        tasks = BackgroundTasks()
        first = manager.enqueue(tasks, order.append, 1)
        second = manager.enqueue(tasks, lambda: 1 / 0)
        self.assertEqual(len(tasks.tasks), 1)

        manager.drain()

        self.assertEqual(order, [1])
        self.assertEqual(
            manager.get_record(first.op_id).status, BackgroundOpStatus.succeeded
        )
        failed = manager.get_record(second.op_id)
        self.assertEqual(failed.status, BackgroundOpStatus.failed)
        self.assertEqual(failed.error, 'division by zero')


if __name__ == '__main__':
    unittest.main()
