"""Tests for the Pi bridge. Run: python3 -m unittest -v test_bridge"""

from __future__ import annotations

import asyncio
import base64
import json
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "server"))

import bridge  # noqa: E402
import server  # noqa: E402


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


BACKUP = {
    "user": {
        "name": "Somebody",
        "rooms": [
            {
                "id": "r1", "name": "Study",
                "blinds": [
                    {"id": "1" * 36, "name": "Left", "encodedMacAddress": b64(bytes.fromhex("665544332211")),
                     "encodedPasskey": b64(bytes.fromhex("0a1b2c3d4e5f"))},
                    {"id": "2" * 36, "name": "Study Right", "encodedMacAddress": b64(bytes.fromhex("0c0b0a090807")),
                     "encodedPasskey": b64(bytes.fromhex("010203040506"))},
                    {"id": "3" * 36, "name": "Gone", "deleted": True,
                     "encodedMacAddress": b64(bytes.fromhex("ffffffffffff")), "encodedPasskey": b64(b"\x01")},
                ],
            },
            {"id": "r2", "name": "Old room", "deleted": True, "blinds": [
                {"name": "X", "encodedMacAddress": b64(bytes.fromhex("eeeeeeeeeeee")), "encodedPasskey": b64(b"\x02")}]},
        ],
    }
}


class FakeRadio:
    """Stands in for Bluetooth. Knows which key each motor accepts."""

    def __init__(self, motors: dict[str, bytes]):
        self.motors = {mac.upper(): key for mac, key in motors.items()}
        self.positions: dict[str, int] = {}
        self.unreachable: set[str] = set()
        self.sessions = 0
        self.delay = 0.0
        self.nearby = [{"mac": "AA:00:00:00:00:01", "name": "SmartBlind_DFU", "rssi": -60},
                       {"mac": "AA:00:00:00:00:09", "name": "SmartBlind_DFU", "rssi": -80},
                       {"mac": "11:22:33:44:55:66", "name": "Some speaker", "rssi": -50}]

    async def scan(self, seconds=10.0, everything=False):
        return [d for d in self.nearby if everything or d["name"].startswith("SmartBlind")]

    async def session(self, mac, key, pos, detail=None, purge=False):
        self.sessions += 1
        mac = mac.upper()
        if mac not in self.motors or mac in self.unreachable:
            return "unreachable", "not found", None
        good = self.motors[mac]
        if key != good:
            return "rejected", "motor hung up after the key", -70
        self.positions[mac] = pos
        return "ok", "", -70

    async def move(self, mac, key, pos, attempts=2, detail=None):
        self.move_attempts = attempts
        if self.delay:
            await asyncio.sleep(self.delay)
        code, error, rssi = await self.session(mac, key, pos)
        if code == "ok" and detail is not None:
            detail.update(getattr(self, "says", {}))
        return code == "ok", error, rssi

    async def probe(self, mac, key, pos=None, listen=8.0):
        self.probes = getattr(self, "probes", 0) + 1
        capture = {"adv": {"name": "SmartBlind_DFU", "rssi": -70}, "gatt": [], "events": []}
        if mac.upper() not in self.motors or mac.upper() in self.unreachable:
            capture["events"].append({"t": 0.1, "op": "error", "what": "find", "err": "not found"})
            return "unreachable", "not found", None, capture
        capture["gatt"] = [{"service": "1400", "handle": 1, "chars": [
            {"uuid": "1402", "handle": 3, "props": ["notify", "read"], "descriptors": []},
            {"uuid": "1651", "handle": 6, "props": ["read"], "descriptors": []}]}]
        capture["events"] = [{"t": 0.2, "op": "read", "when": "start", "uuid": "1651", "hex": "5f0a"},
                             {"t": 0.9, "op": "notify", "uuid": "1402", "hex": "01"}]
        if pos is not None:
            self.positions[mac.upper()] = pos
        return "ok", "", -70, capture


class ParsingTests(unittest.TestCase):
    def test_parse_hex_spellings(self):
        for text in ("0a1b2c", "0A:1B:2C", "0x0A, 0x1B, 0x2C", "[0a 1b 2c]"):
            self.assertEqual(bridge.parse_hex(text), bytes.fromhex("0a1b2c"), text)
        for bad in ("", "abc", "zz", "0x"):
            with self.assertRaises(ValueError):
                bridge.parse_hex(bad)

    def test_normalize_mac(self):
        self.assertEqual(bridge.normalize_mac("aabbccddeeff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(bridge.normalize_mac("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")
        with self.assertRaises(ValueError):
            bridge.normalize_mac("aabbcc")

    def test_parse_position(self):
        self.assertEqual(bridge.parse_position("open"), 100)
        self.assertEqual(bridge.parse_position("Close"), 200)
        self.assertEqual(bridge.parse_position("35"), 35)
        for bad in ("201", "-1", "wide"):
            with self.assertRaises(ValueError):
                bridge.parse_position(bad)

    def test_backup_import_reverses_mac_and_skips_deleted(self):
        blinds = bridge.blinds_from_backup(BACKUP)
        self.assertEqual(
            blinds,
            [
                {"name": "Study Left", "mac": "11:22:33:44:55:66", "key": "0a1b2c3d4e5f"},
                {"name": "Study Right", "mac": "07:08:09:0A:0B:0C", "key": "010203040506"},
            ],
        )

    def test_backup_import_flat_layout_with_room_ids(self):
        flat = {"user": {"name": "Somebody", "rooms": [{"id": 7, "name": "Den"}],
                         "blinds": [{"name": "Window", "roomId": 7,
                                     "encodedMacAddress": b64(bytes.fromhex("060504030201")),
                                     "encodedPasskey": b64(bytes.fromhex("aabbccddeeff"))}]}}
        self.assertEqual(bridge.blinds_from_backup(flat),
                         [{"name": "Den Window", "mac": "01:02:03:04:05:06", "key": "aabbccddeeff"}])

    def test_backup_import_ignores_junk(self):
        self.assertEqual(bridge.blinds_from_backup({"x": [1, "a", None, {"encodedMacAddress": "!!", "encodedPasskey": "!!"}]}), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = bridge.Store(Path(self.tmp.name))

    def test_upsert_find_and_private_file(self):
        blind, created = self.store.upsert("Kitchen", "aa:bb:cc:dd:ee:ff", "0A1B")
        self.assertTrue(created)
        self.assertEqual((blind["id"], blind["mac"], blind["key"]), ("aabbccddeeff", "AA:BB:CC:DD:EE:FF", "0a1b"))
        _, created = self.store.upsert("Kitchen window", "AABBCCDDEEFF", "ff")
        self.assertFalse(created)
        self.assertEqual(len(self.store.blinds), 1)
        self.store.save_blinds()
        mode = stat.S_IMODE(self.store.blinds_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        again = bridge.Store(Path(self.tmp.name))
        for who in ("kitchen WINDOW", "aabbccddeeff", "AA:BB:CC:DD:EE:FF"):
            self.assertEqual(again.find(who)["key"], "ff", who)
        self.assertIsNone(again.find("nope"))

    def test_duplicate_names_get_a_number(self):
        self.store.upsert("Window", "000000000001", "01")
        blind, _ = self.store.upsert("Window", "000000000002", "02")
        self.assertEqual(blind["name"], "Window 2")

    def test_cli_import_list_setup(self):
        backup = Path(self.tmp.name) / "backup.json"
        backup.write_text(json.dumps(BACKUP))
        bridge.main(["--dir", self.tmp.name, "import", str(backup)])
        bridge.main(["--dir", self.tmp.name, "setup", "--server", "http://x/", "--token", "t", "--name", "den"])
        bridge.main(["--dir", self.tmp.name, "rename", "Study Left", "Desk"])
        bridge.main(["--dir", self.tmp.name, "remove", "Study Right"])
        store = bridge.Store(Path(self.tmp.name))
        self.assertEqual([b["name"] for b in store.blinds], ["Desk"])
        self.assertEqual(store.config, {"server": "http://x", "token": "t", "name": "den"})
        self.assertEqual(stat.S_IMODE(store.config_path.stat().st_mode), 0o600)
        with self.assertRaises(SystemExit):
            bridge.main(["--dir", self.tmp.name, "remove", "Nope"])
        with self.assertRaises(SystemExit):
            bridge.main(["--dir", self.tmp.name, "add", "Bad", "12345", "01"])


class ServiceTests(unittest.TestCase):
    """The bridge loop against the real server over HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = dict(server.DEFAULT_CONFIG)
        cfg.update(port=0, admin_password_hash=server.hash_password("x" * 10, rounds=1000),
                   bridge_token="bt", api_token="at")
        srv_dir = Path(self.tmp.name) / "srv"
        srv_dir.mkdir()
        self.httpd, self.hub = server.make_server(cfg, srv_dir)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.hub.db.close)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

        pi_dir = Path(self.tmp.name) / "pi"
        pi_dir.mkdir()
        self.store = bridge.Store(pi_dir)
        self.store.upsert("Kitchen", "AA:00:00:00:00:01", "0a1b2c3d4e5f")
        self.store.upsert("Study", "AA:00:00:00:00:02", "111111111111")
        self.store.config = {"server": f"http://127.0.0.1:{self.httpd.server_address[1]}", "token": "bt", "name": "den"}
        self.radio = FakeRadio({"AA:00:00:00:00:01": bytes.fromhex("0a1b2c3d4e5f"),
                                "AA:00:00:00:00:02": bytes.fromhex("111111111111")})
        self.logs = []
        self.bridge = bridge.Bridge(self.store, self.radio, log=self.logs.append)

    def step(self, wait=0):
        return asyncio.run(self.bridge.step(wait))

    def test_registers_blinds_without_leaking_keys(self):
        self.assertEqual(self.step(), 0)
        state = self.hub.state()
        self.assertEqual([(b["name"], b["bridge"], b["pos"]) for b in state["blinds"]],
                         [("Kitchen", "den", None), ("Study", "den", None)])
        self.assertNotIn("0a1b2c", json.dumps(self.bridge.report(0)))
        self.assertNotIn("0a1b2c", json.dumps(self.hub.blinds))

    def test_all_blinds_move_one_per_step_and_report(self):
        self.step()
        self.hub.enqueue(["all"], 200, "manual")
        self.assertEqual(self.step(), 1)          # picks up both, moves the first
        self.assertEqual(len(self.radio.positions), 1)
        self.assertEqual(self.step(None), 1)      # reports the first, moves the second
        self.assertEqual(self.radio.positions, {"AA:00:00:00:00:01": 200, "AA:00:00:00:00:02": 200})
        self.assertEqual(self.step(), 0)          # reports the second
        state = self.hub.state()
        self.assertEqual([(b["pos"], b["busy"], b["ok"]) for b in state["blinds"]], [(200, False, True)] * 2)
        self.assertEqual(self.bridge.results, [])
        saved = bridge.Store(self.store.folder)
        self.assertEqual([b["pos"] for b in saved.blinds], [200, 200])

    def test_failure_is_reported(self):
        self.step()
        self.radio.unreachable.add("AA:00:00:00:00:02")
        self.hub.enqueue([self.store.find("Study")["id"]], 100, "manual")
        self.step()
        self.step()
        study = [b for b in self.hub.state()["blinds"] if b["name"] == "Study"][0]
        self.assertEqual((study["ok"], study["err"], study["busy"]), (False, "not found", False))
        self.assertTrue(any("Study FAILED: not found" in e["message"] for e in self.hub.state()["log"]))

    def test_results_survive_a_server_outage(self):
        self.step()
        self.hub.enqueue(["all"], 100, "manual")
        self.step()
        good = self.store.config["server"]
        self.store.config["server"] = "http://127.0.0.1:9"   # nothing listens here
        with self.assertRaises(OSError):
            self.step()
        self.assertEqual(len(self.bridge.results), 1)
        self.store.config["server"] = good
        self.step(None)
        self.step()
        self.assertFalse(any(b["busy"] for b in self.hub.state()["blinds"]))

    def test_unknown_blind_and_bad_position_do_not_crash(self):
        self.step()
        self.bridge.pending.append({"cmd": "c1", "blind": "ffffffffffff", "pos": 100})
        self.bridge.pending.append({"cmd": "c2", "blind": self.store.blinds[0]["id"], "pos": 999})
        self.step()
        self.step()
        self.assertEqual(self.radio.positions, {})
        # c1's result went out with the second sync; c2's is waiting to go.
        self.assertEqual(self.bridge.results, [{"cmd": "c2", "ok": False, "err": "bad position"}])

    def test_wrong_token_raises_http_error(self):
        self.store.config["token"] = "nope"
        with self.assertRaises(bridge.urllib.error.HTTPError) as ctx:
            self.step()
        self.assertEqual(ctx.exception.code, 401)


class Pump:
    """Runs the bridge loop in a background thread, as the service would."""

    def __init__(self, bridge_obj):
        self.bridge = bridge_obj
        self.stop = threading.Event()
        self.exited = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        async def loop():
            while not self.stop.is_set():
                try:
                    await self.bridge.step(0)
                except SystemExit:
                    self.exited.set()
                    return
                except OSError:
                    pass
                await asyncio.sleep(0.02)
        asyncio.run(loop())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)


def release_zip(version=None) -> bytes:
    """The real release zip, optionally with a different VERSION."""
    sys.path.insert(0, str(HERE.parent / "tools"))
    import io, zipfile
    import make_release
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in make_release.release_files():
            rel = item.relative_to(make_release.ROOT).as_posix()
            data = (version + "\n").encode() if (rel == "VERSION" and version) else item.read_bytes()
            archive.writestr("blindbridge/" + rel, data)
    return buffer.getvalue()


class SettingsTests(ServiceTests):
    """Settings actions travel server -> bridge over the same sync channel."""

    def setUp(self):
        super().setUp()
        self.bridge.heartbeat = 0.05
        self.step()   # register with the server

    def test_scan_marks_known_motors(self):
        with Pump(self.bridge):
            found = self.hub.admin("den", "scan")["found"]
        self.assertEqual([(f["mac"], f["known"]) for f in found],
                         [("AA:00:00:00:00:01", "Kitchen"), ("AA:00:00:00:00:09", "")])

    def test_import_rename_remove(self):
        with Pump(self.bridge):
            result = self.hub.admin("den", "import", {"backup": BACKUP})
            self.assertEqual(result, {"added": ["Study Left", "Study Right"], "updated": []})
            self.hub.admin("den", "rename", {"blind": "Study Left", "name": "Desk"})
            with self.assertRaises(server.ValidationError) as ctx:
                self.hub.admin("den", "rename", {"blind": "Desk", "name": "kitchen"})
            self.assertIn("already has that name", str(ctx.exception))
            self.hub.admin("den", "remove", {"blind": "Study Right"})
            with self.assertRaises(server.ValidationError):
                self.hub.admin("den", "remove", {"blind": "Nope"})
            with self.assertRaises(server.ValidationError):
                self.hub.admin("den", "import", {"backup": {"nothing": 1}})
            with self.assertRaises(server.ValidationError):
                self.hub.admin("den", "add", {"name": "Bad", "mac": "123", "key": "01"})
            with self.assertRaises(server.ValidationError):
                self.hub.admin("den", "selfdestruct")
            import time as _t; _t.sleep(0.2)
        names = sorted(b["name"] for b in self.hub.state()["blinds"])
        self.assertEqual(names, ["Desk", "Kitchen", "Study"])
        self.assertNotIn("0a1b2c3d4e5f", json.dumps(self.hub.blinds))
        self.assertEqual(sorted(b["name"] for b in bridge.Store(self.store.folder).blinds), names)

    def test_admin_needs_a_known_online_bridge(self):
        with self.assertRaises(server.ValidationError) as ctx:
            self.hub.admin("nope", "scan")
        self.assertIn("unknown bridge", str(ctx.exception))
        self.hub.bridges["den"]["last_seen"] -= server.ONLINE_WINDOW + 5
        with self.assertRaises(server.ValidationError) as ctx:
            self.hub.admin("den", "scan")
        self.assertIn("offline", str(ctx.exception))

    def test_admin_times_out_cleanly_when_bridge_is_silent(self):
        with self.assertRaises(server.ValidationError) as ctx:
            self.hub.admin("den", "scan", timeout=0.3)
        self.assertIn("did not answer", str(ctx.exception))
        self.assertEqual((self.hub.queue, self.hub.inflight), ([], {}))

    def test_restart_exits_after_the_answer_is_delivered(self):
        with Pump(self.bridge) as pump:
            self.assertEqual(self.hub.admin("den", "restart"), {})
            self.assertTrue(pump.exited.wait(3))

    def test_restart_does_not_wait_out_a_long_poll(self):
        waits = []
        real_post = self.bridge._post
        self.bridge._post = lambda body: (waits.append(body["wait"]), real_post(body))[1]
        self.bridge.pending.append({"cmd": "r1", "op": "restart", "args": {}})
        self.step()                      # runs the restart command
        with self.assertRaises(SystemExit):
            asyncio.run(self.bridge.step())   # default wait: must be 0 now, then exit
        self.assertEqual(waits[-1], 0)

    def test_slow_move_keeps_checking_in(self):
        self.radio.delay = 0.6
        self.hub.enqueue([self.store.find("Kitchen")["id"]], 200, "manual")
        with Pump(self.bridge):
            seen = set()
            deadline = __import__("time").time() + 3
            while __import__("time").time() < deadline:
                seen.add(self.hub.bridges["den"]["generation"])
                if self.radio.positions:
                    break
                __import__("time").sleep(0.05)
            __import__("time").sleep(0.3)
        self.assertGreaterEqual(len(seen), 3)          # several check-ins during one move
        kitchen = [b for b in self.hub.state()["blinds"] if b["name"] == "Kitchen"][0]
        self.assertEqual((kitchen["pos"], kitchen["busy"]), (200, False))

    def test_update_installs_into_the_bridge_folder_and_restarts(self):
        target = Path(self.tmp.name) / "install"
        (target / "bridge").mkdir(parents=True)
        (target / "bridge" / "blinds.json").write_text("KEEP")
        old = bridge.INSTALL_DIR
        bridge.INSTALL_DIR = target
        self.addCleanup(setattr, bridge, "INSTALL_DIR", old)
        payload = bridge.base64.b64encode(release_zip("9.9.9")).decode()
        with Pump(self.bridge) as pump:
            result = self.hub.admin("den", "update", {"zip_b64": payload})
            self.assertTrue(pump.exited.wait(3))
        self.assertEqual((result["from"], result["to"]), ("unknown", "9.9.9"))
        self.assertTrue((target / "bridge" / "bridge.py").is_file())
        self.assertTrue((target / "server" / "ui.html").is_file())
        self.assertEqual((target / "bridge" / "blinds.json").read_text(), "KEEP")
        with Pump(bridge.Bridge(self.store, self.radio, log=self.logs.append)):
            with self.assertRaises(server.ValidationError) as ctx:
                self.hub.admin("den", "update", {"zip_b64": "bm90IGEgemlw"})
            self.assertIn("not a zip", str(ctx.exception))


class CaptureTests(ServiceTests):
    """Capture mode: recordings of what a motor says, saved on the bridge."""

    def setUp(self):
        super().setUp()
        self.bridge.heartbeat = 0.05
        self.step()

    def files(self):
        return self.store.capture_names()

    def test_capture_now_saves_a_private_file_without_the_key(self):
        with Pump(self.bridge):
            got = self.hub.admin("den", "capture", {"blind": "Kitchen", "note": "app shows 87%"})
            self.assertEqual((got["code"], got["reads"], got["notifies"], got["chars"]), ("ok", 1, 1, 2))
            self.assertEqual(self.files(), [got["file"]])
            for _ in range(100):
                if self.hub.state()["bridges"][0]["captures"] == 1:
                    break
                __import__("time").sleep(0.02)
        path = self.store.captures_dir / got["file"]
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text()
        record = json.loads(text)
        self.assertEqual((record["kind"], record["note"], record["blind"]["name"], record["pos"]),
                         ("probe", "app shows 87%", "Kitchen", None))
        self.assertNotIn("0a1b2c3d4e5f", text)
        self.assertNotIn('"bt"', text)
        self.assertEqual(self.radio.positions, {})                      # a capture never moves the blind
        self.assertEqual(self.hub.state()["bridges"][0]["captures"], 1)

    def test_capture_of_an_unreachable_motor_is_still_saved(self):
        self.radio.unreachable.add("AA:00:00:00:00:01")
        with Pump(self.bridge):
            got = self.hub.admin("den", "capture", {"blind": "Kitchen"})
        self.assertEqual((got["code"], got["errors"]), ("unreachable", 1))
        self.assertEqual(len(self.files()), 1)
        with Pump(self.bridge):
            with self.assertRaises(server.ValidationError):
                self.hub.admin("den", "capture", {"blind": "Nobody"})

    def test_capture_mode_records_moves_and_still_moves(self):
        with Pump(self.bridge):
            self.assertFalse(self.hub.state()["bridges"][0]["capture"])
            self.assertEqual(self.hub.admin("den", "capture_mode", {"on": True}), {"on": True, "minutes": 60})
        self.assertTrue(json.loads(self.store.config_path.read_text())["capture"])
        self.step()
        self.assertTrue(self.hub.state()["bridges"][0]["capture"])
        ok, _ = asyncio.run(self.bridge.move_blind(self.store.find("Study"), 100))
        self.assertTrue(ok)
        self.assertEqual(self.radio.positions, {"AA:00:00:00:00:02": 100})
        self.assertEqual(self.radio.sessions, 0)                        # the recorded try did the move
        record = json.loads((self.store.captures_dir / self.files()[0]).read_text())
        self.assertEqual((record["kind"], record["pos"], record["result"]["code"]), ("move", 100, "ok"))
        with Pump(self.bridge):
            self.hub.admin("den", "capture_mode", {"on": False})
        asyncio.run(self.bridge.move_blind(self.store.find("Study"), 200))
        self.assertEqual((len(self.files()), self.radio.sessions), (1, 1))

    def test_capture_mode_switches_itself_off(self):
        self.store.config["capture"] = True                              # on, with no end time recorded
        self.assertFalse(self.bridge.capturing())
        self.assertFalse(json.loads(self.store.config_path.read_text())["capture"])
        now = [1000.0]
        self.bridge.clock = lambda: now[0]
        asyncio.run(self.bridge.admin("capture_mode", {"on": True}))
        now[0] += bridge.CAPTURE_FOR - 5
        self.assertTrue(self.bridge.capturing())
        self.assertTrue(self.bridge.report(0)["capture"])
        now[0] += 10
        self.assertFalse(self.bridge.report(0)["capture"])
        saved = json.loads(self.store.config_path.read_text())
        self.assertEqual((saved["capture"], "capture_until" in saved), (False, False))
        asyncio.run(self.bridge.move_blind(self.store.find("Study"), 100))
        self.assertEqual(self.files(), [])                               # an expired mode records nothing

    def test_a_failed_recorded_move_falls_back_to_the_plain_move(self):
        self.store.config.update(capture=True, capture_until=self.bridge.clock() + 600)
        self.radio.unreachable.add("AA:00:00:00:00:02")
        ok, error = asyncio.run(self.bridge.move_blind(self.store.find("Study"), 100))
        self.assertEqual((ok, error, self.radio.move_attempts), (False, "not found", 1))
        self.assertEqual(len(self.files()), 1)

    def test_bundle_clear_and_pruning(self):
        self.store.save_blinds()
        for n in range(bridge.CAPTURE_KEEP + 3):
            self.store.save_capture({"kind": "probe", "blind": {"name": "Kitchen"}, "n": n}, f"20260101-{n:06d}")
        names = self.files()
        self.assertEqual(len(names), bridge.CAPTURE_KEEP)
        self.assertTrue(names[0].startswith("20260101-000003"))         # the three oldest were dropped
        self.bridge.capture_count = len(names)
        with Pump(self.bridge):
            got = self.hub.admin("den", "captures")
            import zlib
            records = json.loads(zlib.decompress(base64.b64decode(got["z"])))
            self.assertEqual((got["count"], got["total"], len(records)), (bridge.CAPTURE_KEEP,) * 3)
            self.assertEqual([r["n"] for r in records], list(range(3, bridge.CAPTURE_KEEP + 3)))
            self.assertEqual(records[-1]["file"], names[-1])
            small = self.store.bundle_captures(limit=1)                 # always at least the newest
            self.assertEqual(small["count"], 1)
            self.assertEqual(self.hub.admin("den", "capture_clear"), {"removed": bridge.CAPTURE_KEEP})
        self.assertEqual(self.files(), [])
        self.assertTrue(self.store.blinds_path.exists())           # clearing touches captures only

    def test_captures_never_ride_along_in_a_release_or_update(self):
        import io, zipfile
        sys.path.insert(0, str(HERE.parent))
        import updater
        install = Path(self.tmp.name) / "install"
        for rel in ("server/server.py", "bridge/bridge.py", "VERSION", "bridge/captures/20260101-x-probe.json"):
            (install / rel).parent.mkdir(parents=True, exist_ok=True)
            (install / rel).write_text("1.0.0\n" if rel == "VERSION" else "x = 1\n")
        for name in ("pending.json", "stuck.txt", "bridge.json", "blinds.json", "wifi_off"):
            (install / "bridge" / name).write_text("{}")
        names = zipfile.ZipFile(io.BytesIO(updater.build_zip(install))).namelist()
        self.assertEqual(sorted(names), ["blindbridge/VERSION", "blindbridge/bridge/bridge.py", "blindbridge/server/server.py"])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for rel in ("server/server.py", "bridge/bridge.py"):
                archive.writestr("blindbridge/" + rel, "x = 1\n")
            archive.writestr("blindbridge/VERSION", "9.9.9\n")
            archive.writestr("blindbridge/bridge/captures/evil.json", "{}")
        with self.assertRaises(updater.UpdateError):
            updater.read_update(buffer.getvalue())


class FakeGatt:
    """Just enough of bleak's client to exercise BleakRadio.probe."""

    def __init__(self, key: bytes, hang_up_after_key=False, refuse_key=False):
        from types import SimpleNamespace as NS
        base = "-1212-efde-1600-785feabcd123"

        def char(short, handle, props, descriptors=()):
            return NS(uuid=f"0000{short}{base}", handle=handle, properties=list(props), descriptors=list(descriptors))

        name = NS(uuid="00002901-0000-1000-8000-00805f9b34fb", handle=9, characteristic_uuid=f"00001651{base}")
        cccd = NS(uuid="00002902-0000-1000-8000-00805f9b34fb", handle=4, characteristic_uuid=f"00001402{base}")
        self.chars = [char("1409", 1, ["read", "write"]), char("1403", 2, ["read", "write"]),
                      char("1402", 3, ["read", "notify"], [cccd]), char("1651", 8, ["read"], [name]),
                      char("1777", 11, ["read"])]
        self.services = [NS(uuid=f"00001400{base}", handle=0, characteristics=self.chars)]
        self.key, self.hang_up_after_key, self.refuse_key = key, hang_up_after_key, refuse_key
        self.is_connected = True
        self.writes, self.read_uuids, self.callback = [], [], None
        self.callbacks = {}
        self.answers = True          # acknowledge a position and report the angle reached
        self.refuse_connection = False
        self.sensors = b"\xff" * 14
        self.journal = None          # a list to note each connection in, in order
        self.timeouts = []

    def __call__(self, device, timeout=None):
        self.timeouts.append(timeout)
        self.devices = getattr(self, "devices", []) + [device]
        return self

    async def __aenter__(self):
        if self.journal is not None:
            self.journal.append("connect")
        if self.refuse_connection:
            raise TimeoutError()
        return self

    async def __aexit__(self, *exc):
        self.is_connected = False

    async def write_gatt_char(self, uuid, data, response=None):
        if uuid == bridge.KEY_UUID:
            if self.refuse_key:
                raise RuntimeError("write not permitted")
            if self.hang_up_after_key:
                self.is_connected = False
        self.writes.append((uuid, bytes(data)))
        if uuid == bridge.POS_UUID and self.answers:
            ack, angle = self.callbacks.get("1503"), self.callbacks.get("1403")
            if ack:
                ack(None, bytearray(b"\xff" * 20))                                     # stale, must be ignored
                ack(None, bytearray(bytes.fromhex("0314000230") + bytes(data) + bytes(14)))
            if angle:
                asyncio.get_running_loop().call_later(0.2, angle, None, bytearray([max(data[0] - 2, 0)]))

    async def read_gatt_char(self, char):
        short = (char if isinstance(char, str) else char.uuid)[4:8]
        self.read_uuids.append(short)
        if isinstance(char, str):
            return bytearray(self.sensors if short == "1651" else bytes.fromhex("000015a000000000"))
        if short == "1777":
            raise RuntimeError("read not permitted")
        if self.callback and short == "1402":
            self.callback(char, bytearray(b"\x07\x08"))
        return bytearray(b"\x5f" + self.key + b"\x01") if short == "1651" else bytearray(b"\x64")

    async def start_notify(self, char, callback):
        self.callbacks[(char if isinstance(char, str) else char.uuid)[4:8]] = callback
        if not isinstance(char, str):
            self.callback = callback

    async def read_gatt_descriptor(self, desc):
        return bytearray(b"Sensors")


class ProbeTests(unittest.TestCase):
    """BleakRadio.probe against a stand-in for the Bluetooth library."""

    KEY = bytes.fromhex("0a1b2c3d4e5f")

    def run_probe(self, gatt, pos=None, found=True):
        from types import SimpleNamespace as NS, ModuleType
        fake = ModuleType("bleak")
        fake.BleakClient = gatt
        radio = bridge.BleakRadio()
        adv = NS(local_name="SmartBlind_DFU", rssi=-61, tx_power=None, manufacturer_data={0x0059: b"\x01\x02"},
                 service_data={}, service_uuids=["00001400-1212-efde-1600-785feabcd123"])

        async def find(mac):
            return ("device", -61, adv) if found else (None, None, None)

        radio._find = find
        old, old_every = sys.modules.get("bleak"), bridge.CAPTURE_READ_EVERY
        sys.modules["bleak"] = fake
        bridge.CAPTURE_READ_EVERY = 0.3
        try:
            return asyncio.run(radio.probe("aa:00:00:00:00:01", self.KEY, pos=pos, listen=1.6))
        finally:
            bridge.CAPTURE_READ_EVERY = old_every
            if old is None:
                del sys.modules["bleak"]
            else:
                sys.modules["bleak"] = old

    def test_records_everything_but_the_key(self):
        gatt = FakeGatt(self.KEY)
        code, error, rssi, capture = self.run_probe(gatt)
        self.assertEqual((code, error, rssi), ("ok", "", -61))
        self.assertEqual(capture["adv"]["manufacturer"], {"0x0059": "0102"})
        self.assertEqual([c["uuid"][4:8] for c in capture["gatt"][0]["chars"]], ["1409", "1403", "1402", "1651", "1777"])
        ops = [e["op"] for e in capture["events"]]
        for wanted in ("found", "connected", "listed", "key-written", "subscribed", "descriptor", "read", "notify", "error", "done"):
            self.assertIn(wanted, ops)
        self.assertNotIn("1409", gatt.read_uuids)                       # the key characteristic is never read
        text = json.dumps(capture)
        self.assertNotIn(self.KEY.hex(), text)
        self.assertIn("5f<key>01", text)                                # a value echoing the key is masked
        whens = {e["when"] for e in capture["events"] if e["op"] == "read"}
        self.assertEqual(whens, {"start", "during", "end"})
        self.assertEqual(gatt.writes, [(bridge.KEY_UUID, self.KEY)])    # nothing moved
        self.assertTrue(all(a["t"] <= b["t"] for a, b in zip(capture["events"], capture["events"][1:])))

    def test_with_a_position_it_moves_between_reads(self):
        gatt = FakeGatt(self.KEY)
        code, _, _, capture = self.run_probe(gatt, pos=100)
        self.assertEqual(code, "ok")
        self.assertEqual(gatt.writes, [(bridge.KEY_UUID, self.KEY), (bridge.POS_UUID, b"\x64")])
        ops = [e["op"] for e in capture["events"]]
        self.assertLess(ops.index("read"), ops.index("position-written"))
        self.assertIn("read", ops[ops.index("position-written"):])

    def test_failures_are_recorded_not_raised(self):
        code, error, rssi, capture = self.run_probe(FakeGatt(self.KEY), found=False)
        self.assertEqual((code, rssi, capture["adv"]), ("unreachable", None, None))
        code, error, _, capture = self.run_probe(FakeGatt(self.KEY, refuse_key=True))
        self.assertEqual((code, error), ("rejected", "write not permitted"))
        self.assertEqual(capture["events"][-1]["op"], "error")
        self.assertTrue(capture["gatt"])                                # the layout was still recorded
        code, error, _, capture = self.run_probe(FakeGatt(self.KEY, hang_up_after_key=True))
        self.assertEqual((code, error), ("rejected", "motor hung up after the key"))
        gatt = FakeGatt(self.KEY, hang_up_after_key=True)
        self.assertEqual(self.run_probe(gatt, pos=200)[0], "ok")        # the position was written before it left
        self.assertNotIn(self.KEY.hex(), json.dumps(capture))


class ConfirmedMoveTests(unittest.TestCase):
    """Moves wait for the motor to acknowledge and to say where it ended up."""

    KEY = bytes.fromhex("0a1b2c3d4e5f")

    PATH = "/org/bluez/hci0/dev_AA_00_00_00_00_01"

    def radio(self, gatt, found=True, bus_error=None):
        """A real BleakRadio over a stand-in Bluetooth library and a stand-in system bus.

        `radio.journal` lists, in order, what was asked of the system bus and each connection.
        """
        from types import SimpleNamespace as NS, ModuleType
        fake = ModuleType("bleak")
        fake.BleakClient = gatt
        radio = bridge.BleakRadio()
        radio.ack_wait, radio.after_ack, radio.purge_settle = 0.3, 0.0, 0.0
        adv = NS(local_name="SmartBlind_DFU", rssi=-70, service_data={bridge.STATE_UUID: bytes.fromhex("0100000000aa8043")})
        radio.journal = gatt.journal = []
        radio.heard = []                         # every record the scanner handed out; each scan makes a new one

        class Bus:
            def send(bus, message):
                radio.journal.append((message.member, message.path, message.flags.value))

            async def call(bus, message):
                if bus_error:
                    raise bus_error
                radio.journal.append((message.member, message.path, message.flags.value))

            def disconnect(bus):
                radio.journal.append("bus closed")

        async def open_bus():
            return Bus()

        async def find(mac):
            radio.journal.append("find")
            if found is False or (found == "once" and radio.heard):
                return None, None, None
            radio.heard.append(NS(address="AA:00:00:00:00:01", details={"path": self.PATH}))
            return radio.heard[-1], -70, adv

        radio._find, radio.open_bus = find, open_bus
        self.addCleanup(sys.modules.pop, "bleak", None) if "bleak" not in sys.modules else \
            self.addCleanup(sys.modules.__setitem__, "bleak", sys.modules["bleak"])
        sys.modules["bleak"] = fake
        return radio

    def test_acknowledged_move_reports_the_angle_reached(self):
        gatt = FakeGatt(self.KEY)
        gatt.sensors = bytes([67]) + bytes(13)
        detail = {}
        ok, error, rssi = asyncio.run(self.radio(gatt).move("aa:00:00:00:00:01", self.KEY, 200, detail=detail))
        self.assertEqual((ok, error, rssi), (True, "", -70))
        self.assertEqual(gatt.writes, [(bridge.KEY_UUID, self.KEY), (bridge.POS_UUID, b"\xc8")])
        self.assertEqual((detail["ack"], detail["was"], detail["tries"]), ("0314000230c8", 67, 1))
        self.assertEqual((detail["battery"], detail["charging"], detail["adv"]), (67, "solar", "0100000000aa8043"))
        self.assertEqual(detail["stat"], "000015a000000000")             # the raw status bytes, for the log
        self.assertEqual(sorted(gatt.callbacks), ["1503"])               # only what a move needs
        self.assertLess(gatt.read_uuids.index("1402"), 2)                # readings are taken before the move
        self.assertNotIn("angle", detail)

    def test_position_zero_is_acknowledged_in_full_and_readings_are_not_repeated(self):
        gatt = FakeGatt(self.KEY)
        radio = self.radio(gatt)
        first, second = {}, {}
        asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 0, detail=first))
        reads = len(gatt.read_uuids)
        asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 80, detail=second))
        self.assertEqual((first["ack"], second["ack"]), ("031400023000", "031400023050"))   # as a motor sends them
        self.assertEqual((reads, len(gatt.read_uuids)), (2, 2))          # second move within 15 minutes: no reads
        self.assertNotIn("charging", second)

    def test_a_link_that_dies_after_the_acknowledgement_is_still_a_success(self):
        gatt = FakeGatt(self.KEY)

        async def dying_exit(*exc):
            raise RuntimeError("link supervision timeout")

        gatt.__class__ = type("DyingGatt", (FakeGatt,), {"__aexit__": lambda self, *exc: dying_exit()})
        ok, error, _ = asyncio.run(self.radio(gatt).move("aa:00:00:00:00:01", self.KEY, 100))
        self.assertEqual((ok, error), (True, ""))
        self.assertEqual(len([w for w in gatt.writes if w[0] == bridge.POS_UUID]), 1)   # not sent again

    def test_broadcast_position_is_read_without_connecting(self):
        from types import SimpleNamespace as NS
        adv = lambda tail: NS(service_data={bridge.STATE_UUID: bytes.fromhex("5792609a81f0" + tail)})
        self.assertEqual(bridge.adv_position(adv("8000")), 0)            # Bedroom before the first command
        self.assertEqual(bridge.adv_position(adv("80c6")), 198)          # and after it reported angle 198
        self.assertEqual(bridge.adv_position(adv("8043")), 67)           # Study
        self.assertIsNone(bridge.adv_position(adv("80ff")))
        self.assertIsNone(bridge.adv_position(NS(service_data={})))
        self.assertIsNone(bridge.adv_position(NS(service_data={bridge.STATE_UUID: b"\x01\x02"})))
        gatt = FakeGatt(self.KEY)
        gatt.refuse_connection = True
        radio = self.radio(gatt)
        detail = {}
        code, error, rssi = asyncio.run(radio.session("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertEqual((code, error, rssi, detail["was"]), ("unreachable", "TimeoutError", -70, 67))

    def quick(self):
        """No waiting between tries."""
        real_sleep = asyncio.sleep
        bridge.asyncio.sleep = lambda seconds: real_sleep(0)
        self.addCleanup(setattr, bridge.asyncio, "sleep", real_sleep)

    def test_the_kernel_is_made_to_forget_the_motor_before_the_first_connection(self):
        gatt = FakeGatt(self.KEY)
        radio = self.radio(gatt)
        detail = {}
        ok, _, _ = asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertTrue(ok)
        # Connect is sent without waiting for an answer and Disconnect right behind it. The Bluetooth service then
        # drops its record of the motor, so the motor is found again, and only then comes the real connection.
        self.assertEqual(radio.journal, ["find", ("Connect", self.PATH, 1), ("Disconnect", self.PATH, 0), "bus closed",
                                         "find", "connect"])
        self.assertIs(gatt.devices[0], radio.heard[1])                   # the fresh record, not the one from before
        self.assertEqual(gatt.timeouts, [bridge.CONNECT_TIMEOUT])        # long enough for a slow link
        self.assertGreaterEqual(bridge.CONNECT_TIMEOUT, 60)
        self.assertIsInstance(detail["connect"], float)                  # how long connecting took, for the log
        self.assertNotIn("notes", detail)

    def test_a_second_try_does_without_the_purge_and_then_the_radio_counts_as_stuck(self):
        self.quick()
        gatt = FakeGatt(self.KEY)
        gatt.refuse_connection = True
        radio = self.radio(gatt)
        detail = {}
        ok, error, rssi = asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertEqual((ok, error, rssi, detail["tries"], detail["stuck"]), (False, "TimeoutError", -70, 2, True))
        self.assertEqual([step for step in radio.journal if step != "bus closed"],
                         ["find", ("Connect", self.PATH, 1), ("Disconnect", self.PATH, 0), "find", "connect",
                          "find", "connect"])
        self.assertIsInstance(detail["gave_up"], float)
        self.assertEqual(detail["notes"], ["try 1: TimeoutError", "try 2: TimeoutError"])
        self.assertEqual(detail["was"], 67)                              # still learned where the blind is

    def test_a_second_try_can_succeed(self):
        self.quick()
        gatt = FakeGatt(self.KEY)
        gatt.refuse_connection = True
        radio = self.radio(gatt)
        real_enter = FakeGatt.__aenter__

        async def enter(self_):
            if radio.journal.count("connect") >= 1:
                gatt.refuse_connection = False
            return await real_enter(self_)

        gatt.__class__ = type("SecondTimeLucky", (FakeGatt,), {"__aenter__": enter})
        detail = {}
        ok, _, _ = asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertEqual((ok, detail["tries"], detail["notes"]), (True, 2, ["try 1: TimeoutError"]))
        self.assertNotIn("stuck", detail)

    def test_a_motor_that_goes_silent_after_the_purge_is_out_of_range_not_stuck(self):
        self.quick()
        gatt = FakeGatt(self.KEY)
        radio = self.radio(gatt, found="once")
        detail = {}
        ok, error, rssi = asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertEqual((ok, error, rssi), (False, bridge.NOT_FOUND, -70))
        self.assertNotIn("stuck", detail)                                # no restart of the Pi over this
        self.assertNotIn("connect", radio.journal)

    def test_a_purge_that_could_not_run_keeps_the_record_it_has(self):
        gatt = FakeGatt(self.KEY)
        radio = self.radio(gatt, bus_error=RuntimeError("bus is gone"))
        asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 100))
        self.assertEqual(radio.journal.count("find"), 1)                 # nothing was cancelled: no second scan
        self.assertIs(gatt.devices[0], radio.heard[0])

    def test_a_purge_that_cannot_run_never_stops_the_move(self):
        gatt = FakeGatt(self.KEY)
        detail = {}
        ok, _, _ = asyncio.run(self.radio(gatt, bus_error=RuntimeError("bus is gone")).move(
            "aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertEqual((ok, detail["notes"]), (True, ["no purge: bus is gone"]))

        radio = self.radio(FakeGatt(self.KEY))

        async def no_bus():
            raise OSError("no system bus")

        radio.open_bus = no_bus
        from types import SimpleNamespace as NS
        self.assertEqual(asyncio.run(radio.purge(NS(details={"path": self.PATH}))), "no purge: no system bus")
        self.assertEqual(asyncio.run(radio.purge("a device without details")), "no purge: no device path")
        self.assertEqual(asyncio.run(radio.purge(None)), "no purge: no device path")

    def test_a_motor_that_is_not_heard_is_out_of_range_not_stuck(self):
        self.quick()
        radio = self.radio(FakeGatt(self.KEY), found=False)
        detail = {}
        ok, error, rssi = asyncio.run(radio.move("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertEqual((ok, error, rssi, radio.journal), (False, bridge.NOT_FOUND, None, ["find", "find"]))
        self.assertNotIn("stuck", detail)

    def test_system_commands_report_their_exit_code_and_uptime_is_read(self):
        self.assertEqual(bridge.system_run([sys.executable, "-c", "import sys; sys.exit(3)"])[0], 3)
        self.assertEqual(bridge.system_run(["/nonexistent/tool"])[0], -1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "uptime"
            path.write_text("1083.42 4100.11\n")
            self.assertEqual(bridge.system_uptime(str(path)), 1083.42)
            self.assertEqual(bridge.system_uptime(str(path) + ".missing"), float("inf"))

    def test_blank_sensor_readings_are_left_out(self):
        detail = {}
        asyncio.run(self.radio(FakeGatt(self.KEY)).move("aa:00:00:00:00:01", self.KEY, 100, detail=detail))
        self.assertNotIn("battery", detail)
        self.assertEqual(detail["charging"], "solar")

    def test_a_position_the_motor_never_confirms_is_a_failure_and_is_sent_twice_at_most(self):
        gatt = FakeGatt(self.KEY)
        gatt.answers = False
        detail = {}
        ok, error, _ = asyncio.run(self.radio(gatt).move("aa:00:00:00:00:01", self.KEY, 200, detail=detail))
        self.assertEqual((ok, error, detail["tries"]), (False, bridge.UNCONFIRMED, 2))
        self.assertEqual([w for w in gatt.writes if w[0] == bridge.POS_UUID], [(bridge.POS_UUID, b"\xc8")] * 2)

    def test_readings_decode_as_the_motors_sent_them(self):
        self.assertIsNone(bridge.parse_sensors(b"\xff" * 14))            # what both motors answered on Nov 16
        self.assertIsNone(bridge.parse_sensors(bytes([67])))
        self.assertEqual(bridge.parse_sensors(bytes([67]) + bytes(13)), {"battery": 67})
        self.assertEqual(bridge.parse_status(bytes.fromhex("000015a000000000")), {"charging": "solar"})
        self.assertEqual(bridge.parse_status(bytes.fromhex("0000080000000000")), {"charging": "usb"})
        self.assertEqual(bridge.parse_status(bytes(8)), {"charging": ""})
        self.assertIsNone(bridge.parse_status(b"\xff" * 8))
        base = "-1212-efde-1600-785feabcd123"
        events = [                                                       # from the Bedroom capture, Nov 16 09:19
            {"op": "read", "uuid": "00001403" + base, "hex": "ff"},
            {"op": "read", "uuid": "00001651" + base, "hex": "ff" * 14},
            {"op": "notify", "uuid": "00001402" + base, "hex": "000015a000000000"},
            {"op": "position-written", "pos": 200},
            {"op": "notify", "uuid": "00001503" + base, "hex": "0314000230c80000000000000000000000000000"},
            {"op": "notify", "uuid": "00001403" + base, "hex": "c6"},
            {"op": "notify", "uuid": "00001503" + base, "hex": "ff" * 20},
        ]
        self.assertEqual(bridge.Bridge.decode_capture(events), {"charging": "solar", "ack": "0314000230c8", "angle": 198})


class HealthTests(ServiceTests):
    def test_reported_angle_and_battery_reach_the_page(self):
        self.radio.says = {"ack": "0314000230c8", "angle": 198, "battery": 67, "charging": "solar"}
        ok, _ = asyncio.run(self.bridge.move_blind(self.store.find("Study"), 200))
        self.assertTrue(ok)
        saved = self.store.find("Study")
        self.assertEqual((saved["pos"], saved["battery"], saved["charging"]), (198, 67, "solar"))
        self.step()
        study = [b for b in self.hub.state()["blinds"] if b["name"] == "Study"][0]
        self.assertEqual((study["pos"], study["battery"], study["charging"]), (198, 67, "solar"))
        self.assertLess(study["health_ago"], 5)
        kitchen = [b for b in self.hub.state()["blinds"] if b["name"] == "Kitchen"][0]
        self.assertEqual((kitchen["battery"], kitchen["charging"], kitchen["health_ago"]), (None, "", None))

    def test_scan_updates_positions_from_what_motors_broadcast(self):
        async def scan(seconds, everything=False):
            return [{"mac": "AA:00:00:00:00:02", "name": "SmartBlind_DFU", "rssi": -78, "pos": 67},
                    {"mac": "AA:00:00:00:00:09", "name": "SmartBlind_DFU", "rssi": -90, "pos": None}]

        self.radio.scan = scan
        found = asyncio.run(self.bridge.admin("scan", {}))["found"]
        self.assertEqual([(f["known"], f["pos"]) for f in found], [("Study", 67), ("", None)])
        self.assertEqual(self.store.find("Study")["pos"], 67)
        self.assertEqual(json.loads(self.store.blinds_path.read_text())[1]["pos"], 67)
        self.assertEqual(self.bridge.status[self.store.find("Study")["id"]]["rssi"], -78)

    def test_a_failed_move_still_learns_where_the_blind_is(self):
        async def move(mac, key, pos, attempts=3, detail=None):
            detail.update({"was": 67, "tries": 3})
            return False, "TimeoutError", -78

        self.radio.move = move
        ok, error = asyncio.run(self.bridge.move_blind(self.store.find("Study"), 100))
        self.assertEqual((ok, error, self.store.find("Study")["pos"]), (False, "TimeoutError", 67))
        self.assertNotIn("health_at", self.store.find("Study"))

    def stuck_radio(self):
        async def move(mac, key, pos, attempts=2, detail=None):
            self.moves = getattr(self, "moves", 0) + 1
            detail.update({"was": 67, "tries": 2, "stuck": True, "gave_up": 75.0})
            return False, "TimeoutError", -47

        self.radio.move = move
        self.commands = []

        def system(argv, timeout=20.0):
            self.commands.append(" ".join(argv))
            return (0, 's "yes"') if argv[0] == "busctl" else (0, "")

        self.bridge.system = system
        self.bridge.uptime = lambda: 3600.0                              # the Pi has been up for an hour

    def test_stuck_bluetooth_restarts_the_pi_and_the_move_runs_afterwards(self):
        self.stuck_radio()
        study = self.store.find("Study")
        self.step()
        self.hub.enqueue([study["id"]], 100, "manual")
        self.step()                                                      # the move runs and fails, stuck
        self.assertEqual([m["pos"] for m in json.loads(self.store.pending_path.read_text())], [100])
        self.assertEqual(stat.S_IMODE(self.store.pending_path.stat().st_mode), 0o600)
        self.assertNotIn("/usr/bin/systemctl reboot", self.commands)     # not before the page has been told
        self.step()                                                      # the result is delivered, then the restart
        state = [b for b in self.hub.state()["blinds"] if b["name"] == "Study"][0]
        self.assertEqual((state["ok"], state["err"]), (False, bridge.REBOOT_NOTE))
        self.assertEqual(self.commands[-1], "/usr/bin/systemctl reboot")
        self.assertIn(bridge.REBOOT_NOTE, " ".join(e["message"] for e in self.hub.state()["log"]))
        snapshot = self.store.stuck_path.read_text()                     # the evidence, saved before the restart
        self.assertTrue(snapshot.startswith("Study -> 100: TimeoutError after 2 tries"))
        self.assertIn("== bluetooth service log", snapshot)
        self.assertEqual(stat.S_IMODE(self.store.stuck_path.stat().st_mode), 0o600)
        self.assertIn("SNAPSHOT FROM THE LAST TIME BLUETOOTH WAS STUCK", self.bridge.diagnostics())
        self.assertNotIn("SNAPSHOT", snapshot)

        # The Pi comes back: a new bridge finds the saved move and carries it out.
        radio = FakeRadio({"AA:00:00:00:00:01": bytes.fromhex("0a1b2c3d4e5f"),
                           "AA:00:00:00:00:02": bytes.fromhex("111111111111")})
        fresh = bridge.Bridge(bridge.Store(self.store.folder), radio, log=lambda message: None)
        fresh.resume_delay = 0
        saved = fresh.prepare()
        self.assertFalse(fresh.store.pending_path.exists())              # it can never run twice
        asyncio.run(fresh.resume(saved))
        self.assertEqual(radio.positions, {"AA:00:00:00:00:02": 100})
        asyncio.run(fresh.step(0))
        self.assertIn("after the restart, Study moved to 100", [e["message"] for e in self.hub.state()["log"]])
        self.assertEqual(fresh.notes, [])

    def test_the_pi_is_not_restarted_too_soon_too_often_or_when_switched_off(self):
        self.stuck_radio()
        study = self.store.find("Study")
        run = lambda: asyncio.run(self.bridge.move_blind(study, 100))
        self.bridge.uptime = lambda: 60.0                                # the Pi started a minute ago
        self.assertEqual((run(), self.bridge.reboot_requested), ((False, "TimeoutError"), False))
        self.bridge.uptime = lambda: 3600.0
        self.store.config["auto_reboot"] = False
        self.assertEqual((run(), self.bridge.reboot_requested), ((False, "TimeoutError"), False))
        del self.store.config["auto_reboot"]
        self.store.config["last_auto_reboot"] = int(self.bridge.clock()) - 600
        self.assertEqual((run(), self.bridge.reboot_requested), ((False, "TimeoutError"), False))
        self.store.config["last_auto_reboot"] = int(self.bridge.clock()) - 3600
        self.bridge.system = lambda argv, timeout=20.0: (0, 's "challenge"')
        self.assertEqual((run(), self.bridge.reboot_requested), ((False, "TimeoutError"), False))
        self.assertFalse(self.store.pending_path.exists())
        self.bridge.system = lambda argv, timeout=20.0: (0, 's "yes"')
        self.assertEqual((run(), self.bridge.reboot_requested), ((False, bridge.REBOOT_NOTE), True))
        self.assertEqual(asyncio.run(self.bridge.move_blind(study, 100, allow_reboot=False)), (False, "TimeoutError"))

    def test_a_stale_or_broken_saved_move_is_dropped(self):
        study = self.store.find("Study")
        asyncio.run(self.bridge.resume([{"blind": study["id"], "pos": 100, "at": self.bridge.clock() - 3600},
                                        {"blind": "nobody", "pos": 100, "at": self.bridge.clock()},
                                        {"blind": study["id"], "pos": 999, "at": self.bridge.clock()},
                                        {"blind": study["id"], "pos": True, "at": self.bridge.clock()},
                                        {"blind": study["id"], "pos": 100}]))
        self.assertEqual((self.radio.positions, self.bridge.notes), ({}, []))
        self.store.pending_path.write_text("not json")
        self.assertEqual(self.store.take_pending(), [])
        self.assertFalse(self.store.pending_path.exists())
        self.store.pending_path.write_text(json.dumps({"blind": study["id"], "pos": 100, "at": 5}))
        self.assertEqual(self.store.take_pending(), [])                  # not a list of moves

    def test_every_waiting_move_is_carried_out_after_the_restart(self):
        self.stuck_radio()
        study, kitchen = self.store.find("Study"), self.store.find("Kitchen")
        self.step()
        self.hub.enqueue([study["id"], kitchen["id"]], 100, "manual")
        self.hub.enqueue([kitchen["id"]], 200, "manual")                 # a later press for the same blind
        for _ in range(4):
            self.step()
        self.assertEqual(self.moves, 1)                                  # only the first move went to the radio
        self.assertEqual(self.commands[-1], "/usr/bin/systemctl reboot")
        states = {b["name"]: b["err"] for b in self.hub.state()["blinds"]}
        self.assertEqual(states, {"Kitchen": bridge.REBOOT_NOTE, "Study": bridge.REBOOT_NOTE})

        radio = FakeRadio({"AA:00:00:00:00:01": bytes.fromhex("0a1b2c3d4e5f"),
                           "AA:00:00:00:00:02": bytes.fromhex("111111111111")})
        fresh = bridge.Bridge(bridge.Store(self.store.folder), radio, log=lambda message: None)
        fresh.resume_delay = 0
        asyncio.run(fresh.resume(fresh.prepare()))
        self.assertEqual(radio.positions, {"AA:00:00:00:00:02": 100, "AA:00:00:00:00:01": 200})   # the last press wins
        self.assertEqual(len(fresh.notes), 2)

    def test_a_refused_restart_forgets_the_saved_move(self):
        self.stuck_radio()
        asyncio.run(self.bridge.move_blind(self.store.find("Study"), 100))
        self.bridge.system = lambda argv, timeout=20.0: (1, "Access denied")
        self.step()
        self.assertFalse(self.store.pending_path.exists())
        self.assertFalse(self.bridge.reboot_requested)

    def test_diagnostics_gather_the_machine_state_without_secrets(self):
        asked = []

        def system(argv, timeout=20.0):
            asked.append(argv)
            if argv[:2] == ["journalctl", "-k"]:
                return 0, "t1 kernel: usb 1-1: new device\nt2 kernel: Bluetooth: hci0: command 0x200d tx timeout\nt3 kernel: brcmfmac: power save"
            return 0, "output of " + argv[0]

        self.bridge.system = system
        self.step()
        with Pump(self.bridge):
            text = self.hub.admin("den", "diagnostics")["text"]
        self.assertIn("== controller (exit 0)\noutput of bluetoothctl", text)
        self.assertIn("== what the system knows about Study (exit 0)", text)
        self.assertIn("hci0: command 0x200d tx timeout", text)
        self.assertIn("brcmfmac", text)
        self.assertNotIn("usb 1-1", text)                                # only the Bluetooth lines of the kernel log
        self.assertIn("== kernel (exit 0)\noutput of uname", text)
        self.assertIn("== devices the system has on file (exit 0)", text)
        for secret in ("0a1b2c3d4e5f", "111111111111"):
            self.assertNotIn(secret, text)
        self.assertTrue(all(isinstance(argv, list) for argv in asked))   # never through a shell
        self.assertIn(["bluetoothctl", "info", "AA:00:00:00:00:02"], asked)

    def test_recorded_move_uses_what_the_capture_shows(self):
        self.store.config.update(capture=True, capture_until=self.bridge.clock() + 600)
        ok, _ = asyncio.run(self.bridge.move_blind(self.store.find("Study"), 100))
        self.assertTrue(ok)
        self.assertEqual(self.store.find("Study")["pos"], 100)          # the test radio reports no angle


class WifiPolicyTests(unittest.TestCase):
    """Wi-Fi off while a network cable is working, and back on when it is not."""

    def setUp(self):
        import test_netwatch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.make_sys, self.ip_both, self.ip_wifi = test_netwatch.make_sys, test_netwatch.IP_BOTH, test_netwatch.IP_WIFI
        self.sys = self.make_sys(self.root / "sys")
        self.device = self.root / "rfkill-device"
        self.device.write_text("")
        (self.root / "store").mkdir()
        self.store = bridge.Store(self.root / "store")
        self.store.config.update(server="http://x", token="t", name="den")
        self.ip, self.calls, self.logs, self.refuse = self.ip_both, [], [], False

        def system(argv):
            if argv[0] == "ip":
                return 0, self.ip
            self.calls.append(" ".join(argv))
            if self.refuse:
                return 1, "Operation not permitted"
            soft = self.sys / "rfkill" / "rfkill1" / "soft"         # the stand-in switch really switches
            soft.write_text("1\n" if argv[1] == "block" else "0\n")
            return 0, ""

        self.policy = bridge.WifiPolicy(self.store, system, self.logs.append, sys_path=self.sys, device=str(self.device))

    def unplug(self):
        (self.sys / "net" / "eth0" / "carrier").write_text("0\n")

    def test_left_alone_by_default(self):
        for _ in range(4):
            self.assertEqual(self.policy.tick(), "")
        self.assertEqual((self.calls, self.policy.mode), ([], "on"))
        status = self.policy.status()
        self.assertEqual((status["wifi"], status["cable_ready"], status["cable_ip"], status["wifi_ip"], status["can_switch"]),
                         ("on", True, "192.168.1.136", "192.168.1.142", True))

    def test_wifi_goes_off_on_a_working_cable_after_the_grace_and_two_looks(self):
        status = self.policy.set_mode("auto")
        self.assertEqual((status["mode"], status["grace"]), ("auto", int(bridge.NET_GRACE)))
        self.assertEqual(json.loads(self.store.config_path.read_text())["wifi_mode"], "auto")
        self.assertEqual([self.policy.tick(), self.policy.tick(), self.policy.tick()], ["", "", ""])   # still in the grace
        self.policy.not_before = 0
        self.assertEqual(self.policy.tick(), "network cable is working: wifi switched off")
        self.assertEqual(self.calls, ["rfkill block wlan"])
        self.assertTrue(self.policy.keeping_off())
        self.assertEqual(self.policy.tick(), "")                         # nothing more to do
        self.assertEqual(self.policy.status()["wifi"], "off")

    def test_wifi_comes_back_when_the_cable_stops_working(self):
        self.policy.set_mode("auto")
        self.policy.not_before = 0
        self.policy.tick(), self.policy.tick()
        self.assertTrue(self.policy.keeping_off())
        self.unplug()
        self.assertEqual(self.policy.tick(), "no working network cable: wifi switched back on")
        self.assertEqual(self.calls[-1], "rfkill unblock wlan")
        self.assertFalse(self.policy.keeping_off())
        self.assertFalse(self.store.wifi_off_marker.exists())
        (self.sys / "net" / "eth0" / "carrier").write_text("1\n")       # plugged back in, but one look is not enough
        self.assertEqual(self.policy.tick(), "")
        self.assertEqual(self.policy.tick(), "network cable is working: wifi switched off")

    def test_a_cable_with_no_address_does_not_count(self):
        self.ip = self.ip_wifi
        self.policy.set_mode("auto")
        self.policy.not_before = 0
        self.assertEqual([self.policy.tick() for _ in range(4)], [""] * 4)
        self.assertEqual(self.calls, [])

    def test_switching_the_setting_back_restores_wifi(self):
        self.policy.set_mode("auto")
        self.policy.not_before = 0
        self.policy.tick(), self.policy.tick()
        self.policy.set_mode("on")
        self.assertEqual(self.policy.tick(), "wifi switched back on")
        self.assertEqual((self.calls, self.policy.keeping_off()), (["rfkill block wlan", "rfkill unblock wlan"], False))

    def test_wifi_someone_else_switched_off_is_left_alone_unless_the_rule_is_on(self):
        (self.sys / "rfkill" / "rfkill1" / "soft").write_text("1\n")
        self.unplug()
        self.assertEqual((self.policy.tick(), self.calls), ("", []))     # mode "on": not ours to touch
        self.store.config["wifi_mode"] = "auto"
        self.assertEqual(self.policy.tick(), "no working network cable: wifi switched back on")

    def test_after_a_restart_wifi_left_off_with_no_cable_is_switched_on(self):
        self.store.wifi_off_marker.write_text("1")                       # this program switched it off before the restart
        (self.sys / "rfkill" / "rfkill1" / "soft").write_text("1\n")
        self.unplug()
        fresh = bridge.WifiPolicy(self.store, self.policy.system, self.logs.append, sys_path=self.sys, device=str(self.device))
        self.assertEqual(fresh.tick(), "no working network cable: wifi switched back on")

    def test_wifi_left_alone_costs_nothing_and_a_refused_switch_leaves_no_trace(self):
        asked = []
        idle = bridge.WifiPolicy(self.store, lambda argv: (asked.append(argv), (0, ""))[1], self.logs.append,
                                 sys_path=self.sys, device=str(self.device))
        self.assertEqual([idle.tick(), idle.tick(), asked], ["", "", []])   # setting off: not one command is run
        self.policy.set_mode("auto")
        self.policy.not_before = 0
        self.refuse = True
        self.assertEqual([self.policy.tick(), self.policy.tick()], ["", ""])
        self.assertFalse(self.store.wifi_off_marker.exists())            # a refused switch leaves no marker behind
        self.assertIn("could not switch wifi", " ".join(self.logs))
        self.device.unlink()
        with self.assertRaises(ValueError):
            self.policy.set_mode("auto")
        with self.assertRaises(ValueError):
            self.policy.set_mode("off")

    def test_a_machine_without_wifi_cannot_use_the_rule(self):
        policy = bridge.WifiPolicy(self.store, self.policy.system, self.logs.append,
                                   sys_path=self.make_sys(self.root / "wired-only", with_wifi=False), device=str(self.device))
        self.assertEqual((policy.status()["wifi"], policy.tick()), ("none", ""))
        with self.assertRaises(ValueError):
            policy.set_mode("auto")


class NetworkOpsTests(ServiceTests):
    def test_the_page_can_read_and_change_the_wifi_setting(self):
        import test_netwatch
        sys_path = test_netwatch.make_sys(Path(self.tmp.name) / "sys")
        device = Path(self.tmp.name) / "rfkill-device"
        device.write_text("")
        self.bridge.wifi.sys_path, self.bridge.wifi.device = sys_path, str(device)
        self.bridge.system = lambda argv, timeout=20.0: (0, test_netwatch.IP_BOTH if argv[0] == "ip" else "")
        self.step()
        with Pump(self.bridge):
            got = self.hub.admin("den", "network")
            self.assertEqual((got["mode"], got["wifi"], got["cable_ip"]), ("on", "on", "192.168.1.136"))
            got = self.hub.admin("den", "wifi_mode", {"mode": "auto"})
            self.assertEqual((got["mode"], got["grace"]), ("auto", int(bridge.NET_GRACE)))
            with self.assertRaises(server.ValidationError):
                self.hub.admin("den", "wifi_mode", {"mode": "never"})
        self.assertFalse(self.bridge.report(0)["wifi_off"])
        self.bridge.wifi_off = True
        self.step()
        self.assertTrue(self.hub.state()["bridges"][0]["wifi_off"])


class UpdateGateTests(ServiceTests):
    """A bridge takes new program files only from a server it has good reason to trust on the wire."""

    def test_where_updates_may_come_from(self):
        allowed = {"http://127.0.0.1:8787": True, "http://localhost:8787/": True, "http://[::1]:8787": True,
                   "https://blinds.example.net": True, "http://192.168.1.20:8787": False,
                   "http://blinds.example.net": False, "http://localhost.example.net": False, "": False}
        for url, expected in allowed.items():
            self.store.config["server"] = url
            self.assertIs(self.bridge.update_allowed(), expected, url)
        self.store.config["updates_over_http"] = True                    # the owner's own decision
        self.assertTrue(self.bridge.update_allowed())
        self.store.config["updates_over_http"] = "yes"                   # only a real true counts
        self.assertFalse(self.bridge.update_allowed())

    def test_an_update_over_plain_http_from_another_machine_is_refused_untouched(self):
        target = Path(self.tmp.name) / "install"
        old = bridge.INSTALL_DIR
        bridge.INSTALL_DIR = target
        self.addCleanup(setattr, bridge, "INSTALL_DIR", old)
        self.bridge.update_allowed = lambda: False
        payload = bridge.base64.b64encode(release_zip("9.9.9")).decode()
        with self.assertRaises(ValueError) as caught:
            asyncio.run(self.bridge.admin("update", {"zip_b64": payload}))
        self.assertIn("own machine or over https", str(caught.exception))
        self.assertFalse(target.exists())
        self.assertFalse(self.bridge.exit_requested)


for _inherited in [name for name in dir(ServiceTests) if name.startswith("test_")]:
    setattr(UpdateGateTests, _inherited, None)  # those run in ServiceTests itself


class NameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = bridge.Store(Path(self.tmp.name))

    def test_names_are_kept_to_one_printable_line(self):
        self.assertEqual(bridge.clean_name("  Kit\nchen\x1b[31m \x00 "), "Kitchen[31m")
        self.assertEqual(len(bridge.clean_name("x" * 80)), 48)
        blind, _ = self.store.upsert("Bed\r\nroom\x07", "AA:00:00:00:00:01", "0a0b0c0d0e0f")
        self.assertEqual(blind["name"], "Bedroom")
        nameless, _ = self.store.upsert("\n\t", "AA:00:00:00:00:02", "0a0b0c0d0e0f")
        self.assertEqual(nameless["name"], "aa0000000002")

    def test_rename_refuses_an_empty_or_taken_name_from_the_command_line_too(self):
        self.store.upsert("Kitchen", "AA:00:00:00:00:01", "0a0b0c0d0e0f")
        self.store.upsert("Bedroom", "AA:00:00:00:00:02", "0a0b0c0d0e0f")
        self.store.save_blinds()
        for name in ("bedroom", "  ", "\n"):
            with self.assertRaises(SystemExit):
                bridge.main(["--dir", self.tmp.name, "rename", "Kitchen", name])
        self.assertEqual([b["name"] for b in bridge.Store(Path(self.tmp.name)).blinds], ["Kitchen", "Bedroom"])
        bridge.main(["--dir", self.tmp.name, "rename", "Kitchen", "Den\n"])
        self.assertEqual([b["name"] for b in bridge.Store(Path(self.tmp.name)).blinds], ["Den", "Bedroom"])

    def test_there_is_no_key_guessing_command(self):
        self.assertFalse(hasattr(bridge, "find_key"))
        with self.assertRaises(SystemExit):
            bridge.main(["--dir", self.tmp.name, "find-key", "AA:00:00:00:00:01"])


class MoveCheckTests(ServiceTests):
    """After a motor acknowledges a move, its broadcast is checked to see whether the blind turned."""

    def setUp(self):
        super().setUp()
        self.looks = []                         # what the motor will broadcast, look by look
        self.looked = 0

        async def look(mac, timeout=5.0, linger=1.5):
            self.looked += 1
            return (self.looks.pop(0) if self.looks else None), -61

        async def move(mac, key, pos, attempts=2, detail=None):
            detail.update(was=self.was, tries=1)
            self.radio.positions[mac.upper()] = pos
            return True, "", -60

        self.was = 120
        self.radio.look, self.radio.move = look, move
        self.bridge.check_after = self.bridge.check_again = 0.0
        self.study = self.store.find("Study")
        self.step()

    def press(self, pos, steps=5):
        self.hub.enqueue([self.study["id"]], pos, "manual")
        for _ in range(steps):
            self.step()
        return [b for b in self.hub.state()["blinds"] if b["name"] == "Study"][0]

    def messages(self):
        return [e["message"] for e in self.hub.state()["log"]]

    def test_a_blind_that_turned_is_left_alone_and_its_real_position_kept(self):
        self.looks = [198]
        state = self.press(200)
        self.assertEqual((state["ok"], state["err"], state["pos"], self.looked), (True, "", 198, 1))
        self.assertEqual(self.bridge.checks, {})
        self.assertFalse([m for m in self.messages() if "did not move" in m or "stopped" in m])

    def test_a_blind_that_acknowledged_and_stayed_put_is_marked(self):
        self.looks = [120, 121]                                          # acknowledged, and still where it was
        state = self.press(200)
        self.assertEqual((state["ok"], state["err"], state["pos"]), (False, bridge.NOT_MOVED, 121))
        self.assertEqual(self.looked, 2)                                 # looked twice before saying so
        self.assertIn("Study did not move: sent to 200, still at 121", self.messages())
        self.assertIn("Study moved to 200", self.messages())            # what the motor claimed stays in the record
        self.assertEqual(bridge.Store(self.store.folder).find("Study")["pos"], 121)
        self.looks = [100]                                               # the next move clears the mark
        state = self.press(100)
        self.assertEqual((state["ok"], state["err"], state["pos"]), (True, "", 100))

    def test_a_blind_still_turning_at_the_first_look_gets_a_second(self):
        self.looks = [150, 199]
        state = self.press(200)
        self.assertEqual((state["ok"], state["pos"], self.looked), (True, 199, 2))

    def test_a_blind_that_stopped_partway_is_marked_as_such(self):
        self.was = 197
        self.looks = [130, 120]
        state = self.press(100)
        self.assertEqual((state["ok"], state["err"], state["pos"]), (False, bridge.STOPPED_SHORT, 120))
        self.assertIn("Study stopped at 120, sent to 100", self.messages())

    def test_nothing_is_claimed_when_the_broadcast_cannot_be_heard(self):
        self.looks = []
        state = self.press(200)
        self.assertEqual((state["ok"], state["err"], state["pos"], self.looked), (True, "", 200, 2))
        self.assertIn("could not check the move to 200", " ".join(self.logs))

        async def broken(mac, timeout=5.0, linger=1.5):
            raise RuntimeError("scanner fell over")

        self.radio.look = broken
        state = self.press(100)
        self.assertEqual((state["ok"], state["pos"]), (True, 100))       # the service carries on

    def test_a_newer_move_replaces_a_check_that_is_still_to_come(self):
        self.bridge.check_after = 3600.0
        self.press(200, steps=3)
        first = self.bridge.checks[self.study["id"]]
        self.assertEqual((first["pos"], first["was"], self.looked), (200, 120, 0))
        self.press(100, steps=3)
        self.assertEqual((self.bridge.checks[self.study["id"]]["pos"], len(self.bridge.checks), self.looked), (100, 1, 0))
        self.assertIsNone(self.bridge.due_check())
        asyncio.run(self.bridge.verify(first))                           # the old one, should it ever run, does nothing
        self.assertEqual(self.looked, 0)

    def test_a_move_waits_for_no_check_and_the_bridge_does_not_sleep_through_one(self):
        self.bridge.check_after = 3600.0
        self.press(200, steps=3)
        sent = {}
        real_post = self.bridge._post
        self.bridge._post = lambda body: (sent.update(wait=body["wait"]), real_post(dict(body, wait=0)))[1]
        self.bridge.checks[self.study["id"]]["due"] = bridge.time.monotonic() + 4.2
        asyncio.run(self.bridge.step())
        self.assertEqual(sent["wait"], 5)                                # not the usual long wait


for _inherited in [name for name in dir(ServiceTests) if name.startswith("test_")]:
    setattr(MoveCheckTests, _inherited, None)   # those run in ServiceTests itself; this radio answers differently


class LookTests(unittest.TestCase):
    """BleakRadio.look against a stand-in scanner."""

    def test_the_last_broadcast_heard_is_the_one_that_counts(self):
        from types import SimpleNamespace as NS, ModuleType

        def adv(pos, rssi):
            return NS(rssi=rssi, service_data={bridge.STATE_UUID: bytes.fromhex("0100000000aa80") + bytes([pos])})

        heard = [("11:22:33:44:55:66", adv(5, -40)), ("AA:00:00:00:00:01", adv(120, -70)), ("AA:00:00:00:00:01", adv(200, -66))]

        class Scanner:
            def __init__(self, detection_callback=None):
                self.callback = detection_callback

            async def __aenter__(self):
                loop = asyncio.get_running_loop()
                for index, (mac, data) in enumerate(heard):
                    loop.call_later(0.01 * (index + 1), self.callback, NS(address=mac), data)
                return self

            async def __aexit__(self, *exc):
                return False

        fake = ModuleType("bleak")
        fake.BleakScanner = Scanner
        self.addCleanup(sys.modules.pop, "bleak", None) if "bleak" not in sys.modules else \
            self.addCleanup(sys.modules.__setitem__, "bleak", sys.modules["bleak"])
        sys.modules["bleak"] = fake
        radio = bridge.BleakRadio()
        self.assertEqual(asyncio.run(radio.look("aa:00:00:00:00:01", timeout=1.0, linger=0.1)), (200, -66))
        heard.clear()
        self.assertEqual(asyncio.run(radio.look("aa:00:00:00:00:01", timeout=0.05, linger=0.1)), (None, None))

        class Broken(Scanner):
            async def __aenter__(self):
                raise OSError("no adapter")

        fake.BleakScanner = Broken
        self.assertEqual(asyncio.run(radio.look("aa:00:00:00:00:01", timeout=0.05)), (None, None))


class PurgeOnARealBusTests(unittest.TestCase):
    """purge() against a real message bus with a stand-in for the Bluetooth service on it."""

    PATH = "/org/bluez/hci0/dev_AA_00_00_00_00_01"

    def test_connect_then_disconnect_reach_the_service_in_that_order(self):
        import shutil
        import subprocess
        try:
            from dbus_fast import BusType
            from dbus_fast.aio import MessageBus
            from dbus_fast.service import ServiceInterface, method
        except ImportError:
            self.skipTest("dbus_fast is not installed")
        if not shutil.which("dbus-daemon"):
            self.skipTest("dbus-daemon is not installed")
        daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(daemon.wait)
        self.addCleanup(daemon.terminate)
        self.addCleanup(daemon.stdout.close)
        address = daemon.stdout.readline().strip()
        seen = []

        class Device(ServiceInterface):
            def __init__(self):
                super().__init__("org.bluez.Device1")

            @method()
            def Connect(self):
                seen.append("Connect")

            @method()
            def Disconnect(self):
                seen.append("Disconnect")

        async def run():
            service = await MessageBus(bus_address=address, bus_type=BusType.SESSION).connect()
            service.export(self.PATH, Device())
            await service.request_name("org.bluez")
            radio = bridge.BleakRadio()
            radio.purge_settle = 0.0

            async def open_bus():
                return await MessageBus(bus_address=address, bus_type=BusType.SESSION).connect()

            radio.open_bus = open_bus
            from types import SimpleNamespace as NS
            first = await radio.purge(NS(details={"path": self.PATH}))
            second = await radio.purge(NS(details={"path": self.PATH}))
            missing = await radio.purge(NS(details={"path": self.PATH + "_gone"}))
            service.disconnect()
            return first, second, missing

        first, second, missing = asyncio.run(run())
        self.assertEqual((first, second), ("", ""))
        self.assertEqual(seen, ["Connect", "Disconnect"] * 2)
        self.assertTrue(missing.startswith("no purge: "))                # the service refused; reported, not raised


class ReleaseTests(unittest.TestCase):
    def test_release_zip_is_a_valid_update_and_versions_agree(self):
        import updater
        files = updater.read_update(release_zip())
        version = files["VERSION"].decode().strip()
        self.assertEqual(version, bridge.VERSION)
        self.assertEqual(version, server.VERSION)
        for needed in ("server/ui.html", "deploy/install.sh", "updater.py", "tools/make_release.py"):
            self.assertIn(needed, files)
        self.assertFalse([f for f in files if f.endswith((".db", "config.json", "blinds.json", "bridge.json"))])


class RadioShapeTests(unittest.TestCase):
    """Check the installed bleak still has the calls BleakRadio relies on."""

    def test_bleak_api(self):
        try:
            import inspect
            from bleak import BleakClient, BleakScanner
        except ImportError:
            self.skipTest("bleak not installed")
        self.assertIn("return_adv", inspect.signature(BleakScanner.discover).parameters)
        self.assertIn("detection_callback", inspect.signature(BleakScanner.__init__).parameters)
        self.assertIn("timeout", inspect.signature(BleakClient.__init__).parameters)
        self.assertIn("response", inspect.signature(BleakClient.write_gatt_char).parameters)
        self.assertTrue(hasattr(BleakClient, "is_connected"))
        for call in ("read_gatt_char", "start_notify", "read_gatt_descriptor"):
            self.assertTrue(hasattr(BleakClient, call), call)
        self.assertTrue(hasattr(BleakClient, "services"))


if __name__ == "__main__":
    unittest.main()
