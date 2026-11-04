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
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.

"""FIPS 140-3 helper: approved-algorithm policy + enforcement primitives.

This is the shared piece behind the enforcement design (igos-fips-140-3-design.md
§5.7). It exposes:

* ``fips_enabled(conf)``   -- is ``system option fips`` set?
* ``approved(leaf)``       -- the approved token subset for a CLI leaf path.
* ``reject_unapproved(config, key_to_leaf)`` -- messages for explicitly-set
  non-approved values, for a conf-mode ``verify()`` fail-closed gate.
* ``apply_fips_defaults(config, key_to_leaf)`` -- swap a non-approved
  VyOS-``defaultValue`` for the approved one after ``get_config_dict()``.
* ``remove_nodes()``       -- whole features removed under FIPS.

The approved *values* live in data (``approved-algorithms.toml``), not code.
"""

import os
import tomllib
from functools import lru_cache

from vyos.defaults import directories

# Installed location (debian/rules: cp -r data/* /usr/share/vyos/). An in-tree
# copy is used as a fallback so the policy can be loaded during development and
# unit tests run from the source checkout.
policy_file = os.path.join(directories['data'], 'fips/approved-algorithms.toml')
_in_tree_policy_file = os.path.join(
    os.path.dirname(__file__), '..', '..', 'data', 'fips', 'approved-algorithms.toml'
)

fips_config_path = ['system', 'option', 'fips']


@lru_cache(maxsize=None)
def _load_policy(path: str) -> dict:
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_policy(path: str = None) -> dict:
    """Load and cache the approved-algorithm policy."""
    if path is None:
        path = policy_file if os.path.exists(policy_file) else _in_tree_policy_file
    return _load_policy(path)


def fips_enabled(conf=None) -> bool:
    """Return True if FIPS mode is enabled in the configuration.

    ``conf`` may be a vyos.config.Config / ConfigTreeQuery instance (preferred in
    conf-mode, where it reuses the single Config instance). When omitted, a
    ConfigTreeQuery is created.
    """
    if conf is not None:
        return conf.exists(fips_config_path)
    from vyos.configquery import ConfigTreeQuery

    return ConfigTreeQuery().exists(fips_config_path)


def approved(leaf: str, policy: dict = None):
    """Approved token list for a CLI leaf path, or None if unconstrained."""
    policy = policy if policy is not None else load_policy()
    entry = policy.get('leaf', {}).get(leaf)
    if entry is None:
        return None
    return entry.get('approved')


def remove_nodes(policy: dict = None) -> list:
    """Whole features (CLI paths) removed under FIPS."""
    policy = policy if policy is not None else load_policy()
    return list(policy.get('remove_node', []))


def _path_present(conf, path: list) -> bool:
    """True if a config path exists; a '*' element iterates a tagNode level."""
    if '*' not in path:
        return conf.exists(path)
    idx = path.index('*')
    prefix = path[:idx]
    if not conf.exists(prefix):
        return False
    for tag in conf.list_nodes(prefix):
        if _path_present(conf, prefix + [tag] + path[idx + 1:]):
            return True
    return False


def features_present(conf, policy: dict = None) -> list:
    """Labels of `reject_under_fips` features currently configured.

    Used by the interactive fips-enable gate (system_option): FIPS refuses to
    turn on while any non-approvable feature is present.
    """
    policy = policy if policy is not None else load_policy()
    present = []
    for entry in policy.get('reject_under_fips', []):
        if _path_present(conf, entry['path']):
            present.append(entry['label'])
    return present


def reject_feature(label: str, remedy: str = None):
    """Raise a fail-closed ConfigError for a feature with no approvable form."""
    from vyos import ConfigError

    msg = (
        f"FIPS 140-3: '{label}' has no FIPS-approved configuration and cannot be "
        f"used while 'system option fips' is set."
    )
    msg += ' ' + (remedy if remedy else 'Remove it, or disable FIPS.')
    raise ConfigError(msg)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def reject_unapproved(config: dict, key_to_leaf: dict, policy: dict = None) -> list:
    """Return human-readable messages for explicitly-set non-approved values.

    ``config`` is the conf-mode config dict; ``key_to_leaf`` maps a dict key in
    ``config`` to the CLI leaf path whose approved set applies. Only keys that
    are present (i.e. explicitly set by the operator) are checked -- defaults
    are handled by generation-time overrides, not validation (§5.7).
    """
    policy = policy if policy is not None else load_policy()
    messages = []
    for key, leaf in key_to_leaf.items():
        if key not in config:
            continue
        allowed = approved(leaf, policy)
        if allowed is None:
            continue
        bad = [v for v in _as_list(config[key]) if v not in allowed]
        if bad:
            messages.append(
                f"FIPS mode: '{', '.join(bad)}' not approved for "
                f"'{leaf}'. Approved values: {', '.join(allowed)}"
            )
    return messages


def apply_fips_defaults(config: dict, key_to_leaf: dict, policy: dict = None) -> None:
    """Emit the policy's approved default for a leaf the operator left unset.

    For leaves whose *unset* value falls through to a non-approved downstream
    default (e.g. OpenSSH / OpenVPN compiled-in defaults), emit the approved
    ``default`` declared in the policy when the key is absent from ``config``.
    Explicitly-set values are left untouched, so ``reject_unapproved()`` in
    ``verify()`` still fails closed on a non-approved value the operator chose.
    This keeps the approved *values* in data, not code (§5.7).
    """
    policy = policy if policy is not None else load_policy()
    for key, leaf in key_to_leaf.items():
        if key in config:
            continue
        entry = policy.get('leaf', {}).get(leaf)
        if entry is None or 'default' not in entry:
            continue
        config[key] = list(entry['default'])


def default(leaf: str, policy: dict = None):
    """The approved default token list for a CLI leaf, or None if undeclared."""
    policy = policy if policy is not None else load_policy()
    entry = policy.get('leaf', {}).get(leaf)
    if entry is None:
        return None
    value = entry.get('default')
    return list(value) if value is not None else None
