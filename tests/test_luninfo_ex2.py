"""
Tests for extended LUN info, version 2 (system operation 0x83), and the
behaviour it must not change (GetLUNsInfoEx, op 0x81, and drobom info volumes.)

No Drobo hardware is needed: see test_regression for the fake device.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v
"""

import ast
import os
import struct
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (FakeDevice, make_drobo, LUNExDevice, lunex_entry,
                             lunex_reply, legacy_subpage, setUpModule,
                             tearDownModule, TOP)

import Drobo

# every 0xEA system operation that is known to be a pure read.
DOCUMENTED_READS = {0x81, 0x83}


def load_drobom_functions(*names):
    """ the named top level functions of the drobom script, without running
        the script itself. """
    with open(os.path.join(TOP, 'drobom')) as f:
        tree = ast.parse(f.read())
    keep = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {'Drobo': Drobo}
    exec(compile(ast.Module(body=keep, type_ignores=[]), 'drobom', 'exec'), ns)
    return ns


def run_printvolumes(d):
    ns = load_drobom_functions('togig', 'printvolumes')
    out = []
    ns['print'] = lambda *a: out.append(' '.join(str(x) for x in a))
    ns['printvolumes'](d)
    return out


class TestExistingBehaviour(unittest.TestCase):
    """ regression: pins behaviour from before op 0x83 was known. """

    def test_ex_cdb_unchanged_without_ex2_feature(self):
        dev = LUNExDevice([lunex_entry(0)])
        d = make_drobo(dev)
        dev.cdbs.clear()
        d.GetLUNsInfoEx()
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0xea, 0x10, 0x80, 0x81, 0, 0, 0x21,
                        1024, 0)
        ])

    def test_ex_fields_unchanged(self):
        d = make_drobo(LUNExDevice([lunex_entry(2, name=b'Media', fmt=0x02,
                                                total=3 * 10**12,
                                                second=10**12, lun=5)]))
        self.assertEqual(d.GetLUNsInfoEx(), [{
            'index': 2, 'lun': 5, 'name': 'Media', 'flags_a': 0, 'flags_b': 1,
            'total_capacity': 3 * 10**12, 'second_capacity': 10**12,
            'format': 0x02, 'format_name': ['NTFS']}])

    def test_allow_list_holds_only_documented_reads(self):
        ops = Drobo.Drobo._SYSTEM_READ_OPS
        self.assertIn(0x81, ops)
        self.assertTrue(set(ops) <= DOCUMENTED_READS, ops)

    def test_every_other_system_operation_is_refused(self):
        dev = LUNExDevice([lunex_entry(0)])
        d = make_drobo(dev)
        dev.cdbs.clear()
        for op in range(256):
            if op in Drobo.Drobo._SYSTEM_READ_OPS:
                continue
            with self.assertRaises(Drobo.DroboException):
                d._Drobo__system_read(op, 0, 1024)
        self.assertEqual(dev.cdbs, [])

    def test_printvolumes_ex(self):
        d = make_drobo(LUNExDevice([lunex_entry(0, name=b'Volume 1')]))
        self.assertEqual(run_printvolumes(d), [
            'LUN  Size(GB)  Name                              Format',
            '  0     17592  Volume 1                          EXT3'])

    def test_printvolumes_unsupported(self):
        d = make_drobo(LUNExDevice([], supported=False))
        self.assertEqual(run_printvolumes(d), [
            'extended volume information is not supported by this firmware'])

    def test_info_volumes_simulation_output(self):
        r = subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                            '-v', '128', 'info', 'volumes'],
                           cwd=TOP, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn(
            'LUN  Size(GB)  Name                              Format\n'
            '  0     17592  Drobo volume 1                    EXT3\n', r.stdout)

    def test_known_feature_names_unchanged(self):
        self.assertEqual(Drobo._unitfeatures(0x4000 | 0x0400 | 0x20000),
                         ['SUPPORTS_NEW_LUNINFO2', 'SUPPORTS_OPTIONS2',
                          'SUPPORTS_ISCSI'])


if __name__ == '__main__':
    unittest.main()
