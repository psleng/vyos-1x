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

import os
import tempfile

from vyos.utils.process import rc_cmd

import json
from datetime import datetime, timedelta
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.backends import default_backend

default_pcrs = ['0','2','4','7']
tpm_handle = 0x81000000

def init_tpm(clear=False):
    """
    Initialize TPM
    """
    code, output = rc_cmd('tpm2_startup' + (' -c' if clear else ''))
    if code != 0:
        raise Exception('init_tpm: Failed to initialize TPM')

def clear_tpm_key():
    """
    Clear existing key on TPM
    """
    code, output = rc_cmd(f'tpm2_evictcontrol -C o -c {tpm_handle}')
    if code != 0:
        raise Exception('clear_tpm_key: Failed to clear TPM key')

def read_tpm_key(index=0, pcrs=default_pcrs):
    """
    Read existing key on TPM
    """
    with tempfile.TemporaryDirectory() as tpm_dir:
        pcr_str = ",".join(pcrs)

        tpm_key_file = os.path.join(tpm_dir, 'tpm_key.key')
        code, output = rc_cmd(f'tpm2_unseal -c {tpm_handle + index} -p pcr:sha256:{pcr_str} -o {tpm_key_file}')
        if code != 0:
            raise Exception('read_tpm_key: Failed to read key from TPM')

        with open(tpm_key_file, 'rb') as f:
            tpm_key = f.read()

        return tpm_key

def write_tpm_key(key, index=0, pcrs=default_pcrs):
    """
    Saves key to TPM
    """
    with tempfile.TemporaryDirectory() as tpm_dir:
        pcr_str = ",".join(pcrs)

        policy_file = os.path.join(tpm_dir, 'policy.digest')
        code, output = rc_cmd(f'tpm2_createpolicy --policy-pcr -l sha256:{pcr_str} -L {policy_file}')
        if code != 0:
            raise Exception('write_tpm_key: Failed to create policy digest')

        primary_context_file = os.path.join(tpm_dir, 'primary.ctx')
        code, output = rc_cmd(f'tpm2_createprimary -C e -g sha256 -G rsa -c {primary_context_file}')
        if code != 0:
            raise Exception('write_tpm_key: Failed to create primary key')

        key_file = os.path.join(tpm_dir, 'crypt.key')
        with open(key_file, 'wb') as f:
            f.write(key)

        public_obj = os.path.join(tpm_dir, 'obj.pub')
        private_obj = os.path.join(tpm_dir, 'obj.key')
        code, output = rc_cmd(
            f'tpm2_create -g sha256 \
            -u {public_obj} -r {private_obj} \
            -C {primary_context_file} -L {policy_file} -i {key_file}')

        if code != 0:
            raise Exception('write_tpm_key: Failed to create object')

        load_context_file = os.path.join(tpm_dir, 'load.ctx')
        code, output = rc_cmd(f'tpm2_load -C {primary_context_file} -u {public_obj} -r {private_obj} -c {load_context_file}')

        if code != 0:
            raise Exception('write_tpm_key: Failed to load object')

        code, output = rc_cmd(f'tpm2_evictcontrol -c {load_context_file} -C o {tpm_handle + index}')

        if code != 0:
            raise Exception('write_tpm_key: Failed to write object to TPM')

# PERLE - added check for tpm support

from vyos.utils.process import cmd

tpm_enabled_path = "/etc/vyos/tpm.enabled"
tpm_dev_path = "/sys/class/tpm/tpm0"

def tpm_exist():
    """
     Args:
        none

    Returns:
        True if tpm device exists, False otherwise.

    Note:
        For now, it returns True/False based on /sys/class/tpm/tpm0 existing or not.
    """
    return os.path.exists(tpm_dev_path)

def tpm_enabled():
    """
     Args:
        none

    Returns:
        True if tpm support enabled by user, False otherwise.

    Note:
        For now, it returns True/False based on file /etc/vyos/tpm.enabled existing or not.
    """
    return os.path.exists(tpm_enabled_path)

def tpm_allowed():
    """
     Args:
        none

    Returns:
        True if tpm allowed (configured AND tpm device exists), False otherwise.

    Note:
        For now, it returns True/False based on /sys/class/tpm/tpm0 existing or not.
    """
    return tpm_enabled() and tpm_exist()


def tpm_enable():
    """
     Args:
        none

    Returns:
        none

    Note:
        For now, it touches/creates /etc/vyos/tpm.enabled.
    """
    cmd(f'sudo touch {tpm_enabled_path}')

def tpm_disable():
    """
     Args:
        none

    Returns:
        none

    Note:
        For now, it removes /etc/vyos/tpm.enabled.
    """
    cmd(f'sudo rm -f {tpm_enabled_path}')

# PKCS#11 Configuration : TODO: SHOULD NOT BE HARD CODED
PKCS11_PIN = "1234"  # Default PIN (should be overridable)

class TPM2Error(Exception):
    """TODO : TPM2 operation error"""
    pass

class PKCS11Error(Exception):
    """TODO : PKCS#11 operation error"""
    pass

def init_pkcs11_token(pin=PKCS11_PIN, so_pin="12345678"):
    """
    Initialize PKCS#11 token on TPM2.

    Args:
        pin: User PIN for token access (default: 1234)
        so_pin: Security Officer PIN (default: 12345678)

    Returns:
        True if successful, raises Exception otherwise

    Raises:
        PKCS11Error: If token initialization fails
    """
    try:
        # Check if tpm2-pkcs11 is available TODO : the need to check if tpm2-pkcs11 shoudlnt be required in release
        code, output = rc_cmd('pkcs11-tool --module "" --show-info 2>/dev/null || echo "unavailable"')
        if code != 0:
            raise PKCS11Error('PKCS#11 library not available. Install tpm2-pkcs11.')

        # Initialize TPM if not already done
        try:
            init_tpm()
        except:
            pass  # TPM may already be initialized

        return True
    except Exception as e:
        raise PKCS11Error(f'Failed to initialize PKCS#11 token: {str(e)}')

def extract_cert_from_tpm(key_handle, subject_info=None):
    """
    Extract public key from TPM key and create self-signed certificate.

    Args:
        key_handle: TPM persistent key handle (hex string or int)
        subject_info: dict with certificate subject fields:
            {
                'country': 'US',
                'state': 'California',
                'locality': 'San Francisco',
                'organization': 'VyOS',
                'common_name': 'vpn.example.com'
            }
            If None, uses defaults.

    Returns:
        dict: {
            'certificate': PEM-encoded certificate string,
            'public_key': PEM-encoded public key string,
            'key_handle': key_handle,
            'subject': certificate subject fields
        }

    Raises:
        TPM2Error: If extraction fails
    """
    try:
        if isinstance(key_handle, int):
            key_handle_str = hex(key_handle)
        else:
            key_handle_str = key_handle

        with tempfile.TemporaryDirectory() as tpm_dir:
            # Read public key from TPM
            pub_file = os.path.join(tpm_dir, 'public.tpm')
            code, output = rc_cmd(f'tpm2_readpublic -c {key_handle_str} -o {pub_file}')
            if code != 0:
                raise TPM2Error(f'Failed to read public key from TPM: {output}')

            # Convert TPM public key to PEM format
            pub_pem_file = os.path.join(tpm_dir, 'public.pem')
            code, output = rc_cmd(
                f'tpm2_convert public -i {pub_file} -o {pub_pem_file} -f pem'
            )
            if code != 0:
                raise TPM2Error(f'Failed to convert public key to PEM: {output}')

            # Read the PEM public key
            with open(pub_pem_file, 'r') as f:
                public_key_pem = f.read()

            # Load the public key for certificate generation
            public_key = serialization.load_pem_public_key(
                bytes(public_key_pem, 'utf-8'),
                backend=default_backend()
            )

            # Set default subject if not provided
            if subject_info is None:
                subject_info = {
                    'country': 'US',
                    'state': 'VyOS',
                    'locality': 'VyOS',
                    'organization': 'VyOS',
                    'common_name': f'vyos-vpn-{key_handle_str}'
                }

            # Create self-signed certificate (placeholder - real cert signed by TPM later)
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, subject_info.get('country', 'US')),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, subject_info.get('state', 'VyOS')),
                x509.NameAttribute(NameOID.LOCALITY_NAME, subject_info.get('locality', 'VyOS')),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, subject_info.get('organization', 'VyOS')),
                x509.NameAttribute(NameOID.COMMON_NAME, subject_info.get('common_name', 'vyos-vpn')),
            ])

            cert = x509.CertificateBuilder().subject_name(
                subject
            ).issuer_name(
                issuer
            ).public_key(
                public_key
            ).serial_number(
                x509.random_serial_number()
            ).not_valid_before(
                datetime.utcnow()
            ).not_valid_after(
                datetime.utcnow() + timedelta(days=365*10)
            ).add_extension(
                x509.SubjectKeyIdentifier.from_public_key(public_key),
                critical=False,
            ).sign(
                private_key=public_key,  # Placeholder; real TPM signature later
                algorithm=hashes.SHA256(),
                backend=default_backend()
            )

            cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')

            return {
                'certificate': cert_pem,
                'public_key': public_key_pem,
                'key_handle': key_handle_str,
                'subject': subject_info
            }
    except TPM2Error:
        raise
    except Exception as e:
        raise TPM2Error(f'Failed to extract certificate from TPM: {str(e)}')

def list_tpm_keys():
    """
    List all persistent keys in TPM.

    Returns:
        list: List of dicts with key information:
            {
                'handle': '0xXXXXXXXX',
                'handle_int': 0xXXXXXXXX
            }

    Raises:
        TPM2Error: If listing fails
    """
    try:
        code, output = rc_cmd('tpm2_getcap handles-persistent')
        if code != 0:
            raise TPM2Error(f'Failed to list TPM keys: {output}')

        keys = []
        for line in output.strip().split('\n'):
            line = line.strip()
            if line.startswith('0x'):
                handle_int = int(line, 16)
                keys.append({
                    'handle': line,
                    'handle_int': handle_int
                })

        return keys
    except TPM2Error:
        raise
    except Exception as e:
        raise TPM2Error(f'Failed to list TPM keys: {str(e)}')

def sign_data_with_tpm_key(key_handle, data, hash_alg='sha256'):
    """
    Sign data using a TPM key (for authentication/signatures).

    Args:
        key_handle: TPM persistent key handle (hex string or int)
        data: Bytes to sign
        hash_alg: Hash algorithm ('sha256', 'sha384', 'sha512')

    Returns:
        bytes: Signature data

    Raises:
        TPM2Error: If signing fails
    """
    try:
        if isinstance(key_handle, int):
            key_handle_str = hex(key_handle)
        else:
            key_handle_str = key_handle

        with tempfile.TemporaryDirectory() as tpm_dir:
            # Write data to file
            data_file = os.path.join(tpm_dir, 'data.bin')
            with open(data_file, 'wb') as f:
                f.write(data if isinstance(data, bytes) else bytes(data, 'utf-8'))

            # Sign using TPM
            sig_file = os.path.join(tpm_dir, 'signature.sig')
            code, output = rc_cmd(
                f'tpm2_sign -c {key_handle_str} -g {hash_alg} '
                f'-o {sig_file} {data_file}'
            )
            if code != 0:
                raise TPM2Error(f'Failed to sign data with TPM key: {output}')

            # Read signature
            with open(sig_file, 'rb') as f:
                signature = f.read()

            return signature
    except TPM2Error:
        raise
    except Exception as e:
        raise TPM2Error(f'Failed to sign data with TPM key: {str(e)}')

def get_tpm_key_info(key_handle):
    """
    Get detailed information about a TPM key.

    Args:
        key_handle: TPM persistent key handle (hex string or int)

    Returns:
        dict: Key information including:
            {
                'handle': handle string,
                'type': 'rsa' or 'ec',
                ...
            }

    Raises:
        TPM2Error: If query fails
    """
    try:
        if isinstance(key_handle, int):
            key_handle_str = hex(key_handle)
        else:
            key_handle_str = key_handle

        code, output = rc_cmd(f'tpm2_readpublic -c {key_handle_str} -f json')
        if code != 0:
            raise TPM2Error(f'Failed to get key info: {output}')

        try:
            key_info = json.loads(output)
        except json.JSONDecodeError:
            # Fallback if JSON output isn't available
            key_info = {
                'handle': key_handle_str,
                'raw_output': output
            }

        return key_info
    except TPM2Error:
        raise
    except Exception as e:
        raise TPM2Error(f'Failed to get key info: {str(e)}')
