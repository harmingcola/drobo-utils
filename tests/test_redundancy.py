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


class TestRedundancyHelper(unittest.TestCase):
    """ Drobo._redundancylevel(capacities, usable): pure. """

    def level(self, caps, usable):
        return Drobo._redundancylevel(caps, usable)

    def test_5d_example_is_dual(self):
        """ held back / two largest = 1.0049; / largest = 2.0098. """
        self.assertEqual(self.level([TB2, TB2, TB4, TB2, TB4], USABLE_5D),
                         'dual')

    def test_single(self):
        # 4 x 2 TB, one disk held back, plus 1% overhead
        caps = [TB2] * 4
        self.assertEqual(self.level(caps, int(3 * TB2 * 0.99)), 'single')

    def test_single_mixed_sizes(self):
        caps = [TB2, TB2, TB4, TB2, TB4]
        usable = sum(caps) - int(TB4 * 1.01)
        self.assertEqual(self.level(caps, usable), 'single')

    def test_window_edges(self):
        caps = [TB4] * 4
        raw = sum(caps)
        self.assertEqual(self.level(caps, raw - int(TB4 * 1.049)), 'single')
        self.assertEqual(self.level(caps, raw - int(TB4 * 0.951)), 'single')
        self.assertEqual(self.level(caps, raw - int(TB4 * 1.06)),
                         'undetermined')
        self.assertEqual(self.level(caps, raw - int(TB4 * 0.94)),
                         'undetermined')
        self.assertEqual(self.level(caps, raw - int(2 * TB4 * 1.049)), 'dual')
        self.assertEqual(self.level(caps, raw - int(2 * TB4 * 1.06)),
                         'undetermined')

    def test_in_between_is_undetermined(self):
        caps = [TB4] * 4
        self.assertEqual(self.level(caps, sum(caps) - int(TB4 * 1.5)),
                         'undetermined')

    def test_ambiguous_is_undetermined(self):
        """ a tiny second disk makes both ratios close to 1. """
        caps = [TB4, 100000000000, TB4 // 100]
        self.assertEqual(self.level(caps, sum(caps) - TB4 - 60000000000),
                         'undetermined')

    def test_no_usable_capacity(self):
        self.assertEqual(self.level([TB2, TB2], 0), 'undetermined')
        self.assertEqual(self.level([TB2, TB2], None), 'undetermined')

    def test_no_disks(self):
        self.assertEqual(self.level([], USABLE_5D), 'undetermined')
        self.assertEqual(self.level([0, 0, 0], USABLE_5D), 'undetermined')

    def test_usable_not_below_raw(self):
        self.assertEqual(self.level([TB2, TB2], 2 * TB2), 'undetermined')
        self.assertEqual(self.level([TB2, TB2], 3 * TB2), 'undetermined')

    def test_empty_slots_ignored(self):
        self.assertEqual(self.level([TB2, TB2, 0, TB4, 0, TB2, TB4],
                                    USABLE_5D), 'dual')

    def test_dual_needs_two_disks(self):
        # one disk only: held back = everything, never 'dual'
        self.assertNotEqual(self.level([TB4], int(TB4 * 0.001)), 'dual')


class TestGetRedundancyLevel(unittest.TestCase):

    def test_5d_dual_accelerator_excluded(self):
        self.assertEqual(five_d().GetRedundancyLevel(), 'dual')

    def test_5d_single(self):
        raw = 2 * TB4 + 3 * TB2
        self.assertEqual(five_d(raw - int(TB4 * 1.005)).GetRedundancyLevel(),
                         'single')

    def test_accelerator_would_spoil_it_if_counted(self):
        """ counting the mSATA slot changes the answer for this array. """
        raw = 2 * TB4 + 3 * TB2
        self.assertEqual(
            Drobo._redundancylevel([TB2, TB2, TB4, TB2, TB4, MSATA],
                                   raw - int(TB4 * 1.04)), 'undetermined')
        self.assertEqual(five_d(raw - int(TB4 * 1.04)).GetRedundancyLevel(),
                         'single')

    def test_legacy_slot_info_fallback(self):
        """ no sub-page 0x35: capacities from sub-page 0x03. 2 x 500 GB in
            the legacy fixture, one held back. """
        d = make_drobo(CapacityDevice(None, int(500107862016 * 0.99)))
        self.assertEqual(d.GetRedundancyLevel(), 'single')

    def test_legacy_unit_only_mode_sense(self):
        """ a first generation unit: only MODE SENSE page 0x3A reads. """
        dev = FakeDevice()
        d = make_drobo(dev)
        dev.cdbs.clear()
        self.assertIn(d.GetRedundancyLevel(),
                      ('single', 'dual', 'undetermined'))
        self.assertTrue(dev.cdbs)
        self.assertEqual({(c[0], c[2]) for c in dev.cdbs}, {(0x5a, 0x3a)})

    def test_capacity_failure_is_undetermined(self):
        d = five_d()
        with mock.patch.object(d, 'GetSubPageCapacity',
                               side_effect=Drobo.DroboException('x')):
            self.assertEqual(d.GetRedundancyLevel(), 'undetermined')

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            self.assertEqual(Drobo.Drobo.GetRedundancyLevel(None),
                             'undetermined')


class TestPrintHealthLevel(unittest.TestCase):

    def test_dual_after_redundancy_line(self):
        out = run_printhealth(five_d())
        self.assertEqual(out[:2], ['Redundancy: protected',
                                   'Redundancy level: dual'])

    def test_simulation(self):
        r = drobom('info', 'health')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Redundancy level: undetermined', r.stdout)


if __name__ == '__main__':
    unittest.main()
