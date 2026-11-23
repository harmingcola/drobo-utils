"""
SetOptions() and the packed flags byte at +6 of the Options sub-page (0x30).

ReDrobo (docs/PROTOCOL.md, from the vendor's ESAOptions::parse) says that
byte packs use unprotected capacity, realtime data check and background data
check type.  SetOptions() used to write 0 there whatever the unit had, so
changing a threshold could switch those settings off.  It now writes back the
unit's current byte unchanged: the OptionsFlags value in the options dict
(GetOptions returns it), else the byte read fresh with GetOptions.  Units that
report no such byte get exactly the bytes they always got.

The flags stay read only: nothing here makes them settable.

Only the FakeDevice from test_regression is used: no hardware, no network,
state in a temp dir.
"""

import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import FakeDevice, make_drobo, setUpModule, \
    tearDownModule  # noqa: F401,E402
from test_fixtures_extra import (page, responder, options2_page,  # noqa: E402
                                 BASE_FEATURES, SUPPORTS_OPTIONS2,
                                 SUPPORTS_ISCSI, OPTIONS_PATTERN)
from test_options_extra import gen2  # noqa: E402
from test_cov_drobom import DrobomTestCase  # noqa: E402

import Drobo  # noqa: E402

OPTIONS2_FEATURES = BASE_FEATURES | SUPPORTS_OPTIONS2 | SUPPORTS_ISCSI

V1_CDB = bytes.fromhex('55017a30000000000d00')
V2_CDB = bytes.fromhex('55017a31000000001700')


def options_page(flags, yellow=85, red=95):
    return page(0x30, OPTIONS_PATTERN, (yellow, red, flags, 0, 80, 90))


def v1_today(yellow, red):
    """ the 0x30 buffer SetOptions has always sent: flags byte (+6) 0. """
    return bytes.fromhex('7a300009') + bytes([yellow, red]) + b'\0' * 7


def with_flags(buf, flags):
    return buf[:6] + bytes([flags]) + buf[7:]


def v2_today(flags, spindown, ip, mask):
    return (bytes.fromhex('7a310013') + struct.pack('>QH', flags, spindown)
            + bytes(ip) + bytes(mask) + b'\0')


def options(**kw):
    o = {
        'YellowThreshold': 80,
        'RedThreshold': 90,
        'DualDiskRedundancy': True,
        'SpinDownDelayMinutes': 15,
        'UseManualVolumeManagement': False,
        'UseStaticIPAddress': False,
        'IPAddress': '10.1.2.3',
        'NetMask': '255.0.0.0',
    }
    o.update(kw)
    return o


V2_FOR_OPTIONS = (V2_CDB, v2_today(0x3, 15, [10, 1, 2, 3], [255, 0, 0, 0]))


class PutDevice(FakeDevice):
    """ a FakeDevice that records mode selects: puts = [(cdb, buffer)]. """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.puts = []

    def put_sub_page(self, mcb, buffer, DEBUG):
        self.puts.append((bytes(mcb), bytes(buffer)))
        return len(buffer)


def drobo(features=OPTIONS2_FEATURES, version=b'1.1.2', p30=None,
          gen2_style=False, **pages):
    if p30 is not None:
        pages['p30'] = p30
    pages.setdefault('p31', options2_page(0x0001, spindown=14))
    r = responder(features, version, **pages)
    dev = PutDevice(responder=gen2(r) if gen2_style else r)
    d = make_drobo(dev)
    dev.cdbs.clear()
    return d, dev


def sensed(dev):
    return [c[3] for c in dev.cdbs if c[0] == 0x5a]


class TestSetOptionsToday(unittest.TestCase):
    """ regression: units with no flags byte get today's exact bytes. """

    def test_old_firmware_unit(self):
        """ fw < 1.1.0, no OPTIONS2: GetOptions is None, nothing is read. """
        d, dev = drobo(BASE_FEATURES, version=b'1.0.3',
                       p30=options_page(0x05))
        self.assertIsNone(d.GetOptions())
        d.SetOptions({'YellowThreshold': 80, 'RedThreshold': 90})
        self.assertEqual(dev.puts, [(V1_CDB, v1_today(80, 90))])
        self.assertEqual(dev.puts[0][1].hex(), '7a300009505a00000000000000')
        self.assertEqual(sensed(dev), [])

    def test_unit_without_options_page(self):
        """ the 0x30 read fails (the page is not there): flags 0, as ever. """
        d, dev = drobo(BASE_FEATURES)
        d.SetOptions({'YellowThreshold': 80, 'RedThreshold': 90})
        self.assertEqual(dev.puts, [(V1_CDB, v1_today(80, 90))])

    def test_unit_without_options_page_options2(self):
        d, dev = drobo(p31=options2_page(0))
        d.SetOptions(options())
        self.assertEqual(dev.puts, [(V1_CDB, v1_today(80, 90)),
                                    V2_FOR_OPTIONS])

    def test_unit_reporting_zero(self):
        d, dev = drobo(p30=options_page(0))
        d.SetOptions(options())
        self.assertEqual(dev.puts, [(V1_CDB, v1_today(80, 90)),
                                    V2_FOR_OPTIONS])

    def test_dict_zero_wins(self):
        """ an OptionsFlags in the dict is what GetOptions just read. """
        d, dev = drobo(p30=options_page(0x03))
        d.SetOptions(options(OptionsFlags=0))
        self.assertEqual(dev.puts, [(V1_CDB, v1_today(80, 90)),
                                    V2_FOR_OPTIONS])
        self.assertEqual(sensed(dev), [])

    def test_flags_still_read_only(self):
        self.assertIn('OptionsFlags', Drobo.READ_ONLY_OPTIONS)


class TestSetOptionsKeepsFlags(unittest.TestCase):
    """ the unit's current +6 byte is written back unchanged. """

    def test_flags_from_dict(self):
        d, dev = drobo(p30=options_page(0))
        d.SetOptions(options(OptionsFlags=0x05))
        self.assertEqual(dev.puts[0][1][6], 0x05)
        self.assertEqual(dev.puts, [
            (V1_CDB, with_flags(v1_today(80, 90), 0x05)), V2_FOR_OPTIONS])
        self.assertEqual(dev.puts[0][1].hex(), '7a300009505a05000000000000')

    def test_flags_from_dict_not_reread(self):
        d, dev = drobo(p30=options_page(0x03))
        d.SetOptions(options(OptionsFlags=0x05))
        self.assertEqual(dev.puts[0][1][6], 0x05)
        self.assertEqual(sensed(dev), [])

    def test_flags_from_dict_v1_only(self):
        d, dev = drobo(BASE_FEATURES, p30=options_page(0))
        d.SetOptions({'YellowThreshold': 80, 'RedThreshold': 90,
                      'OptionsFlags': 0x05})
        self.assertEqual(dev.puts,
                         [(V1_CDB, with_flags(v1_today(80, 90), 0x05))])

    def test_flags_read_from_unit(self):
        d, dev = drobo(p30=options_page(0x03))
        d.SetOptions(options())
        self.assertEqual(dev.puts[0][1][6], 0x03)
        self.assertEqual(dev.puts, [
            (V1_CDB, with_flags(v1_today(80, 90), 0x03)), V2_FOR_OPTIONS])
        self.assertIn(0x30, sensed(dev))

    def test_flags_read_from_unit_v1_only(self):
        d, dev = drobo(BASE_FEATURES, p30=options_page(0x03))
        d.SetOptions({'YellowThreshold': 80, 'RedThreshold': 90})
        self.assertEqual(dev.puts,
                         [(V1_CDB, with_flags(v1_today(80, 90), 0x03))])

    def test_flags_read_from_gen2_unit(self):
        d, dev = drobo(p30=options_page(0xff), gen2_style=True)
        d.SetOptions(options())
        self.assertEqual(dev.puts, [
            (V1_CDB, with_flags(v1_today(80, 90), 0xff)), V2_FOR_OPTIONS])

    def test_only_byte_6_differs(self):
        for flags in (0x01, 0x03, 0x05, 0x80, 0xff):
            with self.subTest(flags=flags):
                d, dev = drobo(p30=options_page(flags))
                d.SetOptions(options())
                (c1, b1), second = dev.puts
                old = v1_today(80, 90)
                self.assertEqual(c1, V1_CDB)
                self.assertEqual(len(b1), len(old))
                self.assertEqual([i for i in range(len(old))
                                  if b1[i] != old[i]], [6])
                self.assertEqual(b1[6], flags)
                self.assertEqual(second, V2_FOR_OPTIONS)


class TestDrobomSetKeepsFlags(DrobomTestCase):
    """ drobom set <threshold>, end to end against the FakeDevice. """

    def set(self, name, value, flags, features=OPTIONS2_FEATURES):
        dev = PutDevice(responder=responder(
            features, p30=options_page(flags),
            p31=options2_page(0x0001, spindown=14)))
        with self.device(dev):
            r = self.drobom('set', name, value)
        self.assertEqual(r.out, '')
        self.assertEqual(r.err, '')
        return dev.puts

    # what the 0x31 write is for options2_page(0x0001, spindown=14)
    V2 = (V2_CDB, v2_today(0x3, 14, [10, 0, 0, 5], [255, 0, 0, 0]))

    def test_yellow_threshold_flags_zero(self):
        """ regression: a unit reporting 0 gets today's bytes. """
        puts = self.set('YellowThreshold', '70', 0)
        self.assertEqual(puts, [(V1_CDB, v1_today(70, 95)), self.V2])

    def test_yellow_threshold_keeps_flags(self):
        puts = self.set('YellowThreshold', '70', 0x05)
        self.assertEqual(puts, [(V1_CDB, with_flags(v1_today(70, 95), 0x05)),
                                self.V2])

    def test_red_threshold_keeps_flags(self):
        puts = self.set('RedThreshold', '97', 0x03)
        self.assertEqual(puts, [(V1_CDB, with_flags(v1_today(85, 97), 0x03)),
                                self.V2])

    def test_v1_only_unit_keeps_flags(self):
        puts = self.set('YellowThreshold', '70', 0x05, BASE_FEATURES)
        self.assertEqual(puts,
                         [(V1_CDB, with_flags(v1_today(70, 95), 0x05))])

    def test_flags_not_settable(self):
        puts = self.set_refused()
        self.assertEqual(puts, [])

    def set_refused(self):
        dev = PutDevice(responder=responder(
            OPTIONS2_FEATURES, p30=options_page(0x05),
            p31=options2_page(0x0001, spindown=14)))
        with self.device(dev):
            r = self.drobom('set', 'OptionsFlags', '0')
        self.assertEqual(r.out, 'option is read-only:  OptionsFlags\n')
        return dev.puts


NO_ISCSI_FEATURES = BASE_FEATURES | SUPPORTS_OPTIONS2  # e.g. a Drobo S


class TestSetOptionsWithoutIscsi(unittest.TestCase):
    """ units with SUPPORTS_OPTIONS2 but not SUPPORTS_ISCSI: GetOptions has
        no UseStaticIPAddress, IPAddress or NetMask there, so
        SetOptions(GetOptions()) used to raise KeyError after the 0x30
        write.  Those 0x31 fields are now written back as the unit reports
        them (read fresh), like the 0x30 flags byte.  Units with iSCSI get
        exactly the bytes they always got, with no extra read. """

    def test_options_lack_the_address_keys(self):
        d, dev = drobo(NO_ISCSI_FEATURES, p30=options_page(0x05))
        o = d.GetOptions()
        for k in ('UseStaticIPAddress', 'IPAddress', 'NetMask'):
            self.assertNotIn(k, o)

    def test_set_get_options_round_trip(self):
        d, dev = drobo(NO_ISCSI_FEATURES, p30=options_page(0x05),
                       p31=options2_page(0x0001, spindown=14))
        d.SetOptions(d.GetOptions())
        self.assertEqual(dev.puts, [
            (V1_CDB, bytes.fromhex('7a300009' '555f05' '00000000' '0000')),
            (V2_CDB, bytes.fromhex('7a310013' '0000000000000003' '000e'
                                   '0a000005' 'ff000000' '00')),
        ])

    def test_set_get_options_round_trip_gen2(self):
        d, dev = drobo(NO_ISCSI_FEATURES, p30=options_page(0x05),
                       p31=options2_page(0x0001, spindown=14),
                       gen2_style=True)
        d.SetOptions(d.GetOptions())
        self.assertEqual(dev.puts, [
            (V1_CDB, with_flags(v1_today(85, 95), 0x05)),
            (V2_CDB, v2_today(0x3, 14, [10, 0, 0, 5], [255, 0, 0, 0])),
        ])

    def test_unit_values_written_back(self):
        """ the static IP bit (0x0008) and the first address set come from
            the unit as it is now. """
        d, dev = drobo(NO_ISCSI_FEATURES, p30=options_page(0),
                       p31=options2_page(0x0009, spindown=0,
                                         before=('192.168.1.7',
                                                 '255.255.255.0',
                                                 '192.168.1.1', 1500)))
        d.SetOptions(d.GetOptions())
        self.assertEqual(dev.puts, [
            (V1_CDB, v1_today(85, 95)),
            (V2_CDB, v2_today(0x9, 0, [192, 168, 1, 7], [255, 255, 255, 0])),
        ])

    def test_unit_read_before_anything_is_written(self):
        """ the 0x31 read comes first: if it fails, nothing is written. """
        d, dev = drobo(NO_ISCSI_FEATURES, p30=options_page(0x05))
        o = d.GetOptions()
        dev.cdbs.clear()
        with mock.patch.object(dev, 'responder',
                               side_effect=IOError('io_hdr status is: 2')):
            with self.assertRaises(IOError):
                d.SetOptions(o)
        self.assertEqual(dev.puts, [])

    def test_dict_values_win(self):
        """ keys present in the dict are used, not re-read. """
        d, dev = drobo(NO_ISCSI_FEATURES, p30=options_page(0x05))
        d.SetOptions(options(OptionsFlags=0x05))
        self.assertEqual(sensed(dev), [])
        self.assertEqual(dev.puts, [
            (V1_CDB, with_flags(v1_today(80, 90), 0x05)), V2_FOR_OPTIONS])

    def test_iscsi_unit_bytes_unchanged_and_not_reread(self):
        """ regression: an iSCSI unit's GetOptions has every key; exactly
            the bytes sent before this fix, and no read during the set. """
        d, dev = drobo(p30=options_page(0x05),
                       p31=options2_page(0x0001, spindown=14))
        o = d.GetOptions()
        dev.cdbs.clear()
        d.SetOptions(o)
        self.assertEqual(sensed(dev), [])
        self.assertEqual(dev.puts, [
            (V1_CDB, bytes.fromhex('7a300009' '555f05' '00000000' '0000')),
            (V2_CDB, bytes.fromhex('7a310013' '0000000000000003' '000e'
                                   '0a000005' 'ff000000' '00')),
        ])


class TestDrobomSetWithoutIscsi(DrobomTestCase):
    """ drobom set on a unit with OPTIONS2 but not iSCSI, end to end. """

    def test_yellow_threshold(self):
        dev = PutDevice(responder=responder(
            NO_ISCSI_FEATURES, p30=options_page(0x05),
            p31=options2_page(0x0001, spindown=14)))
        with self.device(dev):
            r = self.drobom('set', 'YellowThreshold', '70')
        self.assertEqual(r.out, '')
        self.assertEqual(r.err, '')
        self.assertEqual(dev.puts, [
            (V1_CDB, with_flags(v1_today(70, 95), 0x05)),
            (V2_CDB, v2_today(0x3, 14, [10, 0, 0, 5], [255, 0, 0, 0])),
        ])

    def test_address_options_not_offered(self):
        """ GetOptions has no IPAddress there, so drobom does not set it. """
        dev = PutDevice(responder=responder(
            NO_ISCSI_FEATURES, p30=options_page(0x05),
            p31=options2_page(0x0001, spindown=14)))
        with self.device(dev):
            r = self.drobom('set', 'IPAddress', '10.9.9.9')
        self.assertEqual(r.out, 'option not present:  IPAddress\n')
        self.assertEqual(dev.puts, [])


if __name__ == '__main__':
    unittest.main()
