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

"""
Retrieves (or at least attempts to retrieve) the total number of real CPU cores
installed in a Linux system.

The issue of core count is complicated by existence of SMT, e.g. Intel's Hyper Threading.
GNU nproc returns the number of LOGICAL cores,
which is 2x of the real cores if SMT is enabled.

The idea is to find all physical CPUs and add up their core counts.
It has special cases for x86_64 and MAY work correctly on other architectures,
but nothing is certain.
"""

import os
import re

def _read_cpuinfo():
    with open('/proc/cpuinfo', 'r') as f:
        lines = f.read().strip()
        return re.split(r'\n+', lines)

def _split_line(l):
    l = l.strip()
    parts = re.split(r'\s*:\s*', l)
    return (parts[0], ":".join(parts[1:]))

def _find_cpus(cpuinfo_lines):
    # Make a dict because it's more convenient to work with later,
    # when we need to find physicall distinct CPUs there.
    cpus = {}

    cpu_number = 0

    for l in cpuinfo_lines:
        key, value = _split_line(l)
        if key == 'processor':
            cpu_number = value
            cpus[cpu_number] = {}
        else:
            cpus[cpu_number][key] = value

    return cpus

def _find_physical_cpus():
    cpus = _find_cpus(_read_cpuinfo())

    phys_cpus = {}

    for num in cpus:
        if 'physical id' in cpus[num]:
            # On at least some architectures, CPUs in different sockets
            # have different 'physical id' field, e.g. on x86_64.
            phys_id = cpus[num]['physical id']
            if phys_id not in phys_cpus:
                phys_cpus[phys_id] = cpus[num]
        else:
            # On other architectures, e.g. on ARM, there's no such field.
            # We just assume they are different CPUs,
            # whether single core ones or cores of physical CPUs.
            phys_cpus[num] = cpus[num]

    return phys_cpus

def get_cpus():
    """ Returns a list of /proc/cpuinfo entries that belong to different CPUs.
    """
    cpus_dict = _find_physical_cpus()
    return list(cpus_dict.values())


# Arm implementer codes (MIDR_EL1 bits 31:24). These identify who designed the
# core, not who sells the SoC. Source: Linux arch/arm64/include/asm/cputype.h.
ARM_IMPLEMENTERS = {
    0x41: 'Arm Limited',
    0x42: 'Broadcom',
    0x43: 'Cavium',
    0x44: 'DEC',
    0x46: 'Fujitsu',
    0x48: 'HiSilicon',
    0x49: 'Infineon',
    0x4d: 'Motorola/Freescale',
    0x4e: 'NVIDIA',
    0x50: 'Applied Micro (APM)',
    0x51: 'Qualcomm',
    0x53: 'Samsung',
    0x56: 'Marvell',
    0x61: 'Apple',
    0x66: 'Faraday',
    0x69: 'Intel',
    0x70: 'Phytium',
    0xc0: 'Ampere',
}

# Arm-designed core part numbers (MIDR_EL1 bits 15:4). Only meaningful when the
# implementer is Arm (0x41); other implementers reuse the same numeric space for
# their own cores, so we only decode part names for Arm-designed cores.
ARM_PARTS = {
    0xc05: 'Cortex-A5',
    0xc07: 'Cortex-A7',
    0xc08: 'Cortex-A8',
    0xc09: 'Cortex-A9',
    0xc0d: 'Cortex-A12',
    0xc0e: 'Cortex-A17',
    0xc0f: 'Cortex-A15',
    0xc14: 'Cortex-R4',
    0xc15: 'Cortex-R5',
    0xc17: 'Cortex-R7',
    0xc18: 'Cortex-R8',
    0xc20: 'Cortex-M0',
    0xc21: 'Cortex-M1',
    0xc23: 'Cortex-M3',
    0xc24: 'Cortex-M4',
    0xc27: 'Cortex-M7',
    0xd01: 'Cortex-A32',
    0xd02: 'Cortex-A34',
    0xd03: 'Cortex-A53',
    0xd04: 'Cortex-A35',
    0xd05: 'Cortex-A55',
    0xd06: 'Cortex-A65',
    0xd07: 'Cortex-A57',
    0xd08: 'Cortex-A72',
    0xd09: 'Cortex-A73',
    0xd0a: 'Cortex-A75',
    0xd0b: 'Cortex-A76',
    0xd0c: 'Neoverse-N1',
    0xd0d: 'Cortex-A77',
    0xd0e: 'Cortex-A76AE',
    0xd13: 'Cortex-R52',
    0xd20: 'Cortex-M23',
    0xd21: 'Cortex-M33',
    0xd40: 'Neoverse-V1',
    0xd41: 'Cortex-A78',
    0xd42: 'Cortex-A78AE',
    0xd44: 'Cortex-X1',
    0xd46: 'Cortex-A510',
    0xd47: 'Cortex-A710',
    0xd48: 'Cortex-X2',
    0xd49: 'Neoverse-N2',
    0xd4a: 'Neoverse-E1',
    0xd4b: 'Cortex-A78C',
    0xd4d: 'Cortex-A715',
    0xd4e: 'Cortex-X3',
}


def _to_int(value):
    """Parse a /proc/cpuinfo numeric field (e.g. '0x41' or '8'), or None."""
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return None


def decode_arm_cpu(cpu):
    """Decode the raw Arm MIDR fields of a /proc/cpuinfo entry.

    Arm cores expose only the raw CPU-ID register fields ('CPU implementer',
    'CPU part', 'CPU variant', 'CPU revision') instead of the friendly
    'model name' string that x86 kernels provide. This maps those values to
    human-readable names.

    Returns a dict with any of the keys 'vendor', 'model' and 'revision' that
    could be derived; an empty dict for non-Arm entries or unknown values.
    """
    if 'CPU implementer' not in cpu:
        return {}

    decoded = {}

    implementer = _to_int(cpu.get('CPU implementer'))
    if implementer in ARM_IMPLEMENTERS:
        decoded['vendor'] = ARM_IMPLEMENTERS[implementer]

    part = _to_int(cpu.get('CPU part'))
    if implementer == 0x41 and part in ARM_PARTS:
        decoded['model'] = ARM_PARTS[part]

    # The Arm revision is reported as two separate fields: 'CPU variant' is the
    # major ('r') and 'CPU revision' the minor ('p'), e.g. r0p4.
    variant = _to_int(cpu.get('CPU variant'))
    revision = _to_int(cpu.get('CPU revision'))
    if variant is not None and revision is not None:
        decoded['revision'] = f'r{variant}p{revision}'

    return decoded


def get_core_count():
    """ Returns the total number of physical CPU cores
        (even if Hyper-Threading or another SMT is enabled and has inflated
        the number of cores in /proc/cpuinfo)
    """
    physical_cpus = _find_physical_cpus()

    core_count = 0

    for num in physical_cpus:
        # Some architectures, e.g. x86_64, include a field for core count.
        # Since we found unique physical CPU entries, we can sum their core counts.
        if 'cpu cores' in physical_cpus[num]:
            core_count += int(physical_cpus[num]['cpu cores'])
        else:
            core_count += 1

    return core_count


def get_available_cpus():
    """ List of cpus with ids that are available in the system
        Uses 'lscpu' command

        Returns: list[dict[str, str | int | bool]]: cpus details
    """
    import json

    from vyos.utils.process import cmd

    out = json.loads(cmd('lscpu --extended -b --json'))

    return out['cpus']


def get_half_cpus():
    """ return 1/2 of the numbers of available CPUs """
    return max(1, os.cpu_count() // 2)
