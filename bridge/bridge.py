#!/usr/bin/env python3
"""BlindBridge bridge for a Raspberry Pi (or any Linux box with Bluetooth LE).

Talks to MySmartBlinds tilt motors over Bluetooth and takes its orders from a
BlindBridge server over the sync protocol in docs/PROTOCOL.md. The blind list
and the keys live here, in blinds.json. Keys are never sent to the server.

Needs Python 3.10+ and the `bleak` package.

    python3 bridge.py scan                       # look for motors nearby
    python3 bridge.py import backup.json         # load blinds from the app's backup
    python3 bridge.py list
    python3 bridge.py move Kitchen close         # test one blind, no server needed
    python3 bridge.py setup --server URL --token TOKEN --name livingroom
    python3 bridge.py run                        # the long-running service
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import netwatch

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
INSTALL_DIR = HERE.parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))

MOTOR_NAME = "SmartBlind_DFU"
KEY_UUID = "00001409-1212-efde-1600-785feabcd123"
POS_UUID = "00001403-1212-efde-1600-785feabcd123"   # the motor calls it "Ang"
ACK_UUID = "00001503-1212-efde-1600-785feabcd123"   # the motor calls it "ACK"
STATUS_UUID = "00001402-1212-efde-1600-785feabcd123"   # "Stat"
SENSORS_UUID = "00001651-1212-efde-1600-785feabcd123"  # "Sensors"
STATE_UUID = "00000add-0000-1000-8000-00805f9b34fb" # service data in the advertisement
SOLAR_CHARGING = 0x00040000     # bits in the first four status bytes, little-endian
USB_CHARGING = 0x00080000

ACK_WAIT = 4.0          # seconds to wait for the motor to acknowledge a position
AFTER_ACK = 0.5         # then stay connected this long, so the motor is under way before the link closes
HEALTH_EVERY = 900.0    # read battery and charging state at most this often per motor
HEALTH_TIMEOUT = 3.0    # one such read
CAPTURE_FOR = 3600.0    # capture mode switches itself off after this long
UNCONFIRMED = "sent, but the motor did not confirm it"
NET_EVERY = 8.0         # seconds between looks at the network cable
NET_GRACE = 20.0        # after "Wi-Fi off on cable" is chosen, Wi-Fi stays on this long so the page can say so
CONNECT_TIMEOUT = 75.0  # seconds a connection may take; see BleakRadio.purge() for why so long
PURGE_SETTLE = 0.5      # seconds the system is given to drop a cancelled connection attempt
REBOOT_GAP = 1800.0     # the machine restarts itself over stuck Bluetooth at most this often
REBOOT_NOT_BEFORE = 300.0   # and never in its first five minutes after starting up
PENDING_FRESH = 900.0   # a move saved across such a restart is carried out if it is younger than this
RESUME_DELAY = 20.0     # after a restart, give Bluetooth this long to come up before that move
REBOOT_NOTE = "Bluetooth is stuck. Restarting the Pi, then moving"
CHECK_AFTER = 10.0      # seconds after an acknowledged move before the motor's broadcast is checked
CHECK_AGAIN = 15.0      # and once more this much later, before saying it did not move
CHECK_WITHIN = 5        # a broadcast position this close to the one asked for counts as there
NOT_MOVED = "Motor says it did not move"
STOPPED_SHORT = "Motor says it stopped partway"

POS_MIN, POS_MAX = 0, 200
POSITION_WORDS = {"open": 100, "close": 200, "closed": 200, "up": 0, "half": 150}
LONG_POLL = 25
HEARTBEAT = 10          # while a move is running, check in with the server this often

CAPTURE_KEEP = 40                   # captures kept on disk; the oldest are dropped
CAPTURE_EVENTS = 1500               # most events recorded in one capture
CAPTURE_BUNDLE_BYTES = 450 * 1024   # most capture data sent to the server in one answer
CAPTURE_MOVE_LISTEN = 14.0          # seconds to keep listening after a recorded move
CAPTURE_READ_EVERY = 3.0            # while listening, read every value again this often
STEP_TIMEOUT = 6.0                  # one read or subscribe during a capture


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

def parse_hex(text: str) -> bytes:
    """Hex in any common spelling: 'a1b2', 'A1:B2', '0xA1, 0xB2'."""
    cleaned = text.replace("0x", "").replace("0X", "")
    digits = "".join(ch for ch in cleaned if ch not in " :,-[]")
    if not digits or len(digits) % 2:
        raise ValueError(f"not a whole number of hex bytes: {text!r}")
    try:
        return bytes.fromhex(digits)
    except ValueError:
        raise ValueError(f"not hex: {text!r}") from None


def normalize_mac(text: str) -> str:
    raw = parse_hex(text)
    if len(raw) != 6:
        raise ValueError(f"a Bluetooth address has 6 bytes: {text!r}")
    return ":".join(f"{b:02X}" for b in raw)


def clean_name(text, limit: int = 48) -> str:
    """A name as one printable line of at most `limit` characters."""
    return "".join(ch for ch in str(text) if ch.isprintable()).strip()[:limit]


def parse_position(text: str) -> int:
    word = str(text).strip().lower()
    if word in POSITION_WORDS:
        return POSITION_WORDS[word]
    try:
        value = int(word)
    except ValueError:
        raise ValueError(f"position must be 0-200 or one of {sorted(POSITION_WORDS)}") from None
    if not POS_MIN <= value <= POS_MAX:
        raise ValueError("position must be 0-200")
    return value


def blinds_from_backup(data) -> list[dict]:
    """Pull blinds out of the MySmartBlinds app's backup JSON.

    Every object carrying encodedMacAddress and encodedPasskey is a blind. The
    backup stores the address byte-reversed compared with what the motor
    advertises, so it is flipped here.
    """
    found: list[dict] = []
    room_names: dict[str, str] = {}

    def collect_rooms(node, parent: str) -> None:
        if isinstance(node, dict):
            if parent == "rooms" and "id" in node and isinstance(node.get("name"), str):
                room_names[str(node["id"])] = node["name"].strip()
            for key, value in node.items():
                collect_rooms(value, key)
        elif isinstance(node, list):
            for value in node:
                collect_rooms(value, parent)

    def walk(node, room: str, parent: str) -> None:
        if isinstance(node, dict):
            if node.get("deleted"):
                return
            if "encodedMacAddress" in node and "encodedPasskey" in node:
                try:
                    mac_raw = base64.b64decode(node["encodedMacAddress"], validate=True)
                    key = base64.b64decode(node["encodedPasskey"], validate=True)
                except (ValueError, TypeError):
                    return
                if len(mac_raw) != 6 or not 1 <= len(key) <= 32:
                    return
                room = room or room_names.get(str(node.get("roomId", "")), "")
                name = str(node.get("name") or "").strip()
                if room and room.lower() not in name.lower():
                    name = f"{room} {name}".strip()
                found.append(
                    {
                        "name": name,
                        "mac": ":".join(f"{b:02X}" for b in reversed(mac_raw)),
                        "key": key.hex(),
                    }
                )
                return
            if parent == "rooms" and isinstance(node.get("name"), str):
                room = node["name"].strip()
            for key, value in node.items():
                walk(value, room, key)
        elif isinstance(node, list):
            for value in node:
                walk(value, room, parent)

    collect_rooms(data, "")
    walk(data, "", "")
    unique: dict[str, dict] = {}
    for blind in found:
        unique[blind["mac"]] = blind
    return list(unique.values())


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

class Store:
    """blinds.json and bridge.json in one folder, readable by the owner only."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.blinds_path = self.folder / "blinds.json"
        self.config_path = self.folder / "bridge.json"
        self.blinds: list[dict] = []
        self.config: dict = {}
        self.load()

    def load(self) -> None:
        if self.blinds_path.exists():
            self.blinds = json.loads(self.blinds_path.read_text(encoding="utf-8"))
        if self.config_path.exists():
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)

    def save_blinds(self) -> None:
        self._write(self.blinds_path, self.blinds)

    def save_config(self) -> None:
        self._write(self.config_path, self.config)

    # ---- a move saved across a restart of the Pi

    @property
    def pending_path(self) -> Path:
        return self.folder / "pending.json"

    @property
    def stuck_path(self) -> Path:
        return self.folder / "stuck.txt"

    def save_pending(self, moves: list[dict]) -> None:
        self._write(self.pending_path, moves)

    def take_pending(self) -> list[dict]:
        """The saved moves, oldest first. The file is removed first, so they can never be run twice."""
        try:
            text = self.pending_path.read_text(encoding="utf-8")
            os.remove(self.pending_path)
            moves = json.loads(text)
        except (OSError, ValueError):
            return []
        return [m for m in moves if isinstance(m, dict)] if isinstance(moves, list) else []

    @property
    def wifi_off_marker(self) -> Path:
        return self.folder / "wifi_off"

    # ---- captures: recordings of what a motor said, for working out its data

    @property
    def captures_dir(self) -> Path:
        return self.folder / "captures"

    def capture_names(self) -> list[str]:
        """Oldest first. The names start with a timestamp, so sorting orders them."""
        try:
            return sorted(p.name for p in self.captures_dir.glob("*.json") if p.is_file())
        except OSError:
            return []

    def save_capture(self, record: dict, stamp: str) -> str:
        self.captures_dir.mkdir(mode=0o700, exist_ok=True)
        slug = "".join(ch if ch.isalnum() else "-" for ch in record["blind"]["name"].lower())
        slug = "-".join(part for part in slug.split("-") if part)[:24] or "blind"
        name = f"{stamp}-{slug}-{record['kind']}.json"
        n = 2
        while (self.captures_dir / name).exists():
            name = f"{stamp}-{slug}-{record['kind']}-{n}.json"
            n += 1
        path = self.captures_dir / name
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"))
        os.replace(tmp, path)
        for old in self.capture_names()[:-CAPTURE_KEEP]:
            try:
                os.remove(self.captures_dir / old)
            except OSError:
                pass
        return name

    def bundle_captures(self, limit: int = CAPTURE_BUNDLE_BYTES) -> dict:
        """The newest captures that fit in one answer, compressed. Oldest first."""
        names = self.capture_names()
        picked: list[dict] = []
        size = 0
        for name in reversed(names):
            try:
                text = (self.captures_dir / name).read_text(encoding="utf-8")
                record = json.loads(text)
            except (OSError, ValueError):
                continue
            cost = len(zlib.compress(text.encode("utf-8"), 6))
            if picked and size + cost > limit:
                break
            record["file"] = name
            picked.append(record)
            size += cost
        picked.reverse()
        packed = zlib.compress(json.dumps(picked, separators=(",", ":")).encode("utf-8"), 6)
        return {"count": len(picked), "total": len(names), "z": base64.b64encode(packed).decode("ascii")}

    def clear_captures(self) -> int:
        removed = 0
        for name in self.capture_names():
            try:
                os.remove(self.captures_dir / name)
                removed += 1
            except OSError:
                pass
        return removed

    def find(self, who: str) -> dict | None:
        """Look a blind up by name, id, or address."""
        want = who.strip().lower()
        for blind in self.blinds:
            if want in (blind["name"].lower(), blind["id"], blind["mac"].lower()):
                return blind
        return None

    def rename(self, blind: dict, name: str) -> str:
        """Give a blind a new name. ValueError says why not."""
        name = clean_name(name)
        if not name:
            raise ValueError("a name is required")
        if any(other is not blind and other["name"].lower() == name.lower() for other in self.blinds):
            raise ValueError("another blind already has that name")
        blind["name"] = name
        return name

    def upsert(self, name: str, mac: str, key_hex: str) -> tuple[dict, bool]:
        mac = normalize_mac(mac)
        key_hex = parse_hex(key_hex).hex()
        blind_id = mac.replace(":", "").lower()
        name = clean_name(name) or blind_id
        for blind in self.blinds:
            if blind["id"] == blind_id:
                blind.update(name=name, key=key_hex)
                return blind, False
        taken = {b["name"].lower() for b in self.blinds}
        base, n = name, 2
        while name.lower() in taken:
            name = f"{base} {n}"
            n += 1
        blind = {"id": blind_id, "name": name, "mac": mac, "key": key_hex, "pos": -1, "moved": 0}
        self.blinds.append(blind)
        return blind, True


# --------------------------------------------------------------------------
# Bluetooth
# --------------------------------------------------------------------------

NOT_FOUND = "not found: out of range, or the phone app is connected"


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:80]


def parse_sensors(data: bytes) -> dict | None:
    """Battery level from the motor's "Sensors" value, or None if it holds no reading.

    The value is 14 bytes with the battery percentage first. A motor that has
    nothing to report answers with 0xFF bytes.
    """
    if len(data) < 14 or data[0] > 100:
        return None
    return {"battery": data[0]}


def parse_status(data: bytes) -> dict | None:
    """Charging state from the motor's "Stat" value, or None if it is blank."""
    if len(data) < 4 or all(byte == 0xFF for byte in data[:4]):
        return None
    flags = int.from_bytes(data[:4], "little")
    return {"charging": "usb" if flags & USB_CHARGING else ("solar" if flags & SOLAR_CHARGING else "")}


def adv_position(adv) -> int | None:
    """The position a motor broadcasts about itself, or None.

    Each motor advertises eight bytes of service data: its address, a flag
    byte, and its current position. It can be read without connecting.
    """
    data = (getattr(adv, "service_data", None) or {}).get(STATE_UUID)
    if data is None or len(data) != 8 or data[-1] > 200:
        return None
    return data[-1]


def _plain(raw: bytes) -> str:
    """Command output as text, without the colour codes bluetoothctl prints."""
    text = raw.decode("utf-8", "replace")
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()


def _adv_dict(adv) -> dict:
    """What a motor broadcasts about itself, as plain JSON."""
    return {
        "name": adv.local_name or "",
        "rssi": adv.rssi,
        "tx_power": adv.tx_power,
        "manufacturer": {f"0x{int(cid):04x}": bytes(v).hex() for cid, v in (adv.manufacturer_data or {}).items()},
        "service_data": {str(u): bytes(v).hex() for u, v in (adv.service_data or {}).items()},
        "service_uuids": [str(u) for u in (adv.service_uuids or [])],
    }


class BleakRadio:
    """The real radio. bleak is imported lazily so tests run without it."""

    def __init__(self, scan_timeout: float = 15.0, connect_timeout: float = CONNECT_TIMEOUT):
        self.scan_timeout = scan_timeout
        self.ack_wait = ACK_WAIT
        self.after_ack = AFTER_ACK
        self.health_read: dict[str, float] = {}
        self.connect_timeout = connect_timeout
        self.purge_settle = PURGE_SETTLE
        self.open_bus = None                    # replaced in tests; see purge()

    async def scan(self, seconds: float = 10.0, everything: bool = False) -> list[dict]:
        from bleak import BleakScanner

        seen = await BleakScanner.discover(timeout=seconds, return_adv=True)
        out = []
        for device, adv in seen.values():
            name = adv.local_name or device.name or ""
            if everything or name.startswith("SmartBlind"):
                out.append({"mac": device.address.upper(), "name": name, "rssi": adv.rssi,
                            "pos": adv_position(adv)})
        return sorted(out, key=lambda item: -(item["rssi"] or -999))

    async def _find(self, mac: str):
        from bleak import BleakScanner

        hit: dict = {}
        found = asyncio.Event()

        def on_seen(device, adv) -> None:
            if device.address.upper() == mac:
                hit["device"], hit["rssi"], hit["adv"] = device, adv.rssi, adv
                found.set()

        async with BleakScanner(detection_callback=on_seen):
            try:
                await asyncio.wait_for(found.wait(), self.scan_timeout)
            except asyncio.TimeoutError:
                return None, None, None
        return hit["device"], hit["rssi"], hit["adv"]

    async def look(self, mac: str, timeout: float = 5.0, linger: float = 1.5):
        """Listen for a motor's broadcast without connecting. Returns (position or None, rssi or None).

        Keeps listening for `linger` seconds after the first broadcast and
        goes by the last one, so a position the motor is just changing is
        not read too early.
        """
        from bleak import BleakScanner

        mac = mac.upper()
        hit: dict = {}
        found = asyncio.Event()

        def on_seen(device, adv) -> None:
            if device.address.upper() == mac:
                hit["rssi"], hit["pos"] = adv.rssi, adv_position(adv)
                found.set()

        try:
            async with BleakScanner(detection_callback=on_seen):
                try:
                    await asyncio.wait_for(found.wait(), timeout)
                except asyncio.TimeoutError:
                    return None, None
                await asyncio.sleep(linger)
        except asyncio.CancelledError:
            raise
        except Exception:  # hardware boundary
            return None, None
        return hit.get("pos"), hit.get("rssi")

    async def session(self, mac: str, key: bytes, pos: int, detail: dict | None = None, purge: bool = False):
        """Connect, send the key, send the position, wait for the acknowledgement.

        Returns (code, error, rssi). code is "ok", "unreachable" (never got a
        connection), "rejected" (connected, but a write failed or the motor
        hung up, which is what a wrong key looks like) or "unconfirmed" (the
        position was written but the motor never acknowledged it).

        The motor acknowledges a position on its ACK characteristic within a
        quarter of a second, and the link is closed half a second after that.
        `detail`, if given, is filled with what was seen: `ack` (hex), `was`
        (the position it broadcast beforehand), `adv` (its broadcast state
        bytes), `connect` (seconds the connection took) and, when they were
        read, `battery`, `charging` and `stat`. With `purge`, purge() runs
        before the connection.
        """
        from bleak import BleakClient

        mac = mac.upper()
        rssi = None
        began = None
        stage = "unreachable"
        written = confirmed = False
        detail = {} if detail is None else detail
        try:
            device, rssi, adv = await self._find(mac)
            if device is None:
                return "unreachable", NOT_FOUND, None
            state = (getattr(adv, "service_data", None) or {}).get(STATE_UUID)
            if state:
                detail["adv"] = bytes(state).hex()
            if adv_position(adv) is not None:
                detail["was"] = adv_position(adv)
            if purge:
                device = await self._purged(mac, device, detail.setdefault("notes", []))
                if not detail["notes"]:
                    del detail["notes"]
                if device is None:
                    return "unreachable", NOT_FOUND, rssi
            began = time.monotonic()
            async with BleakClient(device, timeout=self.connect_timeout) as client:
                detail["connect"] = round(time.monotonic() - began, 1)
                stage = "rejected"
                await client.write_gatt_char(KEY_UUID, key, response=True)

                acked = asyncio.Event()
                armed = False

                def on_ack(_, data) -> None:
                    if armed and any(byte != 0xFF for byte in data) and not acked.is_set():
                        detail["ack"] = bytes(data[:6]).hex()
                        acked.set()

                can_ack = True
                try:
                    await asyncio.wait_for(client.start_notify(ACK_UUID, on_ack), STEP_TIMEOUT)
                except Exception as exc:
                    can_ack = False
                    detail.setdefault("notes", []).append(f"no acknowledgements: {_short(exc)}")
                # Battery and charging are read before the move. Once a motor has
                # acknowledged a position it stops answering and the link times out.
                if time.monotonic() - self.health_read.get(mac, -HEALTH_EVERY) >= HEALTH_EVERY:
                    self.health_read[mac] = time.monotonic()
                    await self._health(client, detail)
                armed = True
                await client.write_gatt_char(POS_UUID, bytes([pos]), response=True)
                written = True
                if can_ack:
                    try:
                        await asyncio.wait_for(acked.wait(), self.ack_wait)
                    except asyncio.TimeoutError:
                        pass
                    confirmed = acked.is_set()
                else:
                    confirmed = True
                if confirmed:
                    await asyncio.sleep(self.after_ack)
            return ("ok", "", rssi) if confirmed else ("unconfirmed", UNCONFIRMED, rssi)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # hardware boundary: report, never crash the service
            if confirmed:
                return "ok", "", rssi           # the motor took it; only closing the link went wrong
            if written:
                return "unconfirmed", UNCONFIRMED, rssi
            if stage == "unreachable" and began is not None:
                detail["gave_up"] = round(time.monotonic() - began, 1)      # heard, but no connection
            return stage, _short(exc), rssi

    async def _health(self, client, detail: dict) -> None:
        """Read battery and charging state into `detail`. Blank answers are skipped."""
        for uuid, parse in ((SENSORS_UUID, parse_sensors), (STATUS_UUID, parse_status)):
            if not client.is_connected:
                return
            try:
                data = bytes(await asyncio.wait_for(client.read_gatt_char(uuid), HEALTH_TIMEOUT))
            except Exception:
                continue
            detail.update(parse(data) or {})
            if uuid == STATUS_UUID and any(byte != 0xFF for byte in data):
                detail["stat"] = data[:8].hex()

    async def probe(self, mac: str, key: bytes, pos: int | None = None, listen: float = 8.0):
        """Connect and record everything the motor says. With `pos`, move it too.

        Returns (code, error, rssi, capture). code is as in session(); with a
        position, "ok" means the position was written. The capture holds the
        advertisement, the full list of services and characteristics, every
        value that can be read (at the start, every few seconds, and at the
        end) and every notification, each with its time. The key is never
        recorded and the key characteristic is never read.
        """
        from bleak import BleakClient

        mac = mac.upper()
        started = time.monotonic()
        events: list[dict] = []
        capture: dict = {"adv": None, "gatt": [], "events": events}
        secret = key.hex() if len(key) >= 4 else ""
        rssi = None
        stage = "unreachable"
        moved = hung = False

        def note(op: str, **fields) -> None:
            if len(events) < CAPTURE_EVENTS:
                events.append({"t": round(time.monotonic() - started, 3), "op": op, **fields})

        def shown(data) -> str:
            text = bytes(data).hex()
            return text.replace(secret, "<key>") if secret else text

        try:
            device, rssi, adv = await self._find(mac)
            if device is None:
                note("error", what="find", err="not found")
                return "unreachable", NOT_FOUND, None, capture
            capture["adv"] = _adv_dict(adv)
            note("found", rssi=rssi)
            problems: list = []
            device = await self._purged(mac, device, problems)
            note("purged", **({"err": problems[0]} if problems else {}))
            if device is None:
                note("error", what="find again", err="not found")
                return "unreachable", NOT_FOUND, rssi, capture
            async with BleakClient(device, timeout=self.connect_timeout) as client:
                note("connected")
                stage = "rejected"
                readable, notifying, described = [], [], []
                for service in client.services:
                    chars = []
                    for char in service.characteristics:
                        props = sorted(char.properties)
                        chars.append({
                            "uuid": char.uuid, "handle": char.handle, "props": props,
                            "descriptors": [{"uuid": d.uuid, "handle": d.handle} for d in char.descriptors],
                        })
                        if char.uuid.lower() == KEY_UUID:
                            continue
                        if "read" in props:
                            readable.append(char)
                        if "notify" in props or "indicate" in props:
                            notifying.append(char)
                        described += [d for d in char.descriptors if d.uuid[4:8].lower() in ("2901", "2904")]
                    capture["gatt"].append({"service": service.uuid, "handle": service.handle, "chars": chars})
                note("listed", services=len(capture["gatt"]), readable=len(readable), notifying=len(notifying))

                async def read_all(when: str) -> None:
                    for char in readable:
                        if not client.is_connected:
                            note("error", what=f"read {when}", err="motor hung up")
                            return
                        try:
                            data = await asyncio.wait_for(client.read_gatt_char(char), STEP_TIMEOUT)
                            note("read", when=when, uuid=char.uuid, handle=char.handle, hex=shown(data))
                        except Exception as exc:
                            note("error", what=f"read {char.uuid}", when=when, err=_short(exc))

                def on_notify(char, data) -> None:
                    note("notify", uuid=char.uuid, handle=char.handle, hex=shown(data))

                await client.write_gatt_char(KEY_UUID, key, response=True)
                note("key-written")
                for char in notifying:
                    try:
                        await asyncio.wait_for(client.start_notify(char, on_notify), STEP_TIMEOUT)
                        note("subscribed", uuid=char.uuid, handle=char.handle)
                    except Exception as exc:
                        note("error", what=f"subscribe {char.uuid}", err=_short(exc))
                for desc in described:
                    try:
                        data = await asyncio.wait_for(client.read_gatt_descriptor(desc), STEP_TIMEOUT)
                        note("descriptor", uuid=desc.uuid, handle=desc.handle,
                             char=desc.characteristic_uuid, hex=shown(data))
                    except Exception as exc:
                        note("error", what=f"descriptor {desc.handle}", err=_short(exc))
                await read_all("start")
                if pos is not None:
                    await client.write_gatt_char(POS_UUID, bytes([pos]), response=True)
                    moved = True
                    note("position-written", pos=pos)
                end = time.monotonic() + listen
                next_read = time.monotonic() + CAPTURE_READ_EVERY
                while time.monotonic() < end and client.is_connected:
                    await asyncio.sleep(0.25)
                    if time.monotonic() >= next_read and time.monotonic() < end - 1:
                        await read_all("during")
                        next_read = time.monotonic() + CAPTURE_READ_EVERY
                if client.is_connected:
                    await read_all("end")
                else:
                    hung = True
                    note("error", what="listen", err="motor hung up")
                note("done")
            if hung and not moved:
                return "rejected", "motor hung up after the key", rssi, capture
            return "ok", "", rssi, capture
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # hardware boundary: report, never crash the service
            note("error", what="session", err=_short(exc))
            if moved:
                return "ok", "", rssi, capture
            return stage, _short(exc), rssi, capture

    async def purge(self, device) -> str:
        """Make the kernel forget the slow link settings it keeps for this motor.

        The motors publish preferred connection parameters of one exchange
        every 25 to 250 ms. BlueZ reads them on each connection and hands them
        to the kernel, which keeps them for that address until the machine
        restarts, and the radio then uses the slowest value allowed. A later
        connection that has to read the motor's whole table of services again
        takes over half a minute on such a link. docs/PROTOCOL.md has the
        details.

        The kernel drops what it keeps for an address when a connection
        attempt it was asked for is cancelled before it completes. So this
        asks BlueZ to connect and, right behind it, to disconnect: the cancel
        arrives before the radio has started looking. The connection that
        follows starts on the kernel's fast defaults again.

        BlueZ drops its record of the motor when that attempt fails, so the
        caller has to find the motor again before connecting; see _purged().

        Returns "" when both requests went out, otherwise a few words for the
        log. Nothing here can stop the move: the connection that follows is
        given long enough to succeed on a slow link too.
        """
        path = (getattr(device, "details", None) or {}).get("path") if device is not None else None
        if not path:
            return "no purge: no device path"
        bus = None
        try:
            from dbus_fast import BusType, Message, MessageFlag

            def ask(member: str, flags=None):
                extra = {} if flags is None else {"flags": flags}
                return Message(destination="org.bluez", path=path, interface="org.bluez.Device1",
                               member=member, **extra)

            if self.open_bus is not None:
                bus = await self.open_bus()
            else:
                from dbus_fast.aio import MessageBus
                bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), STEP_TIMEOUT)
            # send() queues at once and in order; the reply to Connect is of no interest.
            bus.send(ask("Connect", MessageFlag.NO_REPLY_EXPECTED))
            reply = await asyncio.wait_for(bus.call(ask("Disconnect")), STEP_TIMEOUT)
            refused = getattr(reply, "error_name", None)
            if refused:
                return f"no purge: {str(refused).rsplit('.', 1)[-1]}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never in the way of the move itself
            return f"no purge: {_short(exc)}"
        finally:
            if bus is not None:
                try:
                    bus.disconnect()
                except Exception:
                    pass
        await asyncio.sleep(self.purge_settle)
        return ""

    async def _purged(self, mac: str, device, notes: list):
        """Purge, then find the motor again. Returns the device to connect to, or None if it is no longer heard."""
        problem = await self.purge(device)
        if problem:
            notes.append(problem)
            return device                       # nothing was cancelled, so the record is still good
        again, _, _ = await self._find(mac)
        return again

    async def move(self, mac: str, key: bytes, pos: int, attempts: int = 2, detail: dict | None = None):
        """Move one blind, retrying. Returns (ok, error, rssi).

        The first try purges what the kernel keeps for the motor (see purge())
        and should connect in 4 to 8 seconds. A second try does without the
        purge, in case the purge itself is ever what gets in the way; on a slow
        link that one can take a minute. A position the motor took but never
        acknowledged is sent once more only.

        If every try heard the motor and still could not connect, `detail`
        gets `stuck`: only restarting the machine is left.
        """
        error, rssi, unconfirmed, deaf = "", None, 0, 0
        detail = {} if detail is None else detail
        for attempt in range(attempts):
            seen: dict = {}
            code, error, seen_rssi = await self.session(mac, key, pos, detail=seen, purge=attempt == 0)
            rssi = seen_rssi if seen_rssi is not None else rssi
            notes = detail.get("notes", []) + seen.pop("notes", [])
            detail.update(seen)
            detail["tries"] = attempt + 1
            if notes:
                detail["notes"] = notes
            if code == "ok":
                return True, "", rssi
            detail.setdefault("notes", []).append(f"try {attempt + 1}: {error}")
            unconfirmed += code == "unconfirmed"
            if unconfirmed >= 2:
                break
            deaf += code == "unreachable" and seen_rssi is not None and error != NOT_FOUND   # heard, but no connection
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5)
        if deaf and deaf == detail.get("tries"):
            detail["stuck"] = True
        return False, error, rssi


# --------------------------------------------------------------------------
# The service loop
# --------------------------------------------------------------------------

class WifiPolicy:
    """Keeps Wi-Fi off while a network cable is working, and on otherwise.

    Off by default (`"wifi_mode": "on"` in bridge.json means Wi-Fi is left
    alone). With `"wifi_mode": "auto"`, tick() is called every few seconds:

    - a cable that is live and has an address, twice in a row: Wi-Fi off.
    - no working cable and Wi-Fi off: Wi-Fi on, because a machine with
      neither is lost to anyone who is not standing next to it. In "on" mode
      this applies only to Wi-Fi this program switched off itself.
    - mode changed back to "on": Wi-Fi on, if this program switched it off.
    """

    def __init__(self, store: Store, system, log, sys_path: Path = netwatch.SYS, device: str = netwatch.RFKILL_DEVICE):
        self.store, self.system, self.log = store, system, log
        self.sys_path, self.device = sys_path, device
        self.ready_for = 0
        self.not_before = 0.0

    @property
    def mode(self) -> str:
        return "auto" if self.store.config.get("wifi_mode") == "auto" else "on"

    def _ip(self) -> str:
        code, out = self.system(["ip", "-4", "-o", "addr", "show", "scope", "global"])
        return out if code == 0 else ""

    def keeping_off(self) -> bool:
        """Is Wi-Fi off because of the cable rule right now?"""
        return self.store.wifi_off_marker.exists() and netwatch.wifi_blocked(self.sys_path) is True

    def status(self) -> dict:
        ip_output = self._ip()
        found = netwatch.addresses(ip_output, self.sys_path)
        blocked = netwatch.wifi_blocked(self.sys_path)
        return {
            "mode": self.mode,
            "can_switch": netwatch.can_switch(self.device),
            "wifi": "none" if blocked is None else ("off" if blocked else "on"),
            "cable": bool(netwatch.cabled(self.sys_path)),
            "cable_ready": netwatch.cable_ready(ip_output, self.sys_path),
            "cable_ip": found.get("wired", ""),
            "wifi_ip": found.get("wifi", ""),
        }

    def set_mode(self, mode: str) -> dict:
        if mode not in ("on", "auto"):
            raise ValueError("unknown Wi-Fi setting")
        if mode == "auto" and not netwatch.can_switch(self.device):
            raise ValueError("the bridge's user may not switch Wi-Fi; add it to the netdev group and restart the bridge")
        if mode == "auto" and netwatch.wifi_blocked(self.sys_path) is None:
            raise ValueError("this machine has no Wi-Fi to switch")
        self.store.config["wifi_mode"] = mode
        self.store.save_config()
        self.ready_for = 0
        self.not_before = time.monotonic() + NET_GRACE
        self.log(f"wifi setting: {'off while a network cable is working' if mode == 'auto' else 'always on'}")
        status = self.status()
        status["grace"] = int(NET_GRACE) if mode == "auto" and status["cable_ready"] and status["wifi"] == "on" else 0
        return status

    def tick(self) -> str:
        """Look at the network and put Wi-Fi where it should be. Returns what was done, or ""."""
        blocked = netwatch.wifi_blocked(self.sys_path)
        if blocked is None:
            return ""
        if self.mode != "auto" and not self.store.wifi_off_marker.exists():
            return ""                           # the usual case: Wi-Fi is none of this program's business
        ready = netwatch.cable_ready(self._ip(), self.sys_path)
        self.ready_for = self.ready_for + 1 if ready else 0
        ours = self.mode == "auto" or self.store.wifi_off_marker.exists()
        if not ready and blocked and ours:
            return self._switch(netwatch.UNBLOCK, "no working network cable: wifi switched back on", marker=False)
        if self.mode != "auto":
            if blocked and self.store.wifi_off_marker.exists():
                return self._switch(netwatch.UNBLOCK, "wifi switched back on", marker=False)
            return ""
        if ready and not blocked and self.ready_for >= 2 and time.monotonic() >= self.not_before:
            return self._switch(netwatch.BLOCK, "network cable is working: wifi switched off", marker=True)
        return ""

    def _switch(self, argv: list[str], what: str, marker: bool) -> str:
        try:
            if marker:
                self.store.wifi_off_marker.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            return ""                       # cannot record it, so do not switch Wi-Fi off
        code, out = self.system(argv)
        if code != 0:
            self.log(f"could not switch wifi ({' '.join(argv)}): {out[:100]}")
            if marker:
                self._forget()
            return ""
        if not marker:
            self._forget()
        self.log(what)
        return what

    def _forget(self) -> None:
        try:
            self.store.wifi_off_marker.unlink()
        except OSError:
            pass


class Bridge:
    def __init__(self, store: Store, radio, clock=time.time, log=None):
        self.store = store
        self.radio = radio
        self.clock = clock
        self.log = log or (lambda message: print(message, flush=True))
        self.started = clock()
        self.status: dict[str, dict] = {}       # id -> ok / err / rssi
        self.results: list[dict] = []
        self.pending: collections.deque = collections.deque()
        self.busy: asyncio.Task | None = None
        self.heartbeat = HEARTBEAT
        self.exit_requested = False
        self.reboot_requested = False
        self.wifi_off = False                   # as last seen by the network watch; sent to the server
        self.waiting: list[dict] = []           # moves to carry out after the restart that has been asked for
        self.checks: dict[str, dict] = {}       # blind id -> a look at its broadcast that is still to come
        self.check_after, self.check_again = CHECK_AFTER, CHECK_AGAIN
        self.resume_delay = RESUME_DELAY
        self.wifi = WifiPolicy(store, lambda argv: self.system(argv), self.log)
        self.uptime = system_uptime             # seconds since the machine started; replaced in tests
        self.notes: list[str] = []              # lines for the server's activity log
        self.system = system_run                # runs one system command; replaced in tests
        self.capture_count = len(store.capture_names())

    # ---- moving ----------------------------------------------------------

    def capturing(self) -> bool:
        """Is capture mode on? It lasts an hour, because it makes every move about 30 seconds slower."""
        config = self.store.config
        if not config.get("capture"):
            return False
        if self.clock() < config.get("capture_until", 0):
            return True
        config["capture"] = False
        config.pop("capture_until", None)
        try:
            self.store.save_config()
        except OSError:
            pass
        self.log("capture mode off: its hour is up")
        return False

    def keep_capture(self, blind: dict, kind: str, pos, code: str, error: str, rssi, capture: dict,
                     note: str = "") -> tuple[str, dict]:
        """Save one recording. Returns (file name, short summary)."""
        events = capture.get("events", [])
        summary = {
            "chars": sum(len(s.get("chars", [])) for s in capture.get("gatt", [])),
            "reads": sum(1 for e in events if e["op"] == "read"),
            "notifies": sum(1 for e in events if e["op"] == "notify"),
            "errors": sum(1 for e in events if e["op"] == "error"),
        }
        now = self.clock()
        record = {
            "v": 1,
            "fw": VERSION,
            "bridge": self.store.config.get("name") or "bridge",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "kind": kind,
            "blind": {"id": blind["id"], "name": blind["name"], "mac": blind["mac"]},
            "pos": pos,
            "last_pos": blind.get("pos", -1),
            "note": note,
            "result": {"code": code, "err": error, "rssi": rssi},
            "summary": summary,
            "decoded": self.decode_capture(events),
            **capture,
        }
        try:
            name = self.store.save_capture(record, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)))
        except OSError as exc:
            self.log(f"could not save capture: {_short(exc)}")
            return "", summary
        self.capture_count = len(self.store.capture_names())
        return name, summary

    @staticmethod
    def decode_capture(events: list[dict]) -> dict:
        """What the last valid readings in a capture say, in plain values."""
        found: dict = {}
        for event in events:
            if event.get("op") not in ("read", "notify") or "<key>" in str(event.get("hex", "")):
                continue
            try:
                data = bytes.fromhex(event.get("hex", ""))
            except ValueError:
                continue
            uuid = str(event.get("uuid", "")).lower()
            if uuid == SENSORS_UUID:
                found.update(parse_sensors(data) or {})
            elif uuid == STATUS_UUID:
                found.update(parse_status(data) or {})
            elif uuid == POS_UUID and len(data) == 1 and data[0] <= 200:
                found["angle"] = data[0]
            elif uuid == ACK_UUID and any(byte != 0xFF for byte in data):
                found["ack"] = data[:6].hex()
        return found

    def may_reboot(self) -> bool:
        """May the machine be restarted because its Bluetooth is stuck?

        Not if that is switched off (`"auto_reboot": false` in bridge.json),
        not in its first five minutes after starting, not within half an hour of the
        last such restart, and not unless the system allows this user to.
        """
        now = self.clock()
        if self.store.config.get("auto_reboot", True) is False:
            return False
        if self.uptime() < REBOOT_NOT_BEFORE:
            return False
        if 0 <= now - self.store.config.get("last_auto_reboot", 0) < REBOOT_GAP:
            return False
        code, out = self.system(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                                 "org.freedesktop.login1.Manager", "CanReboot"])
        return code == 0 and '"yes"' in out

    def prepare(self) -> list[dict]:
        """Returns the moves saved across a restart of the machine."""
        return self.store.take_pending()

    async def resume(self, moves: list[dict]) -> None:
        """Carry out the moves that were waiting when the machine restarted itself. The last one for each blind counts."""
        wanted: dict[str, tuple[dict, int]] = {}
        for move in moves:
            blind = self.store.find(str(move.get("blind", "")))
            pos = move.get("pos")
            age = self.clock() - move.get("at", 0) if isinstance(move.get("at"), (int, float)) else None
            if blind is None or not isinstance(pos, int) or isinstance(pos, bool) or not 0 <= pos <= 200 \
                    or age is None or abs(age) > PENDING_FRESH:
                self.log("a move saved before the restart was too old or unknown, and was dropped")
                continue
            wanted[blind["id"]] = (blind, pos)
        if not wanted:
            return
        await asyncio.sleep(self.resume_delay)
        for blind, pos in wanted.values():
            try:
                ok, error = await self.move_blind(blind, pos, allow_reboot=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ok, error = False, _short(exc)
            self.notes.append(f"after the restart, {blind['name']} " + (f"moved to {pos}" if ok else f"FAILED: {error}"))

    async def move_blind(self, blind: dict, pos: int, allow_reboot: bool = True) -> tuple[bool, str]:
        key = bytes.fromhex(blind["key"])
        self.checks.pop(blind["id"], None)
        ok, error, rssi, attempts = False, "", None, 2
        if self.capturing() and hasattr(self.radio, "probe"):
            # Capture mode: the first try is recorded. If it fails, the plain move still runs.
            code, error, rssi, capture = await self.radio.probe(
                blind["mac"], key, pos=pos, listen=CAPTURE_MOVE_LISTEN)
            self.keep_capture(blind, "move", pos, code, error, rssi, capture)
            ok, attempts = code == "ok", 1
        detail: dict = {}
        if ok:
            detail.update(self.decode_capture(capture["events"]))
        if not ok:
            ok, error, seen = await self.radio.move(blind["mac"], key, pos, attempts=attempts, detail=detail)
            rssi = seen if seen is not None else rssi
        if not ok and detail.get("stuck"):
            # Keep the evidence: the system log does not survive a restart.
            try:
                text = await asyncio.to_thread(self.diagnostics, False)
                fd = os.open(self.store.stuck_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"{blind['name']} -> {pos}: {error} after {detail.get('tries')} tries"
                                 f"; {', '.join(detail.get('notes', []))}\n\n{text}")
            except OSError as exc:
                self.log(f"could not save the stuck-radio snapshot: {_short(exc)}")
        if not ok and detail.get("stuck") and allow_reboot and (self.reboot_requested or self.may_reboot()):
            # Only a restart of the machine clears this. Save the move and restart.
            # A move that fails the same way while that restart is waiting is saved with it.
            try:
                if not self.reboot_requested:
                    self.store.config["last_auto_reboot"] = int(self.clock())
                    self.store.save_config()
                self.waiting.append({"blind": blind["id"], "pos": pos, "at": int(self.clock())})
                self.store.save_pending(self.waiting)
                self.reboot_requested = True
                error = REBOOT_NOTE
            except OSError as exc:
                self.log(f"could not save the move for after a restart: {_short(exc)}")
        self.status[blind["id"]] = {"ok": ok, "err": "" if ok else error, "rssi": rssi}
        health = {name: detail[name] for name in ("battery", "charging") if name in detail}
        if ok:
            angle = detail.get("angle")
            blind["pos"] = angle if isinstance(angle, int) else pos     # where the motor says it is
            blind["moved"] = int(self.clock())
        elif isinstance(detail.get("was"), int):
            blind["pos"] = detail["was"]                                # it did not move; this is where it is
        if health:
            blind.update(health)
            blind["health_at"] = int(self.clock())
        if ok or health or "was" in detail:
            self.store.save_blinds()
        if ok and hasattr(self.radio, "look"):
            # The motor said yes. Whether the blind turned shows in what it broadcasts afterwards.
            self.checks[blind["id"]] = {"blind": blind["id"], "pos": pos, "was": detail.get("was"),
                                        "due": time.monotonic() + self.check_after, "round": 1}
        seen_bits = [f"{name} {detail[name]}" for name in
                     ("was", "ack", "angle", "took", "battery", "charging", "stat", "adv", "connect", "gave_up", "tries")
                     if detail.get(name) not in (None, "")]
        seen_bits += detail.get("notes", [])
        self.log(f"{blind['name']} -> {pos}: {'ok' if ok else 'FAILED ' + error}"
                 + (f" ({', '.join(str(bit) for bit in seen_bits)})" if seen_bits else "")
                 + (f" rssi {rssi}" if rssi is not None else ""))
        return ok, error

    def due_check(self) -> dict | None:
        """A planned look at a blind's broadcast whose time has come, if any."""
        now = time.monotonic()
        for check in self.checks.values():
            if check["due"] <= now:
                return check
        return None

    async def verify(self, check: dict) -> None:
        """Compare what a motor broadcasts with the position it acknowledged.

        A motor can acknowledge a position and not turn. One that does move
        broadcasts its new position within a few seconds. So the broadcast is
        looked at twice, and if it still is not where the motor was sent, the
        blind is marked as failed and the page says so.
        """
        blind = self.store.find(check["blind"])
        if blind is None or self.checks.get(check["blind"]) is not check:
            self.checks = {key: value for key, value in self.checks.items() if value is not check}
            return
        try:
            seen, rssi = await self.radio.look(blind["mac"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a check must never stop the service
            self.log(f"{blind['name']}: could not check the move: {_short(exc)}")
            seen, rssi = None, None
        if self.checks.get(blind["id"]) is not check:
            return                              # a newer move took over while listening
        target, was = check["pos"], check.get("was")
        there = isinstance(seen, int) and abs(seen - target) <= CHECK_WITHIN
        if not there and check["round"] == 1:
            check.update(round=2, due=time.monotonic() + self.check_again)
            return
        del self.checks[blind["id"]]
        if not isinstance(seen, int):
            self.log(f"{blind['name']}: could not check the move to {target}, its broadcast was not heard")
            return
        blind["pos"] = seen
        self.store.save_blinds()
        if there:
            return
        stayed = isinstance(was, int) and abs(seen - was) <= CHECK_WITHIN
        self.status[blind["id"]] = {"ok": False, "err": NOT_MOVED if stayed else STOPPED_SHORT, "rssi": rssi}
        line = (f"{blind['name']} did not move: sent to {target}, still at {seen}" if stayed
                else f"{blind['name']} stopped at {seen}, sent to {target}")
        self.notes.append(line)
        self.log(line)

    def diagnostics(self, with_snapshot: bool = True) -> str:
        """The state of this machine's Bluetooth and network, as text, for the debug zip.

        Read-only commands. No keys, tokens or passwords are in any of them.
        The last part is the same report as it was the last time the radio
        was stuck, saved then because the system log is lost at a restart.
        """
        parts = [f"BlindBridge bridge {VERSION}, made {time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(self.clock()))}",
                 f"bridge running for {int(self.clock() - self.started)} s"]

        def section(title: str, argv: list[str], keep=None, last: int = 0) -> None:
            code, out = self.system(argv)
            lines = out.splitlines()
            if keep is not None:
                lines = [line for line in lines if keep(line)]
            if last:
                lines = lines[-last:]
            parts.append(f"\n== {title} (exit {code})\n" + ("\n".join(lines) or "(nothing)"))

        section("uptime", ["uptime"])
        section("kernel", ["uname", "-srm"])
        section("clock", ["timedatectl"])
        section("services", ["systemctl", "is-active", "bluetooth", "hciuart", "blindbridge-bridge", "blindbridge-server"])
        section("controller", ["bluetoothctl", "show"])
        section("adapter", ["hciconfig", "-a"])
        section("connections", ["hcitool", "con"])
        section("devices the system has on file", ["bluetoothctl", "devices"])
        for blind in self.store.blinds:
            section(f"what the system knows about {blind['name']}", ["bluetoothctl", "info", blind["mac"]])
        parts.append(f"\n== wifi setting\n{json.dumps(self.wifi.status())}")
        section("radio switches", ["rfkill", "list"])
        section("network", ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"])
        section("bluetooth service log", ["journalctl", "-u", "bluetooth", "-n", "150", "--no-pager", "-o", "short-iso"])
        section("kernel bluetooth lines", ["journalctl", "-k", "-n", "6000", "--no-pager", "-o", "short-iso"],
                keep=lambda line: re.search(r"bluetooth|hci\d|brcm|uart", line, re.I) is not None, last=150)
        text = "\n".join(parts)[-150000:]
        if with_snapshot:
            try:
                saved = self.store.stuck_path.read_text(encoding="utf-8")
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.store.stuck_path.stat().st_mtime))
                text += f"\n\n######## SNAPSHOT FROM THE LAST TIME BLUETOOTH WAS STUCK (file written {when}) ########\n{saved}"
            except OSError:
                text += "\n\n(no snapshot: Bluetooth has not been stuck)"
        return text

    # ---- management, asked for by the server's Settings page -------------

    async def admin(self, op: str, args: dict) -> dict:
        """Carry out one management operation. ValueError carries a message for the user."""
        if op == "scan":
            seconds = min(max(float(args.get("seconds", 8)), 2.0), 20.0)
            hits = await self.radio.scan(seconds, everything=bool(args.get("all")))
            known = {b["mac"]: b for b in self.store.blinds}
            changed = False
            for hit in hits:
                blind = known.get(hit["mac"])
                if blind is None:
                    continue
                self.status.setdefault(blind["id"], {"ok": True, "err": ""})["rssi"] = hit["rssi"]
                if isinstance(hit.get("pos"), int) and blind.get("pos") != hit["pos"]:
                    blind["pos"] = hit["pos"]       # what the motor itself broadcasts
                    changed = True
            if changed:
                self.store.save_blinds()
            return {"found": [dict(hit, known=known[hit["mac"]]["name"] if hit["mac"] in known else "")
                              for hit in hits[:60]]}
        if op == "import":
            blinds = blinds_from_backup(args.get("backup"))
            if not blinds:
                raise ValueError("no blinds found in that file")
            added, updated = [], []
            for item in blinds:
                blind, created = self.store.upsert(item["name"], item["mac"], item["key"])
                (added if created else updated).append(blind["name"])
            self.store.save_blinds()
            self.log(f"imported blinds: {len(added)} added, {len(updated)} updated")
            return {"added": added, "updated": updated}
        if op == "add":
            blind, created = self.store.upsert(str(args.get("name", "")), str(args.get("mac", "")), str(args.get("key", "")))
            self.store.save_blinds()
            self.log(f"{'added' if created else 'updated'} blind {blind['name']}")
            return {"name": blind["name"], "created": created}
        if op in ("rename", "remove"):
            blind = self.store.find(str(args.get("blind", "")))
            if blind is None:
                raise ValueError("this bridge does not know that blind")
            if op == "remove":
                self.store.blinds.remove(blind)
                self.status.pop(blind["id"], None)
                self.store.save_blinds()
                self.log(f"removed blind {blind['name']}")
                return {"name": blind["name"]}
            name = self.store.rename(blind, str(args.get("name", "")))
            self.store.save_blinds()
            return {"name": name}
        if op == "capture":
            blind = self.store.find(str(args.get("blind", "")))
            if blind is None:
                raise ValueError("this bridge does not know that blind")
            seconds = min(max(float(args.get("seconds", 8)), 3.0), 20.0)
            code, error, rssi, capture = await self.radio.probe(
                blind["mac"], bytes.fromhex(blind["key"]), listen=seconds)
            name, summary = self.keep_capture(
                blind, "probe", None, code, error, rssi, capture, str(args.get("note", "")).strip()[:160])
            self.status[blind["id"]] = {"ok": code == "ok", "err": "" if code == "ok" else error, "rssi": rssi}
            self.log(f"capture {blind['name']}: {code}{' ' + error if error else ''}, "
                     f"{summary['reads']} reads, {summary['notifies']} notifications, {summary['errors']} errors")
            return {"file": name, "code": code, "err": error, "rssi": rssi, **summary}
        if op == "capture_mode":
            on = bool(args.get("on"))
            self.store.config["capture"] = on
            if on:
                self.store.config["capture_until"] = int(self.clock() + CAPTURE_FOR)
            else:
                self.store.config.pop("capture_until", None)
            self.store.save_config()
            self.log(f"capture mode {'on for an hour: moves are recorded' if on else 'off'}")
            return {"on": on, "minutes": int(CAPTURE_FOR // 60) if on else 0}
        if op == "captures":
            return await asyncio.to_thread(self.store.bundle_captures)
        if op == "capture_clear":
            removed = self.store.clear_captures()
            self.capture_count = len(self.store.capture_names())
            self.log(f"captures cleared: {removed} removed")
            return {"removed": removed}
        if op == "diagnostics":
            return {"text": await asyncio.to_thread(self.diagnostics)}
        if op == "network":
            return await asyncio.to_thread(self.wifi.status)
        if op == "wifi_mode":
            return await asyncio.to_thread(self.wifi.set_mode, str(args.get("mode", "")))
        if op == "logs":
            lines = min(max(int(args.get("lines", 150)), 10), 500)
            return {"text": await asyncio.to_thread(read_journal, "blindbridge-bridge", lines)}
        if op == "restart":
            self.exit_requested = True
            self.log("restart requested from the Settings page")
            return {}
        if op == "update":
            import updater  # lives in the install folder, next to bridge/

            if not self.update_allowed():
                raise ValueError("this bridge takes updates only from a server on its own machine or over https; "
                                 "update it by hand, or set updates_over_http in its bridge.json")
            try:
                data = base64.b64decode(str(args.get("zip_b64", "")), validate=True)
                result = updater.apply_update(data, INSTALL_DIR)
            except (updater.UpdateError, ValueError) as exc:
                raise ValueError(str(exc)) from None
            self.exit_requested = True
            self.log(f"update {result['from']} -> {result['to']}: {len(result['changed'])} files changed, restarting")
            return result
        raise ValueError(f"this bridge does not understand '{op}'; it may need updating")

    def update_allowed(self) -> bool:
        """May the server hand this bridge new program files?

        An update is code that this bridge will run. Over plain http anyone on
        the path between the two machines could put their own in, so it is
        accepted only from a server on this machine or reached over https,
        unless `"updates_over_http": true` in bridge.json says otherwise.
        """
        if self.store.config.get("updates_over_http") is True:
            return True
        url = urllib.parse.urlsplit(str(self.store.config.get("server", "")))
        return url.scheme == "https" or (url.hostname or "") in ("localhost", "127.0.0.1", "::1")

    async def execute(self, command: dict) -> None:
        """Run one command from the server and queue its result."""
        op = command.get("op", "move")
        result = {"cmd": command.get("cmd", ""), "ok": False, "err": ""}
        try:
            if op == "move":
                blind = self.store.find(str(command.get("blind", "")))
                pos = command.get("pos")
                if blind is None:
                    result["err"] = "this bridge does not know that blind"
                elif isinstance(pos, bool) or not isinstance(pos, int) or not POS_MIN <= pos <= POS_MAX:
                    result["err"] = "bad position"
                elif self.reboot_requested:
                    # The radio is known to be stuck and a restart is on its way: do not spend
                    # a minute and a half finding that out again. Save the move for afterwards.
                    self.waiting.append({"blind": blind["id"], "pos": pos, "at": int(self.clock())})
                    self.store.save_pending(self.waiting)
                    self.status[blind["id"]] = {"ok": False, "err": REBOOT_NOTE, "rssi": None}
                    result["err"] = REBOOT_NOTE
                else:
                    ok, error = await self.move_blind(blind, pos)
                    result.update(ok=ok, err="" if ok else error)
            else:
                args = command.get("args")
                result["data"] = await self.admin(str(op), args if isinstance(args, dict) else {})
                result["ok"] = True
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            result["err"] = str(exc)[:200]
        except Exception as exc:  # never let one bad command stop the service
            result["err"] = _short(exc)
        self.results.append(result)

    # ---- talking to the server -------------------------------------------

    def report(self, wait: int) -> dict:
        now = self.clock()
        blinds = []
        for blind in self.store.blinds:
            status = self.status.get(blind["id"], {})
            moved = blind.get("moved") or 0
            blinds.append(
                {
                    "id": blind["id"],
                    "name": blind["name"],
                    "pos": blind.get("pos", -1),
                    "ok": status.get("ok", True),
                    "err": status.get("err", ""),
                    "rssi": status.get("rssi"),
                    "age": int(now - moved) if moved else -1,
                    "battery": blind.get("battery"),
                    "charging": blind.get("charging", ""),
                    "health_age": int(now - blind["health_at"]) if blind.get("health_at") else -1,
                }
            )
        return {
            "fw": VERSION,
            "id": self.store.config.get("name") or "bridge",
            "uptime": int(now - self.started),
            "wait": wait,
            "capture": self.capturing(),
            "captures": self.capture_count,
            "wifi_off": self.wifi_off,
            "blinds": blinds,
            "results": list(self.results),
            "notes": list(self.notes),
        }

    def _post(self, body: dict) -> dict:
        url = self.store.config["server"].rstrip("/") + "/bridge/v1/sync"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.store.config["token"],
            },
        )
        with urllib.request.urlopen(request, timeout=body["wait"] + 20) as response:
            return json.loads(response.read())

    async def step(self, wait: int | None = None) -> int:
        """One sync with the server, then make progress on at most one command.

        A command runs as a background task. While it runs, each step checks in
        with the server (so the bridge never looks offline during a slow move)
        and then waits up to `heartbeat` seconds for the command to finish.
        Returns how many commands finished during this step.
        """
        if wait is None:
            # Do not sit in a long poll when there is work in hand or a restart is due.
            wait = 0 if (self.pending or self.busy or self.exit_requested or self.reboot_requested) else LONG_POLL
            if wait and self.checks:
                soonest = min(check["due"] for check in self.checks.values()) - time.monotonic()
                wait = max(0, min(wait, int(soonest) + 1))
        sent, told = list(self.results), len(self.notes)
        body = self.report(wait)
        reply = await asyncio.to_thread(self._post, body)
        # Results queued while the request was in flight stay for the next sync.
        self.results = [r for r in self.results if not any(r is s for s in sent)]
        del self.notes[:told]
        if self.reboot_requested and not self.results and self.busy is None and not self.pending:
            self.reboot_requested = False
            self.log("restarting the Pi: its Bluetooth hears the motors but cannot connect")
            code, out = self.system(["/usr/bin/systemctl", "reboot"])
            self.waiting = []
            if code != 0:
                self.log(f"the restart was refused: {out[:120]}")
                self.store.take_pending()
        if self.exit_requested and not self.results and self.busy is None:
            raise SystemExit(0)  # systemd starts the service again
        for command in reply.get("commands", []):
            if command.get("op", "move") == "move":
                # A newer move for the same blind replaces one not yet carried out.
                for old in [c for c in self.pending
                            if c.get("op", "move") == "move" and c.get("blind") == command.get("blind")]:
                    self.pending.remove(old)
                    self.results.append({"cmd": old.get("cmd", ""), "ok": False, "err": "replaced by a newer command"})
            self.pending.append(command)
        if self.busy is None and self.pending:
            self.busy = asyncio.create_task(self.execute(self.pending.popleft()))
        elif self.busy is None and not self.reboot_requested and self.due_check() is not None:
            self.busy = asyncio.create_task(self.verify(self.due_check()))
        if self.busy is None:
            return 0
        finished, _ = await asyncio.wait({self.busy}, timeout=self.heartbeat)
        if not finished:
            return 0
        task, self.busy = self.busy, None
        task.result()
        return 1

    async def watch_network(self) -> None:
        """Every few seconds: keep Wi-Fi off while a cable is working, and on when it is not."""
        while True:
            try:
                await asyncio.to_thread(self.wifi.tick)
                self.wifi_off = await asyncio.to_thread(self.wifi.keeping_off)
            except asyncio.CancelledError:
                raise
            except Exception as exc:        # never let the watch die: it is what brings Wi-Fi back
                self.log(f"network watch: {_short(exc)}")
            await asyncio.sleep(NET_EVERY)

    async def run(self) -> None:
        self.log(f"BlindBridge bridge {VERSION} '{self.store.config.get('name')}' -> {self.store.config['server']}")
        saved = self.prepare()
        if saved:
            self.busy = asyncio.create_task(self.resume(saved))
        self.network_watch = asyncio.create_task(self.watch_network())
        delay = 2
        while True:
            try:
                await self.step()
                delay = 2
            except asyncio.CancelledError:
                raise
            except urllib.error.HTTPError as exc:
                detail = "check the token" if exc.code == 401 else exc.reason
                self.log(f"server answered {exc.code}: {detail}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
            except (OSError, ValueError) as exc:
                self.log(f"cannot reach server: {_short(exc)}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


def system_uptime(path: str = "/proc/uptime") -> float:
    """Seconds since this machine started. A machine that cannot say counts as long up."""
    try:
        with open(path, encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return float("inf")


def system_run(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run one system command. Returns (exit code, what it printed). -1 if it could not run."""
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, _short(exc)
    return done.returncode, _plain((done.stdout or b"") + (done.stderr or b""))


def read_journal(unit: str, lines: int) -> str:
    """The last lines of a service's log, or a short explanation if unavailable."""
    try:
        done = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(log not available: {_short(exc)})"
    text = done.stdout or done.stderr or "(no log lines)"
    return text[-60000:]


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _need(store: Store, who: str) -> dict:
    blind = store.find(who)
    if blind is None:
        known = ", ".join(b["name"] for b in store.blinds) or "none yet"
        raise SystemExit(f"No blind called {who!r}. Known: {known}")
    return blind


async def cmd_scan(args, store: Store) -> None:
    print(f"Scanning for {args.seconds} seconds...")
    hits = await BleakRadio().scan(args.seconds, everything=args.all)
    known = {b["mac"]: b["name"] for b in store.blinds}
    if not hits:
        print("Nothing found. Motors only show up when in range and not connected to the phone app.")
    for hit in hits:
        label = known.get(hit["mac"], "")
        print(f"{hit['mac']}  {hit['rssi'] or '?':>4} dBm  {hit['name'] or '(no name)'}  {label}")


def cmd_import(args, store: Store) -> None:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    blinds = blinds_from_backup(data)
    if not blinds:
        raise SystemExit("No blinds found in that file. Is it the app's backup export?")
    for item in blinds:
        blind, created = store.upsert(item["name"], item["mac"], item["key"])
        print(f"{'added  ' if created else 'updated'}  {blind['name']}  {blind['mac']}")
    store.save_blinds()
    print(f"{len(blinds)} blinds saved. Keys are in {store.blinds_path} (owner-only).")


def cmd_add(args, store: Store) -> None:
    blind, created = store.upsert(args.name, args.mac, args.key)
    store.save_blinds()
    print(f"{'Added' if created else 'Updated'} {blind['name']} {blind['mac']}")


def cmd_list(args, store: Store) -> None:
    if not store.blinds:
        print("No blinds yet. Use: import or add.")
    for blind in store.blinds:
        pos = blind.get("pos", -1)
        print(f"{blind['name']:<28} {blind['mac']}  last position: {'unknown' if pos < 0 else pos}")


def cmd_rename(args, store: Store) -> None:
    blind = _need(store, args.blind)
    store.rename(blind, args.name)
    store.save_blinds()
    print(f"Renamed to {blind['name']}")


def cmd_remove(args, store: Store) -> None:
    blind = _need(store, args.blind)
    store.blinds.remove(blind)
    store.save_blinds()
    print(f"Removed {blind['name']}")


async def cmd_move(args, store: Store) -> None:
    pos = parse_position(args.position)
    targets = store.blinds if args.blind.lower() == "all" else [_need(store, args.blind)]
    bridge = Bridge(store, BleakRadio())
    failed = 0
    for blind in targets:
        ok, _ = await bridge.move_blind(blind, pos)
        failed += 0 if ok else 1
    if failed:
        raise SystemExit(1)


def cmd_setup(args, store: Store) -> None:
    store.config.update(server=args.server.rstrip("/"), token=args.token, name=args.name)
    store.save_config()
    print(f"Saved {store.config_path}")


async def cmd_run(args, store: Store) -> None:
    for field in ("server", "token"):
        if not store.config.get(field):
            raise SystemExit("Run setup first: bridge.py setup --server URL --token TOKEN --name NAME")
    await Bridge(store, BleakRadio()).run()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="BlindBridge bridge")
    parser.add_argument("--dir", default=str(HERE), help="folder holding blinds.json and bridge.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="look for motors nearby")
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--all", action="store_true", help="show every Bluetooth device, not only motors")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("import", help="load blinds from the phone app's backup file")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("add", help="add one blind by hand")
    p.add_argument("name")
    p.add_argument("mac")
    p.add_argument("key", help="hex")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="show known blinds")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("rename")
    p.add_argument("blind")
    p.add_argument("name")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("remove")
    p.add_argument("blind")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("move", help="move a blind right now, no server involved")
    p.add_argument("blind", help="name, or 'all'")
    p.add_argument("position", help="open, close, up, half, or 0-200")
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("setup", help="save the server address and token")
    p.add_argument("--server", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--name", default="bridge", help="a short name for this bridge, e.g. livingroom")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("run", help="run the bridge service")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    store = Store(Path(args.dir))
    try:
        result = args.func(args, store)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
