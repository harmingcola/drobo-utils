#!/usr/bin/env python3
"""
tests for the status bit names published by the ReDrobo project (bit 25,
and the bits above 31 of a 64 bit status), and regression tests pinning the
names and order _unitstatus gave before they were added.

run from the top of the tree:  python3 -m unittest discover -s tests -v
"""

import unittest

from test_regression import (make_drobo, StatusDevice, setUpModule,
                             tearDownModule)

import Drobo

# every bit _unitstatus named before, in the order it lists them.
OLD_NAMES = [(0x0002, 'Red alert'), (0x0004, 'Yellow alert'),
             (0x0008, 'No disks'), (0x0010, 'Bad disk'),
             (0x0020, 'Too many missing disks'), (0x0040, 'No redundancy'),
             (0x0080, 'No magic hotspare'), (0x0100, 'no space left'),
             (0x0200, 'Relay out in progress'),
             (0x0400, 'Format in progress'), (0x0800, 'Mismatched disks'),
             (0x1000, 'Unknown version'), (0x2000, 'New firmware installed'),
             (0x4000, 'New LUN available after reboot'),
             (0x40000, 'Incompatible disk pack'),
             (0x100000, 'Power supply missing'),
             (0x200000, 'Fan partially failed'), (0x400000, 'Fan failed'),
             (0x800000, 'Fan missing'), (0x10000000, 'Unknown error')]
OLD_MASK = sum(bit for bit, name in OLD_NAMES)


class TestUnitStatusRegression(unittest.TestCase):
    """ names and order of the bits _unitstatus already knew. """

    def test_each_old_bit(self):
        for bit, name in OLD_NAMES:
            self.assertEqual(Drobo._unitstatus(bit), [name], hex(bit))

    def test_all_old_bits_in_order(self):
        self.assertEqual(Drobo._unitstatus(OLD_MASK),
                         [name for bit, name in OLD_NAMES])

    def test_healthy_bits_15_and_17_stay_unnamed(self):
        """ set on a healthy unit: not alerts, so never flags. """
        self.assertEqual(Drobo._unitstatus(0x00028000), [])
        self.assertEqual(Drobo._unitstatus(OLD_MASK | 0x00028000),
                         [name for bit, name in OLD_NAMES])

    def test_status_ex_ignores_extra_word(self):
        e = make_drobo(StatusDevice(0x0040, 0, 1, 0, 0xffffffff)).GetStatusEx()
        self.assertEqual(e['flags'], ['No redundancy'])
        self.assertEqual(e['severity'], 'red')


if __name__ == '__main__':
    unittest.main()
