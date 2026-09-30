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

# Documentation / Inspiration
# - https://tools.ietf.org/html/rfc3414#appendix-A.3
# - https://github.com/TheMysteriousX/SNMPv3-Hash-Generator

key_length = 1048576

def random(l):
    # os.urandom(8) returns 8 bytes of random data
    import os
    from binascii import hexlify
    return hexlify(os.urandom(l)).decode('utf-8')

def expand(s, l):
    """ repead input string (s) as long as we reach the desired length in bytes """
    from itertools import repeat
    reps = l // len(s) + 1 # approximation; worst case: overrun = l + len(s)
    return ''.join(list(repeat(s, reps)))[:l].encode('utf-8')

def _localize(hashfn, passphrase, engine):
    """ RFC 3414 A.2 password-to-key + engine localization for the given hash """
    tmp = expand(passphrase, key_length)
    hash = hashfn(tmp).digest()
    engine = bytearray.fromhex(engine)
    out = b''.join([hash, engine, hash])
    return hashfn(out).digest().hex()

def plaintext_to_md5(passphrase, engine):
    """ Convert input plaintext passphrase to MD5 hashed version usable by net-snmp """
    from hashlib import md5
    return _localize(md5, passphrase, engine)

def plaintext_to_sha1(passphrase, engine):
    """ Convert input plaintext passphrase to SHA1hashed version usable by net-snmp """
    from hashlib import sha1
    return _localize(sha1, passphrase, engine)

def plaintext_to_sha224(passphrase, engine):
    """ Convert input plaintext passphrase to SHA-224 hashed version usable by net-snmp """
    from hashlib import sha224
    return _localize(sha224, passphrase, engine)

def plaintext_to_sha256(passphrase, engine):
    """ Convert input plaintext passphrase to SHA-256 hashed version usable by net-snmp """
    from hashlib import sha256
    return _localize(sha256, passphrase, engine)

def plaintext_to_sha384(passphrase, engine):
    """ Convert input plaintext passphrase to SHA-384 hashed version usable by net-snmp """
    from hashlib import sha384
    return _localize(sha384, passphrase, engine)

def plaintext_to_sha512(passphrase, engine):
    """ Convert input plaintext passphrase to SHA-512 hashed version usable by net-snmp """
    from hashlib import sha512
    return _localize(sha512, passphrase, engine)
