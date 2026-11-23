"""
Coverage backfill for Drobo.py: firmware, format, mount, discovery and the
debug trace branches.

These are characterisation tests: they pin what the code does today.  Where
today's behaviour is a bug, the test that describes the correct behaviour is
marked unittest.expectedFailure, with a comment, and the bug is left alone
(to be fixed in its own pull request, test first.)

Safety:
  - no network: urllib.request.urlopen is replaced for the whole module by a
    stub that fails the test, and tests that need it install their own fake.
  - nothing runs parted, mkfs, mount or umount: os.system and subprocess are
    replaced for the whole module by stubs that fail the test, and tests
    that need os.system install a recording fake.
  - nothing is written outside temporary directories: format_script's fixed
    /tmp/fmtscript and DiscoverMounts' /etc/mtab are redirected into a
    temporary directory, and the firmware repository is pointed at one.
  - writeFirmware only ever talks to a FakeDevice.

Run from the top of the source tree::

    python3 -m unittest discover -s tests -v

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import builtins
import contextlib
import io
import os
import struct
import subprocess
import tempfile
import unittest
import urllib.request
import zipfile
import zlib
from unittest import mock

from test_regression import FakeDevice, make_drobo, legacy_subpage, \
    gen2_responder, RelayoutDevice

import Drobo
import DroboIOctl

_STATE = None
_GUARDS = []


def _forbidden(what):

    def fail(*a, **k):
        raise AssertionError('%s called during tests: %r %r' % (what, a, k))

    return fail


def setUpModule():
    """ no saved state in the real home directory, no network, no commands. """
    global _STATE
    _STATE = tempfile.TemporaryDirectory()
    os.environ['DROBO_UTILS_STATE_DIR'] = _STATE.name
    for target, name in ((urllib.request, 'urlopen'), (os, 'system'),
                         (subprocess, 'call'), (subprocess, 'run'),
                         (subprocess, 'Popen'), (subprocess, 'check_call'),
                         (subprocess, 'check_output')):
        p = mock.patch.object(target, name, _forbidden(name))
        p.start()
        _GUARDS.append(p)


def tearDownModule():
    while _GUARDS:
        _GUARDS.pop().stop()
    os.environ.pop('DROBO_UTILS_STATE_DIR', None)
    _STATE.cleanup()


# --- helpers ----------------------------------------------------------------

FWHDR = '>ll4sl16slllll256sl'  # the header layout validateFirmware unpacks
FWHDR_LEN = struct.calcsize(FWHDR)  # 312


def _signed(crc):
    return struct.unpack('>l', struct.pack('>L', crc & 0xffffffff))[0]


def make_firmware(body=b'firmware body ' * 10, magic=b'TDIH', extra=b'',
                  hdrcrc_delta=0, bodycrc_delta=0, length_delta=0):
    """ a firmware image (.tdf contents) with a header validateFirmware
        accepts, apart from the deliberate damage asked for. """
    hdrlen = FWHDR_LEN + len(extra)
    bodycrc = zlib.crc32(body) + bodycrc_delta
    hdr = struct.pack(FWHDR, hdrlen, 1, magic, 0x01020304, b'drobo', 1, 0, 0,
                      len(body) + length_delta, _signed(bodycrc),
                      b'about this firmware', 0) + extra
    hdrcrc = zlib.crc32(hdr[0:308] + struct.pack('i', 0) + hdr[312:hdrlen])
    hdr = hdr[:308] + struct.pack('>l', _signed(hdrcrc + hdrcrc_delta)) + \
        hdr[312:]
    return hdr + body


class WritingDevice(FakeDevice):
    """ a FakeDevice that also accepts writes (put_sub_page), recording each
        (CDB, payload), and answers the firmware write status query. """

    def __init__(self, short_after=None, status=0):
        super().__init__()
        self.writes = []
        self.short_after = short_after
        self.status = status
        self.ea_responder = self.respond_ea

    def put_sub_page(self, mcb, buffer, DEBUG):
        self.writes.append((bytes(mcb), bytes(buffer)))
        if self.short_after is not None and \
           len(self.writes) > self.short_after:
            return len(buffer) - 1
        return len(buffer)

    def respond_ea(self, mcb, sz, out):
        return bytes([self.status])[:sz]


class CovTestCase(unittest.TestCase):
    """ resets the module wide debug flags after every test. """

    def setUp(self):
        # Drobo objects made with debug flags are kept until the flags are
        # reset, so __del__ does not print its trace into the test output.
        self.objs = []
        self.addCleanup(self.objs.clear)
        self.addCleanup(setattr, Drobo, 'DEBUG', 0)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def debug(self, flags):
        Drobo.DEBUG = flags

    @contextlib.contextmanager
    def stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            yield out

    def redirect_open(self, mapping):
        """ make open() inside Drobo.py use temp files for the given fixed
            paths.  Any other path must already be inside the temp dir. """
        real_open = builtins.open

        def fake_open(path, *a, **k):
            path = mapping.get(path, path)
            if not os.path.abspath(path).startswith(self.tmp.name + os.sep):
                raise AssertionError('open outside temp dir: %s' % path)
            return real_open(path, *a, **k)

        return mock.patch.object(Drobo, 'open', fake_open, create=True)


# --- firmware -----------------------------------------------------------------


class TestFirmwareRepository(CovTestCase):

    def test_local_firmware_repository(self):
        d = make_drobo(FakeDevice())
        self.assertEqual(d.localFirmwareRepository(),
                         os.path.expanduser('~') + '/.drobo-utils')

    def test_local_firmware_repository_follows_class_attribute(self):
        d = make_drobo(FakeDevice())
        with mock.patch.object(Drobo.Drobo, 'localfwrepository',
                               self.tmp.name):
            self.assertEqual(d.localFirmwareRepository(), self.tmp.name)


class TestValidateFirmware(CovTestCase):

    def setUp(self):
        super().setUp()
        self.d = make_drobo(FakeDevice())

    def validate(self, data, flags=0):
        self.d.fwdata = data
        self.debug(flags)
        with self.stdout() as out:
            good = self.d.validateFirmware()
        return good, out.getvalue()

    def test_length_mismatch(self):
        good, out = self.validate(make_firmware(length_delta=1))
        self.assertEqual(good, 0)
        self.assertIn('header corrupt... Length does not validate.', out)

    def test_bad_magic(self):
        good, out = self.validate(make_firmware(magic=b'XXXX'))
        self.assertEqual(good, 0)
        self.assertIn('bad Magic, not a valid firmware', out)

    def test_header_is_unpacked(self):
        fw = make_firmware(extra=b'x' * 8)
        self.validate(fw)
        self.assertEqual(self.d.fwhdr[0], FWHDR_LEN + 8)
        self.assertEqual(self.d.fwhdr[8], len(fw) - FWHDR_LEN - 8)
        self.assertEqual(self.d.fwhdr[2], b'TDIH')

    # struct.unpack returns the magic as bytes (b'TDIH'); it used to be
    # compared with the text 'TDIH', so every image was rejected.
    def test_good_image_validates(self):
        good, out = self.validate(make_firmware())
        self.assertNotIn('bad Magic', out)
        self.assertEqual(good, 1)

    def test_good_image_traces_every_check(self):
        good, out = self.validate(make_firmware(extra=b'more header'),
                                  Drobo.DBG_Chatty)
        self.assertEqual(good, 1)
        for line in ('validateFirmware start...',
                     'header+body lengths validated.  Good.',
                     'Magic number validated. Good.', 'CRC from header: ',
                     'CRC for body from header: ',
                     '32 bit Cyclic Redundancy Check correct. Good.',
                     'validateFirmware successful...'):
            self.assertIn(line, out)

    def test_header_crc_wrong(self):
        good, out = self.validate(make_firmware(hdrcrc_delta=1))
        self.assertEqual(good, 0)
        self.assertIn('file corrupt, header checksum wrong', out)

    def test_body_crc_wrong(self):
        good, out = self.validate(make_firmware(bodycrc_delta=1))
        self.assertEqual(good, 0)
        self.assertIn('file corrupt, payload checksum wrong', out)


class TestPickFirmware(CovTestCase):
    """ the FakeDevice INQUIRY says hardware revision b'1.00'. """

    def setUp(self):
        super().setUp()
        self.d = make_drobo(FakeDevice())
        self.fw1 = make_firmware(body=b'rev one body')
        self.fw2 = make_firmware(body=b'rev two body')
        self.tdz = self.path('Drobo_1.2.4.tdz')
        with zipfile.ZipFile(self.tdz, 'w') as z:
            z.writestr('Drobo_v1.2.4_1.tdf', self.fw1)
            z.writestr('Drobo_v1.2.4_2.tdf', self.fw2)

    def test_zipped_traces_each_member_and_validates(self):
        self.debug(Drobo.DBG_Chatty | Drobo.DBG_General)
        with mock.patch.object(self.d, 'validateFirmware',
                               return_value=1) as v, self.stdout() as out:
            self.assertEqual(self.d.PickFirmware(self.tdz), 1)
        v.assert_called_once_with()
        out = out.getvalue()
        self.assertIn('Reading Firmware from = %s' % self.tdz, out)
        self.assertIn('Drobo_v1.2.4_1.tdf  ? ', out)
        self.assertIn('Drobo_v1.2.4_2.tdf  ? ', out)
        self.assertIn('firmware for hw rev  1  this drobo is rev  1\n', out)
        self.assertIn('firmware for hw rev  2  this drobo is rev  1\n', out)

    def test_zipped_result_is_validate_result(self):
        with mock.patch.object(self.d, 'validateFirmware', return_value=0):
            self.assertEqual(self.d.PickFirmware(self.tdz), 0)

    # the hardware revision from INQUIRY is bytes: hwlevel[0] used to be an
    # int (0x31) that never equalled the member name's character ('1'), so
    # no member was ever picked and self.fwdata was not set.
    def test_zipped_picks_member_for_hw_revision(self):
        with mock.patch.object(self.d, 'validateFirmware', return_value=1):
            self.d.PickFirmware(self.tdz)
        self.assertEqual(getattr(self.d, 'fwdata', None), self.fw1)

    def test_plain_file_read_and_validated(self):
        name = self.path('Drobo_1.2.4.tdf')
        with open(name, 'wb') as f:
            f.write(b'plain\r\nfirmware\r')
        with mock.patch.object(self.d, 'validateFirmware', return_value=1):
            self.assertEqual(self.d.PickFirmware(name), 1)
        # read in binary mode: no newline translation, bytes not str.
        self.assertEqual(self.d.fwdata, b'plain\r\nfirmware\r')

    # a plain .tdf used to be opened in text mode, so binary firmware was
    # decoded as text (or failed to decode) and fwdata was a str, which
    # validateFirmware cannot unpack.
    def test_plain_file_read_as_bytes(self):
        name = self.path('Drobo_1.2.4.tdf')
        fw = make_firmware()
        with open(name, 'wb') as f:
            f.write(fw)
        with mock.patch.object(self.d, 'validateFirmware', return_value=1):
            self.d.PickFirmware(name)
        self.assertEqual(self.d.fwdata, fw)


INDEX = ('Release /drobo/firmware/armmarvell/1.100.1 |Image /fw/old.tdz|'
         'Release /drobo/docs/armmarvell/1.201.12942 |Image /fw/doc.txt|'
         'Release /drobo/firmware/armother/1.201.12942 |Image /fw/other.tdz|'
         'Release /drobo/firmware/armmarvell/1.201.12942 |'
         'Image /fw/v1/Drobo_1.2.4.tdz|')


class TestPickLatestFirmware(CovTestCase):
    """ the fixture Drobo runs ArmMarvell 1.201.12942, hw revision 1.00 """

    def setUp(self):
        super().setUp()
        self.d = make_drobo(FakeDevice())

    def pick(self, index, flags=0):
        urlopen = mock.Mock(return_value=io.BytesIO(index.encode()))
        self.debug(flags)
        with mock.patch.object(urllib.request, 'urlopen', urlopen), \
             self.stdout() as out:
            r = self.d.PickLatestFirmware()
        self.urlopen = urlopen
        return r, out.getvalue()

    def test_match(self):
        r, out = self.pick(INDEX, Drobo.DBG_Chatty)
        self.assertEqual(r, ('armmarvell', '1.201.12942', b'1.00',
                             '/fw/v1/Drobo_1.2.4.tdz'))
        self.urlopen.assert_called_once_with(
            'ftp://updates.drobo.com/index.txt')
        self.assertIn('looking for firmware for: armmarvell 1.201.12942', out)
        self.assertIn('This Drobo should be running:  /fw/v1/Drobo_1.2.4.tdz',
                      out)

    def test_licensed_path(self):
        r, out = self.pick('Release /drobo/licensed/firmware/armmarvell/'
                           '1.201.12942 |Image /fw/lic.tdz|')
        self.assertEqual(r[3], '/fw/lic.tdz')

    def test_version_in_brackets(self):
        r, out = self.pick('Release /drobo/firmware/armmarvell/v1 '
                           '[1.201.12942]|Image /fw/br.tdz|')
        self.assertEqual(r[3], '/fw/br.tdz')

    def test_no_match(self):
        r, out = self.pick(INDEX.rsplit('Release', 1)[0], Drobo.DBG_Chatty)
        self.assertEqual(r, ('', '', '', ''))
        self.assertIn('no matching firmware found', out)

    def test_simulation(self):
        self.debug(Drobo.DBG_Simulation)
        self.assertEqual(self.d.PickLatestFirmware(),
                         ('fwarch', 'fwv', 'hwlevel', 'value'))


class TestDownloadFirmware(CovTestCase):

    def setUp(self):
        super().setUp()
        self.d = make_drobo(FakeDevice())
        self.fw = make_firmware()

    def urlopen(self):
        self.fake = mock.Mock(return_value=io.BytesIO(self.fw))
        return mock.patch.object(urllib.request, 'urlopen', self.fake)

    def temp_writes_only(self):
        """ open() inside Drobo.py may only touch the temp dir. """
        return self.redirect_open({})

    # the downloaded bytes used to be written to a file opened in text mode
    # ('w+'), which raised TypeError, so no firmware could be downloaded.
    def test_download_plain(self):
        local = self.path('local.tdf')
        with self.urlopen(), self.temp_writes_only():
            self.assertEqual(self.d.downloadFirmware('fw/x.tdf', local),
                             self.fw)

    def test_download_plain_traced_and_written(self):
        local = self.path('local.tdf')
        self.debug(Drobo.DBG_Chatty)
        with self.urlopen(), self.temp_writes_only(), self.stdout() as out:
            data = self.d.downloadFirmware('fw/x.tdf', local)
        self.assertEqual(data, self.fw)
        self.assertEqual(self.d.fwdata, self.fw)
        self.fake.assert_called_once_with('ftp://updates.drobo.com/fw/x.tdf')
        with open(local, 'rb') as f:
            self.assertEqual(f.read(), self.fw)
        out = out.getvalue()
        for line in ('downloading firmware  fw/x.tdf ...',
                     'local copy written', 'downloading done '):
            self.assertIn(line, out)

    def test_download_zipped(self):
        """ a .tdz is handed to PickFirmware, which sets fwdata. """
        local = self.path('local.tdz')

        def pick(name):
            self.d.fwdata = b'picked'
            return 1

        with self.urlopen(), self.temp_writes_only(), \
             mock.patch.object(self.d, 'PickFirmware', side_effect=pick) as p:
            self.assertEqual(self.d.downloadFirmware('fw/x.tdz', local),
                             b'picked')
        p.assert_called_once_with(local)


class TestUpdateFirmwareRepository(CovTestCase):

    LATEST = ('armmarvell', '1.201.12942', b'1.00', '/fw/v1/Drobo_1.2.4.tdz')

    def setUp(self):
        super().setUp()
        self.d = make_drobo(FakeDevice())
        self.repo = self.path('repo')
        self.local = self.repo + '/armmarvell_1.00_Drobo_1.2.4.tdz'
        p = mock.patch.object(Drobo.Drobo, 'localfwrepository', self.repo)
        p.start()
        self.addCleanup(p.stop)

    def run_update(self, latest=LATEST, download=None, valid=1, picked=1):
        self.debug(Drobo.DBG_Chatty)
        self.download = mock.Mock(return_value=download or b'fw')
        self.validate = mock.Mock(return_value=valid)
        self.pickfw = mock.Mock(return_value=picked)
        with mock.patch.object(self.d, 'PickLatestFirmware',
                               return_value=latest), \
             mock.patch.object(self.d, 'downloadFirmware', self.download), \
             mock.patch.object(self.d, 'validateFirmware', self.validate), \
             mock.patch.object(self.d, 'PickFirmware', self.pickfw), \
             self.stdout() as out:
            r = self.d.updateFirmwareRepository()
        return r, out.getvalue()

    def test_already_latest(self):
        r, out = self.run_update(latest=('', '', '', ''))
        self.assertEqual(r, 0)
        self.assertFalse(os.path.exists(self.repo))
        self.download.assert_not_called()

    def test_downloads_when_missing(self):
        r, out = self.run_update()
        self.assertEqual(r, 1)
        self.assertTrue(os.path.isdir(self.repo))
        self.download.assert_called_once_with('/fw/v1/Drobo_1.2.4.tdz',
                                              self.local)
        self.validate.assert_called_once_with()
        self.assertEqual(self.d.fwdata, b'fw')
        self.assertIn('looking for: %s' % self.local, out)
        self.assertIn('not present, fetching from drobo.com', out)
        self.assertIn('correct fw available', out)

    def test_download_does_not_validate(self):
        r, out = self.run_update(valid=0)
        self.assertEqual(r, 0)
        self.assertIn('downloaded firmware did not validate.', out)

    def test_local_copy_used(self):
        os.mkdir(self.repo)
        open(self.local, 'wb').close()
        r, out = self.run_update()
        self.assertEqual(r, 1)
        self.download.assert_not_called()
        self.pickfw.assert_called_once_with(self.local)
        self.assertIn('local copy already present: %s' % self.local, out)

    def test_local_copy_invalid(self):
        os.mkdir(self.repo)
        open(self.local, 'wb').close()
        r, out = self.run_update(picked=0)
        self.assertEqual(r, 0)
        self.assertIn('no valid firmware found', out)


class TestWriteFirmware(CovTestCase):
    """ writeFirmware, only ever against a FakeDevice: assert what it would
        send to a unit. """

    TXID = 7

    def write(self, body_len, flags=0, **devargs):
        self.dev = WritingDevice(**devargs)
        d = make_drobo(self.dev)
        d.transactionID = self.TXID
        self.fw = make_firmware(body=bytes(range(256)) * (body_len // 256) +
                                b'z' * (body_len % 256))
        d.fwdata = self.fw
        d.fwhdr = struct.unpack(FWHDR, self.fw[:FWHDR_LEN])
        self.dev.cdbs.clear()
        self.progress = []
        self.debug(flags)
        with self.stdout() as out:
            d.writeFirmware(self.progress.append)
        self.d = d
        return out.getvalue()

    def cdb(self, txid, flags, length, op=0x70, b2=0x00):
        return struct.pack('>BBBBBBBHB', 0xea, 0x10, b2, op, 0x00, txid, flags,
                           length, 0x00)

    def test_pages_sent(self):
        out = self.write(70000)
        total = len(self.fw)
        w = self.dev.writes
        # page 0: total length, then the header.
        self.assertEqual(w[0], (self.cdb(self.TXID, 0x21, 4 + FWHDR_LEN),
                                struct.pack('>L', total) +
                                self.fw[:FWHDR_LEN]))
        # then the body, 32 KiB at a time, the last one flagged as last.
        body = self.fw[FWHDR_LEN:]
        self.assertEqual(w[1:], [
            (self.cdb(self.TXID, 1, 32768), body[:32768]),
            (self.cdb(self.TXID, 1, 32768), body[32768:65536]),
            (self.cdb(self.TXID, 0, 70000 - 65536), body[65536:]),
        ])
        self.assertEqual(out, '')

    def test_progress_callback(self):
        self.write(70000)
        total = len(self.fw)
        self.assertEqual(self.progress, [
            (FWHDR_LEN + 32768) * 100 / total,
            (FWHDR_LEN + 65536) * 100 / total, 100.0
        ])

    def test_status_query_after_write(self):
        self.write(1000)
        self.assertEqual(self.dev.cdbs, [
            struct.pack('>BBBBBBBHB', 0xea, 0x10, 0x80, 0x71, 0,
                        self.TXID + 1, 0x20, 1, 0)
        ])
        self.assertEqual(self.d.transactionID, self.TXID + 1)

    def test_body_exact_multiple_of_block(self):
        """ current behaviour: when the body is an exact multiple of 32 KiB
            the last block is not flagged as last (more to come = 1). """
        self.write(65536)
        self.assertEqual([c[6] for c, b in self.dev.writes[1:]], [1, 1])
        self.assertEqual([len(b) for c, b in self.dev.writes[1:]],
                         [32768, 32768])

    def test_short_write_stops(self):
        """ a block the device does not take in full ends the loop. """
        self.write(100000, short_after=2)
        self.assertEqual(len(self.dev.writes), 3)
        self.assertEqual(len(self.progress), 2)

    def test_debug_trace(self):
        out = self.write(1000, flags=Drobo.DBG_General, status=3)
        for line in ('Page 0...', 'writeFirmware: i=312, start=312, last=%d' %
                     len(self.fw), 'wrote  1000  bytes.  Cumulative:  %d  of'
                     % len(self.fw), 'writeFirmware Done.  i=%d, len=%d' %
                     (len(self.fw), len(self.fw)),
                     'Drobo thinks write status is:  3'):
            self.assertIn(line, out)


# --- format script, mounts --------------------------------------------------


class FormatMountBase(CovTestCase):

    def setUp(self):
        super().setUp()
        self.script = self.path('fmtscript')
        self.mtab = self.path('mtab')
        p = self.redirect_open({'/tmp/fmtscript': self.script,
                                '/etc/mtab': self.mtab})
        p.start()
        self.addCleanup(p.stop)
        self.chmod = mock.Mock()
        p = mock.patch.object(Drobo.os, 'chmod', self.chmod)
        p.start()
        self.addCleanup(p.stop)
        self.system = mock.Mock(return_value=0)
        p = mock.patch.object(Drobo.os, 'system', self.system)
        p.start()
        self.addCleanup(p.stop)

    def drobo(self, chardevs='/dev/sdq'):
        with mock.patch.object(DroboIOctl, 'DroboIOctl',
                               lambda *a, **k: FakeDevice()):
            return Drobo.Drobo(chardevs, 0)


class TestFormatScript(FormatMountBase):

    def fmt(self, fstype, chardevs='/dev/sdq'):
        d = self.drobo(chardevs)
        with self.stdout() as out:
            r = d.format_script(fstype)
        self.assertEqual(r, '/tmp/fmtscript')
        self.chmod.assert_called_once_with('/tmp/fmtscript', 0o700)
        self.system.assert_not_called()
        with open(self.script) as f:
            return f.read(), out.getvalue()

    def test_ext3_default(self):
        d = self.drobo()
        self.assertEqual(d.format_script(), '/tmp/fmtscript')
        with open(self.script) as f:
            self.assertEqual(f.read(), self.EXT3)

    EXT3 = ('#!/bin/sh\n'
            'parted -s /dev/sdq mklabel gpt\n'
            'parted -s /dev/sdq mkpart primary ext3 0 100%\n'
            'parted -s /dev/sdq print; sleep 5\n'
            'mke2fs -j -i 262144 -L Drobo01 -m 0 '
            '-O sparse_super,^resize_inode /dev/sdq1\n')

    def test_ext3(self):
        self.assertEqual(self.fmt('ext3')[0], self.EXT3)

    def test_ntfs(self):
        script, out = self.fmt('ntfs')
        self.assertEqual(script, (
            '#!/bin/sh\n'
            'parted -s /dev/sdq mklabel gpt\n'
            'parted -s /dev/sdq mkpart primary ntfs 0 100%\n'
            'parted -s /dev/sdq print; sleep 5\n'
            'mkntfs -f -L Drobo01  /dev/sdq1\n'))

    FAT32 = ('#!/bin/sh\n'
             'parted -s /dev/sdq mklabel msdos\n'
             'parted -s /dev/sdq mkpart primary fat32 0 100%\n'
             'parted -s /dev/sdq print; sleep 5\n'
             'mkdosfs -v -v -F 32 -S 4096 -n Drobo01 /dev/sdq1\n')

    def test_fat32(self):
        self.assertEqual(self.fmt('FAT32')[0], self.FAT32)

    def test_msdos(self):
        self.assertEqual(self.fmt('msdos')[0], self.FAT32)

    def test_unsupported(self):
        script, out = self.fmt('hfs+')
        self.assertEqual(script,
                         '#!/bin/sh\nparted -s /dev/sdq mklabel gpt\n')
        self.assertIn('unsupported  partition type hfs+, sorry...', out)

    def test_every_lun(self):
        script, out = self.fmt('ntfs', ['/dev/sdq', '/dev/sdr'])
        lines = script.splitlines()
        self.assertEqual(len(lines), 9)
        self.assertEqual(lines[1], 'parted -s /dev/sdq mklabel gpt')
        self.assertEqual(lines[5], 'parted -s /dev/sdr mklabel gpt')
        self.assertEqual(lines[8], 'mkntfs -f -L Drobo01  /dev/sdr1')


MTAB = ('/dev/sda1 / ext4 rw 0 0\n'
        '/dev/sdq1 /media/drobo ext3 rw 0 0\n'
        'proc /proc proc rw 0 0\n'
        '/dev/sdr1 /media/drobo2 ext3 rw 0 0\n'
        '/dev/sdq2 /media/drobo\\040b ntfs rw 0 0\n')


class TestMounts(FormatMountBase):

    def setUp(self):
        super().setUp()
        with open(self.mtab, 'w') as f:
            f.write(MTAB)

    def test_discover_mounts(self):
        d = self.drobo()
        self.assertEqual(d.DiscoverMounts(),
                         ['/media/drobo', '/media/drobo\\040b'])

    def test_discover_mounts_every_lun(self):
        d = self.drobo(['/dev/sdq', '/dev/sdr'])
        self.assertEqual(d.DiscoverMounts(), [
            '/media/drobo', '/media/drobo2', '/media/drobo\\040b'
        ])

    def test_discover_mounts_none(self):
        d = self.drobo('/dev/sdz')
        self.assertEqual(d.DiscoverMounts(), [])

    def test_discover_mounts_simulation(self):
        d = self.drobo()
        self.debug(Drobo.DBG_Simulation)
        self.assertEqual(d.DiscoverMounts(), ['/drmnt0', '/drmnt2'])

    def test_umount(self):
        d = self.drobo()
        self.debug(Drobo.DBG_Chatty)
        with self.stdout() as out:
            self.assertTrue(d.umount())
        self.assertEqual(self.system.call_args_list, [
            mock.call('umount /media/drobo'),
            mock.call('umount /media/drobo\\040b')
        ])
        self.assertIn('unmounting:  /media/drobo', out.getvalue())

    def test_umount_failure_stops(self):
        d = self.drobo()
        self.system.return_value = 256
        self.assertFalse(d.umount())
        self.assertEqual(self.system.call_args_list,
                         [mock.call('umount /media/drobo')])

    def test_umount_nothing_mounted(self):
        d = self.drobo('/dev/sdz')
        self.assertTrue(d.umount())
        self.system.assert_not_called()

    def test_umount_simulation(self):
        d = self.drobo()
        self.debug(Drobo.DBG_Simulation)
        self.assertTrue(d.umount())
        self.system.assert_not_called()


# --- discovery ----------------------------------------------------------------


class TestDiscoverLUNs(CovTestCase):

    def test_non_simulation_path(self):
        """ every candidate the scan finds is tried; those that fail the
            Drobo() checks are left out. """
        luns = [['/dev/sdb'], ['/dev/sdc', '/dev/sdd'], ['/dev/sde']]
        tried = []

        def fake_drobo(chardevs, debugflags):
            tried.append((chardevs, debugflags))
            if chardevs == ['/dev/sde']:
                raise Drobo.DroboException('not a drobo')
            return object()

        scan = mock.Mock(return_value=luns)
        with mock.patch.object(DroboIOctl, 'drobolunlist', scan), \
             mock.patch.object(Drobo, 'Drobo', fake_drobo), \
             self.stdout() as out:
            found = Drobo.DiscoverLUNs(Drobo.DBG_Detection, 'Newco')
        self.assertEqual(found, luns[:2])
        scan.assert_called_once_with(Drobo.DBG_Detection, 'Newco')
        self.assertEqual(tried, [(l, Drobo.DBG_Detection) for l in luns])
        self.assertIn("trying:  ['/dev/sdc', '/dev/sdd']", out.getvalue())

    def test_real_constructor_checks(self):
        """ through the real Drobo(): a unit with too few slots is dropped. """

        def three_slots(sub_page):
            if sub_page == 0x01:
                body = struct.pack('>BBBQ', 3, 0, 16, 1)
                return struct.pack('>BBH', 0x7a, 1, len(body)) + body
            return legacy_subpage(sub_page)

        devs = {'/dev/sdb': FakeDevice(), '/dev/sdc': FakeDevice(
            responder=three_slots)}
        with mock.patch.object(DroboIOctl, 'drobolunlist',
                               return_value=[['/dev/sdb'], ['/dev/sdc']]), \
             mock.patch.object(DroboIOctl, 'DroboIOctl',
                               lambda path, *a: devs[path]):
            self.assertEqual(Drobo.DiscoverLUNs(), [['/dev/sdb']])

    def test_nothing_found(self):
        with mock.patch.object(DroboIOctl, 'drobolunlist', return_value=[]):
            self.assertEqual(Drobo.DiscoverLUNs(), [])

    def test_simulation(self):
        self.assertEqual(Drobo.DiscoverLUNs(Drobo.DBG_Simulation),
                         [['/dev/sdx', '/dev/sdy'], ['/dev/sdz']])
        self.assertEqual(Drobo.DEBUG, Drobo.DBG_Simulation)

    def test_sets_module_debug(self):
        with mock.patch.object(DroboIOctl, 'drobolunlist', return_value=[]):
            Drobo.DiscoverLUNs(Drobo.DBG_Chatty)
        self.assertEqual(Drobo.DEBUG, Drobo.DBG_Chatty)


class TestDel(CovTestCase):

    def test_closes_device(self):
        dev = FakeDevice()
        dev.closefd = mock.Mock()
        d = make_drobo(dev)
        self.objs.append(d)
        self.debug(Drobo.DBG_Instantiation)
        with self.stdout() as out:
            d.__del__()
        dev.closefd.assert_called_once_with()
        self.assertIsNone(d.fd)
        self.assertIn('__del__ ', out.getvalue())

    def test_no_device(self):
        d = make_drobo(FakeDevice())
        d.fd = None
        d.__del__()  # nothing to close
        self.assertIsNone(d.fd)


# --- debug tracing --------------------------------------------------------------

ALL_DEBUG = 63  # every DBG_* bit except simulation


class TestDebugTrace(CovTestCase):
    """ key query paths with debugflags=63, stdout captured. """

    def construct(self, dev, flags=ALL_DEBUG):
        with mock.patch.object(DroboIOctl, 'DroboIOctl', lambda *a, **k: dev), \
             self.stdout() as out:
            d = Drobo.Drobo('/dev/sdq', flags)
        self.objs.append(d)
        return d, out.getvalue()

    def test_constructor(self):
        d, out = self.construct(FakeDevice())
        self.assertEqual(Drobo.DEBUG, ALL_DEBUG)
        for line in ('__init__ ', 'getsubpage',
                     'DMIP sub_page query:0x01 pattern: >BBHBBBQ',
                     '4 byte returned sense buffer header: 0x7a, 0x1, ',
                     'DMIP response sub_page:0x01 returned: ',
                     'inquiry response: ', 'cfg:  (4, 16, ',
                     "settings:  (1220112079, 8, 'TRUSTED DATA')",
                     'DMIP sub_page query:0x08'):
            self.assertIn(line, out)

    def test_queries(self):
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        d, out = self.construct(dev)
        with self.stdout() as out:
            self.assertEqual(d.GetSubPageCapacity(), (1000, 2000, 3000, 400))
            self.assertEqual(len(d.GetSubPageSlotInfo()), 4)
            self.assertEqual(d.GetSubPageProtocol(), (0, 10))
            self.assertEqual(d.GetSubPageStatus(), ([], 0))
            d.Blink()
        out = out.getvalue()
        for sub_page in (0x02, 0x03, 0x06, 0x09):
            self.assertIn('DMIP sub_page query:0x%02x' % sub_page, out)
            self.assertIn('DMIP response sub_page:0x%02x' % sub_page, out)
        self.assertIn('issuecommand...', out)

    def test_fallback_to_mode_parameter_header(self):
        d, out = self.construct(FakeDevice(responder=gen2_responder()))
        self.assertEqual(d.mode_sense_style, 'dbd')
        self.assertIn('short legacy reply, trying mode parameter header', out)

    def test_raw_page_fallback(self):
        d, out = self.construct(FakeDevice(responder=gen2_responder()))
        d.mode_sense_style = 'legacy'
        with self.stdout() as out:
            ex = d.GetStatusEx()
        self.assertEqual(ex['status'], 0)
        self.assertIn('no legacy page, trying mode parameter header',
                      out.getvalue())

    def test_dbd_reply_trace(self):

        def wrong(sub_page, mcb):
            if not mcb[1] & 0x08:
                return b'\x00\x06' + b'\0' * 6
            page = legacy_subpage(0x02)
            return struct.pack('>HBBBBH', 6 + len(page), 0, 0, 0, 0, 0) + page

        wrong.wants_cdb = True
        out = io.StringIO()
        with mock.patch.object(DroboIOctl, 'DroboIOctl',
                               lambda *a, **k: FakeDevice(responder=wrong)), \
             contextlib.redirect_stdout(out), \
             self.assertRaises(Drobo.DroboException):
            Drobo.Drobo('/dev/sdq', ALL_DEBUG)
        out = out.getvalue()
        self.assertIn('DBD reply for sub_page 0x01 not understood', out)
        self.assertIn('expected 15 bytes for sub_page 0x01', out)

    def test_dbd_ioerror(self):
        """ an IOError on the DBD query counts as no answer. """

        def short(sub_page, mcb):
            if mcb[1] & 0x08:
                raise IOError('no')
            return b'\x00\x06' + b'\0' * 6

        short.wants_cdb = True
        dev = FakeDevice(responder=short)
        with mock.patch.object(DroboIOctl, 'DroboIOctl', lambda *a, **k: dev), \
             self.stdout() as out, self.assertRaises(Drobo.DroboException):
            Drobo.Drobo('/dev/sdq', 0)
        self.assertIn('expected 15 bytes for sub_page 0x01', out.getvalue())

    def test_detection_rejects_with_trace(self):

        def three_slots(sub_page):
            if sub_page == 0x01:
                body = struct.pack('>BBBQ', 3, 0, 16, 1)
                return struct.pack('>BBH', 0x7a, 1, len(body)) + body
            return legacy_subpage(sub_page)

        out = io.StringIO()
        dev = FakeDevice(responder=three_slots)
        with mock.patch.object(DroboIOctl, 'DroboIOctl', lambda *a, **k: dev), \
             contextlib.redirect_stdout(out), \
             self.assertRaises(Drobo.DroboException):
            Drobo.Drobo('/dev/sdq', Drobo.DBG_Detection)
        self.assertIn('/dev/sdq cfg[0] = 3, should >= 4.', out.getvalue())

    def test_relayout_save_failure_trace(self):
        """ an unusable state directory is reported under DBG_General. """
        notadir = self.path('file')
        open(notadir, 'w').close()
        dev = RelayoutDevice()
        with mock.patch.dict(os.environ, {'DROBO_UTILS_STATE_DIR': notadir}):
            d, out = self.construct(dev, Drobo.DBG_General)
            dev.count = 1000
            with self.stdout() as out:
                s, count = d.GetSubPageStatus()
        self.assertIn('no estimate yet ', s)
        self.assertIn('cannot save relay out state to %s/' % notadir,
                      out.getvalue())


class TestSimulationInProcess(CovTestCase):
    """ the -v 128 simulation paths, in process (drobom's tests run it as a
        subprocess, which coverage does not see.) """

    def setUp(self):
        super().setUp()
        with self.stdout():
            self.d = Drobo.Drobo('/dev/sdx', Drobo.DBG_Simulation | ALL_DEBUG)
        self.objs.append(self.d)

    def test_no_device_opened(self):
        self.assertIsNone(self.d.fd)
        self.assertEqual(self.d.fw[6], 'ArmMarvell')

    def test_queries(self):
        d = self.d
        with self.stdout():
            self.assertEqual(d.GetSubPageConfig(), (8, 16, 2199023250944))
            self.assertEqual(d.slot_count, 8)
            free, used, total, unprot = d.GetSubPageCapacity()
            self.assertEqual(free + used, total)
            self.assertEqual(len(d.GetSubPageSlotInfo()), 8)
            self.assertEqual(d.GetSubPageLUNs()[0][3], 'GPT')
            self.assertEqual(d.GetSubPageSettings()[2], 'TRUSTED DATA')
            self.assertEqual(d.GetSubPageProtocol(), (0, 10))
            self.assertIsInstance(d.GetSubPageStatus(), list)
            self.assertEqual(d.GetStatusEx()['status'], 0x0240)
            self.assertEqual(d.GetLUNsInfoEx()[0]['name'], 'Drobo volume 1')
            self.assertEqual(d.GetLUNsInfoEx2()[0]['name'], 'Drobo volume 1')
            self.assertIsNone(d.GetRelayoutEstimate())
            self.assertEqual(d.GetOptions()['YellowThreshold'], 85)

    def test_blink_only_advances_transaction(self):
        t = self.d.transactionID
        with self.stdout() as out:
            self.d.Blink()
        self.assertIn('issuecommand...', out.getvalue())
        self.assertEqual(self.d.transactionID,
                         1 if t > Drobo.MAX_TRANSACTION else t + 1)


if __name__ == '__main__':
    unittest.main()
