"""Main window: two channel panels, arbitrary waveform designer, console."""

from __future__ import annotations

import math
import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import protocol as P  # noqa: E402
from .link import Generator  # noqa: E402

POLL_MS = 1500
WRITE_DEBOUNCE_MS = 350
CH_COLORS = [(0.98, 0.84, 0.25), (0.35, 0.80, 0.95)]      # CH1 yellow, CH2 cyan like the front panel


def parse_freq(text: str) -> float | None:
    """'1k', '1.5 kHz', '80MHz', '250 mHz' (millihertz needs exact case 'mHz') -> Hz."""
    t = text.strip().replace("'", "").replace("_", "").replace(",", ".")
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Zµ]*)\s*$", t)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2)
    scale = {"": 1, "hz": 1, "k": 1e3, "khz": 1e3, "M": 1e6, "MHz": 1e6, "Mhz": 1e6, "mHz": 1e-3,
             "uHz": 1e-6, "µHz": 1e-6}
    if unit in scale:
        return val * scale[unit]
    low = unit.lower()
    if low in ("hz", "khz"):
        return val * scale[low]
    if low == "mhz":            # ambiguous casing: treat as MHz (what people usually mean)
        return val * 1e6
    return None


def wave_sample(wave: int, t: float, duty: float) -> float:
    """Rough preview of the built-in shapes, t in [0,1), result -1..1."""
    t %= 1.0
    if wave == 0:
        return math.sin(2 * math.pi * t)
    if wave in (1, 5):
        return 1.0 if t < 0.5 else -1.0
    if wave == 2:
        return 1.0 if t < duty else -1.0
    if wave == 3:
        return 1 - 4 * abs(t - 0.5)
    if wave == 4:
        return 2 * t - 1
    if wave == 6:
        return 0.6
    if wave == 8:
        return max(0.0, math.sin(2 * math.pi * t)) * 2 - 1
    if wave == 9:
        return abs(math.sin(2 * math.pi * t)) * 2 - 1
    if wave == 10:
        return math.floor(t * 6) / 2.5 - 1
    if wave == 11:
        return 1 - math.floor(t * 6) / 2.5
    if wave == 14:
        return math.sin(t * 9871.3) * math.cos(t * 3127.1)
    if wave == 15:
        return (math.exp(4 * t) - 1) / (math.e ** 4 - 1) * 2 - 1
    if wave == 16:
        return (math.exp(4 * (1 - t)) - 1) / (math.e ** 4 - 1) * 2 - 1
    if wave == 17:
        return math.log1p(t * 50) / math.log(51) * 2 - 1
    if wave == 18:
        return math.log1p((1 - t) * 50) / math.log(51) * 2 - 1
    if wave == 19:
        x = (t - 0.5) * 24
        return math.sin(x) / x if x else 1.0
    if wave == 20:
        return (math.sin(2 * math.pi * t) + math.sin(6 * math.pi * t) / 2 + math.sin(10 * math.pi * t) / 3) / 1.4
    if wave == 21:
        x = (t - 0.5) * 20
        return 2 / (1 + x * x) - 1
    return math.sin(2 * math.pi * t)


def draw_trace(cr, w, h, samples, rgb, grid=True) -> None:
    if grid:
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.set_line_width(1)
        for i in range(1, 8):
            cr.move_to(w * i / 8 + 0.5, 0)
            cr.line_to(w * i / 8 + 0.5, h)
        for i in range(1, 4):
            cr.move_to(0, h * i / 4 + 0.5)
            cr.line_to(w, h * i / 4 + 0.5)
        cr.stroke()
    if not samples:
        return
    cr.set_source_rgb(*rgb)
    cr.set_line_width(2)
    n = len(samples)
    for i, v in enumerate(samples):
        x = w * i / (n - 1)
        y = h / 2 - v * (h / 2 - 4)
        (cr.move_to if i == 0 else cr.line_to)(x, y)
    cr.stroke()


class ChannelPanel(Gtk.Box):
    def __init__(self, win: "MainWindow", ch: int) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.win, self.ch = win, ch
        self.rgb = CH_COLORS[ch - 1]
        self._updating = False
        self._pending: dict[str, int] = {}

        # header: big readout + preview
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "ch-card", f"ch{ch}"])
        card.set_margin_top(4)
        top = Gtk.Box(spacing=8, margin_top=10, margin_start=14, margin_end=14)
        self.badge = Gtk.Label(label=f"CH{ch}", css_classes=["ch-badge", f"ch{ch}-badge"])
        top.append(self.badge)
        self.wave_label = Gtk.Label(label="", css_classes=["dim-label"], hexpand=True, xalign=0)
        top.append(self.wave_label)
        self.out_label = Gtk.Label(label="OFF", css_classes=["out-off"])
        top.append(self.out_label)
        card.append(top)
        self.freq_big = Gtk.Label(label="—", css_classes=["freq-big", f"ch{ch}-text"], xalign=0,
                                  margin_start=14)
        card.append(self.freq_big)
        self.sub_label = Gtk.Label(label="", css_classes=["sub-readout"], xalign=0, margin_start=14)
        card.append(self.sub_label)
        self.preview = Gtk.DrawingArea(content_height=90, margin_start=10, margin_end=10, margin_bottom=10)
        self.preview.set_draw_func(self._draw_preview)
        card.append(self.preview)
        self.append(card)

        group = Adw.PreferencesGroup()
        self.output = Adw.SwitchRow(title="Output", subtitle="Turns the BNC output on")
        self.output.connect("notify::active", self._on_output)
        group.add(self.output)

        names = [f"{i:02d}: {n}" for i, n in enumerate(P.WAVEFORMS)] + [f"Arb.{i}" for i in range(101, 200)]
        self.wave = Adw.ComboRow(title="Waveform", model=Gtk.StringList.new(names), enable_search=True)
        self.wave.connect("notify::selected", self._on_wave)
        group.add(self.wave)

        self.freq = Adw.EntryRow(title="Frequency (e.g. 1k, 2.5 MHz, 440 Hz)", show_apply_button=True)
        self.freq.connect("apply", self._on_freq)
        self.freq.connect("entry-activated", self._on_freq)
        group.add(self.freq)

        self.ampl = self._spin(group, "Amplitude", "Vpp", 0, 25, 0.001, 3, "ampl")
        self.offset = self._spin(group, "Offset", "V", -9.99, 15, 0.01, 2, "offset")
        self.duty = self._spin(group, "Duty cycle", "%  (broken ≥ 1 MHz in FPGA 1.20)", 0, 99.99, 0.01, 2, "duty")
        self.phase = self._spin(group, "Phase", "°", 0, 359.99, 0.01, 2, "phase")
        self.append(group)

    def _spin(self, group, title, unit, lo, hi, step, digits, key):
        row = Adw.SpinRow(title=title, subtitle=unit, digits=digits,
                          adjustment=Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                                                    page_increment=step * 100))
        row.connect("notify::value", self._on_spin, key)
        group.add(row)
        return row

    # -- device -> UI
    def refresh(self) -> None:
        c = self.win.gen.state.ch[self.ch - 1]
        self._updating = True
        try:
            self.output.set_active(c.output)
            idx = c.wave if c.wave < len(P.WAVEFORMS) else (len(P.WAVEFORMS) + c.wave - P.ARB_FIRST)
            if 0 <= idx < self.wave.get_model().get_n_items() and self.wave.get_selected() != idx:
                self.wave.set_selected(idx)
            if not self.freq.has_focus() and not self.freq.get_delegate().has_focus():
                self.freq.set_text(P.format_freq(c.freq_mhz))
            for row, key, val in ((self.ampl, "ampl", c.ampl_mv / 1000), (self.offset, "offset", c.offset_v),
                                  (self.duty, "duty", c.duty_pct), (self.phase, "phase", c.phase_deg)):
                if key not in self._pending and abs(row.get_value() - val) > 1e-6:
                    row.set_value(val)
        finally:
            self._updating = False
        self.freq_big.set_label(P.format_freq(c.freq_mhz))
        self.wave_label.set_label(c.wave_name)
        self.sub_label.set_label(f"{c.ampl_mv / 1000:.3f} Vpp   offs {c.offset_v:+.2f} V   "
                                 f"duty {c.duty_pct:.2f} %   φ {c.phase_deg:.2f}°")
        self.out_label.set_label("ON" if c.output else "OFF")
        self.out_label.set_css_classes(["out-on"] if c.output else ["out-off"])
        self.preview.queue_draw()

    def _draw_preview(self, area, cr, w, h) -> None:
        c = self.win.gen.state.ch[self.ch - 1]
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        samples = [wave_sample(c.wave, 2 * i / 400 + c.phase_deg / 360, c.duty_pct / 100) for i in range(401)]
        draw_trace(cr, w, h, samples, self.rgb if c.output else tuple(x * 0.55 for x in self.rgb))

    # -- UI -> device
    def _on_output(self, row, _p) -> None:
        if self._updating:
            return
        c = self.win.gen.state.ch
        vals = [c[0].output, c[1].output]
        vals[self.ch - 1] = row.get_active()
        self.win.gen.write(10, int(vals[0]), int(vals[1]), cb=lambda: self.win.gen.read(10))

    def _on_wave(self, row, _p) -> None:
        if self._updating:
            return
        i = row.get_selected()
        wave = i if i < len(P.WAVEFORMS) else P.ARB_FIRST + (i - len(P.WAVEFORMS))
        reg = 11 + self.ch - 1
        self.win.gen.write(reg, wave, cb=lambda: self.win.gen.read(reg))

    def _on_freq(self, row) -> None:
        hz = parse_freq(row.get_text())
        if hz is None or hz < 0 or hz > 80e6:
            self.win.toast("Frequency not understood (0 Hz … 80 MHz)")
            return
        c = self.win.gen.state.ch[self.ch - 1]
        reg = 13 + self.ch - 1
        self.win.gen.write(reg, P.freq_to_reg(hz), c.freq_unit, cb=lambda: self.win.gen.read(reg))
        self.win.set_focus(None)

    def _on_spin(self, row, _p, key) -> None:
        if self._updating:
            return
        if key in self._pending:
            GLib.source_remove(self._pending[key])
        self._pending[key] = GLib.timeout_add(WRITE_DEBOUNCE_MS, self._send_spin, key, row)

    def _send_spin(self, key, row) -> bool:
        self._pending.pop(key, None)
        v = row.get_value()
        base, val = {"ampl": (15, P.ampl_to_reg(v)), "offset": (17, P.offset_to_reg(v)),
                     "duty": (19, P.duty_to_reg(v)), "phase": (21, P.phase_to_reg(v))}[key]
        reg = base + self.ch - 1
        self.win.gen.write(reg, val, cb=lambda: self.win.gen.read(reg))
        return False


PRESETS = [
    ("Sine", "sin(x)"), ("Square", "square(t)"), ("Triangle", "tri(t)"), ("Saw", "saw(t)"),
    ("Sinc", "sin(20*(t-0.5)) / (20*(t-0.5)) if t != 0.5 else 1"),
    ("Gauss pulse", "exp(-((t-0.5)*12)**2)"), ("AM", "(1 + 0.5*sin(x)) * sin(20*x)"),
    ("Chirp", "sin(2*pi*(2*t + 30*t*t))"), ("Damped", "exp(-5*t) * sin(30*x)"),
    ("Cardiac-ish", "exp(-((t-0.3)*60)**2) - 0.2*exp(-((t-0.35)*40)**2) + 0.25*exp(-((t-0.6)*15)**2)"),
]


class ArbPage(Gtk.Box):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.win = win
        self.norm: list[float] = []

        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=10, row_spacing=6,
                           column_spacing=6)
        for name, expr in PRESETS:
            b = Gtk.Button(label=name, css_classes=["pill"])
            b.connect("clicked", lambda _b, e=expr: (self.expr.set_text(e), self._compute()))
            flow.append(b)
        self.append(flow)

        group = Adw.PreferencesGroup(
            description="Expression of t (0…1 over one period) or x = 2πt. Functions: sin cos tan exp log sqrt "
                        "abs tanh square(t) tri(t) saw(t) sign … The result is scaled to full range.")
        self.expr = Adw.EntryRow(title="f(t)", text="sin(x) + 0.3*sin(3*x)", show_apply_button=True)
        self.expr.connect("apply", lambda _r: self._compute())
        self.expr.connect("entry-activated", lambda _r: self._compute())
        group.add(self.expr)
        self.slot = Adw.SpinRow(title="Arbitrary slot", subtitle="Arb.1xx on the generator — overwritten!",
                                digits=0, adjustment=Gtk.Adjustment(lower=1, upper=99, step_increment=1, value=99))
        group.add(self.slot)
        self.append(group)

        frame = Gtk.Frame(css_classes=["card"])
        self.area = Gtk.DrawingArea(content_height=220, hexpand=True, vexpand=True)
        self.area.set_draw_func(self._draw)
        frame.set_child(self.area)
        self.append(frame)

        bar = Gtk.Box(spacing=8)
        self.status = Gtk.Label(label="", hexpand=True, xalign=0, css_classes=["dim-label"])
        bar.append(self.status)
        self.spinner = Gtk.Spinner()
        bar.append(self.spinner)
        self.upload = Gtk.Button(label="Upload to generator", css_classes=["suggested-action", "pill"])
        self.upload.connect("clicked", self._on_upload)
        bar.append(self.upload)
        self.append(bar)
        self._compute()

    def _compute(self) -> None:
        try:
            self.norm = P.arb_from_expression(self.expr.get_text())
            self.status.set_label(f"{P.ARB_POINTS} points, 14-bit (0…{P.ARB_MAX})")
            self.expr.remove_css_class("error")
        except Exception as e:                     # noqa: BLE001 - show any expression error to the user
            self.norm = []
            self.status.set_label(f"Error: {e}")
            self.expr.add_css_class("error")
        self.area.queue_draw()

    def _draw(self, area, cr, w, h) -> None:
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        step = max(1, len(self.norm) // max(1, w))
        draw_trace(cr, w, h, self.norm[::step], (0.55, 0.9, 0.45))

    def _on_upload(self, _b) -> None:
        if not self.norm:
            return
        if not self.win.gen.connected:
            self.win.toast("Not connected")
            return
        slot = int(self.slot.get_value())
        self.upload.set_sensitive(False)
        self.spinner.start()
        self.status.set_label(f"Uploading to Arb.{100 + slot}… (BLE takes a few seconds)")

        def done():
            self.spinner.stop()
            self.upload.set_sensitive(True)
            self.status.set_label(f"Stored in Arb.{100 + slot}. Select it as waveform on a channel.")
        self.win.gen.upload_arb(slot, P.arb_to_points(self.norm), cb=done)


class ConsolePage(Gtk.Box):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.win = win
        self.buf = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buf, editable=False, monospace=True, css_classes=["card"],
                            top_margin=8, left_margin=8)
        self.scroll = Gtk.ScrolledWindow(child=view, vexpand=True)
        self.append(self.scroll)
        self.entry = Gtk.Entry(placeholder_text=":r13=0.   (w97 / w98 are blocked)")
        self.entry.connect("activate", self._send)
        self.append(self.entry)

    def log(self, direction: str, text: str) -> None:
        end = self.buf.get_end_iter()
        self.buf.insert(end, f"{direction} {text}\n")
        if self.buf.get_line_count() > 2000:
            start = self.buf.get_start_iter()
            cut = self.buf.get_iter_at_line(500)[1]
            self.buf.delete(start, cut)
        GLib.idle_add(lambda: self.scroll.get_vadjustment().set_value(self.scroll.get_vadjustment().get_upper()))

    def _send(self, entry) -> None:
        text = entry.get_text().strip()
        if text:
            if not text.startswith(":"):
                text = ":" + text
            self.win.gen.raw(text)
            entry.set_text("")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, gen: Generator) -> None:
        super().__init__(application=app, title="PSG9080", default_width=1040, default_height=820)
        self.gen = gen
        self._poll_id: int | None = None

        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.link_label = Gtk.Label(label="", css_classes=["dim-label", "caption"])
        header.pack_start(self.link_label)
        self.sync_btn = Gtk.ToggleButton(icon_name="view-refresh-symbolic", active=True,
                                         tooltip_text="Follow changes made on the front panel")
        header.pack_end(self.sync_btn)
        tv.add_top_bar(header)

        chans = Gtk.Box(spacing=18, homogeneous=True, margin_top=12, margin_bottom=12, margin_start=18,
                        margin_end=18)
        self.panels = [ChannelPanel(self, 1), ChannelPanel(self, 2)]
        for p in self.panels:
            chans.append(p)
        self.stack.add_titled_with_icon(Gtk.ScrolledWindow(child=chans, hscrollbar_policy=Gtk.PolicyType.NEVER),
                                        "channels", "Channels", "media-playback-start-symbolic")
        self.arb = ArbPage(self)
        self.stack.add_titled_with_icon(self.arb, "arb", "Arbitrary", "document-edit-symbolic")
        self.console = ConsolePage(self)
        self.stack.add_titled_with_icon(self.console, "console", "Console", "utilities-terminal-symbolic")
        self.info_label = Gtk.Label(css_classes=["dim-label", "caption"], margin_bottom=6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.stack)
        box.append(self.info_label)
        self.stack.set_vexpand(True)
        tv.set_content(box)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        gen.on_state_changed = self._on_state
        gen.on_link = self._on_link
        gen.on_error = self.toast
        gen.on_raw = self.console.log
        self.connect("close-request", self._on_close)

    def toast(self, msg: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=3))

    def _on_state(self) -> None:
        for p in self.panels:
            p.refresh()
        s = self.gen.state
        if s.model:
            self.info_label.set_label(f"PSG90{s.model}  ·  S/N {s.serial}  ·  versions {s.versions}")

    def _on_link(self, connected: bool, msg: str) -> None:
        self.link_label.set_label(msg)
        if connected:
            self.gen.read(*P.INFO_REGS)
            self.gen.read(*P.POLL_REGS)
            if self._poll_id is None:
                self._poll_id = GLib.timeout_add(POLL_MS, self._poll)
        for p in self.panels:
            p.set_sensitive(connected)

    def _poll(self) -> bool:
        if self.gen.connected and self.sync_btn.get_active() and self.gen.pending() == 0:
            self.gen.read(*P.POLL_REGS)
        return True

    def _on_close(self, *_a) -> bool:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        self.gen.close()
        return False
