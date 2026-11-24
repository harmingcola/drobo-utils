"""
The Drobo module is used to get configuration data 
from a Data Robotics Inc. "Drobo" storage unit.
  
The ways of querying the unit are derived mostly from:
        DROBO Management Protocol Specification Rev. A.0 03/08

referred herein as 'DMP Spec'.  Another source of data was the
droboprotocoldefs.h file (referred to as dmp.h).  The last source 
of information is what the device actually does.  Many inconsistencies 
were identified.  ERRATA is used to identify when a conflict was apparent.

tested with firmware: 1.0.3 and later...

( Drobo is a trademark of Data Robotics Inc. )

copyright:
Drobo Utils Copyright (C) 2008,2009  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.

"""

import fcntl, struct, socket, array, subprocess, time
import os, sys, re
import types

#only for fw download...
import os.path
import urllib.request, urllib.error, urllib.parse
import zipfile, zlib

#only for simulation mode...
import random
import json

# maximum transaction ID before wrapping...
MAX_TRANSACTION = 250

# obviously need to update this with every release.
VERSION = '9999'

# set to non-zero to increase verbosity of library functions.
DEBUG = 0
# It's a bit field,
DBG_Chatty = 0x01
DBG_HWDialog = 0x02
DBG_Instantiation = 0x04
DBG_RawReturn = 0x08
DBG_Detection = 0x10
DBG_General = 0x20

# This isn't entirely simulation mode.  It is to aid development
# when no drobo is available.  You can format random disks, including
# non-drobos.  So do not activate unless you read what the code does first.
# in simulation mode, can run non-root, best for GUI work.
DBG_Simulation = 0x80

# GetOptions keys that are read only: SetOptions does not write them, so
# drobom set refuses them.
READ_ONLY_OPTIONS = ('OptionsFlags',)

#for generic SCSI IO details...
import DroboIOctl


class DroboException(Exception):
    """  If there is a problem accessing the Drobo, this exception is raised.
  """
    def __init__(self, msg="Unknown"):
        self.msg = msg

    def __str__(self):
        print("Problem accessing a Drobo: " + self.msg)


class FirmwareLacks(DroboException):
    """ raised, before anything is sent, for a request the unit's firmware
    does not implement (see FIRMWARE_IMPLEMENTS.)  Callers treat it like
    any refusal: it is a DroboException.
  """


# What a firmware version implements, from static analysis of its image
# (the dispatch tables for MODE SENSE page 0x3A sub-pages and for 0xEA
# system operations.)  Keyed on (major, minor) of the version string in
# sub-page 0x08.  Through the ATA tunnel, firmware 2.1.5 does not refuse a
# request it lacks: the tunnel write never completes, SG_IO times out and
# the kernel resets the USB link.  So on the tunnel, nothing outside these
# sets is sent (Drobo._fw_implements.)  Firmware not listed here is not
# gated.  See "Firmware capability gate" in DEVELOPERS.rst.
FIRMWARE_IMPLEMENTS = {
    (2, 1): {
        # 0x01 config, 0x02 capacity, 0x03 slots, 0x04 LUNs, 0x05 settings,
        # 0x06 protocol, 0x07 LUN info 2, 0x08 firmware, 0x09 status,
        # 0x11 disk pack, 0x15 LUN identity, 0x30/0x31 options,
        # 0x82 interface.  Lacks 0x33, 0x35 to 0x3A and 0x80.  (0xE1/0xE2
        # exist but operate on drives: left out on purpose.)
        'sub_pages': frozenset((0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
                                0x08, 0x09, 0x11, 0x15, 0x30, 0x31, 0x82)),
        # 0x01 to 0x18, 0x20, 0x21, 0x70/0x71 (firmware install/status) and
        # 0x80.  Lacks 0x22 (event log), 0x81 and 0x83 (LUN info ex/ex2.)
        'system_ops': frozenset(tuple(range(0x01, 0x19)) +
                                (0x20, 0x21, 0x70, 0x71, 0x80)),
    },
}


def _fwkey(version):
    """ (major, minor) from a firmware version string like '2.1.5', or
        None if it does not start with two dot separated numbers. """
    if not isinstance(version, str):
        return None
    m = re.match(r'(\d+)\.(\d+)', version.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _ledstatus(n):
    """ return colors decoded, given numeric slot status 
            goal: match what the lights are doing on the unit.

        STATUS: working with ERRATA:
        Developer Notes:
        The spec says: status is high order 3 bits, and starting from 0, goes:
                green, yellow, Red, ( Green Yellow )

        dmp.h says:
        forget the high order 3 bits, it's the whole byte.
        off is supposed to be 0, but doesn't match when flashing red green..
           off, red, yellow, green, yellow-green, red-green, red-off, red-yellow 

        my drobo says: 
                high order bits always 0. status seems to use whole byte.
                green is 3,  
                flashing red-green is 4...  which dmp.h says is yellow green
                flashing red-black is 6...  agrees with dmp.h
                0x80 is indeed an empty slot, as per dmp.h
                unknown-- yellow-green...
        matches neither... hmm...        
    """
    colourstats = [
        'black', 'red', 'yellow', 'green', ['red', 'green'], ['red', 'yellow'],
        ['red', 'black']
    ]
    if DEBUG & DBG_General:
        print('colourstats, argument n is: ', n)
    if (n == 0x80):  # empty
        return 'gray'
    return colourstats[n & 0x0f]


def _timeleft(minutes):
    """ format an estimate of the minutes left, the way Drobo Dashboard does:
        whole hours once an hour or more is left, otherwise whole minutes,
        never less than one minute.
    """
    minutes = max(1, int(minutes + 0.5))
    if minutes >= 60:
        n, unit = (minutes + 30) // 60, 'hour'
    else:
        n, unit = minutes, 'minute'
    return 'about %d %s%s left ' % (n, unit, '' if n == 1 else 's')


def _drivetype(code):
    """ (media, interface) for the disk type byte of sub-page 0x35:
        0 HDD/SATA, 1 HDD/SAS, 2 SSD/SATA, 3 SSD/SAS, 4 SSD/mSATA,
        255 empty slot.
    """
    if code == 255:
        return ('', '')
    return {0: ('HDD', 'SATA'), 1: ('HDD', 'SAS'), 2: ('SSD', 'SATA'),
            3: ('SSD', 'SAS'), 4: ('SSD', 'mSATA')}.get(code,
                                                       ('unknown', 'unknown'))


def _driverpm(code):
    """ spindle speed in RPM for the rotational speed byte of sub-page 0x35.
        The byte counts in steps of 200 RPM (36 is 7200 RPM); 1 means the
        drive does not spin (an SSD) and gives 0; 0 means the speed is
        unknown and gives None.
    """
    if code == 0:
        return None
    if code == 1:
        return 0
    return code * 200


def _rpmtext(rpm):
    """ short text for a speed from _driverpm: '7200', '-' for a drive that
        does not spin, '?' when unknown.
    """
    if rpm is None:
        return '?'
    return str(rpm) if rpm else '-'


def _temptext(temp):
    """ short text for the temperature byte of sub-page 0x35: '-' for 0,
        which means not populated (every slot of a Drobo 5D on firmware
        4.2.3 reads 0, per ReDrobo), else the number.
    """
    return str(temp) if temp else '-'


def _diskhealth(state):
    """ health from the low four bits of the disk state of sub-page 0x35. """
    return {0: 'Good', 1: 'Healed', 2: 'Warning', 3: 'Failed'}.get(state & 0x0f,
                                                                  'Unknown')


def _clockoffset(raw):
    """ the gmtOffset u16 of sub-page 0x05 as minutes east of UTC (read as
        signed, so zones west of UTC are negative: 0xfe20 is -480, UTC-8),
        or None when it is not a plausible offset (more than 14 hours
        either way), as with the bytes first generation units were seen to
        send (0x0800.)  Source: ReDrobo docs/PROTOCOL.md.
    """
    minutes = raw - 0x10000 if raw & 0x8000 else raw
    if abs(minutes) > 14 * 60:
        return None
    return minutes


def _clocktext(clock):
    """ text for a GetClock() dictionary: the unit's clock face and its UTC
        offset, e.g. '2026-08-19 16:19:07 (UTC+02:00)'. """
    off = clock['offset_minutes']
    if off is None:
        return '%s (UTC offset unknown)' % clock['local']
    return '%s (UTC%s%02d:%02d)' % (clock['local'], '-' if off < 0 else '+',
                                    abs(off) // 60, abs(off) % 60)


def vpd_serial(dev, sysfs='/sys'):
    """ the unit serial number (INQUIRY VPD page 0x80) of block device dev
        ('/dev/sdb' or 'sdb'), as the kernel cached it in
        <sysfs>/block/<dev>/device/vpd_pg80, or None if that file is absent,
        unreadable or not a serial number page.  Only reads that file: no
        SCSI command is sent (Linux usb-storage often does not fetch VPD
        pages, so expect None over USB.)
    """
    parts = (dev or '').split('/')
    name = parts[-1]
    if not name or '..' in parts or name == '.':
        return None
    try:
        with open(os.path.join(sysfs, 'block', name, 'device', 'vpd_pg80'),
                  'rb') as f:
            data = f.read(260)
    except (IOError, OSError):
        return None
    if len(data) < 4 or data[1] != 0x80:
        return None
    plen = struct.unpack('>H', data[2:4])[0]
    text = data[4:4 + plen].split(b'\0')[0].decode('latin-1').strip()
    return text or None


def _pagebody(out, sub_page):
    """ given bytes starting at a page 0x3A sub page header, return the sub
        page body (as long as its header says, or as much as arrived), or
        None if the header is not for sub_page.
    """
    if len(out) < 4 or (out[0] & 0x3f) != 0x3a or out[1] != sub_page:
        return None
    plen = struct.unpack('>H', out[2:4])[0]
    return out[4:4 + plen]


def _completion(eta):
    """ format the estimated completion time (seconds since the epoch) in local
        time: 'done around 15:26 ', or with the weekday if not today.
    """
    when = time.localtime(eta)
    today = time.localtime(time.time())
    if time.strftime('%Y%m%d', when) == time.strftime('%Y%m%d', today):
        return time.strftime('done around %H:%M ', when)
    return time.strftime('done around %a %H:%M ', when)


def _unitstatus(n):
    """ return string, given numeric unit status 
   
    STATUS: working with ERRATA:
        here is what the spec says should work (integers...):
    ust = [ 'Normal', 'Red Threshold Exceeded', 'Yellow Threshold Exceeded',
      'No Disks',  'Bad Disk',  'Multiple Disks Removed', 'No Redundancy', 
      'No Magic Hotspare', 'Unit Full', 'Relay Out in Progress', 
      'Format in Progress' ]
    return ust[n]   

    the .h files says... no stupid... it's bit fields.  I don't much care
    as long as folks make up their mind. I have a higher regard for the .h
    so that implementation is active.
    """
    f = []
    if (n & 0x0002):
        f.append('Red alert')
    if (n & 0x0004):
        f.append('Yellow alert')
    if (n & 0x0008):
        f.append('No disks')
    if (n & 0x0010):
        f.append('Bad disk')
    if (n & 0x0020):
        f.append('Too many missing disks')
    if (n & 0x0040):
        f.append('No redundancy')
    if (n & 0x0080):
        f.append('No magic hotspare')
    if (n & 0x0100):
        f.append('no space left')
    if (n & 0x0200):
        f.append('Relay out in progress')
    if (n & 0x0400):
        f.append('Format in progress')
    if (n & 0x0800):
        f.append('Mismatched disks')
    if (n & 0x1000):
        f.append('Unknown version')
    if (n & 0x2000):
        f.append('New firmware installed')
    if (n & 0x4000):
        f.append('New LUN available after reboot')
    # named by newer firmware / the vendor's software:
    if (n & 0x40000):
        f.append('Incompatible disk pack')
    if (n & 0x100000):
        f.append('Power supply missing')
    if (n & 0x200000):
        f.append('Fan partially failed')
    if (n & 0x400000):
        f.append('Fan failed')
    if (n & 0x800000):
        f.append('Fan missing')
    if (n & 0x2000000):  # named by ReDrobo
        f.append('Hybrid tiering state')
    if (n & 0x10000000
        ):  # was going to say 'Let us pray...', but no need to alarm people.
        f.append('Unknown error')

    return f


# names of status bits above bit 31, as published by the ReDrobo project
# (from the vendor's AlertMailer::GetEventIDForStatus.)  The vendor treats
# status as 64 bits wide, but sub-page 0x09 only gives drobo-utils 32 bits:
# where the high word would come from is unknown (the 24 byte record's extra
# word is NOT assumed to be it.)
_STATUS_HIGH_BITS = ((32, 'DroboShare alert'), (33, 'Drive added'),
                     (34, 'Drive removed'),
                     (35, 'Dual disk redundancy updated'),
                     (36, 'Volume usage over limit'),
                     (37, 'All volumes over limit'), (38, 'Target login'),
                     (39, 'Relayout complete'),
                     (41, 'Three SSDs and a disk required'))


def _unitstatus64(n):
    """ names for a 64 bit unit status: _unitstatus of the low word, then the
        named bits of the high word, in bit order.  Nothing reads a 64 bit
        status yet; this is for when a source of one is found.
    """
    f = _unitstatus(n & 0xffffffff)
    f.extend(name for bit, name in _STATUS_HIGH_BITS if n & (1 << bit))
    return f


# status severity, the vendor's own rule (getSeverityForStatus, as published
# by the ReDrobo project): any bit of the red mask, tested 32 bits wide, is
# red; otherwise any bit of the yellow mask, tested 64 bits wide, is yellow;
# otherwise green.  With a 32 bit status only the low word of the yellow mask
# applies.  Bits 15 and 17, set on a healthy unit, are in neither mask.
_SEVERITY_RED = 0x14c4187a
_SEVERITY_YELLOW = 0x36002300244


def _statusseverity(status):
    """ 'red', 'yellow' or 'green' for a numeric unit status (32 or 64 bits.)
    """
    if status & 0xffffffff & _SEVERITY_RED:
        return 'red'
    if status & _SEVERITY_YELLOW:
        return 'yellow'
    return 'green'


# redundancy level, derived from capacities as the ReDrobo project describes
# (the unit does not report it): BeyondRAID holds back the largest disk for
# single disk redundancy and the two largest for dual.  The held back space
# also carries some overhead (0.5% of it on ReDrobo's 5D), so the ratio is
# accepted within this window round 1.0.  The two hypotheses are at least a
# factor of (1 + second largest / largest) apart, 2 for equal disks, so 5%
# leaves room for overhead without confusing them.
_REDUNDANCY_WINDOW = 0.05


def _redundancylevel(capacities, usable):
    """ 'single', 'dual' or 'undetermined', given the raw capacities of the
        installed disks (bytes; empty slots as 0, accelerator excluded) and
        the usable protected total (bytes.)

        held back = sum(capacities) - usable.  single if held back / largest
        is within _REDUNDANCY_WINDOW of 1, dual if held back / (two largest)
        is; undetermined if neither or both fit, or the figures make no sense.
    """
    caps = sorted((c for c in capacities if c), reverse=True)
    if not caps or not usable or usable <= 0:
        return 'undetermined'
    held = sum(caps) - usable
    if held <= 0:
        return 'undetermined'

    def fits(ratio):
        return abs(ratio - 1.0) <= _REDUNDANCY_WINDOW

    single = fits(held / caps[0])
    dual = len(caps) >= 2 and fits(held / (caps[0] + caps[1]))
    if single != dual:
        return 'single' if single else 'dual'
    return 'undetermined'


def _partformat(n):
    """ return Drobo's idea of what the partition type is

        STATUS: working 
        
        worrisome... when multiple partitions present of different types, the
       bit field is set to: NO FORMAT.

    """
    f = []

    if (n & 0x01):
        f.append('NO FORMAT')
    if (n & 0x02):
        f.append('NTFS')
    if (n & 0x04):
        f.append('HFS')
    if (n & 0x80):  # on Peter's...
        f.append('EXT3')
    if (n & 0x08):  # on Matthew Mastracci's
        f.append('EXT3')
    if (n & 0xFF) == 0:
        f.append('FAT32')

    if (len(f) != 1):
        print('hoho! multiple partition types! Brave are we not?')

    return f


def _partscheme(n):
    """ return what drobo thinks the partition scheme is

       STATUS: working, no issues.
       Have set all of them on my drobo using parted, was detected correctly.
   """
    if (n == 0):
        return "No Partitions"
    if (n == 1):
        return "MBR"
    if (n == 2):
        return "APM"
    if (n == 3):
        return "GPT"


def _unitfeatures(norig):
    """ return a list of features supported by a unit
    
        STATUS: working.
        this comes straight from dmp.h.. what they mean isn't documented.
        stuff that looks wrong:        
                -- USED_CAPACITY... should be USE_CAPACITY ... no 'D'
                -- all the 'SUPPORTS' texts are redundant. every bit is about
                   what is supported.  It should be just LUNINFO2
         etc...                        
         feature x0800 and 0x2000 show up on my Drobo v1 running 1.3.0...
         of course, do not know what they are...
    """
    n = norig
    feature_map = [(0x0001, 'NO_AUTO_REBOOT'), (0x0002, 'NO_FAT32_FORMAT'),
                   (0x0004, 'USED_CAPACITY_FROM_HOST'),
                   (0x0008, 'DISKPACKSTATUS'), (0x0010, 'ENCRYPT_NOHEADER'),
                   (0x0020, 'CMD_STATUS_QUERIABLE'),
                   (0x0040, 'VARIABLE_LUN_SIZE_1_16'),
                   (0x0080, 'PARTITION_LUN_GPT_MBR'),
                   (0x0100, 'FAT32_FORMAT_VOLNAME'),
                   (0x0200, 'SUPPORTS_DROBOSHARE'),
                   (0x0400, 'SUPPORTS_NEW_LUNINFO2'),
                   (0x0800, 'feature x0800'), (0x1000, 'LUN_MANAGEMENT'),
                   (0x2000, 'feature x2000'), (0x4000, 'SUPPORTS_OPTIONS2'),
                   (0x8000, 'SUPPORTS_SHUTDOWN'), (0x10000, 'feature x10000'),
                   (0x20000, 'SUPPORTS_ISCSI'), (0x40000, 'feature x40000'),
                   (0x80000, 'feature x80000'),
                   (0x800000, 'SUPPORTS_LUNINFO_EX2'),
                   (0x40000000, 'SUPPORTS_VOLUME_RENAME'),
                   (0x80000000, 'SUPPORTS_SINGLE_LUN_FORMAT')]
    f = []
    for feature in feature_map:
        #print "checking for %s %04x in %04x: " % ( feature[1], feature[0], n )
        if n & feature[0]:
            n = n & ~feature[0]
            f.append(feature[1])
    if n != 0:
        f.append("leftovers (0x%04x)" % n)

    return f


class Drobo:
    """
  Drobo the class for data exchange with the storage units.
  
  briefly, communicates using SCSI sub_page blocks. 
  There are member functions for each page type...

  See the DMP Spec for details.

  All capacity returned are in bytes.

  the rest of this comment is destined only for Drobo.py developers, not users:
  DMP Spec ERRATA, by subpage:
         - in config, last Q is capacity, manual says bytes, sample code 
           says *512, which works better... so actual quantity is 512 byte blocks.
         - in slotinfo, 
            'managed capacity' is always 0, is that OK? 

         - in version, seems to want an extra byte. BB fails, but BBB works... returns  
         - in settings, the 32 byte name starts with a null byte before 
            'TRUSTED DATA', is that normal?
  """
    def __init__(self, chardevs, debugflags=0):
        """ chardev is /dev/sdX... 
         the character device associated with the drobo unit
     """
        global DEBUG

        DEBUG = debugflags

        if DEBUG & DBG_Instantiation:
            print('__init__ ')

        self.fd = None

        if type(chardevs) is list:
            self.char_dev_file = chardevs[0]
            self.char_devs = chardevs
        else:
            self.char_dev_file = chardevs
            self.char_devs = [chardevs]

        self.features = []
        self.transactionID = random.randint(1, MAX_TRANSACTION)

        self.relaystart = 0
        self.relayinitialcount = 0
        self.relayout_estimate = None
        self.name = ''
        self.model = 'unknown'  # from INQUIRY, see DroboIOctl.identify_drobo

        # 'legacy': page 0x3A comes back raw (first generation Drobos...)
        # 'dbd': page follows a mode parameter header (Drobo S Gen 2, USB 3)
        self.mode_sense_style = 'legacy'

        # set when the unit refuses sub-page 0x07 (see GetSubPageLUNs)
        self.luninfo2_refused = False

        if DEBUG & DBG_Simulation:
            self.GetSubPageFirmware() \
            #for the side effect, self.fw & self.features get set
            self.GetSubPageConfig()  # side effect: sets self.slot_count
            self.inquiry = self.inquire()

        else:
            self.fd = DroboIOctl.DroboIOctl(self.char_dev_file, 0, debugflags)
            if self.fd == None:
                raise DroboException

            # more thorough checks for Drobohood...
            # for some reason under ubuntu intrepid, start getting responses of all bits set.
            # need some ways to spot a real drobo.
            cfg = self.GetSubPageConfig()
            #Side effect: Config, sets self.slot_count...

            self.inquiry = self.inquire()
            self.model = DroboIOctl.identify_drobo(
                self.inquiry[8], self.inquiry[9]) or 'unknown'

            if DEBUG & DBG_Detection:
                print("cfg: ", cfg)

            if (len(cfg) != 3):  # We ask this page to return three fields...
                if DEBUG & DBG_Detection:
                    print("%s length of cfg is: %d, should be 3" %
                          (self.char_dev_file, len(cfg)))
                raise DroboException

            if (self.slot_count < 4):
                if DEBUG & DBG_Detection:
                    print(
                        "%s cfg[0] = %s, should >= 4. All Drobos have at least 4 slots"
                        % (self.char_dev_file, cfg[0]))
                raise DroboException  # Assert: All Drobo have 4 slots.

            set = self.GetSubPageSettings()
            self.name = set[2]
            if DEBUG & DBG_Detection:
                print("settings: ", set)

            self.GetSubPageFirmware()
            if (len(self.fw) < 8) and (len(self.fw[7]) < 5):
                if DEBUG & DBG_Detection:
                    print("%s length of fw query: is %d, should be < 8." %
                          (self.char_dev_file, len(self.fw)))
                    print("%s len(fw[7]) query: is %d, should be < 5." %
                          (self.char_dev_file, len(self.fw[7])))
                raise DroboException

            if (self.fw[6].lower() != 'armmarvell'):
                print("interesting, %s fw[6] is: %s" %
                      (self.char_dev_file, self.fw[6]))
            #if ( self.fw[6].lower() != 'armmarvell' ):
            #    if DEBUG & DBG_Detection:
            #      print "%s fw[6] is not armmarvell." % self.char_dev_file
            #    raise DroboException

    def __del__(self):

        if DEBUG & DBG_Instantiation:
            print('__del__ ')

        if self.fd != None:
            self.fd.closefd()

        self.fd = None

    def format_script(self, fstype='ext3'):
        """ return a shell script giving the code to commands required to 
     format the single LUN represented by a given drobo...
     
     pscheme is one of: mbr, apt, gpt,  
          gpt - >= 2 TB, linux or windows...
          mbr <= 2 TB good for FAT32

     fstype is one of:  fat32, ext3, ntfs (HPS+ is not supported on linux)
     sizes in terabytes

     not implemented yet...
     each lun is formatted as a single primary partition.
     partitions are never marked bootable.

     need to detect each lun...

     algorithm:
        do discoverLUNs()
        figure out which LUNs belong to the current Drobo... How?
          -- do not want to partition LUNS on another Drobo!
             That would be bad!

        for l in LUN:
            parted /dev/sd?? mklabel gpt mkpart pri ext3 0 -1
            mke2fs -j ... /dev/sd??

     """

        format_script = '/tmp/fmtscript'
        fd = open(format_script, 'w')
        fd.write("#!/bin/sh\n")

        if fstype == 'FAT32' or fstype == 'msdos':
            ptype = 'msdos'
        else:
            ptype = 'gpt'

        for cd in self.char_devs:
            fd.write("parted -s %s mklabel %s\n" % (cd, ptype))

            # there is a bit of a race condition creating the partition special file.
            # the parted print gives a little time before starting the mkfs, to ensure
            # file is there...

            if fstype == 'ext3':
                # -m 0 -- Drobo takes care of complaining when near the size limit.
                #         little point in complaining early, pretending you have less space.
                # ^resize_inode -- Drobo makes the file system much bigger than the actual space
                #       available ( >= 2TB ) , so it doesn't make sense to allow space for future
                #       inode expansion beyond that.  only makes sense in LVM, that's why this
                #       option is 'off' (the '^' at the beginning.)
                # sparse_super -- there are lots too many superblock copies made by default.
                #       safe enough with fewer.
                fd.write("parted -s %s mkpart primary ext3 0 100%%\n" % cd)
                fd.write("parted -s %s print; sleep 5\n" % cd)
                fd.write(
                    'mke2fs -j -i 262144 -L Drobo01 -m 0 -O sparse_super,^resize_inode %s1\n'
                    % cd)
            elif fstype == 'ntfs':
                fd.write("parted -s %s mkpart primary ntfs 0 100%%\n" % cd)
                fd.write("parted -s %s print; sleep 5\n" % cd)
                fd.write('mkntfs -f -L Drobo01  %s1\n' % cd)
            elif fstype == 'FAT32' or fstype == 'msdos':
                fd.write("parted -s %s mkpart primary fat32 0 100%%\n" % cd)
                fd.write("parted -s %s print; sleep 5\n" % cd)
                fd.write('mkdosfs -v -v -F 32 -S 4096 -n Drobo01 %s1\n' % cd)
            else:
                print('unsupported  partition type %s, sorry...' % fstype)

        fd.close()
        os.chmod(format_script, 0o700)

        return format_script

    def _fw_implements(self, kind, code):
        """ False when a request must not be sent: it would go through the
        ATA tunnel, and the unit's firmware (as read from sub-page 0x08) is
        in FIRMWARE_IMPLEMENTS without it.  kind: 'sub_pages' (page 0x3A
        sub-page) or 'system_ops' (0xEA operation.)  True otherwise,
        including before the firmware version is known.
        """
        if getattr(self.fd, 'tunnel', False) is not True:
            return True
        fw = getattr(self, 'fw', None)
        if not fw or len(fw) < 8:
            return True
        table = FIRMWARE_IMPLEMENTS.get(_fwkey(fw[7]))
        if table is None:
            return True
        return code in table[kind]

    def __gate(self, kind, code):
        """ raise FirmwareLacks unless _fw_implements(kind, code). """
        if self._fw_implements(kind, code):
            return
        what = {'sub_pages': 'sub_page', 'system_ops': '0xEA op'}[kind]
        if DEBUG & DBG_HWDialog:
            print('firmware %s lacks %s 0x%02x: not sent' %
                  (self.fw[7], what, code))
        raise FirmwareLacks("firmware lacks %s 0x%02x" % (what, code))

    def __getsubpage(self, sub_page, pack):
        """Retrieve Sub page from drobo block device.
       Uses DroboIOctl class to run the raw ioctl.

       sub_page: selection code from DMP Spec...
       pack: the pattern of fields in the subpage...

       returns: a list unpacked according to the  structure 

       Each sub_page has a standard header:
           pack is prepended with a standard header '>BBH'
       Drobo is bigendian.
       first byte has some flags and a vendor code.
       second byte is the sub-page code.
       following two bytes have the length of the record (max: 65535)
    """
        if DEBUG & DBG_HWDialog:
            print('getsubpage')

        if DEBUG & DBG_Simulation:
            return ()

        self.__gate('sub_pages', sub_page)
        mypack = '>BBH' + pack
        paklen = struct.calcsize(mypack)
        if DEBUG & DBG_RawReturn:
            print('DMIP sub_page query:0x%02x pattern: %s ' %
                  (sub_page, mypack))

        cmdout = None
        if self.mode_sense_style == 'legacy':
            modepageblock = struct.pack(">BBBBBBBHB", 0x5a, 0, 0x3a, sub_page,
                                        0, 0, 0, paklen, 0)

            cmdout = self.fd.get_sub_page(paklen, modepageblock, 0, DEBUG)

            if len(cmdout) != paklen:
                if DEBUG & DBG_HWDialog:
                    print('short legacy reply, trying mode parameter header')
                cmdout = self.__getsubpage_dbd(sub_page, paklen)
                if cmdout is not None:
                    self.mode_sense_style = 'dbd'
        else:
            cmdout = self.__getsubpage_dbd(sub_page, paklen)

        if cmdout is None:
            print('expected %d bytes for sub_page 0x%02x' % (paklen, sub_page))
            raise DroboException("cmdout is unexpected length")


#    print 'Pack: ' + mypack

        result = struct.unpack(mypack, cmdout)
        if DEBUG & DBG_HWDialog:
            print('4 byte returned sense buffer header: 0x%x, 0x%x, 0x%x' %
                  result[:3])
        if DEBUG & DBG_RawReturn:
            print('DMIP response sub_page:0x%02x returned: %s ' %
                  (sub_page, str(result[3:])))
        return result[3:]

    def __getsubpage_dbd(self, sub_page, paklen):
        """ Retrieve a sub page using a standard MODE SENSE(10), as the Drobo S
        Gen 2 (USB 3) expects.  Asked the legacy way, that unit returns only
        a mode parameter header (00 06 00 00 00 00 00 00) and no page.

        Here DBD (disable block descriptors, byte 1 bit 3) is set, room is left
        for the 8 byte mode parameter header (and any block descriptors the
        device sends anyway), the header is skipped, and the page header is
        checked before the page is returned.

        returns: the paklen bytes of the sub page, or None.
        """
        alloc = paklen + 8 + 16  # header + allowance for a long LBA descriptor
        modepageblock = struct.pack(">BBBBBBBHB", 0x5a, 0x08, 0x3a, sub_page, 0,
                                    0, 0, alloc, 0)
        try:
            out = self.fd.get_sub_page(alloc, modepageblock, 0, DEBUG)
        except IOError:
            return None

        if len(out) < 8:
            return None
        bdl = struct.unpack('>H', out[6:8])[0]
        page = out[8 + bdl:8 + bdl + paklen]

        if (len(page) != paklen) or ((page[0] & 0x3f) != 0x3a) or \
           (page[1] != sub_page):
            if DEBUG & DBG_HWDialog:
                print('DBD reply for sub_page 0x%02x not understood' % sub_page)
            return None
        return page

    def __transactionNext(self):
        """ Increment the transaction member for some modeSelect pages.
    """
        if (self.transactionID > MAX_TRANSACTION):
            self.transactionID = 0
        self.transactionID = self.transactionID + 1

    def __issueCommand(self, command):
        """ issue a command to a Drobo...
     0x06 - blink.
     0x0d - Standby

     returns nothing, look at the drobo to see if it worked.
     note: command is asynchronous, returns before operation is complete.
    """

        if DEBUG & DBG_HWDialog:
            print('issuecommand...')

        if DEBUG & DBG_Simulation:
            self.__transactionNext()
            return

        modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x00, command,
                                    0x00, self.transactionID, 0x01 << 5, 0x01,
                                    0x00)

        # the CDB says host to device, length 1: get_sub_page with out=1
        # sends its one byte, zeroed buffer as that data-out.  A mismatch
        # (no data-out) is a USB phase error, and the host resets the unit.
        # See "0xEA framing" in DEVELOPERS.rst.
        try:
            cmdout = self.fd.get_sub_page(1, modepageblock, 1, DEBUG)

        except:
            print(
                'IF you see, "bad address", it is because you need to be the super user...'
            )
            print(" try sudo or su ...       ")
            sys.exit()

        self.__transactionNext()

        if (len(cmdout) != 1):
            raise DroboException
        # only way to verify success is to look at the Drobo...

    def Sync(self, NewName=None):
        """  Set the Drobo's current time to the host's time,
         and the name to selected value.

     STATUS: works, maybe...
        DRI claims Drobos are all in California time.  afaict, it ignores TZ completely.
        I feed it UTC, and when I read the time, normal routines convert to local time.
        so it looks perfect.  but diagnostics might not agree.
    """
        if DEBUG & DBG_Simulation:
            return

        now = int(time.time())
        payload = "LH32s"
        payloadlen = struct.calcsize(">" + payload)
        if NewName == None:
            NewName = self.GetSubPageSettings()[2]
        if isinstance(NewName, str):
            # the 32s field needs bytes; struct truncates/pads to 32.
            NewName = NewName.encode('ascii')

        buffer = struct.pack(">BBH" + payload, 0x7a, 0x05, payloadlen, now, 0,
                             NewName)
        sblen = len(buffer)

        # mode select CDB.
        modepageblock = struct.pack(">BBBBBBBHB", 0x55, 0x01, 0x7a, 0x05, 0, 0,
                                    0, sblen, 0)
        self.fd.put_sub_page(modepageblock, buffer, DEBUG)

    def SetOptions(self, options):
        """ Set Options.
        accepts a set of options as returned by GetOptions

    STATUS: working. 
         basic thresholds work on Drobo v1.
         OPTIONS2 tested by folks on the internet.  Seem OK.

    The byte at +6 of sub-page 0x30 packs settings on newer units (use
    unprotected capacity, realtime and background data check; ReDrobo), so
    it is written back unchanged: options["OptionsFlags"] when present (as
    GetOptions returns it), else read now.  Units that report no such byte
    get 0 there, as they always have.  It is never set from user input.

    On OPTIONS2 units without iSCSI, GetOptions gives no UseStaticIPAddress,
    IPAddress or NetMask; whichever of those the dict lacks is written back
    as the unit reports it now (sub-page 0x31, read before any write.)
    """
        if DEBUG & DBG_Simulation:  # nothing to write to
            return

        flags = options.get("OptionsFlags")
        if flags is None:
            try:
                current = self.GetOptions()
            except (IOError, DroboException):
                current = None
            flags = (current or {}).get("OptionsFlags", 0)

        unit_fields = ('UseStaticIPAddress', 'IPAddress', 'NetMask')
        if 'SUPPORTS_OPTIONS2' in self.features and \
           not all(k in options for k in unit_fields):
            o2 = self.__getsubpage(0x31, 'QHLLLHLLLH')  # as GetOptions
            options = dict(options)
            options.setdefault('UseStaticIPAddress', (o2[0] & 0x0008) > 0)
            options.setdefault('IPAddress',
                               socket.inet_ntoa(struct.pack('>L', o2[2])))
            options.setdefault('NetMask',
                               socket.inet_ntoa(struct.pack('>L', o2[3])))

        # v1 Options first...
        fmt = 'BBBLBB'
        payloadlen = struct.calcsize(">" + fmt)
        buffer = struct.pack(">BBH" + fmt, 0x7a, 0x30, payloadlen,
                             options["YellowThreshold"],
                             options["RedThreshold"], flags, 0, 0, 0)
        sblen = len(buffer)
        modepageblock = struct.pack(">BBBBBBBHB", 0x55, 0x01, 0x7a, 0x30, 0, 0,
                                    0, sblen, 0)
        self.fd.put_sub_page(modepageblock, buffer, DEBUG)

        if ('SUPPORTS_OPTIONS2' in self.features):
            ip = struct.unpack('I', socket.inet_aton(options['IPAddress']))[0]
            nm = struct.unpack('I', socket.inet_aton(options['NetMask']))[0]
            rawip = socket.htonl(ip)
            rawnm = socket.htonl(nm)
            flags = 0
            if (options["DualDiskRedundancy"]):
                flags |= 0x0001
            if (options["SpinDownDelayMinutes"] > 0):
                flags |= 0x0002
            if (options["UseManualVolumeManagement"]):
                flags |= 0x0004
            if (options["UseStaticIPAddress"]):
                flags |= 0x0008

            fmt = 'QHLLB'
            payloadlen = struct.calcsize(">" + fmt)
            # the ffff mask is there to suppress python 2.5 warning:
            #  DeprecationWarning: struct integer overflow masking is deprecated
            # which is quite strange because not a problem in 2.6.
            buffer = struct.pack(">BBH" + fmt, 0x7a, 0x31, payloadlen, \
              flags, options["SpinDownDelayMinutes"], \
               0xffffffff&rawip, 0xffffffff&rawnm, 0 )
            sblen = len(buffer)
            modepageblock=struct.pack( ">BBBBBBBHB", 0x55, 0x01, 0x7a, \
              0x31, 0, 0, 0, sblen, 0)
            self.fd.put_sub_page(modepageblock, buffer, DEBUG)

        return

    def SetLunSize(self, tb):
        """
       SetLunSize - Sets the maximum LUN size to 'tb' terabytes

       status:  works with no issues!
    """
        if DEBUG & DBG_Simulation:  # nothing to write to
            return

        if (DEBUG & DBG_Chatty):
            print('set lunsize to %d TiB' % tb)

        if not self.umount():
            if (DEBUG & DBG_Chatty):
                print('cannot free up Drobo to set lunsize')
            return

        buffer = struct.pack(">l", tb)
        sblen = len(buffer)

        # mode select CDB.
        modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x0, 0x0f, 0,
                                    self.transactionID, (0x01 << 5) | 0x01,
                                    sblen, 0x00)

        self.fd.put_sub_page(modepageblock, buffer, DEBUG)
        self.__transactionNext()

    def SlotCount(self):
        self.GetSubPageConfig()
        return self.slot_count

    def Blink(self):
        """ asks the Drobo nicely to blink it's lights. aka. Identification 
        If you happen to have five in a row (drool), you can know which is which.

        STATUS: works, no issues.
    """
        self.__issueCommand(6)

    def Standby(self):
        """ asks the Drobo nicely to shutdown, flushing all manner of caches.

        STATUS: command itself works, no issues.... only light tests so far.
                still testing umount code.
    """
        if self.umount():
            self.__issueCommand(0x0d)

    def GetDiagRecord(self, diagcode, decrypt=0):
        """ returns diagnostics as a string...
        diagcodes are either 4 or 7 for the two different Records available.

        STATUS: works fine.

        decryption done in drobom (look at diagprint)
    """
        if DEBUG & DBG_Chatty:
            print("Dumping Diagnostics...")

        # tried 32000 ... it only returned 5K, so try something small.
        buflen = 4096

        modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x80, diagcode,
                                    0x00, self.transactionID,
                                    (0x01 << 5) | 0x01, buflen, 0x00)

        todev = 0

        if DEBUG & DBG_General:
            print("Page 0...")

        cmdout = self.fd.get_sub_page(buflen, modepageblock, todev, DEBUG)
        diags = cmdout
        i = 0
        while len(cmdout) == buflen:
            modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x80,
                                        diagcode, 0x00, self.transactionID,
                                        0x01, buflen, 0x00)

            cmdout = self.fd.get_sub_page(buflen, modepageblock, todev, DEBUG)
            i = i + 1
            diags = diags + cmdout

            if DEBUG & DBG_General:
                print("diags ", i, ", cmdlen=", len(cmdout), " diagslen=",
                      len(diags))

        return diags

    def dumpDiagnostics(self):

        n = time.gmtime()

        dfname = "/tmp/DroboDiag_%d_%02d%02d_%02d%02d%02d.log" % (n[0:6])
        df = open(dfname, "wb")
        d = self.GetDiagRecord(4)
        df.write(d)
        d = self.GetDiagRecord(7)
        self.__transactionNext()
        df.write(d)
        df.close()
        return dfname

    def decodeDiagnostics(self, diagfilename):
        try:
            f = open(diagfilename, 'rb')
            data = f.read()
            f.close()
        except:
            return ''

        # one byte xor; each decrypted byte is one character (latin-1).
        key = data[0] ^ 0x2d
        datam = bytes([x ^ key for x in data]).decode('latin-1')
        return datam

    #
    # constants for use with firmware operations
    #

    fwsite = "ftp://updates.drobo.com/"
    localfwrepository = os.path.expanduser("~") + "/.drobo-utils"

    def localFirmwareRepository(self):
        return Drobo.localfwrepository

    def PickFirmware(self, name):
        """
       read in a given firmware from disk.

       sets self.fwdata

    """
        if (DEBUG & DBG_Chatty):
            print('Reading Firmware from = %s' % name)

        if (name[-1] == 'z'):  # data is zipped...
            inqw = self.inquiry
            hwlevel = inqw[10].decode('latin-1')  # INQUIRY revision is bytes
            z = zipfile.ZipFile(name, 'r')
            for f in z.namelist():
                if (DEBUG & DBG_General):
                    print(f, ' ? ')
                    print('firmware for hw rev ', f[-5], ' this drobo is rev ',
                          hwlevel[0])
                if f[-5] == hwlevel[0]:
                    self.fwdata = z.read(f)
        else:  # old file...
            f = open(name, 'rb')
            self.fwdata = f.read()
            f.close()

        good = self.validateFirmware()
        return good

    def inquire(self):
        """ 
     issue a standard SCSI INQUIRY command, return standard response as tuple.

     STATUS: works.

     trying to understand how to send an INQUIRY:
     SCSI version 2 protocol  INQUIRY...
     protocol : T10/1236-D Revision 20

     byte:  0 - descriptor code.                0x12 -- INQUIRY
            1 - 00 peripheral dev. type code    0x0, 4, 5, 7, e .. 
            2 - reserved
            3 - reserved.
            4-27   target descriptor parameters.
                 0 - target descriptor type code: 0x04 - Identification.

            28-31  dev. type params.

    """
        if DEBUG & DBG_Simulation:  # a Drobo v1's answer, DroboPro length
            return (0, ) * 8 + (b'TRUSTED ', b'Mass Storage    ', b'1.00')

        dpropack = '>BBBBBBBB8s16s4s'
        mypack = dpropack + '20sBB8HH'
        paklen = struct.calcsize(mypack)

        modepageblock = struct.pack("BBBBBB", 0x12, 0, 0, 0, paklen, 0)

        cmdout = self.fd.get_sub_page(paklen, modepageblock, 0, DEBUG)
        if (len(cmdout) == paklen):
            ret = struct.unpack(mypack, cmdout)
        else:
            if (len(cmdout) == 36):  # we have a PRO...
                ret = struct.unpack(dpropack, cmdout)
            else:
                print(
                    'warning: scsi inquire returned %d, bytes instead of %d expected.'
                    % (len(cmdout), paklen))
                raise DroboException
        if DEBUG & DBG_RawReturn:
            print("inquiry response: ", str(ret))
        return ret

    def PickLatestFirmware(self):
        """
       fetch firmware from web site. ... should be a .tdf or .tdz file.
       validate firmware from header...
 
       sets self.fwdata

       go to fwsite
       download index.txt
       figure out which one to download.
       return arch and version of firmware running, and the download file name.
       SCSI INQUIRY is supposed to respond with 'VERSION' 1.0 or 2.0 to tell which to use.
       tdz support: zip file containing two .tdf's.  one for rev1, another for rev2.

     status: works 


    """
        if DEBUG & DBG_Simulation:
            return ('fwarch', 'fwv', 'hwlevel', 'value')

        inqw = self.inquiry
        hwlevel = inqw[10]
        fwv = str(self.fw[0]) + '.' + str(self.fw[1]) + '.' + str(self.fw[2])

        #FIXME ugly hack to force v1 to get onto the v2 firmware stream
        #  current dri index.txt file says v1's should run 1.1.2, but win/Mac dashboards upgrade
        #  to 1.2.4 anyways...  so I guess linux should too.
        #  if a v1 is running 1.1.2, then just claim to be an early v2 firmware, all should work.
        #if fwv == "1.200.11177":
        #   fwv="1.201.12942"

        fwarch = self.fw[6].lower()

        if (DEBUG & DBG_Chatty):
            print('looking for firmware for:', fwarch, fwv, 'hw version:',
                  hwlevel)
        listing_file = urllib.request.urlopen(Drobo.fwsite + "index.txt")
        list_of_firmware_string = listing_file.read().decode().strip("\t\r")
        list_of_firmware = list_of_firmware_string.split("|")
        i = 1
        p = re.compile('\[(.*)\]')
        while i < len(list_of_firmware):
            key = list_of_firmware[i - 1].split()[1]
            value = list_of_firmware[i].split()[1]

            k = key.split('/')

            # profits oblige...
            if k[2] == "licensed":
                k = k[0:2] + k[3:]
                #print k

            # these If's are now nested for ease of debugging, insert a print to taste...
            # the algorithm is wrong wrt, other platforms...
            if k[2] == "firmware":
                #print '    match firmware'
                if k[3] == fwarch:
                    #print '    match k[3] = ', fwarch
                    #if we are on a line that lists the fwversion in [] fixup k[4]
                    m = p.search(list_of_firmware[i - 1])
                    if m:
                        k[4] = m.group(1)
                    if k[4] == fwv:
                        if len(k) > 4:
                            if (DEBUG & DBG_Chatty):
                                print('This Drobo should be running: ', value)
                            return (fwarch, fwv, hwlevel, value)
            i = i + 2
        if (DEBUG & DBG_Chatty):
            print(
                'no matching firmware found, must be the latest and greatest!')
        return ('', '', '', '')

    def downloadFirmware(self, fwname, localfw):
        """
      download given fw file from network repository.
      load self.fwdata with the data from it

      STATUS: works.
    """
        if (DEBUG & DBG_Chatty):
            print('downloading firmware ', fwname, '...')
        self.fwdata = None
        firmware_url = urllib.request.urlopen(Drobo.fwsite + fwname)
        filedata = firmware_url.read()
        f = open(localfw, 'wb')
        f.write(filedata)
        f.close()
        if (DEBUG & DBG_Chatty):
            print('local copy written')

        if (fwname[-1] == 'z'):  # data is zipped...
            self.PickFirmware(localfw)
        else:
            self.fwdata = filedata

        if (DEBUG & DBG_Chatty):
            print('downloading done ')
        return self.fwdata

    def validateFirmware(self):
        """
       requires self.fwdata to be initialized before calling.
       read in the header of the firmware from self.fwdata.

       check the information in the header to confirm that it is a valid firmware image.

       status:
            works for length, and body CRC.  something wrong with header CRC.

       according to dpd.h:
       (hdrlength, hdrVersion, magic, imageVersion, targetName, sequenceNum, bootFailureCount, imageFlashAddress, imageLength, imageCrc32, about, hdrCrc32 ) = struct.unpack('llll16slllll256sl', self.fwdata[0:304])

       & 0xffffffffL is a kludge to work around CRC32 returning different values on 32 vs. 64 bit platforms.
       can remove once py3k arrives.  (see: http://bugs.python.org/issue1202)

       STATUS: working.

    """
        if (DEBUG & DBG_Chatty):
            print('validateFirmware start...')
        self.fwhdr = struct.unpack('>ll4sl16slllll256sl', self.fwdata[0:312])

        if len(self.fwdata) != (self.fwhdr[0] + self.fwhdr[8]):
            print('header corrupt... Length does not validate.')
            return 0

        if (DEBUG & DBG_Chatty):
            print('header+body lengths validated.  Good.')
        #print self.fwhdr

        if self.fwhdr[2] != b'TDIH':
            print('bad Magic, not a valid firmware')
            return 0

        if (DEBUG & DBG_Chatty):
            print('Magic number validated. Good.')
            print('%d + %d = %d length validated. Good.' %
                  (self.fwhdr[0], self.fwhdr[8], len(self.fwdata)))

        # http://bugs.python.org/issue1202
        # doesn't work on 64 bit, only on 32bit... weird...
        blank = struct.pack('i', 0)
        hdrcrc = zlib.crc32(self.fwdata[0:308] + blank +
                            self.fwdata[312:self.fwhdr[0]]) & 0xffffffff
        r = self.fwhdr[11] & 0xffffffff

        if (DEBUG & DBG_Chatty):
            print(
                'CRC from header: %d, calculated using python zlib crc32: %d '
                % (r, hdrcrc))
        if r != hdrcrc:
            print('file corrupt, header checksum wrong')
            return 0
        bodycrc = zlib.crc32(self.fwdata[self.fwhdr[0]:]) & 0xffffffff
        q = self.fwhdr[9] & 0xffffffff

        if (DEBUG & DBG_Chatty):
            print('CRC for body from header: %d, calculated: %d ' %
                  (q, bodycrc))
        if q != bodycrc:
            print('file corrupt, payload checksum wrong')
            return 0

        if (DEBUG & DBG_Chatty):
            print('32 bit Cyclic Redundancy Check correct. Good.')
            print('validateFirmware successful...')

        return 1

    def updateFirmwareRepository(self):
        """
      put the best firmware in self.fwdata.
            return 1 if successful, 0 if not.

      determine currently running firmware and most current from web site.

      check if the most current one has already been downloaded.
      if not, then download it into a local repository.

      Compare the current running firmware against the appropriate file 
      in the local repository

      STATUS: working.

    """
        (fwarch, fwversion, hwlevel, fwpath) = self.PickLatestFirmware()
        if fwarch == '':  # already at latest version...
            return 0

        if not os.path.exists(Drobo.localfwrepository):
            os.mkdir(Drobo.localfwrepository)

        fwname = fwpath.split('/')
        if isinstance(hwlevel, bytes):  # INQUIRY revision; str in simulation
            hwlevel = hwlevel.decode()
        localfw = Drobo.localfwrepository + '/' + fwarch + '_' + hwlevel + '_' + fwname[
            -1]
        if (DEBUG & DBG_Chatty):
            print('looking for: %s' % localfw)
        if DEBUG & DBG_Simulation:  # no download, nothing to write
            return 0
        if not os.path.exists(localfw):
            if (DEBUG & DBG_Chatty):
                print('not present, fetching from drobo.com')
            self.fwdata = self.downloadFirmware(fwpath, localfw)
            good = self.validateFirmware()
            if not good:
                print('downloaded firmware did not validate.')
                return 0
        else:
            if (DEBUG & DBG_Chatty):
                print('local copy already present:', localfw)
            good = self.PickFirmware(localfw)

        if good:
            if (DEBUG & DBG_Chatty):
                print('correct fw available')
            return 1

        print('no valid firmware found')
        return 0

    def writeFirmware(self, function):
        """
        given good firmware data, upload it to the Drobo...

        1_README_*.txt from the resource kit is followed here.

        function -- a callback that accepts a single argument, an integer in the range 0-100.
           indicates % done.  the function is called after each write in the loop.

        STATUS: works.

    """
        if DEBUG & DBG_Simulation:  # nothing to write to
            return

        totallength = len(self.fwdata)
        buffer = struct.pack(">L", totallength) + self.fwdata[0:self.fwhdr[0]]

        modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x00, 0x70, 0x00,
                                    self.transactionID, (0x01 << 5) | 0x01,
                                    len(buffer), 0x00)

        written = self.fd.put_sub_page(modepageblock, buffer, DEBUG)

        if DEBUG & DBG_General:
            print("Page 0...")

        buflen = 32768
        written = buflen
        i = self.fwhdr[0]
        j = i + buflen
        moretocome = 0x01

        if (DEBUG & DBG_General):
            print('writeFirmware: i=%d, start=%d, last=%d fw length= %d\n' % \
               ( i, self.fwhdr[0], totallength, len(buffer) ))

        while (written == buflen) and (i < len(self.fwdata)):

            if (i + buflen) > totallength:  # writing the last record.
                buflen = totallength - i
                moretocome = 0

            modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x00, 0x70,
                                        0x00, self.transactionID, moretocome,
                                        buflen, 0x00)

            j = i + buflen
            written = self.fd.put_sub_page(modepageblock, self.fwdata[i:j],
                                           DEBUG)
            i = j

            function(i * 100 / totallength)

            if (DEBUG & DBG_General):
                print('wrote ', written, ' bytes.  Cumulative: ', i, ' of',
                      totallength)

        if (DEBUG & DBG_General):
            print('writeFirmware Done.  i=%d, len=%d' % (i, totallength))

        self.__transactionNext()

        paklen = 1
        modepageblock = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x80, 0x71, 0,
                                    self.transactionID, 0x01 << 5, paklen, 0)

        cmdout = self.fd.get_sub_page(paklen, modepageblock, 0, DEBUG)
        status = struct.unpack('>B', cmdout)

        if DEBUG & DBG_General:
            print('Drobo thinks write status is: ', status[0])

    def GetCharDev(self):
        return self.char_dev_file

    def GetSubPageConfig(self):
        """ returns: ( MaxNumberOfSlots, MaxNumLUNS, MaxLunSize )
     """
        # SlotCount, Reserved, MaxLuns, MaxLunSz, Reserved, unused, unused

        if DEBUG & DBG_Simulation:
            self.slot_count = 8
            return (8, 16, 2199023250944)

        result = self.__getsubpage(0x01, 'BBBQ')
        self.slot_count = result[0]
        return (result[0], result[2], result[3] * 512)

    def GetSubPageCapacity(self):
        """ returns: ( Free, Used, Virtual, Unprotected ) 
     """
        if DEBUG & DBG_Simulation:
            capacity = 495452160000
            used = random.randint(0, capacity)
            return (capacity - used, used, capacity, 125184245760)

        return self.__getsubpage(0x02, 'QQQQ')

    # field names of the capacity sub-page (0x02), in order: seven big endian
    # u64 on newer firmware (page length 0x38), fewer on older units.
    _CAPACITY_FIELDS = ('free', 'used', 'total', 'unprotected_total',
                        'pt_free', 'pt_used', 'pt_total')

    def GetCapacityEx(self):
        """ every field of the capacity sub-page (0x02) the unit returns.
        returns a dictionary, in bytes:

          free, used, total   - protected capacity (GetSubPageCapacity [0:3])
          unprotected_total   - capacity with no redundancy behind it
                                (GetSubPageCapacity [3])
          pt_free, pt_used, pt_total - the "PT" set of newer firmware

        Fields the unit does not return are None.  Raises DroboException if
        the page holds fewer than three fields.

        Layout from ReDrobo (docs/PROTOCOL.md, ESACapacityInfoPT::parse,
        page length 0x38 seen on a Drobo 5D, fw 4.2.3.)  What "PT" stands
        for is not known.  Untested on hardware here.
        """
        if DEBUG & DBG_Simulation:
            return {'free': 370267914240, 'used': 125184245760,
                    'total': 495452160000, 'unprotected_total': 125184245760,
                    'pt_free': 370267914240, 'pt_used': 125184245760,
                    'pt_total': 495452160000}

        body = self.__getsubpage_raw(0x02, 8 * len(self._CAPACITY_FIELDS))
        n = min(len(body) // 8, len(self._CAPACITY_FIELDS))
        if n < 3:
            raise DroboException("capacity sub_page too short")
        values = list(struct.unpack('>%dQ' % n, body[:n * 8]))
        values += [None] * (len(self._CAPACITY_FIELDS) - n)
        return dict(zip(self._CAPACITY_FIELDS, values))

    def GetSerial(self):
        """ the enclosure serial number from MODE SENSE page 0x3A sub-page
        0x80 (4 byte page header, then a 24 character string), or None.

        Sub-page 0x80 is also claimed by the vendor's event messages
        record, so, like the vendor's ESADeviceSerial::parse, the string is
        believed only if it starts with "DR".  Firmware 4.2.3 on a 5D
        refuses the sub-page; that gives None too.  Facts from ReDrobo
        (docs/PROTOCOL.md.)  A read only; untested on hardware here.
        """
        if DEBUG & DBG_Simulation:
            return 'DRSIMULATED0000'
        try:
            body = self.__getsubpage_raw(0x80, 24)
        except (DroboException, IOError):
            return None
        text = body[:24].split(b'\0')[0].decode('latin-1').strip()
        return text if text.startswith('DR') else None

    def GetSubPageSlotInfo(self):
        """  returns list of slot info, each slot is:
       ( slotId, PhysicalCapacity, ManagedCapacity, leds, Manufacturer, Model ) ... )
       
       leds - indicates what the lights on the front panel are doing.
              returns one colour if one colour is constant, set of
              colours if there is flashing going on.
     """
        if DEBUG & DBG_Simulation:
            return ((0, 500107862016, 0, 'green', 'ST3500830AS',
                     'ST3500830AS'), (1, 750156374016, 0, 'green',
                                      'WDC WD7500AAKS-00RBA0',
                                      'WDC WD7500AAKS-0'),
                    (2, 0, 0, _ledstatus(random.randint(0, 6)), '', ''),
                    (3, 0, 0, 'gray', '', ''), (0, 500107862016, 0, 'green',
                                                'ST3500830AS', 'ST3500830AS'),
                    (1, 750156374016, 0, 'green', 'WDC WD7500AAKS-00RBA0',
                     'WDC WD7500AAKS-0'), (2, 0, 0,
                                           _ledstatus(random.randint(0, 6)),
                                           '', ''), (3, 0, 0, 'gray', '', ''))

        slotrec = 'HBQQB32s16sL'

        i = 0
        pattern = 'B' + slotrec * self.slot_count

        r = self.__getsubpage(0x03, pattern)

        l = []
        j = 0

        # The normal thing to do, which was here, is to use r[0] which is supposed
        # to indicate the number of records returned.  Unfortunately, a bug in
        # Drobo S, has it return 8 for r[0], instead of 5.  So use a surrogate found elsewhere.
        #
        # this is safe because slot_count is initialized in init.
        #
        while (j < self.slot_count):
            i = j * 8
            s = (r[i + 2], r[i + 3], r[i + 4], _ledstatus(r[i + 5]),
                 r[i + 6].decode().strip(" \0"),
                 r[i + 7].decode().strip(" \0"))
            l.append(s)
            j = j + 1

        return l

    def GetSubPageLUNs(self):
        """
         For each LUN, returns detailed information tuple:
            [ ( LUNID, LUN total Capacity, LUN Used Capacity, PartitionScheme*, Format* ),
                ... ]

         *only returned if firmware: SUPPORTS_NEW_LUNINFO2 (essentially >=1.1.0)
          and the unit answers sub-page 0x07 (LUNINFO2.)  If it refuses
          (I/O error or no valid page), the LUNINFO (0x04) tuples are
          returned instead, and 0x07 is not asked again by this object.

      STATUS: works, with worries and omissions:

      question: is it correct to mix 'used capacity' from luninfo, with 
      total capacity from luninfo2?

      parameter luninfo2[k+4] is supposed to be partcount...
      but 'PartCount' is odd. Seems to always be 128 (0x80)
         once partitions are set on my v1.  perhaps indicates maximum?

      if you mix partition types within a single Drobo, Drobo tends to give up
      and claim "no partitions" rather than reporting the union of the types.
     """
        if DEBUG & DBG_Simulation:
            return [(0, 2199023251456, 5092651008, 'GPT', 1, ['EXT3'])]

        lp = "HBQQ"
        l = self.__getsubpage(0x04, 'B' + lp * 8)

        use2 = ('SUPPORTS_NEW_LUNINFO2' in self.features) and \
            not getattr(self, 'luninfo2_refused', False)
        if use2:
            # must ensure li2 fields have correct count.
            li2fieldcount = 10
            li2 = "HBQBBBH3B"
            try:
                l2 = self.__getsubpage(0x07, "B" + li2 * 8)
            except (IOError, DroboException):
                # newer units may refuse 0x07 (it is not in the vendor's Mac
                # driver dispatch table, per ReDrobo): use 0x04 alone, and
                # do not ask again.
                self.luninfo2_refused = True
                use2 = False

        i = 0
        li = []
        while (i < l[0]):
            j = i * 4
            if use2:
                k = 1 + i * li2fieldcount

                # sanity check, in case I ever get out of sync again...
                if l2[k] != 17:
                    print(
                        'Warning: probably have not grokked the LUNINFO2 record correctly'
                    )

                li.append((l2[k + 1], l2[k + 2], l[j + 4],
                           _partscheme(l2[k + 3]), _partformat(l2[k + 5])))
            else:
                li.append((l[j + 2], l[j + 3], l[j + 4]))
            i = i + 1

        return li

    def GetSubPageSettings(self):
        """
        returns: ( currentUTCtime, UTCoffset, DroboName )

        STATUS: works with... 
        ERRATA:
                -- dpd.h and other sources say offset is two bytes.
                -- DMP Spec says it is only one byte.
                -- other sources say it is always set to 'California Time'... 8.
                -- California time is UTC - 8... +8 would be China.
                -- structures returned are supposed to be in network byte order (MSB)
                   when two byte value read in network byte order, result is 2048.
                   they stuff it in LSB order, so the 8 ended up in the higher order byte.
                -- so I just claim it says 8 and shut up.
                -- ReDrobo (checked on a Drobo 5D) found gmtOffset is a u16 of
                   minutes east of UTC, and the time is the unit's LOCAL clock.
                   GetClock() decodes that; this function stays as it was so
                   first generation units behave as before.

     """
        if DEBUG & DBG_Simulation:
            return (1220112079, 8, 'TRUSTED DATA')

        (utc, offset, name) = self.__getsubpage(0x05, 'LH32s')
        name = name.decode().strip(" \0")
        offset = 8  # offset is screwed up returned by Drobo, just set it to what they claim it should be.
        return (utc, offset, name)

    def GetClock(self):
        """ the unit's clock, from the settings sub-page (0x05).  returns a
        dictionary:

          time           - the raw currentTime u32
          raw_offset     - the raw gmtOffset u16
          offset_minutes - gmtOffset as minutes east of UTC (120 is UTC+2),
                           or None when implausible (see _clockoffset)
          local          - the unit's wall clock, 'YYYY-MM-DD HH:MM:SS'
          utc            - the matching Unix time, or None if the offset
                           is unknown

        currentTime is the unit's LOCAL wall clock, not UTC: format it as
        UTC to get the clock face, and subtract the offset to get real UTC.
        Facts from ReDrobo (docs/PROTOCOL.md, checked on a Drobo 5D, fw
        4.2.3.)  drobom set time (Sync) writes the host's UTC time with
        offset 0, so a unit set that way shows UTC with offset 0.

        Gen 1 caveat: the original author saw offset bytes that read 0x0800
        on a first generation unit (hence GetSubPageSettings always saying
        8); those give offset_minutes None here, and the clock face is shown
        as the unit holds it.  GetSubPageSettings is unchanged.
        Untested on hardware here.
        """
        if DEBUG & DBG_Simulation:
            when, raw = 1220112079, 0
        else:
            when, raw = self.__getsubpage(0x05, 'LH32s')[:2]
        off = _clockoffset(raw)
        return {'time': when, 'raw_offset': raw, 'offset_minutes': off,
                'local': time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.gmtime(when)),
                'utc': None if off is None else when - off * 60}

    def GetSubPageProtocol(self):
        """ 
        returns ( Major, Minor )

        STATUS: working... 
            soupcon: at firmware 1.0.3, seemed to demand an additional byte.
              might be a confused artifact of dev.
            at 1.1.1 the additional byte leads to resid > 0 in the C call, 
            so matches docs. 
     """
        if DEBUG & DBG_Simulation:
            return (0, 10)

        return self.__getsubpage(0x06, 'BB')

    def GetSubPageFirmware(self):
        """

        STATUS:  working with ERRATA:

        returns ( majorVer, MinorVer, Build, UpgMajorVer, UpgMinorVer, 
                BuildDate, Arch, )
        above is what the spec says...

        dpd.h says byte 128 has a feature
            agrees on the first four, but says the date is a 32 char string, and
            is followed by Arch[16], and Extra[256],  

            Drobo has a version string after arch, guessed at 16 chars.
            I shortened Extra for that.
            byte 0x80 is supposed to be feature flags, found it a 0x73...
     """
        if DEBUG & DBG_Simulation:
            self.features=['NO_AUTO_REBOOT', 'NO_FAT32_FORMAT', \
                   'USED_CAPACITY_FROM_HOST', 'DISKPACKSTATUS', \
                   'ENCRYPT_NOHEADER', 'CMD_STATUS_QUERIABLE', \
                   'VARIABLE_LUN_SIZE_1_16', 'PARTITION_LUN_GPT_MBR', \
                   'FAT32_FORMAT_VOLNAME', 'SUPPORTS_OPTIONS2', \
                   'SUPPORTS_DROBOSHARE', 'SUPPORTS_NEW_LUNINFO2']
            self.fw=(1, 201, 12942, 12, 6, 'May 13 2008,15:29:32', \
                     'ArmMarvell', '1.1.2', self.features )
            return self.fw

        raw = self.__getsubpage(0x08, 'BBHBB32s16s16s240s')
        result = struct.unpack('>112sL32sH90s', raw[8])
        self.features = _unitfeatures(result[1])
        self.fw = (raw[0], raw[1], raw[2], raw[3], raw[4],
                   raw[5].decode().strip(" \0"), raw[6].decode().strip(" \0"),
                   raw[7].decode().strip(" \0"), self.features)
        return self.fw

    def GetSubPageStatus(self):
        """
     return _unitstatus

     STATUS: works a bit, 
        relayoutcount stuff completely untested...
      Errata: 
      spec says a single byte.
      dmp.h says two longs:  (Status, RelayOutCount)
      when I remove a disk, I get [ 'No Redundancy', 'Relay out in progress'],
      but when I format, it stays empty...
     """
        if DEBUG & DBG_Simulation:
            return _unitstatus(random.randint(0, 16535))

        ss = self.__getsubpage(0x09, 'LL')
        s = _unitstatus(ss[0])

        if ss[1] > 0:  # relay out in progress
            est = self.__relayout_track(ss[1])
            self.relayout_estimate = est
            if est is None:
                s.append('no estimate yet ')
            elif est['blocks_done'] == 0:
                s.append('gathering stats, no estimate yet ')
                self.relayout_estimate = None
            else:
                s.append('%d blocks left ' % ss[1])
                s.append(_timeleft(est['minutes_left']))
                s.append(_completion(est['eta']))
        else:
            self.relayout_estimate = None
            self.__relayout_clear()

        return (s, ss[1])

    def __getsubpage_raw(self, sub_page, maxlen):
        """ Retrieve a variable length sub page: ask for up to maxlen bytes
        after the 4 byte page header, and return the bytes the page header
        says are there (newer firmware returns longer records.)

        Uses the same legacy-then-DBD logic as __getsubpage.  Raises
        DroboException if no page with the right header comes back, and
        FirmwareLacks (one) without sending anything if the firmware lacks
        the sub-page (see _fw_implements.)
        """
        self.__gate('sub_pages', sub_page)
        if self.mode_sense_style == 'legacy':
            alloc = maxlen + 4
            modepageblock = struct.pack(">BBBBBBBHB", 0x5a, 0, 0x3a, sub_page,
                                        0, 0, 0, alloc, 0)
            out = self.fd.get_sub_page(alloc, modepageblock, 0, DEBUG)
            body = _pagebody(out, sub_page)
            if body is not None:
                return body
            if DEBUG & DBG_HWDialog:
                print('no legacy page, trying mode parameter header')

        alloc = maxlen + 4 + 8 + 16
        modepageblock = struct.pack(">BBBBBBBHB", 0x5a, 0x08, 0x3a, sub_page, 0,
                                    0, 0, alloc, 0)
        try:
            out = self.fd.get_sub_page(alloc, modepageblock, 0, DEBUG)
        except IOError:
            out = b''
        body = None
        if len(out) >= 8:
            bdl = struct.unpack('>H', out[6:8])[0]
            body = _pagebody(out[8 + bdl:], sub_page)
        if body is None:
            raise DroboException("no sub_page 0x%02x returned" % sub_page)
        self.mode_sense_style = 'dbd'
        return body

    def GetStatusEx(self):
        """ extended unit status.  returns a dictionary:

          status          - raw status bits, as GetSubPageStatus reads them
          flags           - names of the status bits, including those the
                            disk pack status implies (see below)
          relayout_count  - relay out (data protection) work left
          diskpack_status - disk pack status, or None on older firmware
          double_degraded - double degraded count, or None on older firmware
          extra           - a further u32 of unknown meaning, or None
          severity        - 'red', 'yellow' or 'green', by the vendor's
                            rule (_statusseverity) applied to the status
                            bits and the bits the disk pack status implies,
                            so it agrees with the flags.  extra is not used.

        Newer firmware returns 16, 20 or 24 bytes of status instead of 12.
        Like the vendor's software, disk pack status 2..8 is folded into the
        flags: 2 unknown version, 3 unknown error, 4 mismatched disks,
        5-7 too many missing disks, 8 bit 0x40000 (unnamed.)

        STATUS: decoded from observed behaviour; untested on hardware.
        """
        if DEBUG & DBG_Simulation:
            return {'status': 0x0240, 'flags': _unitstatus(0x0240),
                    'relayout_count': 1250000, 'diskpack_status': 1,
                    'double_degraded': 0, 'extra': None,
                    'severity': _statusseverity(0x0240)}

        body = self.__getsubpage_raw(0x09, 20)
        n = len(body) // 4
        if n < 2:
            raise DroboException("status sub_page too short")
        fields = list(struct.unpack('>%dL' % n, body[:n * 4])) + [None] * 3
        status, relayout, diskpack, dd, extra = fields[:5]

        implied = {2: 0x1000, 3: 0x10000000, 4: 0x0800, 5: 0x0020,
                   6: 0x0020, 7: 0x0020, 8: 0x40000}.get(diskpack, 0)
        return {'status': status, 'flags': _unitstatus(status | implied),
                'relayout_count': relayout, 'diskpack_status': diskpack,
                'double_degraded': dd, 'extra': extra,
                'severity': _statusseverity(status | implied)}

    def GetSlotInfo2(self):
        """ per-drive details, from sub-page 0x35 (newer firmware.)  returns a
        list of dictionaries, one per slot:

          slot, status, errors, disk_state, disk_type, temperature,
          life_remaining, rotational_speed, manufacturer, firmware, serial,
          total_capacity, managed_capacity, and decoded from those:
          health ('Good', 'Healed', 'Warning', 'Failed', 'Unknown'),
          media ('HDD', 'SSD'), interface ('SATA', 'SAS', 'mSATA') and
          rpm (spindle speed: rotational_speed * 200, 0 for a drive that does
          not spin, None when unknown; see _driverpm)
          accelerator (True for an mSATA accelerator slot, disk type 4,
          which is a cache and not a drive bay)

        or None when the firmware does not support the sub-page.

        Page layout (big endian): page header (4 bytes), 4 reserved bytes,
        slot count (1), record length (1), 2 reserved, then one record per
        slot, each at least 108 bytes:
          slot u8, status u8, error count u16, disk state u32, disk type u8,
          temperature u8, life remaining u8, rotational speed u8,
          manufacturer 44s, firmware revision 12s, serial 24s,
          total capacity u64, managed capacity u64.

        STATUS: layout from observed behaviour, untested on hardware.  Units
        are assumed: temperature in Celsius, life remaining in percent.
        """
        if DEBUG & DBG_Simulation:
            return [{'slot': i, 'status': 1, 'errors': 0, 'disk_state': 2,
                     'disk_type': 1, 'temperature': 35 + i,
                     'life_remaining': 100 - 7 * i, 'rotational_speed': 36,
                     'manufacturer': 'WDC WD30EFRX-68EUZN0',
                     'firmware': '82.00A82', 'serial': 'WD-SIMULATED%d' % i,
                     'total_capacity': 3000592982016,
                     'managed_capacity': 2999000000000, 'health': 'Good',
                     'media': 'HDD', 'interface': 'SATA',
                     'rpm': _driverpm(36), 'accelerator': False}
                    for i in range(4)]

        maxlen = 8 + 128 * max(self.slot_count, 8)  # room for padded records
        try:
            body = self.__getsubpage_raw(0x35, maxlen)
        except (DroboException, IOError):
            return None
        if len(body) < 8:
            return None
        count, stride = body[4], body[5]
        if stride < 108:
            return None

        slots = []
        off = 8
        for i in range(count):
            rec = body[off:off + 108]
            if len(rec) < 108:
                break
            f = struct.unpack('>BBHLBBBB44s12s24sQQ', rec)
            text = [x.split(b'\0')[0].decode('latin-1').strip() for x in f[8:11]]
            slots.append({'slot': f[0], 'status': f[1], 'errors': f[2],
                          'disk_state': f[3], 'disk_type': f[4],
                          'temperature': f[5], 'life_remaining': f[6],
                          'rotational_speed': f[7], 'manufacturer': text[0],
                          'firmware': text[1], 'serial': text[2],
                          'total_capacity': f[11], 'managed_capacity': f[12]})
            slots[-1]['health'] = _diskhealth(f[3])
            slots[-1]['media'], slots[-1]['interface'] = _drivetype(f[4])
            slots[-1]['rpm'] = _driverpm(f[7])
            # an mSATA SSD (disk type 4) is the accelerator (cache), not a
            # drive bay: a 5D lists it as a sixth slot (ReDrobo.)
            slots[-1]['accelerator'] = (f[4] == 4)
            off += stride
        return slots

    def GetRedundancyLevel(self):
        """ 'single', 'dual' or 'undetermined': the disk redundancy level,
        which the unit does not report, derived from capacities (see
        _redundancylevel.)  Disk capacities come from GetSlotInfo2
        (sub-page 0x35), leaving out an mSATA accelerator slot, or from
        GetSubPageSlotInfo (0x03) on firmware without 0x35 (those units have
        no accelerator); the usable protected total from GetSubPageCapacity.

        STATUS: rule from ReDrobo, checked on one Drobo 5D there; untested
        here on hardware.
        """
        if DEBUG & DBG_Simulation:
            slots = Drobo.GetSlotInfo2(self)
            usable = Drobo.GetSubPageCapacity(self)[2]
            return _redundancylevel([s['total_capacity'] for s in slots],
                                    usable)
        try:
            slots = self.GetSlotInfo2()
            if slots is None:
                caps = [s[1] for s in self.GetSubPageSlotInfo()]
            else:
                caps = [s['total_capacity'] for s in slots
                        if s['interface'] != 'mSATA']
            usable = self.GetSubPageCapacity()[2]
        except (DroboException, IOError, struct.error):
            return 'undetermined'
        return _redundancylevel(caps, usable)

    # system operations (0xEA) this module may send as reads.  Others exist
    # (firmware download, standby, power off, ...): never add one here without
    # proof that it is a pure read.
    _SYSTEM_READ_OPS = (
        0x81,  # extended LUN info: data-in, only ever polled for display.
        0x83,  # extended LUN info v2 (ESA_SYSOPID_GET_LUNINFO_EX2): data-in,
               # same request shape as 0x81, polled for display by the
               # vendor's software when the firmware advertises 0x800000.
    )

    def __system_read(self, op, arg, length, last=0):
        """ issue a vendor unique system operation that returns data:
            ea 10 80 <op> <arg> 00 21 <length16> <last>, as the vendor's
            software does.  Only operations listed in _SYSTEM_READ_OPS are
            allowed, and not those the firmware lacks (see _fw_implements.)
        """
        if op not in self._SYSTEM_READ_OPS:
            raise DroboException("system operation 0x%02x not allowed" % op)
        self.__gate('system_ops', op)
        cdb = struct.pack(">BBBBBBBHB", 0xea, 0x10, 0x80, op, arg & 0xff, 0,
                          0x21, length, last & 0xff)
        return self.fd.get_sub_page(length, cdb, 0, DEBUG)

    def GetLUNsInfoEx(self):
        """ extended LUN (volume) information, via system operation 0x81.
        returns a list of dictionaries:

          index, lun, name, total_capacity, second_capacity, format,
          format_name, flags_a, flags_b

        or None when the firmware does not support it.

        Reply: byte 1 entry count, byte 2 bit 0 'more to come' (ask again
        starting from the next LUN index), entries of 285 bytes from byte 3:
        index u8, lun u32, name 258s, flags_a u8, flags_b u8,
        total capacity u64, second capacity u64, format u32 (big endian.)

        STATUS: from observed behaviour, untested on hardware.
        second_capacity is probably the used capacity, unconfirmed.
        """
        if DEBUG & DBG_Simulation:
            return [{'index': 0, 'lun': 0, 'name': 'Drobo volume 1',
                     'total_capacity': 17592186044416,
                     'second_capacity': 4398046511104, 'format': 0x08,
                     'format_name': _partformat(0x08), 'flags_a': 0,
                     'flags_b': 1}]

        luns = []
        start = 0
        for _ in range(32):  # bounded, whatever the unit says
            try:
                data = self.__system_read(0x81, start, 1024)
            except (IOError, DroboException):
                return None if not luns else luns
            if len(data) < 3:  # no reply header: an unsupported op can
                return luns or None  # answer GOOD with no data.
            count, more = data[1], data[2] & 1
            last = None
            for i in range(count):
                e = data[3 + i * 285:3 + (i + 1) * 285]
                if len(e) < 285:
                    break
                f = struct.unpack('>BL258sBBQQL', e)
                luns.append({'index': f[0], 'lun': f[1],
                             'name': f[2].split(b'\0')[0].decode('latin-1').strip(),
                             'flags_a': f[3], 'flags_b': f[4],
                             'total_capacity': f[5], 'second_capacity': f[6],
                             'format': f[7], 'format_name': _partformat(f[7])})
                last = f[0]
            if not more or last is None:
                break
            start = last + 1
        return luns

    def GetLUNsInfoEx2(self):
        """ extended LUN (volume) information, version 2, via system
        operation 0x83.  Only sent when the firmware advertises it
        (feature SUPPORTS_LUNINFO_EX2, 0x800000), as the vendor's software
        does.  Returns a list of dictionaries with the GetLUNsInfoEx keys
        plus:

          version     reply format version (byte 0 of each reply)
          initiators  list of {'name': ..., 'address': 'a.b.c.d' or None}
          ex2_flag    flags_b bit 2, from version 2 replies (meaning unknown)

        or None when the unit does not support it.

        Reply: byte 0 version, byte 1 entry count, byte 2 bit 0 'more to
        come' (ask again from the next LUN index.)  Entries start at byte 3
        and are variable length: the same 285 bytes as GetLUNsInfoEx, where
        flags_a (offset 263) is a count n, followed by n names of 224 bytes
        and, in version 2 replies, n IPv4 addresses (4 bytes each, network
        order.)

        STATUS: from the vendor's software, untested on hardware.  The
        names and addresses are probably iSCSI initiators logged in to the
        LUN (names fit an iSCSI qualified name), unconfirmed.  Layout with
        more than one name per entry is inferred from the entry stride.
        """
        if DEBUG & DBG_Simulation:
            return [{'index': 0, 'lun': 0, 'name': 'Drobo volume 1',
                     'total_capacity': 17592186044416,
                     'second_capacity': 4398046511104, 'format': 0x08,
                     'format_name': _partformat(0x08), 'flags_a': 0,
                     'flags_b': 1, 'version': 2, 'ex2_flag': False,
                     'initiators': []}]

        if 'SUPPORTS_LUNINFO_EX2' not in self.features:
            return None

        luns = []
        start = 0
        for _ in range(32):  # bounded, whatever the unit says
            try:
                data = self.__system_read(0x83, start, 0x7c00, 1)
            except (IOError, DroboException):
                return None if not luns else luns
            if len(data) < 3:  # no reply header (see GetLUNsInfoEx)
                return luns or None
            version, count, more = data[0], data[1], data[2] & 1
            off = 3
            last = None
            for i in range(count):
                e = data[off:off + 285]
                if len(e) < 285:
                    break
                f = struct.unpack('>BL258sBBQQL', e)
                n = f[3]
                names_end = off + 285 + 224 * n
                end = names_end + (4 * n if version >= 2 else 0)
                if len(data) < end:
                    break
                initiators = []
                for k in range(n):
                    raw = data[off + 285 + 224 * k:off + 285 + 224 * (k + 1)]
                    addr = None
                    if version >= 2:
                        a = data[names_end + 4 * k:names_end + 4 * k + 4]
                        addr = '.'.join(str(b) for b in a)
                    initiators.append({
                        'name': raw.split(b'\0')[0].decode('latin-1').strip(),
                        'address': addr})
                luns.append({'index': f[0], 'lun': f[1],
                             'name': f[2].split(b'\0')[0].decode('latin-1').strip(),
                             'flags_a': f[3], 'flags_b': f[4],
                             'total_capacity': f[5], 'second_capacity': f[6],
                             'format': f[7], 'format_name': _partformat(f[7]),
                             'version': version,
                             'ex2_flag': version >= 2 and bool(f[4] & 4),
                             'initiators': initiators})
                last = f[0]
                off = end
            if not more or last is None:
                break
            start = last + 1
        return luns

    def GetLUNsInfoBest(self):
        """ the best extended LUN information the unit offers: version 2
        (GetLUNsInfoEx2) when the firmware advertises it, otherwise, or if
        that fails, GetLUNsInfoEx.  None when neither is supported.
        """
        luns = self.GetLUNsInfoEx2()
        if luns is None:
            luns = self.GetLUNsInfoEx()
        return luns

    def GetRelayoutEstimate(self):
        """ query the unit, and if data protection (relay out) is running and
        enough progress has been seen, return a dictionary:

          blocks_left, blocks_done, percent_done, minutes_left, eta, started

        eta and started are seconds since the epoch.  Returns None when no
        relay out is running, or there is no estimate yet.

        The unit only reports how much work is left (the relay out count), so
        the estimate comes from how fast that count falls.  The starting
        point is saved (see __relayout_state_file) so that one shot commands
        like 'drobom status' produce an estimate from the second run on.
        """
        if DEBUG & DBG_Simulation:
            return None
        self.GetSubPageStatus()
        return self.relayout_estimate

    def __relayout_state_file(self):
        """ where the relay out starting point is kept between runs:
        $DROBO_UTILS_STATE_DIR, else $XDG_STATE_HOME/drobo-utils, else
        ~/.local/state/drobo-utils.  One file per unit, named after it.
        """
        d = os.environ.get('DROBO_UTILS_STATE_DIR')
        if not d:
            base = os.environ.get('XDG_STATE_HOME') or \
                   os.path.join(os.path.expanduser('~'), '.local', 'state')
            d = os.path.join(base, 'drobo-utils')
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', self.name or 'drobo')
        return os.path.join(d, 'relayout-%s.json' % name)

    def __relayout_load(self):
        """ starting point from memory, or else from the state file. """
        if self.relaystart > 0:
            return (self.relaystart, self.relayinitialcount)
        try:
            with open(self.__relayout_state_file()) as f:
                st = json.load(f)
            return (float(st['start']), int(st['count']))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def __relayout_save(self, start, count):
        self.relaystart = start
        self.relayinitialcount = count
        f = self.__relayout_state_file()
        try:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            tmp = f + '.tmp'
            with open(tmp, 'w') as fd:
                json.dump({'start': start, 'count': count}, fd)
            os.replace(tmp, f)
        except OSError:
            if DEBUG & DBG_General:
                print('cannot save relay out state to %s' % f)

    def __relayout_clear(self):
        self.relaystart = 0
        self.relayinitialcount = 0
        try:
            os.remove(self.__relayout_state_file())
        except OSError:
            pass

    def __relayout_track(self, count):
        """ update relay out tracking with the current count (blocks left.)
        returns None on the first reading (or after a restart), else a dict
        as described in GetRelayoutEstimate.
        """
        now = time.time()
        st = self.__relayout_load()
        if (st is None) or (count > st[1]):
            # first reading, or more work than when we started: start over.
            self.__relayout_save(now, count)
            return None

        start, initial = st
        self.relaystart, self.relayinitialcount = start, initial
        done = initial - count
        est = {
            'blocks_left': count,
            'blocks_done': done,
            'percent_done': 100.0 * done / initial,
            'minutes_left': None,
            'eta': None,
            'started': start
        }
        runningtime = (now - start) / 60.0
        if done > 0 and runningtime > 0:
            rate = est['percent_done'] / runningtime  # percent per minute
            est['minutes_left'] = (100.0 - est['percent_done']) / rate
            est['eta'] = now + est['minutes_left'] * 60
        else:
            est['blocks_done'] = 0
        return est

    def GetOptions(self):
        """
        OptionsFlags (all units with options): the raw flags byte at +6 of
        sub-page 0x30.  ReDrobo (docs/PROTOCOL.md, from the vendor's
        ESAOptions::parse) names what it packs: use unprotected capacity,
        realtime data check and background data check type.  Which bit is
        which is not published, so it is not decoded.  Read only.

        different fw/hw combinations will return different results.
        return one of:
           None 
               - this drobo does not support Options ( fw < 1.11 )
           ( YellowThresh, RedThresh, AutoDelSnapshot, LowYel, LowRed )
               - drobo supports first version of options ( fw >= 1.11)
             ( above + FeatureOnOffStates, SpinDownDelay, IPAddress, Subnetmask ) 
               - DroboPro only ? my v1's don't support it.
        
        STATUS: untested, seems to agree with dmp.h for Options
            Options2 (only on Drobo Pro) completely un-tested.


        DMIP spec says:
           B-YelThresh, B-RedThresh, 5B-Rsvd, 1bit-AutoDel, B-Rsvd

        dmp.h (published 2007 and summer 2009) says:
           B-YelThresh, B-RedThresh, B-Flags, L-DataCheckParam, B-LowYelThresh, B-LowRedThresh.
        
        later firmware version and dmp published in 2009 adds OPTIONS2 with
        hmm...
     """
        if DEBUG & DBG_Simulation:
            return {"YellowThreshold":85, "RedThreshold":95, \
                    "SpinDownDelayMinutes":5, "SpinDownDelay": True, \
                    "UseStaticIPAddress":True, "IPAddress":'192.168.10.4', \
                    "NetMask":'255.255.255.0', "DualDiskRedundancy":True, \
                    "UseManualVolumeManagement":False, "OptionsFlags": 0 }
        if 'SUPPORTS_OPTIONS2' in self.features or self.fw[7] >= '1.1.0':
            # insert try/except for compatibility with firmware <= 1.1.0
            # never trust the page length field of 0x30 or 0x31: ReDrobo
            # saw 2304 and 64513 on a 5D.  Fixed offsets only.
            o = self.__getsubpage(0x30, 'BBBIBB')
            d = {"YellowThreshold": o[0], "RedThreshold": o[1]}
            # +6: packed flags (use unprotected capacity, realtime data
            # check, background data check type; bit positions unpublished.)
            d["OptionsFlags"] = o[2]
            if ('SUPPORTS_OPTIONS2' in self.features):
                ( flags, d['SpinDownDelayMinutes'], \
                  rawipb, rawnmb, rawgwb, mtub, \
                  rawipa, rawnma, rawgwa, mtua ) = \
                  self.__getsubpage(0x31, 'QHLLLHLLLH' )
                d['Flags'] = flags
                d['DualDiskRedundancy'] = (flags & 0x0001) > 0
                d['SpinDownDelay'] = (flags & 0x0002) > 0
                d['UseManualVolumeManagement'] = (flags & 0x0004) > 0
                if ('SUPPORTS_ISCSI' in self.features):
                    d['UseStaticIPAddress'] = (flags & 0x0008) > 0
                    ipb = socket.ntohl(rawipb)
                    maskb = socket.ntohl(rawnmb)
                    gwb = socket.ntohl(rawgwb)
                    if (mtua > 0):
                        ipa = socket.ntohl(rawipa)
                        maska = socket.ntohl(rawnma)
                        gwa = socket.ntohl(rawgwa)
                        d['IPAddress'] = socket.inet_ntoa(struct.pack(
                            'I', ipa))
                        d['NetMask'] = socket.inet_ntoa(struct.pack(
                            'I', maska))
                        d['Gateway'] = socket.inet_ntoa(struct.pack('I', gwa))
                        d['MTU'] = mtua
                        d['IPAddress2'] = socket.inet_ntoa(
                            struct.pack('I', ipb))
                        d['NetMask2'] = socket.inet_ntoa(
                            struct.pack('I', maskb))
                        d['Gateway2'] = socket.inet_ntoa(struct.pack('I', gwb))
                        d['MTU2'] = mtub
                    else:
                        d['IPAddress'] = socket.inet_ntoa(struct.pack(
                            'I', ipb))
                        d['NetMask'] = socket.inet_ntoa(struct.pack(
                            'I', maskb))
                        d['Gateway'] = socket.inet_ntoa(struct.pack('I', gwb))
            return d
        else:
            return None

    def umount(self):
        """
       umount all file systems using the given Drobo.
       return true on success, false on failure.
    """

        if DEBUG & DBG_Simulation:
            return True

        toumount = self.DiscoverMounts()
        if len(toumount) > 0:
            for i in toumount:
                if DEBUG & DBG_Chatty:
                    print("unmounting: ", i)
                umresult = os.system("umount " + i)
                if umresult != 0:
                    return False
        return True

    def DiscoverMounts(self):
        """
        return the list of mounted file systems using the unit.
    """

        if DEBUG & DBG_Simulation:
            return ["/drmnt0", "/drmnt2"]

        mounts = open("/etc/mtab")
        dlen = len(self.char_dev_file)
        filesystems = []
        for l in mounts.readlines():
            fields = l.split()
            for i in self.char_devs:
                if fields[0][0:dlen] == i:
                    filesystems.append(fields[1])
        mounts.close()
        return filesystems


def DiscoverLUNs(debugflags=0, vendorstring="Drobo"):
    """ find all Drobo LUNs accessible to this user on this system. 
        returns a list of list of character device files 
              samples: [ [ "/dev/sdf", "/dev/sdg" ], [ "/dev/sdh" ] ] 
        vendorstring adds a string to be compared to for SCSI ID LUN call.
        This value changes as new products are introduced.
    """
    global DEBUG
    DEBUG = debugflags

    if DEBUG & DBG_Simulation:
        return [["/dev/sdx", "/dev/sdy"], ["/dev/sdz"]]

    devices = []

    for potential in DroboIOctl.drobolunlist(DEBUG, vendorstring):
        if (DEBUG & DBG_Detection):
            print("trying: ", potential)

        try:
            d = Drobo(potential, DEBUG)
            devices.append(potential)
        except:
            pass

    return devices
