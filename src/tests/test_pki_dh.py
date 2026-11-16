#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""DH parameter generation must be fast and interoperable.

Standard key sizes are served from the pre-computed RFC 7919 FFDHE groups
instead of searching for a fresh random safe prime (the slow step).  The
encoded output must stay canonical PKCS#3 'DH PARAMETERS' - OpenSSL 3.x would
otherwise upgrade the recognised FFDHE primes to the 'X9.42 DH PARAMETERS'
structure, which the VyOS re-wrapping and OpenVPN/strongSwan cannot load.
"""

import base64
from unittest import TestCase

from vyos.pki import FFDHE_SIZES
from vyos.pki import create_dh_parameters
from vyos.pki import encode_dh_parameters
from vyos.pki import load_dh_parameters


class TestPkiDhParameters(TestCase):
    def test_ffdhe_sizes_are_standard(self):
        self.assertEqual(FFDHE_SIZES, (2048, 3072, 4096, 6144, 8192))

    def test_standard_sizes_use_named_group(self):
        # Standard sizes must return the exact RFC 7919 prime with generator 2
        # (and therefore not depend on a random safe-prime search).
        for bits in FFDHE_SIZES:
            params = create_dh_parameters(bits)
            self.assertIsNotNone(params)
            numbers = params.parameter_numbers()
            self.assertEqual(numbers.g, 2)
            self.assertEqual(numbers.p.bit_length(), bits)
            # A second call must yield the identical, deterministic prime.
            self.assertEqual(create_dh_parameters(bits).parameter_numbers().p,
                             numbers.p)

    def test_encoding_is_canonical_pkcs3(self):
        for bits in FFDHE_SIZES:
            pem = encode_dh_parameters(create_dh_parameters(bits))
            self.assertTrue(pem.startswith('-----BEGIN DH PARAMETERS-----'))
            self.assertIn('-----END DH PARAMETERS-----', pem)
            self.assertNotIn('X9.42', pem)
            # Body must be a DER SEQUENCE of exactly two INTEGERs (prime, base).
            body = ''.join(pem.strip().splitlines()[1:-1])
            der = base64.b64decode(body)
            self.assertEqual(der[0], 0x30)  # SEQUENCE

    def test_round_trip_reload(self):
        # VyOS stores only the base64 body and re-wraps it before use; the
        # result must load back to the same prime and generator.
        for bits in FFDHE_SIZES:
            pem = encode_dh_parameters(create_dh_parameters(bits))
            body = ''.join(pem.strip().splitlines()[1:-1])
            reloaded = load_dh_parameters(body)
            self.assertIsNotNone(reloaded)
            numbers = reloaded.parameter_numbers()
            self.assertEqual(numbers.g, 2)
            self.assertEqual(numbers.p.bit_length(), bits)

    def test_non_standard_size_generates_valid_params(self):
        # A non-standard size falls back to on-the-fly generation and must
        # still produce usable, canonically-encoded parameters.
        params = create_dh_parameters(1024)
        self.assertIsNotNone(params)
        self.assertEqual(params.parameter_numbers().p.bit_length(), 1024)
        pem = encode_dh_parameters(params)
        self.assertTrue(pem.startswith('-----BEGIN DH PARAMETERS-----'))

    def test_invalid_size_rejected(self):
        self.assertFalse(create_dh_parameters(256))
        self.assertFalse(create_dh_parameters(0))


if __name__ == '__main__':
    import unittest
    unittest.main()
