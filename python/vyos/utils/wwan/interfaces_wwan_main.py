#!/usr/bin/env python3
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

import asyncio
import os
import signal
import subprocess
import sys
import time
from dbus_next.aio import MessageBus  # pylint: disable=import-error
from vyos.utils.wwan.interfaces_wwan_service_manager import ConfigServiceManager
from dbus_next.constants import BusType  # pylint: disable=import-error
from dbus_next.message import Message  # pylint: disable=import-error
from vyos.utils.wwan import interfaces_wwan_diag as wwan_diag
from vyos.utils.wwan.interfaces_wwan_util import (
    hardware_reset_all_modems,
    system_is_stopping,
)
from vyos.utils.wwan.wwan_logging import setup_logging


# Set up logging — use root logger for manager so all module logs are captured
logger = setup_logging("", "wwan-manager")

class ModemManagerMonitor:
    def __init__(self, service_manager):
        self.service_manager = service_manager
        self.monitoring = False
        self.restart_attempts = 0
        self.max_restart_attempts = 5
        self.restart_delay = 5
        # Dedicated, long-lived system-bus connection used solely to watch
        # org.freedesktop.DBus NameOwnerChanged for ModemManager.  Kept
        # separate from the service/FSM bus because the latter is torn down
        # and replaced on every reconnect (update_bus_connection disconnects
        # the old bus); this watch must survive that swap.
        self._name_owner_bus = None
        self._last_mm_owner = ''
        self._reconnect_lock = asyncio.Lock()
        self._last_reconnected_owner = ''
        self._fsm_reconnect_required = False
        self._restart_exhaustion_backoff = 60
        # G1 -- wedged-but-running MM detection.  `systemctl is-active` only
        # proves the PROCESS is up; an MM whose D-Bus has gone unresponsive
        # still reads 'active' yet answers no method calls, and is invisible
        # everywhere except a CONNECTED interface's ping.  Probe MM's D-Bus
        # each cycle; after this many CONSECUTIVE unanswered probes treat MM
        # as wedged and drive the same recovery as a hard crash.  Conservative
        # threshold -- a too-eager MM restart can cascade.
        self._mm_dbus_miss_count = 0
        self._mm_dbus_miss_threshold = 4

    async def monitor_modemmanager(self):
        """Monitor MM process, D-Bus responsiveness, and pending FSM rebinds."""
        self.monitoring = True
        logger.info("ModemManager monitoring started",
                   extra={'max_attempts': self.max_restart_attempts})

        while self.monitoring:
            try:
                # Check if ModemManager is still running
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "is-active", "ModemManager"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if result.returncode != 0 or result.stdout.strip() != "active":
                    logger.error("ModemManager has crashed or stopped "
                                "(systemd reports inactive)")
                    self._mm_dbus_miss_count = 0  # crash path owns recovery
                    await self.handle_modemmanager_crash()
                elif await self._probe_mm_dbus_responsive():
                    # Process up AND answering D-Bus -- genuinely healthy.
                    if self._mm_dbus_miss_count:
                        logger.info(
                            "ModemManager D-Bus responsive again after "
                            "%d missed probe(s)", self._mm_dbus_miss_count,
                            extra={'mm_dbus_miss_count':
                                   self._mm_dbus_miss_count})
                        self._mm_dbus_miss_count = 0
                    if self._fsm_reconnect_required:
                        logger.warning(
                            "ModemManager is healthy but FSM rebind is still "
                            "pending; retrying reconnect",
                            extra={'current_owner':
                                   self._last_mm_owner or '(unknown)'})
                        await self._reconnect_after_restart(
                            trigger='health_poll_pending_rebind')
                    # Reset restart attempts if ModemManager is running fine
                    if (self.restart_attempts > 0
                            and not self._fsm_reconnect_required):
                        logger.info("ModemManager is stable again, resetting restart counter")
                        self.restart_attempts = 0
                else:
                    # Process is 'active' but did NOT answer D-Bus -- a wedged
                    # MM looks exactly like this.  Count consecutive misses and
                    # only escalate once we are confident (not a transient
                    # blip / mid-restart).
                    self._mm_dbus_miss_count += 1
                    logger.warning(
                        "ModemManager is 'active' but did not answer D-Bus "
                        "(missed probe %d/%d) -- possible wedged ModemManager",
                        self._mm_dbus_miss_count, self._mm_dbus_miss_threshold,
                        extra={'mm_dbus_miss_count': self._mm_dbus_miss_count,
                               'mm_dbus_miss_threshold':
                                   self._mm_dbus_miss_threshold})
                    if self._mm_dbus_miss_count >= self._mm_dbus_miss_threshold:
                        logger.error(
                            "ModemManager wedged: 'active' but unresponsive on "
                            "D-Bus for %d consecutive probes -- forcing restart",
                            self._mm_dbus_miss_count,
                            extra={'mm_dbus_miss_count':
                                   self._mm_dbus_miss_count})
                        self._mm_dbus_miss_count = 0
                        await self.handle_modemmanager_crash()

                # Check every 10 seconds
                await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"Error monitoring ModemManager: {e}")
                await asyncio.sleep(5)

    async def _probe_mm_dbus_responsive(self, timeout: float = 8.0) -> bool:
        """Bounded probe: does ModemManager actually answer on D-Bus?

        A wedged-but-running MM passes `systemctl is-active` but never replies
        to method calls. One bounded GetManagedObjects on the dedicated watch
        bus (or service-bus fallback) tells us whether MM itself is alive,
        independent of FSM-bus replacement. Returns True when no bus exists
        yet so early startup cannot manufacture a false wedge.
        """
        # Probe on the dedicated long-lived NameOwner watch bus whenever it is
        # available. The service/FSM bus is intentionally replaced during MM
        # recovery; probing that transient bus would misdiagnose "FSM rebind
        # still pending" as "MM itself is wedged" and cause a needless second
        # restart. Fall back only when the watch could not be established.
        bus = self._name_owner_bus or getattr(self.service_manager, 'bus', None)
        if bus is None:
            return True
        try:
            from dbus_next import Message  # pylint: disable=import-error
            msg = Message(
                destination="org.freedesktop.ModemManager1",
                path="/org/freedesktop/ModemManager1",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects")
            reply = await asyncio.wait_for(bus.call(msg), timeout=timeout)
            return (reply is not None
                    and reply.message_type.name == "METHOD_RETURN")
        except Exception as e:
            logger.debug(f"ModemManager D-Bus probe failed: {e}")
            return False

    async def handle_modemmanager_crash(self):
        """Recover an inactive or D-Bus-unresponsive ModemManager instance."""
        if self.restart_attempts >= self.max_restart_attempts:
            # Never destroy ConfigServiceManager here. shutdown() clears all
            # FSM/interface dictionaries, making later MM recovery impossible
            # without another config commit. This is an unattended remote
            # unit: cool down, reset the attempt window, and keep trying.
            logger.critical(
                "ModemManager restart attempts exhausted; preserving FSMs and "
                "retrying after cooldown",
                extra={'restart_attempt': self.restart_attempts,
                       'max_attempts': self.max_restart_attempts,
                       'backoff_seconds': self._restart_exhaustion_backoff})
            await asyncio.sleep(self._restart_exhaustion_backoff)
            self.restart_attempts = 0
            return

        self.restart_attempts += 1
        logger.info("Attempting ModemManager restart",
                   extra={'restart_attempt': self.restart_attempts,
                          'max_attempts': self.max_restart_attempts})

        # Boot-scoped diagnostic counter: total MM crashes recovered since
        # power on (distinct from restart_attempts, which is a backoff counter
        # that resets once MM is stable again).
        try:
            mm_restarts = wwan_diag.increment('modemmanager_restart_count')
            logger.info("ModemManager crash-recovery restart recorded",
                       extra={'modemmanager_restart_count': mm_restarts})
        except Exception:
            pass

        # Try to restart ModemManager
        if await self.restart_modemmanager():
            logger.info("ModemManager restarted successfully",
                       extra={'restart_attempt': self.restart_attempts})
            self._fsm_reconnect_required = True
            await self._reconnect_after_restart(trigger='active_restart')
        else:
            logger.error("Failed to restart ModemManager",
                        extra={'restart_attempt': self.restart_attempts})
            await asyncio.sleep(self.restart_delay)

    def stop_monitoring(self):
        """Stop monitoring ModemManager"""
        self.monitoring = False
        logger.info("ModemManager monitoring stopped")

    async def setup_name_owner_watch(self):
        """Watch D-Bus NameOwnerChanged for ModemManager (event-driven restart
        detection).

        The 10s ``systemctl is-active`` poll in :meth:`monitor_modemmanager`
        cannot catch a fast restart: a ``systemctl restart ModemManager`` (or
        systemd's own ``Restart=`` auto-restart after a crash) brings MM back
        well within the poll interval, so the poll observes ``active`` on both
        sides and :meth:`handle_modemmanager_crash` never fires.  The FSMs are
        then left holding stale proxies bound to the dead MM instance while the
        fresh instance leaves the modem in the ``disabled`` state it boots into
        — the connection is never re-established.

        Subscribing to ``org.freedesktop.DBus.NameOwnerChanged`` for the
        ModemManager bus name is event-driven and cannot miss a restart, no
        matter how brief.  This complements (does not replace) the poll, which
        still actively restarts MM if it stays down.
        """
        try:
            self._name_owner_bus = await MessageBus(
                bus_type=BusType.SYSTEM).connect()
            introspect = await self._name_owner_bus.introspect(
                "org.freedesktop.DBus", "/org/freedesktop/DBus")
            dbus_obj = self._name_owner_bus.get_proxy_object(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", introspect)
            dbus_iface = dbus_obj.get_interface("org.freedesktop.DBus")

            # Seed the current owner so we react only to genuine changes
            # (we get no signal for an MM that is already running).
            try:
                self._last_mm_owner = await dbus_iface.call_get_name_owner(
                    "org.freedesktop.ModemManager1")
            except Exception:
                self._last_mm_owner = ''

            dbus_iface.on_name_owner_changed(self._on_mm_name_owner_changed)
            logger.info("ModemManager NameOwnerChanged watch active",
                        extra={'current_owner': self._last_mm_owner or '(none)'})
        except Exception as e:
            logger.error(
                f"Failed to set up ModemManager NameOwnerChanged watch: {e}")

    def _on_mm_name_owner_changed(self, name, old_owner, new_owner):
        """Signal handler: react when ModemManager (re)claims its bus name."""
        if name != "org.freedesktop.ModemManager1":
            return
        if not new_owner:
            # Bare disappearance: the active monitor owns restart/retry. Mark
            # the FSM rebind pending so the health loop completes it once MM's
            # replacement ObjectManager answers.
            logger.warning("ModemManager bus name owner lost",
                           extra={'old_owner': old_owner or '(none)'})
            self._last_mm_owner = ''
            self._fsm_reconnect_required = True
            return
        if new_owner == self._last_mm_owner:
            return  # no real change
        logger.warning(
            "ModemManager (re)appeared on D-Bus — triggering FSM reconnect",
            extra={'old_owner': old_owner or '(none)', 'new_owner': new_owner})
        self._last_mm_owner = new_owner
        self._fsm_reconnect_required = True
        asyncio.create_task(self._reconnect_after_restart(
            trigger='name_owner_changed', expected_owner=new_owner))

    @staticmethod
    async def _get_mm_owner_on_bus(bus) -> str:
        """Return MM's unique D-Bus owner on *bus*, or '' if unavailable."""
        try:
            msg = Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="GetNameOwner",
                signature="s",
                body=["org.freedesktop.ModemManager1"])
            reply = await asyncio.wait_for(bus.call(msg), timeout=5.0)
            if reply.message_type.name == "METHOD_RETURN" and reply.body:
                return str(reply.body[0])
        except Exception:
            pass
        return ''

    async def _reconnect_after_restart(self, trigger='unknown',
                                        expected_owner=''):
        """Rebuild FSM proxies and re-initialize after MM (re)appears.

        Guarded so overlapping NameOwnerChanged signals (down→up often arrives
        as two events) and a racing systemctl-poll recovery cannot drive two
        concurrent reconnects.
        """
        async with self._reconnect_lock:
            bus = None
            try:
                # The bus name can be claimed slightly before MM's
                # ObjectManager is responsive; wait for a real method reply.
                bus = await ModemManagerMonitor.wait_for_modemmanager_dbus()
                if not bus:
                    logger.error(
                        "ModemManager reconnect could not obtain a responsive "
                        "ObjectManager; rebind remains pending",
                        extra={'trigger': trigger,
                               'expected_owner': expected_owner or '(unknown)'})
                    self._fsm_reconnect_required = True
                    return False

                actual_owner = await self._get_mm_owner_on_bus(bus)
                if (actual_owner
                        and actual_owner == self._last_reconnected_owner):
                    logger.info(
                        "FSMs already rebound to this ModemManager owner; "
                        "skipping duplicate reconnect",
                        extra={'trigger': trigger,
                               'owner': actual_owner})
                    self._fsm_reconnect_required = False
                    bus.disconnect()
                    return True

                logger.info(
                    "Reconnecting FSMs to fresh ModemManager instance",
                    extra={'trigger': trigger,
                           'expected_owner': expected_owner or '(unknown)',
                           'actual_owner': actual_owner or '(unknown)',
                           'previous_owner':
                               self._last_reconnected_owner or '(none)'})
                await self.service_manager.update_bus_connection(bus)
                self._last_reconnected_owner = actual_owner or expected_owner
                self._fsm_reconnect_required = False
                logger.info(
                    "FSM reconnect to ModemManager completed",
                    extra={'trigger': trigger,
                           'owner':
                               self._last_reconnected_owner or '(unknown)'})
                return True
            except Exception as e:
                self._fsm_reconnect_required = True
                logger.error(
                    f"FSM reconnect after ModemManager restart failed: {e}",
                    extra={'trigger': trigger,
                           'expected_owner': expected_owner or '(unknown)'})
                return False

    def disconnect_name_owner_watch(self):
        """Tear down the dedicated NameOwnerChanged watch bus."""
        if self._name_owner_bus:
            try:
                self._name_owner_bus.disconnect()
            except Exception as e:  # noqa: BLE001 -- best-effort cleanup
                logger.debug(f"Error disconnecting name-owner watch bus: {e}")
            self._name_owner_bus = None

    # ModemManager is a singleton system service, so the operations that
    # check/start/restart it and wait for it on D-Bus are stateless and
    # act on that one global resource.  They are exposed as static methods
    # so main()'s bootstrap can call them before any monitor instance owns
    # a service manager, while still living with the rest of the
    # ModemManager-control logic.
    @staticmethod
    async def check_and_start_modemmanager():
        """Check if ModemManager is running and start it if necessary"""
        try:
            # Check if ModemManager service is active
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "is-active", "ModemManager"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0 and result.stdout.strip() == "active":
                logger.info("ModemManager is already running")
                return True

            logger.info("ModemManager is not running, attempting to start it...")
            return await ModemManagerMonitor.restart_modemmanager()

        except Exception as e:
            logger.error(f"Error checking/starting ModemManager: {e}")
            return False

    @staticmethod
    async def stop_modemmanager():
        """Stop the ModemManager system service.

        This manager owns ModemManager's lifecycle: it is the sole starter
        of MM (see check_and_start_modemmanager, run on our own startup), so
        MM must not be left running once we are gone.  On a standalone
        `systemctl stop`/`restart igos-wwan-manager` (the system staying up)
        nothing else would stop MM, so we stop it here.

        NOT used on a full-system reboot/poweroff: there systemd stops MM
        right after us via the unit's `After=ModemManager.service` ordering,
        and our own graceful bearer disconnect needs MM alive until we exit.

        Runs on the teardown path — bounded by the caller and must never
        raise.  The blocking `systemctl` call is offloaded to a thread with
        its own timeout so it cannot stall the event loop or wedge exit.
        """
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "stop", "ModemManager"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 -- best-effort teardown
            logger.error(f"Error stopping ModemManager: {e}")
            return False

        if result.returncode == 0:
            logger.info("ModemManager stopped (manager owns its lifecycle)")
            return True
        logger.warning(
            f"ModemManager stop had issues: {result.stderr.strip()}")
        return False

    @staticmethod
    async def restart_modemmanager():
        """Restart ModemManager service with enhanced stability checking.

        Performance-sensitive: this runs in the boot path (once per boot
        when there is no existing MM running) and may also be invoked as a
        crash-recovery nuclear option later.  Steps that exist purely for
        the "MM is currently running and possibly wedged" case are skipped
        when MM is inactive, saving ~2s on the cold-start case.
        """
        try:
            # Detect whether MM is currently up.  When it is NOT, the
            # `systemctl stop` + cleanup sleep below are pure waste -- we
            # are about to start it fresh, there is nothing to stop and
            # nothing to drain.  Skipping them shaves ~2s off cold boot.
            active_result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "is-active", "--quiet", "ModemManager"],
                timeout=10,
            )
            is_active = active_result.returncode == 0

            if is_active:
                # First try to stop it cleanly
                stop_result = await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "stop", "ModemManager"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )

                if stop_result.returncode == 0:
                    logger.info("ModemManager stopped cleanly")
                else:
                    logger.warning(
                        f"ModemManager stop had issues: {stop_result.stderr}")

                # Wait a moment for cleanup of the previous instance.
                await asyncio.sleep(2)
            else:
                logger.info("ModemManager is not running, fresh start (no drain)")

            # Re-trigger USB udev rules before starting MM.
            #
            # Why: at cold boot, udev runs the rules for the modem's parent
            # usb_device but in some cases does NOT persist a /run/udev/data
            # entry for it (we have observed +usb:1-1:1.N entries for the
            # USB interfaces but no +usb:1-1 entry for the parent device).
            # MM uses libgudev which reads /run/udev/data/<...> files, so
            # without a parent-device entry MM never sees ID_MM_PHYSDEV_UID
            # and the modem ends up identified by sysfs path instead of by
            # the physical-slot UID we set in 60-Perle-usb-modem.rules.
            #
            # Re-triggering the usb subsystem with action=change forces udev
            # to re-evaluate the rules AND persist the resulting properties
            # to /run/udev/data/+usb:*.  We then `settle` to make sure all
            # workers finish before we hand off to MM.
            #
            # This is cheap (a couple hundred ms on this hardware) and only
            # runs in the MM-start path -- it does not affect steady-state
            # operation.
            logger.info("Re-triggering udev for USB devices before MM start")
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["udevadm", "trigger", "--action=change",
                     "--subsystem-match=usb"],
                    capture_output=True, text=True, timeout=10,
                )
                await asyncio.to_thread(
                    subprocess.run,
                    ["udevadm", "settle", "--timeout=10"],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception as exc:  # noqa: BLE001 -- best effort
                logger.warning(f"udevadm trigger/settle failed: {exc}")

            # Start ModemManager
            start_result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "start", "ModemManager"],
                capture_output=True,
                text=True,
                timeout=15,
            )

            if start_result.returncode == 0:
                logger.info("ModemManager started successfully")

                # We intentionally do NOT sleep here.  The next step in main()
                # is wait_for_modemmanager_dbus(), which polls the actual
                # ObjectManager interface -- that's the only "ready" signal
                # that matters to us.  The old code added a 3s blanket sleep
                # plus a 1s-per-attempt is-active loop plus a 2s post-confirm
                # sleep before returning to that D-Bus wait, which was 5-8s
                # of pure paranoia on top of the wait it then performed
                # anyway.  Cold-boot impact: ~5s saved.
                return True
            else:
                logger.error(
                    f"Failed to start ModemManager: {start_result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error restarting ModemManager: {e}")
            return False

    @staticmethod
    async def wait_for_modemmanager_dbus():
        """Wait for ModemManager to be available and responsive on D-Bus.

        Tight, monotonic-deadline poll of the ObjectManager interface.
        Back-off progressively starting from 100ms, capped at 1s, so the
        common boot-time case (MM responsive on the first probe once we've
        already started it) returns immediately rather than waiting a full
        poll interval.  Total wall-time upper bound is ~30s, but typical
        happy-path time
        drops from 2-4s to well under 500ms.
        """
        overall_deadline = time.monotonic() + 30.0
        delay = 0.1
        attempt = 0
        bus = None

        logger.info("Waiting for ModemManager D-Bus interface...")
        while time.monotonic() < overall_deadline:
            attempt += 1
            try:
                if bus:
                    bus.disconnect()
                bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                await bus.introspect(
                    "org.freedesktop.ModemManager1",
                    "/org/freedesktop/ModemManager1")
                msg = Message(
                    destination="org.freedesktop.ModemManager1",
                    path="/org/freedesktop/ModemManager1",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
                await asyncio.wait_for(bus.call(msg), timeout=5.0)
                logger.info(
                    "ModemManager is fully available and responsive on D-Bus")
                return bus
            except Exception as e:  # noqa: BLE001 -- intentional broad catch
                logger.debug(
                    f"MM D-Bus not ready (attempt {attempt}): {e}")
            if bus:
                bus.disconnect()
                bus = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)

        logger.error("ModemManager did not become available on D-Bus")
        return None

async def main():
    logger.info("Starting WWAN Interface Manager",
               extra={'software': 'vyos-wwan', 'version': '1.0'})

    # Boot-scoped diagnostic counter: a value >1 since power on means the
    # manager previously crashed or was restarted.
    try:
        start_count = wwan_diag.increment('service_start_count')
        logger.info("WWAN manager start recorded",
                   extra={'service_start_count': start_count})
    except Exception:
        pass

    # Check and start ModemManager if needed
    if not await ModemManagerMonitor.check_and_start_modemmanager():
        logger.error("Could not ensure ModemManager is running")
        sys.exit(1)

    # Wait for ModemManager to be available on D-Bus
    bus = await ModemManagerMonitor.wait_for_modemmanager_dbus()
    if not bus:
        logger.error("ModemManager is not available on D-Bus")
        sys.exit(1)

    manager = None
    monitor = None

    # Graceful shutdown on SIGTERM/SIGINT.  systemd `stop` delivers
    # SIGTERM, which by default would kill us outright and skip the
    # finally-block cleanup below (manager.shutdown(), bus.disconnect()).
    # Setting a stop Event from the signal handler makes the wait()
    # below return so the normal teardown path runs.  This mirrors the
    # sibling igos-wwan-snmp-traps daemon (snmp_traps.py).
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Unavailable off the main thread / on some platforms; fall
            # back to default disposition (SIGINT -> KeyboardInterrupt,
            # caught below).
            pass

    try:
        # Create service manager
        manager = ConfigServiceManager(bus)

        # Create and start ModemManager monitor
        monitor = ModemManagerMonitor(manager)
        # Event-driven MM-restart detection (catches fast restarts the 10s
        # systemctl poll misses); complements monitor_modemmanager().
        await monitor.setup_name_owner_watch()
        monitor_task = asyncio.create_task(monitor.monitor_modemmanager())

        logger.info("Starting WWAN configuration service with ModemManager monitoring...")
        logger.info("Service ready - interfaces will be created via D-Bus calls")

        # Start the service manager without any initial interfaces
        # Interfaces will be created dynamically via D-Bus AddInterface calls
        service_task = asyncio.create_task(manager.run())

        # Wake up when a component task exits OR a shutdown signal fires.
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            [monitor_task, service_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED)

        if stop_event.is_set():
            logger.info("Received shutdown signal, shutting down...")

        # Cancel remaining tasks.  Bound each join: a cancelled task may be
        # parked inside a non-cancellable dbus_next call to a ModemManager
        # that is going down in the same reboot transaction, in which case
        # CancelledError cannot be delivered until that call returns (it may
        # never).  An unbounded `await task` there would wedge the process
        # until systemd's TimeoutStopSec SIGKILLs us.
        for task in pending:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:  # noqa: BLE001 -- best-effort cleanup
                logger.debug(f"Error awaiting cancelled task: {e}")

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Service error: {e}")
        sys.exit(1)
    finally:
        # Cleanup
        if monitor:
            monitor.stop_monitoring()
            monitor.disconnect_name_owner_watch()
        if manager:
            # Bound the shutdown.  manager.shutdown() awaits a per-FSM
            # bearer disconnect over D-Bus to ModemManager (no client-side
            # timeout in dbus_next) plus downstream nft/subprocess teardown,
            # all run sequentially per modem.  If MM is wedged — precisely
            # the failure this service exists to ride out — an unbounded
            # await here would stall until systemd's TimeoutStopSec fires
            # and SIGKILLs us, skipping the bus.disconnect() below.  Cap it
            # so we always reach a clean disconnect; the unit's
            # TimeoutStopSec is set higher as a backstop.
            try:
                await asyncio.wait_for(manager.shutdown(), timeout=20.0)
            except asyncio.TimeoutError:
                logger.error(
                    "ConfigServiceManager shutdown timed out after 20s; "
                    "forcing teardown")
            except Exception as e:  # noqa: BLE001 -- best-effort cleanup
                logger.error(f"Error during manager shutdown: {e}")
        # Disconnect the service/FSM D-Bus connection BEFORE the GPIO reset.
        #
        # LOAD-BEARING ORDERING — do NOT move the GPIO reset above this.
        # The GPIO reset physically drops each modem off USB, which makes
        # ModemManager emit a "modem removed" signal.  If the service bus is
        # still connected, the FSM's still-subscribed MM signal handlers
        # re-enter on that removal ("transitioning to scanning", "Setting
        # interface DOWN", spawning fresh tasks/subprocesses) AFTER we have
        # already cancelled the pending tasks — orphan work that keeps the
        # process (and a child python3) alive until systemd SIGKILLs us at
        # TimeoutStopSec.  Tearing the bus down first means the removal has
        # no live handler to re-enter.  The GPIO reset needs no D-Bus (pure
        # local sysfs via the hardware API), so nothing below depends on it.
        if bus:
            try:
                bus.disconnect()
            except Exception as e:  # noqa: BLE001 -- best-effort cleanup
                logger.debug(f"Error disconnecting service bus: {e}")
        # On a real system reboot/shutdown the modems keep power across the
        # soft reboot and retain their internal state (e.g. a
        # failed/sim-missing latch that orderly disconnect cannot clear).
        # AFTER the orderly bearer disconnect and bus teardown above, force a
        # GPIO reset on every declared modem so each re-enumerates clean at
        # next boot.  Gated on the SIGTERM path AND systemd actually stopping
        # the system, so a plain `systemctl restart igos-wwan-manager`
        # (service restart, system staying up) does NOT power-cycle the modems.
        if stop_event.is_set() and system_is_stopping():
            logger.info(
                "System is stopping — GPIO-resetting all modems "
                "after orderly disconnect"
            )
            try:
                await asyncio.wait_for(hardware_reset_all_modems(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.error("Shutdown GPIO modem reset timed out after 8s")
            except Exception as e:  # noqa: BLE001 -- best-effort cleanup
                logger.error(f"Shutdown GPIO modem reset error: {e}")
            logger.info("WWAN Interface Manager stopped")
            # Hard exit on the confirmed system-stopping path.  The GPIO reset
            # runs modem_reset via asyncio.to_thread; wait_for() above bounds
            # the *await* but cannot cancel the underlying executor thread if
            # its subprocess wedges.  Returning here would then hand control to
            # asyncio.run(), whose shutdown_default_executor() joins that
            # thread with NO timeout — the exact wedge that made systemd wait
            # out TimeoutStopSec and SIGKILL us (leaving a lingering child).
            # The box is being torn down anyway, so bypass interpreter teardown
            # entirely and exit now.  flush stdio first so the final log line
            # is not lost.
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)
        elif stop_event.is_set():
            # Standalone `systemctl stop`/`restart igos-wwan-manager`: a
            # SIGTERM with the system staying UP (the reboot/poweroff branch
            # above already handled the system_is_stopping() case).  This
            # manager owns ModemManager's lifecycle — it is the only thing
            # that starts MM — so it must not leave MM orphaned behind it.
            # Stop MM now, AFTER the orderly bearer disconnect (which needs
            # MM alive) and the service-bus teardown (so MM's exit fires no
            # live FSM signal handler), mirroring the GPIO-reset ordering
            # rationale above.
            #
            # Gated on stop_event so a crash / `Restart=on-failure` — where
            # this finally runs with no signal delivered — leaves MM up for
            # the auto-restarted instance to reuse via
            # check_and_start_modemmanager().  On an explicit `restart` MM is
            # bounced together with us and re-established by the new instance;
            # that is consistent with this service owning MM's life.
            logger.info("Manager stopping — stopping ModemManager")
            try:
                await asyncio.wait_for(
                    ModemManagerMonitor.stop_modemmanager(), timeout=12.0)
            except asyncio.TimeoutError:
                logger.error("ModemManager stop timed out")
            except Exception as e:  # noqa: BLE001 -- best-effort cleanup
                logger.error(f"Error stopping ModemManager: {e}")
        logger.info("WWAN Interface Manager stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(0)
