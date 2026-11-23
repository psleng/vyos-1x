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

"""HTTP downloads resume after stalls and dropped connections."""

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from unittest.mock import patch

# Optional on developer workstations, but imported by vyos package startup.
try:
    __import__('cracklib')
except ModuleNotFoundError:
    cracklib = types.ModuleType('cracklib')
    cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = cracklib

from vyos import remote  # noqa: E402

PAYLOAD = os.urandom(300_000)


class _Handler(BaseHTTPRequestHandler):
    # per-test behaviour, set on the server: list of actions per GET
    # ('drop', n) send n bytes then close; ('stall', n) send n bytes then hang;
    # 'full' serve normally
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.server.payload)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', self.server.etag)
        self.end_headers()

    def do_GET(self):
        server = self.server
        payload = server.payload
        server.requests.append(self.headers.get('Range'))
        server.if_range.append(self.headers.get('If-Range'))
        if server.status:
            self.send_error(server.status)
            return
        action = server.actions.pop(0) if server.actions else 'full'
        start = 0
        rng = self.headers.get('Range')
        if_range = self.headers.get('If-Range')
        if rng and server.ranges and if_range in (None, server.etag):
            start = int(rng.split('=')[1].rstrip('-'))
            if start >= len(payload):
                self.send_error(416)
                return
            self.send_response(206)
            self.send_header(
                'Content-Range', f'bytes {start}-{len(payload) - 1}/{len(payload)}'
            )
        else:
            self.send_response(200)
        self.send_header('ETag', server.etag)
        body = payload[start:]
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if action == 'full':
            self.wfile.write(body)
            return
        kind, count = action
        self.wfile.write(body[:count])
        self.wfile.flush()
        if kind == 'stall':
            server.release.wait(5)
        self.close_connection = True


class TestHttpResume(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.daemon_threads = True
        self.server.actions = []
        self.server.requests = []
        self.server.ranges = True
        self.server.status = 0
        self.server.payload = PAYLOAD
        self.server.etag = '"v1"'
        self.server.if_range = []
        self.server.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.target = os.path.join(self.tmp.name, 'image.iso')
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/image.iso'
        # no real backoff waits in tests
        self.sleep = patch.object(remote.time, 'sleep')
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def download(self, timeout=10.0):
        remote.download(self.target, self.url, timeout=timeout, raise_error=True)
        with open(self.target, 'rb') as f:
            return f.read()

    def test_dropped_connection_resumes_from_offset(self):
        self.server.actions = [('drop', 100_000), ('drop', 50_000)]
        self.assertEqual(self.download(), PAYLOAD)
        self.assertEqual(self.server.requests, [None, 'bytes=100000-', 'bytes=150000-'])

    def test_stall_longer_than_read_timeout_resumes(self):
        self.server.actions = [('stall', 120_000)]
        start = time.monotonic()
        self.assertEqual(self.download(timeout=0.5), PAYLOAD)
        self.assertLess(time.monotonic() - start, 4)
        # resumes from what was written to disk (whole chunks), not from what
        # the server sent
        offset = int(self.server.requests[-1].split('=')[1].rstrip('-'))
        self.assertTrue(0 < offset <= 120_000)

    def test_server_without_range_support_restarts(self):
        self.server.ranges = False
        self.server.actions = [('drop', 100_000)]
        self.assertEqual(self.download(), PAYLOAD)
        self.assertEqual(self.server.requests, [None, 'bytes=100000-'])

    def test_client_error_is_not_retried(self):
        self.server.status = 404
        with self.assertRaises(remote.requests_exceptions.HTTPError):
            self.download()
        self.assertEqual(len(self.server.requests), 1)

    def test_gives_up_when_no_data_arrives(self):
        self.server.actions = [('drop', 0)] * (remote.DOWNLOAD_RETRIES + 5)
        with self.assertRaises(remote.RESUMABLE_ERRORS):
            self.download()
        self.assertEqual(len(self.server.requests), remote.DOWNLOAD_RETRIES + 1)


if __name__ == '__main__':
    unittest.main()


class TestFirmwareArchiveResume(TestHttpResume):
    """Same scenarios for the cellular firmware archive downloader."""

    def setUp(self):
        super().setUp()
        from vyos.utils.wwan import interfaces_wwan_firmware as firmware

        self.firmware = firmware
        self.fw_sleep = patch.object(firmware.time, 'sleep')
        self.fw_sleep.start()

    def tearDown(self):
        self.fw_sleep.stop()
        super().tearDown()

    def download(self, timeout=10.0):
        from pathlib import Path

        with patch.object(self.firmware, 'DOWNLOAD_TIMEOUT', timeout):
            self.firmware.download_archive(self.url, Path(self.target))
        with open(self.target, 'rb') as f:
            return f.read()

    def test_client_error_is_not_retried(self):
        from urllib.error import HTTPError

        self.server.status = 404
        with self.assertRaises(HTTPError):
            self.download()
        self.assertEqual(len(self.server.requests), 1)

    def test_gives_up_when_no_data_arrives(self):
        self.server.actions = [('drop', 0)] * (self.firmware.DOWNLOAD_RETRIES + 5)
        with self.assertRaises(Exception):
            self.download()
        self.assertEqual(len(self.server.requests), self.firmware.DOWNLOAD_RETRIES + 1)


class TestHttpResumeAcrossRuns(TestHttpResume):
    """A failed download is kept and the next run continues it."""

    def run_download(self, resume=True):
        remote.download(self.target, self.url, raise_error=True, resume=resume)
        with open(self.target, 'rb') as f:
            return f.read()

    def fail_first_run(self, keep=100_000):
        # data then nothing at all, until the retry budget is spent
        self.server.actions = [('drop', keep)] + [('drop', 0)] * (
            remote.DOWNLOAD_RETRIES + 1
        )
        with self.assertRaises(remote.RESUMABLE_ERRORS):
            self.run_download()
        self.assertEqual(os.path.getsize(self.target), keep)
        self.assertTrue(os.path.exists(self.target + '.resume'))
        self.server.requests.clear()
        self.server.if_range.clear()
        self.server.actions = []

    def test_next_run_continues_from_partial_file(self):
        self.fail_first_run()
        self.assertEqual(self.run_download(), PAYLOAD)
        self.assertEqual(self.server.requests, ['bytes=100000-'])
        self.assertEqual(self.server.if_range, ['"v1"'])

    def test_changed_file_on_server_restarts(self):
        self.fail_first_run()
        self.server.payload = os.urandom(len(PAYLOAD))
        self.server.etag = '"v2"'
        self.assertEqual(self.run_download(), self.server.payload)
        self.assertEqual(self.server.requests, [None])

    def test_complete_file_is_not_downloaded_again(self):
        self.assertEqual(self.run_download(), PAYLOAD)
        self.server.requests.clear()
        self.assertEqual(self.run_download(), PAYLOAD)
        self.assertEqual(self.server.requests, [])

    def test_without_resume_flag_starts_over(self):
        self.fail_first_run()
        self.assertEqual(self.run_download(resume=False), PAYLOAD)
        self.assertEqual(self.server.requests, [None])


class TestFirmwareResumeAcrossRuns(unittest.TestCase):
    """Cellular firmware archives resume across runs, keyed by SHA-256."""

    def setUp(self):
        import hashlib
        from pathlib import Path

        from vyos.utils.wwan import interfaces_wwan_firmware as firmware

        self.firmware = firmware
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.daemon_threads = True
        for name, value in dict(
            actions=[],
            requests=[],
            if_range=[],
            ranges=True,
            status=0,
            payload=PAYLOAD,
            etag='"v1"',
        ).items():
            setattr(self.server, name, value)
        self.server.release = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.partials = Path(self.tmp.name) / 'partials'
        self.work = Path(self.tmp.name) / 'work'
        self.work.mkdir()
        self.release = {
            'url': f'http://127.0.0.1:{self.server.server_address[1]}/fw.tar',
            'archive_sha256': hashlib.sha256(PAYLOAD).hexdigest(),
        }
        self.patches = [
            patch.object(firmware, 'PARTIAL_DOWNLOAD_DIR', self.partials),
            patch.object(firmware.time, 'sleep'),
            patch.object(firmware, 'print', create=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def partial(self):
        return self.partials / f'{self.release["archive_sha256"]}.part'

    def fail_first_run(self, keep=100_000):
        self.server.actions = [('drop', keep)] + [('drop', 0)] * (
            self.firmware.DOWNLOAD_RETRIES + 1
        )
        with self.assertRaises(Exception):
            self.firmware._download_release_archive(self.release, self.work)
        self.assertEqual(self.partial().stat().st_size, keep)
        self.server.requests.clear()
        self.server.actions = []

    def test_next_run_continues_and_verifies(self):
        self.fail_first_run()
        archive = self.firmware._download_release_archive(self.release, self.work)
        self.assertEqual(archive.read_bytes(), PAYLOAD)
        self.assertEqual(self.server.requests, ['bytes=100000-'])
        self.assertFalse(self.partial().exists())

    def test_mismatching_result_is_discarded(self):
        self.fail_first_run()
        # the server's archive no longer matches the catalog digest
        self.server.payload = os.urandom(len(PAYLOAD))
        with self.assertRaises(ValueError):
            self.firmware._download_release_archive(self.release, self.work)
        self.assertFalse(self.partial().exists())

    def test_complete_partial_from_killed_run_is_used(self):
        self.partials.mkdir()
        self.partial().write_bytes(PAYLOAD)
        archive = self.firmware._download_release_archive(self.release, self.work)
        self.assertEqual(archive.read_bytes(), PAYLOAD)
        self.assertEqual(self.server.requests, [f'bytes={len(PAYLOAD)}-'])

    def test_partials_of_other_archives_are_removed(self):
        self.partials.mkdir()
        other = self.partials / f'{"0" * 64}.part'
        other.write_bytes(b'old')
        self.firmware._download_release_archive(self.release, self.work)
        self.assertFalse(other.exists())
