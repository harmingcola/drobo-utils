#!/usr/bin/env python3
"""
Coverage backfill for DroboIOctl.py: the SCSI generic (sg) layer itself.

No hardware is touched.  The two calls that reach the kernel are replaced:

  * the builtin open() that DroboIOctl.__init__ uses on the device file, and
  * fcntl.ioctl, as imported into DroboIOctl (DroboIOctl.ioctl),

by fakes.  FakeIoctl fills the ctypes buffers the way the sg driver would
(the sg_io_hdr status fields, and the data buffer dxferp points at) and
returns whatever result code a test asks for.

These are characterisation tests: they pin what the code does now.  Tests
that expose a real bug are marked expectedFailure, with the bug described.

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import ctypes
import fcntl
import io
import os
import runpy
import struct
import unittest
from contextlib import redirect_stdout
from unittest import mock

from test_regression import TOP
from test_detection import discover, fake

import Drobo
import DroboIOctl
from DroboIOctl import sg_io_hdr

SCSI_IOCTL_GET_IDLUN = 0x5382
ALL_DEBUG = 63  # every DBG_* flag below DBG_Simulation


class FakeFile:
    """ what the patched open() hands DroboIOctl instead of a device file. """

    def __init__(self, path, mode):
        self.path = path
        self.mode = mode
        self.closed = False

    def close(self):
        self.closed = True


def fake_open(path, mode='r', *a, **k):
    assert path.startswith('/dev/'), path
    return FakeFile(path, mode)


def field_address(hdr, name):
    """ the raw pointer stored in a c_char_p field of an sg_io_hdr. """
    return ctypes.c_void_p.from_buffer(hdr, getattr(sg_io_hdr, name).offset).value


class FakeIoctl:
    """
      Stand-in for fcntl.ioctl.  Every call is recorded.  For SG_IO the
      sg_io_hdr is filled in from the keyword settings:

        result: value returned by ioctl
        data:   bytes copied into the dxferp buffer (FROM_DEV transfers)
        status, resid, sb_len_wr, driver_status, host_status: header fields

      For SG_GET_VERSION_NUM and SCSI_IOCTL_GET_IDLUN the 'version' and
      'idlun' bytes are copied into the caller's buffer.
    """

    def __init__(self, result=0, data=b'', status=0, resid=0, sb_len_wr=0,
                 driver_status=0, host_status=0, version=b'', idlun=b'',
                 inquiry=None):
        self.result = result
        self.data = data
        self.status = status
        self.resid = resid
        self.sb_len_wr = sb_len_wr
        self.driver_status = driver_status
        self.host_status = host_status
        self.version = version
        self.idlun = idlun
        self.inquiry = inquiry
        self.calls = []

    def __call__(self, fd, request, arg, mutate=False):
        record = {'fd': fd, 'request': request, 'mutate': mutate}
        self.calls.append(record)
        if request == sg_io_hdr.SG_IO:
            return self._sg_io(arg, record)
        if request == sg_io_hdr.SG_GET_VERSION_NUM:
            ctypes.memmove(arg, self.version, len(self.version))
        elif request == SCSI_IOCTL_GET_IDLUN:
            ctypes.memmove(arg, self.idlun, len(self.idlun))
        return self.result

    def _sg_io(self, hdr, record):
        record['direction'] = hdr.dxfer_direction
        record['cdb'] = ctypes.string_at(field_address(hdr, 'cmdp'),
                                         hdr.cmd_len)
        record['dxfer_len'] = hdr.dxfer_len
        record['mx_sb_len'] = hdr.mx_sb_len
        record['timeout'] = hdr.timeout
        record['interface_id'] = hdr.interface_id
        buf = field_address(hdr, 'dxferp')
        if hdr.dxfer_direction == sg_io_hdr.SG_DXFER_TO_DEV:
            record['payload'] = ctypes.string_at(buf, hdr.dxfer_len)
        else:
            data = self.data
            if record['cdb'][0] == 0x12 and self.inquiry is not None:
                data = self.inquiry
            data = data[:hdr.dxfer_len]
            ctypes.memmove(buf, data, len(data))
        hdr.status = self.status
        hdr.resid = self.resid
        hdr.sb_len_wr = self.sb_len_wr
        hdr.driver_status = self.driver_status
        hdr.host_status = self.host_status
        return self.result


def open_dev(path='/dev/sdq', **kw):
    """ a DroboIOctl on a fake device file (ioctl is faked per call). """
    with mock.patch.object(DroboIOctl, 'open', fake_open, create=True):
        dio = DroboIOctl.DroboIOctl(path, **kw)
    return dio


def call(fake_ioctl, fn, *args):
    """ run fn(*args) with the fake ioctl, return (result, stdout). """
    out = io.StringIO()
    with mock.patch.object(DroboIOctl, 'ioctl', fake_ioctl), \
         redirect_stdout(out):
        result = fn(*args)
    return result, out.getvalue()


def call_raises(test, exc, fake_ioctl, fn, *args):
    out = io.StringIO()
    with mock.patch.object(DroboIOctl, 'ioctl', fake_ioctl), \
         redirect_stdout(out):
        with test.assertRaises(exc) as cm:
            fn(*args)
    return cm.exception, out.getvalue()


MODE_SENSE_3A_01 = struct.pack('>BBBBBBBBBB', 0x5a, 0, 0x3a, 1, 0, 0, 0, 0,
                               0x14, 0)


# --- hexdump ------------------------------------------------------------------

class TestHexdump(unittest.TestCase):

    def dump(self, label, data):
        out = io.StringIO()
        with redirect_stdout(out):
            DroboIOctl.hexdump(label, data)
        return out.getvalue()

    def test_short_bytes(self):
        self.assertEqual(self.dump('x', b'\x01\xab'), 'x 000:\n01 ab \n')

    def test_empty(self):
        self.assertEqual(self.dump('x', b''), 'x 000:\n\n')

    def test_wraps_every_16_bytes(self):
        text = self.dump('buf', bytes(range(17)))
        self.assertEqual(
            text, 'buf 000:\n' + ''.join('%02x ' % i for i in range(16)) +
            '\nbuf 010: 10 \n')

    def test_exactly_16_bytes_ends_with_empty_label_line(self):
        text = self.dump('b', bytes(16))
        self.assertEqual(text, 'b 000:\n' + '00 ' * 16 + '\nb 010: \n')

    def test_sequence_of_one_byte_bytes(self):
        # iterating a ctypes char array yields 1-byte bytes objects.
        self.assertEqual(self.dump('c', [b'\x7a', b'\x00']), 'c 000:\n7a 00 \n')
        buf = ctypes.create_string_buffer(b'\x05\x06', 2)
        self.assertEqual(self.dump('c', buf), 'c 000:\n05 06 \n')

    def test_list_of_ints(self):
        self.assertEqual(self.dump('i', [0, 255]), 'i 000:\n00 ff \n')


# --- sg_io_hdr ----------------------------------------------------------------

class TestSgIoHdr(unittest.TestCase):

    def test_defaults(self):
        h = sg_io_hdr()
        self.assertEqual(h.interface_id, ord('S'))
        self.assertEqual(h.timeout, 20000)
        for name in ('dxfer_direction', 'cmd_len', 'mx_sb_len', 'iovec_count',
                     'dxfer_len', 'flags', 'pack_id', 'status',
                     'masked_status', 'msg_status', 'sb_len_wr', 'host_status',
                     'driver_status', 'resid', 'duration', 'info'):
            with self.subTest(field=name):
                self.assertEqual(getattr(h, name), 0)
        self.assertIsNone(h.dxferp)
        self.assertIsNone(h.cmdp)
        self.assertIsNone(h.usr_ptr)

    def test_constants_match_scsi_sg_h(self):
        self.assertEqual(sg_io_hdr.SG_IO, 0x2285)
        self.assertEqual(sg_io_hdr.SG_GET_VERSION_NUM, 0x2282)
        self.assertEqual(sg_io_hdr.SG_DXFER_TO_DEV, -2)
        self.assertEqual(sg_io_hdr.SG_DXFER_FROM_DEV, -3)
        self.assertEqual(sg_io_hdr.SAM_STAT_GOOD, 0)
        self.assertEqual(sg_io_hdr.SAM_STAT_CHECK_CONDITION, 2)

    def test_layout_matches_64bit_linux(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest('64 bit layout only')
        # struct sg_io_hdr is 88 bytes on x86_64 / aarch64.
        self.assertEqual(ctypes.sizeof(sg_io_hdr), 88)
        self.assertEqual(sg_io_hdr.usr_ptr.offset, 56)
        self.assertEqual(sg_io_hdr.status.offset, 64)
        self.assertEqual(sg_io_hdr.resid.offset, 72)


# --- construction, version, closefd -------------------------------------------

class TestConstruction(unittest.TestCase):

    def test_opens_device_for_writing(self):
        dio = open_dev('/dev/sdz')
        self.assertEqual(dio.char_dev_file, '/dev/sdz')
        self.assertIsInstance(dio.sg_fd, FakeFile)
        self.assertEqual(dio.sg_fd.path, '/dev/sdz')
        self.assertEqual(dio.sg_fd.mode, 'w')

    def test_default_debug_is_1(self):
        self.assertEqual(open_dev().debug, 1)

    def test_debugflags_kept(self):
        self.assertEqual(open_dev(debugflags=63).debug, 63)

    def test_readwrite_ignored(self):
        dio = open_dev(readwrite=0)
        self.assertEqual(dio.sg_fd.mode, 'w')

    def test_open_failure_propagates(self):
        def refuse(path, mode='r'):
            raise PermissionError(13, 'Permission denied', path)
        with mock.patch.object(DroboIOctl, 'open', refuse, create=True):
            with self.assertRaises(PermissionError):
                DroboIOctl.DroboIOctl('/dev/sdq')

    def test_closefd_closes_file(self):
        dio = open_dev()
        f = dio.sg_fd
        self.assertIsNone(dio.closefd())
        self.assertTrue(f.closed)
        self.assertEqual(dio.sg_fd, -1)

    def test_closefd_twice_is_harmless(self):
        dio = open_dev()
        dio.closefd()
        dio.closefd()
        self.assertEqual(dio.sg_fd, -1)

    def test_closefd_leaves_int_fd_alone(self):
        dio = open_dev()
        dio.sg_fd = 7
        with mock.patch.object(os, 'close') as close:
            dio.closefd()
        close.assert_not_called()
        self.assertEqual(dio.sg_fd, -1)


class TestVersion(unittest.TestCase):

    def test_returns_driver_version(self):
        fio = FakeIoctl(version=struct.pack('l', 30536))
        dio = open_dev()
        result, out = call(fio, dio.version)
        self.assertEqual(result, 30536)
        self.assertEqual(out, '')
        self.assertEqual(fio.calls[0]['request'], sg_io_hdr.SG_GET_VERSION_NUM)
        self.assertIs(fio.calls[0]['fd'], dio.sg_fd)
        self.assertTrue(fio.calls[0]['mutate'])

    # version() used to name an undefined variable 'char_dev_file' here, so
    # a failing SG_GET_VERSION_NUM raised NameError instead of warning.
    def test_failure_prints_warning(self):
        fio = FakeIoctl(result=-1)
        dio = open_dev('/dev/sdq')
        result, out = call(fio, dio.version)
        self.assertIn('/dev/sdq is not an sg device', out)
        self.assertEqual(result, 0)  # the buffer the driver never filled


# --- get_sub_page ---------------------------------------------------------------

class TestGetSubPage(unittest.TestCase):

    def test_good_read(self):
        data = bytes(range(20))
        fio = FakeIoctl(data=data)
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.get_sub_page, 20, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, data)
        self.assertEqual(out, '')
        c = fio.calls[0]
        self.assertEqual(c['request'], sg_io_hdr.SG_IO)
        self.assertTrue(c['mutate'])
        self.assertEqual(c['cdb'], MODE_SENSE_3A_01)
        self.assertEqual(c['direction'], sg_io_hdr.SG_DXFER_FROM_DEV)
        self.assertEqual(c['dxfer_len'], 20)
        self.assertEqual(c['timeout'], 20000)
        self.assertEqual(c['interface_id'], ord('S'))

    def test_returns_whole_buffer_when_device_sends_less_without_resid(self):
        fio = FakeIoctl(data=b'\x7a\x01')
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.get_sub_page, 6, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, b'\x7a\x01\0\0\0\0')

    def test_resid_shortens_result(self):
        fio = FakeIoctl(data=b'abcdefgh', resid=3)
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.get_sub_page, 8, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, b'abcde')

    def test_resid_equal_to_size_gives_empty(self):
        fio = FakeIoctl(data=b'abcd', resid=4)
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.get_sub_page, 4, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, b'')

    def test_negative_resid_ignored(self):
        fio = FakeIoctl(data=b'abcd', resid=-2)
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.get_sub_page, 4, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, b'abcd')

    def test_out_sets_direction_to_device(self):
        fio = FakeIoctl()
        dio = open_dev(debugflags=0)
        call(fio, dio.get_sub_page, 4, MODE_SENSE_3A_01, 1, 0)
        self.assertEqual(fio.calls[0]['direction'], sg_io_hdr.SG_DXFER_TO_DEV)

    def test_ioctl_error_raises(self):
        fio = FakeIoctl(result=-1)
        dio = open_dev(debugflags=0)
        exc, out = call_raises(self, IOError, fio, dio.get_sub_page, 4,
                               MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(str(exc), 'Drobo get_mode_page SG_IO ioctl error')
        self.assertEqual(out, '')

    def test_check_condition_raises(self):
        fio = FakeIoctl(status=sg_io_hdr.SAM_STAT_CHECK_CONDITION, sb_len_wr=18)
        dio = open_dev(debugflags=0)
        exc, _ = call_raises(self, IOError, fio, dio.get_sub_page, 4,
                             MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(str(exc), 'io_hdr status is: 2')

    def test_other_bad_status_raises_hex(self):
        fio = FakeIoctl(status=0x18)  # RESERVATION CONFLICT
        dio = open_dev(debugflags=0)
        exc, _ = call_raises(self, IOError, fio, dio.get_sub_page, 4,
                             MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(str(exc), 'io_hdr status is: 18')

    def test_host_and_driver_status_not_checked(self):
        fio = FakeIoctl(data=b'ok', host_status=7, driver_status=8)
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.get_sub_page, 2, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, b'ok')

    def test_debug_argument_ignored_object_debug_used(self):
        fio = FakeIoctl(data=b'ok')
        dio = open_dev(debugflags=0)
        _, out = call(fio, dio.get_sub_page, 2, MODE_SENSE_3A_01, 0, ALL_DEBUG)
        self.assertEqual(out, '')

    def test_debug_hw_dialog_traces(self):
        fio = FakeIoctl(data=b'\x7a\x01', resid=0)
        dio = open_dev(debugflags=ALL_DEBUG)
        result, out = call(fio, dio.get_sub_page, 2, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(result, b'\x7a\x01')
        self.assertIn('mcb 000:\n5a 00 3a 01 00 00 00 00 14 00 \n', out)
        self.assertIn('4 before ioctl, sense_buffer_len= 0\n', out)
        self.assertIn('5 after ioctl, result=0 status: 0 driver_status: 0 '
                      'host_status: 0 sb_len_wr: 0 resid: 0\n', out)
        self.assertIn('page_buffer 000:\n7a 01 \n', out)
        self.assertIn('the length is:  2\n', out)

    def test_debug_traces_before_raising(self):
        fio = FakeIoctl(result=-1)
        dio = open_dev(debugflags=Drobo.DBG_HWDialog)
        _, out = call_raises(self, IOError, fio, dio.get_sub_page, 2,
                             MODE_SENSE_3A_01, 0, 0)
        self.assertIn('5 after ioctl, result=-1', out)
        self.assertNotIn('page_buffer', out)

    def test_debug_without_hw_dialog_is_quiet(self):
        fio = FakeIoctl(data=b'ok')
        dio = open_dev(debugflags=ALL_DEBUG & ~Drobo.DBG_HWDialog)
        _, out = call(fio, dio.get_sub_page, 2, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(out, '')

    # BUG: get_sub_page sets self.mx_sb_len instead of io_hdr.mx_sb_len, so
    # the sg driver is told the sense buffer is 0 bytes long and never
    # returns sense data on CHECK CONDITION.
    @unittest.expectedFailure
    def test_sense_buffer_length_passed_to_driver(self):
        fio = FakeIoctl(data=b'ok')
        dio = open_dev(debugflags=0)
        call(fio, dio.get_sub_page, 2, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(fio.calls[0]['mx_sb_len'], 64)

    def test_sense_buffer_length_currently_zero(self):
        fio = FakeIoctl(data=b'ok')
        dio = open_dev(debugflags=0)
        call(fio, dio.get_sub_page, 2, MODE_SENSE_3A_01, 0, 0)
        self.assertEqual(fio.calls[0]['mx_sb_len'], 0)
        self.assertEqual(dio.mx_sb_len, 64)


# --- put_sub_page ---------------------------------------------------------------

SET_TIME_CDB = struct.pack('>BBBBBBBBBB', 0xea, 0x10, 0x80, 0, 0, 0, 0, 0, 8, 0)


class TestPutSubPage(unittest.TestCase):

    def test_good_write(self):
        payload = b'\x01\x02\x00\x03payload!'
        fio = FakeIoctl()
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.put_sub_page, SET_TIME_CDB, payload, 0)
        self.assertEqual(result, len(payload))
        self.assertEqual(out, '')
        c = fio.calls[0]
        self.assertEqual(c['request'], sg_io_hdr.SG_IO)
        self.assertEqual(c['cdb'], SET_TIME_CDB)
        self.assertEqual(c['payload'], payload)
        self.assertEqual(c['direction'], sg_io_hdr.SG_DXFER_TO_DEV)
        self.assertEqual(c['dxfer_len'], len(payload))
        self.assertEqual(c['mx_sb_len'], 32)

    def test_resid_subtracted(self):
        fio = FakeIoctl(resid=4)
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.put_sub_page, SET_TIME_CDB, b'12345678', 0)
        self.assertEqual(result, 4)

    def test_negative_resid_ignored(self):
        fio = FakeIoctl(resid=-4)
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.put_sub_page, SET_TIME_CDB, b'12345678', 0)
        self.assertEqual(result, 8)

    def test_check_condition_counts_as_written(self):
        fio = FakeIoctl(status=sg_io_hdr.SAM_STAT_CHECK_CONDITION)
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.put_sub_page, SET_TIME_CDB, b'1234', 0)
        self.assertEqual(result, 4)
        self.assertEqual(out, '')

    def test_ioctl_error_returns_none(self):
        fio = FakeIoctl(result=-1)
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.put_sub_page, SET_TIME_CDB, b'1234', 0)
        self.assertIsNone(result)
        self.assertEqual(out, ' get_mode_page SG_IO ioctl error\n')

    def test_bad_status_returns_none(self):
        fio = FakeIoctl(status=0x08)  # BUSY
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.put_sub_page, SET_TIME_CDB, b'1234', 0)
        self.assertIsNone(result)
        self.assertEqual(out, 'oh no! io_hdr status is: 8\n\n')

    def test_debug_argument_ignored(self):
        fio = FakeIoctl()
        dio = open_dev(debugflags=0)
        _, out = call(fio, dio.put_sub_page, SET_TIME_CDB, b'1234', ALL_DEBUG)
        self.assertEqual(out, '')

    def test_debug_hw_dialog_traces(self):
        fio = FakeIoctl(status=0, resid=1, sb_len_wr=0, driver_status=0,
                        host_status=0)
        dio = open_dev(debugflags=ALL_DEBUG)
        result, out = call(fio, dio.put_sub_page, SET_TIME_CDB, b'1234', 0)
        self.assertEqual(result, 3)
        self.assertEqual(out, 'put_sub_page, 5 after ioctl, result= 0\n'
                         'status:  0\n'
                         'driver_status:  0\n'
                         'host_status:  0\n'
                         'sb_len_wr:  0\n'
                         'resid:  1\n')

    def test_debug_traces_before_error(self):
        fio = FakeIoctl(result=-5)
        dio = open_dev(debugflags=Drobo.DBG_HWDialog)
        result, out = call(fio, dio.put_sub_page, SET_TIME_CDB, b'1234', 0)
        self.assertIsNone(result)
        self.assertTrue(out.startswith('put_sub_page, 5 after ioctl, result= -5\n'))
        self.assertTrue(out.endswith(' get_mode_page SG_IO ioctl error\n'))


# --- identifyLUN ----------------------------------------------------------------

def inquiry(vendor, product):
    return b'\0' * 8 + vendor.ljust(8) + product.ljust(16) + b'1.00'


def kernel_idlun(host, channel, id, lun, unique=0):
    """ struct scsi_idlun as the Linux kernel fills it (native byte order). """
    dev_id = (id & 0xff) | ((lun & 0xff) << 8) | ((channel & 0xff) << 16) | \
        ((host & 0xff) << 24)
    return struct.pack('=II', dev_id, unique)


class TestIdentifyLUN(unittest.TestCase):

    def test_drobo_identity(self):
        fio = FakeIoctl(idlun=bytes([1, 2, 3, 4]) + struct.pack('>l', 99),
                        inquiry=inquiry(b'TRUSTED', b'Mass Storage'))
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.identifyLUN)
        # the idlun buffer is unpacked as channel, lun, id, host.
        self.assertEqual(result, (4, 1, 3, 2, 'TRUSTED ', 'Mass Storage    '))
        self.assertEqual(out, '')
        self.assertEqual(fio.calls[0]['request'], SCSI_IOCTL_GET_IDLUN)
        self.assertTrue(fio.calls[0]['mutate'])
        self.assertEqual(fio.calls[1]['request'], sg_io_hdr.SG_IO)
        self.assertEqual(fio.calls[1]['cdb'], bytes([0x12, 0, 0, 0, 32, 0]))
        self.assertEqual(fio.calls[1]['dxfer_len'], 32)
        self.assertEqual(fio.calls[1]['direction'], sg_io_hdr.SG_DXFER_FROM_DEV)

    def test_usb3_drobo_s(self):
        fio = FakeIoctl(idlun=bytes(8),
                        inquiry=inquiry(b'USB 3.0', b'Drobo S - Gen 2'))
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.identifyLUN)
        self.assertEqual(result[4:], ('USB 3.0 ', 'Drobo S - Gen 2 '))

    # BUG: the kernel's scsi_idlun.dev_id is a native-endian u32 holding
    # id | lun << 8 | channel << 16 | host << 24; on little endian machines
    # identifyLUN unpacks those bytes as (channel, lun, id, host), so the
    # channel and id it returns are swapped.  (Harmless for drobolunlist,
    # which only compares the three together.)
    @unittest.expectedFailure
    def test_channel_and_id_from_kernel_layout(self):
        if struct.pack('=I', 1)[0] != 1:
            self.skipTest('little endian only')
        fio = FakeIoctl(idlun=kernel_idlun(host=3, channel=1, id=2, lun=0),
                        inquiry=inquiry(b'Drobo', b'5D'))
        dio = open_dev(debugflags=0)
        result, _ = call(fio, dio.identifyLUN)
        self.assertEqual(result[:4], (3, 1, 2, 0))

    def test_idlun_ioctl_error_returns_none(self):
        fio = FakeIoctl(result=-1)
        dio = open_dev(debugflags=0)
        result, out = call(fio, dio.identifyLUN)
        self.assertIsNone(result)
        self.assertEqual(out, 'Drobo get_mode_page SG_IO ioctl error\n')
        self.assertEqual(len(fio.calls), 1)

    def test_inquiry_failure_raises(self):
        fio = FakeIoctl(idlun=bytes(8), status=0x02)
        dio = open_dev(debugflags=0)
        call_raises(self, IOError, fio, dio.identifyLUN)

    def test_short_inquiry_raises(self):
        # a device returning fewer than 32 bytes (resid) cannot be unpacked.
        fio = FakeIoctl(idlun=bytes(8), inquiry=inquiry(b'Drobo', b'5D'),
                        resid=10)
        dio = open_dev(debugflags=0)
        call_raises(self, struct.error, fio, dio.identifyLUN)

    def test_debug_traces_inquiry(self):
        fio = FakeIoctl(idlun=bytes(8), inquiry=inquiry(b'Drobo', b'5D'))
        dio = open_dev(debugflags=ALL_DEBUG)
        result, out = call(fio, dio.identifyLUN)
        self.assertEqual(result[4], 'Drobo   ')
        self.assertIn('mcb 000:\n12 00 00 00 20 00 \n', out)
        self.assertIn('the length is:  32\n', out)


# --- drobolunlist ---------------------------------------------------------------

class TestDrobolunlistDebug(unittest.TestCase):
    """ the DBG_Detection trace of drobolunlist, including failure paths. """

    def run_discover(self, devices, debugflags=Drobo.DBG_Detection):
        out = io.StringIO()
        with redirect_stdout(out):
            found = discover(devices, debugflags=debugflags)
        return found, out.getvalue()

    def test_construct_failure_traced(self):
        found, out = self.run_discover({'sdb': OSError('nope')})
        self.assertEqual(found, [])
        self.assertEqual(out, 'examining:  /dev/sdb\n'
                         'rejected: failed to construct LUN pdio\n'
                         'returning list:  []\n')

    def test_identify_failure_traced_and_closed(self):
        dev = fake('Drobo', '5D')
        dev.identifyLUN = mock.Mock(side_effect=IOError('inquiry failed'))
        dev.closefd = mock.Mock()
        found, out = self.run_discover({'sdb': dev})
        self.assertEqual(found, [])
        self.assertIn('rejected: failed to identify LUN\n', out)
        dev.closefd.assert_called_once_with()

    def test_identify_failure_quiet_without_debug(self):
        dev = fake('Drobo', '5D')
        dev.identifyLUN = mock.Mock(side_effect=IOError('inquiry failed'))
        found, out = self.run_discover({'sdb': dev, 'sdc': OSError('x')},
                                       debugflags=0)
        self.assertEqual(found, [])
        self.assertEqual(out, '')

    def test_identify_returning_none_raises(self):
        # identifyLUN returns None when SCSI_IOCTL_GET_IDLUN fails; that is
        # not caught by drobolunlist.
        dev = fake('Drobo', '5D')
        dev.identifyLUN = mock.Mock(return_value=None)
        with self.assertRaises(TypeError):
            discover({'sdb': dev})

    def test_multi_lun_traced(self):
        found, out = self.run_discover({'sdb': fake('Drobo', '5D'),
                                        'sdc': fake('Drobo', '5D')})
        self.assertEqual(found, [['/dev/sdb', '/dev/sdc']])
        self.assertIn('found a Drobo: Drobo 5D\n', out)
        self.assertIn('appending to lundevs...\n', out)
        self.assertIn("returning list:  [['/dev/sdb', '/dev/sdc']]\n", out)

    def test_non_drobo_traced(self):
        found, out = self.run_discover({'sdb': fake('ATA', 'Samsung SSD')})
        self.assertEqual(found, [])
        self.assertIn('rejected: vendor is ATA      (not from DRI)\n', out)

    def test_ignores_partitions_and_other_names(self):
        found = discover({'sda1': OSError('partition'), 'sg0': OSError('sg'),
                          'sdaa': OSError('4 letters'),
                          'sdb': fake('Drobo', '5D')})
        self.assertEqual(found, [['/dev/sdb']])

    def test_unsorted_listing_is_sorted(self):
        names = ['sdc', 'sdb']
        devs = {'sdb': fake('Drobo', '5D', idlun=(1, 0, 0, 0)),
                'sdc': fake('Drobo', '5D', idlun=(2, 0, 0, 0))}
        with mock.patch.object(DroboIOctl.os, 'listdir', lambda d: list(names)), \
             mock.patch.object(DroboIOctl, 'DroboIOctl',
                               lambda p, *a, **k: devs[os.path.basename(p)]):
            found = DroboIOctl.drobolunlist(0, 'Drobo')
        self.assertEqual(found, [['/dev/sdb'], ['/dev/sdc']])

    def test_real_class_through_fake_ioctl(self):
        """ drobolunlist with the real DroboIOctl class, faked at ioctl. """
        fio = FakeIoctl(idlun=bytes(8), inquiry=inquiry(b'Drobo', b'5D'))
        with mock.patch.object(DroboIOctl.os, 'listdir', lambda d: ['sdb']), \
             mock.patch.object(DroboIOctl, 'open', fake_open, create=True), \
             mock.patch.object(DroboIOctl, 'ioctl', fio), \
             redirect_stdout(io.StringIO()):
            found = DroboIOctl.drobolunlist(0, 'Drobo')
        self.assertEqual(found, [['/dev/sdb']])
        self.assertEqual([c['request'] for c in fio.calls],
                         [SCSI_IOCTL_GET_IDLUN, sg_io_hdr.SG_IO])


# --- the module's own self test (__main__) ------------------------------------

class TestMainBlock(unittest.TestCase):
    """ python3 DroboIOctl.py, with open/ioctl/listdir faked. """

    def test_self_test_runs(self):
        opened = []

        def recording_open(path, mode='r', *a, **k):
            f = fake_open(path, mode)
            opened.append(f)
            return f

        page = struct.pack('>BBHBBBQBHH', 0x7a, 1, 20, 4, 16, 1,
                           1099511557632, 0, 0, 0)
        fio = FakeIoctl(version=struct.pack('l', 30536), idlun=bytes(8),
                        data=page, inquiry=inquiry(b'TRUSTED', b'Mass Storage'))
        out = io.StringIO()
        with mock.patch.object(fcntl, 'ioctl', fio), \
             mock.patch.object(os, 'listdir', lambda d: []), \
             redirect_stdout(out):
            runpy.run_path(os.path.join(TOP, 'DroboIOctl.py'),
                           init_globals={'open': recording_open},
                           run_name='__main__')
        text = out.getvalue()
        self.assertEqual([f.path for f in opened], ['/dev/sdg'])
        self.assertTrue(opened[0].closed)
        self.assertIn('version 30536\n', text)
        self.assertIn("identifyLUN (0, 0, 0, 0, 'TRUSTED ', 'Mass Storage    ')",
                      text)
        self.assertIn('doing a sub_page\n20\n', text)
        self.assertIn('(122, 1, 20, 4, 16, 1, 1099511557632, 0, 0, 0)', text)
        self.assertTrue(text.endswith('hunt...\n[]\n'))


if __name__ == '__main__':
    unittest.main()
