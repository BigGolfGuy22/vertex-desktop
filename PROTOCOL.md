# Vertex Golf BLE Protocol — Reverse Engineering Notes

## Device

- Advertised name format: `VTX<nnnnn>` (5-digit unit serial)
- MAC prefix: `FC:61:1F:...` (vendor OUI — scan for it rather than hardcoding)
- Manufacturer: Nordic Semiconductor (company ID 0x0059 in adv)
- Device internally branded as **"repeater"** — it is a BLE bridge that scans for and relays data from the actual sensor unit (`VTX_repeater` string in APK)
- No encryption, no bonding, no pairing PIN — open protocol

## GATT layout

| Service | Characteristic | Properties | Role |
|---|---|---|---|
| `0000fe59-...` | `8ec90003-...` | indicate, write | Nordic Buttonless DFU (firmware update — **do not write here accidentally**) |
| `0000fff0-0904-4eb4-8c23-a5f94f5034ee` | `0000fff1-...` | write, read, notify | **Command channel** — app writes ASCII commands here, receives acks/responses |
| | `0000fff2-...` | read, notify | **Status stream** — 20-byte packets |
| | `0000fff3-...` | read, notify | **Bulk data stream** — putt-stroke packets |

## Transport

- Default MTU: 23 bytes (we observed; maps to 20-byte ATT payload)
- Stroke data transmitted as **6 packets × 20 bytes = 120 bytes per stroke** over `fff3`
- Packet 0 = info/header (contains metadata)
- Packet 4 has a known unused byte
- Acceleration samples stored 4 bytes each

## Command format

ASCII comma-delimited text written to `fff1`. Confirmed commands from APK debug logs:

| Command | Purpose |
|---|---|
| `device,show_list` | Ask repeater to list nearby VTX sensors it can see |
| `deviceCONN,<name>` | Tell repeater to connect to a specific sensor (e.g. `deviceCONN,VTX&lt;nnnnn&gt;`) |
| `deviceCONN,disconnect` | Drop the sensor connection |
| `<timestamp-command>` | Sync device clock. Format TBD — app log: `"Sending timestamp command to device : <value>"` |
| `<live-mode-init>` | Starts live streaming. Format TBD |
| `<sync-mode-init>` | Starts sync mode. Format TBD |
| `<sync-data-request>,<puttId>` | Request a specific stored putt by ID |
| `<target-mode-init>` | Starts target mode. Format TBD |

## Response format

Sensor responses arrive as notifications on `fff1` (or possibly `fff2`) prefixed with `+M<id>`:

- `+M38 :: <data>` — LoftAngleChange
- `+M49 :: <data>` — StaticAim

## Metric catalog (M-models)

Each metric identified by numeric model ID. Parser class per metric: `getMnn<Name>Model`.

| ID | Metric |
|---|---|
| M1 | PathChange |
| M2 | Rhythm |
| M4 | BackStrokeTime |
| M5 | ForwardStrokeTime |
| M6 | TotalStrokeTime |
| M7 | BackStrokeRotation |
| M8 | ClubHeadSpeedImpact |
| M9 | BackStrokeLength |
| M10 | InPlaneRotationBack |
| M11 | InPlaneRotationForward |
| M12 | FaceChange |
| M13 | LoftAngleImpact |
| M14 | LieAngleChange |
| M15 | ForwardStrokeRotation |
| M16 | AttackAngle |
| M19 | ClubHeadAcceleration |
| M20 | ShaftLeanChange |
| M24 | GearEffectTwist |
| M25 | DynamicAim |
| M29 | FaceChange10cmFromAddress |
| M30 | FaceChange10cmBeforeImpact |
| M31 | FaceChange10cmAfterImpact |
| M33 | ShaftLeanAddress |
| M34 | ShaftLeanImpact |
| M35 | LieAngleAddress |
| M36 | LieAngleImpact |
| M37 | GraphStartSample |
| M38 | LoftAngleChange |
| M43 | PathDirectionR2T |
| M44 | TakeAwayTorque |
| M45 | LoftAngleAddress |
| M49 | StaticAim |

(Gaps M3, M17–M18, M21–M23, M26–M28, M32, M39–M42, M46–M48 — either unused, reserved, or I missed them.)

## Operating modes

- **Live mode** — real-time streaming, each stroke triggers a packet burst on `fff3`
- **Sync mode** — device stores strokes locally; app downloads stored sessions on request (`Sync Mode Command for Putt Id <id>`)
- **Target mode** — practice against a target value (app sends initial target config, scores each stroke)

## SQLite columns (local app storage)

```
UserName, PerctangeOfPutter, SizeOfPutter, PutterName, TagNotes, Session, SessionId,
AccelRate, AccelerationStart, AimAlignment, AttackAngle, BackStrokeTime,
BackstrokeLength, BackstrokeRotation, ClubHeadAcceleration, ClubHeadRotation,
ClubHeadSpeedImpact, DataUpload, DateCreate, DateLinkedCreation,
FaceAngle10cm{A|B}{Address|Impact}, FaceAngleAtImpact, FaceChange,
FaceChangeStartVFinish, FaceRotationChange{Back|Forward}, FeetValue,
ForwardStrokeRotation, ForwardStrokeTime, FullStrokeLength, GearEffectTwist,
GyroRate, Impact, ImpactPoint{Change|Horizontal|Vertical}, IsLeftHanded,
LieAngle{Address|Change|Impact}, LoftAngle{Address|Change|Impact},
...
```

## Derived metrics (recovered from blutter AOT analysis)

Several metrics are NOT stored as individual bytes in fff2 — they're computed
on-device by the app from other metrics. Tracing the Dart AOT assembly in
`BLE_Manager/putt_data_parsing/putt_data_packet_parsing.dart` reveals the
formulas:

```
shaft_lean_change  = in_plane_rotation_back − in_plane_rotation_forward
shaft_lean_impact  = shaft_lean_address + shaft_lean_change
loft_angle_address = STATIC_LOFT − shaft_lean_address
loft_angle_change  = −shaft_lean_change
loft_angle_impact  = STATIC_LOFT − shaft_lean_impact
```

`STATIC_LOFT` = 3.0° for the app's default "Custom" putter configuration. For
pre-set putter models (Scotty, Odyssey, etc.) the app reads a different value
from `SharedPreferences["Custom LoftAngle"]`.

Verified against 8 labeled putts (A1, B1–B3, C1–C3, D1) with 100 % match.

Physical meaning: the `in_plane_rotation_back/forward` metrics are the shaft's
pitch excursions during backswing and forward swing respectively. Net change =
backswing − forward-swing. Since the shaft rotating forward delofts the face,
loft change = negative of shaft lean change.

Relevant Dart functions:
- `ShaftLoftCalculator.calculateValues(orig, defaultLoft, defaultShaft)` at
  `0x7ab94c` — returns `{shaftLean: round(orig+defaultLoft), loftAngle: round(orig+defaultShaft)}`
- `PuttDataPacketParsing.getShaftLeanChange(d0, d1)` at `0x87378c` —
  returns `|round(d0+d1, 1)|`. Inputs are `M11.field_23` and `M10.field_23`
  (in-plane rotation forward and back respectively).
- `PuttDataPacketParsing.getShaftLeanImpact(change, address)` at `0x873004` —
  prints/returns `change + address`, with customPutter offset if configured.
- `PuttDataPacketParsing.getLoftAngleAddress(shaftLean)` at `0x7ad990` —
  returns `round(shaftLean + 3.0, 1)` where `3.0` is a hardcoded constant.

## Unknowns / next experiments

1. **Live/Sync/Target mode init commands** — exact bytes. Strategy: write `device,show_list` first, capture response, then write `deviceCONN,VTX&lt;nnnnn&gt;` and observe what `fff1`/`fff2` emit. The device may auto-enter live mode once connected.
2. **Timestamp command format** — probably `timestamp,<epoch>` or similar. Dart string-interp prefixes not recovered from AOT.
3. **Packet 0 header structure** — format of info packet.
4. **Response parsing** — whether `+Mnn` is ASCII-numeric or has a binary value.
5. Consider running Android app through a BLE sniffer dongle (nRF52840 + Sniffer firmware) to capture real traffic, OR use `blutter` tool for Flutter AOT disassembly to recover exact string constants.

## Key Dart symbols present in libapp.so

- File: `package:vertex_golf/BLE_Manager/putt_data_parsing/putt_data_packet_parsing.dart`
- Classes: `RepeaterParser`, `LiveModeDataModel`, `LiveTableDataModel`, `SyncViewModel`, `TargetModeViewmodel`, `CalibrationProcessViewModel`
- Methods: `sendDeviceConnCommand`, `sendTimestampCommand`, `sendTargetModeCommand`, `sendDeviceDisconnectCommandForRepeater`, `parseDeviceLogData`, `parseAccelerationDataAndInsertToLocalDB`, `parseAdvertisementData`, `parseVTXAdvertisementReceivedFromRepeater`, `startSyncModeResponseTimer`, `liveStreamEnded`, `writeStroke`, `showHandshakeToast`

## BLE library

- `flutter_blue_plus` (confirmed by `BmBluetoothCharacteristic`, `BmConnectionStateResponse` symbols)
- On desktop, `bleak` (Python) provides the equivalent BLE abstraction
