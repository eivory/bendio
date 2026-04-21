"""HDLC-style deframer for the Benshi audio RFCOMM stream.

The radio's audio stream uses a simple HDLC-like framing:
- ``0x7E`` marks frame boundaries (start AND end)
- ``0x7D`` is an escape byte; the following byte is XOR-ed with ``0x20``
  (so ``0x7E`` in data becomes ``7D 5E``, and ``0x7D`` in data becomes ``7D 5D``)

Inside each frame we observed a single fixed header byte (``0x00``) followed by
one or more back-to-back SBC frames. Each SBC frame begins with sync byte
``0x9C``. SBC frame lengths are deterministic given the codec parameters
(32 kHz, 16 blocks, mono, loudness allocation, bitpool 18, 8 subbands →
44 bytes per frame on this radio), but for robustness we split on the
sync byte rather than hardcoding the length.
"""
from __future__ import annotations

FLAG = 0x7E
ESCAPE = 0x7D
XOR = 0x20
SBC_SYNC = 0x9C

# TX command-byte convention used by the radio's RFCOMM audio protocol.
# Mirrors the Dart implementation in
# ../../../htcommander_flutter/lib/radio/radio_audio_manager.dart.
CMD_AUDIO_DATA = 0x00
CMD_END = 0x01
CMD_LOOPBACK = 0x02

# End-of-transmission packet. The radio sends this to us at the end of every
# RX burst (we observed it as the short "01 00 01 00 00 00 00 00 00"
# non-SBC packet in Phase 2); we must send it back after our own TX or the
# radio will stay in transmit mode waiting for more audio.
END_OF_TX_PACKET = bytes([
    FLAG, CMD_END, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, FLAG,
])


def build_audio_packet(sbc_frame: bytes) -> bytes:
    """Wrap an SBC frame in the radio's TX audio packet envelope.

    Output layout: ``0x7E 0x00 <escaped SBC bytes> 0x7E``.
    """
    out = bytearray()
    out.append(FLAG)
    out.append(CMD_AUDIO_DATA)
    for b in sbc_frame:
        if b == FLAG or b == ESCAPE:
            out.append(ESCAPE)
            out.append(b ^ XOR)
        else:
            out.append(b)
    out.append(FLAG)
    return bytes(out)


class Deframer:
    """Streaming HDLC-style deframer.

    Feed raw RFCOMM bytes in any chunk size; get back a list of complete
    payload bytes objects (with escaping undone). Partial frames are held
    across ``feed()`` calls.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._escaped = False

    def feed(self, data: bytes | bytearray) -> list[bytes]:
        out: list[bytes] = []
        for b in data:
            if b == FLAG:
                # Closing flag (or just a flag separator). Emit if we have a
                # non-empty frame, then start collecting the next one.
                if self._in_frame and self._buf:
                    out.append(bytes(self._buf))
                self._buf.clear()
                self._in_frame = True
                self._escaped = False
                continue
            if not self._in_frame:
                # Before the first flag, drop anything we see (resync).
                continue
            if self._escaped:
                self._buf.append(b ^ XOR)
                self._escaped = False
                continue
            if b == ESCAPE:
                self._escaped = True
                continue
            self._buf.append(b)
        return out


def decode_sbc_header(frame: bytes) -> dict:
    """Decode the 2-byte SBC frame header (not counting sync) for diagnostics.

    Per A2DP 1.3 §12.9 "Frame Header", the bit layout (MSB first) is:
      byte 0 : 0x9C sync
      byte 1 : sampling_frequency(2) blocks(2) channel_mode(2)
               allocation_method(1) subbands(1)
      byte 2 : bitpool(8)

    allocation_method is *one* bit, not two — a mistake I made first time
    around. subbands piggybacks on byte 1's low bit.
    """
    if len(frame) < 3 or frame[0] != SBC_SYNC:
        return {}
    b1 = frame[1]
    b2 = frame[2]
    freq_bits = (b1 >> 6) & 0x03
    blocks_bits = (b1 >> 4) & 0x03
    mode_bits = (b1 >> 2) & 0x03
    alloc_bit = (b1 >> 1) & 0x01
    subbands_bit = b1 & 0x01
    freq_map = {0: 16000, 1: 32000, 2: 44100, 3: 48000}
    blocks_map = {0: 4, 1: 8, 2: 12, 3: 16}
    mode_map = {0: "mono", 1: "dual", 2: "stereo", 3: "joint"}
    alloc_map = {0: "loudness", 1: "SNR"}
    return {
        "sync": 0x9C,
        "sampling_frequency_hz": freq_map[freq_bits],
        "blocks": blocks_map[blocks_bits],
        "channel_mode": mode_map[mode_bits],
        "allocation_method": alloc_map[alloc_bit],
        "subbands": 8 if subbands_bit else 4,
        "bitpool": b2,
    }


def sbc_frame_length(header: dict) -> int:
    """Compute the on-wire size of an SBC frame (including header + CRC byte)
    from a decoded header dict. Follows the A2DP 1.3 spec formula.
    """
    import math
    subbands = header["subbands"]
    blocks = header["blocks"]
    bitpool = header["bitpool"]
    mode = header["channel_mode"]
    channels = 1 if mode == "mono" else 2

    # 4-byte fixed part (sync + 2 config bytes + CRC)
    length = 4
    # scale factor nibbles: 4 bits × subbands × channels
    length += math.ceil(4 * subbands * channels / 8)
    # samples: depends on channel mode
    if mode in ("mono", "dual"):
        length += math.ceil(blocks * channels * bitpool / 8)
    elif mode == "stereo":
        length += math.ceil(blocks * bitpool / 8) * 2
    else:  # joint stereo adds a flag field
        length += math.ceil((subbands + blocks * bitpool) / 8)
    return length


def split_sbc_frames(packet: bytes, *, header_bytes: int = 1) -> list[bytes]:
    """Split a deframed audio packet into individual SBC frames.

    Packet layout, empirically:

        [header (typically 1 byte, `0x00`)]
        [SBC frame 0 starting with 0x9C] [SBC frame 1] [...]

    We use **length-based** splitting, not sync-scanning: once we've decoded
    the first SBC header, the frame length is deterministic for the whole
    codec configuration. Advancing by exactly that length avoids false
    splits caused by stray ``0x9C`` bytes inside SBC sample data.

    If the first would-be frame doesn't start with 0x9C (e.g. the tail
    "01 00 01 00 00 00 00 00 00" signaling packet), we return an empty
    list and the caller can treat the packet as non-SBC.
    """
    body = packet[header_bytes:] if header_bytes > 0 else packet
    if not body or body[0] != SBC_SYNC or len(body) < 4:
        return []

    # Frame length is determined by the first frame's header; on this
    # radio all frames share the same codec config so it's a constant.
    hdr = decode_sbc_header(body[:3] + b"\x00")  # dummy CRC; decoder ignores it
    if not hdr:
        return []
    try:
        flen = sbc_frame_length(hdr)
    except Exception:
        return []
    if flen <= 0 or flen > 512:
        return []

    frames: list[bytes] = []
    i = 0
    while i + flen <= len(body):
        if body[i] != SBC_SYNC:
            # Drift detected — stop rather than emit garbage.
            break
        frames.append(bytes(body[i : i + flen]))
        i += flen
    return frames
