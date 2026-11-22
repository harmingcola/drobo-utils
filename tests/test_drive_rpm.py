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


class TestDriveRpmCode(unittest.TestCase):
    """ the code is in units of 200 RPM; 1 means a non-rotating drive (SSD)
        and 0 means the speed is unknown.
    """

    def test_spinning_speeds(self):
        for code, want in ((27, 5400), (36, 7200), (50, 10000),
                           (75, 15000), (2, 400), (72, 14400), (255, 51000)):
            self.assertEqual(Drobo._driverpm(code), want, code)

    def test_non_rotating(self):
        self.assertEqual(Drobo._driverpm(1), 0)

    def test_unknown(self):
        self.assertIsNone(Drobo._driverpm(0))


class TestSlotInfo2Rpm(unittest.TestCase):
    """ GetSlotInfo2 adds the decoded speed as 'rpm'. """

    def slots(self, *codes):
        page = slotinfo2_page([with_rpm_code(c, i)
                               for i, c in enumerate(codes)])
        return make_drobo(SlotInfo2Device(page)).GetSlotInfo2()

    def test_rpm_field(self):
        s = self.slots(36, 1, 0, 27)
        self.assertEqual([r['rpm'] for r in s], [7200, 0, None, 5400])

    def test_raw_code_alongside(self):
        s = self.slots(36)
        self.assertEqual((s[0]['rotational_speed'], s[0]['rpm']), (36, 7200))

    def test_simulation_has_rpm(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            s = Drobo.Drobo.GetSlotInfo2(None)
        for r in s:
            self.assertEqual(r['rpm'], Drobo._driverpm(r['rotational_speed']))


class TestInfoDrivesRpm(unittest.TestCase):
    """ 'drobom info drives' shows an RPM column. """

    def test_rpm_column(self):
        r = drobom('info', 'drives')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        head = [l for l in lines if l.startswith('Slot ')][0]
        self.assertIn('RPM', head.split())
        row = [l for l in lines if not l.startswith('Slot ')][0]
        self.assertEqual(row[head.index('RPM') - 3:].split()[0], '14400')

    def test_rpm_text(self):
        """ the column shows the speed, '-' for non-rotating, '?' unknown. """
        self.assertEqual(Drobo._rpmtext(7200), '7200')
        self.assertEqual(Drobo._rpmtext(0), '-')
        self.assertEqual(Drobo._rpmtext(None), '?')


if __name__ == '__main__':
    unittest.main()


class TestSimulatedRPM(unittest.TestCase):
    """ simulation mode should show a realistic spindle speed. """

    def test_simulated_drives_spin_at_7200(self):
        r = subprocess.run([sys.executable, os.path.join(TOP, 'drobom'), '-v',
                            '128', 'info', 'drives'], cwd=TOP,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(' 7200 ', r.stdout)
        self.assertNotIn('14400', r.stdout)
