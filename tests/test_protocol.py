import unittest

from psg9080 import protocol as P


class ProtocolTest(unittest.TestCase):


    def test_read_write_frames(self):
        assert P.read_cmd(13) == b":r13=0.\r\n"
        assert P.write_cmd(14, 12345678, 0) == b":w14=12345678,0.\r\n"


    def test_forbidden_writes(self):
        for reg in (97, 98):
            with self.assertRaises(P.ProtocolError):
                P.write_cmd(reg, 1)


    def test_parse_and_apply_real_replies(self):
        s = P.DeviceState()
        for line in [":r00=80.", ":r01=2490800145.", ":r10=0,1.", ":r11=000.", ":r13=000001000000,1.",
                     ":r14=000010000000,0.", ":r15=08500.", ":r17=1000.", ":r19=5000.", ":r22=09000."]:
            kind, reg, fields = P.parse_reply(line)
            assert kind == "r"
            s.apply(reg, fields)
        assert P.parse_reply(":ok")[0] == "ok"
        c1, c2 = s.ch
        assert s.model == "80" and s.serial == "2490800145"
        assert (c1.output, c2.output) == (False, True)
        assert c1.freq_mhz == 1_000_000 and c1.freq_unit == 1 and c2.freq_mhz == 10_000_000
        assert c1.ampl_mv == 8500 and c1.offset_v == 0 and c1.duty_pct == 50 and c2.phase_deg == 90
        assert c1.wave_name == "Sine"


    def test_conversions(self):
        assert P.freq_to_reg(1000) == 1_000_000
        assert P.freq_to_reg(1e9) == P.FREQ_MAX_MHZ
        assert P.offset_to_reg(0) == 1000 and P.offset_to_reg(-1.5) == 850 and P.offset_to_reg(99) == 2500
        assert P.offset_to_reg(-50) == P.OFFSET_MIN
        assert P.duty_to_reg(50) == 5000 and P.phase_to_reg(450) == 9000
        assert P.format_freq(1_000_000) == "1 kHz" and P.format_freq(12_345_678) == "12.345678 kHz"


    def test_arb(self):
        norm = P.arb_from_expression("sin(x)")
        pts = P.arb_to_points(norm)
        assert len(pts) == 2048 and min(pts) >= 0 and max(pts) == P.ARB_MAX
        frame = P.arb_upload_cmd(1, pts)
        assert frame.startswith(b":a01=") and frame.endswith(b".\r\n") and frame.count(b",") == 2047
        with self.assertRaises(P.ProtocolError):
            P.arb_from_expression("__import__('os')")
        with self.assertRaises(P.ProtocolError):
            P.arb_upload_cmd(0, pts)


    def test_parse_freq(self):
        from psg9080.window import parse_freq
        assert parse_freq("1k") == 1000 and parse_freq("2.5 MHz") == 2.5e6 and parse_freq("440") == 440
        assert parse_freq("250 mHz") == 0.25 and parse_freq("1'000.5 Hz") == 1000.5 and parse_freq("abc") is None


if __name__ == '__main__':
    unittest.main()


class SweepTest(unittest.TestCase):
    def test_apply_live_values(self):
        st = P.DeviceState()
        for line in (":r64=0,00300,0,1.", ":r65=1,1.", ":r66=0000012340.", ":r67=0000056780."):
            _, reg, fields = P.parse_reply(line)
            st.apply(reg, fields)
        sw = st.sweep
        self.assertEqual((sw.channel, sw.time_cs, sw.direction, sw.log, sw.running), (0, 300, 0, True, True))
        self.assertEqual((sw.start_dhz, sw.end_dhz), (12340, 56780))
        self.assertEqual(P.write_cmd(64, *P.sweep_config_cmd(sw)), b":w64=0,300,0,1.\r\n")
        self.assertEqual(P.hz_to_dhz(1234.0), 12340)


class ModulationTest(unittest.TestCase):
    def test_apply_live_values(self):
        st = P.DeviceState()
        for line in (":r24=00,01,00,12.", ":r40=0,0.", ":r43=0000300000.", ":r45=0700.", ":r47=0000020000.",
                     ":r50=0000050000.", ":r52=1800."):
            _, reg, fields = P.parse_reply(line)
            st.apply(reg, fields)
        self.assertEqual(st.screen, P.SCREEN_MOD)
        m1, m2 = st.mod
        self.assertEqual((m1.type, m1.freq_mhz, m1.depth, m1.fm_dev_dhz), (0, 300000, 700, 20000))
        self.assertEqual((m2.hop_dhz, m2.pm_dev), (50000, 1800))

    def test_burst(self):
        st = P.DeviceState()
        for line in (":r40=7,0.", ":r58=0,2.", ":r60=0,0.", ":r61=0000000007,0000000001."):
            _, reg, fields = P.parse_reply(line)
            st.apply(reg, fields)
        self.assertEqual((st.mod[0].type, st.mod[0].burst_count, st.mod[1].burst_count), (7, 7, 1))
        self.assertEqual((st.mod[0].burst_idle, st.mod[1].burst_idle), (0, 2))
        self.assertEqual(P.write_cmd(74, *P.BURST_TRIGGER), b":w74=1,0,0,0.\r\n")

    def test_pulse_mode(self):
        st = P.DeviceState()
        for line in (":r24=00,01,00,08.", ":r40=6,0.", ":r53=0000005000.", ":r55=0000002000.", ":r57=1,0."):
            _, reg, fields = P.parse_reply(line)
            st.apply(reg, fields)
        m = st.mod[0]
        self.assertEqual((m.pulse_width_ns, m.pulse_period_10ns, m.pulse_inverted), (5000, 2000, True))
        self.assertEqual(st.screen, P.mod_screen(6))
        self.assertEqual(P.mod_screen(0), P.SCREEN_MOD)

    def test_am_depth_definition(self):
        # measured on the scope: 70 % set -> 53.5 %, 100 % -> 99.7 %
        self.assertAlmostEqual(P.am_textbook_depth(0.7), 0.538, places=3)
        self.assertEqual(P.am_textbook_depth(1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
