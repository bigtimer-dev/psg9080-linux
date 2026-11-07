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


if __name__ == "__main__":
    unittest.main()
