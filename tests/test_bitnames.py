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


HIGH_NAMES = [(32, 'DroboShare alert'), (33, 'Drive added'),
              (34, 'Drive removed'), (35, 'Dual disk redundancy updated'),
              (36, 'Volume usage over limit'),
              (37, 'All volumes over limit'), (38, 'Target login'),
              (39, 'Relayout complete'),
              (41, 'Three SSDs and a disk required')]


class TestBit25(unittest.TestCase):
    """ bit 25, named by ReDrobo, within our 32 bit status. """

    def test_named(self):
        self.assertEqual(Drobo._unitstatus(1 << 25), ['Hybrid tiering state'])

    def test_order_by_bit(self):
        """ listed between the fan bits and 'Unknown error' (bit 28). """
        names = [name for bit, name in OLD_NAMES]
        want = names[:-1] + ['Hybrid tiering state'] + names[-1:]
        self.assertEqual(Drobo._unitstatus(OLD_MASK | (1 << 25)), want)

    def test_in_status_ex_flags(self):
        e = make_drobo(StatusDevice(1 << 25, 0, 1, 0)).GetStatusEx()
        self.assertEqual(e['flags'], ['Hybrid tiering state'])
        self.assertEqual(e['severity'], 'yellow')


class TestUnitStatus64(unittest.TestCase):
    """ _unitstatus64: names for a 64 bit status, if one is ever supplied.
        Nothing reads a 64 bit status yet. """

    def test_each_high_bit(self):
        for bit, name in HIGH_NAMES:
            self.assertEqual(Drobo._unitstatus64(1 << bit), [name], bit)

    def test_low_word_as_unitstatus(self):
        for n in (0, 0x0242, OLD_MASK, 1 << 25, 0x00028000):
            self.assertEqual(Drobo._unitstatus64(n), Drobo._unitstatus(n))

    def test_low_then_high_in_bit_order(self):
        n = 0x0040 | (1 << 41) | (1 << 33)
        self.assertEqual(Drobo._unitstatus64(n),
                         ['No redundancy', 'Drive added',
                          'Three SSDs and a disk required'])

    def test_unnamed_high_bits_ignored(self):
        self.assertEqual(Drobo._unitstatus64((1 << 40) | (1 << 42)
                                             | (1 << 63)), [])

    def test_unitstatus_ignores_high_bits(self):
        """ the 32 bit helper is unchanged for values above 32 bits. """
        self.assertEqual(Drobo._unitstatus(1 << 33), [])


if __name__ == '__main__':
    unittest.main()
