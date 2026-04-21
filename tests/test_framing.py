"""HDLC deframer / builder round-trip tests.

All offline — no Bluetooth, no audio hardware, no ffmpeg.
"""
from __future__ import annotations

import pytest

from benshi.audio.framing import (
    CMD_AUDIO_DATA,
    CMD_END,
    END_OF_TX_PACKET,
    ESCAPE,
    FLAG,
    Deframer,
    build_audio_packet,
    decode_sbc_header,
    sbc_frame_length,
    split_sbc_frames,
)


def test_end_of_tx_packet_is_correct_byte_sequence():
    # Byte-exact match of _AudioFrame.endFrame in the HTCommander-X Dart
    # reference, which is byte-exact match of what the radio itself sends
    # us at the tail of every RX burst. If this drifts, the radio won't
    # stop transmitting when we're done.
    assert bytes(END_OF_TX_PACKET) == bytes.fromhex(
        "7E 01 00 01 00 00 00 00 00 00 7E".replace(" ", "")
    )


def test_build_audio_packet_no_escapes():
    sbc = bytes.fromhex("9c7112" + "00" * 41)  # 44 bytes, no 7E/7D in body
    pkt = build_audio_packet(sbc)
    assert pkt[0] == FLAG
    assert pkt[-1] == FLAG
    assert pkt[1] == CMD_AUDIO_DATA
    # Body length = len(sbc) because nothing was escaped.
    assert len(pkt) == 1 + 1 + len(sbc) + 1


def test_build_audio_packet_with_escapes():
    # Body deliberately contains both bytes that need escaping.
    sbc = bytes([0x9C, 0x71, 0x7E, 0x7D]) + bytes(40)
    pkt = build_audio_packet(sbc)
    assert pkt[0] == FLAG and pkt[-1] == FLAG
    # In the body we expect 0x7E -> 0x7D 0x5E and 0x7D -> 0x7D 0x5D.
    body = pkt[2:-1]
    assert bytes([ESCAPE, 0x5E]) in body
    assert bytes([ESCAPE, 0x5D]) in body


def test_deframer_single_packet():
    raw = bytes.fromhex(
        "7e 00 9c 71 12 c3 64 34 33 22 7d 5d ad 5f 6b 57 5a d5 d8 b5 3c b1"
        " 4f 6a 04 a3 15 46 b9 52 2d 58 94 56 a4 61 c3 14 94 ce 61 6b 56 d2"
        " d5 77 45 9c 71 12 76 65 43 33 31 4d b1 4b 95 63 91 95 42 55 3d c1"
        " 14 69 54 dc 52 09 15 8d b9 56 6c 88 94 15 66 d5 56 35 5c 32 54 a9"
        " a8 e9 29 7e".replace(" ", "")
    )
    pkts = Deframer().feed(raw)
    assert len(pkts) == 1
    pkt = pkts[0]
    assert pkt[0] == 0x00  # command byte = audio data
    # 0x7D 0x5D should have been un-escaped to 0x7D.
    assert 0x7D in pkt


def test_deframer_incremental():
    """Feed the same bytes split arbitrarily across feed() calls."""
    full = bytes([FLAG, 0x00]) + bytes.fromhex("9c7112") + bytes(41) + bytes([FLAG])
    assert len(full) == 47  # FLAG + cmd + 44-byte frame + FLAG
    d1 = Deframer()
    pkts = []
    # Split in single bytes to stress state machine.
    for b in full:
        pkts.extend(d1.feed(bytes([b])))
    d2 = Deframer()
    pkts_whole = d2.feed(full)
    assert pkts == pkts_whole
    assert len(pkts) == 1


def test_split_sbc_frames_length_based():
    # Build a 2-frame packet with a SBC-sync-looking byte (0x9C) buried in
    # sample data to confirm we don't split on it.
    hdr = bytes([0x9C, 0x71, 0x12])
    frame_body = bytes(41)  # 44 total after header
    # Inject a 0x9C inside the body; length-based split should ignore it.
    body_with_sync = bytearray(frame_body)
    body_with_sync[10] = 0x9C
    frame1 = hdr + bytes(body_with_sync)
    frame2 = hdr + frame_body
    assert len(frame1) == len(frame2) == 44

    packet = bytes([0x00]) + frame1 + frame2
    frames = split_sbc_frames(packet)
    assert len(frames) == 2, (
        "length-based splitter should not mis-split on in-body 0x9C bytes"
    )
    assert all(len(f) == 44 for f in frames)


def test_decode_sbc_header_9c7112():
    hdr = decode_sbc_header(bytes([0x9C, 0x71, 0x12]))
    assert hdr["sampling_frequency_hz"] == 32000
    assert hdr["blocks"] == 16
    assert hdr["channel_mode"] == "mono"
    assert hdr["allocation_method"] == "loudness"
    assert hdr["subbands"] == 8
    assert hdr["bitpool"] == 18


def test_sbc_frame_length_matches_observed_44b():
    hdr = decode_sbc_header(bytes([0x9C, 0x71, 0x12]))
    assert sbc_frame_length(hdr) == 44


@pytest.mark.parametrize(
    "pkt",
    [
        END_OF_TX_PACKET,
        bytes([FLAG, CMD_END]) + bytes(8) + bytes([FLAG]),
    ],
)
def test_deframer_tail_packets_are_non_sbc(pkt):
    """The end-of-TX and similar signaling packets must deframe cleanly
    and produce no SBC frames (first body byte is not 0x9C)."""
    pkts = Deframer().feed(pkt)
    assert len(pkts) == 1
    frames = split_sbc_frames(pkts[0])
    assert frames == []
