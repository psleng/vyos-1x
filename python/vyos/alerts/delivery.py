"""Independent durable bounded delivery queues. Delivery is at least once."""

import asyncio
import copy
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from vyos.alerts.record import ROOT, atomic_json

CHANNELS = ('email', 'sms', 'script')


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Delivery:
    def __init__(self, config, root=ROOT, clock=time.time, handlers=None):
        self.root, self.config, self.clock = Path(root), config, clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.threads = []
        self.handlers = handlers or {
            'email': self.email,
            'sms': self.sms,
            'script': self.script,
        }
        self.queues, self.status = {}, {}
        for channel in CHANNELS:
            path = self.root / 'queue' / (channel + '.json')
            try:
                data = json.loads(path.read_text())
            except FileNotFoundError:
                data = {}
            self.queues[channel] = data.get('items', [])
            self.status[channel] = data.get(
                'status',
                {
                    'dropped': 0,
                    'expired': 0,
                    'last_result': None,
                    'window': clock(),
                    'sent': 0,
                    'limited': 0,
                },
            )
        self.persist()

    def persist(self):
        for channel in CHANNELS:
            atomic_json(
                self.root / 'queue' / (channel + '.json'),
                {'items': self.queues[channel], 'status': self.status[channel]},
            )
        atomic_json(
            self.root / 'status.json',
            {
                c: dict(
                    {k: v for k, v in self.status[c].items() if k != 'summary'},
                    depth=len(self.queues[c]),
                )
                for c in CHANNELS
            },
        )

    def enqueue(self, record, rule):
        targets = {c: as_list(rule.get('notify', {}).get(c)) for c in CHANNELS}
        if rule.get('script'):
            targets['script'].append(rule['script'])
        with self.lock:
            for channel, destinations in targets.items():
                for target in destinations:
                    queue = self.queues[channel]
                    queue.append(
                        {
                            'id': str(uuid.uuid4()),
                            'record': copy.deepcopy(record),
                            'target': target,
                            'created': self.clock(),
                            'next': self.clock(),
                            'attempts': 0,
                        }
                    )
                    if len(queue) > 1000:
                        priority = {'info': 0, 'warning': 1, 'critical': 2}
                        victim = min(
                            range(len(queue)),
                            key=lambda i: (
                                priority[queue[i]['record']['severity']],
                                queue[i]['created'],
                            ),
                        )
                        queue.pop(victim)
                        self.status[channel]['dropped'] += 1
            self.persist()

    def suppress(self, record_id):
        with self.lock:
            for queue in self.queues.values():
                for item in queue:
                    if item['record']['id'] == record_id:
                        item['record']['suppressed'] += 1
            self.persist()

    def start(self):
        for channel in CHANNELS:
            thread = threading.Thread(target=self.worker, args=(channel,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=35)

    def worker(self, channel):
        while not self.stop.is_set():
            if not self.step(channel):
                self.stop.wait(0.5)

    def step(self, channel):
        now = self.clock()
        with self.lock:
            queue, status = self.queues[channel], self.status[channel]
            expired = [q for q in queue if now - q['created'] >= 86400]
            for q in expired:
                queue.remove(q)
            status['expired'] += len(expired)
            if now - status['window'] >= 3600:
                status.update(window=now, sent=0)
                summary = status.pop('summary', None)
                if summary and now - summary['created'] >= 86400:
                    status['expired'] += 1
                    summary = None
                if summary:
                    summary['record']['message'] = (
                        f'{summary.pop("count")} alerts suppressed, see history'
                    )
                    summary['created'] = now
                    summary['next'] = now
                    summary['attempts'] = 0
                    summary['id'] = str(uuid.uuid4())
                    queue.insert(0, summary)
                    if len(queue) > 1000:
                        queue.pop()
                        status['dropped'] += 1
            item = next((q for q in queue if q['next'] <= now), None)
            if item is None:
                if expired:
                    self.persist()
                return False
            limit = int(
                self.config.get('rate_limit', {}).get(
                    channel, 20 if channel == 'sms' else 60
                )
            )
            if channel != 'script' and status['sent'] >= limit:
                if channel == 'sms':
                    # One summary per window, to the first suppressed destination.
                    # The summary consumes a delivery slot in the next window.
                    if 'summary' not in status:
                        status['summary'] = copy.deepcopy(item)
                        status['summary']['count'] = 0
                    status['summary']['count'] += 1
                    queue.remove(item)
                else:
                    item['next'] = status['window'] + 3600
                status['limited'] += 1
                self.persist()
                return True
            work = copy.deepcopy(item)
        try:
            outcome = self.handlers[channel](work['record'], work['target']) or 'sent'
            error = None
        except Exception as exc:  # noqa: BLE001 - isolate all channel failures
            # Never store exception text, which may contain credentials or log data.
            error = type(exc).__name__
        with self.lock:
            status['last_result'] = {
                'time': now,
                'id': work['record']['id'],
                'result': error or outcome,
            }
            live = next((q for q in queue if q['id'] == work['id']), None)
            if error and live:
                live['attempts'] += 1
                live['next'] = now + min(300, 2 ** min(live['attempts'], 9))
            elif not error:
                if outcome == 'sent':
                    status['sent'] += 1
                if live:
                    queue.remove(live)
            self.persist()
        return True

    def email(self, record, target):
        from vyos.utils.email import email_settings, send_mail

        account, recipients = email_settings()
        recipient = recipients.get(target)
        if not recipient:
            raise ValueError('Email recipient no longer configured')
        if not recipient['enabled']:
            return 'muted'
        if not account:
            raise RuntimeError('No active SMTP profile')
        subject = f'{recipient["subject"] or "igOS Alert"} [{record["hostname"]}] {record["severity"].upper()}: {record["message"]}'
        send_mail([recipient['email']], subject, json.dumps(record, indent=2), account)

    def sms(self, record, target):
        recipient = self.config.get('sms_recipient', {}).get(target)
        if not recipient:
            raise ValueError('SMS recipient no longer configured')
        text = (
            f'[{record["hostname"]}] {record["severity"].upper()} {record["message"]}'
        )
        # GSM extensions and Unicode can require multiple segments. ASCII subset
        # without extension-table characters guarantees one GSM segment.
        text = ''.join(
            c if 32 <= ord(c) < 127 and c not in '^{}\\[~]|`' else '?' for c in text
        )[:160]

        async def send():
            from vyos.utils.wwan.wwan_client import WWANClient

            async with WWANClient() as client:
                interface = recipient.get('interface')
                if interface:
                    number = int(interface.removeprefix('wwan'))
                else:
                    number = None
                    for name in sorted(self.config.get('_wwan', [])):
                        candidate = int(name.removeprefix('wwan'))
                        try:
                            if await client.is_connected(candidate):
                                number = candidate
                                break
                        except Exception:  # noqa: BLE001, S112 - try the next modem
                            continue
                    if number is None:
                        raise RuntimeError('No connected WWAN modem')
                result = await client.send_sms(number, recipient['number'], text)
                if result.get('status') != 'sent':
                    raise RuntimeError('SMS was not sent')

        asyncio.run(asyncio.wait_for(send(), timeout=30))

    def script(self, record, target):
        if isinstance(target, str):
            target = {'path': target}
        environment = os.environ.copy()
        environment.update(
            {k: v['value'] for k, v in target.get('environment', {}).items()}
        )
        environment['message'] = record['log'].get('MESSAGE', '')
        environment.update(
            {
                'ALERT_' + k.upper(): str(record.get(k, ''))
                for k in ('id', 'type', 'severity', 'object', 'state', 'message')
            }
        )
        # Existing script arguments are shell syntax; retain that interface.
        import shlex

        command = shlex.quote(target['path']) + ' ' + target.get('arguments', '')
        process = subprocess.Popen(
            command,
            shell=True,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        if code:
            raise RuntimeError('Alert script failed')
