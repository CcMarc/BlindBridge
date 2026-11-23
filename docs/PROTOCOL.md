# Protocols

Two protocols are described here: the Bluetooth protocol of the blind motors,
and the HTTP protocol between a bridge and the server.

The motor notes come from recordings of two first-generation MySmartBlinds
tilt motors (version block `06 3f 93 0b 02 01 0c 03 ...`) made with this
project's capture mode, on a Raspberry Pi 3 running BlueZ 5.82. Where
something was seen on those two motors only, or is not understood, it says so.

## 1. Blind motors (Bluetooth LE)

| Item | Value |
| --- | --- |
| Advertised name | `SmartBlind_DFU` |
| Address type | static random |
| Service | `00001400-1212-efde-1600-785feabcd123` |
| Key characteristic | `00001409-…`, write the blind's key first |
| Position characteristic | `00001403-…`, write one byte |
| Acknowledgement characteristic | `00001503-…`, indications |

Position byte: `100` open (flat), `200` closed, `0` closed the other way.

A motor accepts one connection at a time, so a phone app that is connected to
a blind keeps the bridge out, and the other way round.

### Keys

Each blind has its own key, 6 bytes in every backup seen so far. The phone
app's offline backup is JSON. Each blind in it carries `name`,
`encodedMacAddress` and `encodedPasskey`, the last two in base64. The address
in the backup is byte-reversed compared with the one the motor advertises.

Treat keys like passwords. The bridge stores them in `bridge/blinds.json`,
readable by its owner only, and never sends them to the server's database,
its logs, captures or debug zips.

### Characteristics

Each characteristic names itself in its user-description descriptor.

| UUID (short) | Motor's name | Properties | Seen |
| --- | --- | --- | --- |
| `1401` | ID | read, write, notify | 4 bytes, then the blind's name as text |
| `1402` | Stat | read, write, indicate | 8 bytes, for example `000015a000000000` |
| `1403` | Ang | read, write, indicate | the position. Reads `ff` until the motor has moved in this connection |
| `1404` | Ver | read, write, indicate | version block |
| `1405` | Time | read, write, notify | `ff` x 6 |
| `1407` | RX | read, write, indicate, notify | `ff` x 20 |
| `1409` | (key) | read, write, indicate | never read by the bridge |
| `140a` | Cal | read, indicate | `ffff` |
| `1501` | Sched | read, write, indicate | `ff` x 71 |
| `1502` | DLS | read, write, indicate | `ff` x 12 |
| `1503` | ACK | read, indicate | see below |
| `1603` | Rm Pos | read, write, indicate | `ff` x 12 |
| `1651` | Sensors | read, indicate | `ff` x 14 |

There is also a Nordic DFU service (`1530`), which is where the advertised
name comes from. A value the motor has nothing for reads as `ff` bytes.

### Acknowledgement

Writing a position produces an ACK indication 3 to 250 ms later:
`03 14 00 02 30 <value>`. The first two bytes are the position
characteristic's short UUID, low byte first, and the last is the value
written. Positions 200, 100, 80 and 0 gave `c8`, `64`, `50` and `00`. The
middle three bytes are not understood.

After the acknowledgement the motor usually stops answering. Reads sent to it
fail, and the link ends when its supervision timeout runs out. Nothing useful
can be read after the ACK, so battery and status are read before the position
is written.

### Advertisement

Besides the name and the `1400` service, each motor broadcasts eight bytes of
service data under `0x0add`: its address, low byte first, then `80`, then its
position. The bridge reads this during a scan, before every connection and
after every move, which gives the real position without connecting. The `80`
byte is not understood.

A motor that has moved broadcasts its new position within about five seconds
of the acknowledgement. The value is where it stopped, which can be a step or
two short of the request (198 for 200).

### Battery and charging

- Sensors, 14 bytes: byte 0 is the battery percentage (0 to 100). Then
  little-endian 16-bit raw values: battery voltage, battery charge, solar
  panel voltage, interior temperature, battery temperature, light.
- Stat, first four bytes as a little-endian number: bit `0x00040000` is solar
  charging, `0x00080000` is USB charging.

This layout is the one other open projects for these motors use. A motor
does not always answer. On the two motors tested, Sensors often read blank
and returned a percentage on other moves. The page shows a battery level
when a motor returns one and leaves it out when it does not.

### A motor can acknowledge and not turn

One of the two motors did this while resting at an in-between angle: every
close was acknowledged with the usual ACK, from this bridge and from the phone
app's own Close button, and the blind stayed where it was. Its broadcast
stayed at the old value too. Moving it to a different position with the app's
slider freed it. Why it gets into that state is not known.

So the bridge checks. Ten seconds after an acknowledged move it listens for
the motor's broadcast, without connecting. If the position is within 5 of the
one asked for, the stored position is set to the broadcast one and that is
all. If not, it listens once more 15 seconds later. If the broadcast is still
elsewhere, the blind is marked as failed, with "Motor says it did not move"
(still within 5 of where it was) or "Motor says it stopped partway", the
stored position becomes the broadcast one, and a line goes to the activity
log. A broadcast that cannot be heard proves nothing and changes nothing. A
newer move for the same blind replaces a check that is still to come, and a
check only runs when no command is in hand.

### Slow links on Linux, and the purge

On BlueZ 5.82 with a recent kernel (seen on 6.18, and the same code is in
6.12), a motor connects quickly the first time after the machine starts and
again within about half a minute. After that every connection to that motor
takes more than 30 seconds, until the machine is restarted. The cause, from
the BlueZ and kernel sources and from capture timings:

- Each motor publishes Peripheral Preferred Connection Parameters
  (characteristic `2a04`) of `14 00 c8 00 00 00 f4 01`: one exchange every 25
  to 250 ms, with a 5-second supervision timeout.
- BlueZ reads that on every connection and hands it to the kernel
  (`btd_device_set_conn_param`, MGMT "Load Connection Parameters", MGMT 1.23
  or later). The kernel moves the live link to it and keeps it for that
  address. The Raspberry Pi's radio takes the slowest interval allowed, so
  from then on every exchange takes 250 to 750 ms.
- The kernel's entry for the address outlives the connection. Forgetting the
  device in BlueZ and switching the radio off and on do not remove it. A
  restart of the machine does.
- A first connection starts on the kernel's defaults (30 to 50 ms), and BlueZ
  reads the motor's table of services in 4 to 8 seconds. BlueZ keeps that
  table only while it keeps the device, about 30 seconds after it last saw it.
  Later than that it reads the table again over the slow link, which takes
  more than half a minute.

What the bridge does about it, with no extra permissions:

1. **Purge before connecting.** The kernel drops its entry for an address
   when a connection attempt it was asked for is cancelled before it
   completes. So before the real connection the bridge sends BlueZ
   `Device1.Connect` and, right behind it on the same bus connection,
   `Device1.Disconnect`. The cancel arrives before the radio has started
   looking. BlueZ drops its record of the motor when that attempt fails, so
   half a second later the bridge scans for the motor again and then makes
   the real connection, which starts on fast settings.
2. **A long timeout.** A connection may take 75 seconds, so it succeeds on a
   slow link too if the purge ever does not take.
3. **Two tries.** The first with the purge, the second without.

The bridge's log line for a move carries `connect` (seconds the connection
took) or `gave_up` (seconds until it was abandoned), and what each earlier
try ran into. A connection of 4 to 8 seconds means the purge worked.

If both tries hear the motor and cannot connect, the bridge saves a snapshot
of the machine's Bluetooth state to `bridge/stuck.txt`, saves the move to
`bridge/pending.json`, and restarts the machine. Moves that arrive while that
restart is waiting are saved with it. After the restart it waits 20 seconds
and carries them out, the last one for each blind. It will not restart in the
machine's first five minutes, nor within half an hour of the last time, and
it can be switched off with `"auto_reboot": false` in `bridge/bridge.json`.

### How a move runs

Scan until the motor is heard and note the position it broadcasts. Purge.
Scan again. Connect. Write the key. Subscribe to ACK. Read Sensors and Stat
if that has not been done for this motor in the last 15 minutes. Write the
position. Wait up to 4 seconds for the acknowledgement, then half a second
more. Disconnect. Ten seconds later, check the broadcast.

A position that is written but never acknowledged counts as a failure ("sent,
but the motor did not confirm it") and is tried once more. From the button
press a move takes about as long as the connection does, 5 to 10 seconds.

## 2. Bridge to server

One HTTP endpoint. The bridge calls it in a loop.

```
POST /bridge/v1/sync
Authorization: Bearer <bridge token>
Content-Type: application/json
```

Request body:

```json
{
  "fw": "1.0.0",
  "id": "bridge id",
  "uptime": 1234,
  "wait": 25,
  "capture": false,
  "captures": 0,
  "wifi_off": false,
  "blinds": [
    {"id": "aabbccddeeff", "name": "Kitchen", "pos": 100, "ok": true, "err": "", "rssi": -70, "age": 42,
     "battery": null, "charging": "", "health_age": -1}
  ],
  "results": [
    {"cmd": "9c3bde41a9c3", "ok": true, "err": ""}
  ],
  "notes": []
}
```

- `id`: a short name for this bridge. Several bridges can connect, each with
  its own `id`. The server sends a bridge only the commands for blinds that
  bridge reported.
- `wait`: seconds the server may hold the request open (capped at 25).
- `blinds`: the bridge's full list every time. It replaces whatever this
  bridge reported before and leaves other bridges' blinds alone. `pos` is
  `-1` or absent if unknown. `age` is seconds since that blind last moved,
  `-1` if never. The bridge owns the list and the keys. Keys never go to the
  server in a report.
- `capture`, `captures`: whether capture mode is on, and how many captures
  the bridge holds.
- `wifi_off`: the bridge has switched this machine's Wi-Fi off because a
  network cable is working.
- `results`: outcomes of commands received in the previous response.
- `notes`: lines for the server's activity log, such as "Kitchen did not
  move: sent to 200, still at 120".

Response body:

```json
{"commands": [{"cmd": "9c3bde41a9c3", "blind": "aabbccddeeff", "pos": 200}], "now": 1794580000}
```

The server answers as soon as a command is waiting, otherwise when `wait`
runs out. The bridge carries out a command, then syncs again with the result.
While a command is running it keeps syncing with `wait` 0 about every 10
seconds, so it never looks offline during a slow move. If the bridge is
silent for 45 seconds the server shows it offline. Manual commands not
collected within 2 minutes are dropped. Scheduled ones get 10 minutes.

Text the server takes from a bridge (names, errors, notes) is reduced to one
printable line and cut to length before it is stored or shown.

### Settings actions

The server can also send a bridge a management command. It has `op` and
`args` in place of `blind` and `pos`:

```json
{"cmd": "5d0c1f2a9b7e", "op": "scan", "args": {"seconds": 8}}
```

The bridge answers in `results` with an extra `data` object:
`{"cmd": "...", "ok": true, "err": "", "data": {...}}`.

| op | args | data |
| --- | --- | --- |
| `scan` | `seconds`, `all` | `found`: list of `{mac, name, rssi, pos, known}` |
| `import` | `backup`: the app backup, parsed | `added`, `updated`: lists of names |
| `add` | `name`, `mac`, `key` | `name`, `created` |
| `rename` | `blind`, `name` | `name` |
| `remove` | `blind` | `name` |
| `logs` | `lines` | `text` |
| `restart` | none | none. The bridge exits once the answer is delivered and the service manager starts it again. |
| `update` | `zip_b64`: a release zip | `from`, `to`, `changed`. Then it restarts. Accepted only from a server on the bridge's own machine or over https, unless `"updates_over_http": true` is set in `bridge/bridge.json`. |
| `capture` | `blind`, `seconds` (3 to 20), `note` | `file`, `code`, `err`, `rssi`, and counts: `chars`, `reads`, `notifies`, `errors`. Records one blind without moving it. |
| `capture_mode` | `on` | `on`, `minutes`. While on, the first try of every move is recorded. It switches itself off after an hour. |
| `captures` | none | `count`, `total`, `z`: the newest captures as a JSON list, zlib-compressed, base64 |
| `capture_clear` | none | `removed` |
| `diagnostics` | none | `text`: the state of the bridge machine's Bluetooth and network, from read-only system commands, and the snapshot from the last time Bluetooth was stuck |
| `network` | none | `mode`, `can_switch`, `wifi`, `cable`, `cable_ready`, `cable_ip`, `wifi_ip` |
| `wifi_mode` | `mode`: `on` or `auto` | as `network`, plus `grace` |

The `import` and `add` actions carry blind keys from the browser through the
server's memory to the bridge. They are not written to the server's database
or its logs.

## 3. Captures

A capture is a recording of one Bluetooth session with a motor, made to work
out what its data means. The bridge keeps the newest 40 in
`bridge/captures/`, one JSON file each, readable by the owner only. They are
never part of a release zip.

```json
{
  "v": 1, "fw": "1.0.0", "bridge": "pi", "at": "2026-11-16T09:12:03-0500",
  "kind": "probe", "blind": {"id": "...", "name": "Kitchen", "mac": "C4:..."},
  "pos": null, "last_pos": 100, "note": "app shows battery 87%",
  "result": {"code": "ok", "err": "", "rssi": -64},
  "summary": {"chars": 9, "reads": 24, "notifies": 3, "errors": 0},
  "decoded": {},
  "adv": {"name": "SmartBlind_DFU", "rssi": -64, "tx_power": null,
          "manufacturer": {}, "service_data": {"00000add-...": "..."}, "service_uuids": ["..."]},
  "gatt": [{"service": "...", "handle": 12, "chars": [
    {"uuid": "...", "handle": 14, "props": ["notify", "read"], "descriptors": [{"uuid": "...", "handle": 16}]}]}],
  "events": [
    {"t": 0.0, "op": "found", "rssi": -64},
    {"t": 0.6, "op": "purged"},
    {"t": 5.9, "op": "connected"},
    {"t": 5.9, "op": "listed", "services": 4, "readable": 6, "notifying": 2},
    {"t": 6.2, "op": "key-written"},
    {"t": 6.4, "op": "subscribed", "uuid": "...", "handle": 14},
    {"t": 6.9, "op": "read", "when": "start", "uuid": "...", "handle": 14, "hex": "64"},
    {"t": 7.4, "op": "notify", "uuid": "...", "handle": 14, "hex": "0164"},
    {"t": 14.9, "op": "done"}
  ]
}
```

- `kind` is `probe` (Capture now, the blind is not moved) or `move` (capture
  mode was on. `pos` is the position sent, and there is a `position-written`
  event).
- `t` is seconds since the capture began. Values are hex. Every readable
  characteristic is read at the start, about every 3 seconds while listening,
  and at the end, so a value that changes shows up.
- An `error` event carries `what` and `err`. A capture of a motor that was
  not reached is still saved, with the error in it.
- The key is never recorded. The key characteristic is not read, the key
  write is logged without its value, and a value that echoes the key is shown
  as `<key>`.

A recorded move subscribes to everything and reads everything before it
writes the position, so it is about 30 seconds slower than a plain move.

The Settings page's **Download debug zip** packs the captures with both
service logs, the bridge machine's Bluetooth and network status and the
page's state into one file. See the README for what that file does and does
not contain.

## 4. Activity lines and renames

The server's activity log names blinds. A line stores each blind as a marker,
`\x1f<id>|<name at the time>\x1f`, in place of plain text. When the log is
shown, the marker becomes the name the blind has now, or the stored name if
the blind has been removed. So the history follows a rename, including two
blinds swapping names. A rename also gets its own line.
