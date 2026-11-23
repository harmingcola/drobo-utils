"""
Tests for the options sub-pages, 0x30 (Options) and 0x31 (Options2):
GetOptions' existing keys and values must not change, and the length fields
of both pages must not be trusted (ReDrobo saw impossible values, 2304 and
64513, on a Drobo 5D.)  Layout facts from ReDrobo docs/PROTOCOL.md:

    0x30: +4 yellow %, +5 red %, +6 packed flags (use unprotected capacity,
          realtime data check, background data check type)
    0x31: +4 u64 featureOnOffStates, +0x0C u16 spin down minutes

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

from test_regression import FakeDevice, make_drobo, TOP, setUpModule, \
    tearDownModule
from test_fixtures_extra import (page, responder, options2_page, queried,
                                 BASE_FEATURES, SUPPORTS_OPTIONS2,
                                 OPTIONS_PATTERN)

import Drobo

OPTIONS2_FEATURES = BASE_FEATURES | SUPPORTS_OPTIONS2


def options_page(yellow=85, red=95, flags=0, param=0, lowyel=80, lowred=90,
                 length=None):
    p = page(0x30, OPTIONS_PATTERN, (yellow, red, flags, param, lowyel,
                                     lowred))
    if length is not None:
        p = p[:2] + struct.pack('>H', length) + p[4:]
    return p


def with_length(p, length):
    return p[:2] + struct.pack('>H', length) + p[4:]


def gen2(respond):
    """ the same pages behind a mode parameter header (Drobo S Gen 2.) """

    def wrapped(sub_page, mcb):
        if not mcb[1] & 0x08:
            return b'\x00\x06' + b'\0' * 6
        p = respond(sub_page)
        return struct.pack('>HBBBBH', 6 + len(p), 0, 0, 0, 0, 0) + p

    wrapped.wants_cdb = True
    return wrapped


def drobo(features=OPTIONS2_FEATURES, p30=None, p31=None, gen2_style=False):
    r = responder(features, p30=p30 or options_page(),
                  p31=p31 or options2_page(0x0005, spindown=14))
    dev = FakeDevice(responder=gen2(r) if gen2_style else r)
    d = make_drobo(dev)
    dev.cdbs.clear()
    return d, dev


# what GetOptions returned before this change, for drobo() above.
OLD_V1 = {'YellowThreshold': 85, 'RedThreshold': 95}
OLD_V2 = dict(OLD_V1, Flags=0x0005, SpinDownDelayMinutes=14,
              DualDiskRedundancy=True, SpinDownDelay=False,
              UseManualVolumeManagement=True)
OLD_SIM_KEYS = {'YellowThreshold', 'RedThreshold', 'SpinDownDelayMinutes',
                'SpinDownDelay', 'UseStaticIPAddress', 'IPAddress', 'NetMask',
                'DualDiskRedundancy', 'UseManualVolumeManagement'}


def subset(d, keys):
    return dict((k, d[k]) for k in keys)


class TestOptionsRegression(unittest.TestCase):
    """ existing keys and values, and untrusted page lengths. """

    def test_v1_keys_and_values(self):
        d, _ = drobo(BASE_FEATURES)
        self.assertEqual(subset(d.GetOptions(), OLD_V1), OLD_V1)

    def test_v2_keys_and_values(self):
        d, _ = drobo()
        self.assertEqual(subset(d.GetOptions(), OLD_V2), OLD_V2)

    def test_red_threshold_is_byte_5(self):
        d, _ = drobo(p30=options_page(yellow=70, red=93))
        o = d.GetOptions()
        self.assertEqual((o['YellowThreshold'], o['RedThreshold']), (70, 93))

    def test_spin_down_is_u16_at_0x0c(self):
        d, _ = drobo(p31=options2_page(0, spindown=0x0102))
        self.assertEqual(d.GetOptions()['SpinDownDelayMinutes'], 0x0102)

    def test_impossible_length_fields_ignored(self):
        """ ReDrobo: 0x30 says 2304, 0x31 says 64513. """
        d, _ = drobo(p30=options_page(length=2304),
                     p31=with_length(options2_page(0x0005, spindown=14),
                                     64513))
        self.assertEqual(subset(d.GetOptions(), OLD_V2), OLD_V2)

    def test_impossible_length_fields_ignored_gen2(self):
        d, _ = drobo(p30=options_page(length=2304),
                     p31=with_length(options2_page(0x0005, spindown=14),
                                     64513), gen2_style=True)
        self.assertEqual(subset(d.GetOptions(), OLD_V2), OLD_V2)

    def test_same_sub_pages_read(self):
        d, dev = drobo()
        d.GetOptions()
        self.assertEqual(queried(dev), [0x30, 0x31])

    def test_simulation_keys(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            o = Drobo.Drobo.GetOptions(None)
        self.assertLessEqual(OLD_SIM_KEYS, set(o))
        self.assertEqual((o['YellowThreshold'], o['RedThreshold']), (85, 95))


if __name__ == '__main__':
    unittest.main()
