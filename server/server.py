#!/usr/bin/env python3
"""BlindBridge server.

A small self-hosted replacement for the retired MySmartBlinds cloud. The
bridge (a Raspberry Pi running bridge/bridge.py, or anything else that speaks
the sync protocol in docs/PROTOCOL.md) keeps one outbound long-poll connection
open to this server, so it works from behind a home router with no port
forwarding. More than one bridge can connect; each owns the blinds it reports.
This server provides the web page, the JSON API, and the schedules.

Python 3.10+ standard library only. No pip installs.

    python3 server.py init      # create config.json (asks a few questions)
    python3 server.py run       # start the server
"""

from __future__ import annotations

import argparse
import datetime as dt
import base64
import getpass
import hashlib
import hmac
import io
import json
import math
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
INSTALL_DIR = HERE.parent
if str(INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(INSTALL_DIR))
try:                                        # shared with the bridge, and kept in its folder
    sys.path.append(str(INSTALL_DIR / "bridge"))
    import netwatch
except ImportError:
    netwatch = None

POS_MIN, POS_MAX = 0, 200
MAX_BODY = 64 * 1024
MAX_SYNC_BODY = 1024 * 1024        # bridge reports can carry scan results and logs
MAX_ADMIN_BODY = 12 * 1024 * 1024  # an update zip, base64 encoded
BLIND_REF = re.compile(r"\x1f([^|\x1f]*)\|([^\x1f]*)\x1f")   # a blind named in an activity line: id, name then
MAX_CAPTURE_BYTES = 24 * 1024 * 1024  # captures from a bridge, once unpacked
ADMIN_WAIT = 75          # how long a Settings action waits for the bridge to answer
MAX_WAIT = 25            # seconds a bridge sync may be held open
ONLINE_WINDOW = 45       # bridge counts as online if seen this recently
CMD_TTL = 120            # manual commands expire if the bridge is away
SCHEDULE_TTL = 600       # scheduled commands get a longer grace period
INFLIGHT_TTL = 300       # give up waiting for a result after this long; two slow tries take about three minutes
SESSION_DAYS = 30
PBKDF2_ROUNDS = 600_000


# --------------------------------------------------------------------------
# Passwords and tokens
# --------------------------------------------------------------------------

def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def clean_text(value, limit: int) -> str:
    """Text from outside as one printable line of at most `limit` characters."""
    return "".join(ch for ch in str(value) if ch.isprintable()).strip()[:limit]


def page_policy(page: bytes) -> str:
    """A Content-Security-Policy that allows the page's own script and style blocks and nothing else."""
    def hashes(tag: bytes) -> str:
        blocks = re.findall(rb"<" + tag + rb">(.*?)</" + tag + rb">", page, re.S)
        return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode() + "'"
                        for block in blocks) or "'none'"
    return (f"default-src 'none'; script-src {hashes(b'script')}; style-src {hashes(b'style')}; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def token_equal(given: str, expected: str) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------
# Sunrise / sunset (standard sunrise equation, good to a few minutes)
# --------------------------------------------------------------------------

def sun_times(day: dt.date, lat: float, lon: float):
    """Return (sunrise, sunset) as UTC datetimes, or None in polar day/night.

    lon is degrees east (so the Americas are negative).
    """
    n = day.toordinal() - dt.date(2000, 1, 1).toordinal()
    j_star = n + 0.0008 - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    sin_dec = math.sin(lam) * math.sin(math.radians(23.4397))
    cos_dec = math.cos(math.asin(sin_dec))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * sin_dec) / (
        math.cos(phi) * cos_dec
    )
    if cos_w < -1 or cos_w > 1:
        return None
    w = math.degrees(math.acos(cos_w))
    epoch = dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc)

    def to_dt(julian: float) -> dt.datetime:
        return epoch + dt.timedelta(days=julian - 2451545.0)

    return to_dt(j_transit - w / 360.0), to_dt(j_transit + w / 360.0)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_ACCENT = "#2f5d50"
ACCENT_RE = re.compile(r"#[0-9a-f]{6}")
CLOCKS = ("auto", "12", "24")   # how the page writes times: as the device does, or always one way

DEFAULT_CONFIG = {
    "listen": "127.0.0.1",
    "port": 8787,
    "timezone": "UTC",
    "latitude": None,
    "longitude": None,
    "trust_proxy": False,
    "accent": DEFAULT_ACCENT,
    "clock": "auto",
    "admin_password_hash": "",
    "bridge_token": "",
    "api_token": "",
    "database": "blindbridge.db",
}


def system_timezone() -> str:
    """The zone this machine is set to, as a name like Europe/Rome. UTC if it cannot be told."""
    try:
        name = Path("/etc/timezone").read_text(encoding="ascii").strip()
        ZoneInfo(name)
        return name
    except Exception:
        pass
    try:
        target = os.readlink("/etc/localtime")
        name = target.split("zoneinfo/", 1)[1]
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    for key in ("admin_password_hash", "bridge_token"):
        if not cfg.get(key):
            raise SystemExit(f"{path}: '{key}' is empty. Run: python3 server.py init")
    ZoneInfo(cfg["timezone"])  # fail early on a bad zone name
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# --------------------------------------------------------------------------
# Hub: all live state, guarded by one lock
# --------------------------------------------------------------------------

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ValidationError(Exception):
    pass


class Hub:
    def __init__(self, cfg: dict, base_dir: Path, clock=time.time, cfg_path: Path | None = None):
        self.cfg = cfg
        self.cfg_path = cfg_path          # where settings changes are saved; None keeps them in memory
        self.clock = clock
        self.started = clock()
        self.admin_results: dict[str, dict] = {}
        self.restart_hook = None          # set by cmd_run; called to restart this process
        self.run_command = subprocess.run  # replaced in tests
        self._can_power = None
        self.clock_synced = None            # does the system say its clock is set from a time server? None: not known
        self._no_cable = 0
        self.bridge_dir = None              # where this machine's bridge keeps its files; None: next to the server
        self.sys_path = netwatch.SYS if netwatch is not None else None
        self.tz = ZoneInfo(cfg["timezone"])
        self.cond = threading.Condition()
        self.blinds: dict[str, dict] = {}
        self.bridges: dict[str, dict] = {}
        self.queue: list[dict] = []
        self.inflight: dict[str, dict] = {}
        self.login_failures: dict[str, list[float]] = {}
        db_path = Path(cfg["database"])
        if not db_path.is_absolute():
            db_path = base_dir / db_path
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        self._migrate()
        self._load_blinds()

    # ---- storage ---------------------------------------------------------

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                days TEXT NOT NULL,
                kind TEXT NOT NULL,
                at TEXT NOT NULL DEFAULT '',
                offset_min INTEGER NOT NULL DEFAULT 0,
                blinds TEXT NOT NULL,
                position INTEGER NOT NULL,
                last_fired TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            """
        )
        self.db.commit()

    def _load_blinds(self) -> None:
        row = self.db.execute("SELECT v FROM kv WHERE k='blinds'").fetchone()
        if row:
            try:
                self.blinds = json.loads(row["v"])
            except ValueError:
                self.blinds = {}
        self.blinds = {k: v for k, v in self.blinds.items() if v.get("bridge")}
        for info in self.blinds.values():
            self._bridge(info["bridge"])  # known from before the restart, not yet seen

    def _save_blinds(self) -> None:
        self.db.execute(
            "INSERT INTO kv(k, v) VALUES('blinds', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (json.dumps(self.blinds),),
        )
        self.db.commit()

    def log(self, message: str) -> None:
        with self.cond:
            self.db.execute("INSERT INTO log(ts, message) VALUES(?, ?)", (self.clock(), message))
            self.db.execute(
                "DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - 500"
            )
            self.db.commit()
            line = self.shown(message, current=False)
        print(time.strftime("%Y-%m-%d %H:%M:%S"), line, flush=True)

    # ---- commands --------------------------------------------------------

    def _blind_name(self, blind_id: str) -> str:
        return self.blinds.get(blind_id, {}).get("name") or blind_id

    def _blind_ref(self, blind_id: str) -> str:
        """A blind's name for an activity line, written so that it follows a later rename.

        The line stores the blind's id next to the name it had at the time.
        When the line is shown, the name the blind has now is used; if the
        blind is gone, the stored name.
        """
        name = self._blind_name(blind_id).replace("\x1f", "").replace("|", "/")
        return f"\x1f{blind_id}|{name}\x1f"

    def shown(self, message: str, current: bool = True) -> str:
        """An activity line as text, with each blind under its present name (or the name it had then)."""
        def name(match) -> str:
            blind = self.blinds.get(match.group(1)) if current else None
            return blind["name"] if blind else match.group(2)
        return BLIND_REF.sub(name, message)

    def enqueue(self, blind_ids, position, source: str, ttl: int = CMD_TTL) -> list[str]:
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValidationError("position must be a whole number")
        if not POS_MIN <= position <= POS_MAX:
            raise ValidationError(f"position must be {POS_MIN} to {POS_MAX}")
        if not isinstance(blind_ids, list) or not blind_ids:
            raise ValidationError("blinds must be a non-empty list")
        with self.cond:
            if "all" in blind_ids:
                targets = sorted(self.blinds)
            else:
                targets = []
                for blind_id in blind_ids:
                    if blind_id not in self.blinds:
                        raise ValidationError(f"unknown blind: {blind_id}")
                    if blind_id not in targets:
                        targets.append(blind_id)
            if not targets:
                raise ValidationError("no blinds are known yet")
            now = self.clock()
            ids = []
            for blind_id in targets:
                # A newer command for the same blind replaces one still waiting.
                self.queue = [c for c in self.queue if c.get("blind") != blind_id]
                cmd = {
                    "cmd": secrets.token_hex(6),
                    "blind": blind_id,
                    "pos": position,
                    "created": now,
                    "expires": now + ttl,
                    "source": source,
                }
                self.queue.append(cmd)
                ids.append(cmd["cmd"])
            names = ", ".join(self._blind_ref(b) for b in targets)
            self.log(f"{source}: move {names} to {position}")
            self.cond.notify_all()
            return ids

    def _expire(self, now: float) -> None:
        for cmd in [c for c in self.queue if c["expires"] <= now]:
            self.queue.remove(cmd)
            if "blind" in cmd:
                self.log(f"dropped move for {self._blind_ref(cmd['blind'])}: bridge offline")
        for key in [k for k, c in self.inflight.items() if c["sent"] + INFLIGHT_TTL <= now]:
            cmd = self.inflight.pop(key)
            if "blind" in cmd:
                self.log(f"no result from bridge for {self._blind_ref(cmd['blind'])}")

    def _bridge(self, bridge_id: str) -> dict:
        return self.bridges.setdefault(
            bridge_id, {"last_seen": 0.0, "fw": "", "rssi": None, "generation": 0,
                        "capture": False, "captures": 0, "wifi_off": False}
        )

    def _online(self, bridge_id, now: float) -> bool:
        bridge = self.bridges.get(bridge_id)
        return bool(bridge) and now - bridge["last_seen"] <= ONLINE_WINDOW

    def _waiting_for(self, bridge_id: str) -> list[dict]:
        """Queued commands for this bridge: moves for its blinds, and Settings actions."""
        return [
            c for c in self.queue
            if c.get("bridge") == bridge_id
            or ("blind" in c and self.blinds.get(c["blind"], {}).get("bridge") == bridge_id)
        ]

    def sync(self, payload: dict, wait: float) -> dict:
        """One bridge long-poll: record its report, hand back its waiting commands."""
        bridge_id = clean_text(payload.get("id") or "", 32) or "bridge"
        with self.cond:
            now = self.clock()
            was_online = self._online(bridge_id, now)
            bridge = self._bridge(bridge_id)
            bridge.update(
                last_seen=now,
                fw=clean_text(payload.get("fw", ""), 32),
                rssi=payload.get("rssi") if isinstance(payload.get("rssi"), int) else None,
                capture=payload.get("capture") is True,
                wifi_off=payload.get("wifi_off") is True,
                captures=payload["captures"] if type(payload.get("captures")) is int and payload["captures"] >= 0 else 0,
            )
            if not was_online:
                self.log(f"bridge '{bridge_id}' online")
            self._apply_report(bridge_id, payload, now)
            bridge["generation"] += 1
            generation = bridge["generation"]
            self.cond.notify_all()  # an older sync from this bridge gives way to this one
            deadline = time.monotonic() + max(0.0, min(float(wait), MAX_WAIT))
            while True:
                self._expire(self.clock())
                if generation != bridge["generation"] or self._waiting_for(bridge_id):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=min(remaining, 5.0))
                bridge["last_seen"] = self.clock()
            commands = []
            if generation == bridge["generation"]:
                now = self.clock()
                for cmd in self._waiting_for(bridge_id):
                    self.queue.remove(cmd)
                    cmd["sent"] = now
                    self.inflight[cmd["cmd"]] = cmd
                    if "op" in cmd:
                        commands.append({"cmd": cmd["cmd"], "op": cmd["op"], "args": cmd["args"]})
                    else:
                        commands.append({"cmd": cmd["cmd"], "blind": cmd["blind"], "pos": cmd["pos"]})
            bridge["last_seen"] = self.clock()
            return {"commands": commands, "now": int(self.clock())}

    def _apply_report(self, bridge_id: str, payload: dict, now: float) -> None:
        reported = payload.get("blinds")
        if isinstance(reported, list):
            fresh: dict[str, dict] = {}
            for item in reported[:32]:
                if not isinstance(item, dict):
                    continue
                blind_id = clean_text(item.get("id", ""), 32)
                if not blind_id or blind_id == "all":
                    continue
                pos = item.get("pos")
                age = item.get("age")
                battery = item.get("battery")
                health_age = item.get("health_age")
                fresh[blind_id] = {
                    "battery": battery if type(battery) is int and 0 <= battery <= 100 else None,
                    "charging": item.get("charging") if item.get("charging") in ("solar", "usb") else "",
                    "health": now - health_age if isinstance(health_age, (int, float)) and health_age >= 0 else None,
                    "name": clean_text(item.get("name", ""), 48) or blind_id,
                    "bridge": bridge_id,
                    "pos": pos if isinstance(pos, int) and POS_MIN <= pos <= POS_MAX else None,
                    "ok": bool(item.get("ok", True)),
                    "err": clean_text(item.get("err", ""), 80),
                    "rssi": item.get("rssi") if isinstance(item.get("rssi"), int) else None,
                    "moved": now - age if isinstance(age, (int, float)) and age >= 0 else None,
                }

            def shape(blinds):
                return {key: (value["name"], value["bridge"]) for key, value in blinds.items()}

            # This bridge's list replaces what it reported before. Other bridges'
            # blinds are left alone. A blind two bridges both report goes to
            # whichever reported last, so give each blind to one bridge only.
            merged = {k: v for k, v in self.blinds.items() if v["bridge"] != bridge_id}
            merged.update(fresh)
            changed = shape(merged) != shape(self.blinds)
            self.blinds = merged
            if changed:
                self._save_blinds()
        notes = payload.get("notes")
        if isinstance(notes, list):
            for note in notes[:8]:
                if isinstance(note, str) and clean_text(note, 120):
                    self.log(clean_text(note, 120))
        results = payload.get("results")
        if isinstance(results, list):
            for res in results[:64]:
                if not isinstance(res, dict):
                    continue
                cmd = self.inflight.pop(str(res.get("cmd", "")), None)
                if cmd is None:
                    continue
                if "op" in cmd:
                    data = res.get("data")
                    self.admin_results[cmd["cmd"]] = {
                        "ok": bool(res.get("ok")),
                        "err": clean_text(res.get("err", ""), 300),
                        "data": data if isinstance(data, dict) else {},
                    }
                    continue
                name = self._blind_ref(cmd["blind"])
                if res.get("ok"):
                    self.log(f"{name} moved to {cmd['pos']}")
                else:
                    self.log(f"{name} FAILED: {clean_text(res.get('err') or 'unknown error', 80)}")
            self.cond.notify_all()

    def forget_bridge(self, bridge_id: str) -> bool:
        """Drop a bridge that is gone for good, along with its blinds."""
        with self.cond:
            if bridge_id not in self.bridges:
                return False
            if self._online(bridge_id, self.clock()):
                raise ValidationError("that bridge is online; unplug it first")
            del self.bridges[bridge_id]
            gone = {k for k, v in self.blinds.items() if v["bridge"] == bridge_id}
            self.blinds = {k: v for k, v in self.blinds.items() if k not in gone}
            self.queue = [c for c in self.queue if c.get("blind") not in gone and c.get("bridge") != bridge_id]
            self._save_blinds()
            self.log(f"bridge '{bridge_id}' forgotten")
            return True

    # ---- schedules -------------------------------------------------------

    def list_schedules(self) -> list[dict]:
        with self.cond:
            rows = self.db.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "enabled": bool(row["enabled"]),
                    "days": json.loads(row["days"]),
                    "kind": row["kind"],
                    "at": row["at"],
                    "offset_min": row["offset_min"],
                    "blinds": json.loads(row["blinds"]),
                    "position": row["position"],
                    "last_fired": row["last_fired"],
                }
            )
        return out

    def _clean_schedule(self, data: dict) -> dict:
        name = str(data.get("name", "")).strip()[:60]
        if not name:
            raise ValidationError("name is required")
        kind = data.get("kind")
        if kind not in ("time", "sunrise", "sunset"):
            raise ValidationError("kind must be time, sunrise or sunset")
        at = str(data.get("at", ""))
        if kind == "time":
            try:
                parsed = dt.time.fromisoformat(at)
            except ValueError:
                raise ValidationError("at must look like 07:30") from None
            at = parsed.strftime("%H:%M")
        else:
            at = ""
            if self.cfg.get("latitude") is None or self.cfg.get("longitude") is None:
                raise ValidationError("set latitude and longitude in config.json first")
        offset = data.get("offset_min", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 360:
            raise ValidationError("offset_min must be between -360 and 360")
        days = data.get("days")
        if not isinstance(days, list) or not days or any(d not in DAY_NAMES for d in days):
            raise ValidationError("days must be a list like ['mon','tue']")
        days = [d for d in DAY_NAMES if d in days]
        blinds = data.get("blinds")
        if not isinstance(blinds, list) or not blinds or not all(isinstance(b, str) for b in blinds):
            raise ValidationError("blinds must be a list of ids or ['all']")
        position = data.get("position")
        if isinstance(position, bool) or not isinstance(position, int) or not POS_MIN <= position <= POS_MAX:
            raise ValidationError(f"position must be {POS_MIN} to {POS_MAX}")
        return {
            "name": name,
            "enabled": 1 if data.get("enabled", True) else 0,
            "days": json.dumps(days),
            "kind": kind,
            "at": at,
            "offset_min": offset,
            "blinds": json.dumps(blinds[:32]),
            "position": position,
        }

    def save_schedule(self, data: dict) -> int:
        clean = self._clean_schedule(data)
        with self.cond:
            sched_id = data.get("id")
            if sched_id is None:
                cur = self.db.execute(
                    "INSERT INTO schedules(name, enabled, days, kind, at, offset_min, blinds, position)"
                    " VALUES(:name, :enabled, :days, :kind, :at, :offset_min, :blinds, :position)",
                    clean,
                )
                sched_id = cur.lastrowid
            else:
                if isinstance(sched_id, bool) or not isinstance(sched_id, int):
                    raise ValidationError("id must be a number")
                clean["id"] = sched_id
                cur = self.db.execute(
                    "UPDATE schedules SET name=:name, enabled=:enabled, days=:days, kind=:kind,"
                    " at=:at, offset_min=:offset_min, blinds=:blinds, position=:position,"
                    " last_fired='' WHERE id=:id",
                    clean,
                )
                if cur.rowcount == 0:
                    raise ValidationError("schedule not found")
            self.db.commit()
            return sched_id

    def delete_schedule(self, sched_id: int) -> bool:
        with self.cond:
            cur = self.db.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
            self.db.commit()
            return cur.rowcount > 0

    def fire_time(self, sched: dict, day: dt.date):
        """Local datetime this schedule fires on the given day, or None."""
        if DAY_NAMES[day.weekday()] not in sched["days"]:
            return None
        if sched["kind"] == "time":
            base = dt.datetime.combine(day, dt.time.fromisoformat(sched["at"]), tzinfo=self.tz)
        else:
            lat, lon = self.cfg.get("latitude"), self.cfg.get("longitude")
            if lat is None or lon is None:
                return None
            times = self._local_sun(day, lat, lon)
            if times is None:
                return None
            base = times[0 if sched["kind"] == "sunrise" else 1]
        return base + dt.timedelta(minutes=sched["offset_min"])

    def _local_sun(self, day: dt.date, lat: float, lon: float):
        """Sunrise and sunset whose local calendar date is `day`."""
        rise = sett = None
        for delta in (-1, 0, 1):
            times = sun_times(day + dt.timedelta(days=delta), lat, lon)
            if times is None:
                continue
            local_rise, local_set = (t.astimezone(self.tz) for t in times)
            if local_rise.date() == day:
                rise = local_rise
            if local_set.date() == day:
                sett = local_set
        if rise is None or sett is None:
            return None
        return rise, sett

    def run_schedules(self) -> int:
        """Fire anything due. Called about every 20 seconds. Returns count fired."""
        now = dt.datetime.fromtimestamp(self.clock(), tz=self.tz)
        today = now.date()
        fired = 0
        for sched in self.list_schedules():
            if not sched["enabled"]:
                continue
            when = self.fire_time(sched, today)
            if when is None:
                continue
            stamp = today.isoformat()
            if sched["last_fired"] == stamp:
                continue
            late = (now - when).total_seconds()
            if late < 0 or late > SCHEDULE_TTL:
                continue
            with self.cond:
                self.db.execute(
                    "UPDATE schedules SET last_fired=? WHERE id=?", (stamp, sched["id"])
                )
                self.db.commit()
            try:
                with self.cond:
                    known = [b for b in sched["blinds"] if b == "all" or b in self.blinds]
                if not known:
                    raise ValidationError("none of its blinds exist any more")
                self.enqueue(known, sched["position"], f"schedule '{sched['name']}'", SCHEDULE_TTL)
                fired += 1
            except ValidationError as exc:
                self.log(f"schedule '{sched['name']}' skipped: {exc}")
        with self.cond:
            self._expire(self.clock())
        return fired

    # ---- state for the web page ------------------------------------------

    def state(self) -> dict:
        with self.cond:
            now = self.clock()
            self._expire(now)
            pending = {c["blind"] for c in self.queue if "blind" in c} | {
                c["blind"] for c in self.inflight.values() if "blind" in c
            }
            blinds = []
            for blind_id in sorted(self.blinds, key=lambda b: self.blinds[b]["name"].lower()):
                info = self.blinds[blind_id]
                blinds.append(
                    {
                        "id": blind_id,
                        "name": info["name"],
                        "bridge": info["bridge"],
                        "offline": not self._online(info["bridge"], now),
                        "pos": info["pos"],
                        "ok": info["ok"],
                        "err": info["err"],
                        "rssi": info["rssi"],
                        "moved_ago": int(now - info["moved"]) if info.get("moved") else None,
                        "battery": info.get("battery"),
                        "charging": info.get("charging", ""),
                        "health_ago": int(now - info["health"]) if info.get("health") else None,
                        "busy": blind_id in pending,
                    }
                )
            bridges = []
            for bridge_id in sorted(self.bridges):
                info = self.bridges[bridge_id]
                seen = info["last_seen"]
                bridges.append(
                    {
                        "id": bridge_id,
                        "online": self._online(bridge_id, now),
                        "seen_ago": int(now - seen) if seen else None,
                        "fw": info["fw"],
                        "rssi": info["rssi"],
                        "blinds": sum(1 for b in self.blinds.values() if b["bridge"] == bridge_id),
                        "capture": bool(info.get("capture")),
                        "captures": info.get("captures", 0),
                        "wifi_off": bool(info.get("wifi_off")),
                    }
                )
            seen_agos = [b["seen_ago"] for b in bridges if b["seen_ago"] is not None]
            bridge = {
                "online": any(b["online"] for b in bridges),
                "count": len(bridges),
                "online_count": sum(1 for b in bridges if b["online"]),
                "seen_ago": min(seen_agos) if seen_agos else None,
                "fw": next((b["fw"] for b in bridges if b["online"] and b["fw"]), ""),
            }
            rows = self.db.execute(
                "SELECT ts, message FROM log ORDER BY id DESC LIMIT 40"
            ).fetchall()
            log = [
                {
                    "at": dt.datetime.fromtimestamp(r["ts"], tz=self.tz).strftime("%b %d %H:%M"),
                    "message": self.shown(r["message"]),
                }
                for r in rows
            ]
        local_now = dt.datetime.fromtimestamp(now, tz=self.tz)
        sun = None
        lat, lon = self.cfg.get("latitude"), self.cfg.get("longitude")
        if lat is not None and lon is not None:
            times = self._local_sun(local_now.date(), lat, lon)
            if times:
                sun = {"sunrise": times[0].strftime("%H:%M"), "sunset": times[1].strftime("%H:%M")}
        schedules = self.list_schedules()
        for sched in schedules:
            when = self.fire_time(sched, local_now.date())
            sched["today"] = when.strftime("%H:%M") if when else None
            coming = self.next_run(sched, local_now)
            sched["next_in"], sched["next"] = coming if coming else (None, None)
        return {
            "version": VERSION,
            "time": local_now.strftime("%a %H:%M"),
            "clock_synced": self.clock_synced,
            "accent": self.accent(),
            "clock": self.clock_style(),
            "bridge": bridge,
            "bridges": bridges,
            "blinds": blinds,
            "schedules": schedules,
            "log": log,
            "sun": sun,
        }

    # ---- sessions --------------------------------------------------------

    def login_allowed(self, ip: str) -> bool:
        now = self.clock()
        with self.cond:
            recent = [t for t in self.login_failures.get(ip, []) if now - t < 300]
            if recent:
                self.login_failures[ip] = recent
            else:
                self.login_failures.pop(ip, None)
            return len(recent) < 5

    def login_failed(self, ip: str) -> None:
        with self.cond:
            self.login_failures.setdefault(ip, []).append(self.clock())

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def new_session(self) -> str:
        """Sessions live in the database so a restart or update does not sign anyone out."""
        token = secrets.token_urlsafe(32)
        with self.cond:
            now = self.clock()
            self.db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            self.db.execute(
                "INSERT INTO sessions(token_hash, expires) VALUES(?, ?)",
                (self._hash_token(token), now + SESSION_DAYS * 86400),
            )
            self.db.commit()
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        with self.cond:
            row = self.db.execute(
                "SELECT expires FROM sessions WHERE token_hash=?", (self._hash_token(token),)
            ).fetchone()
            return bool(row) and row["expires"] > self.clock()

    def end_session(self, token: str) -> None:
        with self.cond:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (self._hash_token(token),))
            self.db.commit()

    # ---- Settings page ---------------------------------------------------

    def admin(self, bridge_id: str, op: str, args: dict | None = None, timeout: float = ADMIN_WAIT) -> dict:
        """Ask a bridge to do something and wait for its answer."""
        with self.cond:
            now = self.clock()
            if not isinstance(bridge_id, str) or bridge_id not in self.bridges:
                raise ValidationError("unknown bridge")
            if not self._online(bridge_id, now):
                raise ValidationError("that bridge is offline")
            cmd = {
                "cmd": secrets.token_hex(6),
                "bridge": bridge_id,
                "op": op,
                "args": args or {},
                "created": now,
                "expires": now + timeout,
            }
            self.queue.append(cmd)
            self.cond.notify_all()
            deadline = time.monotonic() + timeout
            while cmd["cmd"] not in self.admin_results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if cmd in self.queue:
                        self.queue.remove(cmd)
                    self.inflight.pop(cmd["cmd"], None)
                    raise ValidationError("the bridge did not answer in time")
                self.cond.wait(timeout=min(remaining, 2.0))
            result = self.admin_results.pop(cmd["cmd"])
        if not result["ok"]:
            raise ValidationError(result["err"] or "the bridge reported an error")
        return result["data"]

    def debug_zip(self, bridge_id: str) -> tuple[str, bytes]:
        """One file with everything needed to look into a problem.

        Captures, the bridge log and the state of the bridge machine's
        Bluetooth and network come from the bridge; the server adds its own
        log and the state the page shows. No password, token or blind key
        is in any of them. Whatever cannot be fetched is noted, not fatal.
        """
        problems = []
        captures, total = [], 0
        try:
            got = self.admin(bridge_id, "captures", timeout=40)
            raw = zlib.decompressobj().decompress(base64.b64decode(str(got.get("z", "")), validate=True),
                                                  MAX_CAPTURE_BYTES)
            captures = json.loads(raw.decode("utf-8"))
            if not isinstance(captures, list):
                raise ValueError("unexpected capture data")
            total = got.get("total") if isinstance(got.get("total"), int) else len(captures)
        except (ValidationError, ValueError, zlib.error) as exc:
            captures = []
            problems.append(f"captures: {exc}")
        try:
            bridge_log = str(self.admin(bridge_id, "logs", {"lines": 400}, timeout=30).get("text", ""))
        except ValidationError as exc:
            bridge_log = ""
            problems.append(f"bridge log: {exc}")
        try:
            system = str(self.admin(bridge_id, "diagnostics", timeout=60).get("text", ""))
        except ValidationError as exc:
            system = ""
            problems.append(f"system: {exc}")
        state = self.state()
        stamp = dt.datetime.fromtimestamp(self.clock(), tz=self.tz)
        info = {
            "made": stamp.isoformat(timespec="seconds"),
            "server": VERSION,
            "python": platform.python_version(),
            "host": platform.node(),
            "bridge": bridge_id,
            "captures_included": len(captures),
            "captures_on_bridge": total,
            "problems": problems,
            "state": state,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("info.json", json.dumps(info, indent=2))
            archive.writestr("captures.json", json.dumps(captures, indent=1))
            archive.writestr("bridge-log.txt", bridge_log)
            archive.writestr("system.txt", system)
            archive.writestr("server-log.txt", read_journal("blindbridge-server", 400))
        safe = "".join(ch for ch in bridge_id if ch.isalnum() or ch in "-_")[:24] or "bridge"
        return f"blindbridge-debug-{safe}-{stamp.strftime('%Y%m%d-%H%M')}.zip", buffer.getvalue()

    def check_clock(self) -> bool | None:
        """Ask the system whether its clock is set from a time server, and note a change.

        Schedules run on this machine's clock. A Raspberry Pi has no clock
        battery: after a power cut it starts with the time it last saved, and
        is wrong until it reaches a time server. The system keeps the clock
        itself (systemd-timesyncd on Raspberry Pi OS); this only reads what it
        reports, so the page can say when the time is not to be trusted.
        """
        try:
            done = self.run_command(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                    capture_output=True, text=True, timeout=5)
            word = (done.stdout or "").strip().lower()
        except (OSError, subprocess.SubprocessError):
            word = ""
        synced = True if word == "yes" else False if word == "no" else None
        with self.cond:
            before, self.clock_synced = self.clock_synced, synced
            if synced is False and before is not False:
                self.log("the clock is not synced with a time server; schedule times may be off")
            elif synced is True and before is False:
                self.log("the clock is synced with a time server again")
        return synced

    def net_failsafe(self) -> bool:
        """A second, independent way for Wi-Fi to come back, in case the bridge is not running.

        If this machine's bridge switched Wi-Fi off (it leaves a marker file
        when it does), Wi-Fi is still off, and no network cable has been live
        for three looks in a row, switch Wi-Fi on. Returns True if it did.
        """
        if netwatch is None:
            return False
        folder = INSTALL_DIR / "bridge" if self.bridge_dir is None else self.bridge_dir
        marker = folder / "wifi_off"
        if not marker.exists() or netwatch.wifi_blocked(self.sys_path) is not True or netwatch.cabled(self.sys_path):
            self._no_cable = 0
            return False
        self._no_cable += 1
        if self._no_cable < 3:
            return False
        self._no_cable = 0
        done = self.run_command(netwatch.UNBLOCK, capture_output=True, text=True, timeout=10)
        if done.returncode != 0:
            return False
        try:
            marker.unlink()
        except OSError:
            pass
        self.log("wifi was off with no network cable connected: switched back on")
        return True

    def _save_cfg(self) -> None:
        if self.cfg_path is not None:
            save_config(self.cfg_path, self.cfg)

    def info(self) -> dict:
        with self.cond:
            now = self.clock()
            return {
                "version": VERSION,
                "python": platform.python_version(),
                "host": platform.node(),
                "uptime": int(now - self.started),
                "timezone": self.cfg["timezone"],
                "latitude": self.cfg.get("latitude"),
                "longitude": self.cfg.get("longitude"),
                "can_restart": self.restart_hook is not None,
                "can_power": self.can_power(),
            }

    def set_location(self, data: dict) -> None:
        timezone = str(data.get("timezone", "")).strip()
        try:
            zone = ZoneInfo(timezone)
        except Exception:
            raise ValidationError("unknown timezone; use a name like Europe/Rome or America/Chicago") from None
        lat, lon = data.get("latitude"), data.get("longitude")
        if lat is None and lon is None:
            pass
        else:
            for value, limit, label in ((lat, 90, "latitude"), (lon, 180, "longitude")):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > limit:
                    raise ValidationError(f"{label} must be a number between -{limit} and {limit}")
        with self.cond:
            self.cfg.update(timezone=timezone, latitude=lat, longitude=lon)
            self.tz = zone
            self._save_cfg()
            self.log("location settings changed")

    def accent(self) -> str:
        value = self.cfg.get("accent")
        return value if isinstance(value, str) and ACCENT_RE.fullmatch(value) else DEFAULT_ACCENT

    def set_accent(self, value) -> str:
        """Save the page's accent color. None or an empty string restores the default."""
        if value in (None, ""):
            value = DEFAULT_ACCENT
        if not isinstance(value, str) or not ACCENT_RE.fullmatch(value.lower()):
            raise ValidationError("color must look like #2f5d50")
        with self.cond:
            self.cfg["accent"] = value.lower()
            self._save_cfg()
        return self.cfg["accent"]

    def clock_style(self) -> str:
        value = self.cfg.get("clock")
        return value if value in CLOCKS else "auto"

    def set_appearance(self, data: dict) -> dict:
        """Save the accent color, the time format, or both. Returns what is now in force."""
        clock = data.get("clock", self.clock_style())
        if clock not in CLOCKS:
            raise ValidationError("time format must be auto, 12 or 24")
        if "accent" in data:
            self.set_accent(data["accent"])
        with self.cond:
            self.cfg["clock"] = clock
            self._save_cfg()
        return {"accent": self.accent(), "clock": clock}

    def next_run(self, sched: dict, now: dt.datetime):
        """(seconds from now, label) for an enabled schedule's next run within a week, or None."""
        if not sched["enabled"]:
            return None
        for ahead in range(8):
            day = now.date() + dt.timedelta(days=ahead)
            when = self.fire_time(sched, day)
            if when is None or when <= now:
                continue
            where = "today" if ahead == 0 else ("tomorrow" if ahead == 1 else when.strftime("%a"))
            return int((when - now).total_seconds()), f"{when.strftime('%H:%M')} {where}"
        return None

    def set_password(self, current, new, keep_session: str) -> None:
        if not isinstance(current, str) or not verify_password(current, self.cfg["admin_password_hash"]):
            raise ValidationError("current password is wrong")
        if not isinstance(new, str) or len(new) < 8:
            raise ValidationError("new password must be at least 8 characters")
        rounds = int(self.cfg["admin_password_hash"].split("$")[1])
        with self.cond:
            self.cfg["admin_password_hash"] = hash_password(new, rounds)
            self._save_cfg()
            # Sign out everywhere else.
            self.db.execute("DELETE FROM sessions WHERE token_hash != ?", (self._hash_token(keep_session),))
            self.db.commit()
            self.log("admin password changed")

    # Shutting down or rebooting the Pi is a privileged request. deploy/install.sh
    # adds a polkit rule that lets this user ask the system to power off or
    # reboot, and nothing else. No sudo is involved, so the service can keep
    # running with "no new privileges".
    POWER_COMMANDS = {
        "poweroff": ["/usr/bin/systemctl", "poweroff"],
        "reboot": ["/usr/bin/systemctl", "reboot"],
    }
    POWER_CHECK = ["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                   "org.freedesktop.login1.Manager", "CanPowerOff"]

    def can_power(self) -> bool:
        # Only a yes is remembered. A no is checked again each time, so the
        # buttons come alive as soon as install.sh has added the permission.
        if not self._can_power:
            try:
                done = self.run_command(self.POWER_CHECK, capture_output=True, text=True, timeout=10)
                self._can_power = done.returncode == 0 and '"yes"' in (done.stdout or "")
            except (OSError, subprocess.SubprocessError):
                self._can_power = False
        return bool(self._can_power)

    def power(self, action) -> None:
        if action not in self.POWER_COMMANDS:
            raise ValidationError("unknown action")
        if not self.can_power():
            raise ValidationError("not allowed yet: run 'sh deploy/install.sh' once on this machine to enable the power buttons")
        self.log("shutting down the Pi" if action == "poweroff" else "rebooting the Pi")

        def go():
            try:
                self.run_command(self.POWER_COMMANDS[action], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                self.log(f"{action} failed: {exc}")

        threading.Timer(1.0, go).start()  # let the reply reach the browser first

    def online_bridges(self) -> list[str]:
        with self.cond:
            now = self.clock()
            return [b for b in sorted(self.bridges) if self._online(b, now)]

    def install_update(self, password, zip_b64) -> dict:
        import updater

        if not isinstance(password, str) or not verify_password(password, self.cfg["admin_password_hash"]):
            raise ValidationError("password is wrong")
        try:
            data = base64.b64decode(str(zip_b64 or ""), validate=True)
            result = updater.apply_update(data, INSTALL_DIR)
        except (updater.UpdateError, ValueError) as exc:
            raise ValidationError(str(exc)) from None
        self.log(f"update {result['from']} -> {result['to']}: {len(result['changed'])} files changed")
        bridges = {}
        for bridge_id in self.online_bridges():
            # A bridge on this machine shares these files and only needs to restart.
            # One on another Pi installs the same zip into its own folder.
            try:
                self.admin(bridge_id, "update", {"zip_b64": zip_b64}, timeout=90)
                bridges[bridge_id] = "updated"
            except ValidationError as exc:
                bridges[bridge_id] = str(exc)
        result["bridges"] = bridges
        result["changed"] = len(result["changed"])
        return result


def read_journal(unit: str, lines: int) -> str:
    try:
        done = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(log not available: {exc})"
    return (done.stdout or done.stderr or "(no log lines)")[-60000:]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BlindBridge"
    sys_version = ""
    timeout = 120    # seconds a connection may sit idle or stall mid-request before it is closed
    hub: Hub = None  # set by make_server

    def log_message(self, fmt, *args):  # quiet: the hub logs what matters
        pass

    def version_string(self) -> str:
        return self.server_version

    # ---- helpers ---------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json", extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self, limit: int = MAX_BODY):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self.close_connection = True
            self._error(413, "request too large")
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._error(400, "body must be JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "body must be a JSON object")
            return None
        return data

    def _drain(self) -> None:
        """Discard a request body without answering, so keep-alive stays in step."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if 0 <= length <= MAX_BODY:
            self.rfile.read(length)
        else:
            self.close_connection = True

    def _client_ip(self) -> str:
        if self.hub.cfg.get("trust_proxy"):
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def _is_https(self) -> bool:
        return bool(self.hub.cfg.get("trust_proxy")) and (
            self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        )

    def _bearer(self) -> str:
        value = self.headers.get("Authorization", "")
        return value[7:].strip() if value.startswith("Bearer ") else ""

    def _session_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "bb_session":
                return value
        return ""

    def _refuse(self, status: int, message: str) -> bool:
        """Turn a request away before its body is read, and close the connection so the body is never parsed."""
        self.close_connection = True
        self._error(status, message)
        return False

    def _authorized(self, mutating: bool) -> bool:
        """API auth: a bearer API token, or a session cookie plus the X-BB header."""
        if token_equal(self._bearer(), self.hub.cfg.get("api_token", "")):
            return True
        if not self.hub.session_valid(self._session_token()):
            return self._refuse(401, "sign in required")
        if mutating and self.headers.get("X-BB") != "1":
            return self._refuse(403, "missing X-BB header")
        return True

    def _admin_ok(self) -> bool:
        """Settings actions need a signed-in browser. The API token is not enough."""
        if not self.hub.session_valid(self._session_token()):
            return self._refuse(401, "sign in required")
        if self.command not in ("GET", "HEAD") and self.headers.get("X-BB") != "1":
            return self._refuse(403, "missing X-BB header")
        return True

    def _admin_get(self, path: str):
        if not self._admin_ok():
            return
        if path == "/api/admin/info":
            return self._json(200, self.hub.info())
        if path == "/api/admin/download":
            import updater

            data = updater.build_zip(INSTALL_DIR)
            name = f"blindbridge-{updater.installed_version(INSTALL_DIR)}.zip"
            return self._send(200, data, "application/zip",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/api/admin/network":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            bridge_id = (query.get("bridge") or [""])[0]
            try:
                return self._json(200, self.hub.admin(bridge_id, "network", timeout=30))
            except ValidationError as exc:
                return self._error(400, str(exc))
        if path == "/api/admin/debug":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            bridge_id = (query.get("bridge") or [""])[0]
            if bridge_id not in self.hub.bridges:
                return self._error(400, "unknown bridge")
            name, data = self.hub.debug_zip(bridge_id)
            return self._send(200, data, "application/zip",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if path == "/api/admin/logs":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            target = (query.get("target") or ["server"])[0]
            try:
                if target == "server":
                    text = read_journal("blindbridge-server", 150)
                else:
                    text = self.hub.admin(target, "logs", {"lines": 150}, timeout=30).get("text", "")
            except ValidationError as exc:
                return self._error(400, str(exc))
            return self._json(200, {"text": str(text)})
        self._error(404, "not found")

    def _admin_post(self, path: str):
        if not self._admin_ok():
            return
        data = self._body(MAX_ADMIN_BODY if path == "/api/admin/update" else MAX_SYNC_BODY)
        if data is None:
            return
        hub = self.hub
        try:
            if path == "/api/admin/scan":
                found = hub.admin(data.get("bridge"), "scan", {"seconds": 8, "all": bool(data.get("all"))}, timeout=60)
                return self._json(200, found)
            if path == "/api/admin/import":
                if not isinstance(data.get("backup"), (dict, list)):
                    raise ValidationError("that is not a backup file")
                return self._json(200, hub.admin(data.get("bridge"), "import", {"backup": data["backup"]}))
            if path == "/api/admin/blind":
                action = data.get("action")
                if action not in ("add", "rename", "remove"):
                    raise ValidationError("unknown action")
                args = {k: data.get(k) for k in ("blind", "name", "mac", "key") if data.get(k) is not None}
                before = hub._blind_name(str(args.get("blind", ""))) if action == "rename" else ""
                got = hub.admin(data.get("bridge"), action, args)
                if action == "rename" and got.get("name") and got["name"] != before:
                    hub.log(f"renamed {before} to {got['name']}"[:160])
                return self._json(200, got)
            if path == "/api/admin/capture":
                action = data.get("action")
                bridge_id = data.get("bridge")
                if action == "now":
                    note = data.get("note")
                    args = {"blind": str(data.get("blind", ""))[:32], "seconds": 8,
                            "note": note[:160] if isinstance(note, str) else ""}
                    got = hub.admin(bridge_id, "capture", args)
                    hub.log(f"capture of {hub._blind_ref(args['blind'])}: "
                            + ("saved" if got.get("code") == "ok" else f"saved, but {str(got.get('err'))[:80]}"))
                    return self._json(200, got)
                if action == "mode":
                    got = hub.admin(bridge_id, "capture_mode", {"on": data.get("on") is True}, timeout=30)
                    hub.log(f"capture mode {'on' if got.get('on') else 'off'} for bridge '{bridge_id}'")
                    return self._json(200, got)
                if action == "clear":
                    return self._json(200, hub.admin(bridge_id, "capture_clear", timeout=30))
                raise ValidationError("unknown action")
            if path == "/api/admin/network":
                mode = data.get("mode")
                if mode not in ("on", "auto"):
                    raise ValidationError("unknown Wi-Fi setting")
                got = hub.admin(data.get("bridge"), "wifi_mode", {"mode": mode}, timeout=30)
                hub.log("wifi: off while a network cable is working" if mode == "auto" else "wifi: always on")
                return self._json(200, got)
            if path == "/api/admin/location":
                hub.set_location(data)
                return self._json(200, {"ok": True})
            if path == "/api/admin/appearance":
                return self._json(200, dict(hub.set_appearance(data), ok=True))
            if path == "/api/admin/password":
                hub.set_password(data.get("current"), data.get("new"), self._session_token())
                return self._json(200, {"ok": True})
            if path == "/api/admin/power":
                hub.power(data.get("action"))
                return self._json(200, {"ok": True})
            if path == "/api/admin/restart":
                target = data.get("target")
                if target == "server":
                    if hub.restart_hook is None:
                        raise ValidationError("this server was not started as a service, so it cannot restart itself")
                    hub.log("restart requested from the Settings page")
                    self._json(200, {"ok": True})
                    return hub.restart_hook()
                hub.admin(target, "restart", timeout=60)
                return self._json(200, {"ok": True})
            if path == "/api/admin/update":
                ip = self._client_ip()
                if not hub.login_allowed(ip):
                    raise ValidationError("too many wrong passwords, wait five minutes")
                try:
                    result = hub.install_update(data.get("password"), data.get("zip_b64"))
                except ValidationError as exc:
                    if "password" in str(exc):
                        hub.login_failed(ip)
                    raise
                result["restarting"] = hub.restart_hook is not None
                self._json(200, result)
                if hub.restart_hook is not None:
                    hub.restart_hook()
                return
        except ValidationError as exc:
            return self._error(400, str(exc))
        self._error(404, "not found")

    # ---- routes ----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                page = (HERE / "ui.html").read_bytes()
            except OSError:
                return self._error(500, "ui.html is missing")
            return self._send(200, page, "text/html; charset=utf-8",
                              {"Content-Security-Policy": page_policy(page)})
        if path == "/api/state":
            if not self._authorized(False):
                return
            return self._json(200, self.hub.state())
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path.startswith("/api/admin/"):
            return self._admin_get(path)
        self._error(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/bridge/v1/sync":
            return self._bridge_sync()
        if path == "/api/login":
            return self._login()
        if path.startswith("/api/admin/"):
            return self._admin_post(path)
        if not self._authorized(True):
            return
        data = self._body()
        if data is None:
            return
        try:
            if path == "/api/logout":
                self.hub.end_session(self._session_token())
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie("", 0)})
            if path == "/api/move":
                ids = self.hub.enqueue(data.get("blinds"), data.get("position"), "manual")
                return self._json(200, {"ok": True, "commands": ids})
            if path == "/api/schedules":
                return self._json(200, {"ok": True, "id": self.hub.save_schedule(data)})
        except ValidationError as exc:
            return self._error(400, str(exc))
        self._error(404, "not found")

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if not self._authorized(True):
            return
        if path.startswith("/api/schedules/"):
            try:
                sched_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._error(400, "bad id")
            if self.hub.delete_schedule(sched_id):
                return self._json(200, {"ok": True})
            return self._error(404, "schedule not found")
        if path.startswith("/api/bridges/"):
            bridge_id = urllib.parse.unquote(path.rsplit("/", 1)[1])
            try:
                if self.hub.forget_bridge(bridge_id):
                    return self._json(200, {"ok": True})
            except ValidationError as exc:
                return self._error(400, str(exc))
            return self._error(404, "bridge not found")
        self._error(404, "not found")

    def _cookie(self, token: str, max_age: int) -> str:
        cookie = f"bb_session={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
        if self._is_https():
            cookie += "; Secure"
        return cookie

    def _login(self):
        data = self._body()
        if data is None:
            return
        ip = self._client_ip()
        if not self.hub.login_allowed(ip):
            return self._error(429, "too many attempts, wait five minutes")
        password = data.get("password")
        if not isinstance(password, str) or not verify_password(
            password, self.hub.cfg["admin_password_hash"]
        ):
            self.hub.login_failed(ip)
            return self._error(401, "wrong password")
        token = self.hub.new_session()
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie(token, SESSION_DAYS * 86400)})

    def _bridge_sync(self):
        if not token_equal(self._bearer(), self.hub.cfg["bridge_token"]):
            self._drain()
            return self._error(401, "bad bridge token")
        data = self._body(MAX_SYNC_BODY)
        if data is None:
            return
        wait = data.get("wait", MAX_WAIT)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            wait = MAX_WAIT
        self._json(200, self.hub.sync(data, wait))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A client that goes away mid-reply (a bridge restarting, a closed tab)
        # is normal and not worth a traceback in the log.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def make_server(cfg: dict, base_dir: Path, clock=time.time, cfg_path: Path | None = None):
    hub = Hub(cfg, base_dir, clock, cfg_path)
    handler = type("BoundHandler", (Handler,), {"hub": hub})
    httpd = Server((cfg["listen"], int(cfg["port"])), handler)
    return httpd, hub


def scheduler_loop(hub: Hub, stop: threading.Event) -> None:
    passes = 0
    while not stop.wait(20 if passes else 2):
        try:
            hub.run_schedules()
        except Exception as exc:  # keep the scheduler alive no matter what
            print("scheduler error:", repr(exc), flush=True)
        try:
            if passes % 3 == 0:                 # once a minute, and right after starting
                hub.check_clock()
        except Exception as exc:
            print("clock check error:", repr(exc), flush=True)
        passes += 1
        try:
            hub.net_failsafe()
        except Exception as exc:
            print("network failsafe error:", repr(exc), flush=True)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def cmd_init(args) -> None:
    path = Path(args.config)
    cfg = dict(DEFAULT_CONFIG, timezone=system_timezone())
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
        print(f"Updating {path}")
    password = args.password
    if password is None:
        while True:
            password = getpass.getpass("Admin password for the web page: ")
            if len(password) < 8:
                print("Use at least 8 characters.")
                continue
            if getpass.getpass("Again: ") == password:
                break
            print("They did not match.")
    elif len(password) < 8:
        raise SystemExit("Password must be at least 8 characters.")
    cfg["admin_password_hash"] = hash_password(password)
    if args.timezone:
        cfg["timezone"] = args.timezone
    elif not args.yes:
        entered = input(f"Timezone [{cfg['timezone']}]: ").strip()
        if entered:
            cfg["timezone"] = entered
    ZoneInfo(cfg["timezone"])
    if args.latitude is not None and args.longitude is not None:
        cfg["latitude"], cfg["longitude"] = args.latitude, args.longitude
    elif not args.yes:
        entered = input("Latitude,longitude for sunrise/sunset schedules (blank to skip): ").strip()
        if entered:
            lat, lon = (float(x) for x in entered.split(","))
            cfg["latitude"], cfg["longitude"] = lat, lon
    if args.listen:
        cfg["listen"] = args.listen
    if args.port:
        cfg["port"] = args.port
    if not cfg.get("bridge_token") or args.new_tokens:
        cfg["bridge_token"] = secrets.token_urlsafe(32)
    if not cfg.get("api_token") or args.new_tokens:
        cfg["api_token"] = secrets.token_urlsafe(32)
    save_config(path, cfg)
    print(f"\nWrote {path}")
    print("Bridge token (goes into the bridge's setup command):")
    print("  " + cfg["bridge_token"])
    print("API token (for scripts and phone shortcuts):")
    print("  " + cfg["api_token"])


def cmd_run(args) -> None:
    path = Path(args.config)
    if not path.exists():
        raise SystemExit(f"{path} not found. Run: python3 server.py init")
    cfg = load_config(path)
    httpd, hub = make_server(cfg, path.resolve().parent, cfg_path=path)
    # Leaving with os._exit lets the service manager start a fresh copy, which is
    # how a restart or an update takes effect. The short delay lets the reply out.
    hub.restart_hook = lambda: threading.Timer(0.8, os._exit, args=(0,)).start()
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(hub, stop), daemon=True).start()
    hub.log(f"BlindBridge server {VERSION} listening on {cfg['listen']}:{cfg['port']}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="BlindBridge server")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="create or update config.json")
    p_init.add_argument("--config", default=str(HERE / "config.json"))
    p_init.add_argument("--password")
    p_init.add_argument("--timezone")
    p_init.add_argument("--latitude", type=float)
    p_init.add_argument("--longitude", type=float)
    p_init.add_argument("--listen", help="address to listen on; 0.0.0.0 for the whole home network")
    p_init.add_argument("--port", type=int)
    p_init.add_argument("--new-tokens", action="store_true", help="replace both tokens")
    p_init.add_argument("--yes", action="store_true", help="do not ask questions")
    p_init.set_defaults(func=cmd_init)
    p_run = sub.add_parser("run", help="start the server")
    p_run.add_argument("--config", default=str(HERE / "config.json"))
    p_run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
