#!/usr/bin/env python3
"""
tests for the per-drive rotational speed code (byte 11 of each sub-page 0x35
record), and regression tests pinning what GetSlotInfo2 and
'drobom info drives' showed before the code was decoded.

run from the top of the tree:  python3 -m unittest discover -s tests -v
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from test_regression import (TOP, make_drobo, slot_record2, slotinfo2_page,
                             SlotInfo2Device, setUpModule, tearDownModule)

import Drobo

# every key GetSlotInfo2 returned before the rotational speed was decoded,
# for the record built by slot_record2(0) (disk state 2, disk type 1, code 72)
BASE_RECORD = {
    'slot': 0, 'status': 1, 'errors': 0, 'disk_state': 2, 'disk_type': 1,
    'temperature': 38, 'life_remaining': 97, 'rotational_speed': 72,
    'manufacturer': 'WDC', 'firmware': '82.00A82', 'serial': 'WD-WCC4N1234567',
    'total_capacity': 3000592982016, 'managed_capacity': 2999000000000,
    'health': 'Warning', 'media': 'HDD', 'interface': 'SAS',
}

SIM_HEADER_COLUMNS = ['Slot', 'Size(GB)', 'Health', 'Type', 'Temp(C)',
                      'Life(%)', 'Errors', 'Model', 'Firmware', 'Serial']


def drobom(*args):
    return subprocess.run([sys.executable, os.path.join(TOP, 'drobom'),
                           '-v', '128'] + list(args),
                          cwd=TOP, capture_output=True, text=True, timeout=60)


def with_rpm_code(code, slot=0):
    """ a slot_record2 record with its rotational speed byte replaced. """
    rec = bytearray(slot_record2(slot))
    rec[11] = code
    return bytes(rec)


class TestSlotInfo2Regression(unittest.TestCase):
    """ pins GetSlotInfo2 output as it was before the rpm was decoded. """

    def test_parsed_record_keys_and_values(self):
        s = make_drobo(SlotInfo2Device(
            slotinfo2_page([slot_record2(0)]))).GetSlotInfo2()
        self.assertEqual(len(s), 1)
        for key, want in BASE_RECORD.items():
            self.assertEqual(s[0][key], want, key)

    def test_raw_code_kept(self):
        """ the raw byte stays available under its old name. """
        s = make_drobo(SlotInfo2Device(
            slotinfo2_page([with_rpm_code(36)]))).GetSlotInfo2()
        self.assertEqual(s[0]['rotational_speed'], 36)

    def test_simulation_records(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            s = Drobo.Drobo.GetSlotInfo2(None)
        self.assertEqual(len(s), 4)
        for i, r in enumerate(s):
            self.assertEqual((r['slot'], r['rotational_speed'],
                              r['temperature'], r['life_remaining'],
                              r['media'], r['interface'], r['health']),
                             (i, 72, 35 + i, 100 - 7 * i, 'HDD', 'SATA',
                              'Good'))


class TestInfoDrivesRegression(unittest.TestCase):
    """ pins the columns of 'drobom -v 128 info drives'. """

    @classmethod
    def setUpClass(cls):
        r = drobom('info', 'drives')
        assert r.returncode == 0, r.stderr + r.stdout
        cls.lines = [l for l in r.stdout.splitlines() if l.strip()]

    def header(self):
        return [l for l in self.lines if l.startswith('Slot ')][0]

    def rows(self):
        return [l for l in self.lines if not l.startswith('Slot ')]

    def test_header_columns_in_order(self):
        cols = [c for c in SIM_HEADER_COLUMNS if c in self.header().split()]
        self.assertEqual(cols, SIM_HEADER_COLUMNS)

    def test_row_values(self):
        rows = self.rows()
        self.assertEqual(len(rows), 8)  # two simulated units, four slots each
        f = rows[1].split()
        self.assertEqual(f[:6], ['1', '3000', 'Good', 'HDD/SATA', '36', '93'])
        self.assertEqual(f[-3:], ['WD30EFRX-68EUZN0', '82.00A82',
                                  'WD-SIMULATED1'])
        self.assertIn('WDC', f)

    def test_columns_line_up_with_header(self):
        """ each value starts or ends under its heading. """
        head = self.header()
        row = self.rows()[0]
        self.assertEqual(row[head.index('Serial'):].strip(), 'WD-SIMULATED0')
        self.assertEqual(row[head.index('Firmware'):].split()[0], '82.00A82')
        self.assertEqual(row[head.index('Model'):].split()[0], 'WDC')


if __name__ == '__main__':
    unittest.main()
