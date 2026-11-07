import sys
import unittest

sys.path.insert(0, ".")
import gi  # noqa: E402

gi.require_version("GLib", "2.0")
from psg9080.link import Generator, Transport  # noqa: E402


class FakeTransport(Transport):
    def __init__(self):
        super().__init__()
        self.connected = True
        self.sent = []

    def send(self, data, done=None):
        self.sent.append(data)


class QueueTest(unittest.TestCase):
    def test_writes_jump_ahead_of_queued_reads(self):
        t = FakeTransport()
        g = Generator(t)
        g.read(10, 11, 12)            # r10 goes out immediately, r11/r12 wait
        g.write(15, 1000)
        g.write(16, 2000)
        queued = [item[0] for item in g._q]
        self.assertEqual(queued, [b":w15=1000.\r\n", b":w16=2000.\r\n", b":r11=0.\r\n", b":r12=0.\r\n"])
        self.assertEqual(t.sent, [b":r10=0.\r\n"])

    def test_program_reply_ending_in_bare_cr(self):
        from psg9080 import protocol as P
        t = FakeTransport()
        g = Generator(t)
        got = []
        g.request(P.program_read_cmd(3), got.append, long=True)
        vals = ["5", "10", "1", "0"] + ["0"] * 396
        reply = (":q03=" + ",".join(vals) + ",\r").encode()
        for i in range(0, len(reply), 20):           # BLE delivers it in pieces
            g._on_bytes(reply[i:i + 20])
        self.assertEqual(len(got), 1)
        steps = P.parse_program(got[0])
        self.assertEqual(steps[0], P.ProgramStep(5, 10, 1, 0))
        self.assertIsNone(g._busy)


if __name__ == "__main__":
    unittest.main()
