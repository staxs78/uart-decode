#!/usr/bin/env python3
"""uart_decode.py — decode UART frames (8N1/8E1/8O1) from scope/logic-analyzer
exports (CSV, VCD, or two-column text). Event-driven: no uniform sampling needed.

Strategy: threshold -> edge list -> try idle-level hypotheses -> estimate bit
time as the LARGEST period explaining every run length and frame spacing as an
integer number of bits -> retry divisors -> keep the hypothesis with the most
valid frames. Reports baud, bytes, framing errors.

Exit codes: 0 = ran (even with no frames), 2 = bad input."""
from __future__ import annotations
import argparse, csv, io, sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

# --------------------------- parsing --------------------------------------- #

def _is_float(tok: str) -> bool:
    try: float(tok); return True
    except ValueError: return False

def parse_table(text: str) -> List[Tuple[float, float]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "//"))]
    if not lines: raise ValueError("empty capture")
    delim = next((d for d in (",", ";", "\t", " ") if d in lines[0]), ",")
    rows = []
    for ln in lines:
        parts = [p.strip() for p in ln.split(delim) if p.strip() != ""]
        if len(parts) >= 2 and _is_float(parts[0]) and _is_float(parts[1]):
            rows.append(parts)
    if len(rows) < 4: raise ValueError("need at least 4 numeric rows")
    t = [float(r[0]) for r in rows]
    if all(t[i] <= t[i + 1] for i in range(len(t) - 1)):
        pass
    elif all(t[i] >= t[i + 1] for i in range(len(t) - 1)):
        rows.reverse()
    else:
        ncol = min(len(r) for r in rows)
        for c in range(ncol - 1, -1, -1):
            vals = [float(r[c]) for r in rows]
            if all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)):
                sc = 1 if c == 0 else 0
                return sorted((vals[i], float(rows[i][sc])) for i in range(len(rows)))
        raise ValueError("could not find a monotonic time column")
    return [(float(r[0]), float(r[1])) for r in rows]

def parse_vcd(text: str, want_signal: Optional[str] = None) -> List[Tuple[float, float]]:
    var_id, timescale, events, t = None, 1e-9, [], 0.0
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("$timescale"):
            body = s.split("$timescale", 1)[1].split("$end")[0].strip()
            num = "".join(ch for ch in body if ch.isdigit() or ch == ".")
            unit = "".join(ch for ch in body if ch.isalpha()).lower()
            timescale = float(num or 1) * {"s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9,
                                           "ps": 1e-12, "fs": 1e-15}.get(unit, 1e-9)
        elif s.startswith("$var"):
            p = s.split()
            if len(p) >= 5 and var_id is None and (want_signal is None or p[4].lstrip("\\") == want_signal):
                var_id = p[3]
        elif s.startswith("#"):
            try: t = float(s[1:].split()[0])
            except ValueError: pass
        elif var_id:
            body = s.split("//")[0].strip()
            if len(body) >= 2 and body[0] in "01" and body[1:].strip() == var_id:
                events.append((t * timescale, 1.0 if body[0] == "1" else 0.0))
    if not events: raise ValueError("no VCD transitions for requested signal")
    return events

# --------------------------- decode core ----------------------------------- #

@dataclass
class Frame:
    t_start: float; data: int; stop_ok: bool; start_ok: bool; bits: str

@dataclass
class Report:
    baud_estimate: float = 0.0; baud_min: float = 0.0; baud_max: float = 0.0
    inverted: bool = False; idle_level: float = 0.0
    frames: List[Frame] = field(default_factory=list)
    framing_errors: int = 0; start_errors: int = 0
    span_s: float = 0.0; transitions: int = 0
    notes: List[str] = field(default_factory=list)

def _threshold(vals):
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12: raise ValueError("signal is flat — no transitions")
    return (lo + hi) / 2.0

def _edges(events, thr):
    out, last = [], None
    for t, v in events:
        lvl = 1.0 if v > thr else 0.0
        if lvl != last: out.append((t, lvl)); last = lvl
    return out

def _level_at(ed, t, idle):
    if t < ed[0][0]: return idle
    lo, hi = 0, len(ed) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ed[mid][0] <= t: lo = mid
        else: hi = mid - 1
    return ed[lo][1]

def _bit_time(values):
    """Largest d such that every value is within 12% of k*d, k integer in 1..64."""
    if not values: return 0.0
    vals = sorted(values)
    cands = {v / m for v in vals[:8] for m in range(1, 13)}
    for d in sorted(cands, reverse=True):
        if d <= 0: continue
        if all(1 <= v / d <= 64 and abs(v / d - round(v / d)) < 0.12 for v in vals):
            return d
    return vals[0]

def _starts(ed, idle, active):
    """(start_time, width_or_None) for every idle->active transition."""
    out = []
    for i in range(1, len(ed)):
        if ed[i - 1][1] == idle and ed[i][1] == active:
            out.append((ed[i][0], (ed[i + 1][0] - ed[i][0]) if i + 1 < len(ed) else None))
    return out

def _frames(ed, d, idle, active, data_bits, parity):
    """Walk frames with bit time d. Returns list[Frame]."""
    extra = 1 if parity != "none" else 0
    frames, i = [], 1
    while i < len(ed) - 1:
        if not (ed[i - 1][1] == idle and ed[i][1] == active):
            i += 1; continue
        t0 = ed[i][0]
        start_ok = _level_at(ed, t0 + d * 0.5, idle) == active
        raw, data, bits = [], 0, ("0" if start_ok else "1")
        for b in range(data_bits):
            bit = 1 if _level_at(ed, t0 + d * (1.5 + b), idle) == active else 0
            raw.append(bit); data |= bit << b; bits += str(bit)
        stop_ok = True
        if extra:
            p = 1 if _level_at(ed, t0 + d * (1.5 + data_bits), idle) == active else 0
            exp = (sum(raw) % 2) if parity == "even" else (1 - sum(raw) % 2)
            if p != exp: stop_ok = False
            bits += str(p)
        s_lvl = _level_at(ed, t0 + d * (1.5 + data_bits + extra), idle)
        if s_lvl != idle: stop_ok = False
        bits += "1" if s_lvl == idle else "0"
        frames.append(Frame(t0, data & ((1 << data_bits) - 1), stop_ok, start_ok, bits))
        t_end = t0 + d * (2 + data_bits + extra)
        while i < len(ed) and ed[i][0] < t_end: i += 1
    return frames

def decode(events, data_bits=8, parity="none") -> Report:
    rep = Report()
    if len(events) < 6:
        rep.notes.append("too few samples to decode"); return rep
    vals = [v for _, v in events]
    thr = _threshold(vals)
    ed = _edges(events, thr)
    rep.transitions = len(ed)
    rep.span_s = events[-1][0] - events[0][0]
    if rep.span_s <= 0:
        rep.notes.append("non-positive time span — check time units"); return rep

    # idle-level hypotheses: first level, last level, longest total, longest single
    totals, longest = {0.0: 0.0, 1.0: 0.0}, {0.0: 0.0, 1.0: 0.0}
    for i in range(len(ed) - 1):
        w = ed[i + 1][0] - ed[i][0]
        totals[ed[i][1]] += w
        longest[ed[i][1]] = max(longest[ed[i][1]], w)
    hyps = []
    for lvl in (ed[0][1], ed[-1][1], max(totals, key=totals.get), max(longest, key=longest.get)):
        if lvl not in hyps: hyps.append(lvl)

    best = None  # (frac, frames, idle, d)
    for idle in hyps:
        active = 0.0 if idle == 1.0 else 1.0
        st = _starts(ed, idle, active)
        widths = [w for _, w in st if w]
        if not widths: continue
        wmin = min(widths)
        spacings = [st[i + 1][0] - st[i][0] for i in range(len(st) - 1)]
        spacings = [s for s in spacings if 0 < s <= 12 * wmin]
        d0 = _bit_time(widths + spacings)
        if d0 <= 0: continue
        for d in (d0, d0 / 2, d0 / 3, d0 / 4):
            fr = _frames(ed, d, idle, active, data_bits, parity)
            if not fr: continue
            good = sum(1 for f in fr if f.start_ok and f.stop_ok)
            frac = good / len(fr)
            # keep highest fraction of valid frames; ties go to the LARGER bit
            # period (iterating largest-first and requiring strict improvement),
            # so a subdivided hypothesis only wins if it is strictly cleaner.
            if best is None or frac > best[0] + 1e-9:
                best = (frac, fr, idle, d)
    if best is None:
        rep.notes.append("no idle->active transitions found (line never starts a frame)")
        return rep

    _, fr, idle, d = best
    rep.frames = fr
    rep.idle_level = idle
    rep.inverted = (idle == 0.0)
    rep.framing_errors = sum(1 for f in fr if not f.stop_ok)
    rep.start_errors = sum(1 for f in fr if not f.start_ok)
    rep.baud_estimate = 1.0 / d
    # plausible range: snap observed run widths to multiples of d
    st = _starts(ed, idle, 0.0 if idle == 1.0 else 1.0)
    snaps = []
    for _, w in st:
        if not w: continue
        k = max(1, round(w / d))
        if abs(w / d - k) < 0.12: snaps.append(w / k)
    if snaps:
        rep.baud_max = 1.0 / min(snaps)
        rep.baud_min = 1.0 / max(snaps)
    else:
        rep.baud_min = rep.baud_max = rep.baud_estimate
    if rep.baud_estimate > 10e6 or rep.baud_estimate < 300:
        rep.notes.append(f"baud estimate {rep.baud_estimate:.0f} is outside the normal UART range "
                         "— check time units (s/ms/us) and column selection")
    if not fr:
        rep.notes.append("no frames decoded — line may be gated, wrong pad, or wrong polarity")
    return rep

# --------------------------- output ---------------------------------------- #

def render(rep, show_ascii=True) -> str:
    out = io.StringIO()
    out.write("=== UART decode report ===\n")
    out.write(f"transitions seen : {rep.transitions}\n")
    out.write(f"capture span     : {rep.span_s:.6g} s\n")
    out.write(f"idle level       : {rep.idle_level:.0f} ({'inverted capture' if rep.inverted else 'normal framing'})\n")
    out.write(f"baud estimate    : {rep.baud_estimate:.1f}  (range {rep.baud_min:.1f} .. {rep.baud_max:.1f})\n")
    out.write(f"frames           : {len(rep.frames)}\n")
    out.write(f"framing errors   : {rep.framing_errors}\n")
    out.write(f"start errors     : {rep.start_errors}\n")
    for n in rep.notes: out.write(f"note             : {n}\n")
    if rep.frames:
        out.write(f"bytes ({len(rep.frames)}): " + " ".join(f"{f.data:02X}" for f in rep.frames) + "\n")
        if show_ascii:
            txt = "".join(chr(f.data) if 32 <= f.data < 127 else "." for f in rep.frames)
            out.write(f"ascii : {txt}\n")
        out.write("\nfirst frames:\n")
        for f in rep.frames[:16]:
            flag = "" if (f.stop_ok and f.start_ok) else "  <-- framing problem"
            out.write(f"  t={f.t_start:.6g}s  0x{f.data:02X}  bits={f.bits}{flag}\n")
        if len(rep.frames) > 16:
            out.write(f"  ... {len(rep.frames) - 16} more frames\n")
    return out.getvalue()

def load_events(path, fmt, vcd_signal):
    with open(path, "r", errors="replace") as fh:
        text = fh.read()
    fmt = fmt or ("vcd" if path.lower().endswith(".vcd") else "csv")
    return parse_vcd(text, vcd_signal) if fmt == "vcd" else parse_table(text)

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Decode UART frames from scope/VCD captures")
    ap.add_argument("capture")
    ap.add_argument("--format", choices=["csv", "vcd"], default=None)
    ap.add_argument("--vcd-signal", default=None)
    ap.add_argument("--data-bits", type=int, default=8, choices=[5, 6, 7, 8])
    ap.add_argument("--parity", choices=["none", "even", "odd"], default="none")
    ap.add_argument("--no-ascii", action="store_true")
    args = ap.parse_args(argv)
    try:
        ev = load_events(args.capture, args.format, args.vcd_signal)
        rep = decode(ev, data_bits=args.data_bits, parity=args.parity)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 2
    print(render(rep, show_ascii=not args.no_ascii))
    return 0

if __name__ == "__main__":
    sys.exit(main())
