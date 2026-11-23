==============================================================
drobom \- CLI for managing data robotics (drobo) storage units
==============================================================

--------------------------------------------------------------
drobom \- CLI for managing data robotics (drobo) storage units
--------------------------------------------------------------

:Manual section: 8
:Date: 16 October 2009
:version: 0.6.x
:Manual group: drobo-utils


SYNOPSIS
========

drobom _command_ _arguments_

DESCRIPTION
===========

Options is one of:

* *\-c, \-\-command* the command to execute.
* *_\-d, \-\-device* the device to operate on (default searches all devices and picks first one found.)
* *\-h, \-\-help* print a usage message
* *\-n, \-\-no* answer all questions no (generally does not do any damage)
* *\-s, \-\-string \<string\>* sometimes new models are not recognized by drobom. See documentation.
* *\-v, \-\-verbose* verbosity, a bit-field to trigger increased output as needed, mostly for debugging. 
   * *1* - General, 
   * *2* - Hardware Dialog, 
   * *4* - Initiation, 
   * *8* - Raw returns, 
   * *16* - Detection
   * *64* will print everything... (default: 0, as terse as possible.)
   * *128* enables simulation mode (for testing when no Drobo is available. Dangerous!)
* *\-V, \-\-version* print the version id.
* *\-y, \-\-yes* answer all questions yes (generally overwrites stuff) 


Command is one of:

* *blink* identify the drobo by making the lights blink
* *diag* dump diagnostics file into /tmp directory
* *diagprint \<diagdumpfile\>*
  print diagnostics file to standard output \<diagdumpfile\> the diagnostic dump file to read.
* *fwcheck* query 
  drobo.com for updates to firmware for the given Drobo
* *fwload  \<fwimage\>*
  load a specific firmware for the given Drobo. Arguments: <fwimage>
  the firmware file to load.
* *fwupgrade* 
  upgrade the firmware to the latest and greatest, recommended by DRI
* *help*
  print this text
* *info*  <toprint>
  print information on a Drobo. The <toprint> argument is a comma separated list of the values below (default is to print all of them): config, capacity (protected free, used and total; newer firmware adds lines for the unprotected total and the "PT" capacity set), protocol, settings, slots, firmware, status, options, luns, scsi, health (redundancy, redundancy level (single, dual or undetermined, derived from the disk and usable capacities), extended status, severity (red, yellow or green, by the vendor's rule), data protection progress), drives (per drive health, type, temperature, life remaining, errors, rotational speed in RPM ('-' for a drive that does not spin, '?' if unknown), model, serial; newer firmware), volumes (volume names, sizes and formats, and on units that report them, the initiators using each volume; newer firmware)
* *list*
  show device files for all Drobos found.
* *set \<options\> \<value\>*
  set firmware options, such as ipaddress, etc... use 'info settings'
* *set name  <newname>*
  Set the name of the Drobo to the given value ( only firmware > 1.3.0 supports this command )
* *set lunsize*  
  Set the size of LUNS on device. Arguments: \<sz\>
  integer number of TiB to set the lunsize to

  Note: After execution, Drobo reboots, wait a few minutes before accessing again

* *set time* - sets various Drobo's clock to UTC
* *shutdown*
  shutdown drobo (DRI calls this 'standby')
* *status*
  report how is the Drobo doing.  While data protection (relay out) is running,
  the second and later runs also estimate the time left and when it will be
  done ("about 2 hours left", "done around 15:26").  The unit only reports the
  work left, so the estimate comes from how fast that falls since the first run.
* *time*
  print the Drobo's clock, then a 'Drobo clock:' line with the unit's own
  wall clock and UTC offset (e.g. 2026-08-19 16:19:07 (UTC+02:00); 'UTC offset
  unknown' when the unit's offset is not plausible, as on first generation
  units).  The same line follows the time in the header of *info*.
* *device*
  raw block device of a drobo (i.e. /dev/sdz) . If not given, assumes all attached drobos.
* *view*
  Start up the graphical user interface.

FILES
=====

$XDG_STATE_HOME/drobo-utils/relayout-<name>.json (default ~/.local/state/drobo-utils/)
  starting point of a running relay out, used for the status estimate.
  Removed when the relay out completes.  Safe to delete.

ENVIRONMENT
===========

DROBO_UTILS_STATE_DIR
  directory to keep the relay out state in, instead of the default above.

This manual page was written by Chris AtLee <chris@atlee.ca> for the Debian
project (but may be used by others).
