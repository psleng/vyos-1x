# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

VARIANT = 'test'

# Intentionally EMPTY placeholder stub -- defines NO pins and NO serial ports.
#
# This file ships in vyos-1x only so that ``import vyos.hardware`` works
# everywhere. On real hardware images vyos-build overlays a per-flavor pin map
# (``igos-am64x-*``) on top of it at build time; that overlay is the sole
# source of truth for pins and serial ports.
#
# With an empty pin map, ``vyos.hardware.BOARD`` becomes the inert stub that
# raises a clear error on first pin use, and ``list_serial_ports()`` returns
# nothing. So "no overlay" is treated as "no hardware present" -- exactly like
# the (absent) ETH/WWAN maps -- instead of advertising phantom serial ports
# with plausible-looking tty/dt_node values.
#
# Do NOT add example pin numbers or serial addresses here: a populated stub
# would fabricate ports/pins on any image lacking an overlay and mask a
# missing-overlay build error.
PINS = {}
SERIAL_PORTS = {}
