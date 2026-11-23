"""
Coverage tests for the drobom command line tool.

drobom is run in-process (runpy, with sys.argv patched and stdout captured) so
that coverage can see it.  Most commands use the -v 128 simulation; where the
simulation does not reach, a FakeDevice from test_regression stands in for the
SCSI layer, or the Drobo method concerned is mocked.

Nothing here may reach real hardware, run parted/mkfs, use the network or
write outside temporary directories: every test starts with guards that make
those paths fail loudly (see DrobomTestCase.setUp.)

These are characterisation tests: they pin what drobom does today.  Where that
is a bug, the test says what should happen and is marked expectedFailure.

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.
"""

import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from unittest import mock

from test_regression import TOP, FakeDevice

import Drobo
import DroboIOctl

DROBOM = os.path.join(TOP, 'drobom')

SIM = ('-v', '128')  # simulation, terse output
SIM_CHATTY = ('-v', '129')  # simulation, chatty output
ONE = ('-d', '/dev/sdz')  # just one of the two simulated Drobos

_STATE = None


def setUpModule():
    """ no test may read or write saved state in the real home directory. """
    global _STATE
    _STATE = tempfile.TemporaryDirectory()
    os.environ['DROBO_UTILS_STATE_DIR'] = _STATE.name


def tearDownModule():
    os.environ.pop('DROBO_UTILS_STATE_DIR', None)
    _STATE.cleanup()


def _forbidden(what):

    def refuse(*a, **k):
        raise AssertionError('test reached %s' % what)

    return refuse


class ScsiDevice(FakeDevice):
    """ a FakeDevice that also answers DroboIOctl.version() (info scsi.) """

    def version(self):
        return 3


class Result:

    def __init__(self, code, out, err):
        self.code = code  # SystemExit code, or 'fell off the end'
        self.out = out
        self.err = err


FELL_THROUGH = 'fell off the end'


class DrobomTestCase(unittest.TestCase):

    def setUp(self):
        # guards: anything that could touch hardware, disks, the network or
        # files outside temp dirs fails, unless a test patches it on top.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        guards = [
            mock.patch.object(DroboIOctl, 'DroboIOctl',
                              _forbidden('DroboIOctl (hardware)')),
            mock.patch.object(DroboIOctl, 'drobolunlist',
                              _forbidden('drobolunlist (hardware)')),
            mock.patch.object(os, 'system', _forbidden('os.system')),
            mock.patch.object(subprocess, 'Popen',
                              _forbidden('subprocess.Popen')),
            mock.patch.object(urllib.request, 'urlopen',
                              _forbidden('the network')),
            mock.patch.object(Drobo.Drobo, 'format_script',
                              _forbidden('format_script (/tmp/fmtscript)')),
            mock.patch.object(Drobo.Drobo, 'dumpDiagnostics',
                              _forbidden('dumpDiagnostics (/tmp)')),
            mock.patch.object(Drobo.Drobo, 'localfwrepository',
                              os.path.join(self.tmp.name, 'fw')),
            mock.patch('builtins.input', _forbidden('an unexpected prompt')),
        ]
        for g in guards:
            g.start()
            self.addCleanup(g.stop)

    def drobom(self, *args, answers=None):
        """ run drobom in-process with the given arguments.
            answers: what to type at each prompt. """
        saved_debug = Drobo.DEBUG
        out, err = io.StringIO(), io.StringIO()
        code = FELL_THROUGH
        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(
                mock.patch.object(sys, 'argv', ['drobom'] + list(args)))
            if answers is not None:
                typed = iter(answers)
                stack.enter_context(
                    mock.patch('builtins.input', lambda *a: next(typed)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                runpy.run_path(DROBOM, run_name='__main__')
            except SystemExit as e:
                code = e.code
            except Exception as e:
                e.drobom_out = out.getvalue()  # what was printed first
                raise
            finally:
                Drobo.DEBUG = saved_debug
        return Result(code, out.getvalue(), err.getvalue())

    @contextlib.contextmanager
    def device(self, dev, luns=(('/dev/sdq', ), )):
        """ a non-simulated Drobo, backed by a FakeDevice. """
        with mock.patch.object(DroboIOctl, 'DroboIOctl', lambda *a, **k: dev), \
             mock.patch.object(Drobo, 'DiscoverLUNs',
                               lambda *a: [list(l) for l in luns]), \
             mock.patch.object(Drobo.Drobo, 'DiscoverMounts', lambda s: []), \
             mock.patch.object(os, 'geteuid', lambda: 0):
            yield dev


def usage_text():
    return 'Usage: drobom [options] <command> [ arguments... ]'


# --- option parsing ----------------------------------------------------------


class TestOptions(DrobomTestCase):

    def test_no_arguments_prints_usage(self):
        r = self.drobom()
        self.assertIsNone(r.code)
        self.assertIn(usage_text(), r.out)
        self.assertIn('drobo management interface, version: %s' %
                      Drobo.VERSION, r.out)
        self.assertIn('\tview\tstart up a management dashboard GUI', r.out)

    def test_help_command(self):
        r = self.drobom('help')
        self.assertIsNone(r.code)
        self.assertIn(usage_text(), r.out)

    def test_dash_h(self):
        for opt in ('-h', '--help'):
            r = self.drobom(*SIM, opt, 'list')
            self.assertIsNone(r.code)
            self.assertIn(usage_text(), r.out)
            self.assertNotIn('/dev/sdx', r.out)

    def test_version(self):
        for opt in ('-V', '--version'):
            r = self.drobom(opt)
            self.assertIsNone(r.code)
            self.assertEqual(r.out, Drobo.VERSION + '\n')

    def test_bad_option(self):
        r = self.drobom('-x', 'list')
        self.assertEqual(r.code, 2)
        self.assertIn(usage_text(), r.out)

    def test_missing_option_argument(self):
        r = self.drobom('-v')
        self.assertEqual(r.code, 2)
        self.assertIn(usage_text(), r.out)

    def test_verbosity_must_be_a_number(self):
        """ pins current behaviour: a traceback, not a message. """
        with self.assertRaises(ValueError):
            self.drobom('-v', 'lots', 'list')

    def test_options_but_no_command(self):
        r = self.drobom(*SIM)
        self.assertIsNone(r.code)
        self.assertIn(usage_text(), r.out)

    def test_command_option(self):
        for args in (('-c', 'list'), ('--command=list', )):
            r = self.drobom(*SIM, *args)
            self.assertIsNone(r.code)
            self.assertEqual(r.out, '/dev/sdx:/dev/sdy /dev/sdz\n')

    def test_long_verbosity(self):
        r = self.drobom('--verbosity=128', 'list')
        self.assertEqual(r.out, '/dev/sdx:/dev/sdy /dev/sdz\n')

    def test_long_verbose(self):
        """ usage advertises --verbose (it used to be rejected as a bad
            option: getopt only knew --verbosity.) """
        for args in (('--verbose=128', ), ('--verbose', '128')):
            r = self.drobom(*args, 'list')
            self.assertIsNone(r.code)
            self.assertEqual(r.out, '/dev/sdx:/dev/sdy /dev/sdz\n')

    def test_string_passed_to_discovery(self):
        with mock.patch.object(Drobo, 'DiscoverLUNs',
                               return_value=[['/dev/sdb']]) as disc:
            for args in (('-s', 'Newco'), ('--string=Newco', )):
                r = self.drobom(*SIM, *args, 'list')
                self.assertEqual(r.out, '/dev/sdb\n')
                disc.assert_called_with(128, 'Newco')

    def test_default_vendor_string_is_empty(self):
        with mock.patch.object(Drobo, 'DiscoverLUNs',
                               return_value=[['/dev/sdb']]) as disc:
            self.drobom(*SIM, 'list')
            disc.assert_called_with(128, '')

    def test_device_selects_a_drobo(self):
        r = self.drobom(*SIM, '-d', '/dev/sdz', 'list')
        self.assertEqual(r.out, '/dev/sdz\n')
        r = self.drobom(*SIM, '--device=sdy', 'list')
        self.assertEqual(r.out, '/dev/sdx:/dev/sdy\n')

    def test_device_not_a_drobo(self):
        r = self.drobom(*SIM, '-d', '/dev/sdq', 'list')
        self.assertIsNone(r.code)
        self.assertEqual(r.out, 'given device: /dev/sdq, is not a Drobo\n')

    def test_device_with_flat_lun_list(self):
        """ older discovery returned plain strings, not lists of them. """
        with mock.patch.object(Drobo, 'DiscoverLUNs',
                               return_value=['/dev/sdb', '/dev/sdc']):
            r = self.drobom(*SIM, '-d', 'sdc', 'list')
        self.assertEqual(r.out, ':'.join('/dev/sdc') + '\n')

    def test_no_drobos(self):
        with mock.patch.object(Drobo, 'DiscoverLUNs', return_value=[]):
            r = self.drobom(*SIM, 'status')
        self.assertIsNone(r.code)
        self.assertEqual(r.out, 'No Drobos discovered\n')

    def test_detection_debug(self):
        r = self.drobom('-v', str(128 | 16), *ONE, 'time')
        self.assertIn("found  [['/dev/sdz']]", r.out)

    def test_root_not_needed_for_simulation(self):
        with mock.patch.object(os, 'geteuid', lambda: 1000):
            r = self.drobom(*SIM, 'list')
        self.assertEqual(r.out, '/dev/sdx:/dev/sdy /dev/sdz\n')

    def test_root_required_without_simulation(self):
        with mock.patch.object(os, 'geteuid', lambda: 1000), \
             mock.patch.object(Drobo, 'DiscoverLUNs',
                               _forbidden('discovery')):
            r = self.drobom('list')
        self.assertIsNone(r.code)
        self.assertEqual(r.out, 'Please try again as root.\n')

    def test_no_and_yes_long_forms(self):
        with mock.patch.object(Drobo.Drobo, 'SetLunSize') as sls:
            r = self.drobom(*SIM, *ONE, '--no', 'set', 'lunsize', '1')
            self.assertIn('Phew', r.out)
            sls.assert_not_called()

    @unittest.expectedFailure
    def test_long_yes(self):
        """ BUG: usage advertises --yes, but the long option is declared
            as 'y', so --yes is rejected (and --y trips 'assert False'.) """
        with mock.patch.object(Drobo.Drobo, 'SetLunSize') as sls:
            r = self.drobom(*SIM, *ONE, '--yes', 'set', 'lunsize', '1')
        sls.assert_called_once_with(1)

    def test_long_y_trips_assert(self):
        """ pins current behaviour of the mis-declared long option. """
        with self.assertRaises(AssertionError):
            self.drobom(*SIM, *ONE, '--y', 'list')

    def test_unknown_command(self):
        r = self.drobom(*SIM, *ONE, 'bogus')
        self.assertEqual(r.code, FELL_THROUGH)
        self.assertIn(usage_text(), r.out)
        self.assertTrue(r.out.endswith('Unknown Command:  bogus\n'))


# --- list, status, time, blink -------------------------------------------------


class TestSimpleCommands(DrobomTestCase):

    def test_list(self):
        r = self.drobom(*SIM, 'list')
        self.assertIsNone(r.code)
        self.assertEqual(r.out, '/dev/sdx:/dev/sdy /dev/sdz\n')

    def test_status_every_drobo(self):
        r = self.drobom(*SIM, 'status')
        self.assertEqual(r.code, FELL_THROUGH)
        lines = r.out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(
            lines[0], r'^/dev/sdx:/dev/sdy /drmnt0:/drmnt2 TRUSTED DATA '
            r'\d\d+% full - \[.*\]$')
        self.assertRegex(lines[1], r'^/dev/sdz /drmnt0:/drmnt2 TRUSTED DATA ')

    def test_status_no_capacity(self):
        with mock.patch.object(Drobo.Drobo, 'GetSubPageCapacity',
                               return_value=(0, 0, 0, 0)):
            r = self.drobom(*SIM, *ONE, 'status')
        self.assertEqual(
            r.out.splitlines()[0],
            'firmware too old to report capacity properly... or the drobo is empty...'
        )
        self.assertIn(' -1% full - ', r.out)

    def test_status_nothing_mounted(self):
        with mock.patch.object(Drobo.Drobo, 'DiscoverMounts', return_value=[]):
            r = self.drobom(*SIM, *ONE, 'status')
        self.assertRegex(r.out, r'^/dev/sdz - TRUSTED DATA ')

    def test_status_percentage(self):
        with mock.patch.object(Drobo.Drobo, 'GetSubPageCapacity',
                               return_value=(750, 249, 1000, 0)), \
             mock.patch.object(Drobo.Drobo, 'GetSubPageStatus',
                               return_value=([], 0)):
            r = self.drobom(*SIM, *ONE, 'status')
        self.assertEqual(r.out,
                         '/dev/sdz /drmnt0:/drmnt2 TRUSTED DATA 25% full - ([], 0)\n')

    @unittest.expectedFailure
    def test_status_unnamed_drobo(self):
        """ BUG: an empty name is replaced with n[2] = "-", but the settings
            are a tuple, so this raises TypeError. """
        with mock.patch.object(Drobo.Drobo, 'GetSubPageSettings',
                               return_value=(1220112079, 8, '')):
            r = self.drobom(*SIM, *ONE, 'status')
        self.assertRegex(r.out, r'^/dev/sdz /drmnt0:/drmnt2 - ')

    def test_status_real_device(self):
        with self.device(FakeDevice()):
            r = self.drobom('status')
        # (the hardware path prints GetSubPageStatus's (flags, count) tuple)
        self.assertEqual(r.out, '/dev/sdq - TRUSTED DATA 66% full - ([], 0)\n')

    def test_time(self):
        d = Drobo.Drobo('/dev/sdz', 128)
        expected = time.ctime(d.GetSubPageSettings()[0])
        clock = Drobo._clocktext(d.GetClock())
        r = self.drobom(*SIM, *ONE, 'time')
        self.assertEqual(
            r.out,
            'Drobo says it is: %s\nDrobo clock: %s\n' % (expected, clock))

    def test_time_without_clock(self):
        """ the clock line is left out when the unit cannot report it. """
        for exc in (Drobo.DroboException('no'), IOError('unsupported')):
            with mock.patch.object(Drobo.Drobo, 'GetClock', side_effect=exc):
                r = self.drobom(*SIM, *ONE, 'time')
            self.assertTrue(r.out.startswith('Drobo says it is: '))
            self.assertNotIn('Drobo clock', r.out)

    def test_blink_simulated(self):
        with mock.patch.object(Drobo.Drobo, 'Blink',
                               autospec=True) as blink:
            r = self.drobom(*SIM, 'blink')
        self.assertEqual(r.out, '')
        self.assertEqual(blink.call_count, 2)  # one per Drobo

    def test_blink_sends_identify(self):
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        with self.device(dev):
            r = self.drobom('blink')
        self.assertEqual(r.out, '')
        ea = [c for c in dev.cdbs if c[0] == 0xea]
        self.assertEqual(len(ea), 1)
        self.assertEqual(ea[0][3], 0x06)


# --- info ----------------------------------------------------------------------


class TestInfo(DrobomTestCase):

    def info(self, what, *, chatty=False, frame=False):
        """ drobom info output.  Chatty output is framed by a header and a
            rule; that is stripped unless frame is set. """
        r = self.drobom(*(SIM_CHATTY if chatty else SIM), *ONE, 'info', what)
        self.assertEqual(r.code, FELL_THROUGH, r.err)
        if chatty and not frame:
            lines = r.out.splitlines(True)
            rule = '-' * 57 + '\n'
            self.assertEqual(lines[4], rule)
            self.assertEqual(lines[-1], rule)
            return ''.join(lines[5:-1])
        return r.out

    def test_config(self):
        self.assertEqual(self.info('config'), '(8, 16, 2199023250944)\n')

    def test_config_chatty(self):
        out = self.info('config', chatty=True, frame=True)
        self.assertIn(
            'Configuration maxima: slots: 8, luns: 16, lunsize: 2 TB\n', out)
        # chatty info is framed with a header naming the Drobo
        self.assertTrue(out.startswith('-' * 57 + '\nDrobo Name: TRUSTED DATA '
                                       'Devices: /dev/sdz\n      Time: '))
        self.assertTrue(out.endswith('-' * 57 + '\n'))
        self.assertIn('\nDrobo clock: 2008-08-30 ', out)

    def test_capacity(self):
        self.assertRegex(
            self.info('capacity'), r"^\(\d+, \d+, \d+, \d+\)\n"
            r"\{'unprotected_total': 125184245760, 'pt_free': 370267914240, "
            r"'pt_used': 125184245760, 'pt_total': 495452160000\}\n$")

    def test_capacity_chatty(self):
        self.assertRegex(self.info('capacity', chatty=True),
                         r'^Capacity \(in GB\):  used: \d+, free: \d+, '
                         r'total: \d+\n'
                         r'Unprotected capacity \(in GB\):  total: 125\n'
                         r'PT capacity \(in GB\):  used: 125, free: 370, '
                         r'total: 495\n$')

    def capacity_ex(self, **fields):
        x = dict.fromkeys(('unprotected_total', 'pt_free', 'pt_used',
                           'pt_total'))
        x.update(fields)
        return mock.patch.object(Drobo.Drobo, 'GetCapacityEx', return_value=x)

    def test_capacity_chatty_some_extra_fields(self):
        with self.capacity_ex(pt_used=0, pt_free=10**12, pt_total=10**12):
            out = self.info('capacity', chatty=True)
        self.assertNotIn('Unprotected', out)
        self.assertTrue(out.endswith(
            '\nPT capacity (in GB):  used: 0, free: 1000, total: 1000\n'))
        with self.capacity_ex(unprotected_total=2 * 10**9):
            out = self.info('capacity', chatty=True)
        self.assertTrue(
            out.endswith('\nUnprotected capacity (in GB):  total: 2\n'))

    def test_capacity_no_extra_fields(self):
        """ older firmware: only the classic tuple / line is printed. """
        with self.capacity_ex():
            self.assertRegex(self.info('capacity'), r'^\([^\n]*\)\n$')
        for exc in (Drobo.DroboException('short'), IOError('unsupported')):
            with mock.patch.object(Drobo.Drobo, 'GetCapacityEx',
                                   side_effect=exc):
                self.assertRegex(self.info('capacity', chatty=True),
                                 r'^Capacity \(in GB\)[^\n]*\n$')

    def test_protocol(self):
        self.assertEqual(self.info('protocol'), '(0, 10)\n')
        self.assertIn('protocol version:  0.10\n',
                      self.info('protocol', chatty=True))

    def test_slots(self):
        out = self.info('slots')
        self.assertIn('   0  500          ST3500830AS                green\n',
                      out)
        self.assertNotIn('Model', out)

    @unittest.expectedFailure
    def test_slots_chatty_alone(self):
        """ BUG (simulation only): a simulated Drobo has no slot_count until
            GetSubPageConfig has run, so 'drobom -v 129 info slots' raises
            AttributeError.  Hardware is unaffected: the constructor reads
            the config there. """
        self.assertIn('number of slots: 8', self.info('slots', chatty=True))

    def test_slots_chatty(self):
        out = self.info('config,slots', chatty=True)
        self.assertIn('query slotinfo result:  number of slots: 8\n', out)
        self.assertIn('slot   GB                Model               Status\n',
                      out)

    def test_firmware(self):
        self.assertRegex(self.info('firmware'),
                         r"^\(1, 201, 12942, 12, 6, .*'ArmMarvell', '1.1.2'")

    def test_firmware_chatty(self):
        out = self.info('firmware', chatty=True)
        self.assertIn('Firmware: 1.1.2\n', out)
        self.assertIn('Revision: 1.201 ( 12942 ) built: May 13 2008', out)
        self.assertIn('Features: NO_AUTO_REBOOT,', out)

    def test_status(self):
        self.assertRegex(self.info('status'),
                         r'^/dev/sdz /drmnt0:/drmnt2 TRUSTED DATA ')

    def test_options(self):
        out = self.info('options')
        self.assertTrue(out.startswith("{'YellowThreshold': 85, "))

    def test_options_chatty(self):
        out = self.info('options', chatty=True)
        self.assertIn('\t YellowThreshold 85\n', out)
        self.assertIn('\t IPAddress 192.168.10.4\n', out)

    def test_options_unsupported(self):
        with mock.patch.object(Drobo.Drobo, 'GetOptions', return_value=None):
            self.assertEqual(self.info('options'),
                             'options not supported on this hardware\n')

    def test_luns(self):
        self.assertEqual(self.info('luns'),
                         "(0, 2199023251456, 5092651008, 'GPT', 1, ['EXT3'])\n")

    def test_luns_chatty(self):
        out = self.info('luns', chatty=True)
        self.assertIn('lun     size (GB) used    PTFmt  FStype:\n', out)
        self.assertIn('  0     2199         5      GPT 1 \n', out)

    def test_luns_chatty_without_luninfo2(self):
        """ LUNINFO-only tuples have no scheme or format: shown as '-'. """
        with mock.patch.object(Drobo.Drobo, 'GetSubPageLUNs',
                               return_value=[(0, 2 * 10**12, 10**9, 0)]):
            out = self.info('luns', chatty=True)
        self.assertIn('\n  0     2000         1        - - \n', out)

    def test_serial(self):
        with mock.patch.object(
                Drobo, 'vpd_serial',
                lambda c: 'DRVPD01' if c == '/dev/sdx' else None):
            r = self.drobom(*SIM, '-d', '/dev/sdx', 'info', 'serial')
        self.assertEqual(r.out, 'Serial: DRSIMULATED0000\n'
                         'VPD serial (/dev/sdx): DRVPD01\n'
                         'VPD serial (/dev/sdy): not available\n')

    def test_serial_not_reported(self):
        with mock.patch.object(Drobo, 'vpd_serial', lambda c: None), \
             mock.patch.object(Drobo.Drobo, 'GetSerial', return_value=None):
            self.assertEqual(self.info('serial'),
                             'Serial: not reported by the unit\n'
                             'VPD serial (/dev/sdz): not available\n')

    def test_drives_accelerator_listed_last(self):

        def row(slot, acc=False):
            return {'slot': slot, 'total_capacity': 10**12, 'health': 'Good',
                    'media': 'SSD' if acc else 'HDD', 'interface': 'SATA',
                    'temperature': 0 if acc else 30, 'life_remaining': 99,
                    'errors': 0, 'rpm': 0 if acc else 7200,
                    'manufacturer': 'M%d' % slot, 'firmware': 'F',
                    'serial': 'S%d' % slot, 'accelerator': acc}

        rows = [row(5, acc=True), row(0), row(1)]
        with mock.patch.object(Drobo.Drobo, 'GetSlotInfo2', return_value=rows):
            lines = self.info('drives').splitlines()
        self.assertEqual([l.split()[0] for l in lines[1:]],
                         ['0', '1', 'Accelerator', '5'])
        self.assertEqual(lines[3],
                         'Accelerator (mSATA cache, not a drive bay):')
        self.assertIn(' SSD/SATA        - ', lines[4])  # temperature 0: '-'

    def test_health_simulated(self):
        self.assertEqual(
            self.info('health'), 'Redundancy: NOT protected (no redundancy)\n'
            'Redundancy level: undetermined\n'
            'Status: No redundancy, Relay out in progress\n'
            'Severity: red\n'
            'Data protection: 1250000 blocks left, no estimate yet\n'
            'Disk pack status: 1\n'
            'Double degraded count: 0\n')

    def status_ex(self, flags=(), relayout=0, diskpack=None, dd=None):
        return mock.patch.object(
            Drobo.Drobo, 'GetStatusEx', return_value={
                'status': 0, 'flags': list(flags), 'relayout_count': relayout,
                'diskpack_status': diskpack, 'double_degraded': dd,
                'extra': None, 'severity': 'green'})

    def test_health_protected(self):
        with self.status_ex(), \
             mock.patch.object(Drobo.Drobo, 'GetRedundancyLevel',
                               return_value='dual'):
            self.assertEqual(self.info('health'),
                             'Redundancy: protected\nRedundancy level: dual\n'
                             'Status: OK\nSeverity: green\n')

    def test_health_relayout_with_estimate(self):
        est = {'percent_done': 42.5, 'minutes_left': 90,
               'eta': time.time() + 90 * 60}
        with self.status_ex(flags=['Relay out in progress'], relayout=10), \
             mock.patch.object(Drobo.Drobo, 'GetRelayoutEstimate',
                               return_value=est):
            out = self.info('health')
        self.assertIn('Redundancy: data protection in progress\n', out)
        self.assertIn(
            'Data protection: 42.5%% done, %s, %s\n' %
            (Drobo._timeleft(90).strip(), Drobo._completion(est['eta']).strip()),
            out)

    def test_drives(self):
        out = self.info('drives')
        self.assertTrue(out.startswith('Slot  Size(GB)  Health   Type '))
        self.assertIn('   0      3000  Good     HDD/SATA       35      100   '
                      '    0   7200  WDC WD30EFRX-68EUZN0     82.00A82  '
                      'WD-SIMULATED0\n', out)

    def test_drives_empty_slot(self):
        rows = [{'slot': 0, 'total_capacity': 0}]
        with mock.patch.object(Drobo.Drobo, 'GetSlotInfo2', return_value=rows):
            self.assertTrue(self.info('drives').endswith('\n   0  empty\n'))

    def test_drives_unsupported(self):
        with mock.patch.object(Drobo.Drobo, 'GetSlotInfo2', return_value=None):
            self.assertEqual(self.info('drives'),
                             'per-drive details are not supported by this firmware\n')

    def test_volumes(self):
        self.assertEqual(self.info('volumes'),
                         'LUN  Size(GB)  Name                              Format\n'
                         '  0     17592  Drobo volume 1                    EXT3\n')

    def test_volumes_with_initiators(self):
        luns = [{'index': 1, 'total_capacity': 2 * 10**12, 'name': 'x' * 40,
                 'format_name': ['NTFS', 'GPT'],
                 'initiators': [{'name': 'iqn.host', 'address': '10.0.0.2'},
                                {'name': 'iqn.other', 'address': ''}]}]
        with mock.patch.object(Drobo.Drobo, 'GetLUNsInfoBest',
                               return_value=luns):
            out = self.info('volumes')
        self.assertIn('  1      2000  %s  NTFS, GPT\n' % ('x' * 32), out)
        self.assertIn('      initiator: iqn.host (10.0.0.2)\n', out)
        self.assertIn('      initiator: iqn.other\n', out)

    def test_volumes_unsupported(self):
        with mock.patch.object(Drobo.Drobo, 'GetLUNsInfoBest',
                               return_value=None):
            self.assertEqual(
                self.info('volumes'),
                'extended volume information is not supported by this firmware\n')

    def test_several_options(self):
        out = self.info('config,protocol')
        self.assertEqual(out, '(8, 16, 2199023250944)\n(0, 10)\n')

    def test_no_such_option(self):
        self.assertEqual(self.info('bogus'),
                         'no such option: bogus\nvalid options:  %s\n' %
                         ', '.join(('config', 'capacity', 'protocol', 'scsi',
                                    'slots', 'firmware', 'status', 'options',
                                    'luns', 'health', 'drives', 'volumes',
                                    'serial')))

    def test_scsi(self):
        with self.device(ScsiDevice()):
            r = self.drobom('-v', '0', 'info', 'scsi')
        self.assertEqual(
            r.out, 'SCSI emulation information:\n'
            "(b'TRUSTED ', b'Mass Storage    ', b'1.00')\n"
            "('/dev/sdq', 1, 0, 0, 0, 'TRUSTED ', 3)\n")

    def test_scsi_chatty(self):
        with self.device(ScsiDevice()):
            r = self.drobom('info', 'scsi')  # default verbosity is chatty
        self.assertIn('Vendor: TRUSTED   Model: Mass Storage     Revision: 1.00\n',
                      r.out)
        self.assertIn('/dev/sdq: bus=scsi1 channel=0 id=0 lun=0 '
                      '(vendor=TRUSTED , version=3)\n', r.out)

    @unittest.expectedFailure
    def test_info_everything_simulated(self):
        """ BUG (known, plans/10-redrobo-findings.md): plain
            'drobom -v 128 info' fails with AttributeError, as a simulated
            Drobo has no .inquiry for the scsi section. """
        r = self.drobom(*SIM, *ONE, 'info')
        self.assertIn('SCSI emulation information:', r.out)

    def test_info_everything(self):
        """ with no options, every section is printed, in order. """
        inquiry = (0, ) * 8 + (b'TRUSTED ', b'Mass Storage    ', b'1.00')
        with mock.patch.object(Drobo.Drobo, 'inquiry', inquiry, create=True), \
             mock.patch.object(DroboIOctl, 'DroboIOctl',
                               lambda *a, **k: ScsiDevice()):
            r = self.drobom(*SIM, *ONE, 'info')
        markers = ['(8, 16, 2199023250944)', 'SCSI emulation information:',
                   'ST3500830AS', "'ArmMarvell'", "{'YellowThreshold'",
                   "'GPT'", 'Redundancy:', 'Slot  Size(GB)', 'LUN  Size(GB)']
        positions = [r.out.index(m) for m in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('Serial:', r.out)  # serial only when asked for


# --- set -------------------------------------------------------------------------


class TestSet(DrobomTestCase):

    def test_set_what(self):
        r = self.drobom(*SIM, *ONE, '-n', 'set')
        self.assertTrue(r.out.startswith('error: set what?\n'))
        self.assertIn(usage_text(), r.out)

    def test_set_name(self):
        """ -n does not apply: set name does not ask. """
        with mock.patch.object(Drobo.Drobo, 'Sync') as sync:
            r = self.drobom(*SIM, *ONE, '-n', 'set', 'name', 'Shelf')
        sync.assert_called_once_with('Shelf')
        self.assertEqual(r.out, '')

    def test_set_name_simulated(self):
        r = self.drobom(*SIM, *ONE, 'set', 'name', 'Shelf')
        self.assertEqual(r.out, '')

    def test_set_name_needs_a_name(self):
        """ pins current behaviour: a traceback, not a message. """
        with self.assertRaises(IndexError):
            self.drobom(*SIM, *ONE, 'set', 'name')

    def test_set_time(self):
        with mock.patch.object(Drobo.Drobo, 'Sync') as sync:
            r = self.drobom(*SIM, *ONE, '-n', 'set', 'time')
        sync.assert_called_once_with()
        self.assertEqual(r.out, '')

    def test_lunsize_invalid(self):
        with mock.patch.object(Drobo.Drobo, 'SetLunSize') as sls:
            r = self.drobom(*SIM, *ONE, '-y', 'set', 'lunsize', '3')
        self.assertIsNone(r.code)
        self.assertTrue(r.out.endswith(
            'lun size needs to be 1, 2, 4, 8, or 16 TiB; 3 is invalid\n'))
        sls.assert_not_called()

    def test_lunsize_not_a_number(self):
        """ pins current behaviour: a traceback, not a message. """
        with self.assertRaises(ValueError):
            self.drobom(*SIM, *ONE, '-n', 'set', 'lunsize', 'big')

    def test_lunsize_no(self):
        with mock.patch.object(Drobo.Drobo, 'SetLunSize') as sls:
            r = self.drobom(*SIM, *ONE, '-n', 'set', 'lunsize', '4')
        sls.assert_not_called()
        lines = r.out.splitlines()
        self.assertRegex(lines[0], r'^/dev/sdz /drmnt0:/drmnt2 TRUSTED DATA ')
        self.assertEqual(lines[1:], [
            'WARNING: lun size > 2 TiB known not work in many cases under Linux',
            'You asked to set the lunsize to 4 ',
            'Phew... You stopped just in time!'
        ])

    def test_lunsize_yes(self):
        with mock.patch.object(Drobo.Drobo, 'SetLunSize') as sls:
            r = self.drobom(*SIM, *ONE, '-y', 'set', 'lunsize', '2')
        sls.assert_called_once_with(2)
        self.assertNotIn('WARNING: lun size', r.out)
        self.assertIn('Done... Drobo is likely now rebooting.', r.out)

    def test_lunsize_prompt(self):
        for answer, done in (('y', True), ('Y', True), ('yes', True),
                             ('n', False), ('', False), ('YES', False)):
            with mock.patch.object(Drobo.Drobo, 'SetLunSize') as sls:
                r = self.drobom(*SIM, *ONE, 'set', 'lunsize', '1',
                                answers=[answer])
            self.assertIn(
                'WARNING: Ready to destroy all you data. Continue? (y/n) ',
                r.out)
            self.assertEqual(sls.called, done, answer)
            self.assertEqual('Phew' in r.out, not done, answer)

    def set_option(self, name, value):
        with mock.patch.object(Drobo.Drobo, 'SetOptions') as so:
            r = self.drobom(*SIM, *ONE, '-n', 'set', name, value)
        return r, so

    def test_option_true_false(self):
        r, so = self.set_option('DualDiskRedundancy', 'False')
        self.assertIs(so.call_args[0][0]['DualDiskRedundancy'], False)
        r, so = self.set_option('UseManualVolumeManagement', 'True')
        self.assertIs(so.call_args[0][0]['UseManualVolumeManagement'], True)
        self.assertEqual(r.out, '')

    def test_option_address(self):
        r, so = self.set_option('IPAddress', '10.1.2.3')
        self.assertEqual(so.call_args[0][0]['IPAddress'], '10.1.2.3')
        r, so = self.set_option('NetMask', '255.255.0.0')
        self.assertEqual(so.call_args[0][0]['NetMask'], '255.255.0.0')

    def test_option_integer(self):
        r, so = self.set_option('SpinDownDelayMinutes', '15')
        opts = so.call_args[0][0]
        self.assertEqual(opts['SpinDownDelayMinutes'], 15)
        self.assertEqual(opts['YellowThreshold'], 85)  # rest unchanged

    def test_option_read_only(self):
        self.assertIn('OptionsFlags', Drobo.READ_ONLY_OPTIONS)
        with mock.patch.object(Drobo.Drobo, 'GetOptions',
                               return_value={'OptionsFlags': 3}):
            r, so = self.set_option('OptionsFlags', '0')
        so.assert_not_called()
        self.assertEqual(r.out, 'option is read-only:  OptionsFlags\n')

    def test_option_not_present(self):
        r, so = self.set_option('Colour', 'blue')
        so.assert_not_called()
        self.assertEqual(r.out, 'option not present:  Colour\n')

    @unittest.expectedFailure
    def test_option_simulated(self):
        """ BUG (Drobo.py): SetOptions ignores the simulation flag and
            writes through self.fd, which is None in simulation, so
            'drobom -v 128 set <option> ...' raises AttributeError. """
        r = self.drobom(*SIM, *ONE, 'set', 'SpinDownDelayMinutes', '15')
        self.assertEqual(r.out, '')


# --- format, firmware, shutdown --------------------------------------------------


class TestFormat(DrobomTestCase):

    def test_needs_file_system(self):
        for args in ((), ('xfs', )):
            r = self.drobom(*SIM, *ONE, '-n', 'format', *args)
            self.assertEqual(
                r.out, 'error: file system format required as argument\n'
                "       Choices are: ('ext3', 'ntfs', 'msdos')\n")

    def format(self, *args, answers=None):
        with mock.patch.object(Drobo.Drobo, 'format_script',
                               return_value='/tmp/fmtscript') as fs, \
             mock.patch.object(subprocess, 'Popen') as popen:
            r = self.drobom(*SIM, *ONE, *args, answers=answers)
        return r, fs, popen

    def test_refused(self):
        r, fs, popen = self.format('-n', 'format', 'ntfs')
        fs.assert_called_once_with('ntfs')
        popen.assert_not_called()
        lines = r.out.splitlines()
        self.assertRegex(lines[0], r'^/dev/sdz /drmnt0:/drmnt2 TRUSTED DATA ')
        self.assertEqual(lines[1:], [
            'preparing a format script for a ntfs file system as you requested',
            'OK, I built the script but nothing is erased yet...',
            'You can have a look at it with: cat /tmp/fmtscript',
            'If you are really sure, go ahead and do: sh /tmp/fmtscript',
            'Phew... You stopped just in time!'
        ])

    def test_prompt_refused(self):
        r, fs, popen = self.format('format', 'ext3', answers=['n'])
        popen.assert_not_called()
        self.assertIn('WARNING: Ready to destroy all your data. Continue? (y/n) '
                      'Phew... You stopped just in time!\n', r.out)

    def test_prompt_accepted(self):
        r, fs, popen = self.format('format', 'msdos', answers=['y'])
        fs.assert_called_once_with('msdos')
        popen.assert_called_once_with('/tmp/fmtscript', bufsize=0, shell=True)
        popen.return_value.wait.assert_called_once_with()
        self.assertIn('ok... firing it off...\n', r.out)

    def test_yes(self):
        r, fs, popen = self.format('-y', 'format', 'ext3')
        popen.assert_called_once()
        self.assertNotIn('Continue?', r.out)


class TestFirmware(DrobomTestCase):

    def test_fwcheck(self):
        r = self.drobom(*SIM, 'fwcheck')
        self.assertEqual(r.out, 'value\nvalue\n')

    def test_fwload_needs_file(self):
        r = self.drobom(*SIM, *ONE, '-n', 'fwload')
        self.assertEqual(r.out, 'error: firmware file required as argument\n')

    def fw(self, cmd, good, *args):
        target = 'PickFirmware' if cmd == 'fwload' else 'updateFirmwareRepository'
        with mock.patch.object(Drobo.Drobo, target, return_value=good) as pick, \
             mock.patch.object(Drobo.Drobo, 'writeFirmware',
                               side_effect=lambda f: f(50)) as write, \
             mock.patch.object(Drobo.Drobo, 'Sync') as sync:
            r = self.drobom(*SIM, *ONE, '-n', cmd, *args)
        return r, pick, write, sync

    def test_fwload_invalid_image(self):
        r, pick, write, sync = self.fw('fwload', 0, '/nonexistent/fw.tdf')
        pick.assert_called_once_with('/nonexistent/fw.tdf')
        write.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(r.out, '')

    def test_fwload(self):
        r, pick, write, sync = self.fw('fwload', 1, '/nonexistent/fw.tdf')
        write.assert_called_once()
        sync.assert_called_once_with()
        self.assertEqual(r.out, 'write is 50% done.\n')

    def test_fwupgrade_up_to_date(self):
        r, pick, write, sync = self.fw('fwupgrade', 0)
        write.assert_not_called()
        self.assertEqual(r.out, '')

    def test_fwupgrade(self):
        r, pick, write, sync = self.fw('fwupgrade', 1)
        write.assert_called_once()
        sync.assert_called_once_with()
        self.assertEqual(r.out, 'write is 50% done.\n')

    def test_fwupgrade_simulated(self):
        """ in simulation, PickLatestFirmware returns str placeholders
            (hwlevel used to be .decode()d, AttributeError.)  Nothing is
            downloaded (urlopen is a guard here) and nothing is written. """
        with mock.patch.object(Drobo.Drobo, 'writeFirmware') as write:
            r = self.drobom(*SIM, *ONE, 'fwupgrade')
        self.assertEqual(r.code, FELL_THROUGH)
        self.assertEqual(r.out, '')
        write.assert_not_called()
        # pinned, review: the local repository (~/.drobo-utils, redirected
        # to a temp dir here) is still created, even in simulation.
        self.assertTrue(os.path.isdir(Drobo.Drobo.localfwrepository))
        self.assertEqual(os.listdir(Drobo.Drobo.localfwrepository), [])


class TestShutdown(DrobomTestCase):

    def test_simulated(self):
        with mock.patch.object(Drobo.Drobo, 'umount',
                               return_value=True) as um:
            r = self.drobom(*SIM, *ONE, 'shutdown')
        um.assert_called_once_with()
        self.assertEqual(r.out, '')

    def shutdown(self, unmounted):
        dev = FakeDevice()
        dev.ea_responder = lambda mcb, sz, out: b'\0' * sz
        with self.device(dev), \
             mock.patch.object(Drobo.Drobo, 'umount',
                               return_value=unmounted) as um:
            r = self.drobom('shutdown')
        um.assert_called_once_with()
        return r, [c for c in dev.cdbs if c[0] == 0xea]

    def test_standby_sent_after_umount(self):
        r, ea = self.shutdown(True)
        self.assertEqual(len(ea), 1)
        self.assertEqual(ea[0][3], 0x0d)

    def test_no_standby_if_umount_fails(self):
        r, ea = self.shutdown(False)
        self.assertEqual(ea, [])


# --- diagnostics -------------------------------------------------------------------


class TestDiagnostics(DrobomTestCase):

    def test_diag(self):
        path = os.path.join(self.tmp.name, 'DroboDiag.log')
        with mock.patch.object(Drobo.Drobo, 'dumpDiagnostics',
                               return_value=path):
            r = self.drobom(*SIM, *ONE, 'diag')
        self.assertEqual(r.out, 'diagnostics in  %s\n' % path)

    def test_diagprint_needs_file(self):
        r = self.drobom(*SIM, *ONE, 'diagprint')
        self.assertEqual(r.out, 'error: diagnostics file required as argument\n')

    def test_diagprint(self):
        text = '-- Drobo diagnostics\nslot 0 ok\n'
        # key 0x2d encrypts ' ' as '\r' and '\n' as "'": the file is read
        # as bytes, so neither is mangled.
        key = 0x2d
        path = os.path.join(self.tmp.name, 'diag.log')
        with open(path, 'wb') as f:
            f.write(bytes(ord(c) ^ key for c in text))
        r = self.drobom(*SIM, *ONE, 'diagprint', path)
        self.assertEqual(r.out, text + '\n')

    def test_diagprint_missing_file(self):
        r = self.drobom(*SIM, *ONE, 'diagprint',
                        os.path.join(self.tmp.name, 'absent'))
        self.assertEqual(r.out, '\n')


# --- view ----------------------------------------------------------------------------


class TestView(DrobomTestCase):

    def fake_qt(self):
        widgets = types.ModuleType('PyQt5.QtWidgets')
        widgets.QApplication = mock.MagicMock()
        core = types.ModuleType('PyQt5.QtCore')
        pkg = types.ModuleType('PyQt5')
        pkg.QtWidgets, pkg.QtCore = widgets, core
        gui = types.ModuleType('DroboGUI')
        gui.DroboGUI = mock.MagicMock()
        return {'PyQt5': pkg, 'PyQt5.QtWidgets': widgets,
                'PyQt5.QtCore': core, 'DroboGUI': gui}

    def test_gui_launched(self):
        mods = self.fake_qt()
        with mock.patch.dict(sys.modules, mods):
            r = self.drobom(*SIM, *ONE, 'view')
        app = mods['PyQt5.QtWidgets'].QApplication
        app.assert_called_once_with(['drobom', *SIM, *ONE, 'view'])
        gui = mods['DroboGUI'].DroboGUI
        gui.assert_called_once()
        self.assertIsInstance(gui.call_args[0][0], Drobo.Drobo)
        gui.return_value.show.assert_called_once_with()
        app.return_value.exec_.assert_called_once_with()
        self.assertEqual(r.out, '')

    @unittest.expectedFailure
    def test_no_qt(self):
        """ BUG: without PyQt5, drobom prints a hint but carries on, and
            then fails importing DroboGUI (ImportError) instead of
            stopping cleanly. """
        with mock.patch.dict(sys.modules, {'PyQt5': None, 'DroboGUI': None}):
            r = self.drobom(*SIM, *ONE, 'view')
        self.assertIn('QT support missing, no GUI possible', r.out)

    def test_no_qt_prints_hint_then_fails(self):
        """ pins current behaviour (see test_no_qt.) """
        with mock.patch.dict(sys.modules, {'PyQt5': None, 'DroboGUI': None}), \
             self.assertRaises(ImportError) as e:
            self.drobom(*SIM, *ONE, 'view')
        self.assertEqual(
            e.exception.drobom_out, 'QT support missing, no GUI possible\n'
            'perhaps following will help: apt-get install python3-qtpy\n')


if __name__ == '__main__':
    unittest.main()
