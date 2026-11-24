#!/usr/bin/env python3
"""
The ATA tunnel transport (DroboIOctl), and the pass-through behaviour it
must leave alone.

No hardware is touched: open() on the device file and fcntl.ioctl are
replaced as in test_cov_ioctl, and sysfs is a temporary directory tree
(DroboIOctl.SYSFS_ROOT).

Part 1 (regression) pins what pass-through sends today, sg_io_hdr by
sg_io_hdr, for devices that must not use the tunnel.

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import ctypes
import io
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from test_regression import FakeDevice, legacy_subpage, gen2_responder, SLOTS
from test_cov_ioctl import FakeFile, fake_open, field_address, ALL_DEBUG

import Drobo
import DroboIOctl
from DroboIOctl import sg_io_hdr

TO_DEV = sg_io_hdr.SG_DXFER_TO_DEV
FROM_DEV = sg_io_hdr.SG_DXFER_FROM_DEV

GEN1 = ('TRUSTED ', 'Mass Storage    ')
GEN2 = ('USB 3.0 ', 'Drobo S - Gen 2 ')


class FakeSysfs:
    """ a sysfs tree in a temporary directory, used as DroboIOctl.SYSFS_ROOT.
        Like the kernel's, vendor and model end in a newline. """

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        p = mock.patch.object(DroboIOctl, 'SYSFS_ROOT', self.root,
                              create=True)
        p.start()
        test.addCleanup(p.stop)

    def device(self, name, vendor, model, cls='block'):
        d = os.path.join(self.root, 'class', cls, name, 'device')
        os.makedirs(d)
        for field, text in (('vendor', vendor), ('model', model)):
            with open(os.path.join(d, field), 'w') as f:
                f.write(text + '\n')

    def partition(self, name):
        d = os.path.join(self.root, 'class', 'block', name)
        os.makedirs(d)
        with open(os.path.join(d, 'partition'), 'w') as f:
            f.write('1\n')


class SgIoRecorder:
    """ stand-in for fcntl.ioctl that records every SG_IO request and answers
        data-in commands through answer(cdb, length) -> (bytes, status). """

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or (lambda cdb, length, payload: (b'', 0))

    def __call__(self, fd, request, hdr, mutate=False):
        assert request == sg_io_hdr.SG_IO, hex(request)
        rec = {
            'cdb': ctypes.string_at(field_address(hdr, 'cmdp'), hdr.cmd_len),
            'direction': hdr.dxfer_direction,
            'dxfer_len': hdr.dxfer_len,
        }
        buf = field_address(hdr, 'dxferp')
        if hdr.dxfer_direction == TO_DEV:
            rec['payload'] = ctypes.string_at(buf, hdr.dxfer_len)
            data, status = self.answer(rec['cdb'], hdr.dxfer_len,
                                       rec['payload'])
        else:
            data, status = self.answer(rec['cdb'], hdr.dxfer_len, None)
            data = data[:hdr.dxfer_len]
            ctypes.memmove(buf, data, len(data))
            hdr.resid = hdr.dxfer_len - len(data)
        hdr.status = status
        self.calls.append(rec)
        return 0


def device_answer(dev):
    """ an SgIoRecorder answer that passes CDBs through to a FakeDevice. """

    def answer(cdb, length, payload):
        try:
            return dev.get_sub_page(length, cdb, payload is not None, 0), 0
        except IOError:
            return b'', 2  # CHECK CONDITION

    return answer


def open_dev(path='/dev/sdq', *args, **kw):
    with mock.patch.object(DroboIOctl, 'open', fake_open, create=True):
        return DroboIOctl.DroboIOctl(path, *args, **kw)


def real_drobo(path, recorder, debugflags=0):
    """ a Drobo.Drobo over the real DroboIOctl, with fake open and ioctl. """
    with mock.patch.object(DroboIOctl, 'open', fake_open, create=True), \
         mock.patch.object(DroboIOctl, 'ioctl', recorder):
        return Drobo.Drobo(path, debugflags)


def no_env(test):
    p = mock.patch.dict(os.environ)
    p.start()
    test.addCleanup(p.stop)
    os.environ.pop('DROBO_UTILS_TRANSPORT', None)


# --- Part 1: regression, pass-through unchanged ------------------------------

CONFIG_LEGACY = bytes.fromhex('5a003a01000000000f00')


class TestPassThroughUnchanged(unittest.TestCase):
    """ devices that are not tunnel identities send exactly what drobo-utils
        has always sent: one SG_IO per command, CDB as built by Drobo.py. """

    def setUp(self):
        no_env(self)
        self.sysfs = FakeSysfs(self)

    def check_config_query(self, path):
        rec = SgIoRecorder(device_answer(FakeDevice()))
        dio = open_dev(path, 0, 0)
        with mock.patch.object(DroboIOctl, 'ioctl', rec):
            out = dio.get_sub_page(15, CONFIG_LEGACY, 0, 0)
        self.assertEqual(out, legacy_subpage(1)[:15])
        self.assertEqual(rec.calls, [{
            'cdb': CONFIG_LEGACY,
            'direction': FROM_DEV,
            'dxfer_len': 15
        }])

    def test_gen1_identity(self):
        self.sysfs.device('sdq', *GEN1)
        self.check_config_query('/dev/sdq')

    def test_no_sysfs_entry(self):
        self.check_config_query('/dev/sdq')

    def test_other_drobo_models(self):
        for name, (v, m) in zip(('sdc', 'sdd', 'sde', 'sdf'),
                                (('Drobo   ', 'Drobo S         '),
                                 ('Drobo   ', '5D              '),
                                 ('Drobo   ', 'DroboPro        '),
                                 ('USB 3.0 ', 'Generic disk    '))):
            with self.subTest(model=m):
                self.sysfs.device(name, v, m)
                self.check_config_query('/dev/' + name)

    def test_ea_data_out_is_one_zero_byte(self):
        self.sysfs.device('sdq', *GEN1)
        rec = SgIoRecorder(lambda cdb, n, p: (b'', 0))
        dio = open_dev('/dev/sdq', 0, 0)
        cdb = bytes.fromhex('ea1000060007200001 00'.replace(' ', ''))
        with mock.patch.object(DroboIOctl, 'ioctl', rec):
            out = dio.get_sub_page(1, cdb, 1, 0)
        self.assertEqual(out, b'\0')
        self.assertEqual(rec.calls, [{
            'cdb': cdb,
            'direction': TO_DEV,
            'dxfer_len': 1,
            'payload': b'\0'
        }])

    def test_mode_select_payload(self):
        self.sysfs.device('sdq', *GEN1)
        rec = SgIoRecorder(lambda cdb, n, p: (b'', 0))
        dio = open_dev('/dev/sdq', 0, 0)
        cdb = bytes.fromhex('55017a0500000000 2a00'.replace(' ', ''))
        with mock.patch.object(DroboIOctl, 'ioctl', rec):
            n = dio.put_sub_page(cdb, b'\x7a\x05' + b'x' * 40, 0)
        self.assertEqual(n, 42)
        self.assertEqual(rec.calls, [{
            'cdb': cdb,
            'direction': TO_DEV,
            'dxfer_len': 42,
            'payload': b'\x7a\x05' + b'x' * 40
        }])

    def test_drobo_discovery_cdbs(self):
        """ the whole Drobo() constructor, through the real DroboIOctl. """
        self.sysfs.device('sdq', *GEN1)
        rec = SgIoRecorder(device_answer(FakeDevice()))
        d = real_drobo('/dev/sdq', rec)
        self.assertEqual(d.slot_count, SLOTS)
        self.assertEqual([(c['cdb'].hex(), c['direction']) for c in rec.calls],
                         [('5a003a01000000000f00', FROM_DEV),
                          ('120000004c00', FROM_DEV),
                          ('5a003a05000000002a00', FROM_DEV),
                          ('5a003a08000000013a00', FROM_DEV)])

    def test_simulation_opens_nothing(self):
        self.sysfs.device('sdq', *GEN2)
        with mock.patch.object(DroboIOctl, 'DroboIOctl') as ctor:
            d = Drobo.Drobo('/dev/sdq', Drobo.DBG_Simulation)
        ctor.assert_not_called()
        self.assertGreaterEqual(d.slot_count, 4)



# --- Part 2: the tunnel -------------------------------------------------------

LBA32 = b'\x00\x00\x00\x20'
CONFIG_DBD = bytes.fromhex('5a083a01000000000f00')

# the request block for CONFIG_DBD with a 15 byte data-in transfer.
GOLDEN_CONFIG_REQUEST = bytes.fromhex(
    'facecafe'  # magic
    '00000001'  # version
    '00000001'  # direction: data-in
    '0000000f'  # transfer length
    '0000000a'  # CDB length
    '5a083a01000000000f00').ljust(512, b'\0')


def reply(data, length=None, magic=0xBEEFFEED, version=1):
    if length is None:
        length = len(data)
    return struct.pack('>LLL', magic, version, length) + data


def rw10(opcode, sectors):
    return bytes([opcode, 0]) + LBA32 + bytes([0]) + struct.pack(
        '>H', sectors) + b'\0'


class FirmwareTunnel:
    """ an SgIoRecorder answer that behaves like Drobo firmware: a WRITE(10)
        at LBA 32 is a tunnel request, run on 'dev' (a FakeDevice); the next
        READ(10) at LBA 32 returns its reply.  Anything else is pass-through
        to the bridge, which knows INQUIRY and answers MODE SENSE with an
        empty mode parameter header (as the real Gen 2 does.) """

    def __init__(self, dev, bridge=GEN2):
        self.dev = dev
        self.bridge = FakeDevice(vendor=bridge[0].encode(),
                                 product=bridge[1].encode(),
                                 responder=lambda sp: b'\x00\x06' + b'\0' * 6)
        self.reply = None
        self.requests = []

    def __call__(self, cdb, length, payload):
        if cdb[0] in (0x2a, 0x28):
            assert cdb[2:6] == LBA32, cdb.hex()
            assert struct.unpack('>H', cdb[7:9])[0] * 512 == length
        if cdb[0] == 0x2a:
            magic, ver, direction, xfer, cdblen = struct.unpack(
                '>5L', payload[:20])
            assert (magic, ver) == (0xFACECAFE, 1)
            inner = payload[20:20 + cdblen]
            dataout = payload[20 + cdblen:20 + cdblen + xfer] \
                if direction == 0 else None
            self.requests.append((inner, direction, xfer, dataout))
            try:
                data = self.dev.get_sub_page(xfer, inner, direction == 0, 0)
            except IOError:
                self.reply = None
                return b'', 2
            self.reply = None if direction == 0 else reply(data[:xfer])
            return b'', 0
        if cdb[0] == 0x28:
            assert self.reply is not None, 'read without a request'
            r, self.reply = self.reply, None
            return r, 0
        try:
            return self.bridge.get_sub_page(length, cdb, payload is not None,
                                            0), 0
        except IOError:
            return b'', 2


class TestRequestBlock(unittest.TestCase):
    """ building the request written to LBA 32. """

    def test_golden_config_query(self):
        self.assertEqual(DroboIOctl.tunnel_request(CONFIG_DBD, 15),
                         GOLDEN_CONFIG_REQUEST)

    def test_data_out_appended(self):
        cdb = bytes.fromhex('ea1000060007200001 00'.replace(' ', ''))
        req = DroboIOctl.tunnel_request(cdb, 1, b'\0')
        self.assertEqual(
            req[:31],
            bytes.fromhex('facecafe 00000001 00000000 00000001 0000000a'
                          .replace(' ', '')) + cdb + b'\0')
        self.assertEqual(len(req), 512)

    def test_sector_rounding(self):
        cdb = b'\x5a' + b'\0' * 9  # 0x14 + 10 = 30 byte header
        for n, size in ((482, 512), (483, 1024), (994, 1024), (995, 1536)):
            with self.subTest(dataout=n):
                req = DroboIOctl.tunnel_request(cdb, n, b'\xff' * n)
                self.assertEqual(len(req), size)
                self.assertEqual(req[30:30 + n], b'\xff' * n)
                self.assertEqual(req[30 + n:], b'\0' * (size - 30 - n))

    def test_twelve_byte_cdb(self):
        cdb = bytes(range(1, 13))
        req = DroboIOctl.tunnel_request(cdb, 100)
        self.assertEqual(struct.unpack('>L', req[16:20])[0], 12)
        self.assertEqual(req[20:32], cdb)


class TestReplyParsing(unittest.TestCase):

    def test_data_returned(self):
        self.assertEqual(DroboIOctl.tunnel_reply(reply(b'abcdef'), 6),
                         b'abcdef')

    def test_clamped_to_transfer_length(self):
        self.assertEqual(DroboIOctl.tunnel_reply(reply(b'abcdef'), 4), b'abcd')

    def test_clamped_to_reply_length_field(self):
        self.assertEqual(
            DroboIOctl.tunnel_reply(reply(b'abcdef', length=3), 6), b'abc')

    def test_clamped_to_bytes_read(self):
        self.assertEqual(
            DroboIOctl.tunnel_reply(reply(b'ab', length=100), 50), b'ab')

    def test_padding_ignored(self):
        raw = reply(b'xyz').ljust(512, b'\0')
        self.assertEqual(DroboIOctl.tunnel_reply(raw, 15), b'xyz')

    def test_bad_magic(self):
        with self.assertRaises(IOError):
            DroboIOctl.tunnel_reply(reply(b'abc', magic=0xFACECAFE), 3)

    def test_zeroes_are_bad_magic(self):
        with self.assertRaises(IOError):
            DroboIOctl.tunnel_reply(b'\0' * 512, 3)

    def test_short_reply(self):
        for n in range(12):
            with self.subTest(n=n):
                with self.assertRaises(IOError):
                    DroboIOctl.tunnel_reply(reply(b'')[:n], 3)

    def test_version_is_not_a_status(self):
        self.assertEqual(
            DroboIOctl.tunnel_reply(reply(b'ok', version=7), 2), b'ok')

    def test_empty_reply(self):
        self.assertEqual(DroboIOctl.tunnel_reply(reply(b''), 15), b'')


class TestSelectionRule(unittest.TestCase):

    def test_identities(self):
        cases = {
            GEN2: True,
            ('USB 3.0', 'Drobo S - Gen 2'): True,
            ('usb 3.0 ', 'DROBO S'): True,
            ('Drobo   ', 'DRDR3           '): True,
            ('Drobo   ', '3rd Gen Drobo   '): True,
            GEN1: False,
            ('Drobo   ', 'Drobo S         '): False,
            ('Drobo   ', 'Drobo3          '): False,
            ('Drobo   ', '5D              '): False,
            ('Drobo   ', 'DroboPro        '): False,
            ('USB 3.0 ', 'Generic disk    '): False,
            ('ATA     ', 'ST3500830AS     '): False,
            ('', ''): False,
        }
        for (vendor, model), want in cases.items():
            with self.subTest(vendor=vendor, model=model):
                self.assertIs(DroboIOctl.wants_tunnel(vendor, model), want)

    def test_bytes_accepted(self):
        self.assertTrue(DroboIOctl.wants_tunnel(b'USB 3.0 ', b'Drobo S - Gen 2'))


class TestTransportSelection(unittest.TestCase):

    def setUp(self):
        no_env(self)
        self.sysfs = FakeSysfs(self)

    def test_gen2_uses_tunnel(self):
        self.sysfs.device('sdq', *GEN2)
        self.assertTrue(open_dev('/dev/sdq').tunnel)

    def test_gen2_sg_node(self):
        self.sysfs.device('sg3', *GEN2, cls='scsi_generic')
        self.assertTrue(open_dev('/dev/sg3').tunnel)

    def test_gen1_uses_pass_through(self):
        self.sysfs.device('sdq', *GEN1)
        self.assertFalse(open_dev('/dev/sdq').tunnel)

    def test_unknown_device_uses_pass_through(self):
        self.assertFalse(open_dev('/dev/sdq').tunnel)

    def test_symlink_resolved(self):
        self.sysfs.device('sdq', *GEN2)
        with mock.patch.object(DroboIOctl.os.path, 'realpath',
                               lambda p: '/dev/sdq'):
            self.assertTrue(open_dev('/dev/disk/by-id/usb-Drobo').tunnel)

    def test_env_forces_pass_through(self):
        self.sysfs.device('sdq', *GEN2)
        os.environ['DROBO_UTILS_TRANSPORT'] = 'passthrough'
        self.assertFalse(open_dev('/dev/sdq').tunnel)

    def test_env_forces_tunnel(self):
        self.sysfs.device('sdq', *GEN1)
        os.environ['DROBO_UTILS_TRANSPORT'] = 'tunnel'
        self.assertTrue(open_dev('/dev/sdq').tunnel)

    def test_env_auto_and_junk_mean_auto(self):
        self.sysfs.device('sdq', *GEN2)
        for value in ('auto', '', 'bogus'):
            with self.subTest(value=value):
                os.environ['DROBO_UTILS_TRANSPORT'] = value
                self.assertTrue(open_dev('/dev/sdq').tunnel)

    def test_argument_beats_env(self):
        self.sysfs.device('sdq', *GEN2)
        os.environ['DROBO_UTILS_TRANSPORT'] = 'tunnel'
        self.assertFalse(open_dev('/dev/sdq', transport='passthrough').tunnel)
        self.assertTrue(open_dev('/dev/sdr', transport='tunnel').tunnel)

    def test_tunnel_refused_on_a_partition(self):
        """ LBA 32 of a partition is somewhere in the middle of the disk. """
        self.sysfs.partition('sdq1')
        with self.assertRaises(IOError):
            open_dev('/dev/sdq1', transport='tunnel')

    def test_detection_trace(self):
        self.sysfs.device('sdq', *GEN2)
        out = io.StringIO()
        with redirect_stdout(out):
            open_dev('/dev/sdq', 0, Drobo.DBG_Detection)
        self.assertIn('tunnel', out.getvalue())

    def test_same_device_shares_a_lock(self):
        a = open_dev('/dev/sdq', transport='tunnel')
        b = open_dev('/dev/sdq', transport='tunnel')
        c = open_dev('/dev/sdr', transport='tunnel')
        self.assertIs(a.tunnel_lock, b.tunnel_lock)
        self.assertIsNot(a.tunnel_lock, c.tunnel_lock)


class TestTunnelTransport(unittest.TestCase):
    """ DroboIOctl.get_sub_page / put_sub_page over the tunnel. """

    def setUp(self):
        no_env(self)
        self.sysfs = FakeSysfs(self)
        self.sysfs.device('sdq', *GEN2)

    def run_io(self, answer, fn, *args, debug=0):
        self.rec = SgIoRecorder(answer)
        self.dio = open_dev('/dev/sdq', 0, debug)
        with mock.patch.object(DroboIOctl, 'ioctl', self.rec), \
             redirect_stdout(io.StringIO()):
            return getattr(self.dio, fn)(*args)

    def test_config_query_wire_traffic(self):
        fw = FirmwareTunnel(FakeDevice(responder=gen2_responder()))
        out = self.run_io(fw, 'get_sub_page', 15, CONFIG_DBD, 0, 0)
        self.assertEqual(out, gen2_responder()(1, CONFIG_DBD)[:15])
        self.assertEqual(self.rec.calls, [{
            'cdb': bytes.fromhex('2a00000000200000 0100'.replace(' ', '')),
            'direction': TO_DEV,
            'dxfer_len': 512,
            'payload': GOLDEN_CONFIG_REQUEST
        }, {
            'cdb': bytes.fromhex('2800000000200000 0100'.replace(' ', '')),
            'direction': FROM_DEV,
            'dxfer_len': 512
        }])

    def test_reply_sector_rounding(self):
        for xfer, sectors in ((500, 1), (501, 2), (1012, 2), (1013, 3)):
            with self.subTest(xfer=xfer):
                fw = FirmwareTunnel(FakeDevice())
                self.run_io(fw, 'get_sub_page', xfer, CONFIG_LEGACY, 0, 0)
                self.assertEqual(self.rec.calls[1]['cdb'], rw10(0x28, sectors))
                self.assertEqual(self.rec.calls[1]['dxfer_len'], sectors * 512)

    def test_lba_is_always_32(self):
        fw = FirmwareTunnel(FakeDevice())
        for xfer in (1, 15, 511, 4096, 20000):
            self.run_io(fw, 'get_sub_page', xfer, CONFIG_LEGACY, 0, 0)
            for c in self.rec.calls:
                self.assertIn(c['cdb'][0], (0x2a, 0x28))
                self.assertEqual(c['cdb'][2:6], LBA32)
                self.assertEqual(len(c['cdb']), 10)
                self.assertEqual(c['cdb'][1], 0)  # no FUA/DPO flags

    def test_ea_data_out_has_no_read_back(self):
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        fw = FirmwareTunnel(dev)
        cdb = bytes.fromhex('ea1000060007200001 00'.replace(' ', ''))
        out = self.run_io(fw, 'get_sub_page', 1, cdb, 1, 0)
        self.assertEqual(out, b'\0')
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(self.rec.calls[0]['cdb'], rw10(0x2a, 1))
        self.assertEqual(fw.requests, [(cdb, 0, 1, b'\0')])

    def test_mode_select_through_tunnel(self):
        dev = FakeDevice()
        dev.ea_responder = None
        sent = []
        dev.get_sub_page = lambda sz, mcb, out, D: sent.append(mcb) or b''
        fw = FirmwareTunnel(dev)
        cdb = bytes.fromhex('55017a0500000000 2a00'.replace(' ', ''))
        payload = b'\x7a\x05' + b'x' * 40
        n = self.run_io(fw, 'put_sub_page', cdb, payload, 0)
        self.assertEqual(n, 42)
        self.assertEqual(len(self.rec.calls), 1)
        self.assertEqual(fw.requests, [(cdb, 0, 42, payload)])

    def test_mode_select_error_returns_none(self):
        fail = lambda cdb, n, p: (b'', 2)
        cdb = bytes.fromhex('55017a0500000000 2a00'.replace(' ', ''))
        self.assertIsNone(self.run_io(fail, 'put_sub_page', cdb, b'xx', 0))

    def test_inquiry_stays_pass_through(self):
        """ the bridge answers INQUIRY: that identity picked the tunnel. """
        fw = FirmwareTunnel(FakeDevice())
        cdb = bytes.fromhex('120000004c00')
        out = self.run_io(fw, 'get_sub_page', 76, cdb, 0, 0)
        self.assertEqual(out[8:32], b'USB 3.0 Drobo S - Gen 2 ')
        self.assertEqual([c['cdb'] for c in self.rec.calls], [cdb])

    def test_bad_magic_raises(self):
        def answer(cdb, n, p):
            return (reply(b'x' * 15, magic=0x12345678), 0) if cdb[0] == 0x28 \
                else (b'', 0)
        with self.assertRaises(IOError):
            self.run_io(answer, 'get_sub_page', 15, CONFIG_DBD, 0, 0)

    def test_short_read_raises(self):
        def answer(cdb, n, p):
            return (b'\xbe\xef\xfe\xed\0\0', 0) if cdb[0] == 0x28 else (b'', 0)
        with self.assertRaises(IOError):
            self.run_io(answer, 'get_sub_page', 15, CONFIG_DBD, 0, 0)

    def test_write_error_raises_and_skips_read(self):
        with self.assertRaises(IOError):
            self.run_io(lambda cdb, n, p: (b'', 2), 'get_sub_page', 15,
                        CONFIG_DBD, 0, 0)
        self.assertEqual(len(self.rec.calls), 1)

    def test_ioctl_failure_raises(self):
        self.rec = None
        dio = open_dev('/dev/sdq', 0, 0)
        with mock.patch.object(DroboIOctl, 'ioctl', lambda *a: -1):
            with self.assertRaises(IOError):
                dio.get_sub_page(15, CONFIG_DBD, 0, 0)

    def test_host_status_error_raises(self):
        dio = open_dev('/dev/sdq', 0, 0)

        def ioctl(fd, req, hdr, mutate=False):
            hdr.host_status = 7  # DID_ERROR
            return 0

        with mock.patch.object(DroboIOctl, 'ioctl', ioctl):
            with self.assertRaises(IOError):
                dio.get_sub_page(15, CONFIG_DBD, 0, 0)

    def test_too_large_refused_before_any_io(self):
        for xfer in (0x10000 - 12 + 1, 0x20000):
            with self.subTest(xfer=xfer):
                with self.assertRaises(IOError):
                    self.run_io(FirmwareTunnel(FakeDevice()), 'get_sub_page',
                                xfer, CONFIG_LEGACY, 0, 0)
                self.assertEqual(self.rec.calls, [])
        with self.assertRaises(IOError):
            self.run_io(FirmwareTunnel(FakeDevice()), 'get_sub_page',
                        0x10000, CONFIG_LEGACY, 1, 0)
        self.assertEqual(self.rec.calls, [])

    def test_largest_allowed_sizes(self):
        fw = FirmwareTunnel(FakeDevice())
        self.run_io(fw, 'get_sub_page', 0x10000 - 12, CONFIG_LEGACY, 0, 0)
        self.assertEqual(self.rec.calls[1]['cdb'], rw10(0x28, 128))
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b''
        cdb = bytes(10)
        cdb = b'\xea' + cdb[1:]
        self.run_io(FirmwareTunnel(dev), 'get_sub_page', 0x10000 - 30, cdb, 1,
                    0)
        self.assertEqual(self.rec.calls[0]['cdb'], rw10(0x2a, 128))

    def test_lock_held_during_io(self):
        held = []
        fw = FirmwareTunnel(FakeDevice())

        def answer(cdb, n, p):
            held.append(self.dio.tunnel_lock.locked())
            return fw(cdb, n, p)

        self.run_io(answer, 'get_sub_page', 15, CONFIG_LEGACY, 0, 0)
        self.assertEqual(held, [True, True])
        self.assertFalse(self.dio.tunnel_lock.locked())

    def test_hwdialog_trace(self):
        fw = FirmwareTunnel(FakeDevice())
        rec = SgIoRecorder(fw)
        out = io.StringIO()
        with mock.patch.object(DroboIOctl, 'ioctl', rec), redirect_stdout(out):
            dio = open_dev('/dev/sdq', 0, ALL_DEBUG)
            dio.get_sub_page(15, CONFIG_LEGACY, 0, 0)
        self.assertIn('tunnel', out.getvalue())

    def test_version_mismatch_traced(self):
        def answer(cdb, n, p):
            return (reply(b'x' * 15, version=2), 0) if cdb[0] == 0x28 \
                else (b'', 0)
        rec = SgIoRecorder(answer)
        dio = open_dev('/dev/sdq', 0, Drobo.DBG_HWDialog)
        out = io.StringIO()
        with mock.patch.object(DroboIOctl, 'ioctl', rec), redirect_stdout(out):
            self.assertEqual(dio.get_sub_page(15, CONFIG_LEGACY, 0, 0),
                             b'x' * 15)
        self.assertIn('version 2', out.getvalue())


class TestGen2DiscoveryThroughTunnel(unittest.TestCase):
    """ Drobo.Drobo() on the Gen 2: every Drobo command inside the tunnel. """

    def setUp(self):
        no_env(self)
        self.sysfs = FakeSysfs(self)
        self.sysfs.device('sdq', *GEN2)

    def check(self, inner):
        dev = FakeDevice(responder=inner)
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        self.fw = FirmwareTunnel(dev)
        self.rec = SgIoRecorder(self.fw)
        d = real_drobo('/dev/sdq', self.rec)
        self.assertEqual(d.slot_count, SLOTS)
        self.assertEqual(d.fw[6], 'ArmMarvell')
        self.assertEqual(d.model, 'Drobo S (gen 2)')
        for c in self.rec.calls:
            self.assertIn(c['cdb'][0], (0x12, 0x2a, 0x28))
            if c['cdb'][0] != 0x12:
                self.assertEqual(c['cdb'][2:6], LBA32)
        self.assertEqual(self.fw.requests[0][0][:4],
                         bytes([0x5a, 0x00, 0x3a, 0x01]))
        return d

    def test_legacy_style_answers(self):
        d = self.check(legacy_subpage)
        self.assertEqual(d.mode_sense_style, 'legacy')
        with mock.patch.object(DroboIOctl, 'ioctl', self.rec):
            self.assertEqual(d.GetSubPageCapacity(), (1000, 2000, 3000, 400))

    def test_mode_header_style_answers(self):
        d = self.check(gen2_responder())
        self.assertEqual(d.mode_sense_style, 'dbd')
        with mock.patch.object(DroboIOctl, 'ioctl', self.rec):
            self.assertEqual(d.GetSubPageCapacity(), (1000, 2000, 3000, 400))

    def test_blink_through_tunnel(self):
        d = self.check(legacy_subpage)
        txid = d.transactionID
        self.fw.requests.clear()
        with mock.patch.object(DroboIOctl, 'ioctl', self.rec):
            d.Blink()
        self.assertEqual(self.fw.requests, [(struct.pack(
            '>BBBBBBBHB', 0xea, 0x10, 0x00, 0x06, 0x00, txid, 0x20, 0x01,
            0x00), 0, 1, b'\0')])

    def test_pass_through_would_fail(self):
        """ the same unit forced to pass-through: today's failure. """
        os.environ['DROBO_UTILS_TRANSPORT'] = 'passthrough'
        fw = FirmwareTunnel(FakeDevice())
        with self.assertRaises(Exception), redirect_stdout(io.StringIO()):
            real_drobo('/dev/sdq', SgIoRecorder(fw))
        self.assertEqual(fw.requests, [])

if __name__ == '__main__':
    unittest.main()
