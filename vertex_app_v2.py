"""Vertex desktop app — HTTP server + default browser (no pywebview).

Serves ui.html on localhost, pushes live putt events over Server-Sent Events.
Launch: python vertex_app_v2.py
"""
from __future__ import annotations

import asyncio
import json
import queue
import random
import struct
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from bleak import BleakClient, BleakScanner


async def _winrt_unpair(address: str) -> bool:
    """Remove a BLE device from Windows pairing by address.

    Uses the WinRT BluetoothLEDevice API directly — no active BLE connection
    required.  This is exactly what "Remove device" does in Windows Bluetooth
    settings, which is the manual workaround users would otherwise need.
    """
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice  # type: ignore
        addr_int = int(address.replace(":", ""), 16)
        ble_dev = await BluetoothLEDevice.from_bluetooth_address_async(addr_int)
        if ble_dev is None:
            return False
        await ble_dev.device_information.pairing.unpair_async()
        ble_dev.close()
        return True
    except Exception:
        return False


def _resource_root() -> Path:
    """Resolve the directory holding ui.html + assets/.
    Works for normal runs (next to this file) AND for PyInstaller-bundled
    builds (inside sys._MEIPASS when --onefile, or next to the .exe for
    --onedir). Checks both locations and picks whichever has ui.html."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
        candidates.append(Path(sys.executable).parent)
    candidates.append(Path(__file__).resolve().parent)
    for c in candidates:
        if (c / "ui.html").exists():
            return c
    return candidates[0]


HERE = _resource_root()
HTML_PATH = HERE / "ui.html"

# BLE
FFF1 = "0000fff1-0904-4eb4-8c23-a5f94f5034ee"
FFF2 = "0000fff2-0904-4eb4-8c23-a5f94f5034ee"
FFF3 = "0000fff3-0904-4eb4-8c23-a5f94f5034ee"
ADV_BYTE = 0x02
HAND_BYTE = 0x01
NAME_PREFIX = "VTX"
PORT = 8765


PUTTER_STATIC_LOFT = 3.0  # degrees; Vertex app default for "Custom" putter


@dataclass
class Putt:
    index: int
    timestamp: int
    back_stroke_time: float
    forward_stroke_time: float
    total_stroke_time: float
    rhythm: float
    back_stroke_rotation: float
    forward_stroke_rotation: float
    club_head_speed: float
    club_head_accel: float
    club_head_accel_decel: bool
    inplane_rotation_back: float
    inplane_rotation_forward: float
    gear_effect_twist: float
    face_angle_10cm_from_address: float
    face_angle_10cm_before_impact: float
    face_change_10cm_after_impact: float
    shaft_lean_address: float
    shaft_lean_impact: float
    shaft_lean_change: float
    loft_angle_address: float
    loft_angle_impact: float
    loft_angle_change: float
    lie_angle_address: float
    lie_angle_impact: float
    lie_angle_change: float
    face_change: float
    back_stroke_length_raw: int


def _signed(pkt: bytes, mag_off: int, sign_off: int, *, high_bit_positive: bool) -> float:
    mag = pkt[mag_off]
    sign_bit = (pkt[sign_off] & 0x80) != 0
    sign = 1 if (sign_bit == high_bit_positive) else -1
    return sign * mag


def decode_putt(packets: list[bytes]) -> Optional[Putt]:
    if len(packets) != 6 or any(len(p) != 20 for p in packets):
        return None
    p0, p1, p2, p3, _p4, _p5 = packets
    ts_raw = struct.unpack_from("<I", p0, 11)[0]
    bst = p1[3] * 0.01
    fst = p2[3] * 0.01
    # Club-head accel at pkt2 off5, sign at off4 (0x80 = decel)
    cha_mag = p2[5] * 0.01
    decel = (p2[4] & 0x80) != 0
    cha = -cha_mag if decel else cha_mag
    bsr = p1[5] * 0.1
    fsr = p1[15] * 0.1
    # Signed metrics (all scale 0.1, sign byte adjacent)
    # Per screenshot convention:
    #   face_angle_10cm_from_address: + = opening   (sign 0x80 → +)
    #   face_angle_10cm_before_impact: + = closing  (sign 0x00 → +)
    #   face_change_10cm_after_impact: + = closing  (sign 0x00 → +)
    #   shaft_lean_address:            + = forward  (sign 0x80 → +)
    #   lie_angle_address / _impact:   + = toe-up   (sign 0x80 → +)
    fa_addr = _signed(p3, 6, 5,  high_bit_positive=True) * 0.1
    fa_before = _signed(p3, 8, 7, high_bit_positive=False) * 0.1
    fc_after = _signed(p3, 10, 9, high_bit_positive=False) * 0.1
    sla = _signed(p3, 14, 13, high_bit_positive=True) * 0.1
    laa = _signed(p3, 16, 15, high_bit_positive=True) * 0.1
    lai = _signed(p3, 18, 17, high_bit_positive=True) * 0.1
    iprb = p1[11] * 0.1  # in-plane rotation back (toe-up direction during backswing)
    iprf = p1[13] * 0.1  # in-plane rotation forward (toe-down direction during forward swing)
    # DERIVED via blutter-traced formulas (see PROTOCOL.md):
    #   shaft_lean_change = iprb − iprf
    #   shaft_lean_impact = shaft_lean_address + shaft_lean_change
    #   loft_angle_address = STATIC_LOFT − shaft_lean_address
    #   loft_angle_change = −(shaft_lean_change)
    #   loft_angle_impact = STATIC_LOFT − shaft_lean_impact
    shaft_lean_change = round(iprb - iprf, 1)
    shaft_lean_impact = round(sla + shaft_lean_change, 1)
    loft_angle_address = round(PUTTER_STATIC_LOFT - sla, 1)
    loft_angle_change = round(-shaft_lean_change, 1)
    loft_angle_impact = round(PUTTER_STATIC_LOFT - shaft_lean_impact, 1)
    return Putt(
        index=p1[2],
        timestamp=ts_raw,
        back_stroke_time=bst,
        forward_stroke_time=fst,
        total_stroke_time=round(bst + fst, 2),
        rhythm=round(bst / fst, 2) if fst > 0 else 0.0,
        back_stroke_rotation=bsr,
        forward_stroke_rotation=fsr,
        face_change=round(bsr - fsr, 1),
        club_head_speed=p1[7] * 0.01,
        club_head_accel=round(cha, 2),
        club_head_accel_decel=decel,
        inplane_rotation_back=iprb,
        inplane_rotation_forward=iprf,
        gear_effect_twist=p2[13] * 0.1,
        face_angle_10cm_from_address=round(fa_addr, 1),
        face_angle_10cm_before_impact=round(fa_before, 1),
        face_change_10cm_after_impact=round(fc_after, 1),
        shaft_lean_address=round(sla, 1),
        shaft_lean_impact=shaft_lean_impact,
        shaft_lean_change=shaft_lean_change,
        loft_angle_address=loft_angle_address,
        loft_angle_impact=loft_angle_impact,
        loft_angle_change=loft_angle_change,
        lie_angle_address=round(laa, 1),
        lie_angle_impact=round(lai, 1),
        lie_angle_change=round(lai - laa, 1),
        back_stroke_length_raw=p1[9],
    )


# ---- event bus: background BLE thread -> queue -> SSE clients ----
event_subscribers: set[queue.Queue] = set()
subscribers_lock = threading.Lock()


def broadcast(event: str, **data):
    # Parameter is named `event` (not `kind`) so callers can still pass a
    # `kind=` kwarg in **data without colliding with the positional arg.
    # The emitted SSE message always uses "kind" as the event-type field.
    msg = {"kind": event, **data}
    with subscribers_lock:
        dead = []
        for q in event_subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            event_subscribers.discard(q)


def _extract_and_broadcast_adv(adv) -> None:
    """Parse the VTX advertisement payload and push a battery broadcast.
    Byte 0 of the manufacturer/service data is the battery percent — matches
    the Vertex app's own log format: "Parsed vtx Adv Data → Battery: X%"."""
    if adv is None:
        return
    # The app reads from what looks like service_data by UUID (fff0) or the
    # raw manufacturer bytes.  Try several candidate buffers and take the
    # first that parses as a sensible battery byte (0-100).
    candidates = []
    try:
        for uuid_key, data in (adv.service_data or {}).items():
            candidates.append(("svc", str(uuid_key), bytes(data)))
    except Exception:
        pass
    try:
        for cid, data in (adv.manufacturer_data or {}).items():
            candidates.append(("mfr", f"0x{cid:04x}", bytes(data)))
    except Exception:
        pass
    if not candidates:
        return
    broadcast("log", text=f"adv payloads: {len(candidates)}")
    for kind, tag, data in candidates:
        hex_s = data.hex() if data else ""
        broadcast("log", text=f"  {kind} {tag} ({len(data)}B) {hex_s[:80]}")
    # Byte 0 = battery %, per the blutter-traced parser. Use the first
    # candidate whose byte 0 is in [0, 100].
    for _, _, data in candidates:
        if data and 0 <= data[0] <= 100:
            pct = int(data[0])
            broadcast("battery", percent=pct)
            broadcast("log", text=f"🔋 battery: {pct}%")
            return


class BleWorker:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt: Optional[asyncio.Event] = None
        self.thread: Optional[threading.Thread] = None
        self._running = False
        self._last_adv = None
        # The live BleakClient while connected, so HTTP handlers on other
        # threads can schedule writes onto our asyncio loop.
        self._client = None
        self._hand: int = 1  # byte[5] in commands; default lefty (matches HAND_BYTE)
        # fff2 putt-packet reassembly buffer.  Shared across threads so
        # send_calibration() can drain it around a calibration round-trip —
        # the sensor emits response/diagnostic frames on fff2 during cal that
        # would otherwise mis-align the 6-packet putt grouping.
        self._fff2_pending: list[bytes] = []
        self._cal_in_flight: bool = False

    def send_calibration(self, kind: str, hand: int) -> bool:
        """Schedule a calibration write from any thread. Returns True if the
        request was queued (connection live), False otherwise. kind ∈
        {'face', 'lie'}. hand: 0 = left, 1 = right."""
        if self._loop is None or self._stop_evt is None:
            return False
        if self._client is None:
            return False
        h = 1 if int(hand) else 0
        self._hand = h
        # Command layout verified from a real btsnoop capture of the official
        # Vertex Android app (2026-05-05 cal session):
        #   face: 01 00 00 00 00 H 00 00 00 00   (was incorrectly 0x02 — that
        #         opcode collides with live-mode-on/off and isn't a cal)
        #   lie:  05 00 00 00 00 H 00 00 00 00   (was incorrectly 0x0a)
        # ACK arrives on fff1 ~9s later as `<opcode> 00 00 00 03 00 00 00 e4 44`,
        # status byte at offset 4 = 0x03 means accepted.
        opcode = {"face": 0x01, "lie": 0x05}.get(kind)
        if opcode is None:
            return False
        cmd = bytes([opcode, 0x00, 0x00, 0x00, 0x00, h, 0x00, 0x00, 0x00, 0x00])

        # Shared flag so the fff1 notification handler (if it fires) can
        # cancel the assume-ok timeout.  Keyed by kind so face and lie can
        # run independently.
        pending_attr = f"_pending_cal_{kind}"

        async def _do_write():
            try:
                client = self._client
                if client is None or not client.is_connected:
                    broadcast("log", text=f"⚠ {kind} calibration: not connected")
                    broadcast("cal_status", which=kind, state="error", reason="not_connected")
                    return
                # Gate fff2 so cal response traffic doesn't poison the putt
                # buffer, and drop any stale partial frames sitting there.
                self._cal_in_flight = True
                self._fff2_pending.clear()
                try:
                    broadcast("log", text=f"→ {kind} calibration {cmd.hex()}")
                    broadcast("cal_status", which=kind, state="sending")
                    await client.write_gatt_char(FFF1, cmd, response=True)
                    broadcast("cal_status", which=kind, state="waiting")
                    # The sensor ACKs on fff1 ~9 s after the write (verified via
                    # btsnoop capture of the official app). Wait up to 12 s; the
                    # on_fff1 handler clears the pending flag when the ACK lands.
                    setattr(self, pending_attr, True)
                    for _ in range(120):
                        if not getattr(self, pending_attr, False):
                            break
                        await asyncio.sleep(0.1)
                    if getattr(self, pending_attr, False):
                        setattr(self, pending_attr, False)
                        broadcast("log", text=f"⚠ {kind} calibration: no fff1 ACK after 12 s — sensor may not have accepted the pose")
                        broadcast("cal_status", which=kind, state="fail",
                                  raw="timeout")
                    # No live-mode re-arm. With the correct cal opcodes
                    # (0x01 face, 0x05 lie) the sensor stays in live mode
                    # throughout — the captured official-app session shows
                    # cal commands didn't interrupt fff2/fff3 streaming.
                finally:
                    # Drop whatever leaked onto fff2 during the cal window and
                    # re-open the gate for real putts.
                    self._fff2_pending.clear()
                    self._cal_in_flight = False
            except Exception as e:
                broadcast("log", text=f"⚠ {kind} calibration write failed: {e}")
                broadcast("cal_status", which=kind, state="error", reason=str(e))
                self._fff2_pending.clear()
                self._cal_in_flight = False

        asyncio.run_coroutine_threadsafe(_do_write(), self._loop)
        return True

    def resume_live_mode(self) -> bool:
        """Re-issue the live-mode-on opcode. Called after a full cal flow,
        matching the official app which only re-arms live mode once at the
        end of calibration (not between cal kinds)."""
        if self._loop is None or self._client is None:
            return False
        live_cmd = bytes([0x02, 0x01, 0x00, 0x00, 0x00, self._hand, 0, 0, 0, 0])

        async def _do():
            try:
                client = self._client
                if client is None or not client.is_connected:
                    broadcast("log", text="⚠ resume live: not connected")
                    return
                broadcast("log", text=f"→ resume live mode {live_cmd.hex()}")
                await client.write_gatt_char(FFF1, live_cmd, response=True)
            except Exception as e:
                broadcast("log", text=f"⚠ resume live failed: {e}")

        asyncio.run_coroutine_threadsafe(_do(), self._loop)
        return True

    def running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        if self._loop and self._stop_evt:
            self._loop.call_soon_threadsafe(self._stop_evt.set)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_evt = asyncio.Event()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            self._running = False
            broadcast("connect_state", state="idle")

    async def _main(self):
        try:
            broadcast("connect_state", state="scanning")
            broadcast("log", text="scanning for VTX sensor…")
            dev = None
            for attempt in range(3):
                if self._stop_evt.is_set():
                    return
                dev = await self._scan_for_vtx()
                if dev:
                    break
                broadcast("log", text=f"not seen, retry {attempt+1}/3")
            if dev is None:
                broadcast("log", text="aborting: sensor not advertising")
                return
            broadcast("log", text=f"found {dev.name} ({dev.address})")
            broadcast("connect_state", state="connecting")

            # Two shots at connecting: first with Windows' cached GATT table
            # (fastest, usually works), and if service enumeration comes up
            # empty / unreachable, a second shot that forces a fresh discovery.
            WinRTArgs = None
            try:
                from bleak.args.winrt import WinRTClientArgs as WinRTArgs  # type: ignore
            except ImportError:
                try:
                    from bleak.backends.winrt.client import WinRTClientArgs as WinRTArgs  # type: ignore
                except Exception:
                    pass

            async def connect_and_discover(force_fresh: bool):
                kwargs = {}
                if force_fresh and WinRTArgs is not None:
                    kwargs["winrt"] = WinRTArgs(use_cached_services=False)
                client = BleakClient(dev.address, **kwargs)
                await client.__aenter__()
                try:
                    broadcast("log", text=f"connected, MTU {client.mtu_size}"
                                         f"{' (fresh discovery)' if force_fresh else ''}")

                    # Some peripherals let unpaired clients discover services
                    # but kill the link the moment they try to enable notify.
                    # Attempt a silent (just-works) pair — it's a no-op if the
                    # device doesn't require one, and it stabilises the link
                    # on devices that do.
                    try:
                        paired = await client.pair()
                        broadcast("log", text=f"pair(): {paired}")
                    except Exception as e:
                        broadcast("log", text=f"pair() not supported or failed: {e}")

                    services = []
                    for attempt in range(4):
                        if attempt > 0:
                            await asyncio.sleep(0.25 * attempt)
                        try:
                            services = list(client.services)
                            if services:
                                break
                        except Exception as e:
                            broadcast("log", text=f"services retry {attempt+1}/4: {e}")
                    if not services:
                        raise RuntimeError("GATT services not reachable")
                    return client, services
                except Exception:
                    await client.__aexit__(None, None, None)
                    raise

            # ── Connect + subscribe, with one automatic bond-clear retry ──────
            # On first attempt connect normally.  If the link drops during
            # subscribe (Windows stale-bond symptom), clear the bond via WinRT
            # and retry once — this is the same as manually doing "Remove
            # device" in Windows Bluetooth settings.
            for bond_attempt in range(2):
                if bond_attempt == 1:
                    broadcast("log", text="clearing stale Windows bond…")
                    cleared = await _winrt_unpair(dev.address)
                    broadcast("log", text="bond cleared — reconnecting" if cleared
                              else "⚠ bond clear not supported; retrying anyway")

                try:
                    client, services = await connect_and_discover(force_fresh=False)
                except Exception as e:
                    broadcast("log", text=f"cached discovery failed: {e} — retrying fresh")
                    try:
                        client, services = await connect_and_discover(force_fresh=True)
                    except Exception as e2:
                        if bond_attempt == 0:
                            broadcast("log", text=f"⚠ GATT discovery failing: {e2} — clearing bond and retrying")
                            continue
                        broadcast("log", text=f"⚠ GATT discovery still failing: {e2}")
                        broadcast("log", text="Things to try (in order):")
                        broadcast("log", text="  1. Close the Vertex app on your phone and turn its Bluetooth off")
                        broadcast("log", text="     (the sensor only accepts one central at a time)")
                        broadcast("log", text="  2. Windows → Settings → Bluetooth → VTX sensor → Remove device")
                        broadcast("log", text="  3. Restart Windows Bluetooth: services.msc → Bluetooth Support → Restart")
                        broadcast("log", text="  4. Wake the sensor (press/shake) and try Connect again")
                        return

                _link_dropped = False
                try:
                    self._client = client  # exposed to HTTP handlers via send_calibration
                    self._fff2_pending.clear()

                    def on_fff2(_c, data: bytearray):
                        # During calibration the sensor emits non-putt frames
                        # on fff2; swallow them so they don't mis-align the
                        # 6-packet putt grouping.
                        if self._cal_in_flight:
                            return
                        self._fff2_pending.append(bytes(data))
                        if len(self._fff2_pending) == 6:
                            putt = decode_putt(self._fff2_pending.copy())
                            self._fff2_pending.clear()
                            if putt:
                                broadcast("putt", putt=asdict(putt))

                    def on_fff3(_c, data: bytearray):
                        broadcast("log", text=f"fff3 {len(data)}B")

                    def on_fff1(_c, data: bytearray):
                        """fff1 carries calibration ACKs from the sensor.
                        Verified ACK format (from 2026-05-05 capture):
                            <echo_opcode> 00 00 00 03 00 00 00 e4 44
                        The `e4 44` trailer is a sensor magic constant; byte 4
                        is the status (0x03 = accepted)."""
                        b = bytes(data)
                        broadcast("log", text=f"fff1 {b.hex()}")
                        if len(b) >= 5:
                            cal_kind = {0x01: "face", 0x05: "lie"}.get(b[0])
                            if cal_kind:
                                setattr(self, f"_pending_cal_{cal_kind}", False)
                                ok = (b[4] == 0x03)
                                broadcast("cal_status", which=cal_kind,
                                          state="ok" if ok else "fail",
                                          raw=b.hex())

                    async def subscribe(uuid: str, cb, required: bool = True) -> bool:
                        """start_notify with a connection-aware retry. On 'Not
                        connected' we can't retry the same client — the link is
                        gone — so we surface a clear error instead."""
                        for attempt in (1, 2):
                            if not client.is_connected:
                                broadcast("log", text=f"⚠ link dropped before subscribe {uuid[4:8]}")
                                if required:
                                    raise RuntimeError("sensor dropped the link")
                                return False
                            try:
                                await client.start_notify(uuid, cb)
                                return True
                            except Exception as e:
                                if attempt == 1:
                                    broadcast("log", text=f"subscribe {uuid[4:8]} retry: {e}")
                                    await asyncio.sleep(0.25)
                                    continue
                                if required:
                                    broadcast("log", text=f"⚠ could not subscribe {uuid[4:8]}: {e}")
                                    raise
                                broadcast("log", text=f"optional subscribe {uuid[4:8]} skipped: {e}")
                                return False

                    # Subscribe IMMEDIATELY after services are ready — some
                    # firmware drops the link if we dawdle too long pre-subscribe.
                    await subscribe(FFF2, on_fff2)
                    await subscribe(FFF3, on_fff3)
                    await subscribe(FFF1, on_fff1, required=False)

                    # Now that the subscription is live, dump the GATT table as a
                    # SINGLE multiline log entry (faster than per-line broadcasts).
                    svc_lines = []
                    for svc in services:
                        short = svc.uuid[4:8] if len(svc.uuid) >= 8 else svc.uuid
                        svc_lines.append(f"svc {short}")
                        for ch in svc.characteristics:
                            props = ",".join(ch.properties)
                            svc_lines.append(f"  char {ch.uuid[4:8]} h=0x{ch.handle:04x} [{props}]")
                    broadcast("log", text="GATT table:\n" + "\n".join(svc_lines))

                    ts_cmd = bytes([0x06, 0x00, 0x00, 0x00, ADV_BYTE, HAND_BYTE]) + struct.pack("<I", int(time.time()))
                    broadcast("log", text=f"→ timestamp {ts_cmd.hex()}")
                    await client.write_gatt_char(FFF1, ts_cmd, response=True)
                    await asyncio.sleep(2.0)
                    live_cmd = bytes([0x02, 0x01, 0x00, 0x00, 0x00, HAND_BYTE, 0, 0, 0, 0])
                    broadcast("log", text=f"→ live mode {live_cmd.hex()}")
                    await client.write_gatt_char(FFF1, live_cmd, response=True)
                    broadcast("connect_state", state="connected")

                    await self._stop_evt.wait()
                    broadcast("log", text="disconnecting…")
                    break  # connected and cleanly done — exit bond_attempt loop

                except RuntimeError as e:
                    if bond_attempt == 0 and "dropped the link" in str(e):
                        # Stale Windows bond detected: will auto-clear and retry.
                        _link_dropped = True
                        broadcast("log", text="⚠ link dropped — likely stale Windows bond, will clear and retry")
                    else:
                        if "dropped the link" in str(e):
                            broadcast("log", text="Sensor disconnected us. Things to try:")
                            broadcast("log", text="  1. Restart Windows Bluetooth: services.msc → Bluetooth Support → Restart")
                            broadcast("log", text="  2. Different Bluetooth adapter / USB dongle (some Realtek/Mediatek radios misbehave)")
                            broadcast("log", text="  3. Wake the sensor (move it) and reconnect within ~5 s")
                        raise
                finally:
                    # connect_and_discover manually entered the client context,
                    # so we manually exit it here to disconnect cleanly.
                    self._client = None
                    try:
                        await client.__aexit__(None, None, None)
                    except Exception:
                        pass

                if not _link_dropped:
                    break  # non-retryable exit
        except Exception as e:
            broadcast("log", text=f"error: {e}")

    async def _scan_for_vtx(self):
        """Scan for the VTX sensor AND grab its advertisement data so we can
        extract battery / serial / firmware before connecting."""
        try:
            devices = await BleakScanner.discover(timeout=8.0, return_adv=True)
            # devices is a dict address -> (BLEDevice, AdvertisementData)
            for address, (d, adv) in devices.items():
                if d.name and d.name.startswith(NAME_PREFIX):
                    self._last_adv = adv
                    _extract_and_broadcast_adv(adv)
                    return d
        except TypeError:
            # Older bleak returns just a list of BLEDevice
            devices_list = await BleakScanner.discover(timeout=8.0)
            for d in devices_list:
                if d.name and d.name.startswith(NAME_PREFIX):
                    self._last_adv = None
                    return d
        return None


worker = BleWorker()

# ─── Demo putt generator ──────────────────────────────────────────────
_demo_counter = {"n": 0}


def make_demo_putt() -> Putt:
    """Generate a plausible synthetic putt that varies realistically each call."""
    _demo_counter["n"] += 1
    idx = _demo_counter["n"]
    # Base stroke characteristics with per-putt drift so consistency varies
    bsr = max(2.0, random.gauss(6.2, 1.2))          # back rotation, deg opening
    # face_change is controlled via how closely fsr tracks bsr
    bias = random.gauss(0.0, 0.9)                    # open/closed bias
    fsr = max(0.5, bsr - bias)                       # forward rotation, deg closing
    bst = max(0.4, random.gauss(0.82, 0.08))         # backswing time s
    fst = max(0.3, random.gauss(0.52, 0.06))         # forward time s
    chs = max(0.5, random.gauss(1.30, 0.15))         # club head speed m/s
    cha = max(0.05, random.gauss(1.20, 0.55))        # club head accel magnitude
    decel = random.random() < 0.75                   # 75% decelerating at impact
    laa = max(1.0, random.gauss(3.2, 0.35))          # lie address
    lai = laa + random.gauss(0, 0.25)                # lie impact drifts slightly
    sla = max(0.0, random.gauss(1.2, 0.5))           # shaft lean address
    fa_addr = random.gauss(1.8, 0.3)
    fa_b_imp = random.gauss(2.0, 0.5)
    fc_a_imp = random.gauss(1.7, 0.6)
    get = max(0.0, random.gauss(0.5, 0.3))
    bsl_raw = min(255, max(50, int(random.gauss(220, 30))))
    iprb = max(3.0, random.gauss(11.0, 1.5))
    iprf = max(3.0, random.gauss(11.5, 1.5))
    sla_r = round(sla, 1)
    iprb_r = round(iprb, 1)
    iprf_r = round(iprf, 1)
    shaft_lean_change = round(iprb_r - iprf_r, 1)
    shaft_lean_impact = round(sla_r + shaft_lean_change, 1)
    loft_angle_address = round(PUTTER_STATIC_LOFT - sla_r, 1)
    loft_angle_change = round(-shaft_lean_change, 1)
    loft_angle_impact = round(PUTTER_STATIC_LOFT - shaft_lean_impact, 1)
    return Putt(
        index=idx,
        timestamp=int(time.time()),
        back_stroke_time=round(bst, 2),
        forward_stroke_time=round(fst, 2),
        total_stroke_time=round(bst + fst, 2),
        rhythm=round(bst / fst, 2) if fst > 0 else 0.0,
        back_stroke_rotation=round(bsr, 1),
        forward_stroke_rotation=round(fsr, 1),
        face_change=round(bsr - fsr, 1),
        club_head_speed=round(chs, 2),
        club_head_accel=round(cha, 2),
        club_head_accel_decel=decel,
        inplane_rotation_back=iprb_r,
        inplane_rotation_forward=iprf_r,
        gear_effect_twist=round(get, 1),
        face_angle_10cm_from_address=round(fa_addr, 1),
        face_angle_10cm_before_impact=round(fa_b_imp, 1),
        face_change_10cm_after_impact=round(fc_a_imp, 1),
        shaft_lean_address=sla_r,
        shaft_lean_impact=shaft_lean_impact,
        shaft_lean_change=shaft_lean_change,
        loft_angle_address=loft_angle_address,
        loft_angle_impact=loft_angle_impact,
        loft_angle_change=loft_angle_change,
        lie_angle_address=round(laa, 1),
        lie_angle_impact=round(lai, 1),
        lie_angle_change=round(lai - laa, 1),
        back_stroke_length_raw=bsl_raw,
    )


ASSETS_DIR = HERE / "assets"
MIME_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg":  "image/svg+xml",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".ico":  "image/x-icon",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass  # quiet

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._serve_html()
        elif path == "/events":
            self._serve_sse()
        elif path.startswith("/assets/"):
            self._serve_asset(path[len("/assets/"):])
        else:
            self.send_error(404)

    def _serve_asset(self, rel: str):
        # Guard against path traversal
        if ".." in rel.split("/"):
            self.send_error(400); return
        target = (ASSETS_DIR / rel).resolve()
        try:
            target.relative_to(ASSETS_DIR.resolve())
        except ValueError:
            self.send_error(400); return
        if not target.is_file():
            self.send_error(404); return
        ctype = MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/toggle":
            if worker.running():
                worker.stop()
                action = "disconnecting"
            else:
                worker.start()
                action = "connecting"
            self._send_json({"ok": True, "action": action})
        elif path == "/demo":
            # Generate a synthetic putt and broadcast
            p = make_demo_putt()
            broadcast("putt", putt=asdict(p))
            broadcast("log", text=f"demo putt #{p.index}: FC {p.face_change:+.1f}°  CHS {p.club_head_speed:.2f} m/s")
            self._send_json({"ok": True, "index": p.index})
        elif path == "/demo/burst":
            # Generate 10 demo putts spaced out a bit (non-blocking timing)
            def burst():
                for _ in range(10):
                    p = make_demo_putt()
                    broadcast("putt", putt=asdict(p))
                    time.sleep(0.35)
            threading.Thread(target=burst, daemon=True).start()
            self._send_json({"ok": True, "started": True})
        elif path in ("/calibrate/face", "/calibrate/lie"):
            # Trigger a device-side calibration write on fff1. Expects the
            # hand in the query string: ?hand=L or ?hand=R (defaults to R).
            qs = urlparse(self.path).query or ""
            hand_raw = "L"
            for kv in qs.split("&"):
                if kv.startswith("hand="):
                    hand_raw = kv.split("=", 1)[1]
                    break
            # Per the captured official-app traffic, byte[5] = 0x01 when the
            # user is set to left-handed (matches our HAND_BYTE constant which
            # is the lefty default for this user). Right-hand sends 0x00.
            hand = 1 if hand_raw.upper().startswith("L") else 0
            kind = "face" if path.endswith("/face") else "lie"
            ok = worker.send_calibration(kind, hand)
            if ok:
                self._send_json({"ok": True, "kind": kind, "hand": "L" if hand == 1 else "R"})
            else:
                self._send_json({"ok": False, "error": "not_connected"})
        elif path == "/finish-calibration":
            # Re-arm live mode after a full cal flow. The official Vertex app
            # only re-arms once, at the end — re-arming between face and lie
            # cals drops the sensor out of cal-ready state.
            ok = worker.resume_live_mode()
            self._send_json({"ok": bool(ok)})
        else:
            self.send_error(404)

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        data = HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=200)
        with subscribers_lock:
            event_subscribers.add(q)
        try:
            # Initial sync of current state
            self._send_sse({"kind": "connect_state", "state": "connected" if worker.running() else "idle"})
            while True:
                try:
                    msg = q.get(timeout=15.0)
                    if not self._send_sse(msg):
                        break
                except queue.Empty:
                    if not self._send_sse({"kind": "ping"}):
                        break
        except Exception:
            pass
        finally:
            with subscribers_lock:
                event_subscribers.discard(q)

    def _send_sse(self, msg: dict) -> bool:
        try:
            payload = f"data: {json.dumps(msg)}\n\n".encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


def main():
    if not HTML_PATH.exists():
        print(f"ui.html not found at {HTML_PATH}", file=sys.stderr)
        sys.exit(1)

    # Silence the SSE disconnect noise: when the webview / browser closes
    # an EventSource, the socket aborts mid-read and the stdlib server dumps
    # a full traceback for WinError 10053 / ECONNRESET.  Those are expected
    # client-side disconnects, not real errors.
    class QuietHTTPServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)

    server = QuietHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Vertex Desktop running at {url}")
    print("Press Ctrl+C to stop.")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    webbrowser.open(url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nShutting down…")
        worker.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
