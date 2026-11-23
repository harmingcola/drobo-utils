#!/usr/bin/env python3
"""
tests for the redundancy level (single/dual/undetermined) derived from
capacities, as described by the ReDrobo project, and regression tests
pinning the queries it is built from and 'drobom info health' before it.

run from the top of the tree:  python3 -m unittest discover -s tests -v
"""

import struct
import unittest
from unittest import mock

from test_regression import (make_drobo, legacy_subpage, slot_record2,
                             slotinfo2_page, FakeDevice, SlotInfo2Device,
                             StatusDevice, setUpModule, tearDownModule)
from test_severity import run_printhealth, drobom

import Drobo

# the Drobo 5D in ReDrobo's write-up: bays of 2/2/4/2/4 TB, an mSATA
# accelerator in a sixth slot, usable (protected total) 5 962 106 142 720.
TB2 = 2000398934016
TB4 = 4000787030016
MSATA = 128035676160
USABLE_5D = 5962106142720


def drive(slot, total, disk_type=0):
    """ a sub-page 0x35 record with a chosen capacity and disk type. """
    rec = bytearray(slot_record2(slot, total=total))
    rec[8] = disk_type
    return bytes(rec)


def capacity_page(free, used, total, unprotected):
    body = struct.pack('>QQQQ', free, used, total, unprotected)
    return struct.pack('>BBH', 0x7a, 0x02, len(body)) + body


class CapacityDevice(SlotInfo2Device):
    """ answers sub-page 0x35 with page (None: unsupported), and 0x02 with
        the given protected total. """

    def __init__(self, page, total, status=(0, 0, 1, 0)):
        self.capacity = capacity_page(total // 2, total - total // 2, total,
                                      0)
        self.status = StatusDevice(*status).status_page()
        super().__init__(page)

    def respond(self, sub_page):
        if sub_page == 0x02:
            return self.capacity
        if sub_page == 0x09:
            return self.status
        return super().respond(sub_page)


def five_d(usable=USABLE_5D):
    recs = [drive(i, c) for i, c in enumerate((TB2, TB2, TB4, TB2, TB4))]
    recs.append(drive(5, MSATA, disk_type=4))
    return make_drobo(CapacityDevice(slotinfo2_page(recs), usable))


class TestSourcesRegression(unittest.TestCase):
    """ what the queries the level is built from return today. """

    def test_capacity_tuple(self):
        d = five_d()
        self.assertEqual(d.GetSubPageCapacity(),
                         (USABLE_5D // 2, USABLE_5D - USABLE_5D // 2,
                          USABLE_5D, 0))

    def test_slotinfo2_5d(self):
        s = five_d().GetSlotInfo2()
        self.assertEqual([r['total_capacity'] for r in s],
                         [TB2, TB2, TB4, TB2, TB4, MSATA])
        self.assertEqual([r['interface'] for r in s],
                         ['SATA'] * 5 + ['mSATA'])
        self.assertEqual([r['media'] for r in s], ['HDD'] * 5 + ['SSD'])

    def test_legacy_slot_info(self):
        d = make_drobo(FakeDevice())
        self.assertIsNone(make_drobo(SlotInfo2Device(None)).GetSlotInfo2())
        self.assertEqual([s[1] for s in d.GetSubPageSlotInfo()],
                         [500107862016, 500107862016, 0, 0])

    def test_simulation_sources(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            c = Drobo.Drobo.GetSubPageCapacity(None)
            s = Drobo.Drobo.GetSlotInfo2(None)
        self.assertEqual(c[2], 495452160000)
        self.assertEqual([r['total_capacity'] for r in s],
                         [3000592982016] * 4)


class TestPrintHealthRegression(unittest.TestCase):
    """ the lines 'drobom info health' printed before the level, in order. """

    def test_healthy_lines_in_order(self):
        out = run_printhealth(make_drobo(StatusDevice(0x00028000, 0, 1, 0)))
        want = ['Redundancy: protected', 'Status: OK', 'Severity: green',
                'Disk pack status: 1', 'Double degraded count: 0']
        self.assertEqual([l for l in out if l in want], want)

    def test_simulation_lines(self):
        r = drobom('info', 'health')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        for line in ('Redundancy: NOT protected (no redundancy)',
                     'Status: No redundancy, Relay out in progress',
                     'Severity: red'):
            self.assertIn(line, r.stdout)


if __name__ == '__main__':
    unittest.main()
