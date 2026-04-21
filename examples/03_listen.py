"""Live RX: decode SBC from the radio and play it through the default speaker.

Same building blocks the ``benshi rfcomm-play`` CLI uses, but stripped down
to the bare minimum to show the library shape. Call it like::

    python examples/03_listen.py 38-d2-00-01-37-0f

Pre-reqs: radio paired via System Settings → Bluetooth, ``brew install ffmpeg``,
``pip install sounddevice``, and ``scripts/mac_bluetooth_setup.py`` run once.
"""
from __future__ import annotations

import signal
import sys
import threading

import sounddevice as sd

from benshi.audio.framing import Deframer, split_sbc_frames
from benshi.audio.macos_rfcomm import dump_rfcomm
from benshi.audio.sbc import SbcStream


def main(address: str, channel: int = 2) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # 32 kHz mono int16 — the codec config this radio family uses.
    speaker = sd.RawOutputStream(
        samplerate=32000, channels=1, dtype="int16",
        blocksize=128, latency="low",
    )
    speaker.start()

    # ffmpeg SBC decoder → speaker.
    decoder = SbcStream(on_pcm=lambda pcm: speaker.write(pcm))

    # HDLC deframer + SBC frame splitter → ffmpeg stdin.
    deframer = Deframer()

    def on_bytes(_t_rel: float, chunk: bytes) -> None:
        for pkt in deframer.feed(chunk):
            frames = split_sbc_frames(pkt)
            if frames:
                decoder.feed(b"".join(frames))

    print(f"Listening on {address} ch {channel}. Ctrl-C to stop.")
    dump_rfcomm(address, channel, on_bytes=on_bytes, stop=stop.is_set)
    decoder.close()
    speaker.stop()
    speaker.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python 03_listen.py <BT_CLASSIC_MAC>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
