#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import json
import os
import select
import signal
from pathlib import Path

from vyos.alerts.catalog import load_catalog
from vyos.alerts.delivery import Delivery
from vyos.alerts.engine import Engine
from vyos.alerts.record import History
from vyos.utils.commit import commit_in_progress2

from systemd import journal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    delivery = Delivery(config)
    history = History(
        size=config.get('history', {}).get('size', 1000), sender=journal.send
    )
    engine = Engine(
        config,
        history,
        delivery,
        catalog=load_catalog() if config.get('alert') else None,
        committing=commit_in_progress2,
    )
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    data = journal.Reader()
    data.seek_tail()
    data.get_previous()
    poll = select.poll()
    poll.register(data, data.get_events())
    delivery.start()
    journal.send('Event handler started', SYSLOG_IDENTIFIER='vyos-event-handler')
    try:
        while not stopping:
            if not poll.poll(1000) or data.process() != journal.APPEND:
                continue
            for entry in data:
                if str(entry.get('_PID')) == str(os.getpid()):
                    continue
                try:
                    engine.process_message(entry)
                except Exception as exc:  # noqa: BLE001 - isolate malformed journal records
                    journal.send(
                        f'Unable to process alert: {type(exc).__name__}',
                        SYSLOG_IDENTIFIER='vyos-event-handler',
                    )
    finally:
        delivery.close()
        data.close()


if __name__ == '__main__':
    main()
