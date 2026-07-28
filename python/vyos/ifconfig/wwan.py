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

from vyos.ifconfig.interface import Interface

@Interface.register
class WWANIf(Interface):
    definition = {
        **Interface.definition,
        **{
            'section': 'wwan',
            'prefixes': ['wwan', ],
            'eternal': 'wwan[0-9]+$',
        },
    }

    def remove(self):
        """
        Remove interface from config. Removing the interface deconfigures all
        assigned IP addresses.
        Example:
        >>> from vyos.ifconfig import WWANIf
        >>> i = WWANIf('wwan0')
        >>> i.remove()
        """

        if self.exists(self.ifname):
            # interface is placed in A/D state when removed from config! It
            # will remain visible for the operating system.
            self.set_admin_state('down')

        super().remove()

    def set_admin_state(self, state):
        # Honour the update()-scoped bring-up deferral: when the WWAN state
        # machine owns link bring-up we must NOT raise wwanN from conf-mode.
        # ModemManager sets the qmi_wwan data-format (raw_ip) during
        # (re)connect and the kernel rejects that change while the netdev is
        # running ("qmi_wwan ... Cannot change a running device"), so racing
        # MM's write here just logs an error. Admin-DOWN is always honoured;
        # an already-UP link is left as-is (suppressing a redundant 'up').
        if state == 'up' and getattr(self, '_defer_admin_up', False):
            return None
        return super().set_admin_state(state)

    # ── FSM-owned IPv6 posture guards ──────────────────────────────
    # When the WWAN state machine owns the interface it is the sole authority
    # for RA/SLAAC/DAD sysctls on wwanN (accept_ra / autoconf / accept_dad /
    # dad_transmits / addr_gen_mode — set in _harden_wwan_ipv6_sysctls). The
    # generic Interface.update() would otherwise write accept_dad and
    # dad_transmits from their XML defaultValues (both 1), plus accept_ra /
    # autoconf, on *every* commit — clobbering the FSM's values. These overrides
    # suppress exactly those writes while super().update() runs on the
    # FSM-managed path (_fsm_owns_ipv6_posture); stock behavior is unchanged
    # when the FSM is not managing the interface.
    def set_ipv6_accept_ra(self, accept_ra):
        if getattr(self, '_fsm_owns_ipv6_posture', False):
            return None
        return super().set_ipv6_accept_ra(accept_ra)

    def set_ipv6_autoconf(self, autoconf):
        if getattr(self, '_fsm_owns_ipv6_posture', False):
            return None
        return super().set_ipv6_autoconf(autoconf)

    def set_ipv6_dad_accept(self, dad):
        if getattr(self, '_fsm_owns_ipv6_posture', False):
            return None
        return super().set_ipv6_dad_accept(dad)

    def set_ipv6_dad_messages(self, dad):
        if getattr(self, '_fsm_owns_ipv6_posture', False):
            return None
        return super().set_ipv6_dad_messages(dad)

    def update(self, config, defer_admin_up=False):
        '''Perform interface setup for wwan

        When *defer_admin_up* is True the caller has determined that the WWAN
        state machine will raise the link itself (ensure_link_up_on_connect),
        which it does only AFTER ModemManager has negotiated the qmi_wwan
        data-format (raw_ip) on the down device. In that case suppress the
        admin-UP that the generic Interface.update() performs at its tail so
        conf-mode does not race MM's raw_ip write on (re)connect.
        '''
        self._defer_admin_up = bool(defer_admin_up)
        # Mark the FSM as sole owner of wwanN IPv6 posture for the duration of
        # super().update(): the overridden set_ipv6_* setters use this flag to
        # suppress the generic writes that would otherwise clobber the FSM's
        # accept_dad / dad_transmits / autoconf / accept_ra (including from XML
        # defaultValues) on every commit.
        self._fsm_owns_ipv6_posture = bool(defer_admin_up)
        try:
            super().update(config)
        finally:
            self._defer_admin_up = False
            self._fsm_owns_ipv6_posture = False
