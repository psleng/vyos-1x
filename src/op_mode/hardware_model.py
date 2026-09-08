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

import sys

import vyos.opmode
from vyos.system import model as _model

from jinja2 import Template

model_template = Template("""Product ID:        {{prod_id or 'unknown'}}
Model:             {{model or 'unknown'}}
Model ID:          {{id or 'unknown'}}
Platform:          {{platform or 'unknown'}}
{% if board_model %}Board model:       {{board_model}}
{% endif %}{% if definition %}Model definition:  {{definition}}
{% endif %}""")


def _read_device_tree_model():
    # The kernel exposes the board's device-tree "model" string here, e.g.
    # "Perle AM6412 IOLAN". Absent on non-DT platforms (x86).
    for path in ('/proc/device-tree/model',
                 '/sys/firmware/devicetree/base/model'):
        try:
            with open(path, 'rb') as f:
                return f.read().rstrip(b'\x00').decode('utf-8', 'replace').strip()
        except OSError:
            continue
    return ''


def _get_raw_data():
    identity = _model.resolve_identity()

    board_model = _read_device_tree_model()

    definition = ''
    try:
        found = _model.find_model()
        if found is not None:
            definition = found.name
    except Exception:  # noqa: BLE001 -- never let display break on a data bug
        definition = ''

    data = dict(identity)
    data['board_model'] = board_model
    data['definition'] = definition
    return data


def _format_model(data):
    return model_template.render(data).strip()


def show(raw: bool):
    data = _get_raw_data()

    if raw:
        return data
    else:
        return _format_model(data)


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
