"""
Features a unit's firmware does not implement must degrade cleanly.

Static analysis of Drobo S firmware 2.1.5 (see DEVELOPERS.rst, "What
firmware 2.1.5 lacks") shows it has no page 0x3A sub-pages 0x33, 0x35 or
0x80, and no 0xEA system operations 0x22, 0x81 or 0x83.  drobo-utils reads
some of those for newer features.  A real unit can refuse such a request in
more than one way:

  1. CHECK CONDITION: DroboIOctl raises IOError ("io_hdr status is: 2").
  2. GOOD status, but no page (only a mode parameter header, as a Drobo S
     Gen 2 returned while rebuilding), a page with the right header but no
     body, or a page whose header is for another sub-page.
  3. for the 0xEA operations, GOOD status with an empty (or too short)
     reply.

For every feature and every failure mode: the library returns None (or its
documented fallback), drobom prints one line, nothing raises, and only a
small, bounded number of CDBs is sent.  The rest of the unit (the default
'drobom info' list, 'status', 'info health') must keep working.

No hardware: see test_regression for the fake device.
"""

import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (FakeDevice, make_drobo, legacy_subpage,
                             FIXTURE_FIELDS, setUpModule, tearDownModule)
from test_cov_drobom import DrobomTestCase

import Drobo

UNSUPPORTED_SUBPAGES = (0x33, 0x35, 0x80)  # not in firmware 2.1.5
UNSUPPORTED_OPS = (0x22, 0x81, 0x83)  # 0xEA system operations, ditto

EX2_FEATURE = 0x800000  # SUPPORTS_LUNINFO_EX2

GEN2_HEADER_ONLY = b'\x00\x06' + b'\0' * 6  # mode parameter header, no page

# how an unsupported sub-page is refused (see the module docstring.)
PAGE_MODES = ('check_condition', 'header_only', 'empty_page', 'short_page',
              'other_subpage')
# how an unsupported 0xEA operation is refused.
OP_MODES = ('check_condition', 'empty', 'short')


def firmware_page(features):
    """ sub-page 0x08 of a unit running 2.1.5 with the given feature bits. """
    extra = struct.pack('>112sL32sH90s', b'', features, b'', 0, b'')
    body = struct.pack('>BBHBB32s16s16s240s', 2, 1, 12942, 12, 6,
                       b'Apr 15 2013', b'ArmMarvell', b'2.1.5', extra)
    return struct.pack('>BBH', 0x7a, 0x08, len(body)) + body


# sub-pages 2.1.5 has that test_regression's fixture leaves out.
EXTRA_PAGES = {
    0x30: ('BBBIBB', (85, 95, 0, 0, 0, 0)),  # options
    0x04: ('B' + 'HBQQ' * 8,  # LUN info: one LUN
           (1, 0, 0, 2199023255552, 1099511627776) + (0, 0, 0, 0) * 7),
}


def supported_page(sub_page):
    if sub_page in EXTRA_PAGES:
        pattern, fields = EXTRA_PAGES[sub_page]
        body = struct.pack('>' + pattern, *fields)
        return struct.pack('>BBH', 0x7a, sub_page, len(body)) + body
    return legacy_subpage(sub_page)


def refused_page(sub_page, mode):
    """ the raw page (no mode parameter header) a unit returns for an
        unsupported sub_page, or None for 'no page at all'. """
    if mode == 'check_condition':
        raise IOError("io_hdr status is: 2")
    if mode == 'header_only':
        return None
    if mode == 'empty_page':  # right header, zero length
        return struct.pack('>BBH', 0x7a, sub_page, 0)
    if mode == 'short_page':  # right header, a few bytes only
        return struct.pack('>BBH', 0x7a, sub_page, 4) + b'\0' * 4
    if mode == 'other_subpage':  # the page of another sub-page
        return legacy_subpage(0x01)
    raise ValueError(mode)


class Fw215Device(FakeDevice):
    """ a unit running firmware 2.1.5: answers the sub-pages it implements,
        refuses 0x33/0x35/0x80 and 0xEA ops 0x22/0x81/0x83 in the chosen
        way.

        style 'legacy': a first generation unit, page 0x3A comes back raw.
        style 'gen2': Drobo S Gen 2 over USB 3; the legacy CDB gets only a
        mode parameter header, DBD gets the page behind one. """

    def __init__(self, page_mode='check_condition', op_mode='check_condition',
                 style='legacy', features=0x0001 | 0x0002 | 0x0080):
        self.page_mode = page_mode
        self.op_mode = op_mode
        self.style = style
        self.features = features

        def respond(sub_page, mcb):
            return self.respond_page(sub_page, mcb)

        respond.wants_cdb = True
        super().__init__(responder=respond)
        self.ea_responder = self.respond_ea

    def page(self, sub_page):
        if sub_page == 0x08:
            return firmware_page(self.features)
        if sub_page in UNSUPPORTED_SUBPAGES:
            return refused_page(sub_page, self.page_mode)
        return supported_page(sub_page)

    def respond_page(self, sub_page, mcb):
        dbd = bool(mcb[1] & 0x08)
        if self.style == 'gen2' and not dbd:
            return GEN2_HEADER_ONLY
        page = self.page(sub_page)
        if page is None:
            return GEN2_HEADER_ONLY
        if not dbd:
            return page
        return struct.pack('>HBBBBH', 6 + len(page), 0, 0, 0, 0, 0) + page

    def respond_ea(self, mcb, sz, out):
        if mcb[3] not in UNSUPPORTED_OPS:
            raise IOError("FakeDevice: unexpected 0xEA op 0x%02x" % mcb[3])
        if self.op_mode == 'check_condition':
            raise IOError("io_hdr status is: 2")
        if self.op_mode == 'empty':
            return b''
        if self.op_mode == 'short':
            return b'\x00\x01'
        raise ValueError(self.op_mode)

    def subpages_asked(self):
        return [c[3] for c in self.cdbs if c[0] == 0x5a]

    def ops_sent(self):
        return [c[3] for c in self.cdbs if c[0] == 0xea]


STYLES = ('legacy', 'gen2')
MAX_CDBS = 2  # legacy CDB, then the DBD retry: nothing more per read


class LibraryCase(unittest.TestCase):

    def unit(self, page_mode='check_condition', op_mode='check_condition',
             style='legacy', features=0x0001 | 0x0002 | 0x0080):
        dev = Fw215Device(page_mode, op_mode, style, features)
        d = make_drobo(dev)
        dev.cdbs.clear()
        return dev, d

    def each_page_mode(self):
        """ (case, device, Drobo) for each style and refusal mode: run
            the checks for each under self.subTest(**case). """
        for style in STYLES:
            for mode in PAGE_MODES:
                yield (dict(style=style, mode=mode),) + self.unit(
                    page_mode=mode, style=style)


class TestFixture(LibraryCase):
    """ the fake itself: the sub-pages 2.1.5 has still work, in both
        styles, after an unsupported read. """

    def test_supported_pages_work(self):
        for case, dev, d in self.each_page_mode():
            with self.subTest(**case):
                self.assertEqual(d.slot_count, 4)
                self.assertEqual(d.fw[7], '2.1.5')
                d.GetSlotInfo2()
                self.assertEqual(d.GetSubPageCapacity(), (1000, 2000, 3000, 400))
                self.assertEqual(len(d.GetSubPageSlotInfo()), 4)

    def test_gen2_style_detected(self):
        dev, d = self.unit(style='gen2')
        self.assertEqual(d.mode_sense_style, 'dbd')


class TestSlotInfo2(LibraryCase):
    """ GetSlotInfo2 (sub-page 0x35, 'drobom info drives'.) """

    def test_none_and_bounded(self):
        for case, dev, d in self.each_page_mode():
            with self.subTest(**case):
                self.assertIsNone(d.GetSlotInfo2())
                self.assertEqual(set(dev.subpages_asked()), {0x35})
                self.assertLessEqual(len(dev.cdbs), MAX_CDBS)

    def test_style_not_changed(self):
        """ a refused sub-page must not switch how later pages are read. """
        for case, dev, d in self.each_page_mode():
            with self.subTest(**case):
                before = d.mode_sense_style
                d.GetSlotInfo2()
                self.assertEqual(d.mode_sense_style, before)


class TestSerial(LibraryCase):
    """ GetSerial (sub-page 0x80, 'drobom info serial'.) """

    def test_none_and_bounded(self):
        for case, dev, d in self.each_page_mode():
            with self.subTest(**case):
                self.assertIsNone(d.GetSerial())
                self.assertEqual(set(dev.subpages_asked()), {0x80})
                self.assertLessEqual(len(dev.cdbs), MAX_CDBS)


class TestRedundancyLevel(LibraryCase):
    """ GetRedundancyLevel: 0x35 refused, so it falls back to 0x03. """

    def test_falls_back_to_slot_info(self):
        for case, dev, d in self.each_page_mode():
            with self.subTest(**case):
                expected = Drobo._redundancylevel(
                    [s[1] for s in d.GetSubPageSlotInfo()],
                    d.GetSubPageCapacity()[2])
                dev.cdbs.clear()
                self.assertEqual(d.GetRedundancyLevel(), expected)
                asked = dev.subpages_asked()
                self.assertIn(0x03, asked)
                self.assertLessEqual(asked.count(0x35), MAX_CDBS)
                self.assertLessEqual(len(dev.cdbs), 2 * MAX_CDBS + 2)


class TestLUNsInfo(LibraryCase):
    """ GetLUNsInfoEx (op 0x81), GetLUNsInfoEx2 (op 0x83, only sent when
        the unit advertises it) and GetLUNsInfoBest ('drobom info
        volumes'.) """

    def each_op_mode(self, features=0x0001):
        for style in STYLES:
            for mode in OP_MODES:
                yield (dict(style=style, mode=mode),) + self.unit(
                    op_mode=mode, style=style, features=features)

    def test_ex_none_and_bounded(self):
        for case, dev, d in self.each_op_mode():
            with self.subTest(**case):
                self.assertIsNone(d.GetLUNsInfoEx())
                self.assertEqual(dev.ops_sent(), [0x81])

    def test_ex2_not_sent_on_2_1_5(self):
        """ 2.1.5 does not advertise SUPPORTS_LUNINFO_EX2: nothing sent. """
        for case, dev, d in self.each_op_mode():
            with self.subTest(**case):
                self.assertIsNone(d.GetLUNsInfoEx2())
                self.assertEqual(dev.cdbs, [])

    def test_ex2_none_and_bounded_when_advertised(self):
        """ should a unit advertise the feature but refuse the op. """
        for case, dev, d in self.each_op_mode(EX2_FEATURE):
            with self.subTest(**case):
                self.assertIsNone(d.GetLUNsInfoEx2())
                self.assertEqual(dev.ops_sent(), [0x83])

    def test_best_none_and_bounded(self):
        for case, dev, d in self.each_op_mode():
            with self.subTest(**case):
                self.assertIsNone(d.GetLUNsInfoBest())
                self.assertEqual(dev.ops_sent(), [0x81])

    def test_best_none_and_bounded_when_ex2_advertised(self):
        for case, dev, d in self.each_op_mode(EX2_FEATURE):
            with self.subTest(**case):
                self.assertIsNone(d.GetLUNsInfoBest())
                self.assertEqual(dev.ops_sent(), [0x83, 0x81])


class TestNeverAsked(unittest.TestCase):
    """ nothing in drobo-utils reads sub-page 0x33 or sends op 0x22. """

    def test_no_reader(self):
        with open(Drobo.__file__) as f:
            src = f.read()
        self.assertNotIn(0x33, Drobo.Drobo._SYSTEM_READ_OPS)
        self.assertNotIn(0x22, Drobo.Drobo._SYSTEM_READ_OPS)
        self.assertNotIn('__getsubpage_raw(0x33', src)
        self.assertNotIn("__getsubpage(0x33", src)


# --- drobom ---------------------------------------------------------------------


class ScsiFw215Device(Fw215Device):
    """ also answers DroboIOctl.version() ('info scsi'.) """

    def version(self):
        return 3


DRIVES_MSG = 'per-drive details are not supported by this firmware\n'
VOLUMES_MSG = 'extended volume information is not supported by this firmware\n'
SERIAL_MSG = ('Serial: not reported by the unit\n'
              'VPD serial (/dev/sdq): not available\n')


class TestDrobom(DrobomTestCase):
    """ drobom on a 2.1.5 unit: one line per unsupported feature. """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(Drobo, 'vpd_serial', lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def run_on(self, dev, *args):
        with self.device(dev):
            return self.drobom(*args)

    def terse(self, dev, *args):
        """ run with -v 0: no chatty banner around 'info' output. """
        return self.run_on(dev, '-v', '0', *args)

    def each_unit(self, features=0x0001 | 0x0002 | 0x0080):
        for style in STYLES:
            for page_mode in PAGE_MODES:
                for op_mode in OP_MODES:
                    yield (dict(style=style, page_mode=page_mode,
                                op_mode=op_mode),
                           ScsiFw215Device(page_mode, op_mode, style,
                                           features))

    def test_info_drives(self):
        for case, dev in self.each_unit():
            with self.subTest(**case):
                r = self.terse(dev, 'info', 'drives')
                self.assertEqual(r.out, DRIVES_MSG)

    def test_info_serial(self):
        for case, dev in self.each_unit():
            with self.subTest(**case):
                r = self.terse(dev, 'info', 'serial')
                self.assertEqual(r.out, SERIAL_MSG)

    def test_info_volumes(self):
        for features in (0x0001, EX2_FEATURE):
            for case, dev in self.each_unit(features):
                with self.subTest(**case):
                    r = self.terse(dev, 'info', 'volumes')
                    self.assertEqual(r.out, VOLUMES_MSG)

    def test_info_health(self):
        for case, dev in self.each_unit():
            with self.subTest(**case):
                r = self.terse(dev, 'info', 'health')
                self.assertIn('Redundancy: ', r.out)
                self.assertIn('Redundancy level: ', r.out)
                self.assertNotIn('undetermined', r.out)
                self.assertIn('Status: OK\n', r.out)

    def test_status(self):
        for case, dev in self.each_unit():
            with self.subTest(**case):
                r = self.run_on(dev, 'status')
                self.assertEqual(r.out,
                                 '/dev/sdq - TRUSTED DATA 66% full - ([], 0)\n')

    def test_default_info_list(self):
        for case, dev in self.each_unit():
            with self.subTest(**case):
                r = self.run_on(dev, 'info')
                self.assertIn(DRIVES_MSG, r.out)
                self.assertIn(VOLUMES_MSG, r.out)
                self.assertIn('Redundancy level: ', r.out)
                self.assertIn('Firmware: 2.1.5', r.out)
                self.assertNotIn('Traceback', r.out + r.err)
                # unsupported reads are bounded, and 0x33 / op 0x22 never sent.
                asked = dev.subpages_asked()
                self.assertNotIn(0x33, asked)
                self.assertLessEqual(asked.count(0x35), 2 * MAX_CDBS)
                self.assertNotIn(0x80, asked)  # serial: only when asked for
                self.assertEqual(dev.ops_sent(), [0x81])


if __name__ == '__main__':
    unittest.main()
