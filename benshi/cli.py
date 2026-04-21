"""Command-line entry point for Phase 1 and Phase 2 work.

Subcommands:
  scan                  List BLE devices advertising the Benshi service.
  connect <ADDR>        Handshake, print device info + first channels,
                        subscribe to HT_STATUS_CHANGED and log events until Ctrl-C.
  channels <ADDR> [-n]  Dump the first N channels as a readable table.
  sniff <ADDR>          Log every inbound GAIA frame as timestamped hex.
                        Intended for Phase 2 (observing BLE while radio is in FM RX).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import typing as t

from . import protocol as p
from .link import RADIO_SERVICE_UUID
from .link import scan as ble_scan
from .radio import Radio

log = logging.getLogger(__name__)


def _cmd_audio_devices(args: argparse.Namespace) -> int:
    """List input/output devices visible to sounddevice, marking defaults."""
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        print(
            "sounddevice not installed. Run: pip install sounddevice",
            file=sys.stderr,
        )
        return 4
    try:
        default_in, default_out = sd.default.device
    except Exception:
        default_in, default_out = (None, None)
    devs = sd.query_devices()

    def _fmt(i: int, d: dict, role: str) -> str:
        in_ch = d.get("max_input_channels", 0)
        out_ch = d.get("max_output_channels", 0)
        rate = int(d.get("default_samplerate", 0))
        is_default = (
            (role == "input" and i == default_in)
            or (role == "output" and i == default_out)
        )
        mark = "*" if is_default else " "
        return f"  {mark} [{i:2d}] {d['name']!r} ({in_ch} in / {out_ch} out ch, {rate} Hz)"

    print("Input devices:")
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            print(_fmt(i, d, "input"))
    print("\nOutput devices:")
    for i, d in enumerate(devs):
        if d.get("max_output_channels", 0) > 0:
            print(_fmt(i, d, "output"))
    print("\n* marks the current system default for that direction.")
    print("Pass either the index or a unique substring of the name to")
    print("  --device on `benshi rfcomm-play` or `benshi rfcomm-tx-mic`.")
    return 0


def _parse_sd_device(s):
    """Coerce a --device CLI value into the form sounddevice expects.

    Integers should be passed as ``int`` (selects device by index);
    non-numeric strings are passed through as-is (matched by substring
    against device name).
    """
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return s


async def _cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning for {args.timeout:.1f}s...")
    results = await ble_scan(timeout=args.timeout, only_benshi=args.only_benshi)
    if not results:
        print("(none found)")
        if not args.only_benshi:
            print(
                "\nNote: macOS hides the real MAC address and shows a\n"
                "per-device UUID instead. If your radio is already paired\n"
                "in System Settings, it may not advertise while connected.\n"
                "Either unpair it temporarily, or use its paired UUID\n"
                "directly with 'benshi connect <UUID>'."
            )
        return 1

    # Sort strongest signal first; radios are usually close.
    results.sort(key=lambda r: (r[2] if r[2] is not None else -999), reverse=True)

    # Highlight likely Benshi radios — by advertised UUID or common names.
    likely_names = ("UV-PRO", "UV-Pro", "GA-5WB", "VR-N76", "VR-N7500", "GMRS-PRO")
    for dev, uuids, rssi in results:
        name = dev.name or "<unnamed>"
        rssi_s = f"{rssi:>4d} dBm" if rssi is not None else "  ?     "
        flag = " "
        if RADIO_SERVICE_UUID in uuids:
            flag = "*"  # strongest indicator
        elif any(n.lower() in name.lower() for n in likely_names):
            flag = "?"  # name match; probably it
        print(f"  {flag} {rssi_s}  {dev.address}  {name}")
    print("\n  * = advertises Benshi service UUID")
    print("  ? = name matches a known Benshi model")
    return 0


async def _cmd_connect(args: argparse.Namespace) -> int:
    async with Radio(args.address) as radio:
        info = await radio.device_info()
        print("=== Device Info ===")
        _print_bitfield(info, indent="  ")

        print("\n=== First channels ===")
        for i in range(args.channels):
            try:
                ch = await radio.read_rf_ch(i)
            except Exception as exc:
                print(f"  [{i:02d}] error: {exc}")
                continue
            if ch.rf_ch is None:
                print(f"  [{i:02d}] <empty slot> ({ch.reply_status.name})")
                continue
            rf = ch.rf_ch
            name = getattr(rf, "name_str", "").rstrip("\x00 ")
            print(
                f"  [{i:02d}] {name:<12}  rx={rf.rx_freq:.6f} MHz  "
                f"tx={rf.tx_freq:.6f} MHz  {rf.bandwidth.name}"
            )

        print("\n=== Subscribing to HT_STATUS_CHANGED (Ctrl-C to exit) ===")

        def _on_event(body: p.EventNotificationBody) -> None:
            t_rel = time.monotonic() - t0
            print(f"[{t_rel:7.3f}s] event_type={body.event_type.name} {body.event!r}")

        t0 = time.monotonic()
        radio.on_notification(_on_event)
        # HT_CH_CHANGED is rejected by UV-PRO firmware (INVALID_PARAMETER);
        # the info we'd get from it rides along on HT_STATUS_CHANGED anyway.
        await radio.register_notification(p.EventType.HT_STATUS_CHANGED)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
        return 0


async def _cmd_channels(args: argparse.Namespace) -> int:
    async with Radio(args.address) as radio:
        for i in range(args.count):
            ch = await radio.read_rf_ch(i)
            if ch.rf_ch is None:
                continue
            rf = ch.rf_ch
            name = getattr(rf, "name_str", "").rstrip("\x00 ")
            print(
                f"{i:03d}\t{name}\t{rf.rx_freq:.6f}\t{rf.tx_freq:.6f}\t"
                f"{rf.rx_mod.name}\t{rf.bandwidth.name}"
            )
    return 0


_SDP_ATTR_NAMES = {
    0x0000: "ServiceRecordHandle",
    0x0001: "ServiceClassIDList",
    0x0002: "ServiceRecordState",
    0x0003: "ServiceID",
    0x0004: "ProtocolDescriptorList",
    0x0005: "BrowseGroupList",
    0x0006: "LanguageBaseAttributeIDList",
    0x0007: "ServiceInfoTimeToLive",
    0x0008: "ServiceAvailability",
    0x0009: "BluetoothProfileDescriptorList",
    0x0100: "ServiceName",
    0x0101: "ServiceDescription",
    0x0102: "ProviderName",
}


def _render_element(el: dict, indent: int = 0) -> None:
    pad = "    " * indent
    tn = el.get("type_name", "?")
    if "array" in el:
        print(f"{pad}{tn}:", flush=True)
        for sub in el["array"]:
            _render_element(sub, indent + 1)
    elif "uuid" in el:
        short = el.get("uuid_short_hex")
        short_s = f" (short={short})" if short else ""
        print(f"{pad}{tn}: {el['uuid']}{short_s}", flush=True)
    elif "value" in el:
        print(f"{pad}{tn}: {el['value']!r}", flush=True)
    else:
        print(f"{pad}{tn} (opaque)", flush=True)


def _cmd_rfcomm_inspect(args: argparse.Namespace) -> int:
    """Dump every SDP attribute of every service record. Read-only."""
    from .audio import macos_rfcomm as rf
    try:
        records = rf.inspect_services(args.address, timeout=args.sdp_timeout)
    except rf.IOBluetoothUnavailable as exc:
        print(f"IOBluetooth not available: {exc}", file=sys.stderr)
        return 3

    if not records:
        print("No service records returned (SDP query may have failed).")
        return 1

    for r in records:
        print(f"=== Record [{r.index:02d}] ===")
        print(f"  name: {r.name!r}")
        ch_s = (
            f"{r.rfcomm_channel}"
            if r.rfcomm_channel is not None
            else "(not extracted)"
        )
        print(f"  rfcomm_channel: {ch_s}")
        if args.dump_attrs:
            print("  attributes:")
            for attr_id in sorted(r.attributes):
                label = _SDP_ATTR_NAMES.get(attr_id, "")
                hdr = f"    [0x{attr_id:04x}]"
                if label:
                    hdr += f"  {label}"
                print(hdr)
                _render_element(r.attributes[attr_id], indent=2)
        print()

    # Short summary table
    print("--- summary ---")
    for r in records:
        ch_s = str(r.rfcomm_channel) if r.rfcomm_channel is not None else "--"
        print(f"  [{r.index:02d}] ch={ch_s:4s}  {r.name!r}")
    return 0


def _generate_sine_pcm(
    freq_hz: float, duration_s: float, sample_rate: int = 32000,
    amplitude: float = 0.2, fade_s: float = 0.005,
) -> bytes:
    """Pure-Python sine tone generator with linear fade in/out to avoid
    click artifacts when stopping/starting abruptly. Returns signed 16-bit
    little-endian mono bytes."""
    import math
    import struct
    n = int(sample_rate * duration_s)
    fade_n = max(1, int(sample_rate * fade_s)) if fade_s > 0 else 0
    peak = amplitude * 32767
    out = bytearray(n * 2)
    two_pi_f_over_fs = 2.0 * math.pi * freq_hz / sample_rate
    for i in range(n):
        if fade_n > 0 and i < fade_n:
            env = i / fade_n
        elif fade_n > 0 and i > n - fade_n:
            env = max(0.0, (n - i) / fade_n)
        else:
            env = 1.0
        v = int(peak * env * math.sin(two_pi_f_over_fs * i))
        struct.pack_into("<h", out, i * 2, v)
    return bytes(out)


# Classic Close Encounters of the Third Kind 5-note motif:
# G4, A4, F4, F3 (octave down), C4 — as scored by John Williams.
_CE3K_NOTES = [
    (391.995, 0.45),  # G4 — "re"
    (440.000, 0.45),  # A4 — "mi"
    (349.228, 0.45),  # F4 — "do"
    (174.614, 0.60),  # F3 — "do" (octave lower, held slightly)
    (261.626, 0.80),  # C4 — "sol" (held for emphasis)
]


def _generate_ce3k_pcm(
    sample_rate: int = 32000, amplitude: float = 0.3, gap_s: float = 0.1,
    octave_shift: int = 0,
) -> bytes:
    """Build the CE3K motif as one continuous PCM blob.

    ``octave_shift`` multiplies every note by ``2**octave_shift``. Narrowband
    FM radios apply a ~300 Hz high-pass to the audio path, so the original F3
    (174.6 Hz) is nearly inaudible on the receiving side; ``octave_shift=1``
    moves the lowest note up to F4 (349 Hz) where the filter leaves it alone.
    """
    multiplier = 2.0 ** octave_shift
    out = bytearray()
    silence_bytes = bytes(int(sample_rate * gap_s) * 2)  # s16 mono
    for i, (freq, dur) in enumerate(_CE3K_NOTES):
        out.extend(_generate_sine_pcm(
            freq * multiplier, dur, sample_rate, amplitude))
        if i < len(_CE3K_NOTES) - 1:
            out.extend(silence_bytes)
    return bytes(out)


def _cmd_rfcomm_tx_mic(args: argparse.Namespace) -> int:
    """Phase 3e-2: live mic capture → streaming SBC encode → RFCOMM TX.

    Pipeline: sounddevice mic callback → ffmpeg (streaming PCM→SBC) →
    HDLC wrap per frame → writeSync_length_ on the RFCOMM channel.

    ``writeSync`` is thread-safe on IOBluetoothRFCOMMChannel, so the
    mic audio thread and ffmpeg stdout reader thread call into the
    channel object directly; the main thread pumps the runloop for
    IOBluetooth delegate callbacks and watches for stop.
    """
    import threading

    from .audio import macos_rfcomm as rf
    from .audio.framing import END_OF_TX_PACKET, build_audio_packet
    from .audio.sbc import SbcEncodeStream, SbcUnavailable
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        print(
            "sounddevice not installed. Run: pip install sounddevice",
            file=sys.stderr,
        )
        return 4

    # --- open RFCOMM first; bail cheaply if the radio isn't accepting us ---
    session = rf.RfcommTxSession(args.address, args.channel)
    print(f"Opening RFCOMM ch {args.channel} on {args.address}...", flush=True)
    open_result = session.open(open_timeout=args.open_timeout)
    if not open_result.opened:
        print(
            f"RFCOMM open failed: phase={open_result.phase} "
            f"IOReturn={open_result.open_status}",
            file=sys.stderr,
        )
        return 2
    print("Channel open. Radio should now be ready for audio.", flush=True)

    stop_flag = threading.Event()

    # Counters (plain lists for nonlocal-y state from multiple threads;
    # they're monotonic increments so races just slightly misreport).
    total_pcm_bytes = [0]
    total_sbc_frames = [0]
    total_packets = [0]

    def prev_sigint(_signum, _frame):
        stop_flag.set()

    prev = signal.signal(signal.SIGINT, prev_sigint)

    def on_sbc_frame(frame: bytes) -> None:
        # Wrap and ship. Called on ffmpeg's stdout reader thread.
        pkt = build_audio_packet(frame)
        session.write(pkt)
        total_sbc_frames[0] += 1
        total_packets[0] += 1

    try:
        encoder = SbcEncodeStream(on_frame=on_sbc_frame)
    except SbcUnavailable as exc:
        print(f"SBC encoder unavailable: {exc}", file=sys.stderr)
        session.close(tail_packet=END_OF_TX_PACKET, post_drain_s=0.5)
        signal.signal(signal.SIGINT, prev)
        return 3

    def mic_callback(indata, frames, time_info, status):
        # Called on sounddevice's audio thread. `indata` is a buffer of
        # bytes (dtype=int16, channels=1). Forward to encoder stdin.
        if status:
            log.warning("mic stream status: %s", status)
        # RawInputStream hands us a CFFI CData buffer; coerce to bytes.
        encoder.feed(bytes(indata))
        total_pcm_bytes[0] += len(indata)

    input_stream = sd.RawInputStream(
        samplerate=32000,
        channels=1,
        dtype="int16",
        blocksize=128,  # one SBC-frame worth per callback (≈4 ms)
        callback=mic_callback,
        device=_parse_sd_device(args.device),
    )

    mode_s = (
        f"Recording for {args.duration} s."
        if args.duration
        else "Press Ctrl-C to stop."
    )
    print(
        f"Starting mic ({input_stream.samplerate:.0f} Hz mono). {mode_s}",
        flush=True,
    )
    input_stream.start()

    t_start = time.monotonic()
    last_report = [t_start]
    try:
        while not stop_flag.is_set():
            session.pump(0.1)
            now = time.monotonic()
            if args.duration and (now - t_start) >= args.duration:
                break
            if now - last_report[0] > 1.0:
                last_report[0] = now
                print(
                    f"[{now - t_start:6.2f}s] pcm_in={total_pcm_bytes[0]}B "
                    f"sbc_frames={total_sbc_frames[0]} "
                    f"pkts_sent={total_packets[0]} "
                    f"write_errors={session.write_errors}",
                    flush=True,
                )
    finally:
        signal.signal(signal.SIGINT, prev)
        print("Stopping mic and flushing encoder...", flush=True)
        input_stream.stop()
        input_stream.close()
        encoder.close()
        # Give the encoder's reader thread a moment to drain any remaining
        # SBC output and call session.write() for the last few frames.
        time.sleep(0.2)
        session.pump(0.2)

        print("Sending end-of-TX and closing channel...", flush=True)
        session.close(
            tail_packet=END_OF_TX_PACKET,
            tail_repeats=3,
            tail_interval_s=0.05,
            post_drain_s=1.5,
        )

    print(
        f"Done. pcm_in={total_pcm_bytes[0]}B "
        f"sbc_frames={total_sbc_frames[0]} "
        f"pkts_sent={total_packets[0]} "
        f"write_errors={session.write_errors}",
        flush=True,
    )
    return 0


def _cmd_rfcomm_tx_tone(args: argparse.Namespace) -> int:
    """Phase 3e-1: batch-TX a sine-wave test tone and prove the full TX path."""
    from .audio import macos_rfcomm as rf
    from .audio.framing import END_OF_TX_PACKET, build_audio_packet
    from .audio.sbc import SbcUnavailable, encode_pcm_to_sbc

    if args.preset in ("ce3k", "ce3k-high"):
        octave = 1 if args.preset == "ce3k-high" else 0
        label_low = "G4–A4–F4–F3–C4" if octave == 0 else "G5–A5–F5–F4–C5"
        extra = (
            ""
            if octave == 0
            else " (octave up — fits within narrowband FM's ~300 Hz HPF)"
        )
        print(
            f"Generating Close Encounters of the Third Kind motif "
            f"({label_low}) at amplitude {args.amplitude}{extra}...",
            flush=True,
        )
        pcm = _generate_ce3k_pcm(
            sample_rate=32000, amplitude=args.amplitude, octave_shift=octave,
        )
    else:
        print(
            f"Generating {args.duration:.2f}s of {args.freq:.0f} Hz sine at "
            f"amplitude {args.amplitude}...",
            flush=True,
        )
        pcm = _generate_sine_pcm(
            freq_hz=args.freq,
            duration_s=args.duration,
            sample_rate=32000,
            amplitude=args.amplitude,
        )
    print(f"  {len(pcm)} PCM bytes = {len(pcm)//2} samples "
          f"({len(pcm)//2/32000:.2f}s @ 32 kHz)", flush=True)

    print("Encoding PCM → SBC via ffmpeg...", flush=True)
    try:
        sbc_stream = encode_pcm_to_sbc(pcm)
    except SbcUnavailable as exc:
        print(f"SBC encoder unavailable: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"SBC encode failed: {exc}", file=sys.stderr)
        return 4
    print(f"  {len(sbc_stream)} SBC bytes", flush=True)

    # Split the flat SBC stream into fixed-size frames (44 B for this codec).
    FRAME_LEN = 44
    frames = [
        sbc_stream[i : i + FRAME_LEN]
        for i in range(0, len(sbc_stream), FRAME_LEN)
        if i + FRAME_LEN <= len(sbc_stream)
    ]
    if not frames:
        print("ffmpeg produced no complete SBC frames; aborting.", file=sys.stderr)
        return 5

    # Sanity check the first frame's header to confirm the codec config
    # matches what the radio expects.
    from .audio.framing import decode_sbc_header
    first_hdr = decode_sbc_header(frames[0])
    if not first_hdr:
        print(f"First SBC frame has no valid sync byte: {frames[0][:4].hex()}",
              file=sys.stderr)
        return 6
    print(
        f"  codec: {first_hdr['sampling_frequency_hz']}Hz "
        f"{first_hdr['blocks']}bl {first_hdr['channel_mode']} "
        f"{first_hdr['allocation_method']} {first_hdr['subbands']}sb "
        f"bp{first_hdr['bitpool']}",
        flush=True,
    )
    if (first_hdr["sampling_frequency_hz"] != 32000
            or first_hdr["channel_mode"] != "mono"
            or first_hdr["subbands"] != 8
            or first_hdr["blocks"] != 16):
        print(
            "⚠  Encoder produced a different codec config than the radio "
            "expects. The radio may reject this stream.",
            flush=True,
        )

    # Build the HDLC-framed audio packets. The end-of-TX frame is handled
    # separately by transmit_rfcomm's tail-packet path (sent after a drain,
    # and repeated for robustness).
    packets = [build_audio_packet(f) for f in frames]
    total_bytes = sum(len(p) for p in packets) + len(END_OF_TX_PACKET)
    print(
        f"  {len(frames)} SBC frames → {len(packets)} packets "
        f"(+1 end frame, {total_bytes} wire bytes)",
        flush=True,
    )

    # Optional: collect any echo / response from the radio during the TX.
    rx_log: list[tuple[float, bytes]] = []

    def on_data(t_rel: float, chunk: bytes) -> None:
        rx_log.append((t_rel, chunk))

    print(
        f"Transmitting to {args.address} ch {args.channel} "
        f"at {args.pace*1000:.1f} ms/frame pacing...",
        flush=True,
    )
    r = rf.transmit_rfcomm(
        args.address,
        args.channel,
        packets,
        pace_interval_s=args.pace,
        open_timeout=args.open_timeout,
        on_data=on_data,
        tail_packet=END_OF_TX_PACKET,
        tail_repeats=3,
        tail_interval_s=0.05,
        post_drain_s=1.5,
    )
    print(
        f"Done. opened={r.opened} phase={r.phase} IOReturn={r.open_status}",
        flush=True,
    )
    if rx_log:
        print(f"Radio responded with {len(rx_log)} chunks during TX:")
        for t_rel, chunk in rx_log[:10]:
            print(f"  [{t_rel:7.3f}s] {len(chunk):3d}B {chunk[:32].hex()}")
        if len(rx_log) > 10:
            print(f"  ...{len(rx_log) - 10} more")
    return 0 if r.opened else 2


def _cmd_rfcomm_play(args: argparse.Namespace) -> int:
    """Phase 3d: open RFCOMM, deframe, decode SBC via ffmpeg, play PCM."""
    import threading

    from .audio import macos_rfcomm as rf
    from .audio.framing import Deframer, split_sbc_frames
    from .audio.sbc import SbcStream, SbcUnavailable
    try:
        import sounddevice as sd  # type: ignore
    except ImportError:
        print(
            "sounddevice not installed. Run: pip install sounddevice",
            file=sys.stderr,
        )
        return 4

    stop_flag = threading.Event()

    def _handle_sigint(_signum, _frame):
        stop_flag.set()

    prev = signal.signal(signal.SIGINT, _handle_sigint)

    # 32 kHz mono int16 — matches the codec config the radio sends.
    # blocksize=128 = one SBC frame of audio (4 ms) per callback, which
    # keeps the output ring buffer shallow. Any smaller and CoreAudio
    # starts underrunning on this host.
    pcm_stream = sd.RawOutputStream(
        samplerate=32000,
        channels=1,
        dtype="int16",
        blocksize=128,
        latency="low",
        device=_parse_sd_device(args.device),
    )
    pcm_stream.start()

    # ffmpeg decoder: SBC in, PCM out. When PCM comes out, shove it at
    # sounddevice. This callback runs on ffmpeg's stdout reader thread.
    total_pcm_bytes = [0]

    def on_pcm(pcm: bytes) -> None:
        total_pcm_bytes[0] += len(pcm)
        try:
            pcm_stream.write(pcm)
        except Exception:
            log.exception("sounddevice write failed")

    try:
        decoder = SbcStream(on_pcm=on_pcm)
    except SbcUnavailable as exc:
        print(f"SBC decoder unavailable: {exc}", file=sys.stderr)
        pcm_stream.stop()
        pcm_stream.close()
        signal.signal(signal.SIGINT, prev)
        return 3

    deframer = Deframer()
    total_sbc_frames = [0]
    last_report = [time.monotonic()]

    def on_bytes(t_rel: float, chunk: bytes) -> None:
        pkts = deframer.feed(chunk)
        for pkt in pkts:
            frames = split_sbc_frames(pkt)
            if not frames:
                continue
            # Concatenate and push to ffmpeg in one write — cheaper than
            # per-frame and ffmpeg handles streamed back-to-back SBC fine.
            decoder.feed(b"".join(frames))
            total_sbc_frames[0] += len(frames)
        now = time.monotonic()
        if now - last_report[0] > 1.0:
            last_report[0] = now
            print(
                f"[{t_rel:8.3f}s] sbc_frames={total_sbc_frames[0]} "
                f"pcm_out={total_pcm_bytes[0]}B",
                flush=True,
            )

    print(
        f"Opening RFCOMM ch {args.channel} on {args.address}, "
        f"playing to default audio device... (Ctrl-C to stop)",
        flush=True,
    )
    try:
        r = rf.dump_rfcomm(
            args.address,
            args.channel,
            on_bytes=on_bytes,
            stop=stop_flag.is_set,
            open_timeout=args.open_timeout,
        )
    finally:
        signal.signal(signal.SIGINT, prev)
        decoder.close()
        try:
            pcm_stream.stop()
            pcm_stream.close()
        except Exception:
            pass

    print(
        f"\nDone. {total_sbc_frames[0]} SBC frames in, "
        f"{total_pcm_bytes[0]} PCM bytes played. "
        f"phase={r.phase} IOReturn={r.open_status}"
    )
    return 0 if r.opened else 2


def _cmd_rfcomm_sbc_dump(args: argparse.Namespace) -> int:
    """Phase 3c: open RFCOMM, deframe 7E/7D, split SBC frames, print per-frame."""
    import threading

    from .audio import macos_rfcomm as rf
    from .audio.framing import Deframer, decode_sbc_header, split_sbc_frames

    stop_flag = threading.Event()

    def _handle_sigint(_signum, _frame):
        stop_flag.set()

    prev = signal.signal(signal.SIGINT, _handle_sigint)

    deframer = Deframer()
    total_bytes = 0
    total_pkts = 0
    total_sbc = 0
    non_sbc_pkts = 0

    def on_bytes(t_rel: float, chunk: bytes) -> None:
        nonlocal total_bytes, total_pkts, total_sbc, non_sbc_pkts
        total_bytes += len(chunk)
        pkts = deframer.feed(chunk)
        for pkt in pkts:
            total_pkts += 1
            frames = split_sbc_frames(pkt)
            if not frames:
                non_sbc_pkts += 1
                print(
                    f"[{t_rel:8.3f}s] pkt {len(pkt):3d}B  [non-SBC] {pkt.hex()}",
                    flush=True,
                )
                continue
            total_sbc += len(frames)
            print(
                f"[{t_rel:8.3f}s] pkt {len(pkt):3d}B  [SBC × {len(frames)}]",
                flush=True,
            )
            for i, f in enumerate(frames):
                tag = ""
                if args.verbose_headers:
                    try:
                        h = decode_sbc_header(f)
                    except Exception:
                        h = None
                    if h:
                        tag = (
                            f" [{h['sampling_frequency_hz']}Hz "
                            f"{h['blocks']}bl {h['channel_mode']} "
                            f"{h['allocation_method']} "
                            f"{h['subbands']}sb bp{h['bitpool']}]"
                        )
                hex_s = f.hex() if not args.short else f[:12].hex() + "..."
                print(
                    f"[{t_rel:8.3f}s]   frame {i}: {len(f):2d}B{tag}  {hex_s}",
                    flush=True,
                )

    print(
        f"Opening RFCOMM ch {args.channel} on {args.address}... (Ctrl-C to stop)",
        flush=True,
    )
    try:
        r = rf.dump_rfcomm(
            args.address,
            args.channel,
            on_bytes=on_bytes,
            stop=stop_flag.is_set,
            open_timeout=args.open_timeout,
        )
    finally:
        signal.signal(signal.SIGINT, prev)

    print(
        f"\nDone. {total_bytes}B raw → {total_pkts} pkts "
        f"({total_sbc} SBC frames, {non_sbc_pkts} non-SBC). "
        f"phase={r.phase} IOReturn={r.open_status}"
    )
    return 0 if r.opened else 2


def _cmd_rfcomm_dump(args: argparse.Namespace) -> int:
    """Phase 3b: open RFCOMM channel and stream raw inbound bytes until Ctrl-C."""
    import threading

    from .audio import macos_rfcomm as rf

    stop_flag = threading.Event()

    def _handle_sigint(_signum, _frame):
        stop_flag.set()

    # Use the plain signal module; the IOBluetooth runloop is sync, not asyncio.
    prev = signal.signal(signal.SIGINT, _handle_sigint)

    total = 0
    max_bytes_per_line = args.width

    def on_bytes(t_rel: float, chunk: bytes) -> None:
        nonlocal total
        total += len(chunk)
        # Break long chunks into fixed-width hex lines for readability.
        for i in range(0, len(chunk), max_bytes_per_line):
            piece = chunk[i : i + max_bytes_per_line]
            print(
                f"[{t_rel:8.3f}s] +{total - len(chunk) + i + len(piece):6d}B  "
                f"{piece.hex(' ')}",
                flush=True,
            )

    print(
        f"Opening RFCOMM ch {args.channel} on {args.address}... (Ctrl-C to stop)",
        flush=True,
    )
    try:
        r = rf.dump_rfcomm(
            args.address,
            args.channel,
            on_bytes=on_bytes,
            stop=stop_flag.is_set,
            open_timeout=args.open_timeout,
        )
    finally:
        signal.signal(signal.SIGINT, prev)

    print(f"\nDone. {total} bytes captured. phase={r.phase} IOReturn={r.open_status}")
    return 0 if r.opened else 2


def _cmd_rfcomm_probe(args: argparse.Namespace) -> int:
    """Phase 3a: list paired devices / enumerate SDP services / try a channel."""
    # Import lazily so `benshi --help` still works on non-Darwin hosts or
    # when IOBluetooth isn't installed.
    from .audio import macos_rfcomm as rf

    try:
        if args.address is None:
            devices = rf.list_paired_devices()
            if not devices:
                print("No paired Classic-BT devices.")
                print(
                    "Pair the radio first: System Settings → Bluetooth → "
                    "select the radio."
                )
                return 1
            print(f"{len(devices)} paired device(s):")
            for d in devices:
                state = "connected" if d.is_connected else "disconnected"
                print(f"  {d.address}  {d.name!r:30s}  [{state}]")
            return 0

        probe = rf.probe_services(args.address, timeout=args.sdp_timeout)
        print(f"Device: {probe.name!r}  address={probe.address}")
        if not probe.services:
            print("  (no services returned after SDP query; radio may not "
                  "respond to SDP while BLE-connected)")
        else:
            for i, svc in enumerate(probe.services):
                ch = svc.rfcomm_channel
                ch_s = f"rfcomm_ch={ch}" if ch is not None else "no RFCOMM channel"
                uuids = ", ".join(svc.service_class_uuids) or "(no uuids)"
                print(f"  [{i:02d}] {svc.name!r:30s} {ch_s:16s}  uuids={uuids}")

        if args.try_channel is not None:
            print(f"\nAttempting RFCOMM open on channel {args.try_channel}...")
            r = rf.try_open_rfcomm(
                args.address, args.try_channel, open_timeout=args.open_timeout
            )
            status_s = (
                f"IOReturn={r.open_status}" if r.open_status is not None else "n/a"
            )
            verdict = "SUCCESS" if r.opened else "FAILED"
            print(
                f"  {verdict}  phase={r.phase}  {status_s}"
            )
            return 0 if r.opened else 2
        return 0
    except rf.IOBluetoothUnavailable as exc:
        print(f"IOBluetooth not available: {exc}", file=sys.stderr)
        return 3


async def _cmd_sniff_all(args: argparse.Namespace) -> int:
    """Subscribe to every notify-capable characteristic on every service,
    and register for HT_STATUS_CHANGED so we have an RX-edge marker in the
    same log stream.

    Intended as the Phase 2 follow-up: tells us whether any undocumented
    service on the radio (e.g. the mystery ba2a-... service on UV-PRO)
    carries data during FM RX.
    """
    from bleak import BleakClient

    from .link import RADIO_WRITE_UUID

    t0 = time.monotonic()
    async with BleakClient(args.address) as client:
        print(f"Connected. Services on {args.address}:", flush=True)
        targets: list[tuple[str, str]] = []  # (service_uuid, char_uuid)
        for service in client.services:
            print(f"  service {service.uuid}", flush=True)
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"    char  {char.uuid}  [{props}]", flush=True)
                if "notify" in char.properties or "indicate" in char.properties:
                    targets.append((service.uuid, char.uuid))

        if not targets:
            print("No notify/indicate-capable characteristics found.", flush=True)
            return 1

        def _short(u: str) -> str:
            return u.split("-")[0]

        def make_cb(svc: str, ch: str) -> t.Callable[[t.Any, bytearray], None]:
            def cb(_char: object, data: bytearray) -> None:
                t_rel = time.monotonic() - t0
                print(
                    f"[{t_rel:8.3f}s] svc={_short(svc)} ch={_short(ch)} "
                    f"{len(data):3d}B {bytes(data).hex()}",
                    flush=True,
                )
            return cb

        for svc, ch in targets:
            try:
                await client.start_notify(ch, make_cb(svc, ch))
                print(f"  subscribed to {ch}", flush=True)
            except Exception as exc:
                print(f"  FAILED subscribing to {ch}: {exc}", flush=True)

        # Ask the radio to start pushing HT_STATUS_CHANGED events so we
        # have an in-band RX-start / RX-end marker in the log.
        # REGISTER_NOTIFICATION: group=BASIC(0x0002), cmd=6, body = event_type.
        # HT_STATUS_CHANGED = 1.
        register_ht_status = bytes.fromhex("0002000601")
        try:
            await client.write_gatt_char(
                RADIO_WRITE_UUID, register_ht_status, response=True
            )
            print(
                "  registered for HT_STATUS_CHANGED "
                "(watch svc=00001100 ch=00001102 for RX/TX edges)",
                flush=True,
            )
        except Exception as exc:
            print(f"  failed to register HT_STATUS_CHANGED: {exc}", flush=True)

        print(
            f"\nListening on {len(targets)} characteristic(s). Ctrl-C to stop.",
            flush=True,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
    return 0


async def _cmd_sniff(args: argparse.Namespace) -> int:
    t0 = time.monotonic()

    def _on_frame(frame: bytes) -> None:
        t_rel = time.monotonic() - t0
        # BLE carries raw Messages (no GAIA FF-01 wrapper) on this radio.
        try:
            msg = p.Message.from_bytes(frame)
            tag = f"{msg.command_group.name}.{msg.command.name}"
            if msg.is_reply:
                tag += "[reply]"
        except Exception:
            # Fall back to GAIA-wrapped interpretation, then give up.
            try:
                gf = p.GaiaFrame.from_bytes(frame)
                m = p.Message.from_bytes(gf.data)
                tag = f"[GAIA] {m.command_group.name}.{m.command.name}"
            except Exception:
                tag = "<unparsed>"
        print(
            f"[{t_rel:8.3f}s] {len(frame):3d}B {tag:40s} {frame.hex()}",
            flush=True,
        )

    async with Radio(args.address) as radio:
        radio.set_raw_frame_handler(_on_frame)
        for ev in args.register:
            await radio.register_notification(p.EventType[ev])
        print(f"Sniffing {args.address}. Ctrl-C to stop.", flush=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
    return 0


def _print_bitfield(obj: object, indent: str = "") -> None:
    # Bitfield types expose declared fields via class-level descriptors; just
    # use vars() / __dict__ for a best-effort pretty print.
    d = getattr(obj, "__dict__", None)
    if not d:
        print(f"{indent}{obj!r}")
        return
    for k, v in d.items():
        if k.startswith("_"):
            continue
        print(f"{indent}{k} = {v!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benshi")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v info, -vv debug"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list BLE devices in range")
    s.add_argument("--timeout", type=float, default=5.0)
    s.add_argument(
        "--only-benshi",
        action="store_true",
        help="show only devices advertising the Benshi service UUID "
        "(most radios don't, so this usually returns nothing)",
    )
    s.set_defaults(func=_cmd_scan)

    c = sub.add_parser("connect", help="connect and tail notifications")
    c.add_argument("address")
    c.add_argument("--channels", type=int, default=32)
    c.set_defaults(func=_cmd_connect)

    ch = sub.add_parser("channels", help="dump channel table")
    ch.add_argument("address")
    ch.add_argument("--count", type=int, default=200)
    ch.set_defaults(func=_cmd_channels)

    sn = sub.add_parser("sniff", help="log every inbound GAIA frame as hex")
    sn.add_argument("address")
    sn.add_argument(
        "--register",
        action="append",
        default=[],
        metavar="EVENT_TYPE",
        help="EventType name to register (repeatable). Defaults to none.",
    )
    sn.set_defaults(func=_cmd_sniff)

    sa = sub.add_parser(
        "sniff-all",
        help="subscribe to every notify/indicate char on every service",
    )
    sa.add_argument("address")
    sa.set_defaults(func=_cmd_sniff_all)

    ri = sub.add_parser(
        "rfcomm-inspect",
        help="Dump all SDP service records and their attributes. Read-only.",
    )
    ri.add_argument("address", help="BT Classic MAC")
    ri.add_argument("--sdp-timeout", type=float, default=10.0)
    ri.add_argument(
        "--dump-attrs",
        action="store_true",
        default=True,
        help="(default) print full attribute tree for each record",
    )
    ri.add_argument(
        "--summary-only",
        dest="dump_attrs",
        action="store_false",
        help="just the summary table, no attribute dump",
    )
    ri.set_defaults(func=_cmd_rfcomm_inspect)

    rd = sub.add_parser(
        "rfcomm-dump",
        help="Phase 3b: open RFCOMM channel and hex-dump inbound bytes",
    )
    rd.add_argument("address", help="BT Classic MAC")
    rd.add_argument("--channel", type=int, default=1)
    rd.add_argument("--open-timeout", type=float, default=5.0)
    rd.add_argument(
        "--width",
        type=int,
        default=32,
        help="Max bytes per output line (default 32).",
    )
    rd.set_defaults(func=_cmd_rfcomm_dump)

    rp = sub.add_parser(
        "rfcomm-play",
        help="Phase 3d: RFCOMM → deframe → SBC decode → speaker",
    )
    rp.add_argument("address", help="BT Classic MAC")
    rp.add_argument("--channel", type=int, default=2)
    rp.add_argument("--open-timeout", type=float, default=10.0)
    rp.add_argument(
        "--device", default=None,
        help="sounddevice output device name or index. Default: system "
             "default output. Run `python -c \"import sounddevice; "
             "print(sounddevice.query_devices())\"` to list.",
    )
    rp.set_defaults(func=_cmd_rfcomm_play)

    ad = sub.add_parser(
        "audio-devices",
        help="List input and output devices visible to sounddevice",
    )
    ad.set_defaults(func=_cmd_audio_devices)

    rm = sub.add_parser(
        "rfcomm-tx-mic",
        help="Phase 3e-2: live mic capture → streaming SBC → RFCOMM TX",
    )
    rm.add_argument("address", help="BT Classic MAC")
    rm.add_argument("--channel", type=int, default=2)
    rm.add_argument(
        "--duration", type=float, default=None,
        help="Auto-stop after N seconds. Default: run until Ctrl-C.",
    )
    rm.add_argument(
        "--device", default=None,
        help="sounddevice input device name or index. Default: system default "
             "input. Run `python -c \"import sounddevice; "
             "print(sounddevice.query_devices())\"` to list.",
    )
    rm.add_argument("--open-timeout", type=float, default=10.0)
    rm.set_defaults(func=_cmd_rfcomm_tx_mic)

    rt = sub.add_parser(
        "rfcomm-tx-tone",
        help="Phase 3e-1: transmit a test-tone sine wave (no mic needed)",
    )
    rt.add_argument("address", help="BT Classic MAC")
    rt.add_argument("--channel", type=int, default=2)
    rt.add_argument(
        "--preset",
        choices=["ce3k", "ce3k-high"],
        default=None,
        help="Play a preset instead of a single sine tone. "
             "'ce3k' = Close Encounters 5-note motif at original pitch. "
             "'ce3k-high' = same motif shifted up one octave so every note "
             "clears the narrowband FM high-pass filter.",
    )
    rt.add_argument("--freq", type=float, default=1000.0,
                    help="Sine tone frequency, Hz (default 1000, ignored if "
                         "--preset is set)")
    rt.add_argument("--duration", type=float, default=1.0,
                    help="Tone duration, seconds (default 1.0, ignored if "
                         "--preset is set)")
    rt.add_argument("--amplitude", type=float, default=0.2,
                    help="Amplitude 0-1 (default 0.2 — loud enough to hear, "
                         "quiet enough not to clip)")
    rt.add_argument(
        "--pace", type=float, default=0.0,
        help="Seconds to sleep between frame writes. Default 0 — writeSync's "
             "own flow control handles pacing, and any artificial sleep here "
             "compounds with writeSync's latency to starve the radio's audio "
             "buffer. Set to a small positive number only if you specifically "
             "want to rate-limit TX for debugging.",
    )
    rt.add_argument("--open-timeout", type=float, default=10.0)
    rt.set_defaults(func=_cmd_rfcomm_tx_tone)

    rs = sub.add_parser(
        "rfcomm-sbc-dump",
        help="Phase 3c: deframe 7E/7D, split SBC frames, print per-frame",
    )
    rs.add_argument("address", help="BT Classic MAC")
    rs.add_argument("--channel", type=int, default=2)
    rs.add_argument("--open-timeout", type=float, default=10.0)
    rs.add_argument(
        "--short",
        action="store_true",
        help="Print only first 12 bytes of each SBC frame's hex",
    )
    rs.add_argument(
        "--verbose-headers",
        action="store_true",
        default=True,
        help="(default) annotate each frame with decoded SBC header",
    )
    rs.add_argument(
        "--no-headers",
        dest="verbose_headers",
        action="store_false",
        help="omit SBC header annotations",
    )
    rs.set_defaults(func=_cmd_rfcomm_sbc_dump)

    rf = sub.add_parser(
        "rfcomm-probe",
        help="Phase 3a: list paired BT Classic devices / SDP / try a channel",
    )
    rf.add_argument(
        "address",
        nargs="?",
        default=None,
        help="BT Classic MAC (AA:BB:CC:DD:EE:FF). Omit to list paired devices.",
    )
    rf.add_argument("--sdp-timeout", type=float, default=10.0)
    rf.add_argument(
        "--try-channel",
        type=int,
        default=None,
        help="After SDP, attempt to open this RFCOMM channel then close.",
    )
    rf.add_argument("--open-timeout", type=float, default=5.0)
    rf.set_defaults(func=_cmd_rfcomm_probe)

    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    try:
        result = args.func(args)
        if asyncio.iscoroutine(result):
            return asyncio.run(result)
        return result
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
