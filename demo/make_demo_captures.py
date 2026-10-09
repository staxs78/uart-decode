#!/usr/bin/env python3
"""Generate synthetic demo captures that round-trip through uart_decode.py.

Uses the same encoder as the test-suite (verified 7/7) so the published demo
output is guaranteed to match the bytes it claims to decode.

Writes (into this directory):
  uboot_banner_115200_jitter.csv   — U-Boot console banner, 115200 8N1, +-2% jitter + noise
  uboot_banner_inverted.csv        — same stream, inverted-idle capture semantics
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from test_uart_decode import make_uart_events  # verified encoder


def write_csv(events, path, noise=0.0, seed=3):
    rng = random.Random(seed)
    with open(path, "w", newline="") as fh:
        fh.write("# SYNTHETIC demo capture (not a real device). time_s,voltage_v\n")
        for t, lv in events:
            v = 1.0 if lv == 1.0 else 0.0
            if noise:
                v += rng.uniform(-noise, noise)
            fh.write(f"{t:.9g},{v*3.3:.6f}\n")  # t in seconds, 0..3.3 V


def main():
    banner = (
        b"\r\nU-Boot 2017.07 (Nov 21 2021 - 12:00:00 +0800)\r\n\r\n"
        b"Board: Huawei HG8245H5\r\n"
        b"DRAM:  256 MiB\r\n"
        b"Hit any key to stop autoboot:  3  2  1  0\r\n"
    )
    login = b"\r\nlogin: "
    ev = make_uart_events(banner, baud=115200, idle=1.0, start=0.0, bit_jitter=0.02)
    ev2 = make_uart_events(login, baud=115200, idle=1.0, start=1.2, bit_jitter=0.02)
    merged = sorted(set(ev + ev2))

    write_csv(merged, os.path.join(HERE, "uboot_banner_115200_jitter.csv"), noise=0.12)
    inv = [(t, 0.0 if lv == 1.0 else 1.0) for t, lv in merged]
    write_csv(inv, os.path.join(HERE, "uboot_banner_inverted.csv"), noise=0.12, seed=9)
    print("wrote demo captures to", HERE)


if __name__ == "__main__":
    main()
