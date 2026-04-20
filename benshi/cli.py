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

log = logging.getLogger(__name__)

from . import protocol as p
from .link import scan as ble_scan, RADIO_SERVICE_UUID
from .radio import Radio


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
        pcm_stream.stop(); pcm_stream.close()
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
    from .audio.framing import Deframer, split_sbc_frames, decode_sbc_header

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
    rp.set_defaults(func=_cmd_rfcomm_play)

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
