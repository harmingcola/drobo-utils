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


if __name__ == '__main__':
    unittest.main()
