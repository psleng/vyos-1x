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
    # Never leave a module config behind for a module that was not verified.
    rm -f "${FIPSMODULE_CNF}" 2>/dev/null || true
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

# Locate the validated provider and its identity manifest (arch-agnostic). The
# manifest is shipped by igos-openssl-fips-provider; OpenSSL loads whatever is
# at ossl-modules/fips.so, so prove that file is the validated build before
# self-testing it.
MANIFEST=$(ls /usr/lib/*/igos-openssl-fips-provider/fips.so.manifest 2>/dev/null | head -n1)
if [ -z "${MANIFEST}" ]; then
    fail_closed "validated provider identity manifest is missing"
fi
FIPS_SO="$(dirname "$(dirname "${MANIFEST}")")/ossl-modules/fips.so"
if [ ! -f "${FIPS_SO}" ]; then
    fail_closed "validated provider module (fips.so) is missing"
fi
EXPECTED_SHA=$(sed -n 's/^sha256=//p' "${MANIFEST}")
EXPECTED_VERSION=$(sed -n 's/^version=//p' "${MANIFEST}")
ACTUAL_SHA=$(sha256sum "${FIPS_SO}" | cut -d' ' -f1)
if [ -z "${EXPECTED_SHA}" ] || [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
    fail_closed "${FIPS_SO} is not the validated provider build (sha256 ${ACTUAL_SHA}, expected ${EXPECTED_SHA:-<none>})"
fi

mkdir -p "$(dirname "${FIPSMODULE_CNF}")"

if ! OPENSSL_CONF=/dev/null openssl fipsinstall -module "${FIPS_SO}" -out "${FIPSMODULE_CNF}" >/dev/null 2>&1; then
    fail_closed "fipsinstall self-test did not pass for ${FIPS_SO}"
fi

# Confirm the provider OpenSSL actually resolves through the same activation
# openssl.cnf uses is the validated version (not merely a file that self-tests).
CHECK_CNF=$(mktemp /run/ssl/fips-check.XXXXXX)
cat > "${CHECK_CNF}" <<EOF
openssl_conf = openssl_init
.include ${FIPSMODULE_CNF}
[openssl_init]
providers = provider_sect
[provider_sect]
fips = fips_sect
EOF
LOADED_VERSION=$(OPENSSL_CONF="${CHECK_CNF}" openssl list -providers 2>/dev/null \
    | awk '/^  fips$/ {f = 1; next} f && /^  [^ ]/ {f = 0} f && $1 == "version:" {print $2; exit}')
rm -f "${CHECK_CNF}"
if [ -z "${EXPECTED_VERSION}" ] || [ "${LOADED_VERSION}" != "${EXPECTED_VERSION}" ]; then
    fail_closed "loaded FIPS provider reports version ${LOADED_VERSION:-<not loaded>}, expected validated ${EXPECTED_VERSION:-<none>}"
fi

# Success: clear any stale failure marker from a previous boot attempt, and
# record that this boot is operating with FIPS active.
rm -f "${FAIL_MARKER}" 2>/dev/null || true
: > "${BOOTED_MARKER}" 2>/dev/null || true
exit 0
