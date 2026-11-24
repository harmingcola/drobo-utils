#!/usr/bin/env python3
"""
The firmware capability gate: never send a request the unit's firmware
does not implement, when that request would go through the ATA tunnel.

Why: on a Drobo S Gen 2 (firmware 2.1.5) reached through the ATA tunnel, a
sub-page the firmware lacks (0x35) never completed: SG_IO timed out and the
kernel reset the USB link of a mounted disk.  Over pass-through the same
request is refused quickly, so pass-through stays as it is.

Part 1 (regression) pins today's behaviour of everything the gate touches:
the pass-through CDBs, the supported requests through the tunnel, units
whose firmware is not in the table, and the SG_IO timeouts.

No hardware: open() and fcntl.ioctl are replaced (test_ata_tunnel), sysfs
is a temporary tree (DroboIOctl.SYSFS_ROOT.)

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import io
import os
import struct
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (FakeDevice, make_drobo, setUpModule,
                             tearDownModule)
from test_ata_tunnel import (FakeSysfs, SgIoRecorder, FirmwareTunnel,
                             real_drobo, no_env, open_dev, GEN2)
from test_cov_ioctl import fake_open
from test_cov_drobom import DrobomTestCase
from test_unsupported_features import (Fw215Device, EX2_FEATURE, DRIVES_MSG,
                                       VOLUMES_MSG, SERIAL_MSG)

import Drobo
import DroboIOctl

REAL_DROBOIOCTL = DroboIOctl.DroboIOctl  # before DrobomTestCase guards it

# --- a Drobo S Gen 2 running 2.1.5, with the reply bytes seen on hardware ---

GEN2_SLOTS = 5

# feature bits: NO_AUTO_REBOOT, NO_FAT32_FORMAT, USED_CAPACITY_FROM_HOST,
# DISKPACKSTATUS, SUPPORTS_NEW_LUNINFO2 ... (not SUPPORTS_LUNINFO_EX2.)
GEN2_FEATURES = 0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0400


def gen2_firmware_page(version=b'2.1.5', features=GEN2_FEATURES):
    """ sub-page 0x08 as the unit returned it on 2026-11-24: begins
        7a 08 01 36 05 29 f5 be 00 00 41 70 72 20 31 35 20 32 30 31 ... """
    extra = struct.pack('>112sL32sH90s', b'', features, b'', 0, b'')
    body = struct.pack('>BBHBB32s16s16s240s', 5, 41, 62910, 0, 0,
                       b'Apr 15 2013,12:53:35', b'ArmMarvell', version, extra)
    return struct.pack('>BBH', 0x7a, 0x08, len(body)) + body


# sub-page 0x01 as seen on hardware: 5 slots, 16 LUNs.
GEN2_CONFIG = bytes.fromhex(
    '7a01001805031001000007ffffffff00').ljust(28, b'\0')


# free, used, usable protected total, unused: 78% full, as on hardware.
GEN2_CAPACITY = (2846 * 10**9, 10118 * 10**9, 12964 * 10**9, 0)


def gen2_slots_page():
    rec = b''
    for slot in range(GEN2_SLOTS):
        model = b'ST4000DM004' if slot < 2 else b'WDC WD30EZRX'
        size = 4000787030016 if slot < 2 else 3000592982016
        rec += struct.pack('>HBQQB32s16sL', slot, slot, size, 0, 3, model,
                           model, 0)
    body = struct.pack('>B', GEN2_SLOTS) + rec
    return struct.pack('>BBH', 0x7a, 0x03, len(body)) + body


def plain_page(sub_page, pattern, *fields):
    body = struct.pack('>' + pattern, *fields)
    return struct.pack('>BBH', 0x7a, sub_page, len(body)) + body


# what firmware 2.1.5 implements (DEVELOPERS.rst, "Firmware capability gate")
FW215_SUBPAGES = (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x11,
                  0x15, 0x30, 0x31, 0x82)
FW215_OPS = tuple(range(0x01, 0x19)) + (0x20, 0x21, 0x70, 0x71, 0x80)
# what it lacks that drobo-utils knows about.
FW215_LACKS_SUBPAGES = (0x33, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x80, 0xe1,
                        0xe2)
FW215_LACKS_OPS = (0x22, 0x81, 0x83)


class Gen2Unit(FakeDevice):
    """ the firmware side of a Drobo S Gen 2 on 2.1.5, as reached through the
        tunnel (FirmwareTunnel runs each tunnelled CDB on this.)  Replies are
        legacy style, as on hardware.  A request the firmware lacks is
        recorded in 'hung' and fails, standing in for the hang (SG_IO
        DID_TIME_OUT, then a USB reset) seen on hardware. """

    def __init__(self, version=b'2.1.5', features=GEN2_FEATURES):
        self.version = version
        self.features = features
        self.hung = []
        super().__init__(vendor=GEN2[0].encode(), product=GEN2[1].encode(),
                         responder=self.page)
        self.ea_responder = self.op

    def page(self, sub_page):
        pages = {
            0x01: GEN2_CONFIG,
            0x02: plain_page(0x02, 'QQQQ', *GEN2_CAPACITY),
            0x03: gen2_slots_page(),
            0x05: plain_page(0x05, 'LH32s', 1795380300, 0, b'Drobo'),
            0x06: plain_page(0x06, 'BB', 0, 11),
            0x08: gen2_firmware_page(self.version, self.features),
            0x09: plain_page(0x09, 'LLLL', 0, 0, 0, 0),
            # options: yellow 85%, red 95%; one 16 TiB LUN, GPT.
            0x30: plain_page(0x30, 'BBBIBB', 85, 95, 0, 0, 0, 0),
            0x04: plain_page(0x04, 'B' + 'HBQQ' * 8, 1, 0, 0, 2**44,
                             10118 * 10**9, *((0, 0, 0, 0) * 7)),
            0x07: plain_page(0x07, 'B' + 'HBQBBBH3B' * 8, 1,
                             0, 0, 2**44, 0, 2, 1, 0, 0, 0, 0,
                             *((0,) * 10 * 7)),
        }
        if sub_page in pages:
            return pages[sub_page]
        if sub_page in FW215_SUBPAGES:
            raise IOError("Gen2Unit: no fixture for sub_page 0x%02x" %
                          sub_page)
        self.hung.append(('sub_page', sub_page))
        raise IOError("DID_TIME_OUT: sub_page 0x%02x" % sub_page)

    def op(self, mcb, sz, out):
        if mcb[3] in FW215_LACKS_OPS:
            self.hung.append(('op', mcb[3]))
            raise IOError("DID_TIME_OUT: 0xEA op 0x%02x" % mcb[3])
        return b'\0' * sz


class TunnelCase(unittest.TestCase):
    """ a Drobo.Drobo over the real DroboIOctl, auto-selected to the tunnel
        by its sysfs identity, with the firmware simulated by Gen2Unit. """

    def setUp(self):
        no_env(self)
        self.sysfs = FakeSysfs(self)
        self.sysfs.device('sdq', *GEN2)

    def unit(self, version=b'2.1.5', features=GEN2_FEATURES):
        self.dev = Gen2Unit(version, features)
        self.fw = FirmwareTunnel(self.dev)
        self.rec = SgIoRecorder(self.fw)
        d = real_drobo('/dev/sdq', self.rec)
        self.assertTrue(d.fd.tunnel)
        p = mock.patch.object(DroboIOctl, 'ioctl', self.rec)
        p.start()
        self.addCleanup(p.stop)
        self.fw.requests.clear()
        self.rec.calls.clear()
        return d

    def inner(self):
        """ the CDBs that went through the tunnel. """
        return [r[0] for r in self.fw.requests]

    def subpages(self):
        return [c[3] for c in self.inner() if c[0] == 0x5a]

    def ops(self):
        return [c[3] for c in self.inner() if c[0] == 0xea]


# --- Part 1: regression ----------------------------------------------------------


class TestGen2Fixture(TunnelCase):
    """ the simulated Gen 2 decodes like the real one. """

    def test_identity(self):
        d = self.unit()
        self.assertEqual(d.slot_count, GEN2_SLOTS)
        self.assertEqual(d.GetSubPageConfig()[:2], (GEN2_SLOTS, 16))
        self.assertEqual(d.fw[:8], (5, 41, 62910, 0, 0, 'Apr 15 2013,12:53:35',
                                    'ArmMarvell', '2.1.5'))
        self.assertIn('SUPPORTS_NEW_LUNINFO2', d.features)
        self.assertNotIn('SUPPORTS_LUNINFO_EX2', d.features)
        self.assertEqual(d.mode_sense_style, 'legacy')

    def test_firmware_reply_bytes(self):
        self.assertEqual(gen2_firmware_page()[:20], bytes.fromhex(
            '7a08013605 29f5be0000 4170722031352032 3031'.replace(' ', '')))
        self.assertEqual(GEN2_CONFIG[:16], bytes.fromhex(
            '7a01001805031001000007ffffffff00'))

    def test_discovery_sends_only_supported_pages(self):
        dev = Gen2Unit()
        fw = FirmwareTunnel(dev)
        real_drobo('/dev/sdq', SgIoRecorder(fw))
        self.assertEqual([r[0][3] for r in fw.requests], [0x01, 0x05, 0x08])
        self.assertEqual(dev.hung, [])


class TestTunnelSupportedUnchanged(TunnelCase):
    """ requests 2.1.5 implements go through the tunnel, byte for byte as
        today, and decode. """

    def test_supported_reads(self):
        d = self.unit()
        self.assertEqual(d.GetSubPageCapacity(), GEN2_CAPACITY)
        self.assertEqual(len(d.GetSubPageSlotInfo()), GEN2_SLOTS)
        self.assertEqual(d.GetSubPageProtocol(), (0, 11))
        self.assertEqual(d.GetStatusEx()['severity'], 'green')
        self.assertEqual(self.inner(), [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x02, 0, 0, 0, 36, 0),
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x03, 0, 0, 0,
                        5 + 72 * GEN2_SLOTS, 0),
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x06, 0, 0, 0, 6, 0),
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x09, 0, 0, 0, 24, 0),
        ])
        self.assertEqual(self.dev.hung, [])

    def test_blink_and_standby_ops(self):
        d = self.unit()
        with mock.patch.object(d, 'umount', lambda: True):
            d.Blink()
            d.Standby()
        self.assertEqual(self.ops(), [0x06, 0x0d])

    def test_ex2_feature_gated(self):
        """ 2.1.5 does not advertise SUPPORTS_LUNINFO_EX2: op 0x83 is never
            sent, by the existing feature check. """
        d = self.unit()
        self.assertIsNone(d.GetLUNsInfoEx2())
        self.assertEqual(self.fw.requests, [])


class TestTunnelOtherFirmwareUnchanged(TunnelCase):
    """ firmware not in the table (or unreadable) keeps today's behaviour:
        every request is sent. """

    def each_version(self):
        for version in (b'4.2.3', b'2.0.3', b'3.1.5', b'1.1.2', b'junk', b''):
            yield version, self.unit(version)

    def test_slotinfo2_sent(self):
        for version, d in self.each_version():
            with self.subTest(version=version):
                self.assertIsNone(d.GetSlotInfo2())
                self.assertEqual(self.subpages(), [0x35])

    def test_serial_sent(self):
        for version, d in self.each_version():
            with self.subTest(version=version):
                self.assertIsNone(d.GetSerial())
                self.assertEqual(self.subpages(), [0x80])

    def test_luninfo_ex_sent(self):
        for version, d in self.each_version():
            with self.subTest(version=version):
                self.assertIsNone(d.GetLUNsInfoEx())
                self.assertEqual(self.ops(), [0x81])


def pass_through_unit(style='legacy', features=0x0001 | 0x0002 | 0x0080):
    dev = Fw215Device('check_condition', 'check_condition', style, features)
    d = make_drobo(dev)
    dev.cdbs.clear()
    return dev, d


class TestPassThroughByteIdentical(unittest.TestCase):
    """ a 2.1.5 unit over pass-through: what is sent is not changed by the
        gate, CDB for CDB (the #46 behaviour: send, and degrade.) """

    def test_slotinfo2(self):
        dev, d = pass_through_unit()
        self.assertIsNone(d.GetSlotInfo2())
        maxlen = 8 + 128 * 8
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x35, 0, 0, 0,
                        maxlen + 4, 0)])

    def test_serial(self):
        dev, d = pass_through_unit()
        self.assertIsNone(d.GetSerial())
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x80, 0, 0, 0, 28, 0)])

    def test_luninfo_ex(self):
        dev, d = pass_through_unit()
        self.assertIsNone(d.GetLUNsInfoEx())
        self.assertEqual(dev.cdbs, [bytes.fromhex('ea108081000021040000')])

    def test_luninfo_ex2_when_advertised(self):
        dev, d = pass_through_unit(features=EX2_FEATURE)
        self.assertIsNone(d.GetLUNsInfoEx2())
        self.assertEqual(dev.cdbs, [bytes.fromhex('ea1080830000217c0001')])

    def test_redundancy_level(self):
        dev, d = pass_through_unit()
        level = d.GetRedundancyLevel()
        self.assertNotEqual(level, 'undetermined')
        self.assertEqual([c[3] for c in dev.cdbs], [0x35, 0x03, 0x02])

    def test_gen2_style_pass_through(self):
        dev, d = pass_through_unit(style='gen2')
        d.GetSlotInfo2()
        d.GetSerial()
        d.GetLUNsInfoEx()
        self.assertEqual([(c[0], c[3]) for c in dev.cdbs],
                         [(0x5a, 0x35), (0x5a, 0x80), (0xea, 0x81)])


class TimeoutCase(unittest.TestCase):
    """ records the SG_IO timeout (milliseconds) of each command. """

    def setUp(self):
        no_env(self)
        self.sysfs = FakeSysfs(self)
        self.sysfs.device('sdq', *GEN2)

    def timeouts(self, transport):
        seen = []
        fw = FirmwareTunnel(FakeDevice())
        rec = SgIoRecorder(fw)

        def ioctl(fd, req, hdr, mutate=False):
            seen.append((hdr.cmdp[0:1], hdr.timeout))
            return rec(fd, req, hdr, mutate)

        dio = open_dev('/dev/sdq', 0, 0, transport=transport)
        with mock.patch.object(DroboIOctl, 'ioctl', ioctl):
            dio.get_sub_page(15, bytes.fromhex('5a003a01000000000f00'), 0, 0)
            dio.put_sub_page(bytes.fromhex('55017a05000000002400'), b'\0' * 36, 0)
        return seen


class TestSgIoTimeouts(TimeoutCase):
    """ the SG_IO timeout pass-through commands carry. """

    def test_pass_through_default(self):
        self.assertEqual(DroboIOctl.sg_io_hdr().timeout, 20000)

    def test_pass_through_commands(self):
        self.assertEqual(self.timeouts('passthrough'),
                         [(b'\x5a', 20000), (b'\x55', 20000)])


# --- Part 2: the gate ------------------------------------------------------------


class TestGateTable(unittest.TestCase):
    """ the table of what firmware implements, and its key. """

    def test_fw_2_1(self):
        t = Drobo.FIRMWARE_IMPLEMENTS[(2, 1)]
        self.assertEqual(t['sub_pages'], frozenset(FW215_SUBPAGES))
        self.assertEqual(t['system_ops'], frozenset(FW215_OPS))
        for sp in FW215_LACKS_SUBPAGES:
            self.assertNotIn(sp, t['sub_pages'])
        for op in FW215_LACKS_OPS:
            self.assertNotIn(op, t['system_ops'])

    def test_only_2_1_known(self):
        self.assertEqual(list(Drobo.FIRMWARE_IMPLEMENTS), [(2, 1)])

    def test_fixed_requests_are_implemented(self):
        """ requests drobo-utils sends with fixed codes, outside the gated
            read paths, all exist in 2.1.5: blink 0x06, standby 0x0d, LUN
            size 0x0f, diagnostics 0x04/0x07, firmware 0x70/0x71; MODE
            SELECT of 0x05 (clock/name), 0x30 and 0x31 (options.) """
        t = Drobo.FIRMWARE_IMPLEMENTS[(2, 1)]
        for op in (0x06, 0x0d, 0x0f, 0x04, 0x07, 0x70, 0x71):
            self.assertIn(op, t['system_ops'])
        for sp in (0x05, 0x30, 0x31):
            self.assertIn(sp, t['sub_pages'])

    def test_version_key(self):
        for text, key in (('2.1.5', (2, 1)), ('2.1.0', (2, 1)),
                          ('2.1', (2, 1)), (' 4.2.3 ', (4, 2)),
                          ('10.20.30', (10, 20)), ('2', None), ('', None),
                          ('junk', None), ('v2.1.5', None), (None, None),
                          (215, None)):
            with self.subTest(text=text):
                self.assertEqual(Drobo._fwkey(text), key)

    def test_lacks_is_a_drobo_exception(self):
        self.assertTrue(issubclass(Drobo.FirmwareLacks, Drobo.DroboException))


class TestTunnelGated(TunnelCase):
    """ 2.1.x through the tunnel: nothing the firmware lacks is sent, and
        each call returns what it returns today when the unit refuses. """

    VERSIONS = (b'2.1.5', b'2.1.0', b'2.1.9')

    def each_unit(self, features=GEN2_FEATURES):
        for version in self.VERSIONS:
            yield version, self.unit(version, features)

    def assertNothingSent(self):
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.dev.hung, [])

    def test_slotinfo2(self):
        for version, d in self.each_unit():
            with self.subTest(version=version):
                before = d.mode_sense_style
                self.assertIsNone(d.GetSlotInfo2())
                self.assertNothingSent()
                self.assertEqual(d.mode_sense_style, before)

    def test_serial(self):
        for version, d in self.each_unit():
            with self.subTest(version=version):
                self.assertIsNone(d.GetSerial())
                self.assertNothingSent()

    def test_luninfo_ex(self):
        for version, d in self.each_unit():
            with self.subTest(version=version):
                self.assertIsNone(d.GetLUNsInfoEx())
                self.assertNothingSent()

    def test_luninfo_best(self):
        for version, d in self.each_unit():
            with self.subTest(version=version):
                self.assertIsNone(d.GetLUNsInfoBest())
                self.assertNothingSent()

    def test_luninfo_ex2_even_if_advertised(self):
        for version, d in self.each_unit(GEN2_FEATURES | EX2_FEATURE):
            with self.subTest(version=version):
                self.assertIsNone(d.GetLUNsInfoEx2())
                self.assertIsNone(d.GetLUNsInfoBest())
                self.assertNothingSent()

    def test_redundancy_level_from_slot_info(self):
        for version, d in self.each_unit():
            with self.subTest(version=version):
                caps = [s[1] for s in d.GetSubPageSlotInfo()]
                usable = d.GetSubPageCapacity()[2]
                self.fw.requests.clear()
                self.assertEqual(d.GetRedundancyLevel(),
                                 Drobo._redundancylevel(caps, usable))
                self.assertEqual(self.subpages(), [0x03, 0x02])
                self.assertEqual(self.dev.hung, [])

    def test_every_lacking_sub_page_refused_before_io(self):
        d = self.unit()
        for sp in FW215_LACKS_SUBPAGES + (0x0a, 0x10, 0x32, 0x81, 0xff):
            with self.subTest(sub_page=sp):
                with self.assertRaises(Drobo.FirmwareLacks):
                    d._Drobo__getsubpage_raw(sp, 24)
                with self.assertRaises(Drobo.FirmwareLacks):
                    d._Drobo__getsubpage(sp, 'BB')
        self.assertNothingSent()

    def test_every_lacking_op_refused_before_io(self):
        d = self.unit()
        for op in (0x81, 0x83):
            with self.assertRaises(Drobo.FirmwareLacks):
                d._Drobo__system_read(op, 0, 1024)
        with self.assertRaises(Drobo.DroboException):
            d._Drobo__system_read(0x22, 0, 1024)  # not a read op at all
        self.assertNothingSent()

    def test_implemented_requests_pass(self):
        d = self.unit()
        for sp in FW215_SUBPAGES:
            self.assertTrue(d._fw_implements('sub_pages', sp), hex(sp))
        for op in FW215_OPS:
            self.assertTrue(d._fw_implements('system_ops', op), hex(op))
        for sp in FW215_LACKS_SUBPAGES:
            self.assertFalse(d._fw_implements('sub_pages', sp), hex(sp))
        for op in FW215_LACKS_OPS:
            self.assertFalse(d._fw_implements('system_ops', op), hex(op))

    def test_trace(self):
        d = self.unit()
        out = io.StringIO()
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_HWDialog), \
             redirect_stdout(out):
            self.assertIsNone(d.GetSlotInfo2())
        self.assertIn('firmware 2.1.5 lacks sub_page 0x35: not sent',
                      out.getvalue())


class TestGateScope(unittest.TestCase):
    """ where the gate does not apply. """

    def test_pass_through_2_1_5_not_gated(self):
        dev, d = pass_through_unit()
        self.assertEqual(d.fw[7], '2.1.5')
        self.assertTrue(d._fw_implements('sub_pages', 0x35))
        self.assertTrue(d._fw_implements('system_ops', 0x81))

    def test_mock_fd_not_gated(self):
        """ only a real tunnel (fd.tunnel is True) turns the gate on. """
        dev, d = pass_through_unit()
        d.fd = mock.MagicMock()
        self.assertTrue(d._fw_implements('sub_pages', 0x35))

    def test_before_firmware_read(self):
        dev, d = pass_through_unit()
        d.fd = mock.MagicMock(tunnel=True)
        del d.fw
        self.assertTrue(d._fw_implements('sub_pages', 0x35))
        d.fw = ()
        self.assertTrue(d._fw_implements('sub_pages', 0x35))
        d.fw = (5, 41, 62910, 0, 0, '', 'ArmMarvell', '2.1.5', [])
        self.assertFalse(d._fw_implements('sub_pages', 0x35))

    def test_simulation(self):
        d = Drobo.Drobo(['/dev/sdz'], Drobo.DBG_Simulation)
        self.assertIsNone(d.fd)
        self.assertTrue(d._fw_implements('sub_pages', 0x35))


class TestTunnelTimeout(TimeoutCase):
    """ a tunnel command that hangs fails in TUNNEL_TIMEOUT_MS. """

    def test_value(self):
        self.assertEqual(DroboIOctl.TUNNEL_TIMEOUT_MS, 10000)

    def test_tunnel_commands(self):
        t = DroboIOctl.TUNNEL_TIMEOUT_MS
        self.assertEqual(self.timeouts('tunnel'),
                         [(b'\x2a', t), (b'\x28', t), (b'\x2a', t)])

    def test_inquiry_on_tunnel_device_keeps_pass_through_timeout(self):
        seen = []

        def ioctl(fd, req, hdr, mutate=False):
            seen.append(hdr.timeout)
            return SgIoRecorder(FirmwareTunnel(FakeDevice()))(fd, req, hdr)

        dio = open_dev('/dev/sdq', 0, 0, transport='tunnel')
        with mock.patch.object(DroboIOctl, 'ioctl', ioctl):
            dio.get_sub_page(76, bytes.fromhex('120000004c00'), 0, 0)
        self.assertEqual(seen, [20000])


class TestDrobomOnGen2Tunnel(DrobomTestCase):
    """ drobom against a simulated 2.1.5 unit through the tunnel: nothing
        the firmware lacks is sent, and the usual one-line messages. """

    def setUp(self):
        super().setUp()
        no_env(self)
        for p in (mock.patch.object(Drobo, 'vpd_serial', lambda *a, **k: None),
                  mock.patch.object(DroboIOctl, 'open', fake_open,
                                    create=True)):
            p.start()
            self.addCleanup(p.stop)

    def run_on(self, *args, features=GEN2_FEATURES):
        self.dev = Gen2Unit(features=features)
        self.fw = FirmwareTunnel(self.dev)
        rec = SgIoRecorder(self.fw)

        def ioctl(fd, req, hdr, mutate=False):
            if req != DroboIOctl.sg_io_hdr.SG_IO:  # sg version, GET_IDLUN
                return 0
            return rec(fd, req, hdr, mutate)

        dio = REAL_DROBOIOCTL('/dev/sdq', 0, 0, transport='tunnel')
        with mock.patch.object(DroboIOctl, 'ioctl', ioctl), self.device(dio):
            r = self.drobom('-v', '0', *args)
        self.assertEqual(self.dev.hung, [])
        self.assertNotIn('Traceback', r.out + r.err)
        return r

    def subpages(self):
        return [r[0][3] for r in self.fw.requests if r[0][0] == 0x5a]

    def test_info_health(self):
        r = self.run_on('info', 'health')
        self.assertNotIn(0x35, self.subpages())
        self.assertIn(0x03, self.subpages())
        self.assertIn('Redundancy: protected\n', r.out)
        self.assertIn('Redundancy level: single', r.out)
        self.assertIn('Status: OK\n', r.out)

    def test_info_drives(self):
        self.assertEqual(self.run_on('info', 'drives').out, DRIVES_MSG)

    def test_info_volumes(self):
        for features in (GEN2_FEATURES, GEN2_FEATURES | EX2_FEATURE):
            with self.subTest(features=features):
                self.assertEqual(
                    self.run_on('info', 'volumes', features=features).out,
                    VOLUMES_MSG)

    def test_info_serial(self):
        self.assertEqual(self.run_on('info', 'serial').out, SERIAL_MSG)

    def test_default_info_list(self):
        r = self.run_on('info')
        self.assertIn(DRIVES_MSG, r.out)
        self.assertIn(VOLUMES_MSG, r.out)
        self.assertIn("'ArmMarvell', '2.1.5'", r.out)
        self.assertIn('green', r.out)
        self.assertIn("'YellowThreshold': 85", r.out)
        self.assertIn('Redundancy level: single', r.out)
        asked = self.subpages()
        for sp in asked:
            self.assertIn(sp, FW215_SUBPAGES)
        for r in self.fw.requests:
            if r[0][0] == 0xea:
                self.assertIn(r[0][3], FW215_OPS)


if __name__ == '__main__':
    unittest.main()
