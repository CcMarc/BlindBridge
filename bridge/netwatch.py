"""What this machine's network looks like, read from /sys, and its Wi-Fi switch.

Shared by the bridge and the server. Nothing here needs root. Switching Wi-Fi
uses `rfkill`, which works for a user who may write to /dev/rfkill (on
Raspberry Pi OS: members of the `netdev` group).

It serves one optional setting: keep Wi-Fi off while a network cable is
working. Wi-Fi must come back by itself when the cable stops working, or the
machine is lost to anyone who is not standing next to it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SYS = Path("/sys/class")
RFKILL_DEVICE = "/dev/rfkill"
BLOCK = ["rfkill", "block", "wlan"]
UNBLOCK = ["rfkill", "unblock", "wlan"]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:          # reading `carrier` of an interface that is down fails; that is a no
        return ""


def interfaces(sys: Path = SYS) -> dict[str, str]:
    """Real network interfaces by name: "wired" or "wifi". Loopback, tunnels and bridges are left out."""
    found = {}
    try:
        entries = sorted((sys / "net").iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name == "lo" or not (entry / "device").exists():
            continue
        if (entry / "wireless").exists() or (entry / "phy80211").exists():
            found[entry.name] = "wifi"
        elif _read(entry / "type") == "1":               # Ethernet
            found[entry.name] = "wired"
    return found


def cabled(sys: Path = SYS) -> list[str]:
    """Wired interfaces that have a live cable in them."""
    return [name for name, kind in interfaces(sys).items()
            if kind == "wired" and _read(sys / "net" / name / "carrier") == "1"]


def wifi_blocked(sys: Path = SYS) -> bool | None:
    """Is Wi-Fi switched off by its software switch? None if this machine has no Wi-Fi."""
    states = []
    try:
        entries = list((sys / "rfkill").iterdir())
    except OSError:
        return None
    for entry in entries:
        if _read(entry / "type") == "wlan":
            states.append(_read(entry / "soft") == "1")
    return any(states) if states else None


def can_switch(device: str = RFKILL_DEVICE) -> bool:
    return os.access(device, os.W_OK)


def addresses(ip_output: str, sys: Path = SYS) -> dict[str, str]:
    """The first IPv4 address of each kind, from the output of `ip -4 -o addr show scope global`."""
    kinds = interfaces(sys)
    found: dict[str, str] = {}
    for line in ip_output.splitlines():
        match = re.match(r"\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
        if match and kinds.get(match.group(1)) in ("wired", "wifi"):
            found.setdefault(kinds[match.group(1)], match.group(2))
    return found


def cable_ready(ip_output: str, sys: Path = SYS) -> bool:
    """A cable is in, and the network on it has given this machine an address."""
    if not cabled(sys):
        return False
    kinds = interfaces(sys)
    live = set(cabled(sys))
    for line in ip_output.splitlines():
        match = re.match(r"\s*\d+:\s+(\S+)\s+inet\s+\d+\.\d+\.\d+\.\d+/", line)
        if match and match.group(1) in live and kinds.get(match.group(1)) == "wired":
            return True
    return False
