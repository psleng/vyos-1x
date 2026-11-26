#!/usr/bin/env python3
"""Standardized alert history/catalog/status API for CLI, GUI and cloud."""

# ruff: noqa: UP045 - opmode/GraphQL inspect typing.Optional at runtime
import json
import sys
import typing

import vyos.opmode
from vyos.alerts.catalog import load_catalog
from vyos.alerts.record import ROOT, history_lock, read_history, write_history


def show(
    raw: bool,
    types: typing.Optional[bool] = None,
    status: typing.Optional[bool] = None,
    count: typing.Optional[int] = None,
    severity: typing.Optional[str] = None,
    after: typing.Optional[int] = None,
    boot_id: typing.Optional[str] = None,
    completion: typing.Optional[bool] = None,
):
    if types or completion:
        catalog = load_catalog()
        if completion:
            return ' '.join(catalog['types'])
        if raw:
            return {
                'version': catalog['version'],
                'types': [
                    dict(id=key, **value) for key, value in catalog['types'].items()
                ],
            }
        return '\n'.join(
            f'{key}: {value["description"]}' for key, value in catalog['types'].items()
        )
    if status:
        try:
            result = json.loads((ROOT / 'status.json').read_text())
        except FileNotFoundError:
            result = {}
        return result if raw else json.dumps(result, indent=2)
    if count is not None and not 1 <= count <= 100000:
        raise ValueError('count must be between 1 and 100000')
    if severity and severity not in ('critical', 'warning', 'info'):
        raise ValueError('Unknown severity')
    result = read_history()
    if after is not None:
        if not boot_id:
            raise ValueError('after requires boot-id; sequence numbers are per boot')
        # History retains boot order; only include boots following the cursor.
        indices = [i for i, r in enumerate(result) if r['boot_id'] == boot_id]
        if indices:
            first, last = min(indices), max(indices)
            result = [
                r for r in result[first : last + 1] if r['sequence'] > after
            ] + result[last + 1 :]
    if severity:
        result = [r for r in result if r['severity'] == severity]
    result = list(reversed(result))[: count or 1000]
    return (
        result
        if raw
        else '\n'.join(
            f'{r["timestamp"]} {r["severity"].upper():8} {r["rule"]}: {r["message"]} (suppressed={r["suppressed"]})'
            for r in result
        )
    )


def clear():
    with history_lock(ROOT):
        write_history(ROOT, [])
    return 'Alert history cleared'


if __name__ == '__main__':
    try:
        result = vyos.opmode.run(sys.modules[__name__])
        if result:
            print(result)
    except (ValueError, vyos.opmode.Error) as exc:
        print(exc)
        sys.exit(1)
