#!/usr/bin/env python3
"""
tests for the unit status severity (red/yellow/green, the vendor's own rule
as published by the ReDrobo project), and regression tests pinning what
GetStatusEx() and 'drobom info health' gave before it was added.

run from the top of the tree:  python3 -m unittest discover -s tests -v
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from test_regression import (TOP, make_drobo, StatusDevice, setUpModule,
                             tearDownModule)
from test_luninfo_ex2 import load_drobom_functions

import Drobo

HEALTHY_5D = 0x00028000  # bits 15 and 17: what a healthy Drobo 5D reports


def run_printhealth(d):
    ns = load_drobom_functions('printhealth')
    out = []
    ns['print'] = lambda *a: out.append(' '.join(str(x) for x in a))
    ns['printhealth'](d)
    return out


def drobom(*args):
    return subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                           '-v', '128'] + list(args),
                          cwd=TOP, capture_output=True, text=True, timeout=60)


class TestStatusExRegression(unittest.TestCase):
    """ what GetStatusEx() returned before the severity was added. """

    def test_fields_24_bytes(self):
        e = make_drobo(StatusDevice(0x0242, 5, 4, 1, 7)).GetStatusEx()
        self.assertEqual(
            {k: e[k] for k in ('status', 'flags', 'relayout_count',
                               'diskpack_status', 'double_degraded', 'extra')},
            {'status': 0x0242,
             'flags': ['Red alert', 'No redundancy', 'Relay out in progress',
                       'Mismatched disks'],
             'relayout_count': 5, 'diskpack_status': 4,
             'double_degraded': 1, 'extra': 7})

    def test_healthy_5d_value_has_no_flags(self):
        """ bits 15 and 17 are not named. """
        e = make_drobo(StatusDevice(HEALTHY_5D, 0, 1, 0)).GetStatusEx()
        self.assertEqual(e['flags'], [])
        self.assertEqual(e['status'], HEALTHY_5D)

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            e = Drobo.Drobo.GetStatusEx(None)
        self.assertEqual((e['status'], e['flags'], e['relayout_count'],
                          e['diskpack_status'], e['double_degraded'],
                          e['extra']),
                         (0x0240, ['No redundancy', 'Relay out in progress'],
                          1250000, 1, 0, None))

    def test_get_sub_page_status_unchanged(self):
        d = make_drobo(StatusDevice(HEALTHY_5D, 0))
        self.assertEqual(d.GetSubPageStatus(), ([], 0))


class TestPrintHealthRegression(unittest.TestCase):
    """ the lines 'drobom info health' printed before the severity. """

    def test_healthy(self):
        out = run_printhealth(make_drobo(StatusDevice(HEALTHY_5D, 0, 1, 0)))
        for line in ('Redundancy: protected', 'Status: OK',
                     'Disk pack status: 1', 'Double degraded count: 0'):
            self.assertIn(line, out)
        self.assertLess(out.index('Redundancy: protected'),
                        out.index('Status: OK'))

    def test_no_redundancy(self):
        out = run_printhealth(make_drobo(StatusDevice(0x0040, 0)))
        self.assertIn('Redundancy: NOT protected (no redundancy)', out)
        self.assertIn('Status: No redundancy', out)
        self.assertFalse([l for l in out if l.startswith('Disk pack')])

    def test_simulation_output(self):
        r = drobom('info', 'health')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Redundancy: NOT protected (no redundancy)', r.stdout)
        self.assertIn('Status: No redundancy, Relay out in progress',
                      r.stdout)


RED_MASK = 0x14c4187a
YELLOW_MASK = 0x36002300244


class TestSeverityHelper(unittest.TestCase):
    """ Drobo._statusseverity: the vendor's rule, red before yellow. """

    def test_green(self):
        self.assertEqual(Drobo._statusseverity(0), 'green')

    def test_healthy_5d_is_green(self):
        """ bits 15 and 17 are set when healthy and must not count. """
        self.assertEqual(Drobo._statusseverity(HEALTHY_5D), 'green')
        self.assertEqual(Drobo._statusseverity(1 << 15), 'green')
        self.assertEqual(Drobo._statusseverity(1 << 17), 'green')

    def test_every_red_bit(self):
        for bit in range(32):
            if RED_MASK & (1 << bit):
                self.assertEqual(Drobo._statusseverity(1 << bit), 'red', bit)
                self.assertEqual(
                    Drobo._statusseverity(HEALTHY_5D | (1 << bit)), 'red', bit)

    def test_every_yellow_bit(self):
        for bit in range(64):
            m = 1 << bit
            if YELLOW_MASK & m and not RED_MASK & m:
                self.assertEqual(Drobo._statusseverity(m), 'yellow', bit)

    def test_other_bits_are_green(self):
        for bit in range(64):
            m = 1 << bit
            if not (RED_MASK | YELLOW_MASK) & m:
                self.assertEqual(Drobo._statusseverity(m), 'green', bit)

    def test_red_wins_over_yellow(self):
        self.assertEqual(Drobo._statusseverity(0x0004 | 0x0002), 'red')
        # bit 6 (no redundancy) is in both masks
        self.assertEqual(Drobo._statusseverity(0x0040), 'red')

    def test_red_mask_is_32_bits_wide(self):
        """ the red test is 32 bits wide: a high bit alone is never red. """
        self.assertEqual(Drobo._statusseverity(1 << 33), 'green')
        self.assertEqual(Drobo._statusseverity((1 << 32) | 0x0004), 'yellow')

    def test_yellow_high_bits_need_64_bit_status(self):
        self.assertEqual(Drobo._statusseverity(1 << 41), 'yellow')
        self.assertEqual(Drobo._statusseverity(1 << 37), 'yellow')
        self.assertEqual(Drobo._statusseverity((1 << 41) & 0xffffffff),
                         'green')

    def test_named_examples(self):
        for status, want in ((0x0004, 'yellow'),   # yellow threshold
                             (0x0200, 'yellow'),   # relay out in progress
                             (0x0002, 'red'),      # red threshold
                             (0x0010, 'red'),      # bad disk
                             (0x0400, 'green')):   # format in progress
            self.assertEqual(Drobo._statusseverity(status), want, hex(status))


class TestStatusExSeverity(unittest.TestCase):
    """ GetStatusEx()['severity']. """

    def severity(self, *fields):
        return make_drobo(StatusDevice(*fields)).GetStatusEx()['severity']

    def test_healthy_5d(self):
        self.assertEqual(self.severity(HEALTHY_5D, 0, 1, 0), 'green')

    def test_legacy_12_byte_page(self):
        self.assertEqual(self.severity(0, 0), 'green')
        self.assertEqual(self.severity(0x0200, 100), 'yellow')
        self.assertEqual(self.severity(0x0040, 0), 'red')

    def test_extra_word_is_not_the_high_word(self):
        """ the meaning of the 24 byte record's extra u32 is unknown, so it
            must not be treated as the high half of the status. """
        self.assertEqual(self.severity(0, 0, 1, 0, 1 << (41 - 32)), 'green')

    def test_folded_disk_pack_status_counts(self):
        """ severity agrees with the flags, which include the bits the disk
            pack status implies (4: mismatched disks, a red bit). """
        e = make_drobo(StatusDevice(0, 0, 4)).GetStatusEx()
        self.assertIn('Mismatched disks', e['flags'])
        self.assertEqual(e['severity'], 'red')

    def test_gen2(self):
        d = make_drobo(StatusDevice(0x0004, 0, 1, 0, gen2=True))
        self.assertEqual(d.GetStatusEx()['severity'], 'yellow')

    def test_simulation(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            e = Drobo.Drobo.GetStatusEx(None)
        self.assertEqual(e['severity'], Drobo._statusseverity(e['status']))


class TestPrintHealthSeverity(unittest.TestCase):

    def test_green(self):
        out = run_printhealth(make_drobo(StatusDevice(HEALTHY_5D, 0, 1, 0)))
        self.assertIn('Severity: green', out)
        self.assertEqual(out.index('Severity: green'),
                         out.index('Status: OK') + 1)

    def test_red(self):
        out = run_printhealth(make_drobo(StatusDevice(0x0040, 0)))
        self.assertIn('Severity: red', out)

    def test_simulation_output(self):
        r = drobom('info', 'health')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn('Severity: red', r.stdout)


if __name__ == '__main__':
    unittest.main()
