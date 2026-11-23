"""
More regression fixtures for drobo-utils: characterisation tests.

These pin the CURRENT behaviour of queries the main suite (test_regression.py)
does not cover yet:

    - GetSubPageLUNs(): sub-page 0x04 (LUNINFO) alone, and together with
      sub-page 0x07 (LUNINFO2) when the firmware has SUPPORTS_NEW_LUNINFO2.
    - GetOptions(): sub-pages 0x30 (Options) and 0x31 (Options2).
    - inquire(): the DroboPro answer to INQUIRY (36 bytes, not 76.)

Features always come from the feature bits of the firmware sub-page (0x08),
decoded by _unitfeatures() when the Drobo object is built, never by setting
Drobo.features directly.

The fixtures are made up, but use the record layouts that Drobo.py decodes.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import contextlib
import io
import os
import socket
import struct
import tempfile
import unittest

from test_regression import FakeDevice, make_drobo, legacy_subpage, \
    FIXTURE_FIELDS

import Drobo

_STATE = None


def setUpModule():
    """ no test may read or write saved state in the real home directory. """
    global _STATE
    _STATE = tempfile.TemporaryDirectory()
    os.environ['DROBO_UTILS_STATE_DIR'] = _STATE.name


def tearDownModule():
    os.environ.pop('DROBO_UTILS_STATE_DIR', None)
    _STATE.cleanup()


# feature bits, as in _unitfeatures() (from dmp.h)
NO_AUTO_REBOOT = 0x0001
NO_FAT32_FORMAT = 0x0002
PARTITION_LUN_GPT_MBR = 0x0080
SUPPORTS_NEW_LUNINFO2 = 0x0400
SUPPORTS_OPTIONS2 = 0x4000
SUPPORTS_ISCSI = 0x20000

BASE_FEATURES = NO_AUTO_REBOOT | NO_FAT32_FORMAT | PARTITION_LUN_GPT_MBR

LUN_SLOTS = 8  # both LUN sub-pages always have room for eight records.

LUNINFO_PATTERN = 'B' + 'HBQQ' * LUN_SLOTS
LUNINFO2_PATTERN = 'B' + 'HBQBBBH3B' * LUN_SLOTS
OPTIONS_PATTERN = 'BBBIBB'
OPTIONS2_PATTERN = 'QHLLLHLLLH'

TB = 1 << 40


def page(sub_page, pattern, fields):
    """ bytes of a sub-page with the given body, as a first generation Drobo
        returns it (0x7a/sub_page/length header, then the body.) """
    body = struct.pack('>' + pattern, *fields)
    return struct.pack('>BBH', 0x7a, sub_page, len(body)) + body


def firmware_page(features, version=b'1.1.2'):
    """ firmware sub-page (0x08) with the given feature bits and version.
        The feature word is at byte 112 of the 240 byte 'extra' field. """
    pattern, fields = FIXTURE_FIELDS[0x08]
    extra = struct.pack('>112sL32sH90s', b'', features, b'', 0, b'')
    fields = fields[:7] + (version, extra)
    return page(0x08, pattern, fields)


def luninfo_page(records):
    """ LUNINFO sub-page (0x04): count, then eight ( H, LUN id, total
        capacity, used capacity ) records, unused ones zero. """
    fields = [len(records)]
    for rec in records + [(0, 0, 0, 0)] * (LUN_SLOTS - len(records)):
        fields += list(rec)
    return page(0x04, LUNINFO_PATTERN, fields)


def luninfo2_record(lun, total, scheme, fmt, marker=17, partcount=0x80):
    """ one LUNINFO2 record: ( marker, LUN id, total capacity, partition
        scheme, partition count, format, H, 3 x B ).  GetSubPageLUNs() warns
        unless the marker is 17. """
    return (marker, lun, total, scheme, partcount, fmt, 0, 0, 0, 0)


def luninfo2_page(records):
    """ LUNINFO2 sub-page (0x07): count, then eight records. """
    fields = [len(records)]
    for rec in records + [(0,) * 10] * (LUN_SLOTS - len(records)):
        fields += list(rec)
    return page(0x07, LUNINFO2_PATTERN, fields)


def responder(features=BASE_FEATURES, version=b'1.1.2', **pages):
    """ a legacy Drobo whose firmware sub-page reports the given feature bits
        and version.  pages maps 'p04' style names to sub-page bytes. """

    def respond(sub_page):
        if sub_page == 0x08:
            return firmware_page(features, version)
        key = 'p%02x' % sub_page
        if key in pages:
            return pages[key]
        return legacy_subpage(sub_page)

    return respond


def queried(dev):
    """ the page 0x3A sub-pages asked for, in order. """
    return [c[3] for c in dev.cdbs if c[0] == 0x5a and c[2] == 0x3a]


# --- GetSubPageLUNs ------------------------------------------------------

LUNINFO = luninfo_page([(19, 0, 2 * TB, 5092651008),
                        (19, 1, 1 * TB, 1234567)])

LUNINFO2 = luninfo2_page([
    luninfo2_record(0, 2 * TB - 4096, 3, 0x80),  # GPT, EXT3
    luninfo2_record(1, 1 * TB - 4096, 1, 0x02),  # MBR, NTFS
])


class TestLUNInfo(unittest.TestCase):
    """ GetSubPageLUNs() on firmware without SUPPORTS_NEW_LUNINFO2. """

    def setUp(self):
        self.dev = FakeDevice(responder=responder(p04=LUNINFO, p07=LUNINFO2))
        self.d = make_drobo(self.dev)

    def test_feature_absent(self):
        self.assertNotIn('SUPPORTS_NEW_LUNINFO2', self.d.features)

    def test_luns(self):
        """ ( LUN id, total capacity, used capacity ) per LUN; the leading H
            of each record is skipped. """
        self.assertEqual(self.d.GetSubPageLUNs(),
                         [(0, 2 * TB, 5092651008), (1, 1 * TB, 1234567)])

    def test_only_luninfo_is_read(self):
        self.dev.cdbs.clear()
        self.d.GetSubPageLUNs()
        self.assertEqual(queried(self.dev), [0x04])
        paklen = struct.calcsize('>BBH' + LUNINFO_PATTERN)
        self.assertEqual(self.dev.cdbs[0],
                         struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x04, 0, 0,
                                     0, paklen, 0))

    def test_no_luns(self):
        dev = FakeDevice(responder=responder(p04=luninfo_page([])))
        self.assertEqual(make_drobo(dev).GetSubPageLUNs(), [])

    def test_eight_luns(self):
        recs = [(19, n, (n + 1) * TB, n) for n in range(LUN_SLOTS)]
        dev = FakeDevice(responder=responder(p04=luninfo_page(recs)))
        self.assertEqual(make_drobo(dev).GetSubPageLUNs(),
                         [(n, (n + 1) * TB, n) for n in range(LUN_SLOTS)])


class TestLUNInfo2(unittest.TestCase):
    """ GetSubPageLUNs() on firmware with SUPPORTS_NEW_LUNINFO2. """

    FEATURES = BASE_FEATURES | SUPPORTS_NEW_LUNINFO2

    def setUp(self):
        self.dev = FakeDevice(responder=responder(
            self.FEATURES, p04=LUNINFO, p07=LUNINFO2))
        self.d = make_drobo(self.dev)

    def test_feature_from_firmware_page(self):
        self.assertIn('SUPPORTS_NEW_LUNINFO2', self.d.features)
        self.assertIn('SUPPORTS_NEW_LUNINFO2', self.d.fw[8])

    def test_luns(self):
        """ LUN id and total capacity come from LUNINFO2, used capacity from
            LUNINFO, scheme and format decoded from LUNINFO2. """
        self.assertEqual(self.d.GetSubPageLUNs(), [
            (0, 2 * TB - 4096, 5092651008, 'GPT', ['EXT3']),
            (1, 1 * TB - 4096, 1234567, 'MBR', ['NTFS']),
        ])

    def test_both_sub_pages_are_read(self):
        self.dev.cdbs.clear()
        self.d.GetSubPageLUNs()
        self.assertEqual(queried(self.dev), [0x04, 0x07])
        paklen = struct.calcsize('>BBH' + LUNINFO2_PATTERN)
        self.assertEqual(self.dev.cdbs[1],
                         struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x07, 0, 0,
                                     0, paklen, 0))

    def test_count_comes_from_luninfo(self):
        """ LUNINFO's count decides how many LUNs are reported. """
        one = luninfo_page([(19, 0, 2 * TB, 5092651008)])
        dev = FakeDevice(responder=responder(self.FEATURES, p04=one,
                                             p07=LUNINFO2))
        self.assertEqual(make_drobo(dev).GetSubPageLUNs(),
                         [(0, 2 * TB - 4096, 5092651008, 'GPT', ['EXT3'])])

    def test_schemes_and_formats(self):
        recs = [luninfo2_record(0, TB, 0, 0x00),  # none, FAT32
                luninfo2_record(1, TB, 2, 0x04),  # APM, HFS
                luninfo2_record(2, TB, 3, 0x08),  # GPT, EXT3 (other bit)
                luninfo2_record(3, TB, 1, 0x01)]  # MBR, not formatted
        info = luninfo_page([(19, n, TB, 0) for n in range(4)])
        dev = FakeDevice(responder=responder(self.FEATURES, p04=info,
                                             p07=luninfo2_page(recs)))
        luns = make_drobo(dev).GetSubPageLUNs()
        self.assertEqual([(l[3], l[4]) for l in luns],
                         [('No Partitions', ['FAT32']), ('APM', ['HFS']),
                          ('GPT', ['EXT3']), ('MBR', ['NO FORMAT'])])

    def test_bad_marker_warns_but_decodes(self):
        recs = [luninfo2_record(0, TB, 3, 0x80, marker=18)]
        info = luninfo_page([(19, 0, TB, 42)])
        dev = FakeDevice(responder=responder(self.FEATURES, p04=info,
                                             p07=luninfo2_page(recs)))
        d = make_drobo(dev)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            luns = d.GetSubPageLUNs()
        self.assertIn('probably have not grokked the LUNINFO2', out.getvalue())
        self.assertEqual(luns, [(0, TB, 42, 'GPT', ['EXT3'])])


# --- GetOptions ----------------------------------------------------------

OPTIONS = page(0x30, OPTIONS_PATTERN, (85, 95, 0, 0, 80, 90))


def ip(text):
    """ an IPv4 address as the 32 bit big-endian number the Drobo sends. """
    return struct.unpack('>L', socket.inet_aton(text))[0]


def options2_page(flags, spindown=15, before=('10.0.0.5', '255.0.0.0',
                                              '10.0.0.1', 1500),
                  after=('0.0.0.0', '0.0.0.0', '0.0.0.0', 0)):
    """ Options2 sub-page (0x31): flags, spin down delay, then two sets of
        ( IP address, netmask, gateway, MTU ). """
    fields = [flags, spindown]
    for addr, mask, gw, mtu in (before, after):
        fields += [ip(addr), ip(mask), ip(gw), mtu]
    return page(0x31, OPTIONS2_PATTERN, fields)


class TestOptions(unittest.TestCase):
    """ GetOptions(), read side. """

    def drobo(self, features=BASE_FEATURES, version=b'1.1.2', **pages):
        pages.setdefault('p30', OPTIONS)
        self.dev = FakeDevice(responder=responder(features, version, **pages))
        d = make_drobo(self.dev)
        self.dev.cdbs.clear()
        return d

    def test_options_without_options2(self):
        """ firmware >= 1.1.0 without SUPPORTS_OPTIONS2: only the thresholds
            of sub-page 0x30. """
        d = self.drobo(p31=options2_page(0x0007))
        self.assertNotIn('SUPPORTS_OPTIONS2', d.features)
        self.assertEqual(d.GetOptions(),
                         {'YellowThreshold': 85, 'RedThreshold': 95,
                          'OptionsFlags': 0})
        self.assertEqual(queried(self.dev), [0x30])

    def test_options_cdb(self):
        d = self.drobo()
        d.GetOptions()
        paklen = struct.calcsize('>BBH' + OPTIONS_PATTERN)
        self.assertEqual(self.dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x30, 0, 0, 0, paklen, 0)
        ])

    def test_old_firmware_has_no_options(self):
        """ firmware < 1.1.0 without SUPPORTS_OPTIONS2: None, nothing read. """
        d = self.drobo(version=b'1.0.3')
        self.assertIsNone(d.GetOptions())
        self.assertEqual(queried(self.dev), [])

    def test_options2_feature_on_old_firmware(self):
        """ the feature alone is enough, whatever the version. """
        d = self.drobo(BASE_FEATURES | SUPPORTS_OPTIONS2, b'1.0.3',
                       p31=options2_page(0))
        self.assertEqual(d.GetOptions()['YellowThreshold'], 85)

    def test_options2(self):
        d = self.drobo(BASE_FEATURES | SUPPORTS_OPTIONS2,
                       p31=options2_page(0x0005, spindown=15))
        self.assertIn('SUPPORTS_OPTIONS2', d.features)
        self.assertEqual(d.GetOptions(), {
            'YellowThreshold': 85,
            'RedThreshold': 95,
            'OptionsFlags': 0,
            'Flags': 0x0005,
            'SpinDownDelayMinutes': 15,
            'DualDiskRedundancy': True,
            'SpinDownDelay': False,
            'UseManualVolumeManagement': True,
        })
        self.assertEqual(queried(self.dev), [0x30, 0x31])

    def test_options2_cdb(self):
        d = self.drobo(BASE_FEATURES | SUPPORTS_OPTIONS2,
                       p31=options2_page(0))
        d.GetOptions()
        paklen = struct.calcsize('>BBH' + OPTIONS2_PATTERN)
        self.assertEqual(self.dev.cdbs[1],
                         struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x31, 0, 0,
                                     0, paklen, 0))

    def test_options2_flag_bits(self):
        d = self.drobo(BASE_FEATURES | SUPPORTS_OPTIONS2,
                       p31=options2_page(0x0002))
        o = d.GetOptions()
        self.assertEqual((o['DualDiskRedundancy'], o['SpinDownDelay'],
                          o['UseManualVolumeManagement']),
                         (False, True, False))

    def test_iscsi_one_address(self):
        """ iSCSI unit, second MTU zero: the first address set only. """
        d = self.drobo(BASE_FEATURES | SUPPORTS_OPTIONS2 | SUPPORTS_ISCSI,
                       p31=options2_page(0x0008))
        o = d.GetOptions()
        self.assertTrue(o['UseStaticIPAddress'])
        self.assertEqual((o['IPAddress'], o['NetMask'], o['Gateway']),
                         ('10.0.0.5', '255.0.0.0', '10.0.0.1'))
        self.assertNotIn('MTU', o)
        self.assertNotIn('IPAddress2', o)

    def test_iscsi_two_addresses(self):
        """ second MTU set: the second set is reported as IPAddress, and the
            first as IPAddress2. """
        d = self.drobo(BASE_FEATURES | SUPPORTS_OPTIONS2 | SUPPORTS_ISCSI,
                       p31=options2_page(0, after=('192.168.10.4',
                                                   '255.255.255.0',
                                                   '192.168.10.1', 9000)))
        o = d.GetOptions()
        self.assertFalse(o['UseStaticIPAddress'])
        self.assertEqual(
            (o['IPAddress'], o['NetMask'], o['Gateway'], o['MTU']),
            ('192.168.10.4', '255.255.255.0', '192.168.10.1', 9000))
        self.assertEqual(
            (o['IPAddress2'], o['NetMask2'], o['Gateway2'], o['MTU2']),
            ('10.0.0.5', '255.0.0.0', '10.0.0.1', 1500))


# --- inquire -------------------------------------------------------------


class InquiryDevice(FakeDevice):
    """ a legacy Drobo whose INQUIRY reply is inquiry_len bytes long (36 for
        a DroboPro, 76 for first generation units.) """

    def __init__(self, inquiry_len, **kw):
        super().__init__(**kw)
        self.inquiry_len = inquiry_len

    def get_sub_page(self, sz, mcb, out, DEBUG):
        if mcb[0] == 0x12:
            self.cdbs.append(bytes(mcb))
            std = b'\0' * 8 + self.vendor + self.product + b'1.00'
            return std.ljust(self.inquiry_len, b'\0')[:self.inquiry_len]
        return super().get_sub_page(sz, mcb, out, DEBUG)


class TestInquiry(unittest.TestCase):
    """ inquire(), called when the Drobo object is built. """

    INQUIRY_LEN = struct.calcsize('>BBBBBBBB8s16s4s20sBB8HH')

    def test_full_inquiry(self):
        d = make_drobo(FakeDevice())
        self.assertEqual(len(d.inquiry), 23)
        self.assertEqual(d.inquiry[8:11],
                         (b'TRUSTED ', b'Mass Storage    ', b'1.00'))

    def test_inquiry_cdb(self):
        dev = InquiryDevice(36, product=b'DroboPro        ')
        make_drobo(dev)
        inq = [c for c in dev.cdbs if c[0] == 0x12]
        self.assertEqual(inq, [bytes([0x12, 0, 0, 0, self.INQUIRY_LEN, 0])])

    def test_drobopro_36_byte_inquiry(self):
        """ a DroboPro answers with the 36 byte standard INQUIRY data only:
            accepted, and decoded as the first eleven fields. """
        dev = InquiryDevice(36, product=b'DroboPro        ')
        d = make_drobo(dev)
        self.assertEqual(d.inquiry, (0, 0, 0, 0, 0, 0, 0, 0, b'TRUSTED ',
                                     b'DroboPro        ', b'1.00'))
        self.assertEqual(d.slot_count, 4)

    def test_other_length_refused(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(Drobo.DroboException):
                make_drobo(InquiryDevice(50))
        self.assertIn('scsi inquire returned 50', out.getvalue())


if __name__ == '__main__':
    unittest.main()
