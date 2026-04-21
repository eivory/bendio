"""Live TX: capture from the default mic, encode to SBC, transmit on the radio.

Same building blocks the ``bendio rfcomm-tx-mic`` CLI uses. Call it like::

    python examples/04_ptt.py 38-d2-00-01-37-0f

Talks for up to 10 seconds, or until Ctrl-C. A second radio on the same
frequency should hear your voice.
"""
from __future__ import annotations

import signal
import sys
import threading
import time

import sounddevice as sd

from bendio.audio.framing import END_OF_TX_PACKET, build_audio_packet
from bendio.audio.macos_rfcomm import RfcommTxSession
from bendio.audio.sbc import SbcEncodeStream


def main(address: str, channel: int = 2, max_seconds: float = 10.0) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    session = RfcommTxSession(address, channel)
    open_result = session.open()
    if not open_result.opened:
        print(f"open failed: {open_result.phase} (IOReturn={open_result.open_status})")
        return 2
    print(f"Talking into mic for up to {max_seconds}s (Ctrl-C to stop early)...")

    # Each SBC frame out of ffmpeg gets HDLC-wrapped and shoved at the radio.
    # Safe to call session.write() from the ffmpeg reader thread —
    # IOBluetooth's writeSync:length: is thread-safe.
    def on_sbc_frame(frame: bytes) -> None:
        session.write(build_audio_packet(frame))

    encoder = SbcEncodeStream(on_frame=on_sbc_frame)

    def on_mic(indata, frames, time_info, status):
        # sounddevice audio thread → ffmpeg stdin.
        encoder.feed(bytes(indata))

    mic = sd.RawInputStream(
        samplerate=32000, channels=1, dtype="int16",
        blocksize=128, callback=on_mic,
    )
    mic.start()

    t_start = time.monotonic()
    try:
        while not stop.is_set() and time.monotonic() - t_start < max_seconds:
            session.pump(0.1)
    finally:
        mic.stop()
        mic.close()
        encoder.close()
        time.sleep(0.2)  # let the encoder drain its last few frames
        session.close(tail_packet=END_OF_TX_PACKET, post_drain_s=1.5)
    print("Done.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python 04_ptt.py <BT_CLASSIC_MAC>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
