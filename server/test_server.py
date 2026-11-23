"""Tests for the BlindBridge server. Run: python3 -m unittest -v test_server"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import server  # noqa: E402
from fake_bridge import FakeBridge  # noqa: E402

PASSWORD = "correct horse battery"


def make_cfg(**extra) -> dict:
    cfg = dict(server.DEFAULT_CONFIG)
    cfg.update(
        port=0,
        timezone="America/New_York",
        admin_password_hash=server.hash_password(PASSWORD, rounds=1000),
        bridge_token="bridge-token-for-tests",
        api_token="api-token-for-tests",
        latitude=40.0,
        longitude=-75.0,
    )
    cfg.update(extra)
    return cfg


class Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


class LiveServer:
    """A real server on a spare port, plus tiny HTTP helpers."""

    def __init__(self, **cfg_extra):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd, self.hub = server.make_server(make_cfg(**cfg_extra), Path(self.tmp.name))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.hub.db.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                return res.status, json.loads(res.read() or b"{}"), res.headers
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}"), exc.headers

    def api(self, method, path, body=None):
        return self.call(method, path, body, {"Authorization": "Bearer api-token-for-tests"})

    def bridge(self, wait=0, **kw) -> FakeBridge:
        return FakeBridge(self.base, "bridge-token-for-tests", wait=wait, **kw)


class PasswordTests(unittest.TestCase):
    def test_round_trip(self):
        stored = server.hash_password("hunter2hunter2", rounds=1000)
        self.assertTrue(server.verify_password("hunter2hunter2", stored))
        self.assertFalse(server.verify_password("hunter2hunter3", stored))
        self.assertFalse(server.verify_password("x", "not-a-hash"))

    def test_token_equal_rejects_empty(self):
        self.assertFalse(server.token_equal("", ""))
        self.assertFalse(server.token_equal("a", ""))
        self.assertTrue(server.token_equal("abc", "abc"))


class SunTests(unittest.TestCase):
    def test_matches_astral_within_four_minutes(self):
        try:
            from astral import Observer
            from astral.sun import sunrise, sunset
        except ImportError:
            self.skipTest("astral not installed")
        places = [(40.0, -75.0), (47.6062, -122.3321), (25.7617, -80.1918), (51.5072, -0.1276)]
        for lat, lon in places:
            for month in range(1, 13):
                day = dt.date(2026, month, 15)
                rise, sett = server.sun_times(day, lat, lon)
                obs = Observer(latitude=lat, longitude=lon)
                ref_rise = sunrise(obs, date=day, tzinfo=dt.timezone.utc)
                ref_set = sunset(obs, date=day, tzinfo=dt.timezone.utc)
                # astral reports the events on the UTC calendar day; ours are the
                # pair around local solar noon, so compare time of day only.
                for ours, ref in ((rise, ref_rise), (sett, ref_set)):
                    diff = abs((ours - ref).total_seconds()) % 86400
                    diff = min(diff, 86400 - diff)
                    self.assertLess(diff, 240, f"{lat},{lon} {day}: {ours} vs {ref}")

    def test_polar_night_returns_none(self):
        self.assertIsNone(server.sun_times(dt.date(2026, 12, 21), 78.2, 15.6))

    def test_local_sun_is_on_the_local_day(self):
        live = LiveServer()
        try:
            day = dt.date(2026, 11, 13)
            rise, sett = live.hub._local_sun(day, 40.0, -75.0)
            self.assertEqual(rise.date(), day)
            self.assertEqual(sett.date(), day)
            self.assertTrue(6 <= rise.hour <= 7, rise)
            self.assertTrue(16 <= sett.hour <= 17, sett)
        finally:
            live.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.live = LiveServer()
        self.addCleanup(self.live.close)

    def test_state_requires_auth(self):
        status, body, _ = self.live.call("GET", "/api/state")
        self.assertEqual(status, 401)
        status, body, _ = self.live.api("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(body["bridge"]["online"])
        self.assertEqual(body["bridge"]["count"], 0)
        self.assertEqual(body["blinds"], [])

    def test_login_cookie_flow_and_csrf_header(self):
        status, _, _ = self.live.call("POST", "/api/login", {"password": "nope"})
        self.assertEqual(status, 401)
        status, _, headers = self.live.call("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn("Secure", headers["Set-Cookie"])                 # plain http: a Secure cookie would never come back
        status, _, _ = self.live.call("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        # A cookie alone must not be enough to change anything.
        status, body, _ = self.live.call(
            "POST", "/api/move", {"blinds": ["all"], "position": 100}, {"Cookie": cookie}
        )
        self.assertEqual(status, 403)
        status, _, _ = self.live.call("POST", "/api/logout", {}, {"Cookie": cookie, "X-BB": "1"})
        self.assertEqual(status, 200)
        status, _, _ = self.live.call("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 401)

    def test_login_lockout(self):
        for _ in range(5):
            self.assertEqual(self.live.call("POST", "/api/login", {"password": "bad"})[0], 401)
        self.assertEqual(self.live.call("POST", "/api/login", {"password": PASSWORD})[0], 429)
        clock = Clock(time.time() + 301)                                 # five minutes on: allowed, and forgotten
        self.live.hub.clock = clock
        self.assertTrue(self.live.hub.login_allowed("127.0.0.1"))
        self.assertEqual(self.live.hub.login_failures, {})

    def test_bridge_token_is_checked(self):
        bad = FakeBridge(self.live.base, "wrong", wait=0)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            bad.sync_once()
        self.assertEqual(ctx.exception.code, 401)
        # The API token must not work as a bridge token either.
        with self.assertRaises(urllib.error.HTTPError):
            FakeBridge(self.live.base, "api-token-for-tests", wait=0).sync_once()

    def test_move_round_trip(self):
        bridge = self.live.bridge()
        self.assertEqual(bridge.sync_once(), [])
        status, body, _ = self.live.api("GET", "/api/state")
        self.assertTrue(body["bridge"]["online"])
        self.assertEqual([b["name"] for b in body["blinds"]], ["Kitchen", "Study"])
        self.assertIsNone(body["blinds"][0]["pos"])

        status, body, _ = self.live.api(
            "POST", "/api/move", {"blinds": ["c0ffee000001"], "position": 200}
        )
        self.assertEqual(status, 200)
        self.assertTrue(self.live.api("GET", "/api/state")[1]["blinds"][0]["busy"])

        commands = bridge.sync_once()
        self.assertEqual([(c["blind"], c["pos"]) for c in commands], [("c0ffee000001", 200)])
        self.assertEqual(bridge.sync_once(), [])  # delivers the result
        state = self.live.api("GET", "/api/state")[1]
        kitchen = state["blinds"][0]
        self.assertEqual((kitchen["pos"], kitchen["busy"], kitchen["ok"]), (200, False, True))
        self.assertTrue(any("Kitchen moved to 200" in e["message"] for e in state["log"]))

    def test_failed_move_is_reported(self):
        bridge = self.live.bridge()
        bridge.sync_once()
        self.live.api("POST", "/api/move", {"blinds": ["c0ffee000002"], "position": 100})
        bridge.fail_next = True
        bridge.sync_once()
        bridge.sync_once()
        state = self.live.api("GET", "/api/state")[1]
        study = [b for b in state["blinds"] if b["name"] == "Study"][0]
        self.assertFalse(study["ok"])
        self.assertEqual(study["err"], "connect failed")
        self.assertTrue(any("Study FAILED" in e["message"] for e in state["log"]))

    def test_move_validation(self):
        self.live.bridge().sync_once()
        for body in (
            {"blinds": ["all"], "position": 201},
            {"blinds": ["all"], "position": -1},
            {"blinds": ["all"], "position": "100"},
            {"blinds": ["all"], "position": True},
            {"blinds": ["nope"], "position": 100},
            {"blinds": [], "position": 100},
            {"blinds": "all", "position": 100},
        ):
            status, _, _ = self.live.api("POST", "/api/move", body)
            self.assertEqual(status, 400, body)

    def test_all_expands_and_newer_command_replaces_older(self):
        bridge = self.live.bridge()
        bridge.sync_once()
        self.live.api("POST", "/api/move", {"blinds": ["all"], "position": 200})
        self.live.api("POST", "/api/move", {"blinds": ["c0ffee000001"], "position": 100})
        commands = {c["blind"]: c["pos"] for c in bridge.sync_once()}
        self.assertEqual(commands, {"c0ffee000001": 100, "c0ffee000002": 200})

    def test_two_bridges_each_get_their_own_commands(self):
        up = self.live.bridge(bridge_id="upstairs", blinds={"aa01": "Bedroom"})
        down = self.live.bridge(bridge_id="downstairs", blinds={"bb01": "Kitchen", "bb02": "Den"})
        up.sync_once()
        down.sync_once()
        state = self.live.api("GET", "/api/state")[1]
        self.assertEqual([b["name"] for b in state["blinds"]], ["Bedroom", "Den", "Kitchen"])
        self.assertEqual((state["bridge"]["count"], state["bridge"]["online_count"]), (2, 2))
        self.assertEqual({b["id"]: b["blinds"] for b in state["bridges"]}, {"upstairs": 1, "downstairs": 2})

        self.live.api("POST", "/api/move", {"blinds": ["all"], "position": 200})
        self.assertEqual([c["blind"] for c in up.sync_once()], ["aa01"])
        self.assertEqual(sorted(c["blind"] for c in down.sync_once()), ["bb01", "bb02"])
        self.assertEqual(up.sync_once(), [])
        self.assertEqual(down.sync_once(), [])
        state = self.live.api("GET", "/api/state")[1]
        self.assertEqual([(b["pos"], b["busy"]) for b in state["blinds"]], [(200, False)] * 3)

        # One bridge re-reporting must not wipe the other's blinds.
        up.sync_once()
        self.assertEqual(len(self.live.api("GET", "/api/state")[1]["blinds"]), 3)

    def test_long_poll_for_one_bridge_ignores_the_others_commands(self):
        up = self.live.bridge(bridge_id="upstairs", blinds={"aa01": "Bedroom"})
        down = self.live.bridge(bridge_id="downstairs", blinds={"bb01": "Kitchen"})
        up.sync_once()
        down.sync_once()
        self.live.api("POST", "/api/move", {"blinds": ["bb01"], "position": 100})
        up.wait = 1
        started = time.monotonic()
        self.assertEqual(up.sync_once(), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual([c["blind"] for c in down.sync_once()], ["bb01"])

    def test_forget_bridge(self):
        self.live.bridge(bridge_id="old", blinds={"aa01": "Attic"}).sync_once()
        status, body, _ = self.live.api("DELETE", "/api/bridges/old")
        self.assertEqual(status, 400)  # still online
        self.live.hub.bridges["old"]["last_seen"] -= server.ONLINE_WINDOW + 5
        state = self.live.api("GET", "/api/state")[1]
        self.assertTrue(state["blinds"][0]["offline"])
        self.assertEqual(self.live.api("DELETE", "/api/bridges/old")[0], 200)
        state = self.live.api("GET", "/api/state")[1]
        self.assertEqual((state["blinds"], state["bridges"]), ([], []))
        self.assertEqual(self.live.api("DELETE", "/api/bridges/old")[0], 404)

    def test_long_poll_wakes_on_command(self):
        bridge = self.live.bridge(wait=0)
        bridge.sync_once()  # register blinds
        bridge.wait = 10
        got = {}

        def poll():
            started = time.monotonic()
            got["commands"] = bridge.sync_once()
            got["took"] = time.monotonic() - started

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.4)
        self.live.api("POST", "/api/move", {"blinds": ["all"], "position": 100})
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(got["commands"]), 2)
        self.assertLess(got["took"], 3)

    def test_long_poll_times_out_empty(self):
        bridge = self.live.bridge(wait=1)
        started = time.monotonic()
        self.assertEqual(bridge.sync_once(), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_oversized_body_rejected(self):
        status, _, _ = self.live.api("POST", "/api/move", {"pad": "x" * (server.MAX_BODY + 10)})
        self.assertEqual(status, 413)

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.live.base + "/api/move", data=b"{nope", method="POST",
            headers={"Authorization": "Bearer api-token-for-tests"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_page_is_served_with_security_headers(self):
        import base64, hashlib, re
        req = urllib.request.Request(self.live.base + "/")
        with urllib.request.urlopen(req, timeout=5) as res:
            raw = res.read()
            policy = res.headers["Content-Security-Policy"]
            self.assertEqual(res.headers["X-Frame-Options"], "DENY")
            self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(res.headers["Server"], "BlindBridge")             # no version numbers for strangers
        page = raw.decode()
        self.assertIn("<title>Blinds</title>", page)
        # Only the page's own script and style may run, named by their hashes. Nothing inline beyond them.
        for tag in ("script", "style"):
            blocks = re.findall(rf"<{tag}>(.*?)</{tag}>".encode(), raw, re.S)
            self.assertEqual(len(blocks), 1)
            digest = base64.b64encode(hashlib.sha256(blocks[0]).digest()).decode()
            self.assertIn(f"{tag}-src 'sha256-{digest}'", policy)
        for part in ("default-src 'none'", "connect-src 'self'", "base-uri 'none'", "form-action 'self'",
                     "frame-ancestors 'none'"):
            self.assertIn(part, policy)
        self.assertNotIn("unsafe", policy)
        self.assertIsNone(re.search(r'\sstyle="|\son[a-z]+="|<script[^>]+src=|javascript:', page))
        self.assertEqual(server.page_policy(b"<p>nothing</p>").count("'none'"), 5)

    def test_a_request_is_turned_away_before_its_body_is_read(self):
        import socket
        for path, extra in (("/api/admin/update", ""), ("/api/move", ""), ("/api/admin/update", "X-BB: 1\r\n")):
            with socket.create_connection(self.live.httpd.server_address, timeout=5) as sock:
                # Claims a 12 MB body and sends none of it. No sign-in, so the answer must come at once.
                sock.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n{extra}"
                             f"Content-Length: {server.MAX_ADMIN_BODY - 1}\r\n\r\n".encode())
                started = time.monotonic()
                answer = b""
                while True:                                              # until the server closes the connection
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    answer += chunk
                self.assertLess(time.monotonic() - started, 2)
                self.assertTrue(answer.startswith(b"HTTP/1.1 401"), answer[:60])
                self.assertIn(b"Connection: close", answer)

    def test_an_idle_connection_is_closed(self):
        import socket
        handler = self.live.httpd.RequestHandlerClass
        self.assertEqual(handler.timeout, 120)
        self.addCleanup(setattr, handler, "timeout", handler.timeout)
        handler.timeout = 0.3
        with socket.create_connection(self.live.httpd.server_address, timeout=5) as sock:
            sock.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\n{")   # then stalls
            started = time.monotonic()
            self.assertEqual(sock.recv(4096), b"")
            self.assertLess(time.monotonic() - started, 3)

    def test_settings_and_database_are_readable_by_their_owner_only(self):
        import os, stat
        path = Path(self.live.tmp.name) / "config.json"
        old_mask = os.umask(0)                                           # the most permissive default there is
        try:
            server.save_config(path, make_cfg())
        finally:
            os.umask(old_mask)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((Path(self.live.tmp.name) / "blindbridge.db").stat().st_mode), 0o600)

    def test_text_from_a_bridge_is_cleaned(self):
        self.assertEqual(server.clean_text("  two\nlines\x00 and \x1b[31mcolour\t", 40), "twolines and [31mcolour")
        self.assertEqual(server.clean_text("x" * 100, 8), "x" * 8)
        self.assertEqual(server.clean_text(None, 8), "None")
        token = {"Authorization": "Bearer bridge-token-for-tests"}
        self.live.call("POST", "/bridge/v1/sync", {"id": "den\n", "fw": "1\r\n2", "wait": 0, "notes": ["ok\nFAKE LINE"],
                                                   "blinds": [{"id": "a1\x00", "name": "Kit\nchen", "err": "a\nb"}]}, token)
        state = self.live.api("GET", "/api/state")[1]
        self.assertEqual((state["bridges"][0]["id"], state["bridges"][0]["fw"]), ("den", "12"))
        self.assertEqual((state["blinds"][0]["id"], state["blinds"][0]["name"], state["blinds"][0]["err"]), ("a1", "Kitchen", "ab"))
        self.assertIn("okFAKE LINE", [entry["message"] for entry in state["log"]])

    def test_schedule_crud(self):
        self.live.bridge().sync_once()
        good = {
            "name": "Morning", "kind": "time", "at": "07:30", "days": ["mon", "fri"],
            "blinds": ["all"], "position": 100,
        }
        status, body, _ = self.live.api("POST", "/api/schedules", good)
        self.assertEqual(status, 200)
        sched_id = body["id"]
        listed = self.live.api("GET", "/api/state")[1]["schedules"]
        self.assertEqual(listed[0]["days"], ["mon", "fri"])
        edited = dict(good, id=sched_id, name="Later", at="08:00", enabled=False)
        self.assertEqual(self.live.api("POST", "/api/schedules", edited)[0], 200)
        listed = self.live.api("GET", "/api/state")[1]["schedules"]
        self.assertEqual((listed[0]["name"], listed[0]["at"], listed[0]["enabled"]), ("Later", "08:00", False))
        for bad in (
            dict(good, kind="noon"),
            dict(good, at="25:00"),
            dict(good, days=[]),
            dict(good, days=["funday"]),
            dict(good, position=500),
            dict(good, name=" "),
            dict(good, id=9999),
            dict(good, offset_min=9000),
        ):
            self.assertEqual(self.live.api("POST", "/api/schedules", bad)[0], 400, bad)
        self.assertEqual(self.live.api("DELETE", f"/api/schedules/{sched_id}")[0], 200)
        self.assertEqual(self.live.api("DELETE", f"/api/schedules/{sched_id}")[0], 404)

    def test_sun_schedule_needs_location(self):
        live = LiveServer(latitude=None, longitude=None)
        try:
            status, body, _ = live.api(
                "POST", "/api/schedules",
                {"name": "Dusk", "kind": "sunset", "days": ["mon"], "blinds": ["all"], "position": 200},
            )
            self.assertEqual(status, 400)
            self.assertIn("latitude", body["error"])
        finally:
            live.close()


class ClockTests(unittest.TestCase):
    """The page is told whether the system says its clock is set from a time server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub = server.Hub(make_cfg(), Path(self.tmp.name))
        self.addCleanup(self.hub.db.close)
        self.answer = "yes\n"
        self.calls = []

        def run(argv, **kwargs):
            self.calls.append(" ".join(argv))
            if self.answer is None:
                raise FileNotFoundError("timedatectl")
            return SimpleNamespace(returncode=0, stdout=self.answer, stderr="")

        self.hub.run_command = run

    def messages(self):
        return [entry["message"] for entry in self.hub.state()["log"]]

    def test_not_known_until_asked_then_what_the_system_reports(self):
        self.assertIsNone(self.hub.state()["clock_synced"])
        self.assertIs(self.hub.check_clock(), True)
        self.assertEqual(self.calls, ["timedatectl show -p NTPSynchronized --value"])    # reads only, changes nothing
        self.assertIs(self.hub.state()["clock_synced"], True)
        self.assertEqual(self.messages(), [])                                            # nothing to say when all is well

    def test_an_unsynced_clock_is_logged_once_and_so_is_its_return(self):
        self.answer = "no\n"
        self.hub.check_clock()
        self.hub.check_clock()
        self.assertIs(self.hub.state()["clock_synced"], False)
        self.assertEqual(self.messages(), ["the clock is not synced with a time server; schedule times may be off"])
        self.answer = "yes\n"
        self.hub.check_clock()
        self.assertEqual(self.messages()[0], "the clock is synced with a time server again")
        self.assertIs(self.hub.state()["clock_synced"], True)

    def test_a_system_that_cannot_say_is_not_reported_as_wrong(self):
        for self.answer in (None, "", "maybe\n"):
            self.assertIsNone(self.hub.check_clock())
        self.assertIsNone(self.hub.state()["clock_synced"])
        self.assertEqual(self.messages(), [])


class NetFailsafeTests(unittest.TestCase):
    """The server's own way of bringing Wi-Fi back, for when the bridge is not running."""

    def setUp(self):
        sys.path.insert(0, str(Path(server.INSTALL_DIR) / "bridge"))
        import test_netwatch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.sys = test_netwatch.make_sys(root / "sys", eth_carrier="0", wifi_soft="1")
        self.folder = root / "bridge"
        self.folder.mkdir()
        self.hub = server.Hub(make_cfg(), root)
        self.addCleanup(self.hub.db.close)
        self.hub.sys_path, self.hub.bridge_dir = self.sys, self.folder
        self.calls = []

        def run(argv, **kwargs):
            self.calls.append(" ".join(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.hub.run_command = run

    def test_wifi_comes_back_after_three_looks_without_a_cable(self):
        (self.folder / "wifi_off").write_text("1")
        self.assertEqual([self.hub.net_failsafe(), self.hub.net_failsafe(), self.calls], [False, False, []])
        self.assertTrue(self.hub.net_failsafe())
        self.assertEqual(self.calls, ["rfkill unblock wlan"])
        self.assertFalse((self.folder / "wifi_off").exists())

    def test_it_keeps_out_of_the_way_otherwise(self):
        for _ in range(5):                                               # Wi-Fi is off, but this program did not do it
            self.assertFalse(self.hub.net_failsafe())
        (self.folder / "wifi_off").write_text("1")
        self.hub.net_failsafe(), self.hub.net_failsafe()
        (self.sys / "net" / "eth0" / "carrier").write_text("1\n")       # the cable is back: start counting again
        self.assertFalse(self.hub.net_failsafe())
        (self.sys / "net" / "eth0" / "carrier").write_text("0\n")
        self.assertEqual([self.hub.net_failsafe(), self.hub.net_failsafe()], [False, False])
        (self.sys / "rfkill" / "rfkill1" / "soft").write_text("0\n")    # Wi-Fi is on already
        self.assertFalse(self.hub.net_failsafe())
        self.assertEqual(self.calls, [])


class AdminHttpTests(unittest.TestCase):
    """The Settings endpoints: who may call them, and what they change."""

    def setUp(self):
        self.live = LiveServer()
        self.addCleanup(self.live.close)
        self.cfg_path = Path(self.live.tmp.name) / "config.json"
        self.live.hub.cfg_path = self.cfg_path
        status, _, headers = self.live.call("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def adm(self, method, path, body=None, xbb=True):
        headers = {"Cookie": self.cookie}
        if xbb:
            headers["X-BB"] = "1"
        return self.live.call(method, path, body, headers)

    def test_api_token_cannot_use_settings(self):
        self.assertEqual(self.live.api("GET", "/api/admin/info")[0], 401)
        self.assertEqual(self.live.api("POST", "/api/admin/location", {"timezone": "UTC"})[0], 401)
        self.assertEqual(self.live.api("POST", "/api/admin/update", {"password": PASSWORD, "zip_b64": ""})[0], 401)
        self.assertEqual(self.live.call("GET", "/api/admin/info")[0], 401)

    def test_cookie_needs_the_header_to_change_things(self):
        self.assertEqual(self.adm("POST", "/api/admin/location", {"timezone": "UTC"}, xbb=False)[0], 403)
        self.assertEqual(self.adm("GET", "/api/admin/info", xbb=False)[0], 200)

    def test_info_and_location(self):
        status, info, _ = self.adm("GET", "/api/admin/info")
        self.assertEqual((status, info["version"], info["timezone"], info["can_restart"]),
                         (200, server.VERSION, "America/New_York", False))
        good = {"timezone": "America/Chicago", "latitude": 41.88, "longitude": -87.63}
        self.assertEqual(self.adm("POST", "/api/admin/location", good)[0], 200)
        saved = json.loads(self.cfg_path.read_text())
        self.assertEqual((saved["timezone"], saved["latitude"]), ("America/Chicago", 41.88))
        self.assertEqual(self.live.hub.tz.key, "America/Chicago")
        cleared = {"timezone": "America/Chicago", "latitude": None, "longitude": None}
        self.assertEqual(self.adm("POST", "/api/admin/location", cleared)[0], 200)
        self.assertIsNone(self.live.api("GET", "/api/state")[1]["sun"])
        for bad in ({"timezone": "Mars/Olympus"}, {"timezone": "UTC", "latitude": 95, "longitude": 0},
                    {"timezone": "UTC", "latitude": 10, "longitude": None}, {"timezone": "UTC", "latitude": "x", "longitude": 1}):
            self.assertEqual(self.adm("POST", "/api/admin/location", bad)[0], 400, bad)

    def test_accent_color(self):
        self.assertEqual(self.live.api("GET", "/api/state")[1]["accent"], server.DEFAULT_ACCENT)
        self.assertEqual(self.live.api("POST", "/api/admin/appearance", {"accent": "#123456"})[0], 401)
        status, body, _ = self.adm("POST", "/api/admin/appearance", {"accent": "#1A4B8C"})
        self.assertEqual((status, body["accent"]), (200, "#1a4b8c"))
        self.assertEqual(json.loads(self.cfg_path.read_text())["accent"], "#1a4b8c")
        self.assertEqual(self.live.api("GET", "/api/state")[1]["accent"], "#1a4b8c")
        for bad in ("red", "#12345", "#12345g", "#1234567", "url(x)", 5, ["#123456"], "#123456;}"):
            self.assertEqual(self.adm("POST", "/api/admin/appearance", {"accent": bad})[0], 400, bad)
        self.assertEqual(self.live.api("GET", "/api/state")[1]["accent"], "#1a4b8c")
        self.assertEqual(self.adm("POST", "/api/admin/appearance", {"accent": None})[1]["accent"], server.DEFAULT_ACCENT)

    def test_time_format(self):
        self.assertEqual(self.live.api("GET", "/api/state")[1]["clock"], "auto")
        self.adm("POST", "/api/admin/appearance", {"accent": "#1a4b8c"})
        status, body, _ = self.adm("POST", "/api/admin/appearance", {"clock": "12"})
        self.assertEqual((status, body["clock"], body["accent"]), (200, "12", "#1a4b8c"))
        self.assertEqual(json.loads(self.cfg_path.read_text())["clock"], "12")
        state = self.live.api("GET", "/api/state")[1]
        self.assertEqual(state["clock"], "12")
        self.assertRegex(state["time"], r"^\w{3} \d\d:\d\d$")   # the API keeps one format; the page converts
        for bad in ("am", 12, None, ["12"], ""):
            self.assertEqual(self.adm("POST", "/api/admin/appearance", {"clock": bad})[0], 400, bad)
        self.assertEqual(self.adm("POST", "/api/admin/appearance", {"clock": "24", "accent": "nope"})[0], 400)
        self.assertEqual(self.live.api("GET", "/api/state")[1]["clock"], "12")
        self.assertEqual(self.adm("POST", "/api/admin/appearance", {"accent": None})[1], {"accent": server.DEFAULT_ACCENT, "clock": "12", "ok": True})
        self.live.hub.cfg["clock"] = "13"
        self.assertEqual(self.live.api("GET", "/api/state")[1]["clock"], "auto")

    def test_a_bad_accent_in_the_settings_file_falls_back(self):
        self.live.hub.cfg["accent"] = "</style><script>"
        self.assertEqual(self.live.api("GET", "/api/state")[1]["accent"], server.DEFAULT_ACCENT)

    def test_password_change_signs_out_other_sessions(self):
        _, _, headers = self.live.call("POST", "/api/login", {"password": PASSWORD})
        other = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.adm("POST", "/api/admin/password", {"current": "wrong", "new": "long enough pw"})[0], 400)
        self.assertEqual(self.adm("POST", "/api/admin/password", {"current": PASSWORD, "new": "short"})[0], 400)
        self.assertEqual(self.adm("POST", "/api/admin/password", {"current": PASSWORD, "new": "a brand new one"})[0], 200)
        self.assertEqual(self.adm("GET", "/api/admin/info")[0], 200)
        self.assertEqual(self.live.call("GET", "/api/state", headers={"Cookie": other})[0], 401)
        self.assertEqual(self.live.call("POST", "/api/login", {"password": PASSWORD})[0], 401)
        self.assertEqual(self.live.call("POST", "/api/login", {"password": "a brand new one"})[0], 200)
        self.assertTrue(server.verify_password("a brand new one", json.loads(self.cfg_path.read_text())["admin_password_hash"]))

    def test_sessions_survive_a_restart(self):
        token = self.cookie.split("=", 1)[1]
        again = server.Hub(self.live.hub.cfg, Path(self.live.tmp.name))
        self.addCleanup(again.db.close)
        self.assertTrue(again.session_valid(token))
        self.assertFalse(again.session_valid("made-up"))
        stored = again.db.execute("SELECT token_hash FROM sessions").fetchall()
        self.assertNotIn(token, [row["token_hash"] for row in stored])   # only a hash is kept

    def test_bridge_actions_report_offline_and_unknown(self):
        status, body, _ = self.adm("POST", "/api/admin/scan", {"bridge": "nope"})
        self.assertEqual((status, body["error"]), (400, "unknown bridge"))
        self.assertEqual(self.adm("POST", "/api/admin/blind", {"bridge": "x", "action": "explode"})[0], 400)
        self.assertEqual(self.adm("POST", "/api/admin/import", {"bridge": "x", "backup": "text"})[0], 400)
        status, body, _ = self.adm("POST", "/api/admin/restart", {"target": "server"})
        self.assertEqual(status, 400)
        self.assertIn("not started as a service", body["error"])

    def test_capture_endpoints_are_settings_only(self):
        body = {"action": "mode", "bridge": "den", "on": True}
        self.assertEqual(self.live.api("POST", "/api/admin/capture", body)[0], 401)     # API token is not enough
        self.assertEqual(self.live.api("GET", "/api/admin/debug?bridge=den")[0], 401)
        self.assertEqual(self.adm("POST", "/api/admin/capture", body, xbb=False)[0], 403)
        status, answer, _ = self.adm("POST", "/api/admin/capture", body)
        self.assertEqual((status, answer["error"]), (400, "unknown bridge"))
        self.assertEqual(self.adm("GET", "/api/admin/debug?bridge=ghost")[0], 400)
        self.live.bridge(bridge_id="den").sync_once()
        self.assertEqual(self.adm("POST", "/api/admin/capture", {"action": "explode", "bridge": "den"})[0], 400)

    def test_bridge_reports_its_capture_state(self):
        self.live.bridge(bridge_id="den").sync_once()
        entry = self.live.api("GET", "/api/state")[1]["bridges"][0]
        self.assertEqual((entry["capture"], entry["captures"]), (False, 0))
        self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "capture": True, "captures": 3},
                       {"Authorization": "Bearer bridge-token-for-tests"})
        entry = self.live.api("GET", "/api/state")[1]["bridges"][0]
        self.assertEqual((entry["capture"], entry["captures"]), (True, 3))
        self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "capture": "yes", "captures": -4},
                       {"Authorization": "Bearer bridge-token-for-tests"})
        entry = self.live.api("GET", "/api/state")[1]["bridges"][0]
        self.assertEqual((entry["capture"], entry["captures"]), (False, 0))

    def test_activity_lines_follow_a_rename(self):
        token = {"Authorization": "Bearer bridge-token-for-tests"}

        def report(*blinds, results=()):
            self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "results": list(results), "blinds": [
                {"id": blind_id, "name": name, "pos": 100, "ok": True, "err": "", "age": 5} for blind_id, name in blinds]}, token)

        def messages():
            return [entry["message"] for entry in self.live.api("GET", "/api/state")[1]["log"]]

        report(("a1", "Study"), ("b2", "Bedroom"))
        self.assertEqual(self.live.api("POST", "/api/move", {"blinds": ["a1"], "position": 100})[0], 200)
        sent = self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0}, token)[1]["commands"]
        report(("a1", "Study"), ("b2", "Bedroom"), results=[{"cmd": sent[0]["cmd"], "ok": True}])
        self.assertEqual(messages()[:2], ["Study moved to 100", "manual: move Study to 100"])

        report(("a1", "Bedroom"), ("b2", "Study"))                     # the two names are swapped
        self.assertEqual(messages()[:2], ["Bedroom moved to 100", "manual: move Bedroom to 100"])
        report(("b2", "Study"))                                         # the blind is removed: the name it had then
        self.assertEqual(messages()[:2], ["Study moved to 100", "manual: move Study to 100"])
        raw = [row["message"] for row in self.live.hub.db.execute("SELECT message FROM log ORDER BY id DESC LIMIT 1")]
        self.assertEqual(raw, ["\x1fa1|Study\x1f moved to 100"])
        self.assertEqual(self.live.hub.shown(raw[0], current=False), "Study moved to 100")

        report(("c3", "Odd|na\x1fme\nsecond line"))                      # a name cannot break the marker or the log
        self.assertEqual(self.live.hub.shown(self.live.hub._blind_ref("c3")), "Odd|namesecond line")
        self.assertEqual(self.live.hub.shown(self.live.hub._blind_ref("c3"), current=False), "Odd/namesecond line")
        self.assertEqual(self.live.hub.shown("plain line from before"), "plain line from before")

    def test_a_rename_is_written_to_the_activity_log(self):
        self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "blinds": [
            {"id": "a1", "name": "Study", "pos": 100, "ok": True, "err": "", "age": 5}]},
            {"Authorization": "Bearer bridge-token-for-tests"})
        self.live.hub.admin = lambda bridge_id, op, args=None, timeout=1: {"name": args["name"]}
        body = {"bridge": "den", "action": "rename", "blind": "a1", "name": "Bedroom"}
        self.assertEqual(self.adm("POST", "/api/admin/blind", body)[0], 200)
        self.assertEqual(self.live.api("GET", "/api/state")[1]["log"][0]["message"], "renamed Study to Bedroom")

    def test_network_routes_are_settings_only_and_checked(self):
        self.assertEqual(self.live.api("GET", "/api/admin/network?bridge=den")[0], 401)
        self.assertEqual(self.live.api("POST", "/api/admin/network", {"bridge": "den", "mode": "auto"})[0], 401)
        self.assertEqual(self.adm("POST", "/api/admin/network", {"bridge": "den", "mode": "auto"}, xbb=False)[0], 403)
        self.live.bridge(bridge_id="den").sync_once()
        self.assertEqual(self.adm("POST", "/api/admin/network", {"bridge": "den", "mode": "off"})[0], 400)
        asked = []
        self.live.hub.admin = lambda bridge_id, op, args=None, timeout=1: (asked.append((op, args)), {"mode": "auto", "grace": 20})[1]
        status, got, _ = self.adm("POST", "/api/admin/network", {"bridge": "den", "mode": "auto"})
        self.assertEqual((status, got["grace"], asked), (200, 20, [("wifi_mode", {"mode": "auto"})]))
        self.assertEqual(self.adm("GET", "/api/admin/network?bridge=den")[0], 200)
        self.assertEqual(asked[-1][0], "network")

    def test_bridge_reports_wifi_off(self):
        token = {"Authorization": "Bearer bridge-token-for-tests"}
        self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "wifi_off": True}, token)
        self.assertTrue(self.live.api("GET", "/api/state")[1]["bridges"][0]["wifi_off"])
        self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "wifi_off": "yes"}, token)
        self.assertFalse(self.live.api("GET", "/api/state")[1]["bridges"][0]["wifi_off"])

    def test_bridge_notes_reach_the_activity_log(self):
        self.live.bridge(bridge_id="den").sync_once()
        self.live.call("POST", "/bridge/v1/sync",
                       {"id": "den", "wait": 0, "notes": ["after the restart, Study moved to 100", 7, "", "x" * 500]},
                       {"Authorization": "Bearer bridge-token-for-tests"})
        messages = [entry["message"] for entry in self.live.api("GET", "/api/state")[1]["log"]]
        self.assertIn("after the restart, Study moved to 100", messages)
        self.assertIn("x" * 120, messages)
        self.assertNotIn("x" * 121, " ".join(messages))

    def test_battery_readings_are_checked_before_they_are_shown(self):
        def report(**fields):
            blind = {"id": "a1", "name": "Study", "pos": 198, "ok": True, "err": "", "age": 5, **fields}
            self.live.call("POST", "/bridge/v1/sync", {"id": "den", "wait": 0, "blinds": [blind]},
                           {"Authorization": "Bearer bridge-token-for-tests"})
            return self.live.api("GET", "/api/state")[1]["blinds"][0]

        got = report(battery=67, charging="solar", health_age=30)
        self.assertEqual((got["battery"], got["charging"]), (67, "solar"))
        self.assertTrue(29 <= got["health_ago"] <= 32)
        got = report(battery=255, charging="<b>", health_age=-1)
        self.assertEqual((got["battery"], got["charging"], got["health_ago"]), (None, "", None))
        got = report(battery=True, charging=None)
        self.assertEqual((got["battery"], got["charging"], got["health_ago"]), (None, "", None))
        got = report()                                                   # an older bridge sends none of it
        self.assertEqual((got["battery"], got["charging"], got["health_ago"]), (None, "", None))

    def test_debug_zip_is_made_even_when_the_bridge_is_silent_and_holds_no_secrets(self):
        import io, zipfile
        self.live.bridge(bridge_id="den").sync_once()
        real_admin = self.live.hub.admin

        def quick(bridge_id, op, args=None, timeout=1):
            return real_admin(bridge_id, op, args, timeout=0.3)             # nobody answers: a fake bridge

        self.live.hub.admin = quick
        req = urllib.request.Request(self.live.base + "/api/admin/debug?bridge=den", headers={"Cookie": self.cookie})
        with urllib.request.urlopen(req, timeout=15) as res:
            data = res.read()
            self.assertRegex(res.headers["Content-Disposition"], r'filename="blindbridge-debug-den-\d{8}-\d{4}\.zip"')
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(sorted(archive.namelist()),
                         ["bridge-log.txt", "captures.json", "info.json", "server-log.txt", "system.txt"])
        info = json.loads(archive.read("info.json"))
        self.assertEqual((info["server"], info["bridge"], info["captures_included"]), (server.VERSION, "den", 0))
        self.assertEqual(len(info["problems"]), 3)
        self.assertEqual(json.loads(archive.read("captures.json")), [])
        everything = b"".join(archive.read(n) for n in archive.namelist()).decode()
        for secret in ("bridge-token-for-tests", "api-token-for-tests", PASSWORD, "pbkdf2"):
            self.assertNotIn(secret, everything)

    def test_debug_zip_unpacks_what_the_bridge_sends(self):
        import base64, zlib
        self.live.bridge(bridge_id="den").sync_once()
        records = [{"kind": "probe", "file": "a.json", "events": [{"op": "read", "hex": "5f"}]}]

        def answer(bridge_id, op, args=None, timeout=1):
            if op == "captures":
                return {"count": 1, "total": 7, "z": base64.b64encode(zlib.compress(json.dumps(records).encode())).decode()}
            return {"text": "bluetooth status" if op == "diagnostics" else "bridge log line"}

        self.live.hub.admin = answer
        name, data = self.live.hub.debug_zip("den")
        import io, zipfile
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(json.loads(archive.read("captures.json")), records)
        self.assertEqual(archive.read("bridge-log.txt"), b"bridge log line")
        self.assertEqual(archive.read("system.txt"), b"bluetooth status")
        info = json.loads(archive.read("info.json"))
        self.assertEqual((info["captures_included"], info["captures_on_bridge"], info["problems"]), (1, 7, []))
        self.live.hub.admin = lambda *a, **k: {"z": "not base64!!", "text": ""}
        info = json.loads(zipfile.ZipFile(io.BytesIO(self.live.hub.debug_zip("den")[1])).read("info.json"))
        self.assertEqual(len(info["problems"]), 1)

    def test_logs_endpoint_answers_even_without_a_journal(self):
        status, body, _ = self.adm("GET", "/api/admin/logs?target=server")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["text"], str)
        self.assertEqual(self.adm("GET", "/api/admin/logs?target=ghost")[0], 400)

    def test_download_current_is_a_valid_update_without_secrets(self):
        import io, zipfile
        sys.path.insert(0, str(server.INSTALL_DIR))
        import updater
        self.assertEqual(self.live.api("GET", "/api/admin/download")[0], 401)       # API token is not enough
        req = urllib.request.Request(self.live.base + "/api/admin/download", headers={"Cookie": self.cookie})
        with urllib.request.urlopen(req, timeout=15) as res:
            data = res.read()
            self.assertEqual(res.headers["Content-Type"], "application/zip")
            self.assertIn(f'filename="blindbridge-{server.VERSION}.zip"', res.headers["Content-Disposition"])
        files = updater.read_update(data)                                       # passes the update checks
        self.assertEqual(files["VERSION"].decode().strip(), server.VERSION)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("blindbridge/server/ui.html", names)
        self.assertFalse([n for n in names if n.endswith(("config.json", "blinds.json", "bridge.json", ".db"))
                          or "/venv/" in n or "__pycache__" in n])

    def test_download_never_packs_secrets_even_if_present(self):
        sys.path.insert(0, str(server.INSTALL_DIR))
        import updater
        root = Path(self.live.tmp.name) / "inst"
        for rel, text in {"VERSION": "1.0.0", "server/server.py": "x=1", "bridge/bridge.py": "y=1",
                          "server/config.json": "SECRET", "bridge/blinds.json": "KEYS", "bridge/bridge.json": "TOKEN",
                          "server/blindbridge.db": "DB", "venv/bin/python": "ENV", "server/__pycache__/a.pyc": "C",
                          "notes.txt": "stray"}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        files = updater.read_update(updater.build_zip(root))
        self.assertEqual(sorted(files), ["VERSION", "bridge/bridge.py", "server/server.py"])

    def test_power_buttons(self):
        calls = []

        class Done:
            def __init__(self, out): self.returncode, self.stdout, self.stderr = 0, out, ""

        allowed = {"value": False}

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "busctl":
                return Done('s "yes"\n' if allowed["value"] else 's "challenge"\n')
            return Done("")

        self.live.hub.run_command = fake_run
        self.live.hub._can_power = None
        self.assertFalse(self.adm("GET", "/api/admin/info")[1]["can_power"])
        status, body, _ = self.adm("POST", "/api/admin/power", {"action": "poweroff"})
        self.assertEqual(status, 400)
        self.assertIn("deploy/install.sh", body["error"])
        self.assertEqual(self.adm("POST", "/api/admin/power", {"action": "format-disk"})[0], 400)
        self.assertEqual(self.live.api("POST", "/api/admin/power", {"action": "reboot"})[0], 401)
        self.assertFalse([c for c in calls if c[-1] in ("poweroff", "reboot")])

        allowed["value"] = True
        # The permission was added after the first look: info must notice without a restart.
        self.assertTrue(self.adm("GET", "/api/admin/info")[1]["can_power"])
        self.assertEqual(self.adm("POST", "/api/admin/power", {"action": "reboot"})[0], 200)
        time.sleep(1.4)
        self.assertEqual(calls[-1], ["/usr/bin/systemctl", "reboot"])
        self.assertEqual(self.adm("POST", "/api/admin/power", {"action": "poweroff"})[0], 200)
        time.sleep(1.4)
        self.assertEqual(calls[-1], ["/usr/bin/systemctl", "poweroff"])
        self.assertTrue(any("shutting down the Pi" in e["message"] for e in self.live.api("GET", "/api/state")[1]["log"]))

    def test_update_flow(self):
        import base64, io, zipfile
        target = Path(self.live.tmp.name) / "install"
        (target / "server").mkdir(parents=True)
        (target / "server" / "config.json").write_text("KEEP")
        (target / "VERSION").write_text("0.0.1")
        old = server.INSTALL_DIR
        server.INSTALL_DIR = target
        self.addCleanup(setattr, server, "INSTALL_DIR", old)
        restarted = []
        self.live.hub.restart_hook = lambda: restarted.append(True)

        def make(files):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, text in files.items():
                    archive.writestr(name, text)
            return base64.b64encode(buffer.getvalue()).decode()

        good = {"blindbridge/server/server.py": "x = 1\n", "blindbridge/bridge/bridge.py": "y = 2\n",
                "blindbridge/VERSION": "7.7.7\n", "blindbridge/updater.py": "z = 3\n"}
        status, body, _ = self.adm("POST", "/api/admin/update", {"password": "wrong", "zip_b64": make(good)})
        self.assertEqual((status, body["error"]), (400, "password is wrong"))
        evil = dict(good, **{"blindbridge/server/config.json": "{}"})
        status, body, _ = self.adm("POST", "/api/admin/update", {"password": PASSWORD, "zip_b64": make(evil)})
        self.assertEqual(status, 400)
        self.assertIn("settings or data file", body["error"])
        status, body, _ = self.adm("POST", "/api/admin/update", {"password": PASSWORD, "zip_b64": "!!!"})
        self.assertEqual(status, 400)
        self.assertEqual((restarted, (target / "VERSION").read_text()), ([], "0.0.1"))

        status, body, _ = self.adm("POST", "/api/admin/update", {"password": PASSWORD, "zip_b64": make(good)})
        self.assertEqual(status, 200)
        self.assertEqual((body["from"], body["to"], body["changed"], body["restarting"], body["bridges"]),
                         ("0.0.1", "7.7.7", 4, True, {}))
        self.assertEqual(restarted, [True])
        self.assertEqual((target / "server" / "config.json").read_text(), "KEEP")
        self.assertEqual((target / "server" / "server.py").read_text(), "x = 1\n")

    def test_wrong_update_passwords_are_throttled(self):
        for _ in range(4):   # the failed login in other tests is not shared; setUp logged in once successfully
            self.adm("POST", "/api/admin/update", {"password": "wrong", "zip_b64": ""})
        self.adm("POST", "/api/admin/update", {"password": "wrong", "zip_b64": ""})
        status, body, _ = self.adm("POST", "/api/admin/update", {"password": PASSWORD, "zip_b64": ""})
        self.assertEqual(status, 400)
        self.assertIn("too many", body["error"])


class SchedulerTests(unittest.TestCase):
    """Drive the hub directly with a fake clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tz = server.ZoneInfo("America/New_York")
        # Friday 13 Nov 2026, 07:00 local
        self.clock = Clock(dt.datetime(2026, 11, 13, 7, 0, tzinfo=self.tz).timestamp())
        self.hub = server.Hub(make_cfg(), Path(self.tmp.name), self.clock)
        self.addCleanup(self.hub.db.close)
        self.hub.sync({"blinds": [{"id": "a1", "name": "Kitchen", "pos": 100, "age": 5}]}, 0)

    def at(self, hour, minute, day=13):
        self.clock.now = dt.datetime(2026, 11, day, hour, minute, tzinfo=self.tz).timestamp()

    def add(self, **kw):
        base = {"name": "S", "kind": "time", "at": "07:30", "days": server.DAY_NAMES,
                "blinds": ["all"], "position": 200}
        base.update(kw)
        return self.hub.save_schedule(base)

    def test_fires_once_at_its_time(self):
        self.add()
        self.at(7, 29)
        self.assertEqual(self.hub.run_schedules(), 0)
        self.at(7, 30)
        self.assertEqual(self.hub.run_schedules(), 1)
        self.assertEqual(self.hub.run_schedules(), 0)
        self.assertEqual([(c["blind"], c["pos"]) for c in self.hub.queue], [("a1", 200)])
        self.at(7, 30, day=14)
        self.assertEqual(self.hub.run_schedules(), 1)

    def test_state_says_when_each_schedule_runs_next(self):
        self.add(name="Morning")                      # 07:30 daily
        self.add(name="Weekend", at="09:00", days=["sat", "sun"])
        self.add(name="Off", enabled=False)
        def coming():
            return {s["name"]: s["next"] for s in self.hub.state()["schedules"]}
        self.assertEqual(coming(), {"Morning": "07:30 today", "Weekend": "09:00 tomorrow", "Off": None})
        self.at(7, 30)
        self.assertEqual(coming()["Morning"], "07:30 tomorrow")
        self.at(12, 0, day=15)  # Sunday, after both
        self.assertEqual((coming()["Morning"], coming()["Weekend"]), ("07:30 tomorrow", "09:00 Sat"))
        soonest = min((s for s in self.hub.state()["schedules"] if s["next_in"] is not None), key=lambda s: s["next_in"])
        self.assertEqual((soonest["name"], soonest["next_in"]), ("Morning", 19 * 3600 + 1800))
        self.add(name="Sun", kind="sunset")
        self.assertRegex(coming()["Sun"], r"^1[67]:\d\d today$")
        self.hub.cfg.update(latitude=None, longitude=None)
        self.assertIsNone(coming()["Sun"])

    def test_does_not_fire_long_after(self):
        self.add()
        self.at(7, 41)
        self.assertEqual(self.hub.run_schedules(), 0)

    def test_respects_days_and_enabled(self):
        self.add(days=["sat"])
        self.add(enabled=False)
        self.at(7, 31)
        self.assertEqual(self.hub.run_schedules(), 0)
        self.at(7, 31, day=14)  # Saturday
        self.assertEqual(self.hub.run_schedules(), 1)

    def test_sunset_with_offset(self):
        self.add(kind="sunset", offset_min=-30)
        sunset = self.hub._local_sun(dt.date(2026, 11, 13), 40.0, -75.0)[1]
        fire = sunset - dt.timedelta(minutes=30)
        self.clock.now = (fire - dt.timedelta(minutes=1)).timestamp()
        self.assertEqual(self.hub.run_schedules(), 0)
        self.clock.now = (fire + dt.timedelta(seconds=30)).timestamp()
        self.assertEqual(self.hub.run_schedules(), 1)

    def test_missing_blind_is_skipped_not_crashed(self):
        self.add(blinds=["gone"])
        self.at(7, 30)
        self.assertEqual(self.hub.run_schedules(), 0)
        self.assertEqual(self.hub.queue, [])

    def test_commands_expire_when_bridge_is_away(self):
        self.hub.enqueue(["a1"], 100, "manual")
        self.clock.now += server.CMD_TTL + 1
        self.hub.run_schedules()
        self.assertEqual(self.hub.queue, [])
        self.assertFalse(self.hub.state()["blinds"][0]["busy"])

    def test_inflight_without_result_clears(self):
        self.hub.enqueue(["a1"], 100, "manual")
        self.hub.sync({}, 0)
        self.assertTrue(self.hub.state()["blinds"][0]["busy"])
        self.clock.now += server.INFLIGHT_TTL + 1
        self.assertFalse(self.hub.state()["blinds"][0]["busy"])

    def test_report_without_blinds_keeps_known_blinds(self):
        self.hub.sync({"fw": "x"}, 0)
        self.assertEqual(list(self.hub.blinds), ["a1"])

    def test_blinds_survive_restart(self):
        self.hub.db.close()
        again = server.Hub(make_cfg(), Path(self.tmp.name), self.clock)
        self.addCleanup(again.db.close)
        self.assertEqual(again.blinds["a1"]["name"], "Kitchen")
        state = again.state()
        self.assertEqual(state["bridges"][0]["id"], "bridge")
        self.assertEqual(state["bridges"][0]["seen_ago"], None)
        self.assertTrue(state["blinds"][0]["offline"])

    def test_bridge_goes_offline(self):
        self.assertTrue(self.hub.state()["bridge"]["online"])
        self.clock.now += server.ONLINE_WINDOW + 1
        self.assertFalse(self.hub.state()["bridge"]["online"])


if __name__ == "__main__":
    unittest.main()
