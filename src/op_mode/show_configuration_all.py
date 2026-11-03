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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Render the running configuration including default values.
#
# The legacy 'cli-shell-api showConfig --show-show-defaults' path no longer
# emits defaults: since the migration to XML <defaultValue>/xml_ref the CStore
# node.def templates no longer carry a machine-readable 'default:' directive, so
# the authoritative defaults are only available via get_config_defaults(). This
# renders the configuration with those defaults overlaid, while reusing the
# native '--show-hide-secrets' output as a base so that secret values stay
# masked and the output format is unchanged.
#
# Defaults are overlaid only for items that are actually configured, i.e. into
# subtrees that already exist in the active configuration - not for every node
# in the reference tree. This matches the legacy behaviour: 'show configuration
# all' shows the defaults of the configured items, not every default.

from vyos.config import Config
from vyos.configtree import ConfigTree
from vyos.utils.process import cmd


def _merge_configured_defaults(config_tree: ConfigTree, active_tree: ConfigTree,
                               defaults: dict, path: list) -> None:
    """Overlay default values onto 'config_tree' in place, but only for items
    that are actually configured.

    Unlike vyos.config.merge_defaults, which overlays every default in the
    schema, this descends only into subtrees that already exist in the active
    configuration ('active_tree'). Defaults are therefore shown for the
    configured items only - not for every node in the entire reference tree.
    This restores the behaviour of the legacy 'show configuration all'.

    'active_tree' is an immutable snapshot of the explicitly configured values;
    existence is tested against it (not the tree being mutated) so that overlaid
    defaults never cause further defaults to be pulled in.
    """
    for key, value in defaults.items():
        current = path + [key]
        if isinstance(value, dict):
            if not value:
                # A valueless default node: only meaningful below a configured
                # parent, which is guaranteed here since we descend solely into
                # subtrees present in the active configuration.
                if path and not config_tree.exists(current):
                    config_tree.set(current)
            elif active_tree.exists(current):
                # Descend only into subtrees the operator actually configured.
                _merge_configured_defaults(config_tree, active_tree, value, current)
        elif isinstance(value, list):
            if path and not config_tree.exists(current):
                for item in value:
                    config_tree.set(current, value=item, replace=False)
        else:
            if path and not config_tree.exists(current):
                config_tree.set(current, value=value)


def get_configuration_all() -> str:
    # Native renderer with secret masking as the base: this preserves the
    # canonical output format and hides secret values. Defaults are overlaid
    # afterwards and are never secret, so masking is unaffected.
    base = cmd('cli-shell-api showConfig --show-active-only --show-hide-secrets')
    config_tree = ConfigTree(base)
    # Immutable snapshot of what is explicitly configured, used to decide which
    # subtrees should receive default values.
    active_tree = ConfigTree(base)

    defaults = Config().get_config_defaults([], get_first_key=True, recursive=True)
    _merge_configured_defaults(config_tree, active_tree, defaults, [])

    return config_tree.to_string()


if __name__ == '__main__':
    print(get_configuration_all())
