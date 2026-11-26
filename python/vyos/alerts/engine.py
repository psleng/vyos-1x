"""Journal matching, edge detection and per-rule hold-down."""

import time
from collections import defaultdict

from vyos.alerts.catalog import compile_fields, expand, match_fields, rules


class Engine:
    def __init__(
        self,
        config,
        history,
        delivery,
        catalog=None,
        committing=lambda: False,
        clock=time.monotonic,
    ):
        self.history, self.delivery = history, delivery
        self.committing, self.clock = committing, clock
        self.hold_down = int(config.get('hold_down', 60))
        self.explicit_hold_down = 'hold_down' in config
        self.rules = rules(config, catalog)
        self.fields = [compile_fields(rule['match']) for rule in self.rules]
        self.edges, self.recent = {}, {}
        self.observers = []
        # Observe both states even if only one direction is configured.
        if catalog:
            for rule in self.rules:
                if not rule.get('edge'):
                    continue
                for type_id, definition in catalog['types'].items():
                    if definition.get('edge', {}).get('group') == rule['edge']['group']:
                        observer = expand(
                            rule['name'], dict(type=type_id, **rule['values']), catalog
                        )
                        self.observers.append(
                            (observer, compile_fields(observer['match']))
                        )

    @staticmethod
    def values(rule, entry, captures):
        values = defaultdict(str, {k: str(v) for k, v in entry.items()})
        values.update(rule.get('values', {}))
        values.update(captures)
        values['user'] = captures.get('user', captures.get('user2', ''))
        values['host'] = captures.get('host', captures.get('host2', ''))
        return values

    def process_message(self, entry):
        if not entry.get('MESSAGE') or entry.get('SYSLOG_IDENTIFIER') == 'igos-alert':
            return
        now = self.clock()
        # Bound hold-down memory to active windows.
        self.recent = {
            k: v for k, v in self.recent.items() if now - v[0] < self.hold_down
        }
        updates = {}
        for observer, fields in self.observers:
            captures = match_fields(fields, entry)
            if captures is not None:
                values = self.values(observer, entry, captures)
                edge = observer['edge']
                updates[(edge['group'], edge['key'].format_map(values))] = observer[
                    'state'
                ].format_map(values)
        for rule, fields in zip(self.rules, self.fields):
            captures = match_fields(fields, entry)
            if captures is None or (
                rule.get('suppress_during_commit') and self.committing()
            ):
                continue
            values = self.values(rule, entry, captures)
            if rule.get('edge'):
                edge = rule['edge']
                key = (edge['group'], edge['key'].format_map(values))
                state = rule.get('state', '').format_map(values)
                if self.edges.get(key) == state:
                    continue
                updates[key] = state
            object_name = rule.get('object', '').format_map(values)
            key = (
                'event' if rule['type'] == 'custom' else 'alert',
                rule['name'],
                object_name,
            )
            legacy = (
                rule['type'] == 'custom'
                and rule.get('script')
                and not rule.get('notify')
            )
            if key in self.recent and not (legacy and not self.explicit_hold_down):
                self.history.suppress(self.recent[key][1])
                self.delivery.suppress(self.recent[key][1])
                continue
            record = self.history.record(rule, entry, values)
            self.history.append(record)
            self.delivery.enqueue(record, rule)
            self.recent[key] = (now, record['id'])
        self.edges.update(updates)
