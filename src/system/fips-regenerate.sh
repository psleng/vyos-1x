#!/bin/sh
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
# Regenerate the OpenSSL FIPS module config (/run/ssl/fipsmodule.cnf) early in
# boot, and fail closed if the module self-test fails.
#
# openssl.cnf '.include's fipsmodule.cnf when FIPS mode is enabled, but it lives
# in /run (tmpfs) and is wiped on every reboot. Without regenerating it before
# any OpenSSL consumer starts, OpenSSL config loading would fail system-wide.
#
# Fail-closed (controlled): if the module self-test fails, the system must not
# operate network crypto services in an unverified state. We runtime-mask those
# services (cleared on next reboot) and raise a loud, persistent failure state,
# while leaving console recovery (getty) intact.
#
# OPENSSL_CONF is pointed at /dev/null so that fipsinstall itself does not try
# to load the system openssl.cnf (which '.include's the not-yet-existing
# fipsmodule.cnf, i.e. a chicken-and-egg at boot).

OPENSSL_CNF=/etc/ssl/openssl.cnf
FIPSMODULE_CNF=/run/ssl/fipsmodule.cnf
FAIL_MARKER=/run/vyos-fips-selftest-failed
# Runtime (tmpfs) marker recording that this boot came up with FIPS active.
# Being in /run it is recreated on every boot, so op-mode can derive the
# pending-reboot state as (configured != booted) without any persistent state.
BOOTED_MARKER=/run/vyos-fips-booted

# Network-exposed crypto services disabled on self-test failure (console getty
# is intentionally left running for recovery).
CRYPTO_UNITS="ssh.service ssh@.service ssh-tpm-service.service strongswan.service openvpn@.service nginx.service vyos-http-api.service"

fail_closed() {
    reason="$1"
    printf '%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "${FAIL_MARKER}" 2>/dev/null

    msg="FIPS 140-3 MODULE SELF-TEST FAILED: ${reason}. The system is NOT operating in FIPS mode; network crypto services have been disabled. Recover via the console."
    logger -p auth.crit -t fips "${msg}" 2>/dev/null || true
    {
        echo ''
        echo '**************************************************************'
        echo "${msg}"
        echo '**************************************************************'
    } > /dev/console 2>/dev/null || true

    # Runtime-mask (and stop, if somehow already up) the crypto services so they
    # do not start this boot; the mask lives in /run and clears on reboot.
    for unit in ${CRYPTO_UNITS}; do
        systemctl mask --runtime --now "${unit}" >/dev/null 2>&1 || true
    done

    exit 1
}

# Clear any stale boot-state marker in case the service is re-run manually; it
# is (re)written below only if this boot actually brings FIPS up successfully.
rm -f "${BOOTED_MARKER}" 2>/dev/null || true

# Only act if the active OpenSSL config actually expects the FIPS module config;
# otherwise FIPS mode is not enabled and there is nothing to do.
if ! grep -q 'fipsmodule.cnf' "${OPENSSL_CNF}" 2>/dev/null; then
    exit 0
fi

# Locate the validated provider (arch-agnostic).
FIPS_SO=$(ls /usr/lib/*/ossl-modules/fips.so 2>/dev/null | head -n1)
if [ -z "${FIPS_SO}" ]; then
    fail_closed "validated provider module (fips.so) is missing"
fi

mkdir -p "$(dirname "${FIPSMODULE_CNF}")"

if ! OPENSSL_CONF=/dev/null openssl fipsinstall -module "${FIPS_SO}" -out "${FIPSMODULE_CNF}" >/dev/null 2>&1; then
    rm -f "${FIPSMODULE_CNF}" 2>/dev/null || true
    fail_closed "fipsinstall self-test did not pass for ${FIPS_SO}"
fi

# Success: clear any stale failure marker from a previous boot attempt, and
# record that this boot is operating with FIPS active.
rm -f "${FAIL_MARKER}" 2>/dev/null || true
: > "${BOOTED_MARKER}" 2>/dev/null || true
exit 0
