"""Bode plot: step the PSG9080 frequency and measure gain/phase on a Rigol DHO (SCPI over LAN, port 5555).

The sweep runs in a worker thread. Generator commands are handed to the GLib main loop (the Generator object
is not thread-safe) and the worker waits for the ':ok'. Scope access uses one persistent socket with short
pauses between commands: the DHO800/900 SCPI server restarts if hammered with connections, and querying a
measurement on a channel that is switched off made it drop the connection in testing.
"""

from __future__ import annotations

import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib

from . import protocol as P

SCPI_PORT = 5555
INVALID = 9.0e37            # Rigol returns 9.9E37 for "no valid measurement"
SCALES = [s * 10 ** e for e in range(-3, 2) for s in (1, 2, 5)]     # V/div: 1 mV … 50 V


@dataclass
class BodePoint:
    freq: float
    vin: float
    vout: float
    gain_db: float
    phase_deg: float | None


def log_freqs(start: float, stop: float, per_decade: int) -> list[float]:
    if start <= 0 or stop <= start or per_decade < 1:
        raise ValueError("need 0 < start < stop and at least 1 point per decade")
    n = max(2, int(round(math.log10(stop / start) * per_decade)) + 1)
    return [start * (stop / start) ** (i / (n - 1)) for i in range(n)]


class Scope:
    def __init__(self, host: str, port: int = SCPI_PORT, timeout: float = 5.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self.f = None
        self.lock = threading.Lock()

    def connect(self) -> str:
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.f = self.sock.makefile("rb")
        return self.query("*IDN?")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = self.f = None

    def write(self, cmd: str) -> None:
        with self.lock:
            self.sock.sendall((cmd + "\n").encode())
            time.sleep(0.05)

    def query(self, cmd: str) -> str:
        with self.lock:
            self.sock.sendall((cmd + "\n").encode())
            line = self.f.readline()
            time.sleep(0.03)
        if not line:
            raise ConnectionError("scope closed the connection")
        return line.decode(errors="replace").strip()

    def qfloat(self, cmd: str) -> float:
        try:
            return float(self.query(cmd))
        except ValueError:
            return float("inf")

    def block(self, cmd: str) -> bytes:
        """Query returning an IEEE-488.2 definite-length block (#<n><len><data>)."""
        with self.lock:
            self.sock.sendall((cmd + "\n").encode())
            if self.f.read(1) != b"#":
                raise ConnectionError(f"no data block for {cmd}")
            nd = int(self.f.read(1))
            n = int(self.f.read(nd))
            data = self.f.read(n)
            self.f.readline()
            time.sleep(0.03)
        return data

    def tone(self, ch: int, freq: float) -> "Tone":
        """Download the displayed waveform of a channel and measure the component at freq (single-bin DFT).

        :MEAS:ITEM? makes this firmware's SCPI server drop the connection, so measurements are done here."""
        self.write(f":WAV:SOUR CHAN{ch}")
        self.write(":WAV:MODE NORM")
        self.write(":WAV:FORM BYTE")
        pre = [float(x) for x in self.query(":WAV:PRE?").split(",")]
        xinc, yinc, yorg, yref = pre[4], pre[7], pre[8], pre[9]
        raw = self.block(":WAV:DATA?")
        return Tone.analyse(raw, xinc, yinc, yorg, yref, freq, self.qfloat(f":CHAN{ch}:SCAL?"))

    def save_state(self, chans) -> dict:
        st = {"tb": self.query(":TIM:SCAL?"), "trig": self.query(":TRIG:EDGE:SOUR?"),
              "sweep": self.query(":TRIG:SWE?"), "chans": {}}
        for ch in chans:
            st["chans"][ch] = (self.query(f":CHAN{ch}:DISP?"), self.query(f":CHAN{ch}:SCAL?"))
        return st

    def restore_state(self, st: dict) -> None:
        for ch, (disp, scale) in st["chans"].items():
            self.write(f":CHAN{ch}:DISP 1")          # scale only sticks while the channel is shown
            self.write(f":CHAN{ch}:SCAL {scale}")
            self.write(f":CHAN{ch}:DISP {disp}")
        self.write(f":TIM:SCAL {st['tb']}")
        self.write(f":TRIG:EDGE:SOUR {st['trig']}")
        self.write(f":TRIG:SWE {st['sweep']}")
        self.write(":RUN")


@dataclass
class Tone:
    vpp: float          # 2 x amplitude of the fundamental at the test frequency
    phase: float        # degrees, relative to the start of the record
    pk_pk: float        # raw min..max in volts
    span_div: float     # raw min..max in divisions
    clipped: bool

    @staticmethod
    def analyse(raw: bytes, xinc: float, yinc: float, yorg: float, yref: float, freq: float,
                scale: float) -> "Tone":
        n = len(raw)
        if n < 16:
            raise ValueError("waveform too short")
        clipped = sum(1 for b in raw if b in (0, 255)) > n // 200
        v = [(b - yorg - yref) * yinc for b in raw]
        # use a whole number of periods (avoids leakage when the record is not an exact multiple)
        per = 1 / (freq * xinc)
        m = int(int(n / per) * per) if n >= per else n
        m = max(16, m)
        w = 2 * math.pi * freq * xinc
        re = sum(v[i] * math.cos(w * i) for i in range(m)) * 2 / m
        im = sum(v[i] * math.sin(w * i) for i in range(m)) * 2 / m
        pk = max(v) - min(v)
        return Tone(2 * math.hypot(re, im), math.degrees(math.atan2(-im, re)), pk,
                    pk / scale if scale else 0.0, clipped)


class BodeSweep(threading.Thread):
    def __init__(self, gen, scope: Scope, *, gen_ch: int, in_ch: int, out_ch: int, freqs: list[float],
                 ampl_vpp: float, on_point: Callable[[BodePoint], None], on_status: Callable[[str], None],
                 on_done: Callable[[str], None]) -> None:
        super().__init__(daemon=True)
        self.gen, self.scope = gen, scope
        self.gen_ch, self.in_ch, self.out_ch = gen_ch, in_ch, out_ch
        self.freqs, self.ampl = freqs, ampl_vpp
        self.on_point, self.on_status, self.on_done = on_point, on_status, on_done
        self.stop_evt = threading.Event()
        self.scales: dict[int, float] = {}
        self.settle = 0.4           # seconds of acquisition after each change before the capture

    # -- generator calls marshalled onto the main loop
    def _gen(self, reg: int, *vals: int, timeout: float = 5.0) -> None:
        ev = threading.Event()
        GLib.idle_add(lambda: (self.gen.write(reg, *vals, cb=ev.set), False)[1])
        if not ev.wait(timeout):
            raise TimeoutError(f"generator did not acknowledge w{reg:02d}")

    def _status(self, msg: str) -> None:
        GLib.idle_add(self.on_status, msg)

    def stop(self) -> None:
        self.stop_evt.set()

    # -- scope helpers
    def _capture(self, f: float) -> dict[int, Tone]:
        """Run, let it settle, stop, and analyse the same acquisition on both channels."""
        sc = self.scope
        chans = sorted({self.in_ch, self.out_ch})
        for _ in range(6):
            sc.write(":RUN")
            time.sleep(self.settle)
            sc.write(":STOP")
            time.sleep(0.1)
            tones = {ch: sc.tone(ch, f) for ch in chans}
            changed = False
            for ch, t in tones.items():
                scale = self.scales[ch]
                if t.clipped:
                    bigger = [s for s in SCALES if s > scale * 1.01]
                    if bigger:
                        self.scales[ch], changed = bigger[0], True
                elif t.span_div < 2.5:
                    target = t.pk_pk / 6
                    smaller = [s for s in SCALES if target <= s < scale * 0.99]
                    if smaller:
                        self.scales[ch], changed = smaller[0], True
                if changed and self.scales[ch] != scale:
                    sc.write(f":CHAN{ch}:SCAL {self.scales[ch]:g}")
            if not changed:
                return tones
        return tones

    def run(self) -> None:
        sc = self.scope
        st = self.gen.state.ch[self.gen_ch - 1]
        saved_gen = (st.freq_mhz, st.freq_unit, st.ampl_mv, self.gen.state.ch[0].output, self.gen.state.ch[1].output)
        saved_scope = sc.save_state({self.in_ch, self.out_ch})
        result = "Sweep finished"
        try:
            self._status("Setting up scope and generator…")
            for ch in sorted({self.in_ch, self.out_ch}):
                sc.write(f":CHAN{ch}:DISP 1")
                # start coarse: guaranteed not to clip, autoscale refines it
                self.scales[ch] = min((s for s in SCALES if s >= self.ampl / 4), default=SCALES[-1])
                sc.write(f":CHAN{ch}:SCAL {self.scales[ch]:g}")
            sc.write(f":TRIG:EDGE:SOUR CHAN{self.in_ch}")
            sc.write(":TRIG:SWE AUTO")
            self._gen(15 + self.gen_ch - 1, P.ampl_to_reg(self.ampl))
            outs = [self.gen.state.ch[0].output, self.gen.state.ch[1].output]
            outs[self.gen_ch - 1] = True
            self._gen(10, int(outs[0]), int(outs[1]))
            same = self.in_ch == self.out_ch
            for i, f in enumerate(self.freqs):
                if self.stop_evt.is_set():
                    result = "Sweep stopped"
                    break
                self._status(f"{i + 1}/{len(self.freqs)}  {P.format_freq(int(f * 1000))}")
                self._gen(13 + self.gen_ch - 1, P.freq_to_reg(f), st.freq_unit)
                sc.write(f":TIM:SCAL {max(5e-9, min(50.0, 3 / (f * 10))):.3e}")   # ~3 periods on screen
                tones = self._capture(f)
                tin, tout = tones[self.in_ch], tones[self.out_ch]
                vout = tout.vpp
                vin = self.ampl if same else tin.vpp           # one channel: compare with the set amplitude
                phase = None if same else (tout.phase - tin.phase + 180) % 360 - 180
                if vin > 0 and vout > 0:
                    GLib.idle_add(self.on_point, BodePoint(f, vin, vout, 20 * math.log10(vout / vin), phase))
        except Exception as e:                          # noqa: BLE001 - report any failure to the UI
            result = f"Sweep failed: {e}"
        finally:
            try:
                self._gen(10, int(saved_gen[3]), int(saved_gen[4]))
                self._gen(13 + self.gen_ch - 1, saved_gen[0], saved_gen[1])
                self._gen(15 + self.gen_ch - 1, saved_gen[2])
            except Exception as e:                      # noqa: BLE001
                result += f" (generator restore failed: {e})"
            try:
                sc.restore_state(saved_scope)
            except Exception as e:                      # noqa: BLE001
                result += f" (scope restore failed: {e})"
            GLib.idle_add(self.on_done, result)


def to_csv(points: list[BodePoint]) -> str:
    rows = ["freq_hz,vin_vpp,vout_vpp,gain_db,phase_deg"]
    for p in points:
        rows.append(f"{p.freq:.6g},{p.vin:.6g},{p.vout:.6g},{p.gain_db:.4f},"
                    f"{'' if p.phase_deg is None else f'{p.phase_deg:.3f}'}")
    return "\n".join(rows) + "\n"
