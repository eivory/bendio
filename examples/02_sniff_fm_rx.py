"""Phase 2 helper: while the radio is in FM RX, log every inbound BLE frame.

Run this, put the radio in FM receiver mode, key a second radio on the same
frequency, and watch what comes in. Expected: only control-plane frames
(HT_STATUS_CHANGED with the RX bit toggling) — no audio payloads on BLE.

Append the trace to ../docs/ble_fm_rx_trace.md.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time

from bendio import Radio
from bendio import protocol as p

ALL_EVENTS = [
    p.EventType.HT_STATUS_CHANGED,
    p.EventType.HT_CH_CHANGED,
    p.EventType.HT_SETTINGS_CHANGED,
    p.EventType.DATA_RXD,
    p.EventType.RADIO_STATUS_CHANGED,
    p.EventType.USER_ACTION,
    p.EventType.SYSTEM_EVENT,
    p.EventType.POSITION_CHANGE,
]


async def main(address: str) -> None:
    t0 = time.monotonic()

    def on_frame(frame: bytes) -> None:
        t_rel = time.monotonic() - t0
        print(f"[{t_rel:8.3f}s] {len(frame):3d}B {frame.hex()}")

    async with Radio(address) as radio:
        radio.set_raw_frame_handler(on_frame)
        for ev in ALL_EVENTS:
            try:
                await radio.register_notification(ev)
            except Exception as exc:
                print(f"# couldn't register {ev.name}: {exc}", file=sys.stderr)

        print("# Put radio in FM RX now. Ctrl-C to stop.", file=sys.stderr)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python 02_sniff_fm_rx.py <BT_ADDR>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
