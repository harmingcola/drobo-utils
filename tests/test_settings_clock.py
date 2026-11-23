"""
Tests for the system settings sub-page (0x05): GetSubPageSettings, which
must not change (first generation units included), and GetClock, which
reads currentTime as the unit's local wall clock and gmtOffset as a u16 of
minutes east of UTC (facts from ReDrobo docs/PROTOCOL.md, checked on a
Drobo 5D.)

No Drobo hardware is needed: see test_regression for the fake device.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v
"""

import os
import struct
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (FakeDevice, make_drobo, legacy_subpage, TOP,
                             setUpModule, tearDownModule)

import Drobo

# 2026-08-19 16:19:07 on the unit's clock face (ReDrobo's example)
CLOCK = 1787156347


def settings_page(when, offset, name=b'RAIDDEADREDEMPTION'):
    body = struct.pack('>LH32s', when, offset, name)
    return struct.pack('>BBH', 0x7a, 0x05, len(body)) + body


def drobo_with(when, offset):
    def respond(sub_page):
        if sub_page == 0x05:
            return settings_page(when, offset)
        return legacy_subpage(sub_page)

    dev = FakeDevice(responder=respond)
    d = make_drobo(dev)
    dev.cdbs.clear()
    return d, dev


def drobom(*args):
    env = dict(os.environ, TZ='UTC')
    return subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                           '-v', '129'] + list(args), cwd=TOP, env=env,
                          capture_output=True, text=True, timeout=60)


class TestSettingsRegression(unittest.TestCase):
    """ GetSubPageSettings and its drobom output, as they were. """

    def test_offset_always_reported_as_8(self):
        """ whatever the raw bytes (Gen 1 sent 0x0800), offset reads 8. """
        for raw in (0, 8, 120, 0x0800, 0xfe20, 0xffff):
            d, _ = drobo_with(CLOCK, raw)
            self.assertEqual(d.GetSubPageSettings(),
                             (CLOCK, 8, 'RAIDDEADREDEMPTION'), raw)

    def test_cdb_unchanged(self):
        d, dev = drobo_with(CLOCK, 120)
        d.GetSubPageSettings()
        paklen = struct.calcsize('>BBHLH32s')
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x05, 0, 0, 0, paklen, 0)
        ])

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            s = Drobo.Drobo.GetSubPageSettings(None)
        self.assertEqual(s, (1220112079, 8, 'TRUSTED DATA'))

    def test_info_header(self):
        r = drobom('info', 'config')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[1],
                         'Drobo Name: TRUSTED DATA Devices: /dev/sdx:/dev/sdy')
        self.assertEqual(lines[2], '      Time: Sat Aug 30 16:01:19 2008')

    def test_time_command(self):
        r = drobom('time')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0],
                         'Drobo says it is: Sat Aug 30 16:01:19 2008')


if __name__ == '__main__':
    unittest.main()
