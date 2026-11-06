# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
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

import base64
import contextlib
import datetime
import ipaddress
import os
import subprocess
import textwrap

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.x509.extensions import ExtensionNotFound
from cryptography.x509.oid import ExtensionOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives.asymmetric import dsa
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric import rsa

CERT_BEGIN='-----BEGIN CERTIFICATE-----\n'
CERT_END='\n-----END CERTIFICATE-----'
KEY_BEGIN='-----BEGIN PRIVATE KEY-----\n'
KEY_END='\n-----END PRIVATE KEY-----'
KEY_EC_BEGIN='-----BEGIN EC PRIVATE KEY-----\n'
KEY_EC_END='\n-----END EC PRIVATE KEY-----'
KEY_ENC_BEGIN='-----BEGIN ENCRYPTED PRIVATE KEY-----\n'
KEY_ENC_END='\n-----END ENCRYPTED PRIVATE KEY-----'
KEY_PUB_BEGIN='-----BEGIN PUBLIC KEY-----\n'
KEY_PUB_END='\n-----END PUBLIC KEY-----'
CRL_BEGIN='-----BEGIN X509 CRL-----\n'
CRL_END='\n-----END X509 CRL-----'
CSR_BEGIN='-----BEGIN CERTIFICATE REQUEST-----\n'
CSR_END='\n-----END CERTIFICATE REQUEST-----'
DH_BEGIN='-----BEGIN DH PARAMETERS-----\n'
DH_END='\n-----END DH PARAMETERS-----'
OVPN_BEGIN = '-----BEGIN OpenVPN Static key V{0}-----\n'
OVPN_END = '\n-----END OpenVPN Static key V{0}-----'
OPENSSH_KEY_BEGIN='-----BEGIN OPENSSH PRIVATE KEY-----\n'
OPENSSH_KEY_END='\n-----END OPENSSH PRIVATE KEY-----'

# Print functions

encoding_map = {
    'PEM': serialization.Encoding.PEM,
    'OpenSSH': serialization.Encoding.OpenSSH
}

public_format_map = {
    'SubjectPublicKeyInfo': serialization.PublicFormat.SubjectPublicKeyInfo,
    'OpenSSH': serialization.PublicFormat.OpenSSH
}

private_format_map = {
    'PKCS8': serialization.PrivateFormat.PKCS8,
    'OpenSSH': serialization.PrivateFormat.OpenSSH
}

hash_map = {
    'sha256': hashes.SHA256,
    'sha384': hashes.SHA384,
    'sha512': hashes.SHA512,
}

def get_certificate_fingerprint(cert, hash):
    hash_algorithm = hash_map[hash]()
    fp = cert.fingerprint(hash_algorithm)

    return fp.hex(':').upper()

def encode_certificate(cert):
    return cert.public_bytes(encoding=serialization.Encoding.PEM).decode('utf-8')

def encode_public_key(cert, encoding='PEM', key_format='SubjectPublicKeyInfo'):
    if encoding not in encoding_map:
        encoding = 'PEM'
    if key_format not in public_format_map:
        key_format = 'SubjectPublicKeyInfo'
    return cert.public_bytes(
        encoding=encoding_map[encoding],
        format=public_format_map[key_format]).decode('utf-8')

def encode_private_key(private_key, encoding='PEM', key_format='PKCS8', passphrase=None):
    if encoding not in encoding_map:
        encoding = 'PEM'
    if key_format not in private_format_map:
        key_format = 'PKCS8'
    encryption = serialization.NoEncryption() if not passphrase else serialization.BestAvailableEncryption(bytes(passphrase, 'utf-8'))
    return private_key.private_bytes(
        encoding=encoding_map[encoding],
        format=private_format_map[key_format],
        encryption_algorithm=encryption).decode('utf-8')

def _encode_der_length(length):
    # Minimal ASN.1 DER length octets (short form < 128, else long form).
    if length < 0x80:
        return bytes([length])
    encoded = b''
    while length:
        encoded = bytes([length & 0xFF]) + encoded
        length >>= 8
    return bytes([0x80 | len(encoded)]) + encoded

def _encode_der_integer(value):
    # Minimal ASN.1 DER encoding of a non-negative INTEGER.
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, 'big')
    if raw[0] & 0x80:
        # Prepend a zero byte so the value is not misread as negative.
        raw = b'\x00' + raw
    return b'\x02' + _encode_der_length(len(raw)) + raw

def encode_dh_parameters(dh_parameters):
    # Emit canonical PKCS#3 "DH PARAMETERS" (an ASN.1 SEQUENCE of just the
    # prime and the generator).  We encode it ourselves instead of relying on
    # ``parameter_bytes(ParameterFormat.PKCS3)`` because OpenSSL 3.x recognises
    # the RFC 7919 (ffdheXXXX) primes as named groups and silently upgrades the
    # output to the "X9.42 DH PARAMETERS" structure (which also carries the
    # subgroup order ``q``).  VyOS stores only the base64 body and re-wraps it
    # with a PKCS#3 header, and consumers such as OpenVPN/strongSwan read it
    # back with ``PEM_read_bio_DHparams`` which only understands PKCS#3 — so an
    # X9.42 body wrapped in a PKCS#3 header would fail to load.  For primes that
    # are not a named group (freshly generated parameters) this produces output
    # byte-for-byte identical to the previous implementation.
    numbers = dh_parameters.parameter_numbers()
    body = _encode_der_integer(numbers.p) + _encode_der_integer(numbers.g)
    der = b'\x30' + _encode_der_length(len(body)) + body
    b64 = '\n'.join(textwrap.wrap(base64.b64encode(der).decode('ascii'), 64))
    return f'{DH_BEGIN}{b64}{DH_END}\n'

# EC Helper

def get_elliptic_curve(size):
    curve_func = None
    name = f'SECP{size}R1'
    if hasattr(ec, name):
        curve_func = getattr(ec, name)
    else:
        curve_func = ec.SECP256R1() # Default to SECP256R1
    return curve_func()

# Creation functions

# OpenSSL curve names for the EC key sizes we support. OpenSSL rejects the RFC
# 'secp256r1' spelling for P-256 (it expects 'prime256v1'), so map explicitly.
_EC_SIZE_TO_OPENSSL_CURVE = {
    224: 'secp224r1',
    256: 'prime256v1',
    384: 'secp384r1',
    521: 'secp521r1',
}

def _run_openssl(args, stdin=None, pass_fds=()):
    """Run an ``openssl`` subcommand and return its stdout (PEM) bytes.

    Cryptographic work runs in the system OpenSSL process, so when FIPS mode is
    active it is performed by the validated FIPS provider (and a non-approved
    request fails closed inside the module). pyca/``cryptography`` is
    deliberately avoided for generation/signing because it self-loads the
    ``default`` provider and never routes through ``fips.so`` (see
    igos-fips-140-3-design.md). Results are parsed back into ``cryptography``
    objects for callers; parsing existing material is not FIPS-critical.
    """
    proc = subprocess.run(
        ['openssl', *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=tuple(pass_fds),
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError(
            f"openssl {args[0] if args else ''} failed: "
            + proc.stderr.decode(errors='replace').strip()
        )
    return proc.stdout

@contextlib.contextmanager
def _fd_inputs(inputs):
    """Expose byte blobs to an ``openssl`` child as anonymous, RAM-only,
    seekable ``memfd`` file descriptors -- no filesystem path ever exists.

    ``inputs`` maps a label to ``bytes``/``str``. Yields ``(paths, fds)`` where
    ``paths[label]`` is the ``/proc/self/fd/N`` string to hand to openssl and
    ``fds`` is the list to pass via ``pass_fds`` (which clears close-on-exec for
    the child while keeping the fd numbers stable). This is how the CA private
    key reaches openssl for signing without touching disk -- see the "Private
    key on disk" subsection of igos-fips-140-3-design.md.
    """
    fds = []
    paths = {}
    try:
        for label, data in inputs.items():
            if isinstance(data, str):
                data = data.encode()
            fd = os.memfd_create(label, os.MFD_CLOEXEC)
            os.write(fd, data)
            os.lseek(fd, 0, os.SEEK_SET)
            fds.append(fd)
            paths[label] = f'/proc/self/fd/{fd}'
        yield paths, fds
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)

def create_private_key(key_type, key_size=None):
    if key_type == 'rsa':
        pem = _run_openssl(
            ['genpkey', '-algorithm', 'RSA',
             '-pkeyopt', f'rsa_keygen_bits:{int(key_size)}']
        )
    elif key_type == 'dsa':
        # DSA needs parameters first; stream them to the key step over stdin so
        # nothing lands on disk. DSA is non-approved and is gated out under FIPS.
        params = _run_openssl(
            ['genpkey', '-genparam', '-algorithm', 'DSA',
             '-pkeyopt', f'dsa_paramgen_bits:{int(key_size)}']
        )
        pem = _run_openssl(['genpkey', '-paramfile', '/dev/stdin'], stdin=params)
    elif key_type == 'ec':
        curve = _EC_SIZE_TO_OPENSSL_CURVE.get(int(key_size) if key_size else 256,
                                              'prime256v1')
        pem = _run_openssl(
            ['genpkey', '-algorithm', 'EC', '-pkeyopt', f'ec_paramgen_curve:{curve}']
        )
    else:
        return None

    return serialization.load_pem_private_key(pem, password=None)

def _openssl_subject_arg(subject):
    """Build an ``openssl`` ``-subj`` string, escaping the DN metacharacters."""
    def esc(value):
        return (str(value).replace('\\', '\\\\')
                .replace('/', '\\/').replace('+', '\\+'))

    return (f"/C={esc(subject['country'])}/ST={esc(subject['state'])}"
            f"/L={esc(subject['locality'])}/O={esc(subject['organization'])}"
            f"/CN={esc(subject['common_name'])}")

def _san_openssl_value(subject_alt_names):
    """Render a ``subjectAltName`` value from mixed IP/str alt-name objects."""
    parts = []
    for obj in subject_alt_names:
        if isinstance(obj, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            parts.append(f'IP:{obj}')
        elif isinstance(obj, str):
            parts.append(f'email:{obj}' if '@' in obj else f'DNS:{obj}')
    return ','.join(parts)

def _san_from_request(cert_req):
    """Re-render the SAN of a loaded CSR as an openssl ``subjectAltName`` value.

    The CSRs produced here only ever carry a (non-critical) SAN, mirroring the
    previous pyca behaviour of copying the request's extensions into the cert.
    """
    try:
        ext = cert_req.extensions.get_extension_for_class(
            x509.SubjectAlternativeName)
    except ExtensionNotFound:
        return ''
    parts = []
    for gn in ext.value:
        if isinstance(gn, x509.DNSName):
            parts.append(f'DNS:{gn.value}')
        elif isinstance(gn, x509.RFC822Name):
            parts.append(f'email:{gn.value}')
        elif isinstance(gn, x509.IPAddress):
            parts.append(f'IP:{gn.value}')
    return ','.join(parts)

def create_certificate_request(subject, private_key, subject_alt_names=[]):
    key_pem = encode_private_key(private_key)

    args = ['req', '-new', '-sha256', '-subj', _openssl_subject_arg(subject)]
    san = _san_openssl_value(subject_alt_names) if subject_alt_names else ''
    if san:
        args += ['-addext', f'subjectAltName={san}']

    with _fd_inputs({'key': key_pem}) as (paths, fds):
        pem = _run_openssl(args + ['-key', paths['key']], pass_fds=fds)

    return load_certificate_request(pem.decode(), wrap_tags=False)

def _certificate_v3_section(cert_type, is_ca, is_sub_ca, san_value):
    """Render the openssl x509 v3 extension section, matching the prior pyca set.

    * BasicConstraints (critical): CA:TRUE[,pathlen:0] for a sub-CA, else CA:FALSE.
    * KeyUsage (critical): digitalSignature always; keyCertSign+cRLSign for a CA.
    * ExtendedKeyUsage: client/serverAuth per role (both for a CA).
    * subjectKeyIdentifier=hash; authorityKeyIdentifier=keyid for non-root certs.
    * subjectAltName copied from the request.
    """
    lines = ['[v3]']

    basic = 'critical,CA:TRUE' if is_ca else 'critical,CA:FALSE'
    if is_ca and is_sub_ca:
        basic += ',pathlen:0'
    lines.append(f'basicConstraints={basic}')

    key_usage = 'critical,digitalSignature'
    if is_ca:
        key_usage += ',keyCertSign,cRLSign'
    lines.append(f'keyUsage={key_usage}')

    if is_ca:
        ext_key_usage = 'clientAuth,serverAuth'
    elif cert_type == 'client':
        ext_key_usage = 'clientAuth'
    elif cert_type == 'server':
        ext_key_usage = 'serverAuth'
    else:
        ext_key_usage = ''
    if ext_key_usage:
        lines.append(f'extendedKeyUsage={ext_key_usage}')

    lines.append('subjectKeyIdentifier=hash')

    if not is_ca or is_sub_ca:
        lines.append('authorityKeyIdentifier=keyid:always')

    if san_value:
        lines.append(f'subjectAltName={san_value}')

    return '\n'.join(lines) + '\n'

def create_certificate(cert_req, ca_cert, ca_private_key, valid_days=365, cert_type='server', is_ca=False, is_sub_ca=False):
    # The self-signed cases pass the request itself as ``ca_cert`` (issuer ==
    # subject); a CA-signed cert passes a real certificate.
    self_signed = isinstance(ca_cert, x509.CertificateSigningRequest)

    ext_section = _certificate_v3_section(
        cert_type, is_ca, is_sub_ca, _san_from_request(cert_req))

    inputs = {
        'csr': cert_req.public_bytes(serialization.Encoding.PEM),
        'cakey': encode_private_key(ca_private_key),
        'ext': ext_section,
    }
    if not self_signed:
        inputs['cacert'] = encode_certificate(ca_cert)

    args = ['x509', '-req', '-sha256', '-days', str(int(valid_days)),
            '-set_serial', str(x509.random_serial_number())]

    with _fd_inputs(inputs) as (paths, fds):
        args += ['-in', paths['csr'],
                 '-extfile', paths['ext'], '-extensions', 'v3']
        if self_signed:
            args += ['-signkey', paths['cakey']]
        else:
            args += ['-CA', paths['cacert'], '-CAkey', paths['cakey']]
        pem = _run_openssl(args, pass_fds=fds)

    return load_certificate(pem.decode(), wrap_tags=False)

def _index_txt_serial(serial_number):
    """Serial as openssl's index.txt expects it: uppercase, even-length hex."""
    value = format(serial_number, 'X')
    return value if len(value) % 2 == 0 else '0' + value

def create_certificate_revocation_list(ca_cert, ca_private_key, serial_numbers=[]):
    if not serial_numbers:
        return False

    now = datetime.datetime.now(datetime.timezone.utc)
    revoked = now.strftime('%y%m%d%H%M%SZ')
    # index.txt needs a (cosmetic) expiry per row; it is not emitted in the CRL.
    expiry = (now + datetime.timedelta(days=3650)).strftime('%y%m%d%H%M%SZ')
    index = ''.join(
        f"R\t{expiry}\t{revoked}\t{_index_txt_serial(sn)}\tunknown\t/CN=revoked\n"
        for sn in serial_numbers
    )

    inputs = {
        'cakey': encode_private_key(ca_private_key),
        'cacert': encode_certificate(ca_cert),
        'index': index,
    }
    with _fd_inputs(inputs) as (paths, fds):
        config = (
            '[ca]\n'
            'default_ca = CA_default\n'
            '[CA_default]\n'
            f"database = {paths['index']}\n"
            f"certificate = {paths['cacert']}\n"
            f"private_key = {paths['cakey']}\n"
            'default_md = sha256\n'
            'default_crl_days = 1\n'
        )
        with _fd_inputs({'config': config}) as (cfg_paths, cfg_fds):
            pem = _run_openssl(
                ['ca', '-config', cfg_paths['config'], '-gencrl'],
                pass_fds=fds + cfg_fds,
            )

    return load_crl(pem.decode(), wrap_tags=False)

# RFC 7919 finite-field Diffie-Hellman (FFDHE) group primes, all with
# generator g=2.  These are standardised, publicly vetted safe primes, so they
# can be used verbatim instead of searching for a fresh random safe prime —
# which is the expensive, multi-second-to-multi-minute step in DH parameter
# "generation".  Verified byte-for-byte against OpenSSL's named groups and
# RFC 7919 Appendix A.
_FFDHE_GENERATOR = 2
_RFC7919_FFDHE_PRIMES = {
    2048: int(
        'ffffffffffffffffadf85458a2bb4a9aafdc5620273d3cf1d8b9c583ce2d'
        '3695a9e13641146433fbcc939dce249b3ef97d2fe363630c75d8f681b202'
        'aec4617ad3df1ed5d5fd65612433f51f5f066ed0856365553ded1af3b557'
        '135e7f57c935984f0c70e0e68b77e2a689daf3efe8721df158a136ade735'
        '30acca4f483a797abc0ab182b324fb61d108a94bb2c8e3fbb96adab760d7'
        'f4681d4f42a3de394df4ae56ede76372bb190b07a7c8ee0a6d709e02fce1'
        'cdf7e2ecc03404cd28342f619172fe9ce98583ff8e4f1232eef28183c3fe'
        '3b1b4c6fad733bb5fcbc2ec22005c58ef1837d1683b2c6f34a26c1b2effa'
        '886b423861285c97ffffffffffffffff',
        16),
    3072: int(
        'ffffffffffffffffadf85458a2bb4a9aafdc5620273d3cf1d8b9c583ce2d'
        '3695a9e13641146433fbcc939dce249b3ef97d2fe363630c75d8f681b202'
        'aec4617ad3df1ed5d5fd65612433f51f5f066ed0856365553ded1af3b557'
        '135e7f57c935984f0c70e0e68b77e2a689daf3efe8721df158a136ade735'
        '30acca4f483a797abc0ab182b324fb61d108a94bb2c8e3fbb96adab760d7'
        'f4681d4f42a3de394df4ae56ede76372bb190b07a7c8ee0a6d709e02fce1'
        'cdf7e2ecc03404cd28342f619172fe9ce98583ff8e4f1232eef28183c3fe'
        '3b1b4c6fad733bb5fcbc2ec22005c58ef1837d1683b2c6f34a26c1b2effa'
        '886b4238611fcfdcde355b3b6519035bbc34f4def99c023861b46fc9d6e6'
        'c9077ad91d2691f7f7ee598cb0fac186d91caefe130985139270b4130c93'
        'bc437944f4fd4452e2d74dd364f2e21e71f54bff5cae82ab9c9df69ee86d'
        '2bc522363a0dabc521979b0deada1dbf9a42d5c4484e0abcd06bfa53ddef'
        '3c1b20ee3fd59d7c25e41d2b66c62e37ffffffffffffffff',
        16),
    4096: int(
        'ffffffffffffffffadf85458a2bb4a9aafdc5620273d3cf1d8b9c583ce2d'
        '3695a9e13641146433fbcc939dce249b3ef97d2fe363630c75d8f681b202'
        'aec4617ad3df1ed5d5fd65612433f51f5f066ed0856365553ded1af3b557'
        '135e7f57c935984f0c70e0e68b77e2a689daf3efe8721df158a136ade735'
        '30acca4f483a797abc0ab182b324fb61d108a94bb2c8e3fbb96adab760d7'
        'f4681d4f42a3de394df4ae56ede76372bb190b07a7c8ee0a6d709e02fce1'
        'cdf7e2ecc03404cd28342f619172fe9ce98583ff8e4f1232eef28183c3fe'
        '3b1b4c6fad733bb5fcbc2ec22005c58ef1837d1683b2c6f34a26c1b2effa'
        '886b4238611fcfdcde355b3b6519035bbc34f4def99c023861b46fc9d6e6'
        'c9077ad91d2691f7f7ee598cb0fac186d91caefe130985139270b4130c93'
        'bc437944f4fd4452e2d74dd364f2e21e71f54bff5cae82ab9c9df69ee86d'
        '2bc522363a0dabc521979b0deada1dbf9a42d5c4484e0abcd06bfa53ddef'
        '3c1b20ee3fd59d7c25e41d2b669e1ef16e6f52c3164df4fb7930e9e4e588'
        '57b6ac7d5f42d69f6d187763cf1d5503400487f55ba57e31cc7a7135c886'
        'efb4318aed6a1e012d9e6832a907600a918130c46dc778f971ad00380929'
        '99a333cb8b7a1a1db93d7140003c2a4ecea9f98d0acc0a8291cdcec97dcf'
        '8ec9b55a7f88a46b4db5a851f44182e1c68a007e5e655f6affffffffffff'
        'ffff',
        16),
    6144: int(
        'ffffffffffffffffadf85458a2bb4a9aafdc5620273d3cf1d8b9c583ce2d'
        '3695a9e13641146433fbcc939dce249b3ef97d2fe363630c75d8f681b202'
        'aec4617ad3df1ed5d5fd65612433f51f5f066ed0856365553ded1af3b557'
        '135e7f57c935984f0c70e0e68b77e2a689daf3efe8721df158a136ade735'
        '30acca4f483a797abc0ab182b324fb61d108a94bb2c8e3fbb96adab760d7'
        'f4681d4f42a3de394df4ae56ede76372bb190b07a7c8ee0a6d709e02fce1'
        'cdf7e2ecc03404cd28342f619172fe9ce98583ff8e4f1232eef28183c3fe'
        '3b1b4c6fad733bb5fcbc2ec22005c58ef1837d1683b2c6f34a26c1b2effa'
        '886b4238611fcfdcde355b3b6519035bbc34f4def99c023861b46fc9d6e6'
        'c9077ad91d2691f7f7ee598cb0fac186d91caefe130985139270b4130c93'
        'bc437944f4fd4452e2d74dd364f2e21e71f54bff5cae82ab9c9df69ee86d'
        '2bc522363a0dabc521979b0deada1dbf9a42d5c4484e0abcd06bfa53ddef'
        '3c1b20ee3fd59d7c25e41d2b669e1ef16e6f52c3164df4fb7930e9e4e588'
        '57b6ac7d5f42d69f6d187763cf1d5503400487f55ba57e31cc7a7135c886'
        'efb4318aed6a1e012d9e6832a907600a918130c46dc778f971ad00380929'
        '99a333cb8b7a1a1db93d7140003c2a4ecea9f98d0acc0a8291cdcec97dcf'
        '8ec9b55a7f88a46b4db5a851f44182e1c68a007e5e0dd9020bfd64b64503'
        '6c7a4e677d2c38532a3a23ba4442caf53ea63bb454329b7624c8917bdd64'
        'b1c0fd4cb38e8c334c701c3acdad0657fccfec719b1f5c3e4e46041f3881'
        '47fb4cfdb477a52471f7a9a96910b855322edb6340d8a00ef092350511e3'
        '0abec1fff9e3a26e7fb29f8c183023c3587e38da0077d9b4763e4e4b94b2'
        'bbc194c6651e77caf992eeaac0232a281bf6b3a739c1226116820ae8db58'
        '47a67cbef9c9091b462d538cd72b03746ae77f5e62292c311562a846505d'
        'c82db854338ae49f5235c95b91178ccf2dd5cacef403ec9d1810c6272b04'
        '5b3b71f9dc6b80d63fdd4a8e9adb1e6962a69526d43161c1a41d570d7938'
        'dad4a40e329cd0e40e65ffffffffffffffff',
        16),
    8192: int(
        'ffffffffffffffffadf85458a2bb4a9aafdc5620273d3cf1d8b9c583ce2d'
        '3695a9e13641146433fbcc939dce249b3ef97d2fe363630c75d8f681b202'
        'aec4617ad3df1ed5d5fd65612433f51f5f066ed0856365553ded1af3b557'
        '135e7f57c935984f0c70e0e68b77e2a689daf3efe8721df158a136ade735'
        '30acca4f483a797abc0ab182b324fb61d108a94bb2c8e3fbb96adab760d7'
        'f4681d4f42a3de394df4ae56ede76372bb190b07a7c8ee0a6d709e02fce1'
        'cdf7e2ecc03404cd28342f619172fe9ce98583ff8e4f1232eef28183c3fe'
        '3b1b4c6fad733bb5fcbc2ec22005c58ef1837d1683b2c6f34a26c1b2effa'
        '886b4238611fcfdcde355b3b6519035bbc34f4def99c023861b46fc9d6e6'
        'c9077ad91d2691f7f7ee598cb0fac186d91caefe130985139270b4130c93'
        'bc437944f4fd4452e2d74dd364f2e21e71f54bff5cae82ab9c9df69ee86d'
        '2bc522363a0dabc521979b0deada1dbf9a42d5c4484e0abcd06bfa53ddef'
        '3c1b20ee3fd59d7c25e41d2b669e1ef16e6f52c3164df4fb7930e9e4e588'
        '57b6ac7d5f42d69f6d187763cf1d5503400487f55ba57e31cc7a7135c886'
        'efb4318aed6a1e012d9e6832a907600a918130c46dc778f971ad00380929'
        '99a333cb8b7a1a1db93d7140003c2a4ecea9f98d0acc0a8291cdcec97dcf'
        '8ec9b55a7f88a46b4db5a851f44182e1c68a007e5e0dd9020bfd64b64503'
        '6c7a4e677d2c38532a3a23ba4442caf53ea63bb454329b7624c8917bdd64'
        'b1c0fd4cb38e8c334c701c3acdad0657fccfec719b1f5c3e4e46041f3881'
        '47fb4cfdb477a52471f7a9a96910b855322edb6340d8a00ef092350511e3'
        '0abec1fff9e3a26e7fb29f8c183023c3587e38da0077d9b4763e4e4b94b2'
        'bbc194c6651e77caf992eeaac0232a281bf6b3a739c1226116820ae8db58'
        '47a67cbef9c9091b462d538cd72b03746ae77f5e62292c311562a846505d'
        'c82db854338ae49f5235c95b91178ccf2dd5cacef403ec9d1810c6272b04'
        '5b3b71f9dc6b80d63fdd4a8e9adb1e6962a69526d43161c1a41d570d7938'
        'dad4a40e329ccff46aaa36ad004cf600c8381e425a31d951ae64fdb23fce'
        'c9509d43687feb69edd1cc5e0b8cc3bdf64b10ef86b63142a3ab8829555b'
        '2f747c932665cb2c0f1cc01bd70229388839d2af05e454504ac78b758282'
        '2846c0ba35c35f5c59160cc046fd8251541fc68c9c86b022bb7099876a46'
        '0e7451a8a93109703fee1c217e6c3826e52c51aa691e0e423cfc99e9e316'
        '50c1217b624816cdad9a95f9d5b8019488d9c0a0a1fe3075a577e23183f8'
        '1d4a3f2fa4571efc8ce0ba8a4fe8b6855dfe72b0a66eded2fbabfbe58a30'
        'fafabe1c5d71a87e2f741ef8c1fe86fea6bbfde530677f0d97d11d49f7a8'
        '443d0822e506a9f4614e011e2a94838ff88cd68c8bb7c5c6424cffffffff'
        'ffffffff',
        16),
}

# Public tuple of DH key sizes served instantly from the RFC 7919 FFDHE table.
FFDHE_SIZES = tuple(sorted(_RFC7919_FFDHE_PRIMES))

def create_dh_parameters(bits=2048):
    if not bits or bits < 512:
        print("Invalid DH parameter key size")
        return False

    bits = int(bits)

    # Standard key sizes map onto the RFC 7919 FFDHE groups: these are
    # pre-computed, vetted safe primes, so we can return them instantly instead
    # of hunting for a fresh random safe prime (the slow part that made
    # 'generate pki dh' take seconds-to-minutes, especially on router hardware).
    # Non-standard sizes fall back to on-the-fly generation.
    ffdhe_prime = _RFC7919_FFDHE_PRIMES.get(bits)
    if ffdhe_prime is not None:
        return dh.DHParameterNumbers(ffdhe_prime, _FFDHE_GENERATOR).parameters()

    return dh.generate_parameters(generator=2, key_size=bits)

# Wrap functions

def wrap_public_key(raw_data):
    return KEY_PUB_BEGIN + raw_data + KEY_PUB_END

def wrap_private_key(raw_data, passphrase=None, ec=False):
    begin = KEY_BEGIN
    end = KEY_END

    if passphrase:
        begin = KEY_ENC_BEGIN
        end = KEY_ENC_END
    elif ec:
        begin = KEY_EC_BEGIN
        end = KEY_EC_END

    return begin + raw_data + end

def wrap_openssh_public_key(raw_data, type):
    return f'{type} {raw_data}'

def wrap_openssh_private_key(raw_data):
    return OPENSSH_KEY_BEGIN + raw_data +  OPENSSH_KEY_END

def wrap_certificate_request(raw_data):
    return CSR_BEGIN + raw_data + CSR_END

def wrap_certificate(raw_data):
    return CERT_BEGIN + raw_data + CERT_END

def wrap_crl(raw_data):
    return CRL_BEGIN + raw_data + CRL_END

def wrap_dh_parameters(raw_data):
    return DH_BEGIN + raw_data + DH_END

def wrap_openvpn_key(raw_data, version='1'):
    return OVPN_BEGIN.format(version) + raw_data + OVPN_END.format(version)

# Load functions
def load_public_key(raw_data, wrap_tags=True):
    if wrap_tags:
        raw_data = wrap_public_key(raw_data)

    try:
        return serialization.load_pem_public_key(bytes(raw_data, 'utf-8'))
    except ValueError:
        return False

def _load_private_key(raw_data, passphrase):
    try:
        return serialization.load_pem_private_key(bytes(raw_data, 'utf-8'), password=passphrase)
    except (ValueError, TypeError):
        return False

def load_private_key(raw_data, passphrase=None, wrap_tags=True):
    if passphrase is not None:
        passphrase = bytes(passphrase, 'utf-8')

    result = False

    if wrap_tags:
        for ec_test in [False, True]:
            wrapped_data = wrap_private_key(raw_data, passphrase, ec_test)
            if result := _load_private_key(wrapped_data, passphrase):
                return result
        return False
    else:
        return _load_private_key(raw_data, passphrase)

def load_openssh_public_key(raw_data, type):
    try:
        return serialization.load_ssh_public_key(bytes(f'{type} {raw_data}', 'utf-8'))
    except ValueError:
        return False

def load_openssh_private_key(raw_data, passphrase=None, wrap_tags=True):
    if wrap_tags:
        raw_data = wrap_openssh_private_key(raw_data)

    try:
        return serialization.load_ssh_private_key(bytes(raw_data, 'utf-8'), password=passphrase)
    except ValueError:
        return False

def load_certificate_request(raw_data, wrap_tags=True):
    if wrap_tags:
        raw_data = wrap_certificate_request(raw_data)

    try:
        return x509.load_pem_x509_csr(bytes(raw_data, 'utf-8'))
    except ValueError:
        return False

def load_certificate(raw_data, wrap_tags=True):
    if wrap_tags:
        raw_data = wrap_certificate(raw_data)

    try:
        return x509.load_pem_x509_certificate(bytes(raw_data, 'utf-8'))
    except ValueError:
        return False

def load_crl(raw_data, wrap_tags=True):
    if wrap_tags:
        raw_data = wrap_crl(raw_data)

    try:
        return x509.load_pem_x509_crl(bytes(raw_data, 'utf-8'))
    except ValueError:
        return False

def load_dh_parameters(raw_data, wrap_tags=True):
    if wrap_tags:
        raw_data = wrap_dh_parameters(raw_data)

    try:
        return serialization.load_pem_parameters(bytes(raw_data, 'utf-8'))
    except ValueError:
        return False

# Verify

def is_ca_certificate(cert):
    if not cert:
        return False

    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS)
        return ext.value.ca
    except ExtensionNotFound:
        return False

def verify_certificate(cert, ca_cert):
    # Verify certificate was signed by specified CA
    if ca_cert.subject != cert.issuer:
        return False

    ca_public_key = ca_cert.public_key()
    try:
        if isinstance(ca_public_key, rsa.RSAPublicKeyWithSerialization):
            ca_public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                padding=padding.PKCS1v15(),
                algorithm=cert.signature_hash_algorithm)
        elif isinstance(ca_public_key, dsa.DSAPublicKeyWithSerialization):
            ca_public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                algorithm=cert.signature_hash_algorithm)
        elif isinstance(ca_public_key, ec.EllipticCurvePublicKeyWithSerialization):
            ca_public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                signature_algorithm=ec.ECDSA(cert.signature_hash_algorithm))
        else:
            return False # We cannot verify it
        return True
    except InvalidSignature:
        return False

def verify_crl(crl, ca_cert):
    # Verify CRL was signed by specified CA
    if ca_cert.subject != crl.issuer:
        return False

    ca_public_key = ca_cert.public_key()
    try:
        if isinstance(ca_public_key, rsa.RSAPublicKeyWithSerialization):
            ca_public_key.verify(
                crl.signature,
                crl.tbs_certlist_bytes,
                padding=padding.PKCS1v15(),
                algorithm=crl.signature_hash_algorithm)
        elif isinstance(ca_public_key, dsa.DSAPublicKeyWithSerialization):
            ca_public_key.verify(
                crl.signature,
                crl.tbs_certlist_bytes,
                algorithm=crl.signature_hash_algorithm)
        elif isinstance(ca_public_key, ec.EllipticCurvePublicKeyWithSerialization):
            ca_public_key.verify(
                crl.signature,
                crl.tbs_certlist_bytes,
                signature_algorithm=ec.ECDSA(crl.signature_hash_algorithm))
        else:
            return False # We cannot verify it
        return True
    except InvalidSignature:
        return False

def verify_ca_chain(sorted_names, pki_node):
    if len(sorted_names) == 1: # Single cert, no chain
        return True

    for name in sorted_names:
        cert = load_certificate(pki_node[name]['certificate'])
        verified = False
        for ca_name in sorted_names:
            if name == ca_name:
                continue
            ca_cert = load_certificate(pki_node[ca_name]['certificate'])
            if verify_certificate(cert, ca_cert):
                verified = True
                break
        if not verified and name != sorted_names[-1]:
            # Only permit top-most certificate to fail verify (e.g. signed by public CA not explicitly in chain)
            return False
    return True

# Certificate chain

def find_parent(cert, ca_certs):
    for ca_cert in ca_certs:
        if verify_certificate(cert, ca_cert):
            return ca_cert
    return None

def find_chain(cert, ca_certs):
    remaining = ca_certs.copy()
    chain = [cert]

    while remaining:
        parent = find_parent(chain[-1], remaining)
        if parent is None:
            # No parent in the list of remaining certificates or there's a circular dependency
            break
        elif parent == chain[-1]:
            # Self-signed: must be root CA (end of chain)
            break
        else:
            remaining.remove(parent)
            chain.append(parent)

    return chain

def sort_ca_chain(ca_names, pki_node):
    def ca_cmp(ca_name1, ca_name2, pki_node):
        cert1 = load_certificate(pki_node[ca_name1]['certificate'])
        cert2 = load_certificate(pki_node[ca_name2]['certificate'])

        if verify_certificate(cert1, cert2): # cert1 is child of cert2
            return -1
        return 1

    from functools import cmp_to_key
    return sorted(ca_names, key=cmp_to_key(lambda cert1, cert2: ca_cmp(cert1, cert2, pki_node)))
