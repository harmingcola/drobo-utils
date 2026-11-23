"""
Tests for 'drobom info drives' (printdrives): its current output is pinned,
then a temperature of 0 (not populated on some firmware, e.g. a Drobo 5D on
4.2.3) shows as '-', and an mSATA accelerator slot (disk type 4) is listed
apart from the drive bays.  Facts from ReDrobo docs/PROTOCOL.md.

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

from test_regression import (make_drobo, slot_record2, slotinfo2_page,
                             SlotInfo2Device, TOP, setUpModule,
                             tearDownModule)
from test_luninfo_ex2 import load_drobom_functions

import Drobo

HEADER = ('Slot  Size(GB)  Health   Type      Temp(C)  Life(%)  Errors    RPM  '
          'Model                    Firmware  Serial')


def record(slot, disk_type=0, temp=38, total=3000592982016, maker=b'WDC',
           rpm=36, state=0, life=97):
    rec = bytearray(slot_record2(slot, temp=temp, total=total, maker=maker,
                                 life=life))
    rec[4:8] = struct.pack('>L', state)
    rec[8] = disk_type
    rec[11] = rpm
    return bytes(rec)


def run_printdrives(records):
    d = make_drobo(SlotInfo2Device(slotinfo2_page(records)))
    ns = load_drobom_functions('togig', 'printdrives')
    out = []
    ns['print'] = lambda *a: out.append(' '.join(str(x) for x in a))
    ns['printdrives'](d)
    return out


# a 5D like layout: five bays (one empty) and the mSATA accelerator.
FIVE_D = [record(0), record(1, total=4000787030016), record(2, total=0),
          record(3), record(4, total=4000787030016),
          record(5, disk_type=4, total=128035676160, maker=b'M4-CT128M4SSD3',
                 rpm=1, state=0x20, life=91)]


class TestInfoDrivesRegression(unittest.TestCase):
    """ output that must not change. """

    def test_output_with_temperatures(self):
        self.assertEqual(run_printdrives([record(0), record(1, temp=41),
                                          record(2, total=0)]), [
            HEADER,
            '   0      3000  Good     HDD/SATA       38       97       0   '
            '7200  WDC                      82.00A82  WD-WCC4N1234567',
            '   1      3000  Good     HDD/SATA       41       97       0   '
            '7200  WDC                      82.00A82  WD-WCC4N1234567',
            '   2  empty',
        ])

    def test_ssd_in_a_bay_stays_a_bay(self):
        """ disk type 2 (SSD/SATA) is an ordinary drive in a bay. """
        out = run_printdrives([record(0), record(1, disk_type=2, rpm=1)])
        self.assertEqual(len(out), 3)
        self.assertTrue(out[2].startswith('   1      3000  Good     SSD/SATA'))

    def test_unsupported(self):
        d = make_drobo(SlotInfo2Device(None))
        ns = load_drobom_functions('togig', 'printdrives')
        out = []
        ns['print'] = lambda *a: out.append(' '.join(str(x) for x in a))
        ns['printdrives'](d)
        self.assertEqual(out, ['per-drive details are not supported by this '
                               'firmware'])

    def test_bay_rows_of_5d_layout(self):
        """ the bays of a 5D come out as before, in slot order. """
        out = run_printdrives(FIVE_D)
        self.assertEqual(out[0], HEADER)
        self.assertEqual([l[:4] for l in out[1:6]],
                         ['   0', '   1', '   2', '   3', '   4'])
        self.assertEqual(out[3], '   2  empty')

    def test_simulation_output(self):
        r = subprocess.run([sys.executable, os.path.join(TOP, 'drobom'), '-v',
                            '128', 'info', 'drives'], cwd=TOP,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines.count(HEADER), 2)
        self.assertEqual(
            lines[1], '   0      3000  Good     HDD/SATA       35      100   '
            '    0   7200  WDC WD30EFRX-68EUZN0     82.00A82  WD-SIMULATED0')
        self.assertEqual(len(lines), 10)

    def test_slotinfo2_keys(self):
        d = make_drobo(SlotInfo2Device(slotinfo2_page(FIVE_D)))
        s = d.GetSlotInfo2()
        self.assertEqual(len(s), 6)
        self.assertEqual((s[5]['disk_type'], s[5]['media'],
                          s[5]['interface'], s[5]['temperature']),
                         (4, 'SSD', 'mSATA', 38))


class TestTemperatureNotPopulated(unittest.TestCase):

    def test_zero_shows_dash(self):
        out = run_printdrives([record(0, temp=0)])
        self.assertEqual(out[1],
                         '   0      3000  Good     HDD/SATA        -       97'
                         '       0   7200  WDC                      82.00A82'
                         '  WD-WCC4N1234567')

    def test_dash_lines_up_with_header(self):
        out = run_printdrives([record(0, temp=0), record(1, temp=41)])
        end = HEADER.index('Temp(C)') + len('Temp(C)')
        self.assertEqual(out[1][end - 1], '-')
        self.assertEqual(out[2][end - 2:end], '41')

    def test_temperature_text(self):
        self.assertEqual(Drobo._temptext(0), '-')
        self.assertEqual(Drobo._temptext(38), '38')


class TestAccelerator(unittest.TestCase):

    def test_slotinfo2_flags_accelerator(self):
        s = make_drobo(SlotInfo2Device(slotinfo2_page(FIVE_D))).GetSlotInfo2()
        self.assertEqual([r['accelerator'] for r in s],
                         [False] * 5 + [True])

    def test_only_msata_is_accelerator(self):
        recs = [record(i, disk_type=t) for i, t in enumerate((0, 1, 2, 3))]
        s = make_drobo(SlotInfo2Device(slotinfo2_page(recs))).GetSlotInfo2()
        self.assertEqual([r['accelerator'] for r in s], [False] * 4)

    def test_simulation_has_no_accelerator(self):
        with mock.patch.object(Drobo, 'DEBUG', Drobo.DBG_Simulation):
            s = Drobo.Drobo.GetSlotInfo2(None)
        self.assertEqual([r['accelerator'] for r in s], [False] * 4)

    def test_listed_apart_from_bays(self):
        out = run_printdrives(FIVE_D)
        self.assertEqual(len(out), 8)
        self.assertEqual(out[6], 'Accelerator (mSATA cache, not a drive bay):')
        self.assertEqual(out[7],
                         '   5       128  Good     SSD/mSATA      38       91'
                         '       0      -  M4-CT128M4SSD3           82.00A82'
                         '  WD-WCC4N1234567')

    def test_accelerator_listed_after_bays_whatever_its_slot(self):
        recs = [FIVE_D[5][:0] + bytes([0]) + FIVE_D[5][1:]] + \
            [record(i) for i in (1, 2)]
        out = run_printdrives(recs)
        self.assertEqual([l[:4] for l in out[1:3]], ['   1', '   2'])
        self.assertEqual(out[-2], 'Accelerator (mSATA cache, not a drive bay):')
        self.assertTrue(out[-1].startswith('   0       128  Good     SSD/mSATA'))

    def test_no_accelerator_no_extra_lines(self):
        out = run_printdrives(FIVE_D[:5])
        self.assertEqual(len(out), 6)
        self.assertNotIn('ccelerator', '\n'.join(out))


if __name__ == '__main__':
    unittest.main()
