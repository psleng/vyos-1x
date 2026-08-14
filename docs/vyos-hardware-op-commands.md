# VyOS `test hardware` Operational Commands

Out-of-band diagnostics for board GPIO, serial transceivers, and modem
control pins. These commands talk directly to `vyos.hardware.api` and
**bypass the configuration model** — they are intended for bring-up,
bench testing, and field diagnostics on a live system.

> Pin / port state set this way is retained by the kernel GPIO controller
> until something else reprograms it. Use with care.

The command tree lives under `test hardware` (not `set` / `show`) to
make its out-of-band nature explicit.

---

## Show commands

### `test hardware show serial`
List declared serial ports with their transceiver type and tty device.

### `test hardware show modem`
List declared modems.

### `test hardware show pin`
Show GPIO pin state for **all** declared pins.

### `test hardware show pin name <pin-name>`
Show state for a single GPIO pin.

| Parameter | Description |
|---|---|
| `<pin-name>` | Declared GPIO pin name (tab-completion available) |

### `test hardware show rtc`
Show the RV-3028 real-time clock's backup switchover mode.

---

## Serial transceiver control

### `test hardware serial <port-or-tty> protocol <proto>`
Switch a serial port transceiver to the given protocol.

`<port-or-tty>` accepts either form:

- The pinmap port name, e.g. `UARTC2` (case-insensitive).
- Any device path that resolves to a declared port's tty:
  `/dev/ttyS2`, `/dev/igos/uartc2`, an app-installed alias symlink, etc.
  The lookup follows `realpath`, so any symlink chain works as long
  as it eventually points at the same kernel device node.

| Parameter | Values |
|---|---|
| `<port-or-tty>` | Port name or tty path (tab-completion offers both) |
| `<proto>` | `isolate`, `rs232`, `rs485h`, `rs485f`, `rs422` |

### Optional modifiers

These may be appended to the `protocol` command in any order supported
by the tree:

#### `... termination <on\|off>`
Enable or disable line termination.

#### `... termination <on\|off> slew-rate <on\|off>`
Set both termination and slew-rate in a single command.

#### `... slew-rate <on\|off>`
Set slew-rate without changing termination.

---

## Modem control (out-of-band GPIO)

### `test hardware modem <name> reset`
Issue an unconditional reset pulse to the modem.

### `test hardware modem <name> power <on\|off>`
Drive the modem power-enable line.

### `test hardware modem <name> sim <1\|2>`
Select the active SIM slot via the SIM-MUX GPIO.

| Parameter | Description |
|---|---|
| `<name>` | Declared modem name (tab-completion from `show modem`) |

---

## Raw GPIO pin control

### `test hardware pin <name> set <0\|1>`
Drive the named pin to logic level `0` or `1`. The kernel GPIO controller
retains the value until reprogrammed.

### `test hardware pin <name> pulse`
Pulse the named pin (default 200 ms, asserted = 1).

| Parameter | Description |
|---|---|
| `<name>` | Declared GPIO pin name (tab-completion from `show pin`) |

---

## RTC backup switchover mode

The RV-3028 backup switchover mode (BSM) controls whether the RTC keeps
time from its backup cell when main power is removed. It is a **one-time,
non-volatile** setting stored in the RTC's own EEPROM — it survives power
loss and eMMC reflashes — so it is normally written **once at
manufacturing**, not at boot. This command exists only as a field/repair
escape hatch (e.g. after an RTC is replaced).

### `test hardware rtc backup-mode <mode>`
Set the RV-3028 backup switchover mode.

| Parameter | Values |
|---|---|
| `<mode>` | `level-switching` (LSM, production default), `direct-switching` (DSM), `disabled` |

On hardware without an RV-3028 (EVM, x86, …) the command is a safe no-op:
it detects the chip by driver name and reports that no RV-3028 is present
rather than touching any other RTC.

---

## Examples

```sh
# Inspect hardware state
test hardware show serial
test hardware show modem
test hardware show pin
test hardware show pin name MODEM0_UNCOND_RESET

# Switch serial port UARTC2 to RS-485 half-duplex with termination
test hardware serial UARTC2 protocol rs485h termination on

# Same thing, addressed by tty path instead of port name
test hardware serial /dev/ttyS2 protocol rs485h termination on

# Modem bring-up sequence
test hardware modem modem0 power on
test hardware modem modem0 sim 1
test hardware modem modem0 reset

# Raw GPIO poke
test hardware pin SYS_STAT_GREEN set 1
test hardware pin MODEM0_UNCOND_RESET pulse

# RTC backup mode (repair only; normally set at manufacturing)
test hardware show rtc
test hardware rtc backup-mode level-switching
```

---

## Notes

- All commands invoke `${vyos_op_scripts_dir}/test_hardware.py` under
  the hood; see the script for exact behavior and exit codes.
- This subtree is **not** persisted in the running config — re-issuing
  any conf-mode change that touches the same hardware (e.g. a serial or
  WWAN commit) will overwrite the out-of-band state.
- Definition source: [op-mode-definitions/test-hardware.xml.in](op-mode-definitions/test-hardware.xml.in).
