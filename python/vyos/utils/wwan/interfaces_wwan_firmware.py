# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared cellular firmware catalog, X.509/CMS signing, archive, and cache handling.

Catalogs are authenticated before use; their signed hashes authenticate archives.
"""

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import urlopen

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

RESERVE = 32 * 1024 * 1024
MAX_ARCHIVE = 4 * 1024**3
MAX_MEMBERS = 10000
MAX_MANIFEST_SIZE = 65536
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT = 30
DEFAULT_CACHE_ARCHIVES = 2
DEFAULT_CACHE_BYTES = 512 * 1024**2
STANDALONE_METADATA_URL = 'https://local.invalid/archive'


TRUST_DIR = Path('/usr/share/vyos/keys')
REVOCATION_STATE = Path('/config/firmware-signing/revocations.json')
MAX_METADATA = 4 * 1024 * 1024
MAX_SIGNATURE = 128 * 1024
MAX_CRL = 1024 * 1024


class FirmwareTrustError(ValueError):
    """Authentication failed; never silently substitute another catalog."""


def sidecar_url(url, suffix):
    """Append to the URL path, preserving a query string."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path + suffix))


def fetch_bytes(url, limit=MAX_METADATA):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
        raise ValueError('Firmware metadata requires an HTTP(S) URL without credentials')
    with urlopen(url, timeout=30) as response:
        if urlsplit(response.geturl()).scheme not in ('http', 'https'):
            raise ValueError('Unsupported firmware metadata redirect')
        length = response.headers.get('Content-Length')
        if length is not None and int(length) > limit:
            raise FirmwareTrustError('Firmware metadata exceeds size limit')
        content = response.read(limit + 1)
    if len(content) > limit:
        raise FirmwareTrustError('Firmware metadata exceeds size limit')
    return content


def _certificates(data):
    # This also works on Debian's cryptography version (before the plural API).
    marker = b'-----END CERTIFICATE-----'
    result = []
    for piece in data.split(marker):
        if piece.strip():
            result.append(x509.load_pem_x509_certificate(piece.strip() + b'\n' + marker + b'\n'))
    return result


def load_roots(trust_dir=TRUST_DIR):
    roots = []
    # The shared image key directory contains other trust domains.
    pattern = 'igos-firmware-root-*.pem' if Path(trust_dir) == TRUST_DIR else '*.pem'
    for path in sorted(Path(trust_dir).glob(pattern)):
        data = path.read_bytes()
        if b'PRIVATE KEY' in data:
            raise FirmwareTrustError('Private keys are forbidden in firmware trust directory')
        try:
            certificates = _certificates(data)
            for cert in certificates:
                constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
                # A flat signing-material directory may also hold the public
                # leaf certificate. Only CA certificates can be trust anchors.
                if not constraints.ca:
                    continue
                usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
                if cert.issuer != cert.subject or not constraints.ca or not usage.key_cert_sign or not usage.crl_sign:
                    raise ValueError('Not a root CA')
                if any(cert.subject == previous.subject for previous in roots):
                    raise ValueError('Firmware roots must have distinct subjects')
                roots.append(cert)
        except (ValueError, x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm) as error:
            raise FirmwareTrustError(f'Invalid firmware root CA: {path.name}') from error
    if not roots:
        raise FirmwareTrustError('No firmware root CA is installed')
    return roots


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _validated_crls(data, roots):
    if len(data) > MAX_CRL:
        raise FirmwareTrustError('Firmware revocation list exceeds size limit')
    marker = b'-----END X509 CRL-----'
    crls, floors = [], {}
    now = datetime.now(timezone.utc)
    try:
        for piece in data.split(marker):
            if not piece.strip():
                continue
            crl = x509.load_pem_x509_crl(piece.strip() + b'\n' + marker + b'\n')
            issuers = [root for root in roots if crl.issuer == root.subject and crl.is_signature_valid(root.public_key())]
            if len(issuers) != 1:
                raise ValueError('Untrusted or ambiguous CRL issuer')
            if hasattr(crl, 'next_update_utc'):
                last_update, next_update = crl.last_update_utc, crl.next_update_utc
            else:
                last_update = _utc(crl.last_update)
                next_update = _utc(crl.next_update) if crl.next_update is not None else None
            if next_update is None or not last_update <= now < next_update:
                raise ValueError('Revocation list is expired or not yet valid')
            number = crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
            fingerprint = issuers[0].fingerprint(hashes.SHA256()).hex()
            if fingerprint in floors:
                raise ValueError('Duplicate revocation list')
            floors[fingerprint] = {'number': number, 'sha256': hashlib.sha256(crl.public_bytes(serialization.Encoding.DER)).hexdigest()}
            crls.append(crl)
    except (ValueError, TypeError, x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm) as error:
        raise FirmwareTrustError(f'Invalid firmware revocation list: {error}') from error
    if not crls:
        raise FirmwareTrustError('Firmware revocation list is missing')
    return crls, floors


def _record_revocations(floors, state_path):
    """Reject older CRLs after learning a newer root-signed revocation list."""
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with state_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if not isinstance(state, dict) or any(
                not isinstance(value, dict) or type(value.get('number')) is not int or
                not isinstance(value.get('sha256'), str) or len(value['sha256']) != 64
                for value in state.values()
            ):
                raise ValueError('Invalid state structure')
        except (ValueError, TypeError) as error:
            raise FirmwareTrustError('Firmware revocation state is corrupt; refusing to reset it') from error
        for root, new in floors.items():
            old = state.get(root)
            if old and (new['number'] < old['number'] or
                        (new['number'] == old['number'] and new['sha256'] != old['sha256'])):
                raise FirmwareTrustError('Firmware revocation list rollback detected')
        state.update(floors)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=state_path.parent, mode='w', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(state_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def verify_file(content_path, signature, revocations, *, trust_dir=TRUST_DIR, state_path=REVOCATION_STATE):
    """Validate detached CMS, current signer trust/expiry/purpose, and root CRL.

    Certificate signingTime is not a trusted timestamp. Historical standalone
    signatures with expired signers are rejected; catalog installs use a current
    signer and authenticate historical archives through their signed SHA-256.
    """
    if not signature or len(signature) > MAX_SIGNATURE:
        raise FirmwareTrustError('Firmware signature is missing or exceeds size limit')
    roots = load_roots(trust_dir)
    crls, floors = _validated_crls(revocations, roots)
    # Record authenticated revocations even if the payload's signer is revoked.
    # A network fallback must never resurrect a cached catalog with an older CRL.
    _record_revocations(floors, state_path)
    with tempfile.TemporaryDirectory(prefix='firmware-trust-') as temporary:
        work = Path(temporary)
        bundle = work/'trust.pem'
        bundle.write_bytes(b''.join(root.public_bytes(serialization.Encoding.PEM) for root in roots) +
                           b''.join(crl.public_bytes(serialization.Encoding.PEM) for crl in crls))
        sig = work/'signature.der'
        sig.write_bytes(signature)
        signer = work/'signer.pem'
        try:
            result = subprocess.run([
                'openssl', 'cms', '-verify', '-binary', '-inform', 'DER', '-in', str(sig),
                '-content', str(content_path), '-CAfile', str(bundle), '-no-CApath', '-no-CAstore',
                '-purpose', 'any', '-crl_check', '-x509_strict', '-check_ss_sig', '-verify_retcode',
                '-verify_depth', '0', '-auth_level', '2',
                '-signer', str(signer), '-out', os.devnull,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FirmwareTrustError('Unable to run firmware signature verification') from error
        if result.returncode:
            raise FirmwareTrustError('Firmware signature verification failed (signature, trust, expiry, or revocation)')
        try:
            signers = _certificates(signer.read_bytes())
            if len(signers) != 1:
                raise ValueError('Exactly one signing certificate is required')
            cert = signers[0]
            if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise ValueError('CA certificates cannot sign firmware directly')
            if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
                raise ValueError('Signing certificate lacks digitalSignature usage')
            if ExtendedKeyUsageOID.CODE_SIGNING not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value:
                raise ValueError('Signing certificate lacks codeSigning purpose')
            issuer_crls = [crl for crl in crls if crl.issuer == cert.issuer]
            if len(issuer_crls) != 1 or not any(cert.issuer == root.subject for root in roots):
                raise ValueError('Signing certificate must be issued directly by a firmware root')
            # CMS may contain its own older CRLs. Independently enforce the
            # authenticated external CRL, whose number passed the rollback check.
            if issuer_crls[0].get_revoked_certificate_by_serial_number(cert.serial_number) is not None:
                raise ValueError('Firmware signing certificate is revoked')
        except (ValueError, x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm) as error:
            raise FirmwareTrustError(f'Invalid firmware signing certificate: {error}') from error
        return cert.fingerprint(hashes.SHA256()).hex()


def sign_file(content_path, key_path, cert_path, *, password=None):
    """Produce a detached binary CMS signature using a code-signing leaf."""
    # OpenSSL hashes the file incrementally; firmware archives can be gigabytes.
    # Passphrases go through stdin, never process arguments or the output files.
    try:
        result = subprocess.run([
            'openssl', 'cms', '-sign', '-binary', '-md', 'sha256', '-nosmimecap',
            '-in', str(content_path), '-signer', str(cert_path), '-inkey', str(key_path),
            '-passin', 'stdin', '-outform', 'DER',
        ], input=(password or b'') + b'\n', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FirmwareTrustError('Unable to run firmware signing') from error
    if result.returncode or not result.stdout or len(result.stdout) > MAX_SIGNATURE:
        raise FirmwareTrustError('Firmware signing failed; check the leaf certificate, key, and passphrase')
    return result.stdout


def load_catalog(url, cache_path, parser, *, refresh=True, trust_dir=TRUST_DIR, state_path=REVOCATION_STATE):
    """Store signed bytes atomically; re-verify on every read, including offline."""
    cache_path = Path(cache_path)

    def verify(content, sig, crl):
        if len(content) > MAX_METADATA:
            raise FirmwareTrustError('Firmware catalog exceeds size limit')
        with tempfile.TemporaryDirectory(prefix='firmware-catalog-') as temporary:
            target = Path(temporary)/'catalog.json'
            target.write_bytes(content)
            verify_file(target, sig, crl, trust_dir=trust_dir, state_path=state_path)
        return parser(json.loads(content))

    def read_cache():
        try:
            cached = json.loads(cache_path.read_text())
            if cached['source_url'] != url or cached['signed_cache'] != 1:
                raise ValueError('Wrong source or unsigned legacy cache')
            parts = [base64.b64decode(cached[key], validate=True) for key in ('content', 'signature', 'crl')]
        except (KeyError, ValueError, binascii.Error) as error:
            raise FirmwareTrustError('No verified firmware catalog cache for this URL') from error
        return verify(*parts)

    if not refresh:
        return read_cache()
    if not url:
        raise ValueError('No firmware catalog URL is configured')
    try:
        content = fetch_bytes(url)
        signature = fetch_bytes(sidecar_url(url, '.sig'), MAX_SIGNATURE)
        crl = fetch_bytes(sidecar_url(url, '.crl'), MAX_CRL)
    except (OSError, TimeoutError):
        if cache_path.is_file():
            return read_cache()
        raise
    entries = verify(content, signature, crl)
    cache = {'signed_cache': 1, 'source_url': url, **{
        name: base64.b64encode(value).decode('ascii') for name, value in
        (('content', content), ('signature', signature), ('crl', crl))}}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache_path.parent, mode='w', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(cache, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(cache_path)
    except OSError:
        # A valid online catalog can be used without cache retention.
        pass
    finally:
        if 'temporary' in locals():
            temporary.unlink(missing_ok=True)
    return entries


def component(value):
    """Validate an identifier before using it in a filesystem path."""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*', value):
        raise ValueError(f'Invalid firmware identifier: {value!r}')
    return value


def native_version(value):
    """Accept the opaque revision reported by ModemManager, including build dates."""
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or value in ('.', '..') or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid native firmware revision')
    return value


def revision_rank(pattern, revision):
    """Rank a hardware match: exact, longest prefix, then catch-all."""
    if pattern == '*':
        return 0
    if not isinstance(pattern, str) or not re.fullmatch(r'\d+(?:\.\d+)*(?:\.\*)?', pattern):
        raise ValueError(f'Invalid hardware revision pattern: {pattern!r}')
    if pattern.endswith('.*'):
        return len(pattern.split('.')) if revision.startswith(pattern[:-1]) else -1
    return 10000 if pattern == revision else -1


def version_key(version):
    """Compare numeric igOS labels, treating 1.0 and 1.0.0 as equal."""
    if not isinstance(version, str) or not re.fullmatch(r'\d+(?:\.\d+)*', version):
        raise ValueError(f'Invalid igOS version: {version!r}')
    parts = tuple(map(int, version.split('.')))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def parse_catalog(data):
    """Validate a schema-1 catalog and return its model/revision tracks."""
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
        raise ValueError('Archive installation requires a schema-1 firmware catalog')
    models = data.get('cellular-firmware')
    if not isinstance(models, list):
        raise TypeError('Missing cellular-firmware list')
    entries, seen = [], set()
    for model in models:
        manufacturer = model['manufacturer']
        if not isinstance(manufacturer, str) or not manufacturer.strip():
            raise ValueError('Missing manufacturer')
        component(model['model'])
        if model['installer'] not in ('uxfp', 'tfl'):
            raise ValueError('Unsupported firmware installer')
        for track in model['revisions']:
            pattern = track['hardware_revision']
            revision_rank(pattern, '0')
            key = (manufacturer, model['model'], pattern)
            if key in seen:
                raise ValueError('Duplicate model/revision block')
            seen.add(key)
            versions, labels, native = {}, set(), set()
            for release in track['firmware']:
                label = release['igos_version']
                order = version_key(label)
                native_version(release['telit_version'])
                if order in labels or release['telit_version'] in native:
                    raise ValueError('Duplicate firmware version in revision track')
                labels.add(order)
                native.add(release['telit_version'])
                url = urlsplit(release['url'])
                if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password:
                    raise ValueError('Firmware URL must be absolute HTTP(S) without credentials')
                if not re.fullmatch(r'[0-9a-fA-F]{64}', release['archive_sha256']):
                    raise ValueError('archive_sha256 must contain 64 hexadecimal characters')
                versions[label] = release
            if not versions:
                raise ValueError('Empty firmware track')
            entries.append({**model, 'hardware_revision': pattern, 'releases': versions})
    return entries


def space(path, needed):
    """Require enough free space while preserving the device reserve."""
    if needed < 0 or shutil.disk_usage(path).free < needed + RESERVE:
        raise ValueError(f'Insufficient space in {path}: need {needed + RESERVE} bytes including reserve')


def download_archive(url, destination):
    """Check size before writing and bound streaming even without Content-Length."""
    space(destination.parent, 0)
    with urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
        if urlsplit(response.geturl()).scheme not in ('http', 'https'):
            raise ValueError('Unsupported firmware redirect')
        length = response.headers.get('Content-Length')
        expected = int(length) if length is not None else None
        if expected is not None:
            if expected < 0 or expected > MAX_ARCHIVE:
                raise ValueError('Firmware archive exceeds size limit')
            space(destination.parent, expected)
        total = 0
        interactive = sys.stdout.isatty()
        with ExitStack() as progress_context:
            progress = None
            if interactive and expected:
                from vyos.progressbar import Progressbar

                progress = progress_context.enter_context(Progressbar())
            try:
                with destination.open('xb') as output:
                    while chunk := response.read(CHUNK_SIZE):
                        total += len(chunk)
                        if total > MAX_ARCHIVE:
                            raise ValueError('Firmware archive exceeds size limit')
                        space(destination.parent, len(chunk))
                        output.write(chunk)
                        if progress is not None:
                            progress.progress(total, expected)
                        elif interactive:
                            print(f'\rDownloaded {total / 1024**2:.1f} MiB', end='', file=sys.stderr, flush=True)
                if expected is not None and total != expected:
                    raise ValueError('Incomplete firmware download')
            finally:
                if interactive and (progress is None or total != expected):
                    print(file=sys.stderr, flush=True)


def safe_path(name):
    """Reject archive paths that could escape the extraction directory."""
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or '..' in path.parts
        or '\\' in name
        or re.match(r'^[A-Za-z]:', name)
        or not path.parts
    ):
        raise ValueError(f'Unsafe archive path: {name!r}')
    return path


def extract_archive(source, destination):
    """Extract regular files/directories only, with bounded counts and sizes."""
    destination.mkdir(parents=True, exist_ok=False)
    # A tar containing an intact ZIP can also pass is_zipfile (ZIP permits
    # leading bytes). Recognize the outer tar before probing for ZIP.
    is_zip = not tarfile.is_tarfile(source) and zipfile.is_zipfile(source)
    opener = zipfile.ZipFile if is_zip else tarfile.open
    with opener(source) as archive:
        records, names, total = [], set(), 0
        members = archive.infolist() if is_zip else archive
        for member in members:
            if len(records) >= MAX_MEMBERS:
                raise ValueError('Too many archive entries')
            name = member.filename if is_zip else member.name
            path = safe_path(name)
            if path in names:
                raise ValueError(f'Duplicate archive path: {name}')
            names.add(path)
            directory = member.is_dir() if is_zip else member.isdir()
            if is_zip:
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError(f'Unsafe ZIP entry: {name}')
                size = member.file_size
            else:
                if not (member.isfile() or member.isdir()) or member.issparse():
                    raise ValueError(f'Unsafe tar entry: {name}')
                size = member.size
            if size < 0:
                raise ValueError('Negative archive entry size')
            total += size
            if total > MAX_ARCHIVE:
                raise ValueError('Extracted firmware exceeds size limit')
            records.append((member, path, directory))
        space(destination, total)
        for member, path, directory in records:
            target = destination.joinpath(*path.parts)
            if directory:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                incoming = archive.open(member) if is_zip else archive.extractfile(member)
                with incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, CHUNK_SIZE)


def read_manifest(root):
    """Read the release metadata without accepting an oversized manifest."""
    path = root / 'firmware.json'
    if path.stat().st_size > MAX_MANIFEST_SIZE:
        raise ValueError('Firmware manifest exceeds size limit')
    return json.loads(path.read_text())


def verify_archive(path, expected):
    """Verify archive integrity; this does not establish publisher identity."""
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected.lower():
        raise ValueError('Firmware archive SHA-256 mismatch')


def manufacturer_key(value):
    name = value.strip().casefold()
    return 'telit' if name in ('telit', 'telit cinterion') else name


def validate_manifest(root, modem, release, installer, pattern):
    """Check release compatibility and locate the flashing payload."""
    manifest_path = root / 'firmware.json'
    manifest = read_manifest(root)
    expected = {
        'model': modem['model'],
        'installer': installer,
        'hardware_revision': pattern,
        **{key: release[key] for key in ('igos_version', 'telit_version')},
    }
    for key in ('manufacturer', 'telit_package_version', 'payload_entry'):
        if key in release:
            expected[key] = release[key]
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f'Archive manifest {key} does not match catalog/modem')
    if revision_rank(manifest['hardware_revision'], modem['hardware_revision']) < 0:
        raise ValueError('Archive does not support modem hardware revision')
    entry = manifest.get('payload_entry')
    if entry:
        payload = root.joinpath(*safe_path(entry).parts)
    else:
        candidates = (
            list(root.rglob('contents.xml'))
            if installer == 'tfl'
            else [p for p in root.rglob('*') if p.is_file() and p != manifest_path]
        )
        if len(candidates) != 1:
            raise ValueError('Ambiguous firmware payload; specify payload_entry')
        payload = candidates[0]
    if not payload.exists() or (installer == 'uxfp' and not payload.is_file()):
        raise ValueError('Missing or invalid firmware payload')
    if installer == 'tfl' and payload.name == 'contents.xml':
        payload = payload.parent
    return payload, manifest


def verify_standalone_signature(archive, source):
    if urlsplit(source).scheme in ('http', 'https'):
        signature = fetch_bytes(sidecar_url(source, '.sig'), MAX_SIGNATURE)
        crl = fetch_bytes(sidecar_url(source, '.crl'), MAX_CRL)
    else:
        def read_sidecar(suffix, limit):
            path = Path(str(source) + suffix)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
                raise ValueError(f'Missing or invalid firmware {suffix} sidecar')
            return path.read_bytes()
        signature = read_sidecar('.sig', MAX_SIGNATURE)
        crl = read_sidecar('.crl', MAX_CRL)
    verify_file(archive, signature, crl)


@contextmanager
def prepare_standalone(root, modem, source, force_unsigned=False):
    """Authenticate a standalone archive before extracting and checking compatibility."""
    if force_unsigned and urlsplit(source).scheme:
        raise ValueError('force-unsigned requires a local archive file')
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='adhoc-', dir=root) as temporary:
        work = Path(temporary)
        archive = work / 'download.archive'
        url = urlsplit(source)
        if url.scheme in ('http', 'https'):
            if not url.netloc or url.username or url.password:
                raise ValueError('Invalid archive URL')
            download_archive(source, archive)
        else:
            local = Path(source)
            if not local.is_file() or local.is_symlink():
                raise ValueError('Archive must be a local regular file or HTTP(S) URL')
            size = local.stat().st_size
            if size > MAX_ARCHIVE:
                raise ValueError('Archive exceeds size limit')
            space(work, size)
            shutil.copyfile(local, archive)
        if not force_unsigned:
            verify_standalone_signature(archive, source)
        # The detached signature authenticates these bytes before extraction.
        with archive.open('rb') as stream:
            archive_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        extracted = work / 'payload'
        extract_archive(archive, extracted)
        manifest = read_manifest(extracted)
        # Reuse catalog validation for identifiers, installer and version fields.
        release = {**manifest, 'url': STANDALONE_METADATA_URL, 'archive_sha256': archive_sha256}
        parse_catalog(
            {
                'schema': 1,
                'cellular-firmware': [
                    {
                        'manufacturer': manifest['manufacturer'],
                        'model': manifest['model'],
                        'installer': manifest['installer'],
                        'revisions': [{'hardware_revision': manifest['hardware_revision'], 'firmware': [release]}],
                    }
                ],
            }
        )
        if manufacturer_key(manifest['manufacturer']) != manufacturer_key(modem['manufacturer']):
            raise ValueError('Archive manufacturer does not match modem')
        payload, manifest = validate_manifest(
            extracted, modem, release, manifest['installer'], manifest['hardware_revision']
        )
        yield {'payload': payload, 'installer': manifest['installer'], 'manifest': manifest, 'modem': modem,
               'unsigned': force_unsigned}


def archive_cache_name(release):
    """Use the catalog archive's readable basename, never a URL path."""
    name = unquote(urlsplit(release['url']).path.rsplit('/', 1)[-1])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name) or name in ('.', '..'):
        raise ValueError('Invalid firmware archive filename')
    if not name.endswith(('.tar', '.zip', '.tar.gz', '.tgz', '.tar.xz')):
        raise ValueError('Unsupported firmware archive filename')
    return name


class ArchiveCache:
    """Readable archive cache. Mutations require the updater lock.

    File mtime records last verified use; extraction directories never persist.
    """

    def __init__(self, root, max_archives=DEFAULT_CACHE_ARCHIVES, max_bytes=DEFAULT_CACHE_BYTES):
        if not 1 <= max_archives <= 100 or not 1 <= max_bytes <= MAX_ARCHIVE * 100:
            raise ValueError('Invalid firmware cache limits')
        self.root = Path(root)
        self.max_archives = max_archives
        self.max_bytes = max_bytes

    def path(self, digest, filename=None):
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
            raise ValueError('Invalid cache SHA-256')
        if filename is not None:
            normalized = archive_cache_name({'url': STANDALONE_METADATA_URL.rsplit('/', 1)[0] + '/' + filename})
            if normalized != filename or '/' in filename or '\\' in filename:
                raise ValueError('Invalid cache filename')
            return self.root / filename
        # Existing hash-named files remain usable until migrated on access.
        return self.root / (digest.lower() + '.archive')

    def contains(self, digest, filename=None):
        path = self.path(digest, filename)
        if path.is_file() and not path.is_symlink():
            if filename is None:
                return True
            try:
                return self._digest_file(path).read_text().strip() == digest.lower()
            except OSError:
                return False
        return filename is not None and self.contains(digest)

    @staticmethod
    def _digest_file(path):
        return path.with_name(path.name + '.sha256')

    def _record_digest(self, path, digest):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='.partial-', mode='w', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(digest.lower() + '\n')
            temporary.replace(self._digest_file(path))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _remove(self, path):
        path.unlink()
        self._digest_file(path).unlink(missing_ok=True)

    def entries(self):
        return [path for path in self.root.glob('*')
                if not path.is_symlink() and path.is_file()
                and (re.fullmatch(r'[0-9a-f]{64}\.archive', path.name)
                     or path.name.endswith(('.tar', '.zip', '.tar.gz', '.tgz', '.tar.xz')))]

    def prune(self, incoming_size=0, incoming_count=0):
        # The global operation lock guarantees no other cache copy is active.
        # Remove incomplete copies left by a killed updater before accounting.
        for partial in self.root.glob('.partial-*'):
            if partial.is_file() and not partial.is_symlink():
                partial.unlink()
        entries = sorted(self.entries(), key=lambda path: (path.stat().st_mtime_ns, path.name))
        size = sum(path.stat().st_size for path in entries)
        count = len(entries)
        for path in entries:
            if count + incoming_count <= self.max_archives and size + incoming_size <= self.max_bytes:
                break
            size -= path.stat().st_size
            count -= 1
            self._remove(path)

    def get(self, digest, filename=None):
        self.prune()
        if not self.contains(digest, filename):
            return None
        path = self.path(digest, filename)
        if filename is not None and not path.exists():
            path = self.path(digest)
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest.lower():
            print('Discarding corrupt cached firmware archive; downloading again.')
            self._remove(path)
            return None
        if filename is not None:
            target = self.path(digest, filename)
            if path != target:
                path.replace(target)
                path = target
            self._record_digest(path, digest)
        path.touch()
        print('Using verified cached firmware archive.')
        return path

    def store(self, source, digest, filename=None):
        size = source.stat().st_size
        if size > self.max_bytes:
            print('Firmware archive exceeds cache size budget; not retained.')
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.path(digest, filename)
        if self.contains(digest, filename):
            return
        self.prune(incoming_size=size, incoming_count=1)
        space(self.root, size)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='.partial-', delete=False) as stream:
                temporary = Path(stream.name)
                with source.open('rb') as incoming:
                    shutil.copyfileobj(incoming, stream, CHUNK_SIZE)
                stream.flush()
                os.fsync(stream.fileno())
            # Never publish a partially copied or changed archive.
            with temporary.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest.lower():
                    raise ValueError('Archive changed before cache insertion')
            temporary.replace(target)
            if filename is not None:
                self._record_digest(target, digest)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self, digest, filename=None):
        if not self.contains(digest, filename):
            raise ValueError('Firmware archive is not cached')
        path = self.path(digest, filename)
        if path.exists():
            self._remove(path)
        legacy = self.path(digest)
        if legacy != path and legacy.exists():
            self._remove(legacy)


@contextmanager
def prepare_release(root, vendor, modem, release, installer, pattern, cache=None):
    """Own exactly one transient release directory; always clean our files."""
    parent = root / component(vendor) / component(modem['model']) / component(modem['hardware_revision'])
    parent.mkdir(parents=True, exist_ok=True)
    work = parent / quote(native_version(release['telit_version']), safe='.-')
    # Never delete or reuse a directory left by another invocation/package.
    work.mkdir(mode=0o700, exist_ok=False)
    try:
        archive = None
        if cache is not None:
            try:
                archive = cache.get(release['archive_sha256'], archive_cache_name(release))
            except OSError as error:
                print(f'Firmware cache unavailable; downloading instead: {error}')
        if archive is None:
            archive = work / 'download.archive'
            download_archive(release['url'], archive)
            verify_archive(archive, release['archive_sha256'])
        extracted = work / 'payload'
        extract_archive(archive, extracted)
        payload, manifest = validate_manifest(extracted, modem, release, installer, pattern)
        if cache is not None and archive.parent == work:
            try:
                cache.store(archive, release['archive_sha256'], archive_cache_name(release))
            except (OSError, ValueError) as error:
                # Optional retention must not prevent an otherwise valid update.
                print(f'Unable to retain firmware archive: {error}')
        yield {'payload': payload, 'installer': installer, 'manifest': manifest, 'modem': modem}
    finally:
        shutil.rmtree(work)
