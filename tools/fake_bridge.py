#!/usr/bin/env python3
"""A pretend bridge, for trying the server without any Bluetooth hardware.

It speaks the same sync protocol as the real bridge and "moves" two made-up
blinds instantly.

    python3 fake_bridge.py http://127.0.0.1:8787 <bridge token>
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


class FakeBridge:
    def __init__(self, base_url: str, token: str, wait: int = 25,
                 bridge_id: str = "fakebridge", blinds: dict | None = None):
        self.url = base_url.rstrip("/") + "/bridge/v1/sync"
        self.token = token
        self.wait = wait
        self.bridge_id = bridge_id
        self.started = time.time()
        names = blinds or {"c0ffee000001": "Kitchen", "c0ffee000002": "Study"}
        self.blinds = {
            blind_id: {"name": name, "pos": -1, "ok": True, "err": "", "moved": None}
            for blind_id, name in names.items()
        }
        self.results: list[dict] = []
        self.fail_next = False

    def body(self) -> dict:
        now = time.time()
        return {
            "fw": "fake-0.1",
            "id": self.bridge_id,
            "uptime": int(now - self.started),
            "rssi": -55,
            "wait": self.wait,
            "blinds": [
                {
                    "id": blind_id,
                    "name": info["name"],
                    "pos": info["pos"],
                    "ok": info["ok"],
                    "err": info["err"],
                    "rssi": -70,
                    "age": int(now - info["moved"]) if info["moved"] else -1,
                }
                for blind_id, info in self.blinds.items()
            ],
            "results": self.results,
        }

    def sync_once(self) -> list[dict]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.body()).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
        )
        with urllib.request.urlopen(request, timeout=self.wait + 15) as response:
            reply = json.loads(response.read())
        self.results = []
        for command in reply.get("commands", []):
            if "op" in command:
                self.results.append({"cmd": command["cmd"], "ok": False, "err": "the pretend bridge cannot do that"})
                continue
            blind = self.blinds.get(command["blind"])
            ok = blind is not None and not self.fail_next
            if blind is not None:
                blind["ok"] = ok
                blind["err"] = "" if ok else "connect failed"
                if ok:
                    blind["pos"] = command["pos"]
                    blind["moved"] = time.time()
            self.results.append(
                {"cmd": command["cmd"], "ok": ok, "err": "" if ok else "connect failed"}
            )
            self.fail_next = False
        return reply.get("commands", [])


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    bridge = FakeBridge(sys.argv[1], sys.argv[2])
    print("Fake bridge running. Ctrl+C to stop.")
    while True:
        try:
            for command in bridge.sync_once():
                if "op" not in command:
                    print("moved", command["blind"], "to", command["pos"])
        except urllib.error.HTTPError as exc:
            print("server said", exc.code, exc.read().decode(errors="replace"))
            time.sleep(5)
        except OSError as exc:
            print("cannot reach server:", exc)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
