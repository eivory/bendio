#!/usr/bin/env python3
"""Quick sanity check: record from a mic and report peak + RMS.

Prints the list of available input devices first. Takes an optional device
name or index; defaults to the system's current input.

Uses ``sd.RawInputStream`` so no numpy dependency — same API the library
uses in ``rfcomm-tx-mic``.

Examples:
    python scripts/mic_check.py                 # default input, 2 s
    python scripts/mic_check.py --device 1      # by index
    python scripts/mic_check.py --device "MacBook Pro Microphone"
    python scripts/mic_check.py --duration 5
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import time

try:
    import sounddevice as sd
except ImportError:
    print("sounddevice missing. pip install sounddevice", file=sys.stderr)
    sys.exit(1)


def parse_device(s):
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return s


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=None,
                   help="Input device name or index (default: system default)")
    p.add_argument("--duration", type=float, default=2.0)
    p.add_argument("--rate", type=int, default=32000)
    args = p.parse_args()

    print("Input devices visible to sounddevice:")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            mark = "*" if i == sd.default.device[0] else " "
            print(
                f"  {mark} [{i}] {d['name']!r} "
                f"({d['max_input_channels']} ch, "
                f"{int(d.get('default_samplerate', 0))} Hz)"
            )
    print("  (* marks the current default input)\n")

    device = parse_device(args.device)
    peak = [0]
    sample_count = [0]
    sum_sq = [0.0]

    def cb(indata, frames, time_info, status):
        if status:
            print(f"stream status: {status}", file=sys.stderr)
        # indata is a CFFI buffer; unpack the int16 samples.
        fmt = f"<{frames}h"
        samples = struct.unpack(fmt, bytes(indata))
        for s in samples:
            a = -s if s < 0 else s
            if a > peak[0]:
                peak[0] = a
            sum_sq[0] += s * s
        sample_count[0] += frames

    selected = device if device is not None else "default"
    print(f"Recording {args.duration:.1f}s from {selected!r} @ {args.rate} Hz...")
    stream = sd.RawInputStream(
        samplerate=args.rate,
        channels=1,
        dtype="int16",
        blocksize=0,
        callback=cb,
        device=device,
    )
    stream.start()
    time.sleep(args.duration)
    stream.stop()
    stream.close()

    if sample_count[0] == 0:
        print("! no samples captured at all — stream never delivered any data.")
        return 3

    rms = math.sqrt(sum_sq[0] / sample_count[0]) if sample_count[0] else 0.0
    peak_dbfs = 20 * math.log10(peak[0] / 32767) if peak[0] > 0 else float("-inf")
    rms_dbfs = 20 * math.log10(rms / 32767) if rms > 0 else float("-inf")
    print(f"samples captured: {sample_count[0]}")
    print(f"peak = {peak[0]:>6d}  ({peak_dbfs:.1f} dBFS)")
    print(f"rms  = {rms:>8.1f}  ({rms_dbfs:.1f} dBFS)")

    if peak[0] == 0:
        print("\n! peak is exactly 0 — that's true silence. This device is not")
        print("  producing any audio. Pick a different --device, or:")
        print("    System Settings → Sound → Input → set the real mic.")
        return 2
    if peak_dbfs < -40:
        print("\n! Peak below -40 dBFS — very quiet. Speak louder / move closer.")
        return 1
    print("\n✓ Mic is producing audio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
