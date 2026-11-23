# Security Policy

BlindBridge runs on your network, holds the keys to your blinds and can
install software on the Pi it runs on, so security reports get priority
over every other kind of issue.

## Reporting a vulnerability

Please do NOT open a public GitHub issue for anything security related.

Report privately via **GitHub's private vulnerability reporting** on this
repository (Security tab > Report a vulnerability). Include the BlindBridge
version, your Pi model and operating system version, and reproduction
steps if you have them.

You will get an acknowledgement, and a fix or a considered response
before any public disclosure. Please allow a reasonable disclosure
window. This project is maintained on a best effort basis.

## Supported versions

| Version | Supported |
| ------- | --------- |
| Latest release | Yes |
| Older releases | Upgrade to the latest release first |

## Scope notes

- BlindBridge is built for a home network or a VPN. The page is plain
  HTTP by design. Reports that amount to "the page is not safe when
  exposed to the internet" are working as intended. The README says not
  to do that.
- Settings actions, which include installing an update and restarting
  the Pi, need a signed in browser session. Any way to reach them with
  the API token, a bridge token or no sign in at all is firmly in scope.
- The blinds' keys stay in `bridge/blinds.json` on the bridge. Any way to
  get them into the server's data, a log, a download or the debug zip is
  in scope.
- A bridge accepts a software update from its server only on the same
  machine or over HTTPS, unless its owner has set `updates_over_http`.
  Bypasses of that check are in scope.
- The update zip is not signed. The person who uploads it, signed in and
  confirming with the password, is trusted. Reports that amount to "an
  administrator can install code" are working as intended.
- The motors' own Bluetooth protocol and its key scheme belong to the
  motors. Weaknesses there cannot be fixed here.

Never attach a debug zip, a backup file, `blinds.json`, `config.json` or
`bridge.json` to a public issue.
