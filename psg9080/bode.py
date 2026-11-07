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
    def _autoscale(self, ch: int) -> float:
        """Adjust V/div until the signal spans ~2…7 divisions; return Vpp (inf if not measurable)."""
        sc = self.scope
        scale = sc.qfloat(f":CHAN{ch}:SCAL?")
        vpp = float("inf")
        for _ in range(6):
            time.sleep(0.25)
            vpp = sc.qfloat(f":MEAS:ITEM? VPP,CHAN{ch}")
            if vpp >= INVALID or vpp > 7.5 * scale:
                bigger = [s for s in SCALES if s > scale]
                if not bigger:
                    break
                scale = bigger[0]
            elif vpp < 2 * scale:
                target = vpp / 5
                smaller = [s for s in SCALES if target <= s < scale]
                if not smaller:
                    break
                scale = smaller[0]
            else:
                break
            sc.write(f":CHAN{ch}:SCAL {scale:g}")
        return vpp

    def run(self) -> None:
        sc = self.scope
        st = self.gen.state.ch[self.gen_ch - 1]
        saved = (st.freq_mhz, st.freq_unit, st.output, self.gen.state.ch[0].output, self.gen.state.ch[1].output)
        result = "Sweep finished"
        try:
            self._status("Setting up scope and generator…")
            for ch in (self.in_ch, self.out_ch):
                sc.write(f":CHAN{ch}:DISP ON")
            sc.write(f":TRIG:EDGE:SOUR CHAN{self.in_ch}")
            sc.write(":TRIG:SWE AUTO")
            sc.write(":RUN")
            self._gen(15 + self.gen_ch - 1, P.ampl_to_reg(self.ampl))
            outs = [self.gen.state.ch[0].output, self.gen.state.ch[1].output]
            outs[self.gen_ch - 1] = True
            self._gen(10, int(outs[0]), int(outs[1]))
            for i, f in enumerate(self.freqs):
                if self.stop_evt.is_set():
                    result = "Sweep stopped"
                    break
                self._status(f"{i + 1}/{len(self.freqs)}  {P.format_freq(int(f * 1000))}")
                self._gen(13 + self.gen_ch - 1, P.freq_to_reg(f), st.freq_unit)
                sc.write(f":TIM:SCAL {max(5e-9, min(50.0, 2 / (f * 10))):.3e}")   # ~2 periods on screen
                time.sleep(0.3)
                vin = self._autoscale(self.in_ch)
                vout = self._autoscale(self.out_ch)
                time.sleep(0.2)
                ph = sc.qfloat(f":MEAS:ITEM? RRPH,CHAN{self.out_ch},CHAN{self.in_ch}")
                phase = None if abs(ph) >= INVALID or not math.isfinite(ph) else ph
                if 0 < vin < INVALID and 0 < vout < INVALID:
                    pt = BodePoint(f, vin, vout, 20 * math.log10(vout / vin), phase)
                    GLib.idle_add(self.on_point, pt)
        except Exception as e:                          # noqa: BLE001 - report any failure to the UI
            result = f"Sweep failed: {e}"
        finally:
            try:
                # restore the generator: frequency and output states as they were before the sweep
                self._gen(13 + self.gen_ch - 1, saved[0], saved[1])
                self._gen(10, int(saved[3]), int(saved[4]))
            except Exception as e:                      # noqa: BLE001
                result += f" (restore failed: {e})"
            GLib.idle_add(self.on_done, result)


def to_csv(points: list[BodePoint]) -> str:
    rows = ["freq_hz,vin_vpp,vout_vpp,gain_db,phase_deg"]
    for p in points:
        rows.append(f"{p.freq:.6g},{p.vin:.6g},{p.vout:.6g},{p.gain_db:.4f},"
                    f"{'' if p.phase_deg is None else f'{p.phase_deg:.3f}'}")
    return "\n".join(rows) + "\n"
