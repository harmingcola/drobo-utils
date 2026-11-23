"""
Tests for GetSubPageLUNs when LUNINFO2 (sub-page 0x07) is refused.  The
vendor's Mac driver has no 0x07 in its dispatch table (ReDrobo docs/
PROTOCOL.md), so newer units may refuse it even when the firmware feature
SUPPORTS_NEW_LUNINFO2 is set: the LUNINFO (0x04) result is returned then.

No Drobo hardware is needed: see test_regression for the fake device.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import FakeDevice, make_drobo, setUpModule, \
    tearDownModule
from test_fixtures_extra import (responder, queried, LUNINFO, LUNINFO2,
                                 BASE_FEATURES, SUPPORTS_NEW_LUNINFO2, TB)

import Drobo

FEATURES = BASE_FEATURES | SUPPORTS_NEW_LUNINFO2

OLD_LUNS = [(0, 2 * TB, 5092651008), (1, 1 * TB, 1234567)]
NEW_LUNS = [(0, 2 * TB - 4096, 5092651008, 'GPT', ['EXT3']),
            (1, 1 * TB - 4096, 1234567, 'MBR', ['NTFS'])]


def refusing(features=FEATURES, refuse=(0x07,), how='ioerror', **pages):
    """ a responder that refuses the given sub-pages: by an I/O error (as a
        CHECK CONDITION comes back), or by a short reply. """
    inner = responder(features, p04=LUNINFO, p07=LUNINFO2, **pages)

    def respond(sub_page):
        if sub_page in refuse:
            if how == 'ioerror':
                raise IOError("io_hdr status is: 2")
            return b'\x7a' + bytes([sub_page]) + b'\0\0'
        return inner(sub_page)

    return respond


def drobo(**kw):
    dev = FakeDevice(responder=refusing(**kw))
    d = make_drobo(dev)
    dev.cdbs.clear()
    return d, dev


def quiet(f, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return f(*a)


class TestLUNsRegression(unittest.TestCase):
    """ what GetSubPageLUNs did before the fallback, where it still applies. """

    def test_luninfo2_used_when_answered(self):
        d, dev = drobo(refuse=())
        self.assertEqual(d.GetSubPageLUNs(), NEW_LUNS)
        self.assertEqual(queried(dev), [0x04, 0x07])

    def test_luninfo_only_without_feature(self):
        d, dev = drobo(features=BASE_FEATURES, refuse=(0x07,))
        self.assertEqual(d.GetSubPageLUNs(), OLD_LUNS)
        self.assertEqual(queried(dev), [0x04])

    def test_luninfo_refused_still_fails(self):
        for features in (BASE_FEATURES, FEATURES):
            for how in ('ioerror', 'short'):
                d, _ = drobo(features=features, refuse=(0x04,), how=how)
                with self.assertRaises((IOError, Drobo.DroboException)):
                    quiet(d.GetSubPageLUNs)

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            self.assertEqual(Drobo.Drobo.GetSubPageLUNs(None),
                             [(0, 2199023251456, 5092651008, 'GPT', 1,
                               ['EXT3'])])


if __name__ == '__main__':
    unittest.main()
