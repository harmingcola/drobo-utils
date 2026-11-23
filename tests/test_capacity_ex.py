"""
Tests for the capacity sub-page (0x02): GetSubPageCapacity, which must not
change, and GetCapacityEx, which reads the seven field record of newer
firmware (layout from ReDrobo docs/PROTOCOL.md, checked on a Drobo 5D.)

No Drobo hardware is needed: see test_regression for the fake device.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v
"""

import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (FakeDevice, make_drobo, legacy_subpage,
                             setUpModule, tearDownModule)
from test_luninfo_ex2 import load_drobom_functions

import Drobo

GB = 1000 * 1000 * 1000

# protected free/used/total, unprotected total: what a Gen 1 returns.
FOUR = (100 * GB, 200 * GB, 300 * GB, 40 * GB)
# ... and the "PT" free/used/total that newer firmware adds (page length 0x38).
SEVEN = FOUR + (11 * GB, 22 * GB, 33 * GB)


def capacity_page(fields):
    body = struct.pack('>%dQ' % len(fields), *fields)
    return struct.pack('>BBH', 0x7a, 0x02, len(body)) + body


def capacity_responder(fields, gen2=False):
    """ a Drobo whose capacity sub-page holds the given u64 fields; legacy
        (raw page) or Gen 2 style (mode parameter header first.) """

    def page(sub_page):
        if sub_page == 0x02:
            return capacity_page(fields)
        return legacy_subpage(sub_page)

    if not gen2:
        return page

    def respond(sub_page, mcb):
        if not mcb[1] & 0x08:
            return b'\x00\x06' + b'\0' * 6
        p = page(sub_page)
        return struct.pack('>HBBBBH', 6 + len(p), 0, 0, 0, 0, 0) + p

    respond.wants_cdb = True
    return respond


def drobo_with(fields, gen2=False):
    dev = FakeDevice(responder=capacity_responder(fields, gen2))
    d = make_drobo(dev)
    dev.cdbs.clear()
    return d, dev


def run_printcapacity(d, chatty=True):
    ns = load_drobom_functions('togig', 'printcapacity')
    out = []
    ns['print'] = lambda *a: out.append(' '.join(str(x) for x in a))
    with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Chatty if chatty else 0):
        ns['printcapacity'](d)
    return out


class TestCapacityRegression(unittest.TestCase):
    """ GetSubPageCapacity and 'drobom info capacity', as they were. """

    def test_four_tuple_legacy(self):
        d, _ = drobo_with(FOUR)
        self.assertEqual(d.GetSubPageCapacity(), FOUR)

    def test_four_tuple_from_seven_field_page(self):
        """ a long page still gives the first four fields. """
        d, _ = drobo_with(SEVEN)
        self.assertEqual(d.GetSubPageCapacity(), FOUR)

    def test_four_tuple_gen2(self):
        d, _ = drobo_with(SEVEN, gen2=True)
        self.assertEqual(d.GetSubPageCapacity(), FOUR)

    def test_cdb_unchanged(self):
        d, dev = drobo_with(SEVEN)
        d.GetSubPageCapacity()
        paklen = struct.calcsize('>BBHQQQQ')
        self.assertEqual(dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x02, 0, 0, 0, paklen, 0)
        ])

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            c = Drobo.Drobo.GetSubPageCapacity(None)
        self.assertEqual(len(c), 4)
        self.assertEqual(c[0] + c[1], c[2])

    def test_printcapacity_chatty_first_line(self):
        d, _ = drobo_with(FOUR)
        out = run_printcapacity(d)
        self.assertEqual(out[0],
                         'Capacity (in GB):  used: 200, free: 100, total: 300')

    def test_printcapacity_plain_first_line(self):
        d, _ = drobo_with(FOUR)
        out = run_printcapacity(d, chatty=False)
        self.assertEqual(out[0], str(FOUR))


KEYS = ('free', 'used', 'total', 'unprotected_total', 'pt_free', 'pt_used',
        'pt_total')


class TestCapacityEx(unittest.TestCase):
    """ GetCapacityEx: every field the unit returns, by name. """

    def test_seven_fields(self):
        d, _ = drobo_with(SEVEN)
        self.assertEqual(d.GetCapacityEx(), dict(zip(KEYS, SEVEN)))

    def test_seven_fields_gen2(self):
        d, _ = drobo_with(SEVEN, gen2=True)
        self.assertEqual(d.GetCapacityEx(), dict(zip(KEYS, SEVEN)))

    def test_four_field_page(self):
        """ older firmware: the PT fields are None. """
        d, _ = drobo_with(FOUR)
        c = d.GetCapacityEx()
        self.assertEqual(c, dict(zip(KEYS, FOUR + (None,) * 3)))

    def test_three_field_page(self):
        d, _ = drobo_with(FOUR[:3])
        c = d.GetCapacityEx()
        self.assertEqual((c['free'], c['used'], c['total']), FOUR[:3])
        self.assertIsNone(c['unprotected_total'])
        self.assertIsNone(c['pt_total'])

    def test_partial_trailing_field_ignored(self):
        page = capacity_page(FOUR)
        page = page[:2] + struct.pack('>H', 36) + page[4:] + b'\x01\x02\x03\x04'

        def respond(sub_page):
            return page if sub_page == 0x02 else legacy_subpage(sub_page)

        dev = FakeDevice(responder=respond)
        c = make_drobo(dev).GetCapacityEx()
        self.assertIsNone(c['pt_free'])
        self.assertEqual(c['unprotected_total'], FOUR[3])

    def test_stale_bytes_past_declared_length_ignored(self):
        """ bytes past the page length are stale buffer contents. """
        page = capacity_page(FOUR) + struct.pack('>3Q', 1, 2, 3)

        def respond(sub_page):
            return page if sub_page == 0x02 else legacy_subpage(sub_page)

        c = make_drobo(FakeDevice(responder=respond)).GetCapacityEx()
        self.assertIsNone(c['pt_free'])

    def test_one_query_asks_for_whole_record(self):
        d, dev = drobo_with(SEVEN)
        d.GetCapacityEx()
        self.assertEqual(len(dev.cdbs), 1)
        cdb = dev.cdbs[0]
        self.assertEqual(cdb[:4], bytes([0x5a, 0x00, 0x3a, 0x02]))
        self.assertGreaterEqual(struct.unpack('>H', cdb[7:9])[0], 4 + 0x38)

    def test_too_short_raises(self):
        d, _ = drobo_with(FOUR[:2])
        with self.assertRaises(Drobo.DroboException):
            d.GetCapacityEx()

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            c = Drobo.Drobo.GetCapacityEx(None)
        self.assertEqual(set(c), set(KEYS))
        self.assertEqual(c['free'] + c['used'], c['total'])


class TestInfoCapacityEx(unittest.TestCase):
    """ drobom info capacity: new fields as extra lines only. """

    def test_chatty_seven_fields(self):
        d, _ = drobo_with(SEVEN)
        out = run_printcapacity(d)
        self.assertEqual(out, [
            'Capacity (in GB):  used: 200, free: 100, total: 300',
            'Unprotected capacity (in GB):  total: 40',
            'PT capacity (in GB):  used: 22, free: 11, total: 33',
        ])

    def test_chatty_four_fields(self):
        d, _ = drobo_with(FOUR)
        out = run_printcapacity(d)
        self.assertEqual(out, [
            'Capacity (in GB):  used: 200, free: 100, total: 300',
            'Unprotected capacity (in GB):  total: 40',
        ])

    def test_chatty_three_fields(self):
        """ nothing extra to show: output exactly as before. """
        d, _ = drobo_with(FOUR[:3])
        d.GetSubPageCapacity = lambda: FOUR
        out = run_printcapacity(d)
        self.assertEqual(out, [
            'Capacity (in GB):  used: 200, free: 100, total: 300'])

    def test_plain_seven_fields(self):
        d, _ = drobo_with(SEVEN)
        out = run_printcapacity(d, chatty=False)
        self.assertEqual(out[0], str(FOUR))
        self.assertEqual(out[1:], [str({'unprotected_total': 40 * GB,
                                        'pt_free': 11 * GB,
                                        'pt_used': 22 * GB,
                                        'pt_total': 33 * GB})])

    def test_extended_read_failure_keeps_old_output(self):
        d, _ = drobo_with(FOUR)

        def fail():
            raise Drobo.DroboException('refused')

        d.GetCapacityEx = fail
        self.assertEqual(run_printcapacity(d), [
            'Capacity (in GB):  used: 200, free: 100, total: 300'])


if __name__ == '__main__':
    unittest.main()
