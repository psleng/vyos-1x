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
from vyos.system.hardware import get_board_identity

from jinja2 import Template

model_template = Template("""Manufacturer:     {{vendor or 'unknown'}}
Model:            {{model_name or 'unknown'}}
Revision:         {{revision or 'unknown'}}
Serial Number:    {{serial or 'unknown'}}
UUID:             {{uuid or 'unknown'}}""")


def _get_raw_data():
    # Board identity comes straight from the nvmem identity EEPROM cells
    # (perle-device-info: vendor/product/model/board_revision/serial/uuid),
    # or from DMI on x86.
    return get_board_identity()


def _format_model(data):
    model_name = ' '.join(
        value for value in (data.get('product'), data.get('model')) if value
    )
    context = dict(data)
    context['model_name'] = model_name
    return model_template.render(context).strip()


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
