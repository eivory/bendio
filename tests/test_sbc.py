"""SBC encoder/decoder tests.

These require ffmpeg to be installed. If ffmpeg isn't on PATH the tests
skip rather than fail, so CI without ffmpeg still passes (but CI with
ffmpeg provides real coverage).
"""
from __future__ import annotations

import shutil
import time

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg not installed",
)

from benshi.audio.framing import decode_sbc_header
from benshi.audio.sbc import (
    SbcEncodeStream,
    SbcStream,
    SbcUnavailable,
    encode_pcm_to_sbc,
)


def _silence_pcm(seconds: float = 1.0, rate: int = 32000) -> bytes:
    return bytes(int(seconds * rate) * 2)


def test_encode_pcm_to_sbc_produces_correct_codec_header():
    pcm = _silence_pcm(0.25)  # 250 ms
    sbc = encode_pcm_to_sbc(pcm)
    # At this codec config, exactly 44 bytes per frame.
    assert len(sbc) % 44 == 0, f"ffmpeg output {len(sbc)} B; expected multiple of 44"
    # First frame header must match 9C 71 12 — the radio will reject any
    # other codec config on the wire.
    assert sbc[0] == 0x9C
    hdr = decode_sbc_header(sbc[:3])
    assert hdr["sampling_frequency_hz"] == 32000
    assert hdr["channel_mode"] == "mono"
    assert hdr["subbands"] == 8
    assert hdr["blocks"] == 16
    assert hdr["allocation_method"] == "loudness"
    assert hdr["bitpool"] == 18


def test_encode_one_second_yields_250_frames():
    sbc = encode_pcm_to_sbc(_silence_pcm(1.0))
    assert len(sbc) == 250 * 44  # 250 frames/sec × 44 bytes/frame


def test_streaming_encoder_splits_into_44b_frames():
    frames = []
    enc = SbcEncodeStream(on_frame=frames.append)
    try:
        enc.feed(_silence_pcm(1.0))
        time.sleep(0.5)  # let the ffmpeg reader thread drain
    finally:
        enc.close()
    assert len(frames) >= 245, f"got only {len(frames)} frames"
    assert all(len(f) == 44 for f in frames)
    assert all(f[0] == 0x9C for f in frames)


def test_streaming_decoder_round_trip_through_encoder():
    """End-to-end: encode PCM silence, feed the SBC bytes back through
    the streaming decoder, confirm PCM comes out at the right rate."""
    # Encode 1 s of silence to SBC.
    sbc = encode_pcm_to_sbc(_silence_pcm(1.0))
    assert len(sbc) == 11000

    # Decode back to PCM via the streaming decoder.
    pcm_out = bytearray()
    dec = SbcStream(on_pcm=lambda chunk: pcm_out.extend(chunk))
    try:
        dec.feed(sbc)
        # Give the stdout reader a moment to drain.
        time.sleep(0.5)
    finally:
        dec.close()
    # 1 s of audio = 32000 samples × 2 bytes mono = 64000 B.
    # ffmpeg may emit slightly less at EOF; allow a small tolerance.
    assert 60000 <= len(pcm_out) <= 64000, f"got {len(pcm_out)} PCM bytes"


def test_sbc_stream_raises_when_ffmpeg_missing(monkeypatch):
    """If ffmpeg disappears from PATH, we want a clear typed error."""
    monkeypatch.setattr("benshi.audio.sbc.shutil.which", lambda _: None)
    with pytest.raises(SbcUnavailable):
        SbcStream(on_pcm=lambda _: None)
    with pytest.raises(SbcUnavailable):
        SbcEncodeStream(on_frame=lambda _: None)
    with pytest.raises(SbcUnavailable):
        encode_pcm_to_sbc(b"\x00\x00")
