"""
Tests for the unit serial number: GetSerial() reads MODE SENSE page 0x3A
sub-page 0x80 (4 byte header, 24 character string, believed only if it
starts with "DR", as the vendor's ESADeviceSerial::parse checks; facts
from ReDrobo docs/PROTOCOL.md), and vpd_serial() reads the INQUIRY unit
serial number page (VPD 0x80) that the kernel may have cached in sysfs.
Neither is asked for by default.

No Drobo hardware is needed: see test_regression for the fake device.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v
"""

import ast
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (FakeDevice, make_drobo, legacy_subpage, TOP,
                             setUpModule, tearDownModule)
from test_fixtures_extra import queried

import Drobo

SERIAL = b'DRB154001A00123'


def serial_page(text=SERIAL):
    body = text.ljust(24, b'\0')[:24]
    return struct.pack('>BBH', 0x7a, 0x80, len(body)) + body


def serial_responder(page=None, refuse=False):
    def respond(sub_page):
        if sub_page == 0x80:
            if refuse:
                raise IOError("io_hdr status is: 2")
            return page
        return legacy_subpage(sub_page)

    return respond


def drobo(page=None, refuse=False):
    dev = FakeDevice(responder=serial_responder(page or serial_page(),
                                                refuse))
    d = make_drobo(dev)
    dev.cdbs.clear()
    return d, dev


def drobom_names(name):
    with open(os.path.join(TOP, 'drobom')) as f:
        tree = ast.parse(f.read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and n.targets[0].id == name:
            return ast.literal_eval(n.value)
    raise KeyError(name)


DEFAULT_INFO = ("config", "capacity", "protocol", "scsi", "slots", "firmware",
                "status", "options", "luns", "health", "drives", "volumes")


class TestSerialRegression(unittest.TestCase):
    """ nothing reads sub-page 0x80 today, and 'info' prints the same set. """

    def test_default_info_options(self):
        self.assertEqual(drobom_names('valid_print'), DEFAULT_INFO)

    def test_existing_queries_never_ask_0x80(self):
        d, dev = drobo()
        d.GetSubPageConfig()
        d.GetSubPageCapacity()
        d.GetSubPageSettings()
        d.GetSubPageFirmware()
        d.GetSubPageStatus()
        d.GetSubPageSlotInfo()
        self.assertNotIn(0x80, queried(dev))

    def test_constructor_never_asks_0x80(self):
        dev = FakeDevice(responder=serial_responder(serial_page()))
        make_drobo(dev)
        self.assertNotIn(0x80, queried(dev))


class TestGetSerial(unittest.TestCase):

    def test_serial(self):
        d, _ = drobo()
        self.assertEqual(d.GetSerial(), 'DRB154001A00123')

    def test_full_width(self):
        d, _ = drobo(serial_page(b'DR' + b'9' * 22))
        self.assertEqual(d.GetSerial(), 'DR' + '9' * 22)

    def test_not_a_serial(self):
        """ 0x80 is also claimed by the event messages record. """
        for text in (b'222:CAPACITY="3.63TiB"', b'D', b'', b'TRB154001A00123',
                     b'dr154001A00123'):
            d, _ = drobo(serial_page(text))
            self.assertIsNone(d.GetSerial(), text)

    def test_refused(self):
        """ firmware 4.2.3 refuses it (ReDrobo.) """
        d, _ = drobo(refuse=True)
        self.assertIsNone(d.GetSerial())

    def test_wrong_page(self):
        d, _ = drobo(legacy_subpage(0x02))
        self.assertIsNone(d.GetSerial())

    def test_header_only(self):
        d, _ = drobo(b'\x00\x06' + b'\0' * 6)
        self.assertIsNone(d.GetSerial())

    def test_only_mode_sense_sent(self):
        d, dev = drobo(refuse=True)
        d.GetSerial()
        self.assertTrue(dev.cdbs)
        for cdb in dev.cdbs:
            self.assertEqual(cdb[:4], bytes([0x5a, cdb[1], 0x3a, 0x80]))
            self.assertIn(cdb[1], (0x00, 0x08))

    def test_cdb(self):
        d, dev = drobo()
        d.GetSerial()
        self.assertEqual(len(dev.cdbs), 1)
        self.assertEqual(dev.cdbs[0][:4], bytes([0x5a, 0x00, 0x3a, 0x80]))
        self.assertGreaterEqual(struct.unpack('>H', dev.cdbs[0][7:9])[0], 28)

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            self.assertTrue(Drobo.Drobo.GetSerial(None).startswith('DR'))


def vpd(serial, length=None):
    if length is None:
        length = len(serial)
    return bytes([0, 0x80]) + struct.pack('>H', length) + serial


class TestVPDSerial(unittest.TestCase):
    """ vpd_serial reads /sys/block/<dev>/device/vpd_pg80, nothing else. """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, 'block', 'sdb', 'device')
        os.makedirs(self.dir)

    def write(self, data):
        with open(os.path.join(self.dir, 'vpd_pg80'), 'wb') as f:
            f.write(data)

    def test_serial(self):
        self.write(vpd(SERIAL))
        self.assertEqual(Drobo.vpd_serial('/dev/sdb', self.tmp.name),
                         'DRB154001A00123')
        self.assertEqual(Drobo.vpd_serial('sdb', self.tmp.name),
                         'DRB154001A00123')

    def test_padding_stripped(self):
        self.write(vpd(b'  DRB154001A00123 \0\0'))
        self.assertEqual(Drobo.vpd_serial('sdb', self.tmp.name),
                         'DRB154001A00123')

    def test_length_field_limits(self):
        self.write(vpd(SERIAL + b'XXXX', length=len(SERIAL)))
        self.assertEqual(Drobo.vpd_serial('sdb', self.tmp.name),
                         'DRB154001A00123')

    def test_any_serial_accepted(self):
        """ older units have serials starting with 'T' or '0'. """
        self.write(vpd(b'TDB0912345678'))
        self.assertEqual(Drobo.vpd_serial('sdb', self.tmp.name),
                         'TDB0912345678')

    def test_missing(self):
        self.assertIsNone(Drobo.vpd_serial('sdb', self.tmp.name))
        self.assertIsNone(Drobo.vpd_serial('sdz', self.tmp.name))

    def test_not_a_serial_page(self):
        for data in (b'', b'\0\x80', bytes([0, 0x83, 0, 3]) + b'abc',
                     vpd(b''), vpd(b'   ')):
            self.write(data)
            self.assertIsNone(Drobo.vpd_serial('sdb', self.tmp.name), data)

    def test_unreadable(self):
        os.makedirs(os.path.join(self.dir, 'vpd_pg80'))
        self.assertIsNone(Drobo.vpd_serial('sdb', self.tmp.name))

    def test_no_path_tricks(self):
        self.assertIsNone(Drobo.vpd_serial('../sdb', self.tmp.name))
        self.assertIsNone(Drobo.vpd_serial('', self.tmp.name))


class TestInfoSerial(unittest.TestCase):

    def test_optional_not_default(self):
        self.assertEqual(drobom_names('optional_print'), ('serial',))
        self.assertNotIn('serial', drobom_names('valid_print'))

    def test_info_serial(self):
        r = subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                            '-v', '129', 'info', 'serial'], cwd=TOP,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('no such option', r.stdout)
        self.assertIn('Serial: DRSIMULATED0000', r.stdout)
        self.assertIn('VPD serial (/dev/sdx): not available', r.stdout)

    def test_default_info_has_no_serial(self):
        """ every default option (but scsi, which needs a real device even
            in simulation mode) prints no serial. """
        opts = ','.join(o for o in DEFAULT_INFO if o != 'scsi')
        r = subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                            '-v', '129', 'info', opts], cwd=TOP,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('Serial:', r.stdout)


if __name__ == '__main__':
    unittest.main()
