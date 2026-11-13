# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
"""Opt-in SMS command authentication, execution, replies and message helpers.

The WWAN state machine owns ordinary SMS storage, transport and listener methods.
This module adds no independent service or import-time I/O.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import secrets
import shlex
import signal
import time
import uuid
from collections import deque
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import NamedTuple

# Reuse the manager's configured WWAN logger; add no logging service/handler.
logger = logging.getLogger('vyos.utils.wwan.interfaces_wwan_state_machine')

# Command admission and response limits.
WRAPPER = '/opt/vyatta/bin/vyatta-op-cmd-wrapper'
CONFIRM_TTL = 120
GSM_SEGMENT_SIZE = 150
# The legacy wrapper evaluates templates internally. Do not pass shell syntax,
# control characters, escapes, or globs through it, even as argv elements.
SAFE_ARGUMENT = re.compile(r'[A-Za-z0-9_./:@%+=, -]+\Z')

UNICODE_SEGMENT_SIZE = 67
PASSWORD_MASK = '********'
TRUNCATION_MARKER = '...'

# Modem sender numbers may omit +; configured allowlist numbers must include it.
MODEM_NUMBER_PATTERN = re.compile(r'\+?[1-9][0-9]{1,14}\Z')
CONFIG_NUMBER_PATTERN = re.compile(r'\+[1-9][0-9]{1,14}\Z')

# GSM extension characters consume two septets; other GSM characters consume one.
GSM_BASIC_CHARACTERS = frozenset(
    '@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà'
)
GSM_EXTENSION_CHARACTERS = frozenset('^{}\\[~]|€')


class PendingAction(NamedTuple):
    expires_at: float
    nonce: str
    words: list[str]


# Catalog and message helpers.


@lru_cache(maxsize=1)
def load_catalog():
    installed = Path('/usr/share/vyos/wwan/sms-command-verbs.json')
    source = Path(__file__).resolve().parents[4] / 'data/wwan/sms-command-verbs.json'
    # Source fallback is for checkouts only; a broken installed catalog fails closed.
    path = installed if installed.exists() else source
    catalog = json.loads(path.read_text())['verbs']
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError('Invalid SMS verb catalog')
    for verb, entry in catalog.items():
        if not re.fullmatch(r'[a-z]+', verb) or not isinstance(entry, dict):
            raise ValueError('Invalid SMS verb entry')
        if (
            not re.fullmatch(r'[a-z]+', entry.get('wrapper_verb', ''))
            or entry.get('tier') not in ('read-only', 'privileged')
            or ('confirm' in entry and not isinstance(entry['confirm'], bool))
        ):
            raise ValueError('Invalid SMS verb policy')
        # Only show is structurally restricted to the informational CLI tree.
        if entry['tier'] == 'read-only' and entry['wrapper_verb'] != 'show':
            raise ValueError('Only show may use the read-only tier')
        if entry['tier'] == 'privileged' and entry.get('confirm') is False:
            raise ValueError('Privileged verbs must require confirmation')
    if catalog.get('show', {}).get('tier') != 'read-only':
        raise ValueError('Missing read-only show verb')
    return catalog


def normalize_number(number, *, require_plus=False):
    """Validate international number syntax; tolerate modem-omitted + only."""
    pattern = CONFIG_NUMBER_PATTERN if require_plus else MODEM_NUMBER_PATTERN
    if not isinstance(number, str) or not pattern.fullmatch(number):
        return None
    return number.lstrip('+')


def number_allowed(number, allowlist):
    """Compare complete international numbers, tolerating a modem-omitted +."""
    normalized = normalize_number(number)
    return normalized is not None and any(
        normalized == normalize_number(candidate) for candidate in allowlist
    )


def mask_password(text):
    """Mask the first whitespace-delimited field without revealing its length."""
    fields = text.split(None, 1)
    if not fields:
        return ''
    return PASSWORD_MASK + (' ' + fields[1] if len(fields) > 1 else '')


def bound_reply(text, maximum):
    """Return one bounded SMS payload and its conservative segment budget."""
    if maximum <= 0:
        return '', 0
    text = text.strip() or 'no output'
    uses_unicode = any(
        char not in GSM_BASIC_CHARACTERS and char not in GSM_EXTENSION_CHARACTERS
        for char in text
    )
    per_segment = UNICODE_SEGMENT_SIZE if uses_unicode else GSM_SEGMENT_SIZE
    budget = maximum * per_segment
    available = budget - len(TRUNCATION_MARKER)
    total_units = prefix_units = prefix_end = 0

    # Count the complete payload and remember the prefix that leaves room for
    # truncation. Keeping only counters avoids allocating one cost per character.
    for index, char in enumerate(text):
        units = (
            len(char.encode('utf-16-be')) // 2
            if uses_unicode
            else (2 if char in GSM_EXTENSION_CHARACTERS else 1)
        )
        total_units += units
        if index == prefix_end and prefix_units + units <= available:
            prefix_units += units
            prefix_end += 1

    if total_units > budget:
        text = text[:prefix_end] + TRUNCATION_MARKER
        total_units = prefix_units + len(TRUNCATION_MARKER)
    return text, (total_units + per_segment - 1) // per_segment


# Configuration defaults and shared CLI/D-Bus validation.


DEFAULTS = {
    'rate_limit': 5,
    'timeout': 20,
    'max_reply_segments': 4,
}

# Accepted legacy input is discarded below; it cannot enable suffix matching.
_ALLOWED_OPTIONS = frozenset(
    {
        *DEFAULTS,
        'password',
        'password_hash',
        'allow_from',
        'privileged',
        'phone_match_digits',
    }
)
_PRIVILEGED_OPTIONS = frozenset({'verb', 'allow_from'})


def normalize_config(config):
    """Copy and validate CLI/runtime shape, numbers, verbs, and numeric limits."""
    if not isinstance(config, dict):
        raise TypeError('SMS command configuration must be a dictionary')
    if config.keys() - _ALLOWED_OPTIONS:
        raise ValueError('Unknown SMS command configuration option')
    cfg = deepcopy(config)
    cfg.pop('phone_match_digits', None)
    privileged = cfg.get('privileged', {})
    if not isinstance(privileged, dict) or privileged.keys() - _PRIVILEGED_OPTIONS:
        raise ValueError('Invalid SMS privileged configuration')
    for key, default in DEFAULTS.items():
        try:
            value = int(cfg.get(key, default))
        except (ValueError, TypeError):
            raise ValueError(f'Invalid SMS {key}') from None
        if value < 1:
            raise ValueError(f'SMS {key} must be a positive integer')
        cfg[key] = value
    for group in (cfg, privileged):
        numbers = group.get('allow_from', [])
        if not isinstance(numbers, list):
            raise TypeError('SMS allow-from must be a list of E.164 numbers')
        if any(normalize_number(n, require_plus=True) is None for n in numbers):
            raise ValueError(
                'SMS allow-from requires E.164 format: +countrycode and number, at most 15 digits'
            )
    if not cfg.get('allow_from'):
        raise ValueError('SMS commands require a nonempty allow-from list')
    verbs = cfg.get('privileged', {}).get('verb', [])
    catalog = load_catalog()
    if not isinstance(verbs, list) or any(
        v not in catalog or catalog[v]['tier'] != 'privileged' for v in verbs
    ):
        raise ValueError('Unknown or nonprivileged SMS verb')
    return cfg


def verify_config(config, policy=None):
    """Validate a candidate password; None disables enhanced login policy checks."""
    from vyos.utils.auth import (
        PASSWORD_POLICY_DEFAULT_MINIMUM_DIGIT,
        PASSWORD_POLICY_DEFAULT_MINIMUM_LENGTH,
        PASSWORD_POLICY_DEFAULT_MINIMUM_SPECIAL,
        PASSWORD_POLICY_DEFAULT_MINIMUM_UPPERCASE,
        EPasswdStrength,
        evaluate_strength,
        password_policy_check,
        validate_password_hash,
    )

    cfg = normalize_config(config)
    password = cfg.get('password')
    if password is not None:
        if not password or any(c.isspace() for c in password):
            raise ValueError('SMS password must be nonempty and contain no whitespace')
        # None means login's enhanced policy is disabled. An empty mapping
        # represents an explicitly enabled policy using default thresholds.
        if policy is not None:
            error = password_policy_check(
                password,
                minlen=int(
                    policy.get('minimum_length', PASSWORD_POLICY_DEFAULT_MINIMUM_LENGTH)
                ),
                dcredit=-int(
                    policy.get('minimum_digit', PASSWORD_POLICY_DEFAULT_MINIMUM_DIGIT)
                ),
                ucredit=-int(
                    policy.get(
                        'minimum_uppercase', PASSWORD_POLICY_DEFAULT_MINIMUM_UPPERCASE
                    )
                ),
                ocredit=-int(
                    policy.get(
                        'minimum_special', PASSWORD_POLICY_DEFAULT_MINIMUM_SPECIAL
                    )
                ),
            )
            if error:
                raise ValueError(error)
            strength = evaluate_strength(password)
            if strength['strength'] in (EPasswdStrength.WEAK, EPasswdStrength.ERROR):
                raise ValueError(
                    'SMS password does not meet password strength requirements'
                )
    elif not cfg.get('password_hash'):
        raise ValueError('SMS commands require a password')
    else:
        validate_password_hash(cfg['password_hash'])
    return cfg


def runtime_config(config):
    """Accept only hashed credentials at the manager/D-Bus boundary."""
    from vyos.utils.auth import validate_password_hash

    cfg = normalize_config(config)
    if 'password' in cfg:
        raise ValueError('Plaintext SMS passwords cannot be sent to the manager')
    validate_password_hash(cfg.get('password_hash'))
    return cfg


# Bounded operational-command execution.


async def run_command(argv, timeout, *, max_reply_segments=4):
    """Capture bounded merged output; kill and reap the process group on every exit."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, 'PAGER': 'cat', 'VYATTA_PAGER': 'cat', 'TERM': 'dumb'},
    )
    # Derive retained output from the configured reply cap, not a second limit.
    capture_limit = (
        max_reply_segments * GSM_SEGMENT_SIZE * 4
    )  # UTF-8: up to 4 bytes/character
    output = bytearray()
    truncated = False

    async def read():
        nonlocal truncated
        while chunk := await proc.stdout.read(4096):
            room = capture_limit - len(output)
            output.extend(chunk[:room])
            truncated |= len(chunk) > room
        await proc.wait()

    timed_out = False
    try:
        await asyncio.wait_for(read(), timeout)
    except TimeoutError:
        timed_out = True
    finally:
        # Descendants may outlive the wrapper, or keep its stdout open.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    result = output.decode('utf-8', errors='replace')
    if truncated:
        result += '\n' + TRUNCATION_MARKER
    if timed_out:
        result = 'command timed out\n' + result
    elif proc.returncode and not result.strip():
        result = f'command failed: exit {proc.returncode}'
    return result


# Authenticated command requests and serialized replies.


class Gateway:
    """Bounded state scoped to one interface and one authorization configuration."""

    def __init__(self, fsm, config):
        self.fsm = fsm
        self.config = deepcopy(config)
        self.rates = {}
        self.pending = {}
        self.tasks = set()
        self.closed = False
        self.executing_tasks = {}
        if not hasattr(fsm, '_sms_command_reply_lock'):
            fsm._sms_command_reply_lock = asyncio.Lock()
        self.reply_lock = fsm._sms_command_reply_lock

    def audit(self, number, outcome, verb='-', tier='-', elapsed_ms=None):
        logger.info(
            'SMS command interface=wwan%s sender=%s outcome=%s verb=%s tier=%s elapsed_ms=%s',
            self.fsm.interface_number,
            number,
            outcome,
            verb,
            tier,
            elapsed_ms if elapsed_ms is not None else '-',
        )

    def close(self):
        self.closed = True
        self.pending.clear()
        for task in tuple(self.tasks):
            # An accepted action may itself disconnect this modem. Allow it to
            # finish under its command timeout, even after transport teardown.
            if not self.executing_tasks.get(task, False):
                task.cancel()

    def current(self):
        return (
            not self.closed
            and (self.fsm.config or {}).get('sms_command') == self.config
            and getattr(self.fsm, '_sms_command_armed', False)
        )

    def submit(self, number, text):
        """Apply sender authorization and the configured per-sender rate limit."""
        cfg = self.config
        sender = normalize_number(number)
        if not self.current():
            return
        if not sender or not number_allowed(number, cfg.get('allow_from', [])):
            self.audit(sender or '<invalid>', 'denied-sender')
            return
        own = getattr(self.fsm, '_sms_command_own_numbers', [])
        if isinstance(own, str):
            own = [own]
        if number_allowed(number, own):
            self.audit(number, 'denied-own-number')
            return
        # The full normalized number keys rate limits and confirmation ownership.
        now = time.monotonic()
        rate = self.rates.setdefault(sender, deque())
        while rate and now - rate[0] >= 60:
            rate.popleft()
        if len(rate) >= cfg['rate_limit']:
            self.audit(number, 'rate-limited')
            return
        # Count admitted attempts, including bad authentication, to bound KDF work.
        rate.append(now)
        task = self.fsm._safe_create_task(
            self.handle(number, sender, text), name='sms-command'
        )
        if task is not None:
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def reply(self, number, text, maximum):
        """Serialize reply submissions without blocking command execution."""
        if maximum <= 0 or not self.current():
            return maximum
        async with self.reply_lock:
            return await self._send_reply(number, text, maximum)

    async def _send_reply(self, number, text, maximum):
        """Return spent budget; a failed send closes this request's reply channel."""
        if maximum <= 0:
            return 0
        payload, parts = bound_reply(text, maximum)
        if not self.current():
            return maximum
        started = time.monotonic()
        logger.info(
            'SMS reply sending interface=wwan%s number=%s budget_parts=%s response=%s',
            self.fsm.interface_number,
            number,
            parts,
            json.dumps(payload, ensure_ascii=False),
        )
        try:
            # One submission: the modem supplies multipart headers so handsets
            # can reassemble the complete reply rather than separate (i/n) texts.
            await self.fsm.sms_send(number, payload)
        except Exception as error:  # noqa: BLE001 - contain D-Bus transport failures
            # Transport errors can include the original SMS: log only the type.
            logger.warning(
                'SMS command reply delivery failed interface=wwan%s number=%s error=%s',
                self.fsm.interface_number,
                number,
                getattr(error, 'type', type(error).__name__),
            )
            return maximum
        logger.info(
            'SMS reply sent interface=wwan%s number=%s budget_parts=%s elapsed_ms=%s',
            self.fsm.interface_number,
            number,
            parts,
            int((time.monotonic() - started) * 1000),
        )
        return parts

    async def handle(self, number, sender, text):
        from vyos.utils.auth import verify_password

        request_started = time.monotonic()
        remaining = self.config['max_reply_segments']

        async def reply(text):
            nonlocal remaining
            remaining -= await self.reply(number, text, remaining)

        # 1. Authenticate this request. No credential/session survives the message.
        # Token is a raw whitespace-free field; do not shlex the password itself.
        fields = text.strip().split(None, 1)
        if len(fields) != 2:
            return
        authenticated = await asyncio.to_thread(
            verify_password, fields[0], self.config['password_hash']
        )
        if not authenticated or not self.current():
            self.audit(number, 'denied-authentication')
            return
        self.audit(
            number,
            'authenticated',
            elapsed_ms=int((time.monotonic() - request_started) * 1000),
        )
        # 2. Validate command syntax before consulting the operational wrapper.
        if any(ord(c) < 32 or ord(c) == 127 for c in fields[1]):
            await reply('command must be a single line')
            return
        try:
            words = shlex.split(fields[1])
        except ValueError:
            await reply('invalid command quoting')
            return
        if not words or any(not SAFE_ARGUMENT.fullmatch(word) for word in words):
            await reply('unsupported command characters')
            return
        try:
            catalog = load_catalog()
        except (OSError, ValueError, KeyError, TypeError):
            logger.error('SMS command catalog unavailable or invalid')
            return
        now = time.monotonic()
        self.pending = {
            s: item for s, item in self.pending.items() if item.expires_at > now
        }
        # 3. Resolve a confirmation to the exact command it authorizes.
        confirmed = False
        if words[0] == 'CONFIRM':
            key = (sender, words[1]) if len(words) == 2 else None
            item = self.pending.get(key)
            if (
                len(words) != 2
                or not item
                or not secrets.compare_digest(item.nonce, words[1])
            ):
                self.audit(number, 'denied-confirmation')
                await reply('invalid or expired confirmation')
                return
            # Consume before execution, never restore on failure.
            _, _, words = self.pending.pop(key)
            confirmed = True
        # 4. Apply catalog/verb policy and the optional tighter action allowlist.
        verb = words[0]
        entry = catalog.get(verb)
        if not entry or (
            verb != 'show'
            and verb not in self.config.get('privileged', {}).get('verb', [])
        ):
            self.audit(number, 'denied-verb')
            await reply('unknown or disabled command verb')
            return
        privileged = entry['tier'] == 'privileged'
        tighter = self.config.get('privileged', {}).get('allow_from', [])
        if privileged and tighter and not number_allowed(number, tighter):
            return
        if (privileged or entry.get('confirm', False)) and not confirmed:
            self.audit(number, 'confirmation-required', verb, entry['tier'])
            nonce = secrets.token_hex(8)
            self.pending[(sender, nonce)] = PendingAction(
                now + CONFIRM_TTL, nonce, words
            )
            await reply(
                f'CONFIRM {nonce} to run {shlex.join(words)}. Expires in 120s. Reply: <password> CONFIRM {nonce}',
            )
            return
        # 5. Execute independently; only response submissions share a lock.
        task = asyncio.current_task()
        try:
            if confirmed:
                await reply(f'Accepted: {verb}')
            if not self.current():
                return
            self.audit(number, 'accepted', verb, entry['tier'])
            self.executing_tasks[task] = privileged
            execution_started = time.monotonic()
            try:
                output = await run_command(
                    [WRAPPER, entry['wrapper_verb'], *words[1:]],
                    self.config['timeout'],
                    max_reply_segments=self.config['max_reply_segments'],
                )
            finally:
                self.executing_tasks.pop(task, None)
            self.audit(
                number,
                'executed',
                verb,
                entry['tier'],
                elapsed_ms=int((time.monotonic() - execution_started) * 1000),
            )
            await reply(output)
            self.audit(
                number,
                'finished',
                verb,
                entry['tier'],
                elapsed_ms=int((time.monotonic() - request_started) * 1000),
            )
        except asyncio.CancelledError:
            self.audit(number, 'cancelled', verb, entry['tier'])
            raise
        except Exception:  # noqa: BLE001 - contain operational command failures
            logger.error(
                'SMS command execution failed interface=wwan%s verb=%s',
                self.fsm.interface_number,
                verb,
            )
            await reply('command execution failed')
        finally:
            self.executing_tasks.pop(task, None)


def submit(fsm, number, text):
    cfg = (fsm.config or {}).get('sms_command')
    if not cfg or not cfg.get('allow_from'):
        return
    gateway = getattr(fsm, '_sms_command_gateway', None)
    if gateway is None or gateway.config != cfg:
        if gateway:
            gateway.close()
        gateway = fsm._sms_command_gateway = Gateway(fsm, cfg)
    gateway.submit(number, text)


@cache
def _modem_constants():
    """Reuse protocol names without loading the FSM for CLI validation/completion."""
    from vyos.utils.wwan import interfaces_wwan_state_machine

    return interfaces_wwan_state_machine


@dataclass
class _Receive:
    runtime: object
    path: str
    generation: str
    eligible: bool
    drained: bool
    modem_path: str
    storage_path: str
    task: object = None
    failed: bool = False
    partial: object = None
    record: object = None
    initial_state: int = 0
    network_timestamp: str = ''
    next_id: int = 1

    def current(self):
        host = self.runtime.host
        return (
            self.generation == getattr(host, '_sms_command_generation', None)
            and self.modem_path == host.modem_path
            and self.storage_path == host._sms_storage_path()
        )


_scope = ContextVar('sms_command_receive', default=None)


def _active_receive():
    scope = _scope.get()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return scope if scope is not None and scope.task is task else None


class _CommandLogFilter(logging.Filter):
    """Observe legacy receive failures without exposing credential payloads."""

    def filter(self, record):
        scope = _active_receive()
        message = record.getMessage()
        if scope is not None and message.startswith('Failed to process incoming SMS:'):
            scope.failed = True
            record.msg = 'SMS command receive failed; transport details omitted'
            record.args = ()
        # The unchanged host logs full configuration values during initialization.
        if message.startswith('Config structure:') and 'sms_command' in message:
            record.msg = (
                'Configuration keys logged separately; SMS credential values omitted'
            )
            record.args = ()
        return True


def _install_log_filter():
    if not any(isinstance(item, _CommandLogFilter) for item in logger.filters):
        logger.addFilter(_CommandLogFilter())


class SmsCommandRuntime:
    """Only command state and hooks; file I/O, sending and receiving stay in the host."""

    def __init__(self, host):
        self.host = host
        self.receive_lock = asyncio.Lock()
        self.setup_lock = asyncio.Lock()
        self.setup_task = None
        self.receiving = {}
        self.handles = None
        self.draining = False
        self.setup_failed = False
        if not hasattr(host, '_sms_command_generation'):
            host._sms_command_generation = uuid.uuid4().hex
            host._sms_command_armed = False
        _install_log_filter()

    def remember_handles(self):
        host = self.host
        self.handles = (
            getattr(host, '_sms_messaging_iface', None),
            getattr(host, '_sms_added_handler', None),
            getattr(host, '_sms_listener_bound_path', None),
        )

    def restore_handles(self):
        if self.handles and self.handles[0] is not None:
            (
                self.host._sms_messaging_iface,
                self.host._sms_added_handler,
                self.host._sms_listener_bound_path,
            ) = self.handles

    def cleanup(self):
        host = self.host
        self.restore_handles()
        self.handles = None
        host._sms_command_armed = False
        host._sms_command_generation = uuid.uuid4().hex
        for props, handler in self.receiving.values():
            try:
                props.off_properties_changed(handler)
            except Exception:  # noqa: BLE001 - keep host cleanup independent
                logger.debug('SMS multipart completion unsubscribe failed')
        self.receiving.clear()
        gateway = getattr(host, '_sms_command_gateway', None)
        if gateway is not None:
            gateway.close()
            del host._sms_command_gateway

    def ticket(self, path, *, drained=None, command_eligible=None, generation=None):
        host = self.host
        drained = self.draining if drained is None else drained
        return _Receive(
            self,
            path,
            generation
            if generation is not None
            else getattr(host, '_sms_command_generation', None),
            bool(getattr(host, '_sms_command_armed', False))
            if command_eligible is None
            else command_eligible,
            drained,
            host.modem_path,
            host._sms_storage_path(),
        )

    async def properties(self, path):
        host = self.host
        intro = await host.bus.introspect(
            _modem_constants().MODEM_MANAGER_SERVICE, path
        )
        proxy = host.bus.get_proxy_object(
            _modem_constants().MODEM_MANAGER_SERVICE, path, intro
        )
        return proxy.get_interface('org.freedesktop.DBus.Properties')

    async def receive(self, ticket, original):
        ticket.task = asyncio.current_task()
        for attempt in range(3):
            async with self.receive_lock:
                if not ticket.current():
                    return False
                ticket.failed = False
                token = _scope.set(ticket)
                try:
                    # Read state first so a partial text/complete-state race never executes.
                    props = await self.properties(ticket.path)
                    ticket.initial_state = (
                        await props.call_get(_modem_constants().SMS_INTERFACE, 'State')
                    ).value
                    ticket.network_timestamp = (
                        await props.call_get(
                            _modem_constants().SMS_INTERFACE, 'Timestamp'
                        )
                    ).value
                    await original(ticket.path)
                    if ticket.record and ticket.record.get('status') == 'receiving':
                        await self.watch_completion(ticket, props)
                except Exception as error:  # noqa: BLE001 - do not expose SMS payloads
                    ticket.failed = True
                    logger.warning(
                        'SMS command receive failed interface=wwan%s error=%s',
                        self.host.interface_number,
                        type(error).__name__,
                    )
                finally:
                    _scope.reset(token)
            if not ticket.failed:
                return True
            if attempt < 2:
                logger.warning(
                    'SMS receive retry interface=wwan%s path=%s attempt=%s/3',
                    self.host.interface_number,
                    ticket.path,
                    attempt + 2,
                )
                await asyncio.sleep(0.5 * (attempt + 1))
        logger.error('Incoming SMS retries exhausted; modem copy retained')
        return False

    async def watch_completion(self, ticket, props):
        if ticket.path in self.receiving:
            return

        def changed(interface, values, invalidated):
            if self.receiving.get(ticket.path) != (props, changed):
                return
            if (
                interface == _modem_constants().SMS_INTERFACE
                and 'State' in values
                and values['State'].value == 3
            ):
                props.off_properties_changed(changed)
                self.receiving.pop(ticket.path, None)
                self.host._safe_create_task(
                    self.host._handle_incoming_sms(
                        ticket.path,
                        drained=ticket.drained,
                        command_eligible=ticket.eligible,
                        generation=ticket.generation,
                    ),
                    name='sms-completed',
                )

        props.on_properties_changed(changed)
        self.receiving[ticket.path] = (props, changed)
        latest = await props.call_get(_modem_constants().SMS_INTERFACE, 'State')
        changed(_modem_constants().SMS_INTERFACE, {'State': latest}, [])

    def redact(self, records):
        for record in records:
            if record.get('credential_redacted') and record.get('text', '').startswith(
                '[redacted]'
            ):
                record['text'] = PASSWORD_MASK + record['text'][len('[redacted]') :]
            if record.get('direction') == 'incoming' and not record.get(
                'credential_redacted'
            ):
                record['text'] = mask_password(record.get('text', ''))
                record['credential_redacted'] = True
        return records

    def history(self, records, save):
        scope = _active_receive()
        if scope is not None and scope.runtime is self and not scope.current():
            raise ValueError('Stale SMS command attachment')
        before = json.dumps(records)
        self.redact(records)
        if json.dumps(records) != before:
            try:
                save(records)
            except OSError as error:
                logger.warning(
                    'SMS history redaction writeback failed interface=wwan%s error=%s; returning loaded history',
                    self.host.interface_number,
                    type(error).__name__,
                )
        if scope is None or scope.runtime is not self:
            return records
        scope.next_id = max((record.get('id', 0) for record in records), default=0) + 1
        view = deepcopy(records)
        result = []
        for record in view:
            if record.get('source_path') == scope.path:
                if record.get('source_generation') != scope.generation:
                    # Hide only from the legacy path-only duplicate check; restore on save.
                    record['_sms_original_source_path'] = record.pop('source_path')
                elif record.get('status') == 'receiving':
                    scope.partial = record
                    continue
            result.append(record)
        return result

    def prepare_record(self, scope, record):
        if not scope.current():
            raise ValueError('Stale SMS command attachment')
        if scope.initial_state == 2 and record.get('status') == 'received':
            raise ValueError('SMS assembly completed during read; reread complete text')
        record['id'] = scope.partial['id'] if scope.partial else scope.next_id
        record['source_generation'] = scope.generation
        if scope.drained or not scope.eligible:
            record['drained'] = True
        eligible = (
            scope.eligible
            and not record.get('drained')
            and record.get('status') == 'received'
            and getattr(self.host, '_sms_command_armed', False)
        )
        if eligible:
            try:
                timestamp = datetime.datetime.fromisoformat(scope.network_timestamp)
                eligible = (
                    timestamp.tzinfo is not None
                    and timestamp >= self.host._sms_command_armed_at
                )
            except (ValueError, TypeError, AttributeError):
                eligible = False
        if eligible:
            record['command_consumed'] = True
        return eligible

    def saved(self, scope, record, text, eligible):
        scope.record = record
        logger.info(
            'Incoming SMS interface=wwan%s number=%s path=%s id=%s state=%s drained=%s command_eligible=%s message=%s',
            self.host.interface_number,
            record.get('number'),
            scope.path,
            record['id'],
            3 if record.get('status') == 'received' else 2,
            bool(record.get('drained')),
            bool(eligible),
            json.dumps(mask_password(text), ensure_ascii=False),
        )
        if eligible:
            submit(self.host, record['number'], text)

    async def setup(self, original):
        async with self.setup_lock:
            host = self.host
            self.restore_handles()
            if (
                getattr(host, '_sms_command_armed', False)
                and host._sms_listener_bound_path == host.modem_path
            ):
                return
            for attempt in range(3):
                host._sms_command_armed = False
                host._sms_command_generation = uuid.uuid4().hex
                self.draining = True
                self.setup_failed = False
                try:
                    await original()
                    if (
                        host._sms_listener_bound_path != host.modem_path
                        or not host.modem_path
                    ):
                        raise RuntimeError('SMS listener did not attach')
                    messaging = host._sms_messaging_iface
                    callback = host._sms_added_handler
                    original_callback = getattr(
                        callback, '_sms_original_callback', callback
                    )
                    generation = host._sms_command_generation

                    def added(
                        path,
                        received,
                        callback=original_callback,
                        generation=generation,
                    ):
                        if generation == getattr(host, '_sms_command_generation', None):
                            callback(path, received)

                    added._sms_original_callback = original_callback
                    messaging.off_added(callback)
                    messaging.on_added(added)
                    host._sms_added_handler = added
                    self.remember_handles()
                    # Close the callback-wrapping gap using the original drain method.
                    drained = await host._drain_existing_sms()
                    if drained is False:
                        self.setup_failed = True
                    if self.setup_failed:
                        raise RuntimeError('SMS backlog drain failed')
                    props = await self.properties(host.modem_path)
                    remaining = (
                        await props.call_get(
                            _modem_constants().MESSAGING_INTERFACE, 'Messages'
                        )
                    ).value
                    history = host._sms_load()
                    for path in remaining:
                        message_props = await self.properties(path)
                        state = (
                            await message_props.call_get(
                                _modem_constants().SMS_INTERFACE, 'State'
                            )
                        ).value
                        if state in (2, 3) and not any(
                            record.get('source_path') == path
                            and record.get('source_generation')
                            == host._sms_command_generation
                            for record in history
                        ):
                            raise RuntimeError('Incoming backlog was not persisted')
                    try:
                        host._sms_command_own_numbers = (
                            await props.call_get(
                                _modem_constants().MODEM_INTERFACE, 'OwnNumbers'
                            )
                        ).value
                    except Exception:  # noqa: BLE001 - optional modem metadata
                        host._sms_command_own_numbers = []
                    host._sms_command_armed_at = datetime.datetime.now(
                        datetime.UTC
                    ).replace(microsecond=0)
                    host._sms_command_armed = True
                    logger.info('SMS command gateway armed')
                    return
                except Exception as error:  # noqa: BLE001 - bound setup retries
                    logger.warning(
                        'SMS command listener setup failed interface=wwan%s error=%s',
                        host.interface_number,
                        type(error).__name__,
                    )
                finally:
                    self.draining = False
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
            logger.error('SMS listener setup retries exhausted')

    def state_changed(self, state):
        if state not in ('CONNECTED', 'REGISTERED_IDLE') or getattr(
            self.host, '_admin_disabled', False
        ):
            if self.setup_task is not None and not self.setup_task.done():
                self.setup_task.cancel()
            self.setup_task = None
            self.host._cleanup_sms_listener()
        elif self.setup_task is None or self.setup_task.done():
            coro = self.host._setup_sms_listener()
            try:
                self.setup_task = self.host._safe_create_task(
                    coro, name='sms-command-listener'
                )
            except RuntimeError:
                coro.close()


class _SmsCommandHooks:
    """Thin overrides calling the original FSM methods, never replacing their bodies."""

    def apply_config(self, config):
        if config.get('sms_command') or getattr(self, '_sms_command_runtime', None):
            configuration_changed(self, config)
        return super().apply_config(config)

    def transition(self, event):
        if not (getattr(self, 'config', None) or {}).get('sms_command'):
            return super().transition(event)
        before = getattr(self.machine, 'current_state', '')
        result = super().transition(event)
        if self.machine.current_state != before and (self.config or {}).get(
            'sms_command'
        ):
            runtime_for(self).state_changed(self.machine.current_state)
        return result

    def _apply_parsed_configuration(self):
        result = super()._apply_parsed_configuration()
        runtime = runtime_for(self, create=False)
        if runtime is not None:
            runtime.restore_handles()
        return result

    def _sms_load(self):
        records = super()._sms_load()
        if not (getattr(self, 'config', None) or {}).get('sms_command'):
            return records
        return runtime_for(self).history(records, self._sms_save)

    def _sms_save(self, messages):
        if (getattr(self, 'config', None) or {}).get('sms_command'):
            runtime_for(self).redact(messages)
            for message in messages:
                if '_sms_original_source_path' in message:
                    message['source_path'] = message.pop('_sms_original_source_path')
        return super()._sms_save(messages)

    def _sms_append(self, msg):
        if not (getattr(self, 'config', None) or {}).get('sms_command'):
            return super()._sms_append(msg)
        record = msg
        scope = _active_receive()
        if (
            scope is None
            or scope.runtime.host is not self
            or record.get('direction') != 'incoming'
        ):
            return super()._sms_append(record)
        text = record.get('text', '')
        eligible = scope.runtime.prepare_record(scope, record)
        result = super()._sms_append(record)
        scope.runtime.saved(scope, record, text, eligible)
        return result

    def _handle_incoming_sms(
        self, sms_path, *, drained=None, command_eligible=None, generation=None
    ):
        original = super()._handle_incoming_sms
        if not (getattr(self, 'config', None) or {}).get('sms_command'):
            return original(sms_path)
        runtime = runtime_for(self)
        # This method intentionally returns a coroutine synchronously: the legacy
        # Added callback captures eligibility now, before ensure_future schedules it.
        ticket = runtime.ticket(
            sms_path,
            drained=drained,
            command_eligible=command_eligible,
            generation=generation,
        )
        return runtime.receive(ticket, original)

    async def _setup_sms_listener(self):
        if not (getattr(self, 'config', None) or {}).get('sms_command'):
            return await super()._setup_sms_listener()
        return await runtime_for(self).setup(super()._setup_sms_listener)

    async def _drain_existing_sms(self):
        runtime = runtime_for(self, create=False)
        if runtime is None:
            return await super()._drain_existing_sms()
        previous = runtime.draining
        runtime.draining = True
        try:
            await super()._drain_existing_sms()
        except Exception:  # noqa: BLE001 - fail closed on backlog failure
            runtime.setup_failed = True
        finally:
            runtime.draining = previous

    def _cleanup_sms_listener(self):
        runtime = runtime_for(
            self, create=bool((getattr(self, 'config', None) or {}).get('sms_command'))
        )
        if runtime is not None:
            runtime.cleanup()
        return super()._cleanup_sms_listener()


@cache
def with_sms_commands(base):
    """Compose command hooks at construction; the source FSM class is untouched."""
    return type('SmsCommandStateMachine', (_SmsCommandHooks, base), {})


def runtime_for(fsm, *, create=True):
    runtime = getattr(fsm, '_sms_command_runtime', None)
    if (
        runtime is None
        and create
        and (getattr(fsm, 'config', None) or {}).get('sms_command')
    ):
        runtime = SmsCommandRuntime(fsm)
        runtime.remember_handles()
        fsm._sms_command_runtime = runtime
    return runtime


def configuration_changed(fsm, config):
    if (getattr(fsm, 'config', None) or {}).get('sms_command') == config.get(
        'sms_command'
    ):
        return
    runtime = runtime_for(fsm, create=False)
    if runtime is not None:
        if runtime.setup_task is not None and not runtime.setup_task.done():
            runtime.setup_task.cancel()
        fsm._cleanup_sms_listener()
        del fsm._sms_command_runtime
    else:
        gateway = getattr(fsm, '_sms_command_gateway', None)
        if gateway is not None:
            gateway.close()
            del fsm._sms_command_gateway
        fsm._sms_command_armed = False
        fsm._sms_command_generation = uuid.uuid4().hex
    if config.get('sms_command'):
        runtime = SmsCommandRuntime(fsm)
        runtime.remember_handles()
        fsm._sms_command_runtime = runtime
