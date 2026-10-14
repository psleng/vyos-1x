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

import re
import sys

import vyos.opmode
from vyos.utils.cpu import get_cpus
from vyos.utils.cpu import get_core_count
from vyos.utils.cpu import decode_arm_cpu

from jinja2 import Template

cpu_template = Template("""
{% for cpu in cpus %}
{% if 'physical id' in cpu %}CPU socket: {{cpu['physical id']}}{% endif %}
{% if 'vendor_id' in cpu %}CPU Vendor:       {{cpu['vendor_id']}}{% endif %}
{% if 'model name' in cpu %}Model:            {{cpu['model name']}}{% endif %}
{% if 'cpu cores' in cpu %}Cores:            {{cpu['cpu cores']}}{% endif %}
{% if 'cpu MHz' in cpu %}Current MHz:      {{cpu['cpu MHz']}}{% endif %}
{# ARM (and other architectures) do not expose the x86 keys above in       #}
{# /proc/cpuinfo, so fall back to the fields they do provide. The raw MIDR  #}
{# register values are decoded to human-readable names where possible and   #}
{# shown alongside the raw hex. The clock speed is intentionally omitted on #}
{# Arm: /proc/cpuinfo carries only BogoMIPS (a generic-timer artefact, not  #}
{# the core clock), and this platform exposes no reliable clock source.     #}
{% if 'CPU implementer' in cpu %}CPU Vendor:       {{cpu.get('cpu_vendor', 'Unknown')}} ({{cpu['CPU implementer']}}){% endif %}
{% if 'CPU architecture' in cpu %}CPU Architecture: ARMv{{cpu['CPU architecture']}}{% endif %}
{% if 'CPU part' in cpu %}Model:            {{cpu.get('cpu_model', 'Unknown')}} ({{cpu['CPU part']}}){% endif %}
{% if 'cpu_revision' in cpu %}CPU Revision:     {{cpu['cpu_revision']}}{% endif %}
{% endfor %}
""")

cpu_summary_template = Template("""
Physical CPU cores: {{count}}
CPU model(s): {{models | join(", ")}}
""")

def _enrich_arm(cpu_data):
    # Augment each CPU entry with decoded Arm identity (vendor/model/revision)
    # without discarding the raw /proc/cpuinfo fields, so both the human output
    # and the raw/API output carry readable names on Arm.
    enriched = []
    for cpu in cpu_data:
        cpu = dict(cpu)
        decoded = decode_arm_cpu(cpu)
        if 'vendor' in decoded:
            cpu['cpu_vendor'] = decoded['vendor']
        if 'model' in decoded:
            cpu['cpu_model'] = decoded['model']
        if 'revision' in decoded:
            cpu['cpu_revision'] = decoded['revision']
        enriched.append(cpu)
    return enriched

def _cpu_model(cpu):
    # x86 exposes a friendly "model name"; ARM and some other architectures
    # do not, so build a best-effort label from the fields they provide.
    if 'model name' in cpu:
        return cpu['model name']

    if 'cpu_model' in cpu:
        revision = cpu.get('cpu_revision')
        return f"{cpu['cpu_model']} ({revision})" if revision \
            else cpu['cpu_model']

    parts = []
    if 'cpu_vendor' in cpu:
        parts.append(cpu['cpu_vendor'])
    elif 'CPU implementer' in cpu:
        parts.append(f"impl {cpu['CPU implementer']}")
    if 'CPU part' in cpu:
        parts.append(f"part {cpu['CPU part']}")
    if parts:
        return "ARM CPU (" + ", ".join(parts) + ")"

    return 'unknown'

def _get_raw_data():
    return _enrich_arm(get_cpus())

def _format_cpus(cpu_data):
    env = {'cpus': cpu_data}
    rendered = cpu_template.render(env)
    # The per-field "{% if %}" lines leave empty lines for keys that an
    # architecture does not expose; collapse runs of blank lines so each CPU
    # block is contiguous with a single blank line between CPUs.
    return re.sub(r'\n\s*\n+', '\n\n', rendered).strip()

def _get_summary_data():
    count = get_core_count()
    cpu_data = _enrich_arm(get_cpus())
    models = [_cpu_model(c) for c in cpu_data]
    env = {'count': count, "models": models}

    return env

def _format_cpu_summary(summary_data):
    return cpu_summary_template.render(summary_data).strip()

def show(raw: bool):
    cpu_data = _get_raw_data()

    if raw:
        return cpu_data
    else:
        return _format_cpus(cpu_data)

def show_summary(raw: bool):
    cpu_summary_data = _get_summary_data()

    if raw:
        return cpu_summary_data
    else:
        return _format_cpu_summary(cpu_summary_data)


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
