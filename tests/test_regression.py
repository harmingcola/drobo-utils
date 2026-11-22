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
import unittest
from unittest import mock

TOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOP)

import Drobo
import DroboIOctl

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
    """ bytes of a sub-page, as returned by a first generation Drobo. """
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

    def test_info(self):
        r = self.drobom('info', 'config,capacity,firmware,settings,protocol')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('(8, 16, 2199023250944)', r.stdout)


if __name__ == '__main__':
    unittest.main()
