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
    """Minimal DHO SCPI server: CH1 = generator, CH2 = RC low-pass output."""

    def __init__(self, gen):
        super().__init__(daemon=True)
        self.gen = gen
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.scale = {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
        self.disp = set()

    def _vpp(self, ch):
        c = self.gen.state.ch[0]
        f, a = c.freq_mhz / 1000, c.ampl_mv / 1000 if c.output else 0.0
        v = a if ch == 1 else a / math.sqrt(1 + (f / FC) ** 2)
        return 9.9e37 if ch not in self.disp or v > 8 * self.scale[ch] else v

    def run(self):
        conn, _ = self.srv.accept()
        f = conn.makefile("rb")
        for raw in f:
            cmd = raw.decode().strip()
            reply = None
            if cmd == "*IDN?":
                reply = "FAKE,DHO,0,1"
            elif cmd.endswith(":DISP ON"):
                self.disp.add(int(cmd[5]))
            elif cmd.endswith(":SCAL?") and cmd.startswith(":CHAN"):
                reply = f"{self.scale[int(cmd[5])]:e}"
            elif cmd.startswith(":CHAN") and ":SCAL " in cmd:
                self.scale[int(cmd[5])] = float(cmd.split()[1])
            elif cmd.startswith(":MEAS:ITEM? VPP"):
                reply = f"{self._vpp(int(cmd[-1])):e}"
            elif cmd.startswith(":MEAS:ITEM? RRPH"):
                fr = self.gen.state.ch[0].freq_mhz / 1000
                reply = f"{-math.degrees(math.atan(fr / FC)):e}"
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
        scope.close()
        self.assertEqual(done, ["Sweep finished"])
        self.assertEqual(len(pts), 9)
        for p in pts:
            expected = -10 * math.log10(1 + (p.freq / FC) ** 2)
            self.assertAlmostEqual(p.gain_db, expected, places=3)
            self.assertAlmostEqual(p.phase_deg, -math.degrees(math.atan(p.freq / FC)), places=3)
        # generator restored: frequency back to 1 kHz, outputs back off
        self.assertEqual(gen.state.ch[0].freq_mhz, 1_000_000)
        self.assertEqual(gen.log[-1], (10, (0, 0)))
        csv = B.to_csv(pts)
        self.assertTrue(csv.startswith("freq_hz,") and csv.count("\n") == 10)


if __name__ == "__main__":
    unittest.main()
