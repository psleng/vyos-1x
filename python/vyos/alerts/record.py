"""Bounded alert history and atomic, private JSON storage shared with op-mode."""

import fcntl
import json
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/var/lib/vyos/alerts')
BOOT_ID = (
    Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if Path('/proc/sys/kernel/random/boot_id').exists()
    else str(uuid.uuid4())
)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def history_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'history.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_history(root=ROOT):
    path = Path(root) / 'history.jsonl'
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line]
    except FileNotFoundError:
        return []


def write_history(root, records):
    path = Path(root) / 'history.jsonl'
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        for record in records:
            stream.write(json.dumps(record) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class History:
    def __init__(self, root=ROOT, size=1000, sender=None):
        self.root, self.size, self.sender = Path(root), int(size), sender
        self.sequence = 0
        self.boot_id = BOOT_ID
        self.lock = threading.RLock()
        with history_lock(self.root):
            records = read_history(self.root)
            self.sequence = max(
                (r['sequence'] for r in records if r.get('boot_id') == self.boot_id),
                default=0,
            )
            try:
                cursor = json.loads((self.root / 'sequence.json').read_text())
                if cursor['boot_id'] == self.boot_id:
                    self.sequence = max(self.sequence, cursor['sequence'])
            except FileNotFoundError:
                pass

    def record(self, rule, entry, values):
        self.sequence += 1
        atomic_json(
            self.root / 'sequence.json',
            {'boot_id': self.boot_id, 'sequence': self.sequence},
        )
        timestamp = entry.get('__REALTIME_TIMESTAMP', datetime.now(UTC))
        if isinstance(timestamp, datetime):
            timestamp = timestamp.astimezone(UTC).isoformat()
        severity = rule['severity']
        if rule.get('source_severity') and not rule.get('severity_override'):
            severity = str(entry.get('WWAN_ALERT_SEVERITY', severity)).lower()
        if severity not in ('critical', 'warning', 'info'):
            severity = rule['severity']

        def render(key):
            return rule.get(key, '').format_map(values)

        return {
            'id': str(uuid.uuid4()),
            'boot_id': self.boot_id,
            'sequence': self.sequence,
            'timestamp': str(timestamp),
            'hostname': socket.gethostname(),
            'rule': rule['name'],
            'type': rule['type'],
            'category': rule['category'],
            'severity': severity,
            'object': render('object'),
            'state': render('state'),
            'message': render('message'),
            'log': {
                k: str(entry[k])
                for k in ('SYSLOG_IDENTIFIER', '_SYSTEMD_UNIT', 'MESSAGE')
                if k in entry
            },
            'suppressed': 0,
        }

    def append(self, record):
        with self.lock, history_lock(self.root):
            records = read_history(self.root)
            records.append(record)
            write_history(self.root, records[-self.size :])
        if self.sender:
            self.sender(
                record['message'],
                SYSLOG_IDENTIFIER='igos-alert',
                **{
                    'ALERT_' + k.upper(): str(v)
                    for k, v in record.items()
                    if k != 'log'
                },
            )

    def suppress(self, record_id):
        with self.lock, history_lock(self.root):
            records = read_history(self.root)
            for record in records:
                if record['id'] == record_id:
                    record['suppressed'] += 1
                    break
            write_history(self.root, records)
