"""Tests for netwatch: reading the network state from a stand-in /sys tree."""

import tempfile
import unittest
from pathlib import Path

import netwatch


def make_sys(root: Path, eth_carrier="1", eth_up=True, wifi_soft="0", with_wifi=True, with_eth=True) -> Path:
    sys = root / "class"
    (sys / "net" / "lo").mkdir(parents=True)
    (sys / "net" / "lo" / "type").write_text("772\n")
    if with_eth:
        eth = sys / "net" / "eth0"
        (eth / "device").mkdir(parents=True)
        (eth / "type").write_text("1\n")
        if eth_up:
            (eth / "carrier").write_text(eth_carrier + "\n")
    if with_wifi:
        wlan = sys / "net" / "wlan0"
        (wlan / "device").mkdir(parents=True)
        (wlan / "phy80211").mkdir()
        (wlan / "type").write_text("1\n")
        (wlan / "carrier").write_text("1\n")
        p2p = sys / "net" / "p2p-dev-wlan0"          # no device link: not a real interface
        p2p.mkdir(parents=True)
        (p2p / "type").write_text("1\n")
        for index, kind in enumerate(("bluetooth", "wlan")):
            entry = sys / "rfkill" / f"rfkill{index}"
            entry.mkdir(parents=True)
            (entry / "type").write_text(kind + "\n")
            (entry / "soft").write_text((wifi_soft if kind == "wlan" else "0") + "\n")
    return sys


IP_BOTH = """2: eth0    inet 192.168.1.136/24 brd 192.168.1.255 scope global dynamic noprefixroute eth0\\       valid_lft 86000sec preferred_lft 86000sec
3: wlan0    inet 192.168.1.142/24 brd 192.168.1.255 scope global dynamic noprefixroute wlan0\\       valid_lft 86000sec preferred_lft 86000sec
"""
IP_WIFI = IP_BOTH.splitlines()[1] + "\n"


class NetwatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_interfaces_are_told_apart(self):
        sys = make_sys(self.root)
        self.assertEqual(netwatch.interfaces(sys), {"eth0": "wired", "wlan0": "wifi"})
        self.assertEqual(netwatch.cabled(sys), ["eth0"])
        self.assertEqual(netwatch.addresses(IP_BOTH, sys), {"wired": "192.168.1.136", "wifi": "192.168.1.142"})
        self.assertEqual(netwatch.addresses(IP_WIFI, sys), {"wifi": "192.168.1.142"})
        self.assertEqual(netwatch.interfaces(self.root / "nowhere"), {})

    def test_a_cable_counts_only_when_live_and_addressed(self):
        self.assertTrue(netwatch.cable_ready(IP_BOTH, make_sys(self.root / "a")))
        self.assertFalse(netwatch.cable_ready(IP_WIFI, make_sys(self.root / "b")))                    # plugged in, no address yet
        self.assertFalse(netwatch.cable_ready(IP_BOTH, make_sys(self.root / "c", eth_carrier="0")))   # unplugged
        self.assertFalse(netwatch.cable_ready(IP_BOTH, make_sys(self.root / "d", eth_up=False)))      # interface down
        self.assertFalse(netwatch.cable_ready(IP_BOTH, make_sys(self.root / "e", with_eth=False)))

    def test_wifi_switch_state(self):
        self.assertIs(netwatch.wifi_blocked(make_sys(self.root / "a")), False)
        self.assertIs(netwatch.wifi_blocked(make_sys(self.root / "b", wifi_soft="1")), True)
        self.assertIsNone(netwatch.wifi_blocked(make_sys(self.root / "c", with_wifi=False)))
        self.assertIsNone(netwatch.wifi_blocked(self.root / "nowhere"))
        self.assertFalse(netwatch.can_switch(str(self.root / "no-such-device")))
        device = self.root / "rfkill-device"
        device.write_text("")
        self.assertTrue(netwatch.can_switch(str(device)))


if __name__ == "__main__":
    unittest.main()
