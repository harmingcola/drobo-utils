
---------------
DEVELOPER NOTES
---------------


This software is copyright under GPL.  See near end file for details...

This is a pile of random useful bits for developers.  (aka notes to myself ;-)


Working With Source
-------------------

Project Source code is managed using a git repository provided by 
https://github.com/petersilva/drobo-utils  Git ( http://git-scm.com/ ) provides a fully 
distributed development model, so one can exchange patches arbitrarily 
among developers.  On the sf.net repository, the 'master' branch is 
the final integration target for future releases. 


Getting a Source Tree 
---------------------

the following checks out the master branch of the source code tree and
puts it in the drobo-utils subdirectory of the current tree.

git clone https://github.com/petersilva/drobo-utils.git


Before doing Any commits, ensure that the author fields are 
appropriately set.  One can obtain patches applied to the branch 
with git pull, and commit patches for distribution with git push.


Build Dependencies
------------------

To build the package from source, there are a number of other packages needed::
 
 boule% sudo aptitude install debhelper dh-python 
 boule% sudo aptitude install python3-docutils python3-setuptools
 boule%

The second line only required to build documentation.  On the other hand,
to get a complete list of packages you need, it is best to use a shell window 
to grep in the Debian package control file (which defines what the 
dependencies are for the build system)::

     peter@pepino% grep Depend debian/control
     Build-Depends: debhelper (>= 9), python3, python-docutils, python3-setuptools, dh-python
     Depends: ${shlibs:Depends}, ${misc:Depends}, parted
     peter@pepino%



GIT Configure Patch Author
--------------------------

To ensure the correct author on patches created, make sure to set your 
author settings.  Something like the following is appropriate if you 
use a single identity for all your code contributions::

  boule% git config --global user.name "firstname lastname"
  boule% git config --global user.email "developer@yourdomain.net"
  boule% 

Others may wish for the identity to be associated with each project.

Using a Source Snapshot
-----------------------

Sometimes, when there are issues, the correction gets checked in, but there 
hasn't been time to do a full release process.  If you really need the fix, 
then your only option is to get it from the developers' git repository.  
If you are told 'it is in git', that refers to the git source code management 
system, and the server for that is at github.com.  How to get it::

 # git clone https://github.com/petersilva/drobo-utils mine
 # cd mine                 # go into the source directory you downloaded
 # ./drobom status         # try it out...
 # python setup.py install # install it in system places...
 # drobom status           # try it out...
 # git pull # get any changes made since the git clone was done.
 # python setup.py install # install the changes in system places.

Sample checkout of a stable version.  To view available branches::

 % git branch -r
 origin/HEAD -> origin/master
 origin/master
 origin/noC
 origin/peter
 origin/r0.2.1
 origin/r0_3_3
 origin/r0_4_0
 origin/r0_5_0
 origin/r0_6_0

Where a version is something like r0_4_0.  then you can pick anyone to work with::

 % git branch r0_4_0 -r origin/r0_4_0
 Branch r0_4_0 set up to track remote branch r0_4_0 from origin.
 % git checkout r0_4_0
 Switched to branch 'r0_4_0'
 %

When you use git to get a tree, it keeps copies of metadata to be able to 
track changes.  If you want a copy that is contains no git cruft, 
rm -rf .git in the root of the source tree.

If you are mixing downloaded packages and source installs, check out the next 
section for gotchas.

Dpkg vs. Python Install
-----------------------

The 'setup.py' script, mentioned in the previous section, is a convention  
from the distutils python packaging system.  distutils installation is slightly 
different from installation from debian packages.  There doesn't seem to be a 
distutils way to remove a package. touch all the files, do an installation, then 
manually remove the files it installed.

drobo-utils has been picked up for inclusion in debian.  The "real" packaging 
for debian packages is kept in a separate tree, and maintained by debian 
developers.  

The debian/ setup puts stuff in /usr/sbin while setup.py puts things in /usr/bin.  
Python install does not install man pages either, which the dpkg takes care of.  
The libs are placed differently too.  haven't reviewed for other conflicts, least 
confusing to use one or the other method on a system.  

(if you do distutils install, then remove the debian package via: dpkg --purge drobo-utils)


Running the Tests
-----------------

There are regression tests in the tests/ directory.  They need no Drobo: the
SCSI generic layer is replaced by a fake device that answers the way first 
generation Drobos do, and drobom itself is exercised in simulation mode (-v 128)::

  boule% make test
  boule% python3 -m unittest discover -s tests -v   # same thing.

Run them before and after any change to Drobo.py, DroboIOctl.py or drobom.
Since few developers still have every model to test with, the tests are the 
main protection against breaking older units while adding newer ones.


How Drobos identify themselves
------------------------------

Discovery (DroboIOctl.drobolunlist) looks at the vendor and product ids in
each disk's standard INQUIRY data.  On Linux the same strings are in
/sys/block/sdX/device/vendor and .../model.  DroboIOctl.DROBO_IDENTITIES is
the list of (vendor prefix, product prefix, model name) rules, checked in
order, ignoring case; drobom -s adds one more vendor prefix.  Anything that
matches still has to pass the Drobo() constructor checks (config page has
three fields, at least 4 slots, sane firmware page.)  Drobo.model holds the
model name, and drobom -v 16 prints it as each unit is found.

What units report, by family.  Source: the IOKit matching dictionaries of the
vendor's Mac SCSI driver (TrustedDataSCSIDriver.kext, Dashboard 3.5.0), all
for SCSI peripheral type 0 (disk).  The Mac Dashboard does no string matching
of its own: it enumerates the devices that driver attached to.

 * First and second generation Drobo: vendor 'TRUSTED' (the vendor's driver
   is still named after Trusted Data, apparently the company's earlier name),
   product 'Mass Storage' or 'USB Mass Storage'.
 * Third generation Drobo: vendor 'Drobo' or 'DROBO', product 'Drobo3',
   'Gen3' or '3rd Gen DRDR3-A'.
 * DroboPro and DroboElite: vendor 'DROBO'/'Drobo', product 'DroboPro' or
   'DroboElite'.
 * Drobo S Gen 2 on USB 3: vendor 'USB 3.0', product 'Drobo S - Gen 2' (the
   only Drobo whose vendor is not the company name.)
 * Drobo 5D, 5D3, 5C, 8D and Mini: vendor 'Drobo' (5D and Mini also seen as
   'DROBO'), product '5D', '5D3', '5C', '8D' or 'Mini'.  When connected by
   Thunderbolt the product gets a ' - TB' suffix ('5D - TB', 'Mini - TB', ...)
 * iSCSI units B800i, B810i and B1200i: vendor 'Drobo', product the model.
 * Catch-all: the Mac driver also attaches to vendor 'Drobo' with an empty
   product id, and to vendor 'Drobo' or 'DROBO' with product 'Drobo'.

The Windows Dashboard matches device instance paths instead.  Those are lower
case, with '_' for spaces, e.g. usbstor#disk&ven_usb_3.0&prod_drobo_s_-_gen_2.
Its strings (usbstor#, sbp2#/sbp#/dsbp# for FireWire, drobo_s with usb_3.0,
3rd_gen_dr, drdr3) are the same identities seen through that naming.

Thunderbolt: the Mac Dashboard also ships a driver for the Drobo Thunderbolt
controller, which shows up as a PCI device (vendor id 0x4452, device ids
0x5570 and 0x3844) and needs its own SCSI host driver.  Linux has no such
driver, so on Linux only the USB ports of 5D/5D3/8D/Mini class units are
expected to work (untested.)

eSATA: the Mac Dashboard treats a unit as eSATA only when its product id from
the serial number (below) is 10, one of the two Drobo S ids, and it is not on
FireWire.  It then tunnels requests through ordinary disk reads and writes
(ATASCSISession), not SCSI pass-through.  drobo-utils has no such tunnel, so
the Drobo S family on eSATA is "data only".

Unit serial number: the Mac Dashboard reads the INQUIRY unit serial number
(VPD page 0x80) and derives a product id from it.  A 15 character serial
starting with 'D' (newer style, e.g. DRB...) carries it as three hex digits
at characters 8 to 10; an older serial starting with 'T' (12 characters, or
the same with '0' in place of the 'T') carries one hex digit at character 8.
Decoded from the Dashboard's product id to model name lookup:

  ======  ==========================  ======  ==========================
  id      model                       id      model
  ======  ==========================  ======  ==========================
  0       Drobo (gen 1, probably)     26      Drobo 5N
  2       Drobo (gen 2, probably)     28      B810i
  4       DroboPro                    29      Drobo (gen 3, probably)
  5, 10   Drobo S (one is Gen 2)      32      B810n
  7       Drobo FS                    33      Drobo 5C
  8       DroboElite                  34      Drobo 8D
  9       DroboPro FS                 35      Drobo 5N2
  11      B800i                       36      Drobo 8N
  12      B800fs                      38      Drobo 5D3
  16      B1200i                      39      B820n
  23      Drobo 5D                    40      B820i
  25      Drobo Mini                  45      Drobo 8i
  ======  ==========================  ======  ==========================

drobo-utils sends no INQUIRY for VPD pages: reading them over USB is not
always safe (Linux usb-storage normally skips them), so it would need testing
on real units first.  If the kernel did read it, it is in
/sys/block/sdX/device/vpd_pg80, and Drobo.vpd_serial() reads that file (only
that file); drobom info serial shows it.  The product id is not derived from
the serial yet: whether "characters 8 to 10" above counts from 0 or 1 is not
settled, and no real serial was at hand to check it against the table.


Protocol Notes: Drobo S Gen 2 (USB 3)
-------------------------------------

Observed behaviour of the Drobo S Gen 2 (USB id 1a62:000a), and of the vendor's 
own Dashboard software when it talks to one.  Useful for supporting other 
USB 3 era units:

 * INQUIRY: vendor 'USB 3.0', product 'Drobo S - Gen 2'.  The vendor's software 
   also recognizes it by product id, not vendor.

 * Sub-pages: the same MODE SENSE(10) page 0x3A sub-pages as earlier units, but:

   - the vendor's software always sets DBD (byte 1 = 0x08) and allows room 
     for a mode parameter header.  __getsubpage() tries the legacy form first 
     (so older units see no change), then that form.
   - observed only while the unit was rebuilding after a drive failure: both 
     forms returned just a mode parameter header, 00 06 00 00 00 00 00 00, and 
     no page.  The vendor's software could not see the unit at that time either, 
     so this may be the busy state rather than the protocol.  To be confirmed on 
     a healthy unit.

 * Transport: over USB (and FireWire, Thunderbolt) the vendor's software uses 
   ordinary SCSI pass-through, like drobo-utils.  Only for eSATA (device path 
   containing 'esata') does it tunnel requests through reads and writes of the 
   disk itself.

 * Do NOT send, even though it looks like a read: MODE SENSE page 0x3A, 
   sub-page 0xE1 or 0xE2, with a slot number in byte 4.  The vendor's software 
   uses it to prepare a drive bay for removal.

 * Opcode 0xEA (already used here for blink/standby) is the vendor unique 
   channel.  The vendor's software also uses it for firmware download, 
   diagnostics, the user event log, standby and power off.  Treat any new 
   0xEA operation as dangerous until proven to be a pure read.

 * 0xEA framing must match the data phase.  Static analysis of the Drobo S
   firmware 2.1.5 (its USB layer's 0xEA handler) shows the unit takes the
   direction from byte 2 bit 7 (clear: host to device) and the transfer
   length from bytes 7-8, and waits for that much data-out before acting.
   If the host's actual transfer disagrees (say length 1 but no data-out),
   the USB layer reports a phase error, and USB mass storage rules then
   make the host reset the device.  drobo-driverkit reports a Drobo 8D
   restarting over and over on exactly that framing of blink.
   drobo-utils is not affected: __issueCommand (blink 0x06, standby 0x0d)
   sends ea 10 00 <op> 00 <txid> 20 00 01 00 through get_sub_page(1, cdb,
   1), i.e. SG_DXFER_TO_DEV with a one byte, zeroed buffer, which is the
   well formed form the vendor's software uses.  Keep it that way: the
   TestVendorCommandFraming tests (tests/test_cov_ioctl.py) check that every
   0xEA command's SG direction and dxfer_len agree with its CDB.  Not yet
   confirmed with a usbmon capture on hardware.

 * 0xEA system operation layout (10 bytes): ea 10 <dir> <op> <arg> <txid> 
   <flags> <length16> <x>, where dir is 0x80 for a read (data from the unit.)  
   The vendor's software reads extended LUN info with op 0x81, arg = first 
   LUN index, flags 0x21, 1024 bytes.  Reply: entry count (byte 1), 'more' 
   flag (byte 2 bit 0; ask again from the next index), then 285 byte entries: 
   index, lun (u32), name (258), two flag bytes, total capacity (u64), a 
   second capacity (u64, probably used), format (u32, as _partformat.)
   Drobo.py only issues system reads listed in Drobo._SYSTEM_READ_OPS.

 * Extended LUN info version 2 (GetLUNsInfoEx2) is system operation 0x83
   (named ESA_SYSOPID_GET_LUNINFO_EX2 in the vendor's software):
   ea 10 80 83 <first LUN index> 00 21 7c 00 01, a 31744 byte read.  It
   differs from op 0x81 only in the op code, the length and the last byte.
   The vendor's software issues it as a data-in request (the same
   pass-through path as INQUIRY and MODE SENSE), from its periodic status
   poll only, and only when the firmware feature table has bit 0x800000
   (SUPPORTS_LUNINFO_EX2); otherwise it uses op 0x81.  Reply: format
   version (byte 0), entry count (byte 1), 'more' flag (byte 2 bit 0, paged
   as op 0x81), then variable length entries: the 285 byte op 0x81 entry,
   whose first flag byte (offset 263) is a count n, followed by n names of
   224 bytes and, from version 2, n IPv4 addresses (4 bytes, network
   order.)  From version 2, bit 2 of the second flag byte is a flag of
   unknown meaning.  The names and addresses look like iSCSI initiators
   logged in to the LUN (unconfirmed; the vendor's parser only handles one
   name per entry correctly, so the layout for several is inferred from
   the entry stride.)  drobom info volumes uses GetLUNsInfoBest(): version
   2 if advertised and working, else op 0x81.

 * Status, sub-page 0x09, is variable length.  Big endian u32s after the 4 
   byte page header: status bits, relay out count (the 12 byte form), then 
   disk pack status (16 bytes), double degraded count (20 bytes), and a 
   further value of unknown meaning (24 bytes.)  Older firmware returns the 
   12 byte form.  GetStatusEx() reads whatever the unit returns.  The vendor's 
   software folds disk pack status into the status bits: 2 -> 0x1000, 
   3 -> 0x10000000, 4 -> 0x800, 5..7 -> 0x20, 8 -> 0x40000.  Newer bits: 
   0x40000 incompatible disk pack, 0x100000 power supply missing, 0x200000 
   fan partially failed, 0x400000 fan failed, 0x800000 fan missing.  The 
   vendor's software treats status as 64 bits wide; above bit 31 it knows 
   incompatible Drobo on DroboShare (32), volume usage over limit (36, all 
   volumes 37) and iSCSI target login failed (38).  Where those high bits 
   come from (perhaps the 24 byte record's extra value) is unconfirmed.

 * Sub-page 0x35, slot info 2 (GetSlotInfo2): page header, 4 reserved bytes, 
   slot count (byte 8), record length (byte 9), 2 reserved, then per slot 
   (at least 108 bytes, big endian): slot, status, error count (u16), disk 
   state (u32), disk type, temperature, life remaining, rotational speed, 
   manufacturer (44), firmware revision (12), serial (24), total and managed 
   capacity (u64 each.)
   Disk type byte: 0 HDD/SATA, 1 HDD/SAS, 2 SSD/SATA, 3 SSD/SAS, 4 SSD/mSATA, 
   255 empty.  Health is the low four bits of disk state: 0 good, 1 healed, 
   2 warning, 3 failed.  Rotational speed counts in steps of 200 RPM (27 is 
   5400, 36 is 7200); 1 means a drive that does not spin (an SSD) and 0 an 
   unknown speed.  The vendor's service passes the byte through untouched; 
   its user interface does the conversion.

 * Other sub-pages the vendor's software reads, not yet used here: 0x11 disk 
   pack info, 0x33 system info, 0x37 fans, 0x3A battery.

 * Relay out (data protection) time left is not reported by the unit.  It is 
   estimated from how fast the relay out count falls since the relay out was 
   first seen (GetSubPageStatus, GetRelayoutEstimate.)  That starting point is 
   saved under $DROBO_UTILS_STATE_DIR / $XDG_STATE_HOME/drobo-utils so separate 
   drobom runs share it; when running drobom in a container, mount that directory.

 * USB transfer size: the vendor's software caps USB transfers at 512 KiB 
   for several older Drobo USB ids.  Linux uses 1 MiB on SuperSpeed by default; 
   on one Gen 2 the reads that timed out (while a drive was failing) were all 
   1 MiB.  If a unit shows repeated 'reset SuperSpeed USB device' messages, try:
   echo 1024 > /sys/block/sdX/device/max_sectors

 * What firmware 2.1.5 lacks.  Static analysis of the Drobo S 2.1.5 image
   (release.Drobo3_rev3.tdf: HAManager::modeSense and
   HAManager::systemOperation in runesa.vxe; nothing was run) finds no
   page 0x3A sub-pages 0x33 (system info), 0x35 (slot info 2) or 0x80
   (serial), nor 0x36-0x3A; and no 0xEA system operations 0x22 (event
   log), 0x81 or 0x83 (extended LUN info).  Those come from later firmware
   (5D/8D, 4.x).  A Drobo S Gen 2 may run 2.1.5.  So there GetSlotInfo2,
   GetSerial, GetLUNsInfoEx/Ex2/Best return None, GetRedundancyLevel uses
   sub-page 0x03, and drobom info drives/serial/volumes print one line
   saying so.  An unknown sub-page or op should end as CHECK CONDITION
   (IOError), but the code also treats a GOOD reply with no page, an
   empty or short page, another sub-page's page, or an empty 0xEA reply
   as 'not supported', with at most two CDBs per read and no retries
   (tests/test_unsupported_features.py.)  Untested on hardware.
   Sub-page 0x07 (LUN info 2) is present in 2.1.5.

Protocol Notes: facts from ReDrobo
----------------------------------

The ReDrobo project (https://github.com/fetzu/ReDrobo, MIT licence) publishes
a protocol write-up, docs/PROTOCOL.md, checked against a live Drobo 5D
(firmware 4.2.3) and the vendor's Dashboard.  drobo-utils uses the facts
below from it; the code is our own.

 * Status severity (GetStatusEx()['severity'], _statusseverity, shown by
   drobom info health).  The vendor's own rule, from its
   getSeverityForStatus: red if status & 0x14c4187a (tested 32 bits wide),
   else yellow if status & 0x36002300244 (tested 64 bits wide), else green.
   Our status is a u32 from sub-page 0x09, so only the low word of the
   yellow mask (0x02300244) can apply.  A healthy 5D reports 0x00028000:
   bits 15 and 17 are set when healthy, are in neither mask and are not
   alerts (their meaning is unknown), so it is green.  The severity is taken
   over the status bits plus the bits the disk pack status implies, so it
   agrees with the flags; the 24 byte record's extra word is not used.

 * Status bit names (from the vendor's AlertMailer::GetEventIDForStatus).
   Within 32 bits, bit 25 (0x2000000) is 'Hybrid tiering state', now named
   by _unitstatus (after the fan bits, before 'Unknown error').  Above 32
   bits: 32 DroboShare alert, 33 drive added, 34 drive removed, 35 dual
   disk redundancy updated, 36 volume usage over limit, 37 all volumes over
   limit, 38 target login, 39 relayout complete, 41 three SSDs and a disk
   required.  These are named by _unitstatus64, which nothing calls yet: no
   source of a 64 bit status is known, and the 24 byte record's extra word
   is NOT assumed to be the high word.  Bits 15 and 17 (set when healthy)
   are deliberately left unnamed, so they never show up as alert flags.

 * Redundancy level (GetRedundancyLevel, _redundancylevel, shown by drobom
   info health as 'Redundancy level: single/dual/undetermined').  The unit
   does not report it (ReDrobo found sub-page 0x11 all zeros on a 5D), but
   BeyondRAID holds back the largest disk for single redundancy and the two
   largest for dual, so: held back = raw installed (sum of the disk
   capacities, without an mSATA accelerator slot) minus the usable
   protected total (GetSubPageCapacity()[2]).  Single if held back /
   largest is within 5% of 1.0, dual if held back / (two largest) is,
   undetermined if neither or both fit (both can only fit when the second
   largest disk is tiny), or if there is no usable capacity.  ReDrobo's 5D
   (2/2/4/2/4 TB, usable 5 962 106 142 720 bytes) gives 1.0049 for dual and
   2.0098 for single, and its owner confirmed dual.  The window: the excess
   over 1.0 is overhead (0.5% of the held back space there; relatively more
   on arrays whose held back disks are small next to the whole), while the
   two hypotheses differ by a factor of 1 + second largest / largest (2 for
   equal disks), so 5% tolerates the overhead without confusing them.
   Capacities come from sub-page 0x35, where disk type 4 (mSATA) marks the
   accelerator, or from 0x03 on firmware without 0x35 (no accelerator
   there).  Confirmed on one enclosure only (ReDrobo's own validation
   table still lists the derivation as open).

 * Capacity, sub-page 0x02, is seven big endian u64 on newer firmware (page
   length 0x38): protected free, used and total, unprotected total, then
   free, used and total of a "PT" set (meaning unknown.)  The first four are
   what GetSubPageCapacity() has always read; GetCapacityEx() reads as many
   fields as the unit returns, up to seven, and drobom info capacity shows
   the unprotected total and the PT set as extra lines when present.  As
   with every record, bytes past the declared page length are stale buffer
   contents (on a 5D, fragments of its event log) and are ignored.

 * Settings, sub-page 0x05: currentTime (u32 at +4) is the unit's LOCAL
   wall clock, not UTC, and gmtOffset (u16 at +8) is minutes east of UTC
   (120 is UTC+2).  ReDrobo saw a 5D read 16:19:07 when UTC was 14:19:08 on
   a UTC+2 host.  This explains the "offset is screwed up" ERRATA in
   GetSubPageSettings(), which still returns offset 8 whatever the unit
   says, because the original author saw bytes reading 0x0800 on a first
   generation unit.  GetClock() decodes it properly: the clock face is the
   time formatted as UTC; the offset is read as a signed u16 and accepted
   only within 14 hours either way (else None, as for those Gen 1 bytes);
   real UTC is time minus offset.  drobom set time (Sync) writes the host's
   UTC with offset 0, so units set by drobo-utils show UTC+00:00.  drobom
   prints 'Drobo clock: YYYY-MM-DD HH:MM:SS (UTC+hh:mm)' under the info
   header and from drobom time.  Gen 1 caveat: whether first generation
   units use the same encoding is unknown; there, expect 'UTC offset
   unknown'.

 * Options, sub-page 0x30: +4 yellow threshold %, +5 red threshold % (both
   already read as YellowThreshold and RedThreshold), +6 a byte of packed
   flags: use unprotected capacity, realtime data check, background data
   check type (names from the vendor's ESAOptions::parse; which bit is
   which is not published).  GetOptions() returns that byte, undecoded, as
   OptionsFlags, which is read only (Drobo.READ_ONLY_OPTIONS; drobom set
   refuses it).  SetOptions() writes that byte back unchanged
   (read-modify-write: OptionsFlags from the options dict, else read with
   GetOptions); it used to write 0, which may have cleared those settings
   on newer units.  Units with no such byte (GetOptions None, or no 0x30
   page) still get 0 there.
   Options2, sub-page 0x31: +4 u64 featureOnOffStates (our Flags), +0x0C
   u16 spin down delay in minutes (our SpinDownDelayMinutes, confirmed: 14
   on ReDrobo's 5D), then the iSCSI addresses.  The page length fields of
   0x30 and 0x31 report impossible values (2304 and 64513 for a 1308 byte
   record), so they are never used: both pages are read at fixed offsets.

 * Slot info 2, sub-page 0x35: temperature reads 0 in every slot of a 5D
   on firmware 4.2.3 (not populated), so drobom info drives shows 0 as '-'
   (_temptext).  A 5D lists its mSATA accelerator as a sixth slot, and its
   config sub-page (0x01) counts six slots too, so the slot count cannot
   tell bays from the accelerator.  The disk type byte can: GetSlotInfo2()
   sets 'accelerator' for disk type 4 (SSD/mSATA), as GetRedundancyLevel()
   already assumes, and drobom info drives lists such slots after the bays
   under 'Accelerator (mSATA cache, not a drive bay):'.  An SSD in a bay
   (type 2 or 3) stays a bay.

 * LUN info 2, sub-page 0x07, is absent from the dispatch table of the
   vendor's Mac driver, so newer units should be expected to refuse it even
   when the firmware sets SUPPORTS_NEW_LUNINFO2.  GetSubPageLUNs() then
   returns the LUNINFO (0x04) tuples (LUN id, total, used) instead of
   failing, sets Drobo.luninfo2_refused and does not ask for 0x07 again.
   A refused 0x04 still raises.

 * Device serial, sub-page 0x80: a 4 byte page header, then a 24 character
   string.  The vendor's ESADeviceSerial::parse believes it only if it
   starts with 'D', 'R', because the event messages record claims the same
   sub-page.  A 5D on 4.2.3 refuses it.  GetSerial() reads it (a MODE SENSE,
   with the variable length reader) and returns it if it starts with "DR",
   else None; it is not read by the constructor or by default.  drobom
   info serial (an optional choice, not in the default list) prints it,
   then the VPD page 0x80 serial from sysfs for each device (see "How Drobos
   identify themselves").


Making a Release
----------------

Procedure::

  1 - make a branch
   # assuming you have a local repository...
   git branch <branch>  # creates the branch, from the cwd (ought to be master)
   git checkout <branch> # switches current dir to the branch.

  2 - Stamp the branch with version 
   vi CHANGES.rst          # complete change manifest for release
   vi debian/changelog     # copy manifest from txt, add signature.
   vi setup.py 		   # edit version
   vi Drobo.py             # edit VERSION

  3 - Build packages for testing & Install them. (see separate recipe.)

  4 - Run QA.
   Record results of release tests in the branch (QA.rst) As new tests are created, 
   modify QA.rst on trunk for to keep references for the next release.

  5 - Commit & Push QA'd branch

    git commit -a
    git push origin origin:refs/heads/<branch>


Quality Assurance (QA.rst)
==========================

QA.rst is a quality assurance log.  The version on the trunk of the releases 
indicates the QA procedure to be applied to the next version during the 
release process.  Since a branch is created for each release, the version 
of QA.rst acts as a quality log for that release.  so one can do a git 
checkout, or git export to get the quality log for any release (QA.rst 
introduce in version 0.4.0)


Building Debian & Ubuntu Packages
=================================

Assumes you have installed the Build dependencies
Method 1::

 % git clone https://github.com/petersilva/drobo-utils drobo-utils-<version>
 % cd drobo-utils-<version>
 % git checkout <tag for version you want>
 % debuild -us -uc

builds a .deb in the parent directory.

method 2::


 # obtain a fresh tree 
 % git clone https://github.com/petersilva/drobo-utils drobo-utils-<version>
 % cd drobo-utils-<version>
 % rm -rf .git  # get rid of Git cruft, yielding a raw source tree.
 % chmod 755 debian/rules  # I dunno why the permissions are wrong...

 # this debian/ config is just for non-distro packages.
 # builds for debian and Ubuntu.

 % dpkg-buildpackage -rfakeroot
 % cd ..
 # rename it for whatever distro is appropriate...
 % mv drobo_utils_0.3.3-1_i386 --> droboutils_0.3.3-1_i386_ubunutuIntrepid.deb

 # rebuild the source tar because it will have the 'debian' link in it.
 % cd drobo-utils-0.99.9
 % rm debian
 % cd ..
 % tar -czvf drobo-utils-0.3.3-1.tgz drobo-utils-0.99.9

apply QA tests. as per QA.rst recording results there.


Updating Documentation
----------------------

**OBSOLETE** site moved to github.com, which renders rst into html automagically.
this section no longer relevant.

use the restructured text tools (from the python-docutils package.)
to build things using:

 % make doc

Have a look at Makefile for how that works.
update the web site:

 % scp README.html <user>,drobo-utils@web.sourceforge.net:htdocs

Droboshare
----------

Droboshare is not directly supported by drobo utils running on a linux host.  
However, the droboshare itself is a linux host, and it is possible to run
drobo-utils un-modified on the droboshare itself.  There is download called 
the Droboshare Augmented Root File system (DARFS), which includes a python 
interpreter and drobo-utils.

Open Task: Reverse Engineer Dashboard <-> Droboshare Protocol
=============================================================

Why isn't there full support in host based drobo-utils itself?  Digital 
Robotics hasn't released details of the protocol used by the proprietary 
dashboard to communicate with a droboshare, so it would be a lot of work to 
reverse engineer that.  So support of a droboshare from a linux GUI on a 
host system is not likely in the near future.  

If someone wants to figure that out, it might be a good thing (tm)
After that is figured out, the next step would be to understand
how to flash the firmware remotely.  That would eliminate the last function
that cannot be done with open source.

Building DARFS
==============

DARFS - Droboshare Augmented Root File System. A pile of stuff that can
be run on a droboshare.

Have a look here:

http://groups.google.com/group/drobo-talk/web/building-droboshare-apps-on-debianish-os?hl=en

TRIM/DISCARD
------------

Drobo is the only consumer-level storage unit that does `Thin Provisioning`_ (allocating 
a device larger than the physical space available, allowing space upgrades without
OS changes.)  Drobo does this by understanding the file system blocks, which is
why it only supports a very limited set of file systems and cannot support full 
disk encryption.

There is considerable industry activity about adding `ATA TRIM`_ and corresponding 
`SCSI UNMAP`_ commands.  These commands, for their respective command sets, add 
the ability for the operating systems' file system code to indicate blocks that
are not in use to storage units. Drobo would work with any file system that 
uses these commands, with far less firmware. On linux, that file systems that 
are starting to support TRIM/DISCARD are:  ext4, btrfs, and xfs. It may also 
help with the inherent limitations around full disk encryption. 

These commands are still maturing in support.  Long term, they seem like 
The right thing to do.

(2009/12/30)


.. _`ATA TRIM`: http://en.wikipedia.org/wiki/TRIM
.. _`Thin Provisioning`: http://en.wikipedia.org/wiki/Thin_provisioning
.. _`SCSI UNMAP`: http://www.t10.org/ftp/t10/document.08/08-149r4.pdf
.. _`Andy Grover on TRIM`: http://blogs.oracle.com/linuxnstuff/2009/04/drobo_and_linux.html

Administrivia
-------------

Revision date: 2021/01/05

copyright:

Drobo Utils Copyright (C) 2008,2009,2021  Peter Silva (Peter.A.Silva@gmail.com)
Drobo Utils comes with ABSOLUTELY NO WARRANTY; For details type see the file
named COPYING in the root of the source directory tree.

 version 9999, somewhen

