"""
Coverage backfill: Drobo.py write commands and diagnostics (plan 9).

Characterisation tests: they pin what the code does today, against a fake
device only. Tests that expose a real bug are marked expectedFailure, with a
comment saying what is wrong; production code is not changed here.

Nothing here can reach hardware: every Drobo is built by make_drobo, which
replaces DroboIOctl.DroboIOctl with a fake, and the write commands land in
the fake's put_sub_page / ea_responder. Nothing is written outside temp dirs:
dumpDiagnostics' hard coded /tmp path is redirected into a TemporaryDirectory.

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import builtins
import io
import os
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import FakeDevice, make_drobo, SLOTS  # noqa: E402

import Drobo  # noqa: E402
import DroboIOctl  # noqa: E402

_STATE = None


def setUpModule():
    """ no test may read or write saved state in the real home directory. """
    global _STATE
    _STATE = tempfile.TemporaryDirectory()
    os.environ['DROBO_UTILS_STATE_DIR'] = _STATE.name


def tearDownModule():
    os.environ.pop('DROBO_UTILS_STATE_DIR', None)
    _STATE.cleanup()


def cdb(*fields):
    """ the 10 byte CDB layout every command here uses. """
    return struct.pack('>BBBBBBBHB', *fields)


class WriteDevice(FakeDevice):
    """ a legacy Drobo that also records mode selects and 0xEA commands.

        puts: [(cdb, buffer)] from put_sub_page.
        ea:   [(cdb, sz, out)] from get_sub_page with an 0xEA CDB.
    """

    def __init__(self, ea_reply=None, **kw):
        super().__init__(**kw)
        self.puts = []
        self.ea = []
        self.closed = 0
        # ea_reply(cdb, sz, out) -> bytes; default: a one byte ack.
        self.ea_reply = ea_reply or (lambda c, sz, out: b'\0' * sz)
        self.ea_responder = self._ea

    def _ea(self, mcb, sz, out):
        self.ea.append((mcb, sz, out))
        return self.ea_reply(mcb, sz, out)

    def put_sub_page(self, mcb, buffer, DEBUG):
        self.puts.append((bytes(mcb), bytes(buffer)))
        return len(buffer)

    def closefd(self):
        self.closed += 1


class DroboCase(unittest.TestCase):
    """ a constructed Drobo on a WriteDevice, CDB log cleared, DEBUG reset. """

    def setUp(self):
        self.dev = WriteDevice()
        self.d = make_drobo(self.dev)
        self.dev.cdbs.clear()
        self.d.transactionID = 7
        self.addCleanup(setattr, Drobo, 'DEBUG', 0)

    def debug(self, flags):
        Drobo.DEBUG = flags

    def quiet(self, fn, *args, **kw):
        """ call fn, return (result, stdout text). """
        out = io.StringIO()
        with redirect_stdout(out):
            r = fn(*args, **kw)
        return r, out.getvalue()


# --- Sync ---------------------------------------------------------------------


# The sub-page header length field is the length of the big endian body
# that follows it: 38 (LH32s), 9 (BBBLBB) and 19 (QHLLB) bytes on every
# platform. (It used to be the native struct size: 42 / 18 / 33 on 64-bit.)
SYNC_LEN = 38
OPT1_LEN = 9
OPT2_LEN = 19

# what the header length used to be (native sizes); only header bytes 2:4
# may differ from a buffer packed with these.
NATIVE_SYNC_LEN = struct.calcsize('LH32s')
NATIVE_OPT1_LEN = struct.calcsize('BBBLBB')
NATIVE_OPT2_LEN = struct.calcsize('QHLLB')


def differing_offsets(a, b):
    assert len(a) == len(b)
    return [i for i in range(len(a)) if a[i] != b[i]]


class TestSync(DroboCase):

    NOW = 1700000000

    def sync(self, *args):
        with mock.patch.object(Drobo.time, 'time',
                               return_value=self.NOW + 0.75):
            self.d.Sync(*args)

    def expected(self, name):
        cdb_ = cdb(0x55, 0x01, 0x7a, 0x05, 0, 0, 0, 42, 0)
        body = struct.pack('>LH32s', self.NOW, 0, name)
        return cdb_, struct.pack('>BBH', 0x7a, 0x05, SYNC_LEN) + body

    def test_sync_with_bytes_name_sends_mode_select_page_05(self):
        self.sync(b'NEWNAME')
        self.assertEqual(self.dev.puts, [self.expected(b'NEWNAME')])
        # exact layout: header, time truncated to whole seconds, 0, name.
        buf = self.dev.puts[0][1]
        self.assertEqual(len(buf), 42)
        self.assertEqual(buf[4:8], bytes.fromhex('6553f100'))
        self.assertEqual(buf[8:10], b'\0\0')
        self.assertEqual(buf[10:17], b'NEWNAME')
        self.assertEqual(buf[17:], b'\0' * 25)

    def test_sync_header_length_is_body_length(self):
        """ the header claims exactly the 38 bytes that follow it (it used
            the native calcsize("LH32s"), 42 on 64-bit hosts). """
        self.sync(b'NEWNAME')
        buf = self.dev.puts[0][1]
        self.assertEqual(struct.unpack('>H', buf[2:4])[0], len(buf) - 4)
        self.assertEqual(len(buf) - 4, 38)

    def test_sync_exact_bytes(self):
        """ Sync is a write: every byte of CDB and buffer is pinned. """
        self.sync(b'NEWNAME')
        self.assertEqual(self.dev.puts, [(
            bytes.fromhex('55017a05000000002a00'),
            bytes.fromhex('7a050026' '6553f100' '0000') + b'NEWNAME'
            + b'\0' * 25)])

    def test_sync_only_the_header_length_changed(self):
        """ against the old (native length) packing, only bytes 2:4 of the
            buffer differ, and the CDB is the same. """
        self.sync(b'NEWNAME')
        mcb, buf = self.dev.puts[0]
        old = struct.pack('>BBHLH32s', 0x7a, 0x05, NATIVE_SYNC_LEN,
                          self.NOW, 0, b'NEWNAME')
        self.assertEqual(mcb, cdb(0x55, 0x01, 0x7a, 0x05, 0, 0, 0, 42, 0))
        self.assertTrue(set(differing_offsets(buf, old)) <= {2, 3})

    def test_sync_sends_no_0xea_and_leaves_transaction_alone(self):
        self.sync(b'X')
        self.assertEqual(self.dev.ea, [])
        self.assertEqual(self.d.transactionID, 7)

    def test_sync_long_name_is_truncated_to_32_bytes(self):
        self.sync(b'A' * 40)
        self.assertEqual(self.dev.puts[0][1][10:], b'A' * 32)

    def test_sync_without_name_keeps_current_name(self):
        """ GetSubPageSettings()[2] is a str under Python 3; Sync encodes it
            for the 32s field. `drobom set time` and the GUI's time sync
            both call Sync() like this. """
        self.sync()
        self.assertEqual(self.dev.puts, [self.expected(b'TRUSTED DATA')])

    def test_sync_with_str_name(self):
        """ `drobom set name NEW` calls Sync(args[2]) with a str. """
        self.sync('NEWNAME')
        self.assertEqual(self.dev.puts, [self.expected(b'NEWNAME')])

    def test_sync_str_and_bytes_names_send_the_same_bytes(self):
        self.sync('NEWNAME')
        self.sync(b'NEWNAME')
        self.assertEqual(self.dev.puts[0], self.dev.puts[1])

    def test_sync_long_str_name_is_truncated_to_32_bytes(self):
        self.sync('B' * 40)
        self.assertEqual(self.dev.puts[0][1][10:], b'B' * 32)

    def test_sync_short_str_name_is_nul_padded(self):
        self.sync('ab')
        self.assertEqual(self.dev.puts[0][1][10:], b'ab' + b'\0' * 30)

    def test_sync_non_ascii_str_name_is_refused_before_sending(self):
        """ the name is ASCII on the wire (GetSubPageSettings decodes it as
            UTF-8, which round trips only ASCII); anything else raises
            before a byte goes to the Drobo. """
        with self.assertRaises(UnicodeEncodeError):
            self.sync('Caf\xe9')
        self.assertEqual(self.dev.puts, [])

    def test_sync_simulation_sends_nothing(self):
        self.debug(Drobo.DBG_Simulation)
        self.sync(b'NEWNAME')
        self.assertEqual(self.dev.puts, [])
        self.assertEqual(self.dev.cdbs, [])


# --- SetOptions -----------------------------------------------------------------


def options(**kw):
    o = {
        'YellowThreshold': 85,
        'RedThreshold': 95,
        'DualDiskRedundancy': False,
        'SpinDownDelayMinutes': 0,
        'UseManualVolumeManagement': False,
        'UseStaticIPAddress': False,
        'IPAddress': '192.168.10.4',
        'NetMask': '255.255.255.0',
    }
    o.update(kw)
    return o


V1_CDB = cdb(0x55, 0x01, 0x7a, 0x30, 0, 0, 0, 13, 0)
V2_CDB = cdb(0x55, 0x01, 0x7a, 0x31, 0, 0, 0, 23, 0)


def v1_buffer(yellow, red):
    # 7a 30 len | yellow red flags=0 datacheck(L)=0 lowyellow=0 lowred=0
    return (struct.pack('>BBH', 0x7a, 0x30, OPT1_LEN)
            + bytes([yellow, red, 0, 0, 0, 0, 0, 0, 0]))


def v2_buffer(flags, spindown, ip, mask):
    # 7a 31 len | flags(Q) spindown(H) ip(L) mask(L) 0(B), big endian,
    # the address bytes in network order.
    return (struct.pack('>BBH', 0x7a, 0x31, OPT2_LEN) + struct.pack('>QH', flags, spindown)
            + bytes(ip) + bytes(mask) + b'\0')


class TestSetOptions(DroboCase):

    def test_v1_only_unit_sends_one_mode_select(self):
        self.d.features = []
        self.d.SetOptions(options(YellowThreshold=80, RedThreshold=90))
        self.assertEqual(self.dev.puts, [(V1_CDB, v1_buffer(80, 90))])

    def test_v1_ignores_options2_fields(self):
        self.d.features = []
        self.d.SetOptions({'YellowThreshold': 1, 'RedThreshold': 2})
        self.assertEqual(self.dev.puts, [(V1_CDB, v1_buffer(1, 2))])

    def test_options2_unit_sends_v1_then_v2(self):
        self.assertIn('SUPPORTS_OPTIONS2', self.d.features)
        self.d.SetOptions(options())
        self.assertEqual(self.dev.puts, [
            (V1_CDB, v1_buffer(85, 95)),
            (V2_CDB, v2_buffer(0, 0, [192, 168, 10, 4], [255, 255, 255, 0])),
        ])

    def test_options2_flag_bits(self):
        cases = [
            ({'DualDiskRedundancy': True}, 0x1, 0),
            ({'SpinDownDelayMinutes': 15}, 0x2, 15),
            ({'UseManualVolumeManagement': True}, 0x4, 0),
            ({'UseStaticIPAddress': True}, 0x8, 0),
            ({'DualDiskRedundancy': True, 'SpinDownDelayMinutes': 1,
              'UseManualVolumeManagement': True,
              'UseStaticIPAddress': True}, 0xf, 1),
        ]
        for kw, flags, spin in cases:
            with self.subTest(kw=kw):
                self.dev.puts.clear()
                self.d.SetOptions(options(**kw))
                self.assertEqual(self.dev.puts[1], (V2_CDB, v2_buffer(
                    flags, spin, [192, 168, 10, 4], [255, 255, 255, 0])))

    def test_options_header_lengths_are_body_lengths(self):
        """ as in Sync: 9 and 19 bytes follow the headers, and the headers
            say so (they used the native calcsize('BBBLBB') and
            calcsize('QHLLB'), 18 and 33 on 64-bit Linux). """
        self.d.SetOptions(options())
        for c, buf in self.dev.puts:
            self.assertEqual(struct.unpack('>H', buf[2:4])[0], len(buf) - 4)
        self.assertEqual([len(b) - 4 for c, b in self.dev.puts], [9, 19])

    def test_options_exact_bytes(self):
        """ SetOptions is a write: every byte of both CDBs and buffers. """
        self.d.SetOptions(options(YellowThreshold=80, RedThreshold=90,
                                  DualDiskRedundancy=True,
                                  SpinDownDelayMinutes=15,
                                  IPAddress='10.1.2.3',
                                  NetMask='255.0.0.0'))
        self.assertEqual(self.dev.puts, [
            (bytes.fromhex('55017a30000000000d00'),
             bytes.fromhex('7a300009' '505a00' '00000000' '0000')),
            (bytes.fromhex('55017a31000000001700'),
             bytes.fromhex('7a310013' '0000000000000003' '000f'
                           '0a010203' 'ff000000' '00')),
        ])

    def test_options_only_the_header_lengths_changed(self):
        """ against the old (native length) packing, only bytes 2:4 of
            each buffer differ; the CDBs are unchanged. """
        self.d.SetOptions(options(YellowThreshold=80, RedThreshold=90))
        (c1, b1), (c2, b2) = self.dev.puts
        old1 = struct.pack('>BBHBBBLBB', 0x7a, 0x30, NATIVE_OPT1_LEN,
                           80, 90, 0, 0, 0, 0)
        old2 = struct.pack('>BBHQHLLB', 0x7a, 0x31, NATIVE_OPT2_LEN,
                           0, 0, 0xc0a80a04, 0xffffff00, 0)
        self.assertEqual((c1, c2), (V1_CDB, V2_CDB))
        self.assertTrue(set(differing_offsets(b1, old1)) <= {2, 3})
        self.assertTrue(set(differing_offsets(b2, old2)) <= {2, 3})

    def test_options2_address_byte_order(self):
        self.d.SetOptions(options(IPAddress='10.1.2.3',
                                  NetMask='255.0.0.0'))
        buf = self.dev.puts[1][1]
        self.assertEqual(buf[14:18], bytes([10, 1, 2, 3]))
        self.assertEqual(buf[18:22], bytes([255, 0, 0, 0]))

    def test_iscsi_unit_sends_same_bytes_and_ignores_iscsi_fields(self):
        """ SetOptions has no iSCSI branch: Gateway/MTU and the second
            interface from GetOptions are not written back. """
        self.d.SetOptions(options(UseStaticIPAddress=True))
        plain = list(self.dev.puts)
        self.dev.puts.clear()
        self.d.features = self.d.features + ['SUPPORTS_ISCSI']
        self.d.SetOptions(options(UseStaticIPAddress=True,
                                  Gateway='192.168.10.1', MTU=9000,
                                  IPAddress2='10.0.0.5',
                                  NetMask2='255.0.0.0'))
        self.assertEqual(self.dev.puts, plain)
        self.assertEqual(len(plain), 2)

    def test_options2_requires_address_fields(self):
        with self.assertRaises(KeyError):
            self.d.SetOptions({'YellowThreshold': 1, 'RedThreshold': 2})
        # the v1 page has already gone out by then.
        self.assertEqual(self.dev.puts, [(V1_CDB, v1_buffer(1, 2))])

    def test_simulation_sends_nothing(self):
        """ in simulation SetOptions writes nothing, with or without
            OPTIONS2 (it used to have no simulation guard.) """
        self.debug(Drobo.DBG_Simulation)
        for features in ([], self.d.features):
            with self.subTest(features=features):
                self.d.features = features
                self.d.SetOptions(options())
                self.assertEqual(self.dev.puts, [])
                self.assertEqual(self.dev.cdbs, [])

    def test_set_options_sends_no_query_and_no_transaction(self):
        """ with OptionsFlags in the dict (as GetOptions returns it),
            nothing is read. """
        self.d.SetOptions(options(OptionsFlags=0))
        self.assertEqual(self.dev.cdbs, [])
        self.assertEqual(self.d.transactionID, 7)

    def test_set_options_without_flags_reads_only_0x30(self):
        """ read-modify-write of the 0x30 flags byte: without OptionsFlags
            in the dict, SetOptions reads it first (one MODE SENSE of 0x30;
            this unit has no such page, so 0 is written, as before.) """
        self.d.SetOptions(options())
        self.assertEqual(self.dev.cdbs, [cdb(0x5a, 0, 0x3a, 0x30, 0, 0, 0,
                                             13, 0)])
        self.assertEqual(self.d.transactionID, 7)


# --- SetLunSize -------------------------------------------------------------------


class TestSetLunSize(DroboCase):

    def test_sends_0xea_0f_with_lun_size_and_advances_transaction(self):
        with mock.patch.object(self.d, 'umount', return_value=True) as um:
            self.d.SetLunSize(16)
        um.assert_called_once_with()
        self.assertEqual(self.dev.puts, [
            (cdb(0xea, 0x10, 0, 0x0f, 0, 7, 0x21, 4, 0),
             b'\x00\x00\x00\x10')
        ])
        self.assertEqual(self.d.transactionID, 8)

    def test_lun_size_is_signed_big_endian(self):
        with mock.patch.object(self.d, 'umount', return_value=True):
            self.d.SetLunSize(2)
            self.d.SetLunSize(-1)
        self.assertEqual([b for c, b in self.dev.puts],
                         [b'\0\0\0\x02', b'\xff\xff\xff\xff'])
        # second command carries the next transaction id.
        self.assertEqual(self.dev.puts[1][0][5], 8)

    def test_simulation_sends_nothing(self):
        self.debug(Drobo.DBG_Simulation)
        with mock.patch.object(self.d, 'umount', return_value=True):
            self.d.SetLunSize(16)
        self.assertEqual(self.dev.puts, [])
        self.assertEqual(self.dev.cdbs, [])

    def test_umount_failure_sends_nothing(self):
        with mock.patch.object(self.d, 'umount', return_value=False):
            self.d.SetLunSize(16)
        self.assertEqual(self.dev.puts, [])
        self.assertEqual(self.d.transactionID, 7)

    def test_chatty_messages(self):
        self.debug(Drobo.DBG_Chatty)
        with mock.patch.object(self.d, 'umount', return_value=False):
            _, out = self.quiet(self.d.SetLunSize, 4)
        self.assertEqual(out, 'set lunsize to 4 TiB\n'
                         'cannot free up Drobo to set lunsize\n')
        with mock.patch.object(self.d, 'umount', return_value=True):
            _, out = self.quiet(self.d.SetLunSize, 4)
        self.assertEqual(out, 'set lunsize to 4 TiB\n')
        self.assertEqual(len(self.dev.puts), 1)


# --- Blink / Standby / __issueCommand / __transactionNext --------------------------


def command_cdb(command, tid):
    return cdb(0xea, 0x10, 0x00, command, 0x00, tid, 0x20, 0x01, 0x00)


class TestIssueCommand(DroboCase):

    def test_blink_cdb(self):
        self.d.Blink()
        self.assertEqual(self.dev.ea, [(command_cdb(0x06, 7), 1, 1)])
        self.assertEqual(self.dev.puts, [])
        self.assertEqual(self.d.transactionID, 8)

    def test_blink_twice_uses_consecutive_transactions(self):
        self.d.Blink()
        self.d.Blink()
        self.assertEqual([c for c, sz, out in self.dev.ea],
                         [command_cdb(6, 7), command_cdb(6, 8)])

    def test_standby_after_umount(self):
        with mock.patch.object(self.d, 'umount', return_value=True) as um:
            self.d.Standby()
        um.assert_called_once_with()
        self.assertEqual(self.dev.ea, [(command_cdb(0x0d, 7), 1, 1)])
        self.assertEqual(self.d.transactionID, 8)

    def test_standby_umount_failure_sends_nothing(self):
        with mock.patch.object(self.d, 'umount', return_value=False):
            self.d.Standby()
        self.assertEqual(self.dev.ea, [])
        self.assertEqual(self.d.transactionID, 7)

    def test_ioctl_error_prints_hint_and_exits(self):
        def boom(c, sz, out):
            raise IOError('bad address')

        self.dev.ea_reply = boom
        with self.assertRaises(SystemExit) as cm:
            self.quiet(self.d.Blink)
        self.assertIsNone(cm.exception.code)
        self.assertEqual(self.d.transactionID, 7)

    def test_ioctl_error_message(self):
        def boom(c, sz, out):
            raise IOError('bad address')

        self.dev.ea_reply = boom
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            self.d.Blink()
        self.assertIn('you need to be the super user', out.getvalue())
        self.assertIn('try sudo or su', out.getvalue())

    def test_short_reply_raises_after_advancing_transaction(self):
        for reply in (b'', b'\0\0'):
            with self.subTest(reply=reply):
                self.d.transactionID = 7
                self.dev.ea_reply = lambda c, sz, out, r=reply: r
                with self.assertRaises(Drobo.DroboException):
                    self.d.Blink()
                self.assertEqual(self.d.transactionID, 8)

    def test_simulation_advances_transaction_without_sending(self):
        self.debug(Drobo.DBG_Simulation)
        self.d.Blink()
        self.d.Standby()  # umount is a no-op success in simulation
        self.assertEqual(self.dev.ea, [])
        self.assertEqual(self.dev.cdbs, [])
        self.assertEqual(self.d.transactionID, 9)

    def test_hwdialog_message(self):
        self.debug(Drobo.DBG_HWDialog)
        _, out = self.quiet(self.d.Blink)
        self.assertEqual(out, 'issuecommand...\n')


class TestTransactionNext(DroboCase):

    def step(self):
        self.d._Drobo__transactionNext()
        return self.d.transactionID

    def test_increments(self):
        self.assertEqual(self.step(), 8)

    def test_wraparound(self):
        """ 250 still steps to 251 (MAX_TRANSACTION is checked before the
            increment); from 251 it wraps to 1. 0 is never produced. """
        self.d.transactionID = Drobo.MAX_TRANSACTION - 1
        self.assertEqual([self.step() for _ in range(4)], [250, 251, 1, 2])

    def test_out_of_range_value_wraps(self):
        self.d.transactionID = 255
        self.assertEqual(self.step(), 1)

    def test_wrapped_id_lands_in_the_cdb(self):
        self.d.transactionID = 251
        self.d.Blink()
        self.d.Blink()
        self.assertEqual([c[5] for c, sz, out in self.dev.ea], [251, 1])

    def test_initial_id_in_range(self):
        for _ in range(20):
            d = make_drobo(WriteDevice())
            self.assertGreaterEqual(d.transactionID, 1)
            self.assertLessEqual(d.transactionID, Drobo.MAX_TRANSACTION)


# --- GetDiagRecord ------------------------------------------------------------


def diag_first(code, tid, buflen=4096):
    return cdb(0xea, 0x10, 0x80, code, 0, tid, 0x21, buflen, 0)


def diag_next(code, tid, buflen=4096):
    return cdb(0xea, 0x10, 0x80, code, 0, tid, 0x01, buflen, 0)


def paged(*pages):
    """ ea reply that hands out the given pages, one per call. """
    it = iter(pages)
    return lambda c, sz, out: next(it)


class TestGetDiagRecord(DroboCase):

    def test_single_short_page(self):
        self.dev.ea_reply = paged(b'abc')
        self.assertEqual(self.d.GetDiagRecord(4), b'abc')
        self.assertEqual(self.dev.ea, [(diag_first(4, 7), 4096, 0)])

    def test_pages_until_a_short_one(self):
        p1, p2, p3 = b'\x01' * 4096, b'\x02' * 4096, b'\x03' * 100
        self.dev.ea_reply = paged(p1, p2, p3)
        self.assertEqual(self.d.GetDiagRecord(7), p1 + p2 + p3)
        self.assertEqual(self.dev.ea, [
            (diag_first(7, 7), 4096, 0),
            (diag_next(7, 7), 4096, 0),
            (diag_next(7, 7), 4096, 0),
        ])

    def test_exact_multiple_needs_an_empty_page_to_stop(self):
        self.dev.ea_reply = paged(b'x' * 4096, b'')
        self.assertEqual(self.d.GetDiagRecord(4), b'x' * 4096)
        self.assertEqual(len(self.dev.ea), 2)

    def test_does_not_advance_transaction(self):
        self.dev.ea_reply = paged(b'a')
        self.d.GetDiagRecord(4)
        self.assertEqual(self.d.transactionID, 7)

    def test_decrypt_flag_is_ignored(self):
        """ decrypt is accepted but unused: decryption happens elsewhere
            (decodeDiagnostics / drobom). Same bytes, same CDBs. """
        page = bytes(range(256)) * 2
        self.dev.ea_reply = lambda c, sz, out: page
        plain = self.d.GetDiagRecord(4)
        sent = list(self.dev.ea)
        self.dev.ea.clear()
        self.assertEqual(self.d.GetDiagRecord(4, decrypt=1), plain)
        self.assertEqual(self.d.GetDiagRecord(4, 1), page)
        self.assertEqual(self.dev.ea[0], sent[0])

    def test_debug_messages(self):
        self.debug(Drobo.DBG_Chatty | Drobo.DBG_General)
        self.dev.ea_reply = paged(b'x' * 4096, b'yy')
        r, out = self.quiet(self.d.GetDiagRecord, 4)
        self.assertEqual(r, b'x' * 4096 + b'yy')
        self.assertEqual(out, 'Dumping Diagnostics...\nPage 0...\n'
                         'diags  1 , cmdlen= 2  diagslen= 4098\n')


# --- dumpDiagnostics ----------------------------------------------------------


GMTIME = time.struct_time((2026, 11, 22, 1, 2, 3, 6, 326, 0))
DIAG_PATH = '/tmp/DroboDiag_2026_1122_010203.log'


class TestDumpDiagnostics(DroboCase):
    """ dumpDiagnostics writes to a hard coded /tmp name; open() is patched
        to send exactly that name into a TemporaryDirectory, and to refuse
        anything else. """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.opened = []
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            self.opened.append((path, a))
            if path != DIAG_PATH:
                raise AssertionError('unexpected open: %r' % path)
            return real_open(self.local(), *a, **kw)

        for p in (mock.patch.object(Drobo, 'open', fake_open, create=True),
                  mock.patch.object(Drobo.time, 'gmtime',
                                    return_value=GMTIME)):
            p.start()
            self.addCleanup(p.stop)

    def local(self):
        return os.path.join(self.tmp, os.path.basename(DIAG_PATH))

    def records(self, rec4, rec7):
        def reply(c, sz, out):
            return {4: rec4, 7: rec7}[c[3]]
        self.dev.ea_reply = reply

    def test_dump_writes_both_records(self):
        """ GetDiagRecord returns bytes (as DroboIOctl.get_sub_page does);
            they go to the file unchanged. (The file used to be opened in
            text mode, so write() raised TypeError and `drobom diag` and
            the GUI button both failed.) """
        self.records(b'\x81\x82four', b'\x83seven')
        self.assertEqual(self.d.dumpDiagnostics(), DIAG_PATH)
        with open(self.local(), 'rb') as f:
            self.assertEqual(f.read(), b'\x81\x82four\x83seven')

    def test_dump_opens_the_file_in_binary_mode(self):
        self.records(b'four', b'seven')
        self.d.dumpDiagnostics()
        self.assertEqual(self.opened, [(DIAG_PATH, ('wb',))])

    def test_dump_keeps_carriage_returns_and_every_byte_value(self):
        rec = bytes(range(256))
        self.records(rec, b'\r\n\r')
        self.d.dumpDiagnostics()
        with open(self.local(), 'rb') as f:
            self.assertEqual(f.read(), rec + b'\r\n\r')

    def test_dump_file_name_order_and_transaction(self):
        """ the name, record 4 then 7, one transaction step at the end. """
        self.records(b'four|', b'seven')
        self.assertEqual(self.d.dumpDiagnostics(), DIAG_PATH)
        with open(self.local(), 'rb') as f:
            self.assertEqual(f.read(), b'four|seven')
        self.assertEqual([c for c, sz, out in self.dev.ea],
                         [diag_first(4, 7), diag_first(7, 7)])
        self.assertEqual(self.d.transactionID, 8)
        self.assertEqual(os.listdir(self.tmp), ['DroboDiag_2026_1122_010203.log'])


# --- decodeDiagnostics --------------------------------------------------------


def encrypt(text, key):
    return ''.join(chr(ord(c) ^ key) for c in text)


class TestDecodeDiagnostics(DroboCase):

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def write(self, data):
        """ write the (encrypted) file byte for byte; a str is one byte
            per character. """
        if isinstance(data, str):
            data = data.encode('latin-1')
        path = os.path.join(self.tmp, 'diag.log')
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_xor_key_comes_from_first_byte(self):
        """ the first plaintext byte is assumed to be '-' (0x2d). """
        plain = '-- Drobo diagnostics --\nslot 0 ok\n'
        for key in (0x00, 0x01, 0x0f, 0x40):
            with self.subTest(key=key):
                path = self.write(encrypt(plain, key))
                self.assertEqual(self.d.decodeDiagnostics(path), plain)

    def test_encrypted_carriage_return_survives(self):
        """ an encrypted byte that happens to be CR (here ' ' ^ 0x2d) is
            not translated to LF (it was, in text mode, and decoded to
            "'" instead of ' '). """
        plain = '-- Drobo --'
        path = self.write(encrypt(plain, 0x2d))
        self.assertEqual(self.d.decodeDiagnostics(path), plain)

    def test_encrypted_crlf_survives(self):
        plain = '-- a\r\nb\rc\n'
        for key in (0x00, 0x2d, 0x27):
            with self.subTest(key=key):
                path = self.write(encrypt(plain, key))
                self.assertEqual(self.d.decodeDiagnostics(path), plain)

    def test_wrong_first_byte_gives_garbage_not_error(self):
        path = self.write(encrypt('Xabc', 0x01))
        out = self.d.decodeDiagnostics(path)
        self.assertEqual(out[0], '-')
        self.assertNotEqual(out, 'Xabc')

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(
            self.d.decodeDiagnostics(os.path.join(self.tmp, 'nope')), '')

    def test_empty_file_raises_index_error(self):
        path = self.write('')
        with self.assertRaises(IndexError):
            self.d.decodeDiagnostics(path)

    def test_binary_diag_file_decodes(self):
        """ a real (binary) diagnostics file has bytes >= 0x80 that are not
            valid UTF-8; they decode one character per byte, whatever the
            locale. (Text mode reading failed to decode them, and the bare
            except returned ''.) """
        raw = bytes(b ^ 0x55 for b in b'-\xa0\xff ok')
        path = self.write(raw)
        self.assertEqual(self.d.decodeDiagnostics(path), '-\xa0\xff ok')

    def test_every_byte_value_decodes_to_one_character(self):
        plain = b'-' + bytes(range(256))
        for key in (0x00, 0x55, 0xd2):
            with self.subTest(key=key):
                path = self.write(bytes(b ^ key for b in plain))
                out = self.d.decodeDiagnostics(path)
                self.assertIsInstance(out, str)
                self.assertEqual(out, plain.decode('latin-1'))


# --- SlotCount, GetCharDev, __del__ --------------------------------------------


class TestSmallAccessors(DroboCase):

    def test_slot_count_queries_config(self):
        self.assertEqual(self.d.SlotCount(), SLOTS)
        paklen = struct.calcsize('>BBHBBBQ')
        self.assertEqual(self.dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0x5a, 0, 0x3a, 0x01, 0, 0, 0, paklen, 0)
        ])

    def test_slot_count_follows_the_unit(self):
        self.d.slot_count = 99
        self.assertEqual(self.d.SlotCount(), SLOTS)

    def test_get_char_dev(self):
        self.assertEqual(self.d.GetCharDev(), '/dev/sdq')

    def test_get_char_dev_list_uses_first(self):
        dev = WriteDevice()
        with mock.patch.object(DroboIOctl, 'DroboIOctl',
                               lambda *a, **k: dev):
            d = Drobo.Drobo(['/dev/sdr', '/dev/sds'], 0)
        self.assertEqual(d.GetCharDev(), '/dev/sdr')
        self.assertEqual(d.char_devs, ['/dev/sdr', '/dev/sds'])


class TestDel(DroboCase):

    def test_del_closes_device_once(self):
        self.d.__del__()
        self.assertEqual(self.dev.closed, 1)
        self.assertIsNone(self.d.fd)
        self.d.__del__()
        self.assertEqual(self.dev.closed, 1)

    def test_garbage_collection_closes_device(self):
        dev = WriteDevice()
        d = make_drobo(dev)
        del d
        self.assertEqual(dev.closed, 1)

    def test_simulation_drobo_has_no_fd(self):
        self.d.fd = None
        self.d.__del__()
        self.assertEqual(self.dev.closed, 0)

    def test_instantiation_message(self):
        self.debug(Drobo.DBG_Instantiation)
        _, out = self.quiet(self.d.__del__)
        self.assertEqual(out, '__del__ \n')
        self.assertEqual(self.dev.closed, 1)


if __name__ == '__main__':
    unittest.main()
