"""Transports (BLE via BlueZ D-Bus, USB serial via termios) and a one-at-a-time command queue.

Everything runs on the GLib main loop; no threads.
"""

from __future__ import annotations

import glob
import os
import termios
from collections import deque
from typing import Callable

from gi.repository import Gio, GLib

from . import protocol as P

BLUEZ = "org.bluez"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_CHAR = "org.bluez.GattCharacteristic1"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
IFACE_OM = "org.freedesktop.DBus.ObjectManager"

# WCH CH9141 transparent UART service
SVC_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
DEFAULT_CHUNK = 20          # bytes per GATT write when the ATT MTU is unknown
REPLY_TIMEOUT_MS = 2500
ARB_TIMEOUT_MS = 30_000


class Transport:
    """Byte pipe. Subclasses call self.on_bytes(data) and self.on_state(connected, message)."""

    def __init__(self) -> None:
        self.on_bytes: Callable[[bytes], None] = lambda b: None
        self.on_state: Callable[[bool, str], None] = lambda c, m: None
        self.connected = False

    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, data: bytes, done: Callable[[], None] | None = None) -> None: ...

    def _set_state(self, connected: bool, msg: str) -> None:
        self.connected = connected
        self.on_state(connected, msg)


# ---------------------------------------------------------------- BLE

class BleTransport(Transport):
    def __init__(self, address: str | None = None) -> None:
        super().__init__()
        self.address = address.upper() if address else None
        self.bus: Gio.DBusConnection | None = None
        self.dev_path: str | None = None
        self.notify_path: str | None = None
        self.write_path: str | None = None
        self.chunk = DEFAULT_CHUNK
        self._queue: deque[tuple[bytes, Callable[[], None] | None]] = deque()
        self._writing = False
        self._sub: int | None = None
        self._retry: int | None = None

    # -- discovery
    def _objects(self) -> dict:
        res = self.bus.call_sync(BLUEZ, "/", IFACE_OM, "GetManagedObjects", None, None,
                                 Gio.DBusCallFlags.NONE, 5000, None)
        return res.unpack()[0]

    def _find_device(self, objs: dict) -> str | None:
        for path, ifaces in objs.items():
            dev = ifaces.get(IFACE_DEVICE)
            if not dev:
                continue
            addr = dev.get("Address", "").upper()
            name = (dev.get("Name") or dev.get("Alias") or "").upper()
            uuids = [u.lower() for u in dev.get("UUIDs", [])]
            if self.address:
                if addr == self.address:
                    return path
            elif name.startswith("BSG") or "PSG" in name or (SVC_UUID in uuids and "CH9141" in str(dev.get("ManufacturerData", ""))):
                return path
        return None

    def open(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._sub = self.bus.signal_subscribe(BLUEZ, IFACE_PROPS, "PropertiesChanged", None, None,
                                                  Gio.DBusSignalFlags.NONE, self._on_props)
            self._connect()
        except GLib.Error as e:
            self._set_state(False, f"Bluetooth error: {e.message}")

    def _connect(self) -> None:
        self._retry = None
        objs = self._objects()
        self.dev_path = self._find_device(objs)
        if not self.dev_path:
            self._set_state(False, "Generator not found. Pair the BSG145 module first (bluetoothctl).")
            self._retry = GLib.timeout_add_seconds(5, self._retry_connect)
            return
        props = objs[self.dev_path][IFACE_DEVICE]
        if props.get("Connected") and props.get("ServicesResolved"):
            self._resolve(objs)
            return
        self._set_state(False, "Connecting over Bluetooth…")
        self.bus.call(BLUEZ, self.dev_path, IFACE_DEVICE, "Connect", None, None,
                      Gio.DBusCallFlags.NONE, 30_000, None, self._on_connect_done)

    def _retry_connect(self) -> bool:
        try:
            self._connect()
        except GLib.Error as e:
            self._set_state(False, f"Bluetooth error: {e.message}")
            self._retry = GLib.timeout_add_seconds(5, self._retry_connect)
        return False

    def _on_connect_done(self, bus, res) -> None:
        try:
            bus.call_finish(res)
        except GLib.Error as e:
            self._set_state(False, f"Connect failed: {e.message}")
            self._retry = GLib.timeout_add_seconds(5, self._retry_connect)
            return
        GLib.timeout_add(300, self._poll_resolved, 0)

    def _poll_resolved(self, n: int) -> bool:
        objs = self._objects()
        if objs.get(self.dev_path, {}).get(IFACE_DEVICE, {}).get("ServicesResolved"):
            self._resolve(objs)
        elif n < 60:
            GLib.timeout_add(300, self._poll_resolved, n + 1)
        else:
            self._set_state(False, "Services not resolved")
            self._retry = GLib.timeout_add_seconds(5, self._retry_connect)
        return False

    def _resolve(self, objs: dict) -> None:
        self.notify_path = self.write_path = None
        for path, ifaces in objs.items():
            ch = ifaces.get(IFACE_CHAR)
            if not ch or not path.startswith(self.dev_path + "/"):
                continue
            uuid = ch.get("UUID", "").lower()
            if uuid == NOTIFY_UUID:
                self.notify_path = path
            elif uuid == WRITE_UUID:
                self.write_path = path
                mtu = int(ch.get("MTU", 0) or 0)
                self.chunk = max(DEFAULT_CHUNK, min(mtu - 3, 180)) if mtu else DEFAULT_CHUNK
        if not (self.notify_path and self.write_path):
            self._set_state(False, "Not a PSG9080 BLE module (FFE1/FFE2 missing)")
            return
        self.bus.call(BLUEZ, self.notify_path, IFACE_CHAR, "StartNotify", None, None,
                      Gio.DBusCallFlags.NONE, 10_000, None, self._on_notify_started)

    def _on_notify_started(self, bus, res) -> None:
        try:
            bus.call_finish(res)
        except GLib.Error as e:
            if "InProgress" not in e.message and "already" not in e.message.lower():
                self._set_state(False, f"Notify failed: {e.message}")
                return
        self._set_state(True, "Connected over Bluetooth")

    def _on_props(self, conn, sender, path, iface, signal, params) -> None:
        name, changed, _ = params.unpack()
        if path == self.notify_path and name == IFACE_CHAR and "Value" in changed:
            self.on_bytes(bytes(changed["Value"]))
        elif path == self.dev_path and name == IFACE_DEVICE and changed.get("Connected") is False:
            self._queue.clear()
            self._writing = False
            self._set_state(False, "Bluetooth disconnected — reconnecting…")
            if self._retry is None:
                self._retry = GLib.timeout_add_seconds(3, self._retry_connect)

    # -- writing (one GATT write in flight; BlueZ rejects overlapping writes)
    def send(self, data: bytes, done=None) -> None:
        for i in range(0, len(data), self.chunk):
            last = i + self.chunk >= len(data)
            self._queue.append((data[i:i + self.chunk], done if last else None))
        self._pump()

    def _pump(self) -> None:
        if self._writing or not self._queue or not self.connected:
            return
        chunk, done = self._queue.popleft()
        self._writing = True
        opts = {"type": GLib.Variant("s", "request")}
        self.bus.call(BLUEZ, self.write_path, IFACE_CHAR, "WriteValue",
                      GLib.Variant("(aya{sv})", (chunk, opts)), None, Gio.DBusCallFlags.NONE, 10_000, None,
                      self._on_written, done)

    def _on_written(self, bus, res, done) -> None:
        self._writing = False
        try:
            bus.call_finish(res)
        except GLib.Error as e:
            self._queue.clear()
            self._set_state(self.connected, f"Write failed: {e.message}")
            return
        if done:
            done()
        self._pump()

    def close(self) -> None:
        if self._retry:
            GLib.source_remove(self._retry)
            self._retry = None
        if self.bus and self.notify_path:
            try:
                self.bus.call_sync(BLUEZ, self.notify_path, IFACE_CHAR, "StopNotify", None, None,
                                   Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
        if self.bus and self._sub is not None:
            self.bus.signal_unsubscribe(self._sub)
            self._sub = None
        self.connected = False


# ---------------------------------------------------------------- USB serial (CH340)

def find_serial_ports() -> list[str]:
    ports = []
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        if "1a86" in p.lower() or "ch34" in p.lower() or "usb-serial" in p.lower():
            ports.append(p)
    return ports or sorted(glob.glob("/dev/ttyUSB*"))


class SerialTransport(Transport):
    BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600,
             115200: termios.B115200}

    def __init__(self, port: str, baud: int = 115200) -> None:
        super().__init__()
        self.port, self.baud = port, baud
        self.fd: int | None = None
        self._watch: int | None = None

    def open(self) -> None:
        try:
            self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0                                        # iflag
            attrs[1] = 0                                        # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0                                        # lflag: raw
            speed = self.BAUDS.get(self.baud, termios.B115200)
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except OSError as e:
            self._set_state(False, f"Cannot open {self.port}: {e.strerror}")
            return
        self._watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.fd, GLib.IOCondition.IN, self._on_readable)
        self._set_state(True, f"Connected on {os.path.basename(self.port)} @ {self.baud}")

    def _on_readable(self, fd, cond) -> bool:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return True
        except OSError as e:
            self._set_state(False, f"Serial error: {e.strerror}")
            self._watch = None
            return False
        if data:
            self.on_bytes(data)
        return True

    def send(self, data: bytes, done=None) -> None:
        if self.fd is None:
            return
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
                view = view[n:]
            except BlockingIOError:
                GLib.usleep(1000)
        if done:
            done()

    def close(self) -> None:
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.connected = False


# ---------------------------------------------------------------- command queue

class Generator:
    """Serialises commands: one in flight, each waits for its ':rNN=' or ':ok' reply."""

    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.state = P.DeviceState()
        self.on_state_changed: Callable[[], None] = lambda: None
        self.on_link: Callable[[bool, str], None] = lambda c, m: None
        self.on_error: Callable[[str], None] = lambda m: None
        self.on_raw: Callable[[str, str], None] = lambda d, s: None     # ('>'|'<', text) for the console
        self._buf = b""
        self._q: deque[tuple[bytes, int, Callable | None]] = deque()
        self._busy: tuple[bytes, int, Callable | None] | None = None
        self._timer: int | None = None
        self.last_fields: list[str] = []
        self.t.on_bytes = self._on_bytes
        self.t.on_state = self._on_link

    def open(self) -> None:
        self.t.open()

    def close(self) -> None:
        self.t.close()

    @property
    def connected(self) -> bool:
        return self.t.connected

    def _on_link(self, connected: bool, msg: str) -> None:
        if not connected:
            self._q.clear()
            self._busy = None
            self._buf = b""
        self.on_link(connected, msg)

    # -- public API
    def read(self, *regs: int, cb: Callable | None = None) -> None:
        for i, r in enumerate(regs):
            self._enqueue(P.read_cmd(r), REPLY_TIMEOUT_MS, cb if i == len(regs) - 1 else None)

    def write(self, reg: int, *values: int, cb: Callable | None = None) -> None:
        self._enqueue(P.write_cmd(reg, *values), REPLY_TIMEOUT_MS, cb)

    def raw(self, line: str, cb: Callable | None = None) -> None:
        line = line.strip()
        if line[:2].lower() == ":w" and line[2:4].isdigit() and int(line[2:4]) in P.FORBIDDEN_WRITES:
            self.on_error(f"w{line[2:4]} is blocked: it changes calibration or rewrites flash")
            return
        self._enqueue((line + "\r\n").encode(), REPLY_TIMEOUT_MS, cb)

    def upload_arb(self, slot: int, points: list[int], cb: Callable | None = None) -> None:
        self._enqueue(P.arb_upload_cmd(slot, points), ARB_TIMEOUT_MS, cb)

    def request(self, data: bytes, cb: Callable[[list[str]], None] | None = None, long: bool = False) -> None:
        """Send a prepared command; cb gets the reply fields (also for ':qNN=' and ':ok')."""
        self._enqueue(data, ARB_TIMEOUT_MS if long else REPLY_TIMEOUT_MS,
                      (lambda: cb(self.last_fields)) if cb else None)

    def pending(self) -> int:
        return len(self._q) + (1 if self._busy else 0)

    # -- internals
    def _enqueue(self, data: bytes, timeout: int, cb) -> None:
        if not self.connected:
            return
        if data[:2].lower() == b":r":
            self._q.append((data, timeout, cb))
        else:
            # writes / uploads go ahead of queued reads (polling must not delay what the user just changed),
            # but keep their order relative to other writes
            i = 0
            for i, item in enumerate(self._q):
                if item[0][:2].lower() == b":r":
                    break
            else:
                i = len(self._q)
            self._q.insert(i, (data, timeout, cb))
        self._next()

    def _next(self) -> None:
        if self._busy or not self._q or not self.connected:
            return
        self._busy = self._q.popleft()
        data, timeout, _ = self._busy
        text = data.decode(errors="replace").strip()
        self.on_raw(">", text if len(text) < 120 else text[:100] + f"… ({len(data)} bytes)")
        self._timer = GLib.timeout_add(timeout, self._on_timeout)
        self.t.send(data)

    def _on_timeout(self) -> bool:
        self._timer = None
        if self._busy:
            self.on_error(f"No reply to {self._busy[0][:20].decode(errors='replace').strip()}")
            self._busy = None
            self._next()
        return False

    def _on_bytes(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if text:
                self._on_line(text)
        # the program read (':qNN=...') ends with a bare CR
        if self._buf.endswith(b"\r") and self._buf[:2] == b":q":
            text, self._buf = self._buf.decode(errors="replace").strip(), b""
            self._on_line(text)

    def _on_line(self, text: str) -> None:
        self.on_raw("<", text)
        try:
            kind, reg, fields = P.parse_reply(text)
        except P.ProtocolError:
            return                       # stray output; keep waiting for the real reply
        self.last_fields = fields
        if kind == "r":
            try:
                self.state.apply(reg, fields)
            except P.ProtocolError as e:
                self.on_error(str(e))
            self.on_state_changed()
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        busy, self._busy = self._busy, None
        if busy and busy[2]:
            busy[2]()
        self._next()
