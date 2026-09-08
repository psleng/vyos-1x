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

"""
Hardware-platform abstraction for fork-only board support (GPIO, serial
muxes, modem power control, etc.).

This package ships in the main ``vyos-1x`` package and contains only
hardware-AGNOSTIC code:

  * :mod:`vyos.hardware.api`   — stable facade for callers
  * :mod:`vyos.hardware.base`  — ``Pin`` dataclass + libgpiod-backed ``Board``
  * :mod:`vyos.hardware.board` — semantic helpers (modem reset, SIM, serial)

The pin map (:mod:`vyos.hardware.pinmap`) ships a small ``VARIANT='test'``
stub in this package; on real hardware images vyos-build overlays a
per-flavor pin map on top of it at build time (see
``vyos-build/data/build-flavors/igos-*``). ``vyos.hardware`` is imported only
by the WWAN runtime and the ``test hardware`` op-mode command — never on a
generic boot/config path — and libgpiod is accessed lazily, so the bundled
stub is inert on non-hardware images. If the pin map module is ever absent,
or defines no pins, ``BOARD`` falls back to a stub that raises a clear error
on first use, so this module remains importable everywhere.

Typical caller usage::

    from vyos.hardware import api as hw
    hw.modem_reset()
    hw.set_pin("UARTC0_MODE0", 1)
"""

import logging

from vyos.hardware.board import BOARD  # noqa: F401

# A library must not print to stderr on its own. Attaching a no-op handler to
# the package logger means that, with no application logging config, records
# are dropped silently instead of falling through to logging.lastResort (which
# would dump WARNING+ to the console). Consumers who want the INFO action log
# call vyos.hardware.api.enable_logging() or configure logging themselves.
logging.getLogger(__name__).addHandler(logging.NullHandler())
