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

"""`add system image latest` downgrade prompt and image download files.

The functions are extracted from the op-mode script so the test does not
need the installer's runtime dependencies.
"""

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'src/op_mode/image_installer.py'


def load(*names, **namespace):
    tree = ast.parse(SCRIPT.read_text())
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(
        compile(ast.Module(body=body, type_ignores=[]), str(SCRIPT), 'exec'), namespace
    )
    return namespace


class TestConfirmLatest(unittest.TestCase):
    def confirm(self, latest, running, no_prompt=False, answer=False):
        from vyos.version import compare_build_versions

        version = types.ModuleType('vyos.version')
        version.compare_build_versions = compare_build_versions
        version.get_version = lambda: running
        ask = Mock(return_value=answer)
        saved = sys.modules.get('vyos.version')
        sys.modules['vyos.version'] = version
        try:
            ns = load('_confirm_latest_version', ask_yes_no=ask, print=Mock())
            return ns['_confirm_latest_version'](latest, no_prompt), ask
        finally:
            sys.modules['vyos.version'] = saved

    def test_newer_release_proceeds_without_asking(self):
        ok, ask = self.confirm('1.5-rolling-202611180000', '999.202611171841')
        self.assertTrue(ok)
        ask.assert_not_called()

    def test_older_release_asks_and_respects_answer(self):
        for answer in (False, True):
            ok, ask = self.confirm(
                '1.5-rolling-202611160734', '999.202611171841', answer=answer
            )
            self.assertEqual(ok, answer)
            ask.assert_called_once()

    def test_same_release_asks(self):
        ok, ask = self.confirm('999.202611171841', '999.202611171841')
        self.assertFalse(ok)
        ask.assert_called_once()

    def test_older_release_without_prompt_declines(self):
        ok, ask = self.confirm(
            '1.5-rolling-202611160734', '999.202611171841', no_prompt=True
        )
        self.assertFalse(ok)
        ask.assert_not_called()

    def test_unorderable_versions_proceed(self):
        ok, ask = self.confirm('', '999.202611171841')
        self.assertTrue(ok)
        ask.assert_not_called()


class TestImageDownloadFiles(unittest.TestCase):
    def test_remove_downloaded_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / 'a.iso'
            for suffix in ('', '.minisig', '.resume'):
                Path(f'{iso}{suffix}').write_text('x')
            ns = load('_remove_downloaded_image', Path=Path, ISO_DOWNLOAD_PATH=str(iso))
            ns['_remove_downloaded_image']()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_nothing_downloaded_is_a_no_op(self):
        ns = load('_remove_downloaded_image', Path=Path, ISO_DOWNLOAD_PATH='')
        ns['_remove_downloaded_image']()

    def test_download_path_is_stable_and_keeps_only_this_url(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            ns = load(
                '_image_download_path',
                Path=Path,
                IMAGE_DOWNLOAD_DIR=tmp,
                IMAGE_DOWNLOAD_KEEP_DAYS=7,
            )
            path = ns['_image_download_path']('https://x/a.iso')
            self.assertEqual(path, ns['_image_download_path']('https://x/a.iso'))
            Path(path).write_text('partial')
            Path(f'{path}.resume').write_text('{}')

            # same URL again: the partial survives
            ns['_image_download_path']('https://x/a.iso')
            self.assertTrue(Path(path).exists())

            # a week-old partial is dropped
            old = time.time() - 8 * 86400
            os.utime(path, (old, old))
            ns['_image_download_path']('https://x/a.iso')
            self.assertFalse(Path(path).exists())

            # another URL drops the previous partial and its record
            Path(path).write_text('partial')
            other = ns['_image_download_path']('https://x/b.iso')
            self.assertNotEqual(other, path)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
