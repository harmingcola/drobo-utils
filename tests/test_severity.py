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


if __name__ == '__main__':
    unittest.main()
