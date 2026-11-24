"""
Regression tests for drobo-utils.

No Drobo hardware is needed: the SCSI generic layer (DroboIOctl.DroboIOctl) is
replaced by a fake device that answers MODE SENSE(10) page 0x3A queries the way
first generation Drobos do (raw sub-page, starting with the 0x7a/sub_page/length
header, with no mode parameter header in front of it.)

The fixtures are made up, but use the record layouts that Drobo.py decodes.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOP)

import Drobo
import DroboIOctl

# DroboIOctl picks its transport from the device's sysfs vendor/model.  Tests
# use made up /dev names that may exist on the host (even be a real Drobo), so
# no test reads the real sysfs: tests that need one build their own tree.
DroboIOctl.SYSFS_ROOT = os.path.join(TOP, 'tests', 'no-such-sysfs')
os.environ.pop('DROBO_UTILS_TRANSPORT', None)  # nor a forced transport.

_STATE = None


def setUpModule():
    """ no test may read or write saved state in the real home directory. """
    global _STATE
    _STATE = tempfile.TemporaryDirectory()
    os.environ['DROBO_UTILS_STATE_DIR'] = _STATE.name


def tearDownModule():
    os.environ.pop('DROBO_UTILS_STATE_DIR', None)
    _STATE.cleanup()

# --- fixtures: what a first generation Drobo returns for each sub-page -------

SLOTS = 4
FEATURES = 0x0001 | 0x0002 | 0x0080 | 0x4000  # NO_AUTO_REBOOT, NO_FAT32_FORMAT, ...

FIXTURE_FIELDS = {
    # sub_page: (pack pattern, fields)  -- pattern as passed to __getsubpage
    0x01: ('BBBQ', (SLOTS, 0, 16, 0xFFFFFFFF)),
    0x02: ('QQQQ', (1000, 2000, 3000, 400)),
    0x05: ('LH32s', (1220112079, 8, b'TRUSTED DATA')),
    0x06: ('BB', (0, 10)),
    0x09: ('LL', (0, 0)),
}


def _firmware_fields():
    extra = struct.pack('>112sL32sH90s', b'', FEATURES, b'', 0, b'')
    return ('BBHBB32s16s16s240s', (1, 201, 12942, 12, 6, b'May 13 2008',
                                   b'ArmMarvell', b'1.1.2', extra))


def _slot_fields():
    rec = []
    for slot in range(SLOTS):
        rec += [slot, slot, 500107862016 * (slot < 2), 0, 1 if slot < 2 else 0,
                b'ST3500830AS' if slot < 2 else b'', b'ST3500830AS'
                if slot < 2 else b'', 0]
    return ('B' + 'HBQQB32s16sL' * SLOTS, tuple([SLOTS] + rec))


FIXTURE_FIELDS[0x08] = _firmware_fields()
FIXTURE_FIELDS[0x03] = _slot_fields()


def legacy_subpage(sub_page):
    """ bytes of a sub-page, as returned by a first generation Drobo.  A
        sub-page it does not have fails the way an unsupported page does on
        a real unit (DroboIOctl raises IOError.) """
    if sub_page not in FIXTURE_FIELDS:
        raise IOError("io_hdr status is: 2")  # unsupported page
    pattern, fields = FIXTURE_FIELDS[sub_page]
    body = struct.pack('>' + pattern, *fields)
    return struct.pack('>BBH', 0x7a, sub_page, len(body)) + body


class FakeDevice:
    """ Stand-in for DroboIOctl.DroboIOctl, records every CDB it is sent. """

    def __init__(self, vendor=b'TRUSTED ', product=b'Mass Storage    ',
                 responder=None, idlun=(1, 0, 0, 0)):
        self.vendor = vendor
        self.product = product
        self.responder = responder or legacy_subpage
        self.idlun = idlun
        self.cdbs = []

    # DroboIOctl interface used by Drobo.py --------------------------------
    def get_sub_page(self, sz, mcb, out, DEBUG):
        self.cdbs.append(bytes(mcb))
        if mcb[0] == 0x12:  # INQUIRY: 8 bytes, vendor, product, revision, ...
            std = b'\0' * 8 + self.vendor + self.product + b'1.00'
            return std.ljust(76, b'\0')[:sz]  # Gen1 answers 76 bytes.
        if mcb[0] == 0xea and getattr(self, 'ea_responder', None):
            return self.ea_responder(bytes(mcb), sz, out)
        if mcb[0] == 0x5a and mcb[2] == 0x3a:  # MODE SENSE(10), page 0x3A
            if getattr(self.responder, 'wants_cdb', False):
                return self.responder(mcb[3], mcb)[:sz]
            return self.responder(mcb[3])[:sz]
        raise IOError("FakeDevice: unexpected CDB %s" % mcb.hex())

    def identifyLUN(self):
        host, channel, id, lun = self.idlun
        return (host, channel, id, lun, self.vendor.decode(),
                self.product.decode())

    def closefd(self):
        pass


def make_drobo(dev):
    with mock.patch.object(DroboIOctl, 'DroboIOctl', lambda *a, **k: dev):
        return Drobo.Drobo('/dev/sdq', 0)


class TestLegacyQueries(unittest.TestCase):
    """ first generation behaviour must not change. """

    def setUp(self):
        self.dev = FakeDevice()
        self.d = make_drobo(self.dev)

    def test_constructor_accepts_legacy_drobo(self):
        self.assertEqual(self.d.slot_count, SLOTS)
        self.assertEqual(self.d.fw[6], 'ArmMarvell')

    def test_mode_sense_cdb_is_unchanged(self):
        """ every page 0x3A query sent to a legacy Drobo is byte for byte
            the CDB drobo-utils has always sent (DBD clear.) """
        self.dev.cdbs.clear()
        self.d.GetSubPageConfig()
        paklen = struct.calcsize('>BBHBBBQ')
        self.assertEqual(self.dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x01, 0, 0, 0, paklen, 0)
        ])

    def test_status_cdb_is_unchanged(self):
        self.dev.cdbs.clear()
        self.d.GetSubPageStatus()
        paklen = struct.calcsize('>BBHLL')
        self.assertEqual(self.dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x09, 0, 0, 0, paklen, 0)
        ])

    def test_one_query_per_sub_page(self):
        self.dev.cdbs.clear()
        self.d.GetSubPageCapacity()
        self.d.GetSubPageSettings()
        self.assertEqual(len(self.dev.cdbs), 2)

    def test_config(self):
        self.assertEqual(self.d.GetSubPageConfig(),
                         (SLOTS, 16, 0xFFFFFFFF * 512))

    def test_capacity(self):
        self.assertEqual(self.d.GetSubPageCapacity(), (1000, 2000, 3000, 400))

    def test_settings(self):
        self.assertEqual(self.d.GetSubPageSettings(),
                         (1220112079, 8, 'TRUSTED DATA'))

    def test_protocol(self):
        self.assertEqual(self.d.GetSubPageProtocol(), (0, 10))

    def test_firmware(self):
        fw = self.d.GetSubPageFirmware()
        self.assertEqual(fw[:8], (1, 201, 12942, 12, 6, 'May 13 2008',
                                  'ArmMarvell', '1.1.2'))
        self.assertIn('NO_AUTO_REBOOT', self.d.features)

    def test_status_healthy(self):
        # ( status list, relay out count )
        self.assertEqual(self.d.GetSubPageStatus(), ([], 0))

    def test_slots(self):
        slots = self.d.GetSubPageSlotInfo()
        self.assertEqual(len(slots), SLOTS)
        self.assertEqual(slots[0][0], 0)
        self.assertEqual(slots[0][4], 'ST3500830AS')
        self.assertEqual(slots[3][4], '')


class RelayoutDevice(FakeDevice):
    """ a legacy Drobo whose status sub-page reports a changing relay out
        count (blocks left to re-protect.) """

    def __init__(self):
        self.count = 0
        super().__init__(responder=self.respond)

    def respond(self, sub_page):
        if sub_page == 0x09:
            body = struct.pack('>LL', 0, self.count)
            return struct.pack('>BBH', 0x7a, 9, len(body)) + body
        return legacy_subpage(sub_page)


class TestRelayoutStatus(unittest.TestCase):
    """ GetSubPageStatus while data protection (relay out) is running. """

    def setUp(self):
        # keep any saved relay out state out of the real home directory, and
        # make clock times predictable.
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        env = mock.patch.dict(os.environ, {
            'DROBO_UTILS_STATE_DIR': self.state.name,
            'TZ': 'UTC'
        })
        self.addCleanup(time.tzset)  # cleanups run last-in first-out:
        env.start()  # restore the environment, then re-read TZ.
        self.addCleanup(env.stop)
        time.tzset()

        self.dev = RelayoutDevice()
        self.d = make_drobo(self.dev)
        self.now = 1000000.0  # 1970-01-12 13:46:40 UTC, a Monday
        patcher = mock.patch.object(Drobo.time, 'time', lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, count, minutes_later=0):
        self.now += minutes_later * 60
        self.dev.count = count
        return self.d.GetSubPageStatus()

    def test_first_reading_has_no_estimate(self):
        s, count = self.status(1000)
        self.assertEqual(count, 1000)
        self.assertIn('no estimate yet ', s)

    def test_no_progress_yet(self):
        self.status(1000)
        s, count = self.status(1000, minutes_later=5)
        self.assertIn('gathering stats, no estimate yet ', s)

    def test_blocks_left_reported(self):
        self.status(1000)
        s, count = self.status(900, minutes_later=10)
        self.assertIn('900 blocks left ', s)

    def test_estimate_in_hours(self):
        """ 10% done in 10 minutes: ~90 minutes left, shown rounded to hours
            once an hour or more is left. """
        self.status(1000)
        s, count = self.status(900, minutes_later=10)
        self.assertIn('about 2 hours left ', s)

    def test_estimate_in_minutes(self):
        self.status(1000)
        s, count = self.status(100, minutes_later=45)  # 90% in 45 min: 5 left
        self.assertIn('about 5 minutes left ', s)

    def test_estimate_never_below_one_minute(self):
        self.status(100000)
        s, count = self.status(1, minutes_later=60)
        self.assertIn('about 1 minute left ', s)

    def test_timer_resets_after_relayout_completes(self):
        self.status(1000)
        self.status(500, minutes_later=10)
        self.status(0, minutes_later=10)  # finished
        s, count = self.status(800, minutes_later=60)  # a new relay out
        self.assertIn('no estimate yet ', s)

    # --- estimated completion time -------------------------------------

    def test_completion_time_today(self):
        """ started 13:46:40, 10% done after 10 min: ~90 min left,
            so done around 15:26 the same day. """
        self.status(1000)
        s, count = self.status(900, minutes_later=10)
        self.assertIn('done around 15:26 ', s)

    def test_completion_time_another_day(self):
        """ 1% done in 10 min: ~990 min left, finishing the next day. """
        self.status(10000)
        s, count = self.status(9900, minutes_later=10)
        self.assertIn('done around Tue 06:26 ', s)

    def test_estimate_survives_separate_runs(self):
        """ drobom status is one reading per run: a second, separate Drobo
            instance (a later run) must still produce an estimate. """
        self.status(1000)
        later = make_drobo(self.dev)
        self.now += 10 * 60
        self.dev.count = 900
        s, count = later.GetSubPageStatus()
        self.assertIn('about 2 hours left ', s)

    def test_state_removed_when_complete(self):
        self.status(1000)
        self.assertTrue(os.listdir(self.state.name))
        self.status(0, minutes_later=10)
        self.assertEqual(os.listdir(self.state.name), [])

    def test_rising_count_restarts_estimate(self):
        """ the count going up means new work (e.g. another drive replaced):
            start over rather than report nonsense. """
        self.status(1000)
        s, count = self.status(1500, minutes_later=10)
        self.assertIn('no estimate yet ', s)

    def test_relayout_estimate_api(self):
        self.status(1000)
        self.now += 10 * 60
        self.dev.count = 900
        e = self.d.GetRelayoutEstimate()
        self.assertEqual(e['blocks_left'], 900)
        self.assertAlmostEqual(e['percent_done'], 10.0)
        self.assertAlmostEqual(e['minutes_left'], 90.0)
        self.assertAlmostEqual(e['eta'], self.now + 90 * 60)

    def test_relayout_estimate_api_idle(self):
        self.dev.count = 0
        self.assertIsNone(self.d.GetRelayoutEstimate())

    def test_unwritable_state_dir_still_works(self):
        with mock.patch.dict(os.environ,
                             {'DROBO_UTILS_STATE_DIR': '/proc/no/such/dir'}):
            d = make_drobo(self.dev)
            self.dev.count = 1000
            s, count = d.GetSubPageStatus()
            self.assertIn('no estimate yet ', s)
            self.now += 600
            self.dev.count = 900
            s, count = d.GetSubPageStatus()  # same instance: in memory
            self.assertIn('about 2 hours left ', s)

    def test_idle_unit_reports_nothing_extra(self):
        s, count = self.status(0)
        self.assertEqual((s, count), ([], 0))


class StatusDevice(FakeDevice):
    """ legacy Drobo whose status sub-page (0x09) has a chosen set of u32
        fields: status, relay out count [, disk pack status
        [, double degraded count [, extra]]].  Longer forms come from newer
        firmware. """

    def __init__(self, *fields, gen2=False):
        self.fields = fields
        if gen2:
            resp = gen2_responder()
            inner = resp

            def respond(sub_page, mcb):
                if sub_page == 0x09 and mcb[1] & 0x08:
                    page = self.status_page()
                    return struct.pack('>HBBBBH', 6 + len(page), 0, 0, 0, 0,
                                       0) + page
                return inner(sub_page, mcb)

            respond.wants_cdb = True
            super().__init__(responder=respond)
        else:
            super().__init__(responder=self.respond)

    def status_page(self):
        body = struct.pack('>%dL' % len(self.fields), *self.fields)
        return struct.pack('>BBH', 0x7a, 0x09, len(body)) + body

    def respond(self, sub_page):
        if sub_page == 0x09:
            return self.status_page()
        return legacy_subpage(sub_page)


class TestStatusEx(unittest.TestCase):
    """ GetStatusEx: the longer status sub-page newer firmware returns. """

    def test_legacy_12_byte_page(self):
        d = make_drobo(StatusDevice(0x0040, 0))
        e = d.GetStatusEx()
        self.assertEqual(e['flags'], ['No redundancy'])
        self.assertEqual(e['relayout_count'], 0)
        self.assertIsNone(e['diskpack_status'])
        self.assertIsNone(e['double_degraded'])

    def test_diskpack_status_16_bytes(self):
        d = make_drobo(StatusDevice(0, 0, 1))
        e = d.GetStatusEx()
        self.assertEqual(e['diskpack_status'], 1)
        self.assertEqual(e['flags'], [])

    def test_diskpack_status_maps_to_flags(self):
        """ the vendor's software folds the disk pack status into the
            status flags: 2 unknown version, 3 unknown error, 4 mismatched,
            5-7 too many missing disks. """
        for dp, flag in ((2, 'Unknown version'), (3, 'Unknown error'),
                         (4, 'Mismatched disks'),
                         (5, 'Too many missing disks'),
                         (7, 'Too many missing disks')):
            d = make_drobo(StatusDevice(0, 0, dp))
            self.assertIn(flag, d.GetStatusEx()['flags'], dp)

    def test_double_degraded_20_bytes(self):
        d = make_drobo(StatusDevice(0x0040, 500, 1, 2))
        e = d.GetStatusEx()
        self.assertEqual(e['double_degraded'], 2)
        self.assertEqual(e['relayout_count'], 500)
        self.assertIsNone(e['extra'])

    def test_extra_field_24_bytes(self):
        d = make_drobo(StatusDevice(0, 0, 1, 0, 0xdeadbeef))
        self.assertEqual(d.GetStatusEx()['extra'], 0xdeadbeef)

    def test_gen2(self):
        d = make_drobo(StatusDevice(0x0040, 7, 1, 0, gen2=True))
        e = d.GetStatusEx()
        self.assertEqual((e['relayout_count'], e['diskpack_status'],
                          e['double_degraded']), (7, 1, 0))

    def test_wrong_page_rejected(self):
        dev = StatusDevice(0, 0, 1)
        d = make_drobo(dev)
        dev.status_page = lambda: legacy_subpage(0x02)
        with self.assertRaises(Drobo.DroboException):
            d.GetStatusEx()

    def test_get_sub_page_status_unchanged(self):
        """ the old API still reads only the 12 byte form. """
        dev = StatusDevice(0x0040, 0, 4, 0)
        d = make_drobo(dev)
        self.assertEqual(d.GetSubPageStatus(), (['No redundancy'], 0))


class TestUnitStatusNames(unittest.TestCase):
    """ names of the status bits drobo-utils has always known. """

    KNOWN = [(0x0002, 'Red alert'), (0x0004, 'Yellow alert'),
             (0x0008, 'No disks'), (0x0010, 'Bad disk'),
             (0x0020, 'Too many missing disks'), (0x0040, 'No redundancy'),
             (0x0080, 'No magic hotspare'), (0x0100, 'no space left'),
             (0x0200, 'Relay out in progress'),
             (0x0400, 'Format in progress'), (0x0800, 'Mismatched disks'),
             (0x1000, 'Unknown version'),
             (0x2000, 'New firmware installed'),
             (0x4000, 'New LUN available after reboot'),
             (0x10000000, 'Unknown error')]

    def test_known_bits(self):
        for bit, name in self.KNOWN:
            self.assertEqual(Drobo._unitstatus(bit), [name], hex(bit))

    def test_combined_order(self):
        self.assertEqual(Drobo._unitstatus(0x0242),
                         ['Red alert', 'No redundancy',
                          'Relay out in progress'])

    def test_healthy(self):
        self.assertEqual(Drobo._unitstatus(0), [])

    def test_newer_bits(self):
        """ bits named by newer firmware / the vendor's software. """
        for bit, name in ((0x40000, 'Incompatible disk pack'),
                          (0x100000, 'Power supply missing'),
                          (0x200000, 'Fan partially failed'),
                          (0x400000, 'Fan failed'),
                          (0x800000, 'Fan missing')):
            self.assertEqual(Drobo._unitstatus(bit), [name], hex(bit))

    def test_diskpack_status_8_is_incompatible(self):
        d = make_drobo(StatusDevice(0, 0, 8))
        self.assertIn('Incompatible disk pack', d.GetStatusEx()['flags'])


def slot_record2(slot, life=97, temp=38, errors=0, maker=b'WDC',
                 fw=b'82.00A82', serial=b'WD-WCC4N1234567', total=3000592982016,
                 managed=2999000000000, stride=108):
    """ one sub-page 0x35 per-drive record (108 bytes, big endian.) """
    rec = struct.pack('>BBHLBBBB44s12s24sQQ', slot, 1, errors, 2, 1, temp,
                      life, 72, maker, fw, serial, total, managed)
    assert len(rec) == 108
    return rec.ljust(stride, b'\0')


def slotinfo2_page(records, stride=108):
    body = struct.pack('>LBBH', 0, len(records), stride, 0) + b''.join(records)
    return struct.pack('>BBH', 0x7a, 0x35, len(body)) + body


class SlotInfo2Device(FakeDevice):

    def __init__(self, page=None, gen2=False):
        self.page = page
        if gen2:
            inner = gen2_responder()

            def respond(sub_page, mcb):
                if sub_page == 0x35 and mcb[1] & 0x08:
                    return struct.pack('>HBBBBH', 6 + len(self.page), 0, 0, 0,
                                       0, 0) + self.page
                if sub_page == 0x35:
                    return b'\x00\x06' + b'\0' * 6
                return inner(sub_page, mcb)

            respond.wants_cdb = True
            super().__init__(responder=respond)
        else:
            super().__init__(responder=self.respond)

    def respond(self, sub_page):
        if sub_page == 0x35:
            if self.page is None:
                raise IOError("io_hdr status is: 2")  # unsupported page
            return self.page
        return legacy_subpage(sub_page)


class TestSlotInfo2(unittest.TestCase):
    """ GetSlotInfo2: per-drive health from sub-page 0x35. """

    def test_fields(self):
        page = slotinfo2_page([slot_record2(0, errors=3),
                               slot_record2(1, life=12, temp=51)])
        d = make_drobo(SlotInfo2Device(page))
        s = d.GetSlotInfo2()
        self.assertEqual(len(s), 2)
        self.assertEqual(s[0]['slot'], 0)
        self.assertEqual(s[0]['errors'], 3)
        self.assertEqual(s[0]['manufacturer'], 'WDC')
        self.assertEqual(s[0]['firmware'], '82.00A82')
        self.assertEqual(s[0]['serial'], 'WD-WCC4N1234567')
        self.assertEqual(s[0]['total_capacity'], 3000592982016)
        self.assertEqual(s[0]['managed_capacity'], 2999000000000)
        self.assertEqual((s[1]['life_remaining'], s[1]['temperature']),
                         (12, 51))
        self.assertEqual(s[1]['rotational_speed'], 72)
        self.assertEqual((s[1]['status'], s[1]['disk_state'],
                          s[1]['disk_type']), (1, 2, 1))

    def test_record_stride_from_header(self):
        page = slotinfo2_page(
            [slot_record2(i, stride=112) for i in range(3)], stride=112)
        s = make_drobo(SlotInfo2Device(page)).GetSlotInfo2()
        self.assertEqual([r['slot'] for r in s], [0, 1, 2])
        self.assertEqual(s[2]['serial'], 'WD-WCC4N1234567')

    def test_full_width_text_is_safe(self):
        page = slotinfo2_page([slot_record2(0, maker=b'M' * 44,
                                            serial=b'S' * 24)])
        s = make_drobo(SlotInfo2Device(page)).GetSlotInfo2()
        self.assertTrue(s[0]['manufacturer'].startswith('MMMM'))
        self.assertTrue(s[0]['serial'].startswith('SSSS'))

    def test_unsupported_firmware_returns_none(self):
        d = make_drobo(SlotInfo2Device(None))
        self.assertIsNone(d.GetSlotInfo2())

    def test_drive_type_codes(self):
        """ one byte gives both media and interface. """
        for code, want in ((0, ('HDD', 'SATA')), (1, ('HDD', 'SAS')),
                           (2, ('SSD', 'SATA')), (3, ('SSD', 'SAS')),
                           (4, ('SSD', 'mSATA')), (255, ('', '')),
                           (9, ('unknown', 'unknown'))):
            self.assertEqual(Drobo._drivetype(code), want, code)

    def test_disk_health_codes(self):
        """ health is the low four bits of the disk state. """
        for state, want in ((0, 'Good'), (1, 'Healed'), (2, 'Warning'),
                            (3, 'Failed'), (0x12, 'Warning'), (7, 'Unknown')):
            self.assertEqual(Drobo._diskhealth(state), want, state)

    def test_decoded_fields(self):
        page = slotinfo2_page([slot_record2(0)])  # state 2, type 1
        s = make_drobo(SlotInfo2Device(page)).GetSlotInfo2()
        self.assertEqual((s[0]['health'], s[0]['media'], s[0]['interface']),
                         ('Warning', 'HDD', 'SAS'))
        self.assertEqual((s[0]['disk_state'], s[0]['disk_type']), (2, 1))

    def test_gen2(self):
        page = slotinfo2_page([slot_record2(i) for i in range(5)])
        s = make_drobo(SlotInfo2Device(page, gen2=True)).GetSlotInfo2()
        self.assertEqual(len(s), 5)


class TestVendorCommands(unittest.TestCase):
    """ the 0xEA vendor unique commands drobo-utils already sends. """

    def test_blink_cdb_is_unchanged(self):
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        d = make_drobo(dev)
        txid = d.transactionID
        dev.cdbs.clear()
        d.Blink()
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0xea, 0x10, 0x00, 0x06, 0x00, txid,
                        0x20, 0x01, 0x00)
        ])

    def test_standby_cdb_is_unchanged(self):
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        d = make_drobo(dev)
        txid = d.transactionID
        dev.cdbs.clear()
        with mock.patch.object(d, 'umount', return_value=True):
            d.Standby()
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0xea, 0x10, 0x00, 0x0d, 0x00, txid,
                        0x20, 0x01, 0x00)
        ])

    def test_blink_and_standby_advance_the_transaction_id(self):
        """ each command carries the current id, then advances it by one
            (251, the largest id, wraps to 1.) """
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        d = make_drobo(dev)
        d.transactionID = Drobo.MAX_TRANSACTION + 1
        dev.cdbs.clear()
        d.Blink()
        with mock.patch.object(d, 'umount', return_value=True):
            d.Standby()
        d.Blink()
        self.assertEqual([c[5] for c in dev.cdbs],
                         [Drobo.MAX_TRANSACTION + 1, 1, 2])
        self.assertEqual(d.transactionID, 3)


def lunex_entry(index, name=b'drobo', total=17592186044416, second=4398046511104,
                fmt=0x08, lun=None):
    """ one 285 byte extended LUN info entry (big endian.) """
    e = struct.pack('>BL258sBBQQL', index, index if lun is None else lun, name,
                    0, 1, total, second, fmt)
    assert len(e) == 285
    return e


def lunex_reply(entries, more=False):
    return struct.pack('BBB', 0, len(entries), 1 if more else 0) + b''.join(
        entries)


class LUNExDevice(FakeDevice):
    """ legacy Drobo answering the extended LUN info system operation
        (0xEA, read, op 0x81), paged a few entries per reply. """

    def __init__(self, luns, per_reply=3, supported=True, always_more=False):
        super().__init__()
        self.luns = luns
        self.per_reply = per_reply
        self.supported = supported
        self.always_more = always_more
        self.ea_responder = self.respond_ea

    def respond_ea(self, mcb, sz, out):
        if not self.supported:
            raise IOError("io_hdr status is: 2")
        start = mcb[4]
        rest = [e for e in self.luns if e[0] >= start]
        page = rest[:self.per_reply]
        more = self.always_more or len(rest) > self.per_reply
        return lunex_reply(page, more)[:sz]


class TestLUNsInfoEx(unittest.TestCase):
    """ GetLUNsInfoEx: extended LUN info via a read system operation. """

    def test_fields(self):
        d = make_drobo(LUNExDevice([lunex_entry(0, name=b'Volume 1')]))
        l = d.GetLUNsInfoEx()
        self.assertEqual(len(l), 1)
        self.assertEqual(l[0]['index'], 0)
        self.assertEqual(l[0]['name'], 'Volume 1')
        self.assertEqual(l[0]['total_capacity'], 17592186044416)
        self.assertEqual(l[0]['second_capacity'], 4398046511104)
        self.assertEqual(l[0]['format'], 0x08)
        self.assertEqual(l[0]['format_name'], Drobo._partformat(0x08))

    def test_exact_cdb(self):
        """ byte for byte what the vendor's software sends: a read (0x80)
            system operation 0x81 from LUN 0, 1024 bytes. """
        dev = LUNExDevice([lunex_entry(0)])
        d = make_drobo(dev)
        dev.cdbs.clear()
        d.GetLUNsInfoEx()
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0xea, 0x10, 0x80, 0x81, 0, 0, 0x21,
                        1024, 0)
        ])

    def test_paging(self):
        dev = LUNExDevice([lunex_entry(i) for i in range(7)], per_reply=3)
        d = make_drobo(dev)
        dev.cdbs.clear()
        l = d.GetLUNsInfoEx()
        self.assertEqual([e['index'] for e in l], list(range(7)))
        self.assertEqual([c[4] for c in dev.cdbs], [0, 3, 6])

    def test_paging_is_bounded(self):
        dev = LUNExDevice([lunex_entry(0)], always_more=True)
        d = make_drobo(dev)
        dev.cdbs.clear()
        d.GetLUNsInfoEx()
        self.assertLessEqual(len(dev.cdbs), 32)

    def test_unsupported_firmware_returns_none(self):
        d = make_drobo(LUNExDevice([], supported=False))
        self.assertIsNone(d.GetLUNsInfoEx())

    def test_only_allowed_system_operations(self):
        """ the read helper must refuse any other operation: others include
            firmware, standby and power off. """
        dev = LUNExDevice([lunex_entry(0)])
        d = make_drobo(dev)
        dev.cdbs.clear()
        for op in (0x00, 0x06, 0x0d, 0x80, 0x82, 0xff):
            with self.assertRaises(Drobo.DroboException):
                d._Drobo__system_read(op, 0, 1024)
        self.assertEqual(dev.cdbs, [])


class TestConstructorSanityChecks(unittest.TestCase):

    def test_rejects_too_few_slots(self):
        def responder(sub_page):
            if sub_page == 0x01:
                body = struct.pack('>BBBQ', 2, 0, 16, 1)
                return struct.pack('>BBH', 0x7a, 1, len(body)) + body
            return legacy_subpage(sub_page)

        with self.assertRaises(Drobo.DroboException):
            make_drobo(FakeDevice(responder=responder))

    def test_rejects_short_answer(self):
        """ a device answering with only a mode parameter header is refused
            (instead of being mis-parsed.) """
        dev = FakeDevice(responder=lambda sp: b'\0\x06' + b'\0' * 6)
        with self.assertRaises(Exception):
            make_drobo(dev)


def gen2_responder(block_descriptors=b''):
    """ answers like the Drobo S Gen 2 over USB 3: a standard MODE SENSE(10)
        mode parameter header in front of the page.  Asked without DBD (the
        legacy CDB), it returns the header alone, as seen on real hardware:
        00 06 00 00 00 00 00 00 """

    def respond(sub_page, mcb):
        if not mcb[1] & 0x08:  # DBD clear: header only, no page.
            return b'\x00\x06' + b'\0' * 6
        page = legacy_subpage(sub_page)
        bdl = len(block_descriptors)
        hdr = struct.pack('>HBBBBH', 6 + bdl + len(page), 0, 0, 0, 0, bdl)
        return hdr + block_descriptors + page

    respond.wants_cdb = True
    return respond


class TestGen2ModeParameterHeader(unittest.TestCase):
    """ Drobo S Gen 2 (USB 3): page 0x3A behind a mode parameter header. """

    def test_constructor_and_decoding(self):
        d = make_drobo(FakeDevice(responder=gen2_responder()))
        self.assertEqual(d.slot_count, SLOTS)
        self.assertEqual(d.GetSubPageCapacity(), (1000, 2000, 3000, 400))
        self.assertEqual(d.GetSubPageSettings(),
                         (1220112079, 8, 'TRUSTED DATA'))
        self.assertEqual(d.GetSubPageFirmware()[:8],
                         (1, 201, 12942, 12, 6, 'May 13 2008', 'ArmMarvell',
                          '1.1.2'))
        self.assertEqual(len(d.GetSubPageSlotInfo()), SLOTS)

    def test_one_dbd_query_once_detected(self):
        """ after the first query, Gen 2 gets one DBD CDB per query. """
        dev = FakeDevice(responder=gen2_responder())
        d = make_drobo(dev)
        dev.cdbs.clear()
        d.GetSubPageCapacity()
        self.assertEqual(len(dev.cdbs), 1)
        cdb = dev.cdbs[0]
        self.assertEqual(cdb[:4], bytes([0x5a, 0x08, 0x3a, 0x02]))
        alloc = struct.unpack('>H', cdb[7:9])[0]
        self.assertGreaterEqual(alloc, struct.calcsize('>BBHQQQQ') + 8)

    def test_block_descriptors_skipped(self):
        dev = FakeDevice(responder=gen2_responder(b'\xaa' * 8))
        d = make_drobo(dev)
        self.assertEqual(d.GetSubPageCapacity(), (1000, 2000, 3000, 400))

    def test_wrong_page_rejected(self):
        """ a header followed by some other page is not mis-parsed. """

        def respond(sub_page, mcb):
            if not mcb[1] & 0x08:
                return b'\x00\x06' + b'\0' * 6
            page = legacy_subpage(0x02 if sub_page != 0x02 else 0x05)
            return struct.pack('>HBBBBH', 6 + len(page), 0, 0, 0, 0, 0) + page

        respond.wants_cdb = True
        with self.assertRaises(Exception):
            make_drobo(FakeDevice(responder=respond))


class TestDiscovery(unittest.TestCase):
    """ DroboIOctl.drobolunlist vendor matching and LUN grouping. """

    def discover(self, devices, vendor="Drobo"):
        names = sorted(devices)

        def factory(path, *a, **k):
            return devices[os.path.basename(path)]

        with mock.patch.object(DroboIOctl.os, 'listdir', lambda d: names), \
             mock.patch.object(DroboIOctl, 'DroboIOctl', factory):
            return DroboIOctl.drobolunlist(0, vendor)

    def test_known_vendors(self):
        found = self.discover({
            'sda': FakeDevice(vendor=b'ATA     ', idlun=(0, 0, 0, 0)),
            'sdb': FakeDevice(vendor=b'TRUSTED ', idlun=(1, 0, 0, 0)),
            'sdc': FakeDevice(vendor=b'Drobo   ', idlun=(2, 0, 0, 0)),
        })
        self.assertEqual(found, [['/dev/sdb'], ['/dev/sdc']])

    def test_multi_lun_grouping(self):
        found = self.discover({
            'sdb': FakeDevice(idlun=(1, 0, 0, 0)),
            'sdc': FakeDevice(idlun=(1, 0, 0, 1)),
        })
        self.assertEqual(found, [['/dev/sdb', '/dev/sdc']])

    def test_vendor_string_option(self):
        """ drobom -s <string> adds a vendor to match. """
        devs = {'sdb': FakeDevice(vendor=b'Newco   ', idlun=(1, 0, 0, 0))}
        self.assertEqual(self.discover(devs), [])
        self.assertEqual(self.discover(devs, 'Newco'), [['/dev/sdb']])

    def test_drobo_s_gen2_found_by_product(self):
        """ the Drobo S Gen 2 on USB 3 reports vendor 'USB 3.0', so is found
            by its product id, without needing -s. """
        found = self.discover({
            'sdb': FakeDevice(vendor=b'USB 3.0 ',
                              product=b'Drobo S - Gen 2 ',
                              idlun=(1, 0, 0, 0)),
        })
        self.assertEqual(found, [['/dev/sdb']])

    def test_generic_usb3_disk_rejected(self):
        found = self.discover({
            'sdb': FakeDevice(vendor=b'USB 3.0 ',
                              product=b'Portable SSD    ',
                              idlun=(1, 0, 0, 0)),
        })
        self.assertEqual(found, [])

    def test_ignores_non_disk_names(self):
        found = self.discover({
            'sdb': FakeDevice(idlun=(1, 0, 0, 0)),
            'sdb1': FakeDevice(idlun=(1, 0, 0, 0)),
        })
        self.assertEqual(found, [['/dev/sdb']])


class TestSimulationMode(unittest.TestCase):
    """ -v 128 runs without hardware, end to end through drobom. """

    def drobom(self, *args):
        return subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                               '-v', '128'] + list(args),
                              cwd=TOP,
                              capture_output=True,
                              text=True,
                              timeout=60)

    def test_list(self):
        r = self.drobom('list')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('/dev/sdx:/dev/sdy', r.stdout)

    def test_status(self):
        r = self.drobom('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('TRUSTED DATA', r.stdout)

    def test_info_volumes(self):
        r = self.drobom('info', 'volumes')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Name', r.stdout)

    def test_info_luns(self):
        r = self.drobom('info', 'luns')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_info_slots(self):
        r = self.drobom('info', 'slots')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('ST3500830AS', r.stdout)

    def test_info_drives(self):
        r = self.drobom('info', 'drives')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Life', r.stdout)
        self.assertIn('Health', r.stdout)
        self.assertIn('SATA', r.stdout)

    def test_info_health(self):
        r = self.drobom('info', 'health')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('redundancy', r.stdout.lower())

    def test_info_status(self):
        r = self.drobom('info', 'status')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_info(self):
        r = self.drobom('info', 'config,capacity,firmware,settings,protocol')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('(8, 16, 2199023250944)', r.stdout)


if __name__ == '__main__':
    unittest.main()
