"""PSG9080 serial protocol (same text protocol over USB CH340 and the BSG145/CH9141 BLE module).

Recovered from the 2021-03-10 firmware (USART1 parser @0x0801D128, dispatcher @0x0801D450) and live tests.

Frame:  ':' letter NN '=' v1 ',' v2 ... '.' '\\r\\n'      NN = two decimal digits
  r = read register NN        -> ':rNN=<fields>.\\r\\n'
  w = write register NN       -> ':ok\\r\\n'
  a = upload 2048-point arbitrary waveform to slot NN (values 0..16383) -> ':ok\\r\\n'
Registers (CHx = odd/even pairs):
  00 model (80)           01 serial number          02 versions
  10 outputs "ch1,ch2"    11/12 waveform            13/14 frequency "mHz,unit"
  15/16 amplitude mV      17/18 offset (1000 = 0 V, 10 mV steps)
  19/20 duty x0.01 %      21/22 phase x0.01 deg     97 calibration table (never write)
  25 CH2 sync flags (waveform, frequency, amplitude, offset, duty, external)
Sweep (verified on a scope 2026-11-07):
  64 "channel,time,direction,mode"  channel 0=CH1 1=CH2, time in 10 ms, direction 0 up 1 down 2 round trip,
                                    mode 0 linear 1 logarithmic
  65 "1,1" running / "0,0" stopped  66/67 start / end frequency in 0.1 Hz
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

WAVEFORMS = [
    "Sine", "Square", "Pulse", "Triangle", "Ramp", "CMOS", "DC", "Dev-Sine", "Half-Wave", "Full-Wave",
    "Pos-Ladder", "Neg-Ladder", "Pos-Trap", "Neg-Trap", "Noise", "Exp-Rise", "Exp-Fall", "Log-Rise",
    "Log-Fall", "Sinc", "Multi tone", "Lorenz",
]
ARB_FIRST = 101          # waveform numbers >= 101 select arbitrary slots ("Arb.101" ...)
ARB_POINTS = 2048
ARB_MAX = 0x3FFF         # 14-bit full scale used by the firmware for ':a' uploads

FREQ_MAX_MHZ = 80_000_000_000      # 80 MHz in mHz
AMPL_MAX_MV = 25_000
OFFSET_ZERO = 1000                 # register value for 0 V
OFFSET_MIN, OFFSET_MAX = 1, 2500   # firmware clamps to 2500; 0 is treated specially, avoid it
DUTY_MAX = 9999
PHASE_MOD = 36000

# Registers that change calibration or rewrite internal flash / reboot: never sent by this app.
FORBIDDEN_WRITES = {97, 98}

_REPLY = re.compile(r"^:(?:([a-zA-Z])(\d\d)=(.*?)\.?|(ok))$")


class ProtocolError(Exception):
    pass


def read_cmd(reg: int) -> bytes:
    return f":r{reg:02d}=0.\r\n".encode()


def write_cmd(reg: int, *values: int) -> bytes:
    if reg in FORBIDDEN_WRITES:
        raise ProtocolError(f"register {reg} is blocked (calibration / flash)")
    if not values:
        raise ProtocolError("write needs at least one value")
    for v in values:
        if not isinstance(v, int) or v < 0:
            raise ProtocolError(f"invalid value {v!r}")
    return f":w{reg:02d}={','.join(str(v) for v in values)}.\r\n".encode()


def arb_upload_cmd(slot: int, points: list[int]) -> bytes:
    if not 1 <= slot <= 99:
        raise ProtocolError("arbitrary slot must be 1..99")
    if len(points) != ARB_POINTS:
        raise ProtocolError(f"need exactly {ARB_POINTS} points")
    pts = [min(ARB_MAX, max(0, int(round(p)))) for p in points]
    return f":a{slot:02d}={','.join(map(str, pts))}.\r\n".encode()


def parse_reply(line: str) -> tuple[str, int | None, list[str]]:
    """Return ('ok', None, []) or (letter, reg, fields), e.g. ('r', 13, [...]) or ('b', 99, [2048 values])."""
    m = _REPLY.match(line.strip())
    if not m:
        raise ProtocolError(f"unexpected reply {line!r}")
    if m.group(4):
        return "ok", None, []
    return m.group(1), int(m.group(2)), m.group(3).split(",")


@dataclass
class Channel:
    index: int                 # 1 or 2
    output: bool = False
    wave: int = 0
    freq_mhz: int = 1_000_000  # millihertz
    freq_unit: int = 0         # display unit flag kept as the device reports it
    ampl_mv: int = 5000
    offset_reg: int = OFFSET_ZERO
    duty_reg: int = 5000
    phase_reg: int = 0

    # register numbers for this channel
    def reg(self, base: int) -> int:
        return base + (self.index - 1)

    @property
    def offset_v(self) -> float:
        return (self.offset_reg - OFFSET_ZERO) / 100.0

    @property
    def duty_pct(self) -> float:
        return self.duty_reg / 100.0

    @property
    def phase_deg(self) -> float:
        return self.phase_reg / 100.0

    @property
    def wave_name(self) -> str:
        if 0 <= self.wave < len(WAVEFORMS):
            return WAVEFORMS[self.wave]
        if self.wave >= ARB_FIRST:
            return f"Arb.{self.wave}"
        return f"#{self.wave}"


SWEEP_DIRECTIONS = ["Up", "Down", "Round trip"]
SWEEP_MODES = ["Linear", "Logarithmic"]
SWEEP_REGS = [64, 65, 66, 67]
SWEEP_TIME_MAX = 64000             # 640 s in 10 ms units


@dataclass
class Sweep:
    channel: int = 0               # 0 = CH1, 1 = CH2
    time_cs: int = 1000            # sweep time in 10 ms units
    direction: int = 0
    log: bool = False
    running: bool = False
    start_dhz: int = 10000         # 0.1 Hz units
    end_dhz: int = 100000


def sweep_config_cmd(sw: "Sweep") -> tuple[int, ...]:
    return (sw.channel, max(1, min(SWEEP_TIME_MAX, sw.time_cs)), sw.direction, int(sw.log))


def hz_to_dhz(hz: float) -> int:
    return max(0, min(FREQ_MAX_MHZ // 100, int(round(hz * 10))))


@dataclass
class DeviceState:
    model: str = ""
    serial: str = ""
    versions: str = ""
    ch: list[Channel] = field(default_factory=lambda: [Channel(1), Channel(2)])
    sweep: Sweep = field(default_factory=Sweep)

    def apply(self, reg: int, fields: list[str]) -> None:
        f = [x.strip() for x in fields]
        try:
            if reg == 0:
                self.model = f[0]
            elif reg == 1:
                self.serial = f[0]
            elif reg == 2:
                self.versions = ",".join(f)
            elif reg == 10:
                self.ch[0].output, self.ch[1].output = f[0] == "1", f[1] == "1"
            elif reg in (11, 12):
                self.ch[reg - 11].wave = int(f[0])
            elif reg in (13, 14):
                c = self.ch[reg - 13]
                c.freq_mhz = int(f[0])
                c.freq_unit = int(f[1]) if len(f) > 1 else 0
            elif reg in (15, 16):
                self.ch[reg - 15].ampl_mv = int(f[0])
            elif reg in (17, 18):
                self.ch[reg - 17].offset_reg = int(f[0])
            elif reg in (19, 20):
                self.ch[reg - 19].duty_reg = int(f[0])
            elif reg in (21, 22):
                self.ch[reg - 21].phase_reg = int(f[0])
            elif reg == 64:
                sw = self.sweep
                sw.channel, sw.time_cs, sw.direction, sw.log = int(f[0]), int(f[1]), int(f[2]), f[3] == "1"
            elif reg == 65:
                self.sweep.running = f[0] == "1"
            elif reg == 66:
                self.sweep.start_dhz = int(f[0])
            elif reg == 67:
                self.sweep.end_dhz = int(f[0])
        except (ValueError, IndexError) as e:
            raise ProtocolError(f"bad fields for r{reg:02d}: {fields}") from e


POLL_REGS = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
INFO_REGS = [0, 1, 2]


# ---- value conversions used by the UI (all return register values, clamped) ----

def freq_to_reg(hz: float) -> int:
    return max(0, min(FREQ_MAX_MHZ, int(round(hz * 1000))))


def ampl_to_reg(vpp: float) -> int:
    return max(0, min(AMPL_MAX_MV, int(round(vpp * 1000))))


def offset_to_reg(volts: float) -> int:
    return max(OFFSET_MIN, min(OFFSET_MAX, OFFSET_ZERO + int(round(volts * 100))))


def duty_to_reg(pct: float) -> int:
    return max(0, min(DUTY_MAX, int(round(pct * 100))))


def phase_to_reg(deg: float) -> int:
    return int(round(deg * 100)) % PHASE_MOD


def format_freq(mhz: int) -> str:
    hz = mhz / 1000
    for div, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if hz >= div:
            return f"{hz / div:.6f}".rstrip("0").rstrip(".") + f" {unit}"
    return f"{hz:.3f}".rstrip("0").rstrip(".") + " Hz"


# ---- arbitrary waveform generation ----

_SAFE = {k: getattr(math, k) for k in ("sin", "cos", "tan", "asin", "acos", "atan", "atan2", "exp", "log",
                                       "log10", "sqrt", "pi", "e", "tau", "floor", "ceil", "fabs", "sinh",
                                       "cosh", "tanh")}
_SAFE.update(abs=abs, min=min, max=max, round=round,
             sign=lambda x: (x > 0) - (x < 0),
             square=lambda x: 1.0 if (x % 1.0) < 0.5 else -1.0,
             tri=lambda x: 1 - 4 * abs((x % 1.0) - 0.5),
             saw=lambda x: 2 * (x % 1.0) - 1)
_ALLOWED_NAMES = set(_SAFE) | {"t", "x"}


def arb_from_expression(expr: str, n: int = ARB_POINTS) -> list[float]:
    """Evaluate expr for t in [0,1) (x = 2*pi*t). Result is normalised to -1..1 (peak)."""
    code = compile(expr, "<waveform>", "eval")
    bad = [name for name in code.co_names if name not in _ALLOWED_NAMES]
    if bad:
        raise ProtocolError(f"unknown name(s): {', '.join(bad)}")
    vals = []
    for i in range(n):
        t = i / n
        v = eval(code, {"__builtins__": {}}, {**_SAFE, "t": t, "x": 2 * math.pi * t})  # noqa: S307 - names whitelisted
        v = float(v)
        if not math.isfinite(v):
            raise ProtocolError(f"value not finite at t={t:.4f}")
        vals.append(v)
    peak = max(abs(v) for v in vals) or 1.0
    return [v / peak for v in vals]


def arb_to_points(norm: list[float]) -> list[int]:
    """Map -1..1 to 0..16383."""
    return [int(round((v + 1) / 2 * ARB_MAX)) for v in norm]
