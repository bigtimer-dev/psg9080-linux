"""Application entry point."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .link import BleTransport, Generator, SerialTransport, find_serial_ports  # noqa: E402
from .window import MainWindow  # noqa: E402

APP_ID = "io.github.bigtimer_dev.PSG9080"

CSS = """
.ch-card { padding-bottom: 4px; }
.ch1 { border-top: 3px solid #fad640; }
.ch2 { border-top: 3px solid #59ccf2; }
.ch-badge { font-weight: 800; padding: 1px 8px; border-radius: 6px; color: #111; }
.ch1-badge { background: #fad640; }
.ch2-badge { background: #59ccf2; }
.ch1-text { color: #fad640; }
.ch2-text { color: #59ccf2; }
.freq-big { font-size: 30pt; font-weight: 300; font-feature-settings: "tnum"; }
.sub-readout { font-feature-settings: "tnum"; opacity: 0.8; }
.out-on { background: #2ec27e; color: #fff; font-weight: 800; padding: 1px 10px; border-radius: 6px; }
.out-off { background: alpha(currentColor, 0.15); font-weight: 800; padding: 1px 10px; border-radius: 6px; }
"""


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.add_main_option("serial", ord("s"), GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Use USB serial port (e.g. /dev/ttyUSB0) instead of Bluetooth", "PORT")
        self.add_main_option("baud", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.INT,
                             "Serial baud rate (default 115200)", "BAUD")
        self.add_main_option("ble", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Bluetooth address of the BSG145 module (default: auto)", "ADDR")
        self.add_main_option("usb", ord("u"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Use the first USB serial port found", None)
        self.add_main_option("page", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Open on page: channels, arb, sweep, mod, meas, memory, bode or console", "PAGE")
        self.opts: dict = {}
        GLib.set_application_name("PSG9080")

    def do_handle_local_options(self, options) -> int:
        self.opts = options.end().unpack()
        return -1

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        act = Gio.SimpleAction.new("quit", None)
        act.connect("activate", lambda *_: self.quit())
        self.add_action(act)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])

    def do_activate(self) -> None:
        port = self.opts.get("serial")
        if not port and self.opts.get("usb"):
            ports = find_serial_ports()
            port = ports[0] if ports else None
        transport = (SerialTransport(port, self.opts.get("baud") or 115200) if port
                     else BleTransport(self.opts.get("ble")))
        gen = Generator(transport)
        win = MainWindow(self, gen)
        if self.opts.get("page") in ("channels", "arb", "sweep", "mod", "meas", "memory", "bode", "console"):
            win.stack.set_visible_child_name(self.opts["page"])
        win.present()
        GLib.idle_add(lambda: (gen.open(), False)[1])


def main() -> int:
    return App().run(sys.argv)
