#!/usr/bin/env python3
"""
Discovery tests: which SCSI devices drobo-utils treats as Drobos.

No hardware is touched: /dev is replaced by a fixed list of names and
DroboIOctl.DroboIOctl by FakeDevice instances from test_regression.

The INQUIRY identities below are what Drobo units report, as listed in the
IOKit matching personalities of the vendor's Mac SCSI driver (see
"How Drobos identify themselves" in DEVELOPERS.rst).

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from test_regression import FakeDevice, make_drobo

import Drobo
import DroboIOctl


def inq(text, width):
    """ an INQUIRY text field: ASCII, left aligned, space padded. """
    return text.encode('ascii').ljust(width)[:width]


# (vendor, product) as a unit reports them in its standard INQUIRY data.
DROBO_IDENTITIES = [
    ('TRUSTED', 'Mass Storage'),
    ('TRUSTED', 'USB Mass Storage'),
    ('Drobo', ''),
    ('Drobo', 'Drobo'),
    ('DROBO', 'Drobo'),
    ('DROBO', 'DroboPro'),
    ('Drobo', 'DroboElite'),
    ('Drobo', 'Drobo3'),
    ('DROBO', 'Drobo3'),
    ('Drobo', 'Gen3'),
    ('Drobo', '3rd Gen DRDR3-A'),
    ('USB 3.0', 'Drobo S - Gen 2'),
    ('Drobo', '5D'),
    ('DROBO', '5D'),
    ('Drobo', '5D - TB'),
    ('Drobo', '5D3'),
    ('Drobo', '5D3 - TB'),
    ('Drobo', '5C'),
    ('Drobo', '8D'),
    ('Drobo', '8D - TB'),
    ('Drobo', 'Mini'),
    ('DROBO', 'Mini'),
    ('Drobo', 'Mini - TB'),
    ('Drobo', 'B800i'),
    ('Drobo', 'B810i'),
    ('Drobo', 'B1200i'),
]

# devices that are not Drobos, and must never be picked up.
NOT_DROBOS = [
    ('ATA', 'WDC WD40EFRX-68N'),
    ('ATA', 'Samsung SSD 870'),
    ('USB 3.0', 'Portable SSD'),
    ('USB 3.0', 'Hard Drive'),
    ('Generic', 'STORAGE DEVICE'),
    ('JMicron', 'Tech'),
    ('WD', 'My Passport 25E2'),
    ('Seagate', 'Expansion Desk'),
]


# the model name drobo-utils should report for each identity.
EXPECTED_MODELS = {
    ('TRUSTED', 'Mass Storage'): 'Drobo (gen 1/2)',
    ('TRUSTED', 'USB Mass Storage'): 'Drobo (gen 1/2)',
    ('Drobo', ''): 'Drobo',
    ('Drobo', 'Drobo'): 'Drobo',
    ('DROBO', 'Drobo'): 'Drobo',
    ('DROBO', 'DroboPro'): 'DroboPro',
    ('Drobo', 'DroboElite'): 'DroboElite',
    ('Drobo', 'Drobo3'): 'Drobo (gen 3)',
    ('DROBO', 'Drobo3'): 'Drobo (gen 3)',
    ('Drobo', 'Gen3'): 'Drobo (gen 3)',
    ('Drobo', '3rd Gen DRDR3-A'): 'Drobo (gen 3)',
    ('USB 3.0', 'Drobo S - Gen 2'): 'Drobo S (gen 2)',
    ('Drobo', '5D'): 'Drobo 5D',
    ('DROBO', '5D'): 'Drobo 5D',
    ('Drobo', '5D - TB'): 'Drobo 5D (Thunderbolt)',
    ('Drobo', '5D3'): 'Drobo 5D3',
    ('Drobo', '5D3 - TB'): 'Drobo 5D3 (Thunderbolt)',
    ('Drobo', '5C'): 'Drobo 5C',
    ('Drobo', '8D'): 'Drobo 8D',
    ('Drobo', '8D - TB'): 'Drobo 8D (Thunderbolt)',
    ('Drobo', 'Mini'): 'Drobo Mini',
    ('DROBO', 'Mini'): 'Drobo Mini',
    ('Drobo', 'Mini - TB'): 'Drobo Mini (Thunderbolt)',
    ('Drobo', 'B800i'): 'Drobo B800i',
    ('Drobo', 'B810i'): 'Drobo B810i',
    ('Drobo', 'B1200i'): 'Drobo B1200i',
}


def fake(vendor, product, idlun=(1, 0, 0, 0), **kw):
    return FakeDevice(vendor=inq(vendor, 8), product=inq(product, 16),
                      idlun=idlun, **kw)


def discover(devices, vendor="Drobo", debugflags=0):
    """ run drobolunlist over a fake /dev holding the given devices. """
    names = sorted(devices)

    def factory(path, *a, **k):
        dev = devices[os.path.basename(path)]
        if isinstance(dev, Exception):
            raise dev
        return dev

    with mock.patch.object(DroboIOctl.os, 'listdir', lambda d: names), \
         mock.patch.object(DroboIOctl, 'DroboIOctl', factory):
        return DroboIOctl.drobolunlist(debugflags, vendor)


class TestDiscoveryRegression(unittest.TestCase):
    """ discovery behaviour as it was before table-driven matching. """

    def test_every_known_identity_is_found(self):
        for vendor, product in DROBO_IDENTITIES:
            with self.subTest(vendor=vendor, product=product):
                self.assertEqual(discover({'sdb': fake(vendor, product)}),
                                 [['/dev/sdb']])

    def test_non_drobos_rejected(self):
        for vendor, product in NOT_DROBOS:
            with self.subTest(vendor=vendor, product=product):
                self.assertEqual(discover({'sdb': fake(vendor, product)}),
                                 [])

    def test_vendor_match_ignores_case(self):
        for vendor in ('trusted', 'TrUsTeD', 'drobo', 'DROBO'):
            with self.subTest(vendor=vendor):
                self.assertEqual(discover({'sdb': fake(vendor, 'x')}),
                                 [['/dev/sdb']])

    def test_product_match_ignores_case(self):
        self.assertEqual(discover({'sdb': fake('USB 3.0', 'DROBO S - GEN 2')}),
                         [['/dev/sdb']])

    def test_nul_padded_fields(self):
        dev = FakeDevice(vendor=b'Drobo\0\0\0', product=b'5D'.ljust(16, b'\0'))
        self.assertEqual(discover({'sdb': dev}), [['/dev/sdb']])

    def test_mixed_bus_two_units_with_luns(self):
        found = discover({
            'sda': fake('ATA', 'WDC WD40EFRX-68N', idlun=(0, 0, 0, 0)),
            'sdb': fake('TRUSTED', 'Mass Storage', idlun=(4, 0, 0, 0)),
            'sdc': fake('TRUSTED', 'Mass Storage', idlun=(4, 0, 0, 1)),
            'sdd': fake('USB 3.0', 'Portable SSD', idlun=(5, 0, 0, 0)),
            'sde': fake('Drobo', '5D', idlun=(6, 0, 0, 0)),
            'sdf': fake('Drobo', '5D', idlun=(6, 0, 0, 1)),
            'sdg': fake('Drobo', '5D', idlun=(6, 0, 0, 2)),
        })
        self.assertEqual(found, [['/dev/sdb', '/dev/sdc'],
                                 ['/dev/sde', '/dev/sdf', '/dev/sdg']])

    def test_unopenable_device_skipped(self):
        found = discover({
            'sda': OSError('permission denied'),
            'sdb': fake('Drobo', 'Mini'),
        })
        self.assertEqual(found, [['/dev/sdb']])

    def test_identify_failure_skipped(self):
        broken = fake('Drobo', 'Mini', idlun=(0, 0, 0, 0))
        broken.identifyLUN = mock.Mock(side_effect=IOError('no answer'))
        found = discover({'sda': broken, 'sdb': fake('Drobo', '8D')})
        self.assertEqual(found, [['/dev/sdb']])

    def test_vendor_option_ignores_case(self):
        devs = {'sdb': fake('NEWCO', 'Box')}
        self.assertEqual(discover(devs), [])
        self.assertEqual(discover(devs, 'newco'), [['/dev/sdb']])

    def test_empty_vendor_option_passes_every_disk(self):
        """ drobom's default -s is '': every disk goes on to the Drobo()
            sanity checks, which are the final filter. """
        devs = {'sda': fake('ATA', 'WDC WD40EFRX-68N', idlun=(0, 0, 0, 0)),
                'sdb': fake('USB 3.0', 'Portable SSD', idlun=(1, 0, 0, 0))}
        self.assertEqual(discover(devs, ''), [['/dev/sda'], ['/dev/sdb']])

    def test_rejection_reported_in_debug_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            discover({'sdb': fake('ATA', 'Disk')}, debugflags=Drobo.DBG_Detection)
        self.assertIn('rejected', out.getvalue())


class TestDiscoverLUNsFinalFilter(unittest.TestCase):
    """ Drobo.DiscoverLUNs keeps only devices that pass the Drobo() checks. """

    def test_generic_usb3_disk_rejected_with_empty_vendor_option(self):
        def not_a_drobo(sub_page):
            return b''  # a plain disk does not answer page 0x3A

        devs = {
            'sda': fake('USB 3.0', 'Portable SSD', idlun=(0, 0, 0, 0),
                        responder=not_a_drobo),
            'sdb': fake('USB 3.0', 'Drobo S - Gen 2', idlun=(1, 0, 0, 0)),
        }
        names = sorted(devs)

        def factory(path, *a, **k):
            return devs[os.path.basename(path)]

        with mock.patch.object(DroboIOctl.os, 'listdir', lambda d: names), \
             mock.patch.object(DroboIOctl, 'DroboIOctl', factory), \
             redirect_stdout(io.StringIO()):
            found = Drobo.DiscoverLUNs(0, '')
        self.assertEqual(found, [['/dev/sdb']])


class TestIdentityTable(unittest.TestCase):
    """ the table-driven matcher: DroboIOctl.DROBO_IDENTITIES and
        DroboIOctl.identify_drobo(). """

    def test_table_shape(self):
        table = DroboIOctl.DROBO_IDENTITIES
        self.assertIsInstance(table, list)
        for rule in table:
            with self.subTest(rule=rule):
                vendor, product, model = rule
                self.assertEqual(vendor, vendor.lower())
                self.assertEqual(product, product.lower())
                self.assertTrue(model)

    def test_every_identity_is_covered(self):
        self.assertEqual(sorted(EXPECTED_MODELS), sorted(DROBO_IDENTITIES))

    def test_model_names(self):
        for (vendor, product), model in EXPECTED_MODELS.items():
            with self.subTest(vendor=vendor, product=product):
                self.assertEqual(
                    DroboIOctl.identify_drobo(inq(vendor, 8),
                                              inq(product, 16)), model)

    def test_accepts_decoded_strings(self):
        self.assertEqual(DroboIOctl.identify_drobo('Drobo   ',
                                                   '5D3             '),
                         'Drobo 5D3')

    def test_non_drobos(self):
        for vendor, product in NOT_DROBOS:
            with self.subTest(vendor=vendor, product=product):
                self.assertIsNone(
                    DroboIOctl.identify_drobo(inq(vendor, 8),
                                              inq(product, 16)))

    def test_drobo_product_from_any_vendor(self):
        """ legacy rule: a product id starting 'Drobo' is enough. """
        self.assertEqual(
            DroboIOctl.identify_drobo(inq('Acme', 8), inq('Drobo X', 16)),
            'Drobo')

    def test_vendor_option(self):
        vendor, product = inq('NEWCO', 8), inq('Box', 16)
        self.assertIsNone(DroboIOctl.identify_drobo(vendor, product))
        self.assertIsNone(DroboIOctl.identify_drobo(vendor, product, 'Drobo'))
        model = DroboIOctl.identify_drobo(vendor, product, 'newco')
        self.assertIsNotNone(model)
        self.assertIn('-s', model)

    def test_discovery_is_table_driven(self):
        devs = {'sdb': fake('Acme', 'StoreBox')}
        self.assertEqual(discover(devs), [])
        table = [('acme', 'storebox', 'Acme StoreBox')]
        table += DroboIOctl.DROBO_IDENTITIES
        with mock.patch.object(DroboIOctl, 'DROBO_IDENTITIES', table):
            self.assertEqual(discover(devs), [['/dev/sdb']])

    def test_discovery_debug_names_the_model(self):
        out = io.StringIO()
        with redirect_stdout(out):
            discover({'sdb': fake('Drobo', '5D3')},
                     debugflags=Drobo.DBG_Detection)
        self.assertIn('Drobo 5D3', out.getvalue())


class TestDroboModel(unittest.TestCase):
    """ Drobo.model names the unit from its INQUIRY data. """

    def test_legacy_unit(self):
        self.assertEqual(make_drobo(FakeDevice()).model, 'Drobo (gen 1/2)')

    def test_5d(self):
        self.assertEqual(make_drobo(fake('Drobo', '5D')).model, 'Drobo 5D')

    def test_unrecognised_identity(self):
        """ found only through -s: still usable, model unknown. """
        self.assertEqual(make_drobo(fake('NEWCO', 'Box')).model, 'unknown')


if __name__ == '__main__':
    unittest.main()
