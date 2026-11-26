"""Same-version reinstall policy without modem or target-only imports."""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from tabulate import tabulate
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


class _IncorrectValue(Exception):
    """Stand-in for vyos.opmode.IncorrectValue (not a ValueError)."""


class TestForceReinstall(unittest.TestCase):
    def handler(self, name):
        tree = ast.parse((ROOT/'src/op_mode/wwan_firmware.py').read_text())
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        compatible = {'versions': ['1.0'], 'releases': {'1.0': {'telit_version': 'M0V.060001'}}, 'manufacturer': 'Telit',
                      'vendor': 'telit', 'modem': {}, 'installer': 'uxfp', 'hardware_revision': '1.0'}
        ns = dict(_firmware_update_lock=nullcontext,
                  _get_compatible_firmware=Mock(return_value=compatible),
                  _running_version=Mock(return_value='1.0'), version_key=lambda v: tuple(map(int, v.split('.'))),
                  _get_archive_cache=Mock(return_value=None), FIRMWARE_ROOT='/tmp/unused',
                  prepare_release=Mock(side_effect=lambda *a, **kw: nullcontext({'manifest': {}})),
                  prepare_standalone=Mock(side_effect=lambda *a, **kw: nullcontext({'manifest': {'telit_version': 'native', 'igos_version': '1.0'}})),
                  _get_modem_identity=Mock(return_value={}), get_version_data=lambda: {},
                  _install_firmware_payload=Mock(), print=Mock(), ask_yes_no=Mock(return_value=False), Path=Path,
                  tabulate=tabulate,
                  urlsplit=lambda source: SimpleNamespace(scheme=""),
                  tarfile=SimpleNamespace(TarError=RuntimeError), zipfile=SimpleNamespace(BadZipFile=RuntimeError),
                  vyos=SimpleNamespace(opmode=SimpleNamespace(DataUnavailable=RuntimeError, IncorrectValue=_IncorrectValue)))
        exec(compile(ast.Module(body=[next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_resolve_igos_version'), method], type_ignores=[]), '<handler>', 'exec'), ns)
        return ns

    def test_telit_revision_is_not_a_cli_selector(self):
        ns = self.handler('set_firmware')
        with self.assertRaises(_IncorrectValue):
            ns['set_firmware']('M0V.060001', 'wwan0')
        ns['prepare_release'].assert_not_called()

    def test_show_exposes_both_versions_and_completion_uses_igos(self):
        for quiet, raw in ((False, False), (True, False), (False, True)):
            with self.subTest(quiet=quiet, raw=raw):
                ns = self.handler('show_firmware')
                compatible = ns['_get_compatible_firmware'].return_value
                compatible['modem'] = {'model': 'FN920C04-WW', 'hardware_revision': '1.10',
                                       'firmware_revision': 'M0V.060001'}
                compatible['releases']['1.0'].update(archive_sha256='digest', release_date='2026-11-01')
                cache = Mock()
                cache.contains.return_value = False
                ns['_get_archive_cache'].return_value = cache
                ns['archive_cache_name'] = lambda release: 'archive.tar'
                result = ns['show_firmware'](raw, 'wwan0', quiet=quiet)
                if raw:
                    self.assertEqual(result['running_version'], 'M0V.060001')
                    self.assertEqual(result['versions'][0]['version'], '1.0')
                    self.assertEqual(result['versions'][0]['telit_version'], 'M0V.060001')
                else:
                    output = '\n'.join(str(call.args[0]) for call in ns['print'].call_args_list)
                    if not quiet:
                        self.assertIn('M0V.060001', output)
                    if quiet:
                        self.assertEqual(output, '1.0')
                    else:
                        self.assertIn('1.0', output)
                        self.assertIn('Telit Version', output)
                        self.assertIn('2026-11-01', output)

    def test_delete_uses_igos_version_and_prints_telit_revision(self):
        ns = self.handler('delete_firmware')
        compatible = ns['_get_compatible_firmware'].return_value
        compatible['releases']['1.0']['archive_sha256'] = 'digest'
        cache = Mock()
        cache.contains.return_value = True
        ns['_get_archive_cache'].return_value = cache
        ns['archive_cache_name'] = lambda release: 'archive.tar'
        ns['ask_yes_no'].return_value = True
        ns['delete_firmware']('wwan0', '1.0')
        cache.delete.assert_called_once_with('digest', 'archive.tar')
        ns['print'].assert_any_call('Deleted cached firmware M0V.060001.')

    def test_catalog_same_version_requires_force_including_latest(self):
        for version in ('1.0', 'latest'):
            for force in (False, True):
                with self.subTest(version=version, no_prompt=True):
                    ns = self.handler('set_firmware')
                    ns['ask_yes_no'].return_value = force
                    ns['set_firmware'](version, 'wwan0', no_prompt=True)
                    ns['print'].assert_any_call('Firmware M0V.060001 is already installed.')
                    self.assertEqual(ns['prepare_release'].call_count, int(force))
                    self.assertEqual(ns['_install_firmware_payload'].call_count, int(force))
                    if force:
                        self.assertTrue(ns['_install_firmware_payload'].call_args.kwargs['no_prompt'])

    def downgrade_handler(self, answer):
        ns = self.handler('set_firmware')
        ns['_running_version'] = Mock(return_value='2.0')
        ns['_get_compatible_firmware'].return_value['releases'] = {'1.0': {'telit_version': 'M0V.060001'}, '2.0': {'telit_version': 'M0V.060002'}}
        ns['ask_yes_no'] = Mock(return_value=answer)
        ns['vyos'].opmode.IncorrectValue = _IncorrectValue
        return ns

    def test_downgrade_asks_before_downloading(self):
        for answer in (False, True):
            with self.subTest(answer=answer):
                ns = self.downgrade_handler(answer)
                ns['set_firmware']('1.0', 'wwan0')
                ns['ask_yes_no'].assert_called_once_with('Download and install older firmware M0V.060001?', default=False)
                self.assertEqual(ns['prepare_release'].call_count, int(answer))

    def test_downgrade_without_prompt_is_refused(self):
        ns = self.downgrade_handler(True)
        with self.assertRaisesRegex(_IncorrectValue, 'retry without now'):
            ns['set_firmware']('1.0', 'wwan0', no_prompt=True)
        ns['prepare_release'].assert_not_called()
        ns['ask_yes_no'].assert_not_called()

    def test_archive_force_keeps_authentication_and_confirmation(self):
        for force in (False, True):
            ns = self.handler('update_archive')
            ns['ask_yes_no'].return_value = force
            ns['update_archive']('wwan0', '/archive.tar', no_prompt=True)
            ns['prepare_standalone'].assert_called_once_with('/tmp/unused', {}, '/archive.tar', force_unsigned=False)
            ns['print'].assert_any_call('Firmware native is already installed.')
            ns['ask_yes_no'].assert_called_once_with('Would you like to force install the same version?', default=False)
            self.assertEqual(ns['_install_firmware_payload'].call_count, int(force))
            if force:
                self.assertTrue(ns['_install_firmware_payload'].call_args.kwargs['no_prompt'])

    def test_cli_uses_prompts_instead_of_force_options(self):
        root = ET.parse(ROOT/'op-mode-definitions/wwan-firmware.xml.in').getroot()
        self.assertEqual(root.findall('.//node[@name="force"]'), [])
        self.assertEqual(root.findall('.//node[@name="force-unsigned"]'), [])
        self.assertEqual(len(root.findall('.//leafNode[@name="now"]')), 3)

    def test_unsigned_archive_requires_explicit_answer_even_with_now(self):
        from tempfile import NamedTemporaryFile
        for answer in (False, True):
            with self.subTest(answer=answer), NamedTemporaryFile() as archive:
                ns = self.handler('update_archive')
                ns['get_version_data'] = lambda: {'build_type': 'development', 'wwan_firmware_allow_unsigned': True}
                ns['ask_yes_no'].return_value = answer
                ns['update_archive']('wwan0', archive.name, no_prompt=True)
                self.assertEqual(ns['prepare_standalone'].call_count, int(answer))
                if answer:
                    self.assertTrue(ns['prepare_standalone'].call_args.kwargs['force_unsigned'])
                else:
                    ns['_install_firmware_payload'].assert_not_called()
