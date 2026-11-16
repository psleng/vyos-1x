"""Same-version reinstall policy without modem or target-only imports."""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


class TestForceReinstall(unittest.TestCase):
    def handler(self, name):
        tree = ast.parse((ROOT/'src/op_mode/wwan_firmware.py').read_text())
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        compatible = {'versions': ['1.0'], 'releases': {'1.0': {}}, 'manufacturer': 'Telit',
                      'vendor': 'telit', 'modem': {}, 'installer': 'uxfp', 'hardware_revision': '1.0'}
        ns = dict(_firmware_update_lock=nullcontext,
                  _get_compatible_firmware=Mock(return_value=compatible),
                  _running_version=Mock(return_value='1.0'), version_key=lambda v: tuple(map(int, v.split('.'))),
                  _get_archive_cache=Mock(return_value=None), FIRMWARE_ROOT='/tmp/unused',
                  prepare_release=Mock(side_effect=lambda *a, **kw: nullcontext({'manifest': {}})),
                  prepare_standalone=Mock(side_effect=lambda *a, **kw: nullcontext({'manifest': {'telit_version': 'native'}})),
                  _get_modem_identity=Mock(return_value={}), get_version_data=lambda: {},
                  _install_firmware_payload=Mock(), print=Mock(),
                  tarfile=SimpleNamespace(TarError=RuntimeError), zipfile=SimpleNamespace(BadZipFile=RuntimeError),
                  vyos=SimpleNamespace(opmode=SimpleNamespace(DataUnavailable=RuntimeError)))
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<handler>', 'exec'), ns)
        return ns

    def test_catalog_same_version_requires_force_including_latest(self):
        for version in ('1.0', 'latest'):
            for force in (False, True):
                with self.subTest(version=version, force=force):
                    ns = self.handler('set_firmware')
                    ns['set_firmware'](version, 'wwan0', force=force)
                    self.assertEqual(ns['prepare_release'].call_count, int(force))
                    self.assertEqual(ns['_install_firmware_payload'].call_count, int(force))
                    if force:
                        self.assertFalse(ns['_install_firmware_payload'].call_args.kwargs['no_prompt'])

    def test_archive_force_keeps_authentication_and_confirmation(self):
        for force in (False, True):
            ns = self.handler('update_archive')
            ns['update_archive']('wwan0', '/archive.tar', force=force)
            ns['prepare_standalone'].assert_called_once_with('/tmp/unused', {}, '/archive.tar', force_unsigned=False)
            self.assertEqual(ns['_install_firmware_payload'].call_count, int(force))
            if force:
                self.assertFalse(ns['_install_firmware_payload'].call_args.kwargs['no_prompt'])

    def test_cli_force_nodes_preserve_existing_flags(self):
        root = ET.parse(ROOT/'op-mode-definitions/wwan-firmware.xml.in').getroot()
        nodes = root.findall('.//node[@name="force"]')
        self.assertEqual(len(nodes), 3)
        for node in nodes:
            command = node.findtext('command')
            self.assertTrue(command.endswith(' --force'))
            self.assertEqual(node.findtext('children/leafNode[@name="now"]/command'), command+' --no-prompt')
