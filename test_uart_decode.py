#!/usr/bin/env python3
"""Synthetic-capture tests for uart_decode.py — round-trip, inverted, noisy, gated."""
import random
import subprocess
import sys
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "uart_decode.py")


def make_uart_events(bytes_out, baud=115200, idle=1.0, invert=False, start=0.0,
                     bit_jitter=0.0, noise=0.0, seed=7, gap_s=0.0):
    """Return list of (t, level) transitions for an 8N1 stream."""
    rng = random.Random(seed)
    bit_t = 1.0 / baud
    t = start
    ev = [(t, idle)]
    active = 0.0 if idle == 1.0 else 1.0
    levels = []
    for b in bytes_out:
        frame = [active]                      # start
        for i in range(8):                    # data LSB first
            frame.append(active if (b >> i) & 1 else 0.0 if active == 1.0 else 1.0)
        frame.append(idle)                    # stop
        # fix data bit levels relative to active/idle
        frame = [active]
        for i in range(8):
            frame.append(active if ((b >> i) & 1) else idle)
        frame.append(idle)
        for lvl in frame:
            d = bit_t
            if bit_jitter:
                d *= 1.0 + rng.uniform(-bit_jitter, bit_jitter)
            t += d
            ev.append((t, lvl))
        t += gap_s
        ev.append((t, idle))
    if invert:
        ev = [(tt, 1.0 if lv == 0.0 else 0.0) for tt, lv in ev]
    return ev


def write_csv(events, path, scale=1.0, noise=0.0, seed=3):
    rng = random.Random(seed)
    with open(path, "w") as fh:
        fh.write("Time,Voltage\n")
        for t, lv in events:
            v = (1.0 if lv == 1.0 else 0.0)
            if noise:
                v += rng.uniform(-noise, noise)
            fh.write(f"{t * scale:.9g},{v:.6f}\n")


def run(args):
    p = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True)
    return p


def test_basic_roundtrip():
    payload = bytes([0x48, 0x65, 0x6C, 0x6C, 0x6F, 0x0D, 0x0A, 0x55, 0x55, 0x3C])
    ev = make_uart_events(payload, baud=115200, idle=1.0)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        pass
    write_csv(ev, fh.name)
    p = run([fh.name])
    assert p.returncode == 0, p.stderr
    out = p.stdout
    assert "baud estimate" in out
    import re
    m = re.search(r"baud estimate\s+:\s+([\d.]+)", out)
    baud = float(m.group(1))
    assert 110000 < baud < 121000, f"baud {baud} out of tolerance"
    assert "48 65 6C 6C 6F 0D 0A 55 55 3C" in out, out
    assert "framing errors   : 0" in out, out
    print(f"PASS basic roundtrip (baud={baud:.0f})")
    return True


def test_slow_baud_9600():
    payload = bytes(b"Hello, console banner U-Boot 2017.07\r\n")
    ev = make_uart_events(payload, baud=9600, idle=1.0, start=0.001)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        pass
    write_csv(ev, fh.name)
    p = run([fh.name])
    assert p.returncode == 0, p.stderr
    import re
    baud = float(re.search(r"baud estimate\s+:\s+([\d.]+)", p.stdout).group(1))
    assert 9400 < baud < 9800, f"baud {baud}"
    assert "Hello, console banner U-Boot 2017.07" in p.stdout, p.stdout
    print(f"PASS 9600 baud ASCII (baud={baud:.0f})")
    return True


def test_inverted_and_low_idle():
    # board where the capture is inverted (idle low at start of file semantics)
    payload = bytes([0xAA, 0x55, 0x00, 0xFF, 0x42])
    ev = make_uart_events(payload, baud=115200, idle=0.0, invert=False)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        pass
    write_csv(ev, fh.name)
    p = run([fh.name])
    assert p.returncode == 0, p.stderr
    assert "AA 55 00 FF 42" in p.stdout, p.stdout
    print("PASS low-idle framing")
    return True


def test_gated_gap():
    # console that prints only in two bursts (boot + one event) with a long silent gap
    payload = bytes(b"U-Boot 2017.07 (Oct  8 2026)")
    ev = make_uart_events(payload, baud=115200, idle=1.0, start=0.0)
    ev2 = make_uart_events(b"\r\nlogin: ", baud=115200, idle=1.0, start=1.5)
    merged = sorted(set(ev + ev2))
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        pass
    write_csv(merged, fh.name)
    p = run([fh.name])
    assert p.returncode == 0, p.stderr
    assert "U-Boot 2017.07" in p.stdout, p.stdout
    assert "login:" in p.stdout, p.stdout
    print("PASS gated bursts")
    return True


def test_flat_signal_flags():
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write("Time,V\n")
        for i in range(50):
            fh.write(f"{i * 1e-6},3.3\n")
    p = run([fh.name])
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    print("PASS flat signal rejected")
    return True


def test_jitter_and_noise():
    # realistic scope capture: ±3% bit jitter + voltage noise
    payload = bytes(b"UART decode under jitter + noise: 0123456789\r\n")
    ev = make_uart_events(payload, baud=115200, idle=1.0, bit_jitter=0.03, seed=11)
    import random as _r
    rng = _r.Random(5)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
        fh.write("Time,Voltage\n")
        for t, lv in ev:
            fh.write(f"{t:.9g},{(1.0 if lv else 0.0) + rng.uniform(-0.15, 0.15):.4f}\n")
    p = run([fh.name])
    assert p.returncode == 0, p.stderr
    assert "0123456789" in p.stdout, p.stdout
    print("PASS jitter + noise")
    return True


def test_vcd_input():
    payload = bytes(b"vcd-burst\r\n")
    ev = make_uart_events(payload, baud=57600, idle=1.0)
    # build a minimal VCD at fs timescale
    with tempfile.NamedTemporaryFile("w", suffix=".vcd", delete=False) as fh:
        fh.write("$timescale 1fs $end\n")
        fh.write("$var wire 1 ! sig $end\n")
        fh.write("$enddefinitions $end\n")
        last = None
        for t, lv in ev:
            v = 1 if lv else 0
            if v != last:
                fh.write(f"#{int(t * 1e15)}\n{v}!\n")
                last = v
    p = run([fh.name, "--vcd-signal", "sig"])
    assert p.returncode == 0, (p.returncode, p.stderr)
    import re
    baud = float(re.search(r"baud estimate\s+:\s+([\d.]+)", p.stdout).group(1))
    assert 57000 < baud < 58300, f"baud {baud}"
    assert "vcd-burst" in p.stdout, p.stdout
    print(f"PASS VCD (baud={baud:.0f})")
    return True


def main():
    tests = [test_basic_roundtrip, test_slow_baud_9600, test_inverted_and_low_idle,
             test_gated_gap, test_flat_signal_flags, test_jitter_and_noise,
             test_vcd_input]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed.append((t.__name__, str(e)))
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
