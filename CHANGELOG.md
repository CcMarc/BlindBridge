# BlindBridge Changelog

## v1.0.0 (11-23-2026)

First public release.

- A bridge for the Raspberry Pi that moves MySmartBlinds tilt motors over
  Bluetooth, using the keys from the phone app's backup file.
- A server with a web page for phones and computers: open, close and set
  any angle for one blind or all of them, with a picture of each blind's
  slats and a battery meter when the motor reports a level.
- An accent color you choose under Settings, 12 or 24 hour times, and
  light and dark mode.
- Schedules by clock time, sunrise or sunset, with an offset and a choice
  of days.
- An API call for Shortcuts and home automation.
- An after move check that listens to the motor and marks a blind that
  acknowledged a move and did not turn.
- A workaround for the slow Bluetooth link that made a motor reachable
  only once per start of the Pi, with a restart of the Pi as a last
  resort.
- A Settings page: scan, import, rename, remove, update from a zip, logs,
  restart, shut down, optional Wi-Fi off while a cable is working, and a
  debug zip.
- A clock on the page that shows when the Pi's time is not synced.
- Support for more than one bridge on one server.
- Install and hardening scripts for Raspberry Pi OS.
