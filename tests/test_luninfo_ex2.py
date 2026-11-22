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


# --- version 2 (op 0x83) fixtures ---------------------------------------------

EX2_FEATURE = 0x800000
_FEATURE_OFFSET = 4 + struct.calcsize('>BBHBB32s16s16s') + 112


def firmware_with(extra_features):
    """ sub-page responder: a legacy unit whose firmware page also
        advertises extra_features. """
    def responder(sub_page):
        page = legacy_subpage(sub_page)
        if sub_page != 0x08:
            return page
        f, = struct.unpack_from('>L', page, _FEATURE_OFFSET)
        return (page[:_FEATURE_OFFSET] + struct.pack('>L', f | extra_features)
                + page[_FEATURE_OFFSET + 4:])
    return responder


def lunex2_entry(index, initiators=(), version=2, **kw):
    """ a 285 byte entry whose count byte (offset 263) says how many
        224 byte names follow it and, from reply version 2, as many
        4 byte addresses after those. """
    e = bytearray(lunex_entry(index, **kw))
    e[263] = len(initiators)
    names = b''.join(n.ljust(224, b'\0') for n, a in initiators)
    addrs = b''.join(bytes(a) for n, a in initiators) if version >= 2 else b''
    return bytes(e) + names + addrs


def lunex2_reply(entries, more=False, version=2):
    return struct.pack('BBB', version, len(entries), 1 if more else 0) + \
        b''.join(entries)


class LUNEx2Device(FakeDevice):
    """ unit answering op 0x83 (and op 0x81, from ex_luns), paged. """

    def __init__(self, luns, ex_luns=None, per_reply=3, version=2,
                 feature=True, ex2_works=True, ex_works=True,
                 always_more=False, truncate=None):
        super().__init__(
            responder=firmware_with(EX2_FEATURE if feature else 0))
        self.luns = luns  # (index, entry bytes)
        self.ex_luns = ex_luns or []
        self.per_reply = per_reply
        self.version = version
        self.ex2_works = ex2_works
        self.ex_works = ex_works
        self.always_more = always_more
        self.truncate = truncate
        self.ea_responder = self.respond_ea

    def respond_ea(self, mcb, sz, out):
        start = mcb[4]
        if mcb[3] == 0x81:
            if not self.ex_works:
                raise IOError("io_hdr status is: 2")
            rest = [e for e in self.ex_luns if e[0] >= start]
            return lunex_reply(rest[:self.per_reply],
                               len(rest) > self.per_reply)[:sz]
        if mcb[3] == 0x83:
            if not self.ex2_works:
                raise IOError("io_hdr status is: 2")
            rest = [e for i, e in self.luns if i >= start]
            more = self.always_more or len(rest) > self.per_reply
            reply = lunex2_reply(rest[:self.per_reply], more, self.version)
            return reply[:min(sz, self.truncate or sz)]
        raise IOError("unexpected system operation 0x%02x" % mcb[3])


def ex2(index, initiators=(), version=2, **kw):
    return (index, lunex2_entry(index, initiators, version, **kw))


IQN_A = b'iqn.1991-05.com.example:host-a'
IQN_B = b'iqn.1991-05.com.example:host-b'


class TestLUNsInfoEx2(unittest.TestCase):
    """ GetLUNsInfoEx2: extended LUN info version 2, op 0x83 (new.) """

    def test_feature_name(self):
        self.assertEqual(Drobo._unitfeatures(EX2_FEATURE),
                         ['SUPPORTS_LUNINFO_EX2'])

    def test_allow_list_is_exactly_the_documented_reads(self):
        self.assertEqual(set(Drobo.Drobo._SYSTEM_READ_OPS), DOCUMENTED_READS)

    def test_exact_cdb(self):
        """ byte for byte what the vendor's software sends: a read (0x80)
            system operation 0x83 from LUN 0, flags 0x21, 31744 (0x7c00)
            bytes, last byte 1. """
        dev = LUNEx2Device([ex2(0)])
        d = make_drobo(dev)
        dev.cdbs.clear()
        d.GetLUNsInfoEx2()
        self.assertEqual(dev.cdbs, [
            bytes([0xea, 0x10, 0x80, 0x83, 0x00, 0x00, 0x21, 0x7c, 0x00, 0x01])
        ])

    def test_fields_without_initiators(self):
        d = make_drobo(LUNEx2Device([ex2(1, name=b'Volume 2', fmt=0x04,
                                         lun=7)]))
        self.assertEqual(d.GetLUNsInfoEx2(), [{
            'index': 1, 'lun': 7, 'name': 'Volume 2', 'flags_a': 0,
            'flags_b': 1, 'total_capacity': 17592186044416,
            'second_capacity': 4398046511104, 'format': 0x04,
            'format_name': ['HFS'], 'version': 2, 'ex2_flag': False,
            'initiators': []}])

    def test_initiators_version_2(self):
        luns = [ex2(0, [(IQN_A, (192, 168, 1, 10)),
                        (IQN_B, (10, 0, 0, 2))]),
                ex2(1, [(IQN_A, (192, 168, 1, 10))], name=b'second')]
        l = make_drobo(LUNEx2Device(luns)).GetLUNsInfoEx2()
        self.assertEqual(l[0]['initiators'], [
            {'name': IQN_A.decode(), 'address': '192.168.1.10'},
            {'name': IQN_B.decode(), 'address': '10.0.0.2'}])
        # the second entry starts after the first one's names and addresses
        self.assertEqual(l[1]['name'], 'second')
        self.assertEqual(l[1]['initiators'], [
            {'name': IQN_A.decode(), 'address': '192.168.1.10'}])

    def test_initiators_version_1_have_no_addresses(self):
        luns = [ex2(0, [(IQN_A, (1, 2, 3, 4))], version=1),
                ex2(1, version=1, name=b'next')]
        l = make_drobo(LUNEx2Device(luns, version=1)).GetLUNsInfoEx2()
        self.assertEqual(l[0]['initiators'],
                         [{'name': IQN_A.decode(), 'address': None}])
        self.assertEqual(l[0]['version'], 1)
        self.assertEqual(l[1]['name'], 'next')

    def test_ex2_flag_needs_version_2(self):
        e2 = bytearray(lunex2_entry(0))
        e2[264] |= 4
        l = make_drobo(LUNEx2Device([(0, bytes(e2))])).GetLUNsInfoEx2()
        self.assertTrue(l[0]['ex2_flag'])
        e1 = bytearray(lunex2_entry(0, version=1))
        e1[264] |= 4
        l = make_drobo(LUNEx2Device([(0, bytes(e1))],
                                    version=1)).GetLUNsInfoEx2()
        self.assertFalse(l[0]['ex2_flag'])

    def test_paging(self):
        luns = [ex2(i, [(IQN_A, (1, 1, 1, i))] * (i % 3)) for i in range(7)]
        dev = LUNEx2Device(luns, per_reply=3)
        d = make_drobo(dev)
        dev.cdbs.clear()
        l = d.GetLUNsInfoEx2()
        self.assertEqual([e['index'] for e in l], list(range(7)))
        self.assertEqual([len(e['initiators']) for e in l],
                         [i % 3 for i in range(7)])
        self.assertEqual([c[4] for c in dev.cdbs], [0, 3, 6])
        self.assertTrue(all(c[3] == 0x83 for c in dev.cdbs))

    def test_paging_is_bounded(self):
        dev = LUNEx2Device([ex2(0)], always_more=True)
        d = make_drobo(dev)
        dev.cdbs.clear()
        d.GetLUNsInfoEx2()
        self.assertLessEqual(len(dev.cdbs), 32)

    def test_truncated_entry_is_dropped(self):
        """ an entry whose names run past the end of the reply is not
            decoded. """
        dev = LUNEx2Device([ex2(0, [(IQN_A, (1, 2, 3, 4))])], truncate=300)
        self.assertEqual(make_drobo(dev).GetLUNsInfoEx2(), [])

    def test_not_sent_without_feature(self):
        """ like the vendor's software, only ask units that advertise it. """
        dev = LUNEx2Device([ex2(0)], feature=False)
        d = make_drobo(dev)
        dev.cdbs.clear()
        self.assertIsNone(d.GetLUNsInfoEx2())
        self.assertEqual(dev.cdbs, [])

    def test_unsupported_returns_none(self):
        self.assertIsNone(
            make_drobo(LUNEx2Device([], ex2_works=False)).GetLUNsInfoEx2())

    def test_disallowed_ops_still_send_nothing(self):
        dev = LUNEx2Device([ex2(0)])
        d = make_drobo(dev)
        dev.cdbs.clear()
        for op in (0x00, 0x02, 0x03, 0x06, 0x0f, 0x16, 0x17, 0x80, 0x82,
                   0x84, 0xff):
            with self.assertRaises(Drobo.DroboException):
                d._Drobo__system_read(op, 0, 1024)
        self.assertEqual(dev.cdbs, [])


class TestBestLUNsInfo(unittest.TestCase):
    """ selection rule: op 0x83 when the firmware advertises feature
        0x800000 (as the vendor's software does), else op 0x81. """

    def test_prefers_ex2_with_feature(self):
        dev = LUNEx2Device([ex2(0, name=b'two')],
                           ex_luns=[lunex_entry(0, name=b'one')])
        d = make_drobo(dev)
        dev.cdbs.clear()
        l = d.GetLUNsInfoBest()
        self.assertEqual(l[0]['name'], 'two')
        self.assertEqual([c[3] for c in dev.cdbs], [0x83])

    def test_ex_without_feature(self):
        dev = LUNEx2Device([ex2(0, name=b'two')], feature=False,
                           ex_luns=[lunex_entry(0, name=b'one')])
        d = make_drobo(dev)
        dev.cdbs.clear()
        l = d.GetLUNsInfoBest()
        self.assertEqual(l[0]['name'], 'one')
        self.assertEqual([c[3] for c in dev.cdbs], [0x81])

    def test_falls_back_to_ex_when_ex2_fails(self):
        dev = LUNEx2Device([], ex2_works=False,
                           ex_luns=[lunex_entry(0, name=b'one')])
        d = make_drobo(dev)
        dev.cdbs.clear()
        self.assertEqual(d.GetLUNsInfoBest()[0]['name'], 'one')
        self.assertEqual([c[3] for c in dev.cdbs], [0x83, 0x81])

    def test_none_when_neither_works(self):
        d = make_drobo(LUNEx2Device([], ex2_works=False, ex_works=False))
        self.assertIsNone(d.GetLUNsInfoBest())

    def test_printvolumes_uses_ex2_and_lists_initiators(self):
        dev = LUNEx2Device([ex2(0, [(IQN_A, (192, 168, 1, 10))],
                                name=b'Volume 1')],
                           ex_luns=[lunex_entry(0, name=b'wrong')])
        self.assertEqual(run_printvolumes(make_drobo(dev)), [
            'LUN  Size(GB)  Name                              Format',
            '  0     17592  Volume 1                          EXT3',
            '      initiator: %s (192.168.1.10)' % IQN_A.decode()])

    def test_printvolumes_falls_back_to_ex(self):
        dev = LUNEx2Device([], ex2_works=False,
                           ex_luns=[lunex_entry(0, name=b'Volume 1')])
        self.assertEqual(run_printvolumes(make_drobo(dev)), [
            'LUN  Size(GB)  Name                              Format',
            '  0     17592  Volume 1                          EXT3'])


if __name__ == '__main__':
    unittest.main()
