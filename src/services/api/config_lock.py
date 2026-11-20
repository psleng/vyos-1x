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

"""Serialization of operations on the HTTP API's shared config session.

Every REST and GraphQL request drives the same ConfigSession, so any
operation that stages changes in it (set/delete/load/merge/save) or commits
it must hold ``config_lock`` for the whole stage-and-commit sequence.

``config_lock`` is a threading lock: acquire it only from worker threads
(``def`` endpoints, ``run_in_threadpool``, background tasks), never directly
on the event loop.
"""

import time
from threading import Lock

from vyos.configsession import ConfigSessionError
from vyos.utils.commit import commit_in_progress2

config_lock = Lock()

# How long an API commit waits for a commit from another session (CLI, SSH,
# ...) to finish before giving up. The system commit lock itself is
# non-blocking, so without this wait the API commit would fail immediately.
# Commits commonly take 20-40 s; the bound plus our own commit must stay well
# under the GUI's 300 s nginx proxy timeout.
SYSTEM_COMMIT_WAIT_SECONDS = 120
_SYSTEM_COMMIT_POLL_SECONDS = 0.5


def wait_for_system_commit(timeout: float = SYSTEM_COMMIT_WAIT_SECONDS):
    """Wait until no other session holds the system commit lock.

    Raises ConfigSessionError if the lock is still held after ``timeout``
    seconds. A commit that starts between this check and our own commit still
    fails with the commit engine's usual "locked" error.
    """
    deadline = time.monotonic() + timeout
    while commit_in_progress2():
        if time.monotonic() >= deadline:
            raise ConfigSessionError(
                'Configuration system locked by another commit in progress; '
                'try again later'
            )
        time.sleep(_SYSTEM_COMMIT_POLL_SECONDS)
