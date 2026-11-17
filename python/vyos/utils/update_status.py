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

"""Cached results of the periodic update checks (system image, cellular firmware).

The check daemons write the result of every attempt, successful or not, so
that show commands and the GUI can answer from the cache instead of fetching
from the internet on every request.
"""

import json
import os
import tempfile
import time
from pathlib import Path

SYSTEM_UPDATE_STATUS = Path('/run/vyos-update-status.json')
CELLULAR_FIRMWARE_STATUS = Path('/run/vyos-cellular-firmware-status.json')
CELLULAR_FIRMWARE_MOTD = Path('/run/motd.d/11-vyos-cellular-firmware')
CELLULAR_FIRMWARE_SERVICE = 'vyos-wwan-firmware-check.service'

UPDATE_CHECK_BASE = ['system', 'update-check']
CELLULAR_FIRMWARE_BASE = UPDATE_CHECK_BASE + ['cellular-firmware']
WWAN_BASE = ['interfaces', 'wwan']

# Daemons check every 12 h; allow an hour of slack before treating a
# successful result as stale.
CHECK_INTERVAL = 12 * 60 * 60
SUCCESS_MAX_AGE = CHECK_INTERVAL + 60 * 60

# After a failed check the daemons retry with backoff, capped at
# RETRY_MAX; a failed result stays fresh until the next retry is due, so
# readers do not re-fetch while the daemon is already retrying.
RETRY_MIN = 5 * 60
RETRY_MAX = 60 * 60
ERROR_MAX_AGE = RETRY_MAX + 5 * 60


def write_status(path, url, error=None, **data) -> None:
    """Atomically record a check attempt; best effort (callers may lack root)."""
    path = Path(path)
    record = {'checked_at': time.time(), 'url': url, 'error': error, **data}
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, mode='w', delete=False, prefix=f'.{path.name}.'
        ) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream)
        temporary.chmod(0o644)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_status(path, url) -> dict | None:
    """Return the cached record if it is for ``url`` and still fresh, else None."""
    try:
        record = json.loads(Path(path).read_text())
        checked_at = float(record['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if record.get('url') != url:
        return None
    max_age = ERROR_MAX_AGE if record.get('error') else SUCCESS_MAX_AGE
    if not 0 <= time.time() - checked_at <= max_age:
        return None
    return record


def read_last_status(path) -> dict | None:
    """Return the most recent record regardless of URL or age."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def next_retry(previous: int | None) -> int:
    """Backoff delay after a failed check: RETRY_MIN doubling up to RETRY_MAX."""
    if not previous:
        return RETRY_MIN
    return min(previous * 2, RETRY_MAX)


def compare_system_version(url) -> dict | None:
    """Compare the running image with the latest one listed at ``url``.

    Returns the same dict as `show system updates` raw output, or None when
    the local version is unknown.
    """
    import jmespath
    import vyos.version

    local_data = vyos.version.get_full_version_data()
    remote_data = vyos.version.get_remote_version(url)
    if not remote_data:
        return {'error': True, 'reason': 'Unable to get remote version'}
    local_version = local_data.get('version')
    if not local_version:
        return None
    remote_version = jmespath.search('[0].version', remote_data)
    image_url = jmespath.search('[0].url', remote_data)
    if local_version != remote_version:
        return {
            'error': False,
            'update_available': True,
            'local_version': local_version,
            'remote_version': remote_version,
            'url': image_url,
        }
    return {
        'update_available': False,
        'local_version': local_version,
        'remote_version': remote_version,
    }


def check_system_version(url) -> dict | None:
    """Fetch, compare, and cache the system image update status for ``url``."""
    result = compare_system_version(url)
    error = result.get('reason') if result and result.get('error') else None
    write_status(SYSTEM_UPDATE_STATUS, url, error=error, result=result)
    return result


def cached_system_version(url) -> dict | None:
    """Cached status for ``url`` if fresh, else a live check (which is cached)."""
    record = read_status(SYSTEM_UPDATE_STATUS, url)
    if record is not None and 'result' in record:
        return record['result']
    return check_system_version(url)


def cellular_firmware_checks_disabled(config) -> bool:
    """True if `system update-check disable` or `... cellular-firmware disable`.

    ``config`` is any object with ``exists(path)`` (Config, ConfigTreeQuery).
    """
    return config.exists(UPDATE_CHECK_BASE + ['disable']) or config.exists(
        CELLULAR_FIRMWARE_BASE + ['disable']
    )


def cellular_firmware_check_wanted(config) -> bool:
    """The cellular firmware check runs only on units with a modem: when an
    enabled wwan interface is configured and checks are not disabled."""
    if cellular_firmware_checks_disabled(config) or not config.exists(WWAN_BASE):
        return False
    return any(
        not config.exists(WWAN_BASE + [name, 'disable'])
        for name in config.list_nodes(WWAN_BASE)
    )


def sync_cellular_firmware_check(wanted: bool, restart: bool = False) -> None:
    """Start or stop the cellular firmware check daemon.

    The unit has no [Install] section, like the WWAN manager: conf_mode is the
    only thing that starts it, so units without a modem never run it.
    """
    from vyos.utils.process import call

    if wanted:
        action = 'restart' if restart else 'start'
        call(f'systemctl {action} {CELLULAR_FIRMWARE_SERVICE}')
    else:
        call(f'systemctl stop {CELLULAR_FIRMWARE_SERVICE}')
        CELLULAR_FIRMWARE_MOTD.unlink(missing_ok=True)
