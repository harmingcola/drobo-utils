
TODO:
-----

These are all Open Tasks... HELP WANTED!

  * add python version testing to test suite (2.4, 2.5, 2.6)
  * add LUN test procedure to DEVELOPERS.rst
  * looking at firmware based LUN creation/deletion & format.
  * disk pack status.
  * alerts.
  * fix make_tarball to exclude .git and debian, like it is supposed to.
  * review getSubPageStatus, DMIP seems better now.
  * review unitstatus vs. diskpack status.
  * make a daemon. reasons:
            -- updated for relay out progress.
            -- alerting 
	    -- unified JSON interface for GUI & WUI.
            -- managed queueing for device (not five GUI's querying in parallel.)
            -- preserve option settings across reboot? (one report.)
            -- periodically synch. the drobo clock to the server.
  
unreleased
----------

  * Drobo S Gen 2 (USB 3): found by INQUIRY product id (vendor is 'USB 3.0'.)
  * discovery: table of known INQUIRY identities (DroboIOctl.DROBO_IDENTITIES)
    covering Gen3, 5D, 5D3, 5C, 8D, Mini, Pro, Elite, B800i/B810i/B1200i;
    Drobo.model and drobom -v 16 name the model.  Untested on those units.
  * Drobo S Gen 2: read sub-pages that follow a mode parameter header (DBD).
  * fix relay out time estimate (was inverted, never shown, never reset.)
  * status: estimated completion time ("done around Tue 03:40"), and the
    relay out starting point is saved between runs, so one shot 'drobom status'
    gives an estimate from its second run.  GetRelayoutEstimate() API.
  * GetStatusEx(): longer status record of newer firmware (disk pack status,
    double degraded count), disk pack status folded into the flags.
  * drobom info health: redundancy summary, status, data protection progress.
  * GetSlotInfo2() and drobom info drives: per drive temperature, life
    remaining, error count, model, firmware and serial (sub-page 0x35.)
  * drives: decode health (good/healed/warning/failed) and drive type
    (HDD/SSD, SATA/SAS/mSATA.)
  * drives: decode the rotational speed code ('rpm' in GetSlotInfo2(), RPM
    column in drobom info drives.)
  * GetLUNsInfoEx() and drobom info volumes: extended LUN info (names,
    sizes, formats) via a read-only system operation, paged.
  * GetLUNsInfoEx2(): extended LUN info version 2 (read-only system
    operation 0x83, only on firmware advertising it), with per volume
    initiator names and addresses; drobom info volumes uses the best
    available source (GetLUNsInfoBest()).
  * GetCapacityEx(): all seven fields of the capacity sub-page (0x02) on
    newer firmware (unprotected total, "PT" free/used/total); drobom info
    capacity shows them as extra lines.  Layout from ReDrobo.
  * GetClock(): the unit's clock read as its local wall clock, with the
    UTC offset decoded as minutes east (None when implausible, as on Gen 1);
    drobom adds a 'Drobo clock' line to the info header and to drobom time.
    GetSubPageSettings() is unchanged.  Facts from ReDrobo.
  * GetOptions(): new read-only key OptionsFlags, the packed flags byte at
    +6 of sub-page 0x30 (use unprotected capacity, realtime and background
    data check; bits not decoded); drobom set refuses it.  Existing keys
    unchanged; the unreliable 0x30/0x31 length fields stay unused.
  * drobom info drives: temperature 0 (not populated on some firmware)
    shows as '-'; an mSATA accelerator slot (disk type 4, GetSlotInfo2()
    'accelerator') is listed apart, after the drive bays.
  * GetSubPageLUNs(): if the unit refuses LUNINFO2 (sub-page 0x07, absent
    from the vendor's Mac driver), fall back to the LUNINFO (0x04) result
    instead of failing.
  * drobom info luns: no longer fails on units without LUNINFO2; '-' for
    the partition scheme and format there.
  * GetSerial(): enclosure serial from sub-page 0x80 (only if it starts
    with "DR"), vpd_serial(): the VPD page 0x80 serial the kernel cached in
    sysfs; drobom info serial (optional, not printed by default).
  * tests: more regression fixtures (LUN info, options, DroboPro inquiry.)
  * name status bits: incompatible disk pack, power supply missing, fan
    partially failed, fan failed, fan missing.
  * add hardware-free regression tests (make test.)
  * DEVELOPERS: protocol notes for the Drobo S Gen 2.
  * status severity (red/yellow/green, the vendor's rule as published by
    ReDrobo): GetStatusEx()['severity'], shown by drobom info health.
  * name status bit 25 (hybrid tiering state); _unitstatus64() names the
    bits above 31 that ReDrobo lists, for when a 64 bit status is found.
  * redundancy level (single/dual/undetermined), derived from capacities as
    ReDrobo describes: GetRedundancyLevel(), shown by drobom info health.
  * fix DroboIOctl.version(): a failing SG_GET_VERSION_NUM printed its
    warning via an undefined name, so it raised NameError instead.
  * fix get_sub_page(): the sense buffer length was set on the wrong object,
    so the kernel was told it was 0 bytes and returned no sense data.
  * fix identifyLUN(): decode the kernel's scsi_idlun (native dev_id) so
    channel and id are no longer swapped, and host numbers above 127 are no
    longer negative (drobom info config, -v 16 trace.)

0.6.2.2: 2010/02/06
-------------------

   * fix for Redhat/python 2.4 wanting to partition everything!

0.6.2.1: 2009/12/30
-------------------

   * fix for Drobo S slotinfo firmware bug (reports 8 instead of 5.)

0.6.2:  2009/12/26
------------------

  * GUI Option setting, thresholds, etc...
  * changed command line syntax to more consistently set <item>
  * documented option setting in CLI
  * more input checking to CLI.
  * improved LUN info display accuracy.
  * debugged DroboPRO support (debugged via google group. Thanks All!)
  * added scsi to info printing options in CLI.

0.6.1: October 7th, 2009
------------------------

  * DroboPro identification fix by spobole, 
  * parted fix from Patrick Bills
  * Looked at newly released DMIP. Found option2 commands for settings.
  * Option query/set implemented (but cannot test for DroboPRO)
  * migrated source code managers from subversion to git
  * minor: consistency of default location to look for fw between lib and gui.
  * fixed that inability to read lun size on old firmwares crashed GUI.
    GUI now works all the way back to the beginning.

0.6.0
-----

  * Major: GUI heavily re-vamped.  Behaves better. 
  * Major: DroboPRO support added (thanks to lemonizer on the google group!)
  * Major: GUI gets online documentation.
  * GUI: layouts replaced fixed dimensions. 
  * GUI: added Rename support 
  * GUI: added Load firmware button 
  * GUI: Show Diagnostics (equiv. to diagprint.)
  * GUI: Name display to main display. Easier to tell which Drobo is which.
  * GUI: added mount point display to space used tool tip.
  * api: added ability to detect un-documented feature bit settings.
  * ddiag.c becomes drobom diagprint
  * catlee fixes the packaging again. 

0.5.0 2009/01/30
----------------

  * replaced DroboDMP C extension by DroboIOctl python class.  no need
    for architecture dependent packages. this also means you can just 
    try it out straight after download, no need to install anything.
    much easier. fewer build dependencies as well.
  * device detection completely re-written.  It works better now.
  * Now displays mount points where it makes sense to do so.
  * status command output better/different. shows all devices and mount points
    for each drobo.
  * full Multiple LUN support.    should do what is expected.
  * full multiple Drobo support.  should do what is expected.
  * added modular info printing with csv list of outputs.
  * firmware 1.3.0, has enabled formerly broken functionality which
    name makes the sync command rename every drobo to 'hi there'.
    fixed.
  * added 'name' command, to name Drobo. (functionality of Sync)
  * corrected detection logic, which depended on formerly immutable name. 
    (affects fw >= 1.3.0) now use same mechanisms as sg_scan from sg3_utils.
  * documentation moved into restructured text. now shows up on the web.
    web site and documntation now consistent.

0.4.0 Release: 2008/12/28
-------------------------

  * IMPORTANT: firmware 1.3.0 support added.  Earlier drobo-utils 
     versions will not recognize a drobo running 1.3.0 or later.
  * CLI is more normal...  use getopts (got rid of device as fixed place arg.)
     now allows command to be an option as well.  
  * Changed DEBUG from a flag requiring code modification, to a bitmap
     honored as a command-line argument (--verbose)
  * misc. improvements in documentation.
  * droboview now only launches for first drobo found in list, and stays foreground
     always.
  * drobom list returns a list that is easier to parse (suitable for use in backticks.)
  * droboview is now just a stub for drobom view. reduced overhead.
  * now reports VERSION id in usage.  There is a -V option too.


0.3.3 release: 2008/11/05
-------------------------

  * lunsize display bug fixed.
  * Chris's man pages & help improvements.
  * more messaging fixups for when you don't invoke as root.
  * fix for ubuntu Intrepid deciding all disks are Drobos. !!
  * fixed all the firmware info parsing issues.
  * firmware load issues should all be gone.
  * Brad's fix for ddiag.c, done properly this time (my bad!)

0.3.2 release 2008/10/25
------------------------

  * for v1, still don't understand index.rst file.
  * after testing, some more fixes, v2 downloads now work.
  * fixes received from Brad Guillory for v2/tdz firmware downloading.
     they don't do any harm afaict, don't have a v2 to test with.
  * added fwload directive to drobom.
  * added root user check to give a bigger hint.

0.3.1 release 2008/10/01
------------------------

  * OK I know checking for firmware updates doesn't work
     right now, but I need to find out why.  code seems correct.
     there is a bugfix for getting rid of the 'licensed' part.

  * fixes for firmware validation:
       * header CRC on 32bit was broken on 64bit.
       * 32 vs. 64 bit kludge needed.

0.3.0 release 2008/09/04
------------------------

  * added ability to format Drobo
  * added ability to set lun sizes
  * bugfix for firmware:
        * CRC32 on 32bit intel. (use signed, instead of unsigned.)
        * fixed header CRC not validating.
  * added ability to set time on Drobo.
  * added simulation mode to Drobo.py, development aid.
  * some work done towards lunsize setting, but incomplete.
  * some work towards indicating relay out progress, not tested...

0.2.2 Released 2008/08/08
-------------------------

  * firmware uploads work with .tdz as well
  * tested with older firmwares, contains a few fixes to improve compatibility.
     works well enought upgrade old firmwares.

0.2.1 Released: 2008/08/06 
--------------------------

  * uploaded to drobospace & sourceforge.
  * firmware upload works for .tdf's 
     
0.1.1 Initial version uploaded  april 2008
------------------------------------------

  * to drobospace.com and sf.net
  * confirmed to work with firmware 1.1.1
  * shows status, disk slots filled, model, etc..
  * shows fw version loaded.
  * blink & standby commands work.
