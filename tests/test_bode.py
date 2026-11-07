import math
import socket
import threading
import unittest
from unittest import mock

from gi.repository import GLib

from psg9080 import bode as B
from psg9080 import protocol as P

FC = 10_000.0       # simulated RC low-pass corner


class FakeScope(threading.Thread):
    """Minimal DHO SCPI server: CH1 = generator, CH2 = RC low-pass output. Serves 1000-point byte waveforms."""

    def __init__(self, gen):
        super().__init__(daemon=True)
        self.gen = gen
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.scale = {1: 1.0, 2: 1.0, 3: 0.05, 4: 0.05}
        self.disp = {1: 1, 2: 0, 3: 0, 4: 0}
        self.tb, self.src, self.trig, self.sweep = 5e-6, 1, "CHAN1", "AUTO"

    def _wave(self, ch):
        c = self.gen.state.ch[0]
        f, a = c.freq_mhz / 1000, (c.ampl_mv / 1000 if c.output else 0.0)
        amp, ph = a / 2, 0.0
        if ch == 2:
            amp /= math.sqrt(1 + (f / FC) ** 2)
            ph = -math.atan(f / FC)
        xinc = self.tb * 10 / 1000
        yinc = self.scale[ch] * 8 / 256
        data = bytes(max(0, min(255, int(round(128 + amp * math.sin(2 * math.pi * f * i * xinc + 0.3 + ph) / yinc))))
                     for i in range(1000))
        return data, xinc, yinc

    def run(self):
        conn, _ = self.srv.accept()
        f = conn.makefile("rb")
        for raw in f:
            cmd = raw.decode().strip()
            reply = None
            if cmd == "*IDN?":
                reply = "FAKE,DHO,0,1"
            elif cmd.startswith(":CHAN") and ":DISP" in cmd:
                ch = int(cmd[5])
                if cmd.endswith("?"):
                    reply = str(self.disp[ch])
                else:
                    self.disp[ch] = int(cmd.split()[1])
            elif cmd.startswith(":CHAN") and ":SCAL" in cmd:
                ch = int(cmd[5])
                if cmd.endswith("?"):
                    reply = f"{self.scale[ch]:e}"
                elif self.disp[ch]:
                    self.scale[ch] = float(cmd.split()[1])
            elif cmd == ":TIM:SCAL?":
                reply = f"{self.tb:e}"
            elif cmd.startswith(":TIM:SCAL "):
                self.tb = float(cmd.split()[1])
            elif cmd == ":TRIG:EDGE:SOUR?":
                reply = self.trig
            elif cmd.startswith(":TRIG:EDGE:SOUR "):
                self.trig = cmd.split()[1]
            elif cmd == ":TRIG:SWE?":
                reply = self.sweep
            elif cmd.startswith(":WAV:SOUR CHAN"):
                self.src = int(cmd[-1])
            elif cmd == ":WAV:PRE?":
                _, xinc, yinc = self._wave(self.src)
                reply = f"0,0,1000,1,{xinc:e},0,0,{yinc:e},0,128"
            elif cmd == ":WAV:DATA?":
                data, _, _ = self._wave(self.src)
                conn.sendall(b"#41000" + data + b"\n")
            elif cmd.startswith(":MEAS"):
                conn.close()            # like the real firmware: never send measurement queries
                return
            if reply is not None:
                conn.sendall((reply + "\n").encode())


class FakeGen:
    def __init__(self):
        self.state = P.DeviceState()
        self.state.ch[0].freq_mhz = 1_000_000
        self.log = []

    def write(self, reg, *vals, cb=None):
        self.log.append((reg, vals))
        self.state.apply(reg, [str(v) for v in vals] if reg != 10 else [str(v) for v in vals])
        if cb:
            GLib.idle_add(cb)


class BodeTest(unittest.TestCase):
    def test_log_freqs(self):
        f = B.log_freqs(100, 1e6, 10)
        self.assertEqual(len(f), 41)
        self.assertAlmostEqual(f[0], 100)
        self.assertAlmostEqual(f[-1], 1e6)
        with self.assertRaises(ValueError):
            B.log_freqs(0, 10, 5)

    def test_sweep_against_simulated_rc(self):
        gen = FakeGen()
        fake = FakeScope(gen)
        fake.start()
        scope = B.Scope("127.0.0.1", fake.port)
        scope.connect()
        pts, done = [], []
        loop = GLib.MainLoop()
        sw = B.BodeSweep(gen, scope, gen_ch=1, in_ch=1, out_ch=2, freqs=B.log_freqs(1e3, 1e5, 4), ampl_vpp=2.0,
                         on_point=lambda p: pts.append(p), on_status=lambda m: None,
                         on_done=lambda m: (done.append(m), loop.quit()))
        with mock.patch.object(B.time, "sleep", lambda s: None):     # no real pauses in the test
            sw.start()
            GLib.timeout_add_seconds(20, loop.quit)
            loop.run()
        scope.query("*IDN?")              # the fake scope handles commands in order: flush
        scope.close()
        self.assertEqual(done, ["Sweep finished"])
        self.assertEqual(len(pts), 9)
        for p in pts:
            expected = -10 * math.log10(1 + (p.freq / FC) ** 2)
            self.assertAlmostEqual(p.gain_db, expected, delta=0.15)       # 8-bit samples
            self.assertAlmostEqual(p.phase_deg, -math.degrees(math.atan(p.freq / FC)), delta=1.0)
        # generator restored: frequency, amplitude and outputs as before
        self.assertEqual(gen.state.ch[0].freq_mhz, 1_000_000)
        self.assertEqual(gen.state.ch[0].ampl_mv, 5000)
        self.assertFalse(gen.state.ch[0].output or gen.state.ch[1].output)
        # scope restored
        self.assertEqual((fake.disp, fake.scale, fake.tb), ({1: 1, 2: 0, 3: 0, 4: 0},
                                                            {1: 1.0, 2: 1.0, 3: 0.05, 4: 0.05}, 5e-6))
        csv = B.to_csv(pts)
        self.assertTrue(csv.startswith("freq_hz,") and csv.count("\n") == 10)


if __name__ == "__main__":
    unittest.main()
