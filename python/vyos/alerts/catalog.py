"""Load the versioned catalog and compile rules without interpreting parameters as regex."""

import copy
import json
import re
from pathlib import Path

CATALOG = Path('/usr/share/vyos/alerts/catalog.json')
PARAMETERS = {'interface', 'service', 'group'}


def load_catalog(path=CATALOG):
    data = json.loads(Path(path).read_text())
    if data.get('version') != 1 or not isinstance(data.get('types'), dict):
        raise ValueError('Unsupported alert catalog')
    return data


def validate_parameter(kind, value):
    patterns = {
        'interface-name': r'[a-zA-Z][a-zA-Z0-9_.:-]{0,14}',
        'wwan-interface': r'wwan[0-9]+',
        'unit-name': r'[a-zA-Z0-9_@.:\\-]+\.service',
        'group-name': r'[a-zA-Z0-9_.:-]+',
    }
    if not isinstance(value, str) or not re.fullmatch(patterns[kind], value):
        raise ValueError(f'Invalid {kind}: {value}')


def expand(name, config, catalog):
    type_id = config.get('type')
    if type_id not in catalog['types']:
        raise ValueError(f'Unknown alert type: {type_id}')
    definition = copy.deepcopy(catalog['types'][type_id])
    params = definition.get('parameters', {})
    values = {}
    for key in PARAMETERS:
        if key in config and key not in params:
            raise ValueError(f'{type_id} does not accept {key}')
    for key, spec in params.items():
        value = config.get(key)
        if not value:
            if spec.get('required'):
                raise ValueError(f'{type_id} requires {key}')
            continue
        validate_parameter(spec['validator'], value)
        values[key] = value
    fields = definition['match']
    # Optional selectors are omitted entirely when not specified.
    for key, value in list(fields.items()):
        optional = [p for p in params if '{' + p + '}' in value and p not in values]
        if optional:
            if key == 'MESSAGE':
                for p in optional:
                    value = value.replace('{' + p + '}', r'\S+')
            else:
                del fields[key]
                continue
        regex = key == 'MESSAGE' or key.endswith('_RE')
        for p, v in values.items():
            if p == 'interface' and key == 'WWAN_ALERT_INTERFACE':
                v = v.removeprefix('wwan')
            value = value.replace('{' + p + '}', re.escape(v) if regex else v)
        fields[key] = value
    definition.update(
        name=name,
        type=type_id,
        values=values,
        severity=config.get('severity', definition['severity']),
        severity_override='severity' in config,
        notify=config.get('notify', {}),
    )
    compile_fields(fields)  # validate before committing
    return definition


def compile_fields(fields):
    return {
        key: re.compile(value) if key == 'MESSAGE' or key.endswith('_RE') else value
        for key, value in fields.items()
    }


def match_fields(fields, entry):
    captures = {}
    for key, expected in fields.items():
        actual = str(entry.get(key.removesuffix('_RE'), ''))
        if isinstance(expected, re.Pattern):
            match = expected.fullmatch(actual)
            if not match:
                return None
            captures.update(
                {k: v for k, v in match.groupdict().items() if v is not None}
            )
        elif actual != expected:
            return None
    return captures


def rules(config, catalog=None):
    result = []
    for name, event in config.get('event', {}).items():
        if 'disable' in event:
            continue
        fields = {'MESSAGE': event['filter']['pattern']}
        if event['filter'].get('syslog_identifier'):
            fields['SYSLOG_IDENTIFIER'] = event['filter']['syslog_identifier']
        result.append(
            {
                'name': name,
                'type': 'custom',
                'category': 'custom',
                'severity': event.get('severity', 'warning'),
                'match': fields,
                'notify': event.get('notify', {}),
                'script': event.get('script'),
                'object': name,
                'message': '{MESSAGE}',
                'values': {},
            }
        )
    for name, alert in config.get('alert', {}).items():
        if 'disable' not in alert:
            result.append(expand(name, alert, catalog or load_catalog()))
    return result
