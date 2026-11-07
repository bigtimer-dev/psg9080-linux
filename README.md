# PSG9080 for Linux

GTK4 / libadwaita control app for the **Juntek PSG9080** (also sold as Joy-IT JT-PSG9080), 80 MHz, 14-bit, 300 MSa/s
two-channel signal generator. Talks to it over the **BSG145 Bluetooth LE module** (WCH CH9141) or the **USB cable** (CH340).

- Both channels: output on/off, 22 built-in waveforms + arbitrary slots, frequency (mHz resolution), amplitude,
  offset, duty, phase. Changes on the front panel show up in the app (polling).
- Arbitrary waveform designer: type an expression of `t` (or pick a preset), preview it and upload 2048 × 14-bit
  points to a slot. Verified by reading the slot back.
- **Bode plot** with a Rigol DHO scope over LAN (SCPI, port 5555): steps the generator over a log frequency range,
  autoscales both scope channels, downloads one acquisition of both channels and measures amplitude and phase at the
  test frequency itself (single-bin DFT), plots gain (dB) and phase, exports CSV. Same channel for input and output =
  generator flatness. Asks before switching anything on; restores generator and scope settings afterwards.
  Note: on DHO800/900 firmware 00.05.02 `:MEAS:ITEM?` makes the SCPI server drop the connection (it restarts after
  ~10 s), which is why the app never uses it.
- **Sweep** of frequency, amplitude or duty cycle: channel, start/end value, time (10 ms … 640 s), up / down / round trip, linear / log,
  start/stop, with a frequency-vs-time preview.
- **Modulation**: AM, FM, PM, ASK, FSK, PSK, pulse mode ("PWM": exact pulse width / period / invert) and burst (pulse count, trigger source, Trigger button) with modulation frequency, depth, FM/PM deviation and FSK hop
  frequency; shows the generator's AM depth as the textbook depth too (they differ, see below).
- **Measure** tab: frequency, period, pulse widths and duty of a signal on EXT IN (1 Hz – 100 MHz, 2 – 20 Vpp), and
  the live count in counter mode (start/stop counting on the panel).
- Console for raw commands, with `w76` (resets all settings to factory defaults) and the calibration / flash
  commands (`w97`, `w98`) blocked.

## Install

    curl -fsSL https://raw.githubusercontent.com/bigtimer-dev/psg9080-linux/main/install.sh | bash
    ./install.sh            # or from a clone; per-user, no root; then open "PSG9080 Signal Generator"
    psg9080                 # Bluetooth (auto-finds the paired BSG145 module)
    psg9080 --usb           # first USB serial port, 115200 baud (--serial /dev/ttyUSB0 --baud N to override)

Pair the Bluetooth module once: `bluetoothctl scan le`, then `pair` / `trust` its address.

## Protocol (from the firmware, see `psg9080/protocol.py`)

`:` letter NN `=` values `.` CRLF. `rNN` read, `wNN` write (`:ok` reply), `aNN` 2048-point arbitrary upload
(0…16383), `bNN` read an arbitrary slot back (16-bit scaled).

| reg | meaning | units |
|---|---|---|
| 00 / 01 / 02 | model / serial / versions | |
| 10 | outputs `ch1,ch2` | 0/1 |
| 11 / 12 | waveform | 0–21 built-in, 101+ arbitrary |
| 13 / 14 | frequency `value,unit` | millihertz, display unit flag |
| 15 / 16 | amplitude | mV (≤ 25000) |
| 17 / 18 | offset | 1000 = 0 V, 10 mV steps |
| 19 / 20 | duty | 0.01 % |
| 21 / 22 | phase | 0.01 ° |
| 25 | CH2 sync flags: waveform, frequency, amplitude, offset, duty, external | 6 × 0/1 |
| 24 | front-panel screen: `0,1,0,1` main, `0,6,X,6` sweep (X = 0 frequency, 1 amplitude, 2 duty), `0,1,0,12` modulation, `0,1,0,8` pulse ("PWM") | sweep/modulation only run on their screen |
| 40 | modulation type `ch1,ch2` | 0 AM, 1 FM, 2 PM, 3 ASK, 4 FSK, 5 PSK, 6 PWM, 7 Burst |
| 43 / 44 | modulation frequency | mHz |
| 45 / 46 | AM depth (also ASK low level) | 0.1 % |
| 47 / 48 | FM deviation | 0.1 Hz |
| 49 / 50 | FSK hop frequency | 0.1 Hz |
| 51 / 52 | PM deviation | 0.1 ° peak-to-peak |
| 60 | burst trigger source `ch1,ch2` | 0 button, 1 internal, 2 ext AC, 3 ext DC |
| 53 / 54 | pulse ("PWM") width | 1 ns |
| 55 / 56 | pulse ("PWM") period | 10 ns |
| 57 | pulse ("PWM") inverted `ch1,ch2` | 0/1 |
| 58 | burst idle level `ch1,ch2` | 0 zero, 1 +max, 2 −max |
| 61 | burst pulse count `ch1,ch2` | 1 … 100000000 |
| 80 | counter value (counter screen `0,5,0,2`) | |
| 81 | measured frequency (measure screen `0,4,0,1`) | Hz |
| 83 / 84 | measured positive / negative pulse width | ns |
| 85 | measured period | ns |
| 86 | measured duty cycle | 0.01 % |
| 76 | **do not write**: `1` resets all settings to factory defaults (blocked in the app) | |
| 74 | write `1,0,0,0` = fire one burst (same as the Trig. key) | one-shot |
| 64 | sweep `channel,time,direction,mode` | ch 0/1, time 10 ms, dir 0 up / 1 down / 2 round trip, mode 0 lin / 1 log |
| 65 | sweep running `1,1` / stopped `0,0` | |
| 66 / 67 | sweep start / end frequency | 0.1 Hz |
| 68 / 69 | amplitude sweep start / end | mV |
| 70 / 71 | duty sweep start / end (Pulse waveform) | 0.01 % |

Sweep and modulation registers were verified by measuring the output with a scope (spectrum / envelope /
instantaneous frequency). AM depth: the generator's depth m makes the envelope run from the set amplitude down to
(1 − m) of it, so the textbook depth (max − min)/(max + min) is m / (2 − m): 70 % set = 53.8 %. The external modulation input (MOD) and starting the counter remotely are not mapped yet.

## Tests

    python3 -m unittest discover -s tests
