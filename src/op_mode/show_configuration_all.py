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

from vyos.config import Config
from vyos.config import merge_defaults
from vyos.configtree import ConfigTree
from vyos.utils.process import cmd


def get_configuration_all() -> str:
    # Native renderer with secret masking as the base: this preserves the
    # canonical output format and hides secret values. Defaults are overlaid
    # afterwards and are never secret, so masking is unaffected.
    base = cmd('cli-shell-api showConfig --show-active-only --show-hide-secrets')
    config_tree = ConfigTree(base)

    defaults = Config().get_config_defaults([], get_first_key=True, recursive=True)
    merge_defaults(config_tree, defaults)

    return config_tree.to_string()


if __name__ == '__main__':
    print(get_configuration_all())
