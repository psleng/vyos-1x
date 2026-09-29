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
import json
import base64

from vyos.utils.process import rc_cmd
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

default_pcrs = ['0','2','4','7']
tpm_handle = 0x81000000
tpm_key_save_dir = "/config/auth/"

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

def read_tpm_key_file(key_file):
    """
    Read existing key on TPM with provided public and private files
    """
    with tempfile.TemporaryDirectory() as tpm_dir:

        primary_context_file = os.path.join(tpm_dir, 'primary.ctx')
        code, output = rc_cmd(f'tpm2_createprimary -C e -g sha256 -G rsa -c {primary_context_file}')
        if code != 0:
            raise Exception('read_tpm_key: Failed to re-create primary key')

        key_file = os.path.join(tpm_key_save_dir, key_file)
        if not key_file or not os.path.exists(key_file):
            raise Exception('read_tpm_key: Failed to find file to read details from ' + key_file)
        else:
            with open(key_file, 'r') as f:
                long_key_json = json.load(f)

            priv_file, pub_file = os.path.join(tpm_dir, 'tmp.priv'), os.path.join(tpm_dir, 'tmp.pub')
            with open(priv_file, 'wb') as f:
                f.write(base64.b64decode(long_key_json['private'].encode('utf-8')))
            with open(pub_file, 'wb') as f:
                f.write(base64.b64decode(long_key_json['public'].encode('utf-8')))

        load_context_file = os.path.join(tpm_dir, 'load.ctx')
        code, output = rc_cmd(f'tpm2_load -C {primary_context_file} -u {pub_file} -r {priv_file} -c {load_context_file}')
        if code != 0:
            print(output)
            raise Exception('read_tpm_key: Failed to load object')

        tpm_key_file = os.path.join(tpm_dir, 'tpm_key.key')
        code, output = rc_cmd(f'tpm2_unseal -c {load_context_file} -o {tpm_key_file}')
        if code != 0:
            raise Exception('read_tpm_key: Failed to read key from TPM')

        with open(tpm_key_file, 'rb') as f:
            tpm_key = f.read()

        with open(key_file, 'r') as f:
            long_key_json = json.load(f)
        nonce = base64.b64decode(long_key_json["nonce"])
        long_key = base64.b64decode(long_key_json["key"])
        cipher = AESGCM(tpm_key)
        tpm_key = cipher.decrypt(nonce, long_key, None)

        return tpm_key.decode("utf-8")

def write_tpm_key_file(key, save_file):
    """
    Write encrypted TPM key and saves them to save_file.pub and save_file.priv files
    """
    with tempfile.TemporaryDirectory() as tpm_dir:
        primary_context_file = os.path.join(tpm_dir, 'primary.ctx')
        code, output = rc_cmd(f'tpm2_createprimary -C e -g sha256 -G rsa -c {primary_context_file}')
        if code != 0:
            raise Exception('write_tpm_key: Failed to create primary key')

        public_obj = os.path.join(tpm_dir, 'tmp.pub')
        private_obj = os.path.join(tpm_dir, 'tmp.priv')
        dir_path = Path(tpm_key_save_dir + save_file)
        dir_path.parent.mkdir(parents=True, exist_ok=True)
        encrypted_file = tpm_key_save_dir + save_file

        # Generate AES key first, then encrypt original private in a new file, and tpm seal the AES key
        aes_256_key_bytes = os.urandom(32)
        cipher = AESGCM(aes_256_key_bytes)
        nonce = os.urandom(12)
        cipher_text = cipher.encrypt(nonce, key, None)

        key_file = os.path.join(tpm_dir, 'crypt.key')
        with open(key_file, 'wb') as f:
            f.write(aes_256_key_bytes)
        code, output = rc_cmd(
            f'tpm2_create -g sha256 \
            -u {public_obj} -r {private_obj} \
            -C {primary_context_file} -i {key_file}')

        encrypted_info = {"nonce": base64.b64encode(nonce).decode("utf-8"),
                            "key": base64.b64encode(cipher_text).decode("utf-8")}

        if code != 0:
            print(output)
            raise Exception('write_tpm_key: Failed to create object')

        with open(private_obj, 'rb') as bin:
            priv_string = bin.read()
            encrypted_info["private"] = base64.b64encode(priv_string).decode("utf-8")
        with open(public_obj, 'rb') as bin:
            pub_string = bin.read()
            encrypted_info["public"] = base64.b64encode(pub_string).decode("utf-8")
        with open(encrypted_file, 'w') as f:
            json.dump(encrypted_info, f)

        return encrypted_file

# PERLE - added check for tpm support

from vyos.utils.process import cmd

tpm_mountpoint = "/run/tpm-state"
tpm_partition = "/dev/mmcblk0p3"
tpm_enabled_path = f"{tpm_mountpoint}/boot/.tpm.enabled"
tpm_dev_path = "/sys/class/tpm/tpm0"

def mount_tpm_state():
    """
    Mount the persistent TPM state partition.
    """

    os.makedirs(tpm_mountpoint, exist_ok=True)

    if not os.path.ismount(tpm_mountpoint):
        code, output = rc_cmd(
            f'mount {tpm_partition} {tpm_mountpoint}'
        )

        if code != 0:
            raise Exception(
                f'mount_tpm_state: Failed to mount '
                f'{tpm_partition}: {output}'
            )


def unmount_tpm_state():
    """
    Unmount the persistent TPM state partition.
    """

    if os.path.ismount(tpm_mountpoint):
        rc_cmd('sync')

        code, output = rc_cmd(
            f'umount {tpm_mountpoint}'
        )

        if code != 0:
            raise Exception(
                f'unmount_tpm_state: Failed to unmount '
                f'{tpm_mountpoint}: {output}'
            )

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
    """
    try:
        mount_tpm_state()
        return os.path.exists(tpm_enabled_path)
    finally:
        unmount_tpm_state()

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
    """

    try:
        mount_tpm_state()

        Path(tpm_enabled_path).parent.mkdir(
            parents=True,
            exist_ok=True
        )

        cmd(f'touch {tpm_enabled_path}')

    finally:
        unmount_tpm_state()

def tpm_disable():
    """
     Args:
        none
    Returns:
        none
    """

    try:
        mount_tpm_state()
        cmd(f'rm -f {tpm_enabled_path}')
    finally:
        unmount_tpm_state()
