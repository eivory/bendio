"""macOS Bluetooth Classic RFCOMM SPP transport via PyObjC + IOBluetooth.

Phase 3a scope (this file so far): just enumerate paired devices, run an SDP
query, and attempt to open a single RFCOMM channel. No data flow, no framing.
The goal is purely to prove that the plumbing works end-to-end on macOS.

Threading model
---------------

``IOBluetooth`` delivers all its results through delegate callbacks that fire
on whichever thread owns the runloop that was current at registration time.
From Python we drive the main thread's runloop manually with short pumps
(see :func:`_pump_runloop`). That keeps the whole thing synchronous-looking
for test code and avoids having to stand up a dedicated ObjC runloop thread
at this stage. If/when we move to continuous audio streaming (Phase 3d+)
we'll promote this to a dedicated thread with its own ``NSRunLoop``.

Permissions
-----------

macOS TCC gates Bluetooth access. The process must have
``NSBluetoothAlwaysUsageDescription`` in its bundle ``Info.plist`` *and* the
user must have approved Bluetooth for it (or for a responsible-process
ancestor). ``scripts/mac_bluetooth_setup.py`` takes care of the plist;
approving the permission happens on first use.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

# These imports are only importable on macOS with pyobjc-framework-IOBluetooth
# installed. We guard here so the rest of the benshi package remains importable
# on non-Darwin hosts.
try:  # pragma: no cover - import is platform-dependent
    from Foundation import NSDate, NSRunLoop  # type: ignore
    from IOBluetooth import IOBluetoothDevice  # type: ignore
    _IOBLUETOOTH_AVAILABLE = True
    _IMPORT_ERROR: Optional[Exception] = None
except Exception as _exc:  # pragma: no cover
    _IOBLUETOOTH_AVAILABLE = False
    _IMPORT_ERROR = _exc

log = logging.getLogger(__name__)


class IOBluetoothUnavailable(RuntimeError):
    """Raised if we're not on macOS or the IOBluetooth bindings aren't usable."""


def _require_iobluetooth() -> None:
    if not _IOBLUETOOTH_AVAILABLE:
        raise IOBluetoothUnavailable(
            f"pyobjc IOBluetooth bindings unavailable: {_IMPORT_ERROR!r}"
        )


def _pump_runloop(seconds: float) -> None:
    """Let the current-thread NSRunLoop process pending sources for ``seconds``."""
    NSRunLoop.currentRunLoop().runUntilDate_(
        NSDate.dateWithTimeIntervalSinceNow_(seconds)
    )


# --------------------------------------------------------------------- models

@dataclass
class PairedDevice:
    name: str
    address: str  # Classic-BT MAC, e.g. "AA:BB:CC:DD:EE:FF"
    is_connected: bool


@dataclass
class ServiceRecord:
    name: Optional[str]
    rfcomm_channel: Optional[int]  # None if not an RFCOMM service
    service_class_uuids: list[str]  # 16-bit or 128-bit UUID strings


@dataclass
class SdpProbeResult:
    name: Optional[str]
    address: str
    services: list[ServiceRecord]


@dataclass
class OpenAttemptResult:
    address: str
    channel: int
    opened: bool
    open_status: Optional[int]  # IOReturn; 0 = kIOReturnSuccess
    phase: str                  # "open_call", "timeout", "open_complete", "closed"


# --------------------------------------------------------------- operations

def list_paired_devices() -> list[PairedDevice]:
    """Return every Classic-BT device currently paired with this Mac."""
    _require_iobluetooth()
    devices = IOBluetoothDevice.pairedDevices() or []
    out: list[PairedDevice] = []
    for d in devices:
        out.append(
            PairedDevice(
                name=str(d.name() or ""),
                address=str(d.addressString() or ""),
                is_connected=bool(d.isConnected()),
            )
        )
    return out


def _device_by_address(address: str):
    _require_iobluetooth()
    dev = IOBluetoothDevice.deviceWithAddressString_(address)
    if dev is None:
        raise RuntimeError(f"no IOBluetoothDevice for address {address!r}")
    return dev


def probe_services(address: str, timeout: float = 10.0) -> SdpProbeResult:
    """Run an SDP query and return the service records.

    This **forces a fresh SDP query** (``performSDPQuery:``) and pumps the
    runloop until either services appear or ``timeout`` elapses. Benshi
    radios in particular often don't publish their services in the cached
    form macOS keeps from the initial pairing, so blindly calling ``.services``
    without an SDP refresh frequently returns ``None``.
    """
    dev = _device_by_address(address)

    err = dev.performSDPQuery_(None)
    # err != 0 isn't necessarily fatal — some firmware still populates services
    # even when performSDPQuery_ returns a non-zero IOReturn. Log and continue.
    if err != 0:
        log.info("performSDPQuery_ returned %d (continuing)", err)

    deadline = time.monotonic() + timeout
    services = None
    while time.monotonic() < deadline:
        _pump_runloop(0.1)
        services = dev.services()
        if services:
            break

    result = SdpProbeResult(
        name=str(dev.name() or "") or None,
        address=address,
        services=[],
    )

    for svc in services or []:
        # Try to extract an RFCOMM channel number if this is an RFCOMM service.
        channel: Optional[int] = None
        try:
            status, chan = svc.getRFCOMMChannelID_(None)
            if status == 0:
                channel = int(chan)
        except Exception:
            channel = None

        # Service class UUIDs: these come through as IOBluetoothSDPUUID objects.
        uuids: list[str] = []
        try:
            items = svc.getServiceClassUUIDs() or []
            for u in items:
                # IOBluetoothSDPUUID has -getUUIDString or similar depending on OS
                desc = None
                for attr in ("getUUIDString", "UUIDString", "description"):
                    try:
                        desc = getattr(u, attr)()
                        if desc:
                            break
                    except Exception:
                        continue
                uuids.append(str(desc) if desc is not None else repr(u))
        except Exception as exc:
            log.debug("couldn't enumerate service class UUIDs: %r", exc)

        name = None
        try:
            name = str(svc.getServiceName() or "") or None
        except Exception:
            name = None

        result.services.append(
            ServiceRecord(name=name, rfcomm_channel=channel, service_class_uuids=uuids)
        )

    return result


def dump_rfcomm(
    address: str,
    channel: int,
    on_bytes: "callable[[float, bytes], None]",
    stop: "callable[[], bool]",
    *,
    open_timeout: float = 5.0,
) -> OpenAttemptResult:
    """Open an RFCOMM channel and stream raw inbound bytes until ``stop()``.

    ``on_bytes(t_rel, chunk)`` is called for every data callback we receive
    from IOBluetooth, with ``t_rel`` measured from the moment the channel
    opens. ``stop()`` is polled between runloop pumps; return True to exit.
    The channel is closed cleanly on exit.
    """
    _require_iobluetooth()
    import objc  # type: ignore
    from Foundation import NSObject  # type: ignore

    t_open: list[float] = []  # populated once open-complete fires

    class _Delegate(NSObject):
        def init(self):  # type: ignore[override]
            self = objc.super(_Delegate, self).init()
            if self is None:
                return None
            self.open_status = None
            self.was_opened = False
            self.is_open = False
            self.closed = False
            return self

        def rfcommChannelOpenComplete_status_(self, ch, status):  # noqa: N802
            self.open_status = int(status)
            if status == 0:
                self.was_opened = True
                self.is_open = True
                t_open.append(time.monotonic())

        def rfcommChannelClosed_(self, ch):  # noqa: N802
            self.is_open = False
            self.closed = True

        def rfcommChannelData_data_length_(self, ch, data, length):  # noqa: N802
            if length <= 0 or data is None:
                return
            # PyObjC delivers `data` as a memoryview-like buffer for void*.
            # Slice by length to be safe.
            try:
                chunk = bytes(data[:length])
            except Exception:
                try:
                    chunk = bytes(data)[:length]
                except Exception:
                    log.exception("couldn't extract bytes from rfcomm data")
                    return
            t_rel = time.monotonic() - (t_open[0] if t_open else time.monotonic())
            try:
                on_bytes(t_rel, chunk)
            except Exception:
                log.exception("on_bytes callback raised")

    dev = _device_by_address(address)
    delegate = _Delegate.alloc().init()

    try:
        status, ch = dev.openRFCOMMChannelAsync_withChannelID_delegate_(
            None, channel, delegate
        )
    except Exception as exc:
        log.exception("openRFCOMMChannelAsync raised")
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=None, phase=f"open_call_exception:{exc!r}",
        )

    if status != 0:
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=int(status), phase="open_call",
        )

    # Wait for open complete
    deadline = time.monotonic() + open_timeout
    while time.monotonic() < deadline:
        _pump_runloop(0.1)
        if delegate.open_status is not None:
            break
    if delegate.open_status is None:
        try:
            if ch is not None:
                ch.closeChannel()
        except Exception:
            pass
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=None, phase="timeout",
        )
    if not delegate.was_opened:
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=delegate.open_status, phase="open_complete",
        )

    # Streaming loop. Pump briefly, check stop, repeat.
    try:
        while not stop():
            _pump_runloop(0.1)
            if delegate.closed:
                break
    finally:
        try:
            if ch is not None and not delegate.closed:
                ch.closeChannel()
        except Exception:
            log.exception("closeChannel raised")
        # Let the close callback fire.
        for _ in range(20):
            if delegate.closed:
                break
            _pump_runloop(0.05)

    phase = "closed" if delegate.closed else "open_complete"
    return OpenAttemptResult(
        address=address, channel=channel, opened=delegate.was_opened,
        open_status=delegate.open_status, phase=phase,
    )


# --------------------------------------------------------- SDP introspection
#
# PyObjC's default binding for ``-[IOBluetoothSDPServiceRecord
# getRFCOMMChannelID:]`` seems to lose the out-parameter; in our probe we
# couldn't extract channel numbers that way. As a workaround we walk the
# raw attribute dictionary ourselves, pulling the channel out of the
# ProtocolDescriptorList (attribute 0x0004).

_SDP_TYPE_NAMES = {
    0: "Nil", 1: "UInt", 2: "Int", 3: "UUID", 4: "String",
    5: "Bool", 6: "Sequence", 7: "Alternative", 8: "URL",
}


def _dump_data_element(elem) -> dict:
    """Recursively describe an IOBluetoothSDPDataElement as plain dicts."""
    out: dict = {}
    try:
        t = int(elem.getTypeDescriptor())
    except Exception:
        t = -1
    out["type"] = t
    out["type_name"] = _SDP_TYPE_NAMES.get(t, f"?{t}")
    try:
        out["size"] = int(elem.getSizeDescriptor())
    except Exception:
        pass

    if t in (1, 2):  # UInt / Int
        try:
            out["value"] = int(elem.getNumberValue())
        except Exception:
            pass
    elif t == 3:  # UUID
        try:
            u = elem.getUUIDValue()
            # Description is e.g. "{length = 4, bytes = 0x00000003}" for 32-bit
            # or "{length = 16, bytes = 0x00000003...}" for 128-bit. Parse the
            # hex and keep the low 16 bits as a short form for comparison.
            import re
            desc = str(u.description())
            out["uuid"] = desc
            m = re.search(r"bytes\s*=\s*0x([0-9a-fA-F]+)", desc)
            if m:
                hx = m.group(1).lower()
                out["uuid_hex"] = hx
                # Bluetooth Base UUID aliases the low 32 bits to 16-bit/32-bit
                # short UUIDs. For 16-byte UUIDs take the first 8 hex chars.
                first32 = hx[:8] if len(hx) >= 8 else hx.zfill(8)
                out["uuid_short_hex"] = first32[-4:]  # last 4 hex = 16-bit form
        except Exception:
            pass
    elif t == 4:  # String
        try:
            out["value"] = str(elem.getStringValue())
        except Exception:
            pass
    elif t == 5:  # Bool
        try:
            out["value"] = bool(elem.getNumberValue())
        except Exception:
            pass
    elif t in (6, 7):  # Sequence / Alternative
        try:
            arr = elem.getArrayValue() or []
            out["array"] = [_dump_data_element(a) for a in arr]
        except Exception:
            pass
    return out


def _extract_rfcomm_channel(pdl_elem: dict) -> Optional[int]:
    """Walk a dumped ProtocolDescriptorList dict, return RFCOMM channel #."""
    if pdl_elem.get("type_name") not in ("Sequence", "Alternative"):
        return None
    for layer in pdl_elem.get("array") or []:
        members = layer.get("array") or []
        if not members:
            continue
        first = members[0]
        # First member should be the protocol UUID
        short = first.get("uuid_short_hex")
        if short == "0003" and len(members) >= 2:
            ch = members[1].get("value")
            if isinstance(ch, int):
                return ch
    return None


@dataclass
class InspectedService:
    index: int
    name: Optional[str]
    rfcomm_channel: Optional[int]
    attributes: dict  # {attribute_id: dumped_element}


def inspect_services(address: str, timeout: float = 10.0) -> list[InspectedService]:
    """SDP query + deep attribute walk. For understanding an unknown device.

    Never opens a connection. Safe to re-run.
    """
    _require_iobluetooth()
    dev = _device_by_address(address)

    err = dev.performSDPQuery_(None)
    if err != 0:
        log.info("performSDPQuery_ returned %d (continuing)", err)

    deadline = time.monotonic() + timeout
    services = None
    while time.monotonic() < deadline:
        _pump_runloop(0.1)
        services = dev.services()
        if services:
            break

    out: list[InspectedService] = []
    for idx, svc in enumerate(services or []):
        name = None
        try:
            n = svc.getServiceName()
            if n:
                name = str(n)
        except Exception:
            pass

        attrs_dict: dict = {}
        try:
            raw = svc.attributes() or {}
            for k, v in raw.items():
                try:
                    kid = int(k)
                except Exception:
                    kid = -1
                attrs_dict[kid] = _dump_data_element(v)
        except Exception:
            log.exception("couldn't walk attributes on record %d", idx)

        ch: Optional[int] = None
        pdl = attrs_dict.get(0x0004)
        if pdl is not None:
            ch = _extract_rfcomm_channel(pdl)

        out.append(InspectedService(
            index=idx, name=name, rfcomm_channel=ch, attributes=attrs_dict
        ))
    return out


class RfcommTxSession:
    """Long-lived RFCOMM write session for streaming TX.

    Caller pattern::

        session = RfcommTxSession(address, channel)
        session.open()                       # blocks until open-complete
        # start mic → encoder → session.write() pipeline on other threads
        while not user_stopped():
            session.pump(0.05)               # drive the main-thread runloop
        session.close(tail_packet=END_OF_TX_PACKET)

    ``write`` is safe to call from any thread (Apple documents
    ``IOBluetoothRFCOMMChannel``'s ``-writeSync:length:`` as thread-safe).
    ``pump`` must be called from the thread that opened the session —
    typically the main thread — to drive delegate callbacks.
    """

    def __init__(self, address: str, channel: int) -> None:
        _require_iobluetooth()
        self._address = address
        self._channel_id = channel
        self._ch = None  # IOBluetoothRFCOMMChannel
        self._delegate = None
        self._open_result: Optional[OpenAttemptResult] = None
        self._write_errors = 0

    @property
    def write_errors(self) -> int:
        return self._write_errors

    @property
    def opened(self) -> bool:
        return self._delegate is not None and bool(self._delegate.was_opened)

    def open(
        self,
        on_data: "Optional[callable[[float, bytes], None]]" = None,
        *,
        open_timeout: float = 10.0,
    ) -> OpenAttemptResult:
        import objc  # type: ignore
        from Foundation import NSObject  # type: ignore

        t_open: list[float] = []

        class _Delegate(NSObject):
            def init(self):  # type: ignore[override]
                self = objc.super(_Delegate, self).init()
                if self is None:
                    return None
                self.open_status = None
                self.was_opened = False
                self.is_open = False
                self.closed = False
                return self

            def rfcommChannelOpenComplete_status_(self, ch, status):  # noqa: N802
                self.open_status = int(status)
                if status == 0:
                    self.was_opened = True
                    self.is_open = True
                    t_open.append(time.monotonic())

            def rfcommChannelClosed_(self, ch):  # noqa: N802
                self.is_open = False
                self.closed = True

            def rfcommChannelData_data_length_(self, ch, data, length):  # noqa: N802
                if on_data is None or length <= 0 or data is None:
                    return
                try:
                    chunk = bytes(data[:length])
                except Exception:
                    try:
                        chunk = bytes(data)[:length]
                    except Exception:
                        return
                t_rel = (
                    time.monotonic() - t_open[0]
                    if t_open
                    else 0.0
                )
                try:
                    on_data(t_rel, chunk)
                except Exception:
                    log.exception("on_data callback raised")

        dev = _device_by_address(self._address)
        delegate = _Delegate.alloc().init()
        try:
            status, ch = dev.openRFCOMMChannelAsync_withChannelID_delegate_(
                None, self._channel_id, delegate
            )
        except Exception as exc:
            self._open_result = OpenAttemptResult(
                address=self._address, channel=self._channel_id,
                opened=False, open_status=None,
                phase=f"open_call_exception:{exc!r}",
            )
            return self._open_result
        if status != 0:
            self._open_result = OpenAttemptResult(
                address=self._address, channel=self._channel_id,
                opened=False, open_status=int(status), phase="open_call",
            )
            return self._open_result

        deadline = time.monotonic() + open_timeout
        while time.monotonic() < deadline:
            _pump_runloop(0.1)
            if delegate.open_status is not None:
                break
        if not delegate.was_opened:
            try:
                if ch is not None:
                    ch.closeChannel()
            except Exception:
                pass
            self._open_result = OpenAttemptResult(
                address=self._address, channel=self._channel_id,
                opened=False, open_status=delegate.open_status, phase="timeout",
            )
            return self._open_result

        self._ch = ch
        self._delegate = delegate
        self._open_result = OpenAttemptResult(
            address=self._address, channel=self._channel_id,
            opened=True, open_status=0, phase="open_complete",
        )
        return self._open_result

    def write(self, packet: bytes) -> int:
        """Synchronously write one RFCOMM packet. Returns IOReturn (0 = OK)."""
        if self._ch is None:
            raise RuntimeError("channel not open")
        try:
            status = self._ch.writeSync_length_(packet, len(packet))
        except Exception:
            log.exception("writeSync_length_ raised")
            self._write_errors += 1
            return -1
        if status != 0:
            self._write_errors += 1
        return int(status)

    def pump(self, seconds: float = 0.05) -> None:
        """Pump the main-thread runloop. Call from the thread that opened()."""
        _pump_runloop(seconds)

    def close(
        self,
        *,
        tail_packet: "Optional[bytes]" = None,
        tail_repeats: int = 3,
        tail_interval_s: float = 0.05,
        post_drain_s: float = 1.5,
    ) -> None:
        """Send the tail frame (repeated), drain, then close the channel."""
        if self._ch is None:
            return
        try:
            if tail_packet is not None:
                for _ in range(max(1, tail_repeats)):
                    self.write(tail_packet)
                    _pump_runloop(tail_interval_s)
            _pump_runloop(post_drain_s)
        finally:
            try:
                if not self._delegate.closed:
                    self._ch.closeChannel()
            except Exception:
                log.exception("closeChannel raised")
            for _ in range(20):
                if self._delegate.closed:
                    break
                _pump_runloop(0.05)
            self._ch = None


def transmit_rfcomm(
    address: str,
    channel: int,
    packets: "list[bytes]",
    *,
    pace_interval_s: float = 0.004,
    open_timeout: float = 10.0,
    on_data: "Optional[callable[[float, bytes], None]]" = None,
    tail_packet: "Optional[bytes]" = None,
    tail_repeats: int = 3,
    tail_interval_s: float = 0.05,
    post_drain_s: float = 1.5,
) -> OpenAttemptResult:
    """Open an RFCOMM channel, write ``packets`` in order (one per call to
    ``writeSync:length:``), then close.

    Between writes we pump the runloop for ``pace_interval_s`` seconds, which
    serves two purposes: it gives the IOBluetooth plumbing time to deliver
    each write to the peer, and it naturally paces output so a 4 ms interval
    produces real-time audio (one SBC frame = 4 ms of audio at this codec).

    Optional ``on_data`` callback fires for any bytes the radio sends us
    during the transmission. Most of the time we won't need it, but it lets
    us snoop for e.g. an echo of our own end-of-TX frame as acknowledgement.
    """
    _require_iobluetooth()
    import objc  # type: ignore
    from Foundation import NSObject  # type: ignore

    t_open: list[float] = []

    class _Delegate(NSObject):
        def init(self):  # type: ignore[override]
            self = objc.super(_Delegate, self).init()
            if self is None:
                return None
            self.open_status = None
            self.was_opened = False
            self.is_open = False
            self.closed = False
            return self

        def rfcommChannelOpenComplete_status_(self, ch, status):  # noqa: N802
            self.open_status = int(status)
            if status == 0:
                self.was_opened = True
                self.is_open = True
                t_open.append(time.monotonic())

        def rfcommChannelClosed_(self, ch):  # noqa: N802
            self.is_open = False
            self.closed = True

        def rfcommChannelData_data_length_(self, ch, data, length):  # noqa: N802
            if on_data is None or length <= 0 or data is None:
                return
            try:
                chunk = bytes(data[:length])
            except Exception:
                try:
                    chunk = bytes(data)[:length]
                except Exception:
                    return
            t_rel = time.monotonic() - (t_open[0] if t_open else time.monotonic())
            try:
                on_data(t_rel, chunk)
            except Exception:
                log.exception("on_data callback raised")

    dev = _device_by_address(address)
    delegate = _Delegate.alloc().init()
    try:
        status, ch = dev.openRFCOMMChannelAsync_withChannelID_delegate_(
            None, channel, delegate
        )
    except Exception as exc:
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=None, phase=f"open_call_exception:{exc!r}",
        )
    if status != 0:
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=int(status), phase="open_call",
        )

    # Wait for open-complete delegate callback.
    deadline = time.monotonic() + open_timeout
    while time.monotonic() < deadline:
        _pump_runloop(0.1)
        if delegate.open_status is not None:
            break
    if not delegate.was_opened:
        try:
            if ch is not None:
                ch.closeChannel()
        except Exception:
            pass
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=delegate.open_status, phase="timeout",
        )

    # Stream the packets. writeSync: returns IOReturn (0 = success).
    write_errors = 0

    def _write(p: bytes) -> None:
        nonlocal write_errors
        try:
            status = ch.writeSync_length_(p, len(p))
        except Exception:
            log.exception("writeSync_length_ raised")
            write_errors += 1
            return
        if status != 0:
            write_errors += 1
            log.warning("writeSync returned IOReturn=%d", status)

    try:
        for pkt in packets:
            _write(pkt)
            _pump_runloop(pace_interval_s)

        # After the last audio packet, give the radio time to drain its
        # internal audio buffer before sending the end-of-TX signal. If we
        # send the end frame while there are still queued audio bytes on
        # the radio's side, the radio finishes playing the audio first and
        # then interprets the end frame — but with our pacing matching the
        # audio rate, the drain is minimal. Bigger issue is the close-race
        # (below).
        if tail_packet is not None:
            # Send the end-of-TX frame, then wait, then repeat a few times
            # for robustness. Each write is cheap; the radio will simply
            # re-receive the same end frame. Repeating guards against the
            # first one being dropped if the channel is already tearing
            # down Bluetooth-side.
            for i in range(max(1, tail_repeats)):
                _write(tail_packet)
                _pump_runloop(tail_interval_s)

        # Hold the channel open long enough that any in-flight writes
        # (especially the tail frame) reach the radio before closeChannel
        # tears everything down. Without this, closing right after the
        # last write seems to drop the end frame and the radio stays
        # wedged in TX.
        _pump_runloop(post_drain_s)
    finally:
        try:
            if ch is not None and not delegate.closed:
                ch.closeChannel()
        except Exception:
            log.exception("closeChannel raised")
        for _ in range(20):
            if delegate.closed:
                break
            _pump_runloop(0.05)

    phase = "closed" if delegate.closed else "open_complete"
    if write_errors:
        phase += f" ({write_errors} write errors)"
    return OpenAttemptResult(
        address=address, channel=channel, opened=delegate.was_opened,
        open_status=delegate.open_status, phase=phase,
    )


def try_open_rfcomm(
    address: str, channel: int, *, open_timeout: float = 5.0
) -> OpenAttemptResult:
    """Open an RFCOMM channel, then immediately close it.

    Purpose: prove we can negotiate an RFCOMM link end-to-end. We don't
    install a data callback or write anything. We just wait for the
    ``rfcommChannelOpenComplete:status:`` delegate callback.
    """
    _require_iobluetooth()
    import objc  # type: ignore
    from Foundation import NSObject  # type: ignore

    class _Delegate(NSObject):  # PyObjC class, lives inside this function to
                                # keep it ad-hoc and avoid class registration
                                # at module import on non-Darwin hosts.
        def init(self):  # type: ignore[override]
            self = objc.super(_Delegate, self).init()
            if self is None:
                return None
            self.open_status = None  # type: ignore[attr-defined]
            # was_opened is latched True on first successful open and never
            # cleared; is_open reflects the live state and goes back to False
            # once the channel is closed. Use was_opened for "did it ever work".
            self.was_opened = False  # type: ignore[attr-defined]
            self.is_open = False     # type: ignore[attr-defined]
            self.closed = False      # type: ignore[attr-defined]
            return self

        def rfcommChannelOpenComplete_status_(self, ch, status):  # noqa: N802
            self.open_status = int(status)
            if status == 0:
                self.was_opened = True
                self.is_open = True

        def rfcommChannelClosed_(self, ch):  # noqa: N802
            self.is_open = False
            self.closed = True

        def rfcommChannelData_data_length_(self, ch, data, length):  # noqa: N802
            # Ignore any incoming data for 3a.
            pass

    dev = _device_by_address(address)
    delegate = _Delegate.alloc().init()

    # openRFCOMMChannelAsync:withChannelID:delegate: — PyObjC returns
    # (status, channel_out) when you pass None for the out-pointer.
    try:
        status, ch = dev.openRFCOMMChannelAsync_withChannelID_delegate_(
            None, channel, delegate
        )
    except Exception as exc:
        log.exception("openRFCOMMChannelAsync_withChannelID_delegate_ raised")
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=None, phase=f"open_call_exception:{exc!r}",
        )

    if status != 0:
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=int(status), phase="open_call",
        )

    deadline = time.monotonic() + open_timeout
    while time.monotonic() < deadline:
        _pump_runloop(0.1)
        if delegate.open_status is not None:
            break

    if delegate.open_status is None:
        # Timed out without any callback. Try to close the half-baked channel.
        try:
            if ch is not None:
                ch.closeChannel()
        except Exception:
            pass
        return OpenAttemptResult(
            address=address, channel=channel, opened=False,
            open_status=None, phase="timeout",
        )

    phase = "open_complete"

    # If it opened, close it immediately. This is a probe, nothing more.
    if delegate.is_open and ch is not None:
        try:
            ch.closeChannel()
        except Exception:
            log.exception("closeChannel raised")
        for _ in range(20):  # up to ~1s for the close callback to fire
            _pump_runloop(0.05)
            if delegate.closed:
                phase = "closed"
                break

    return OpenAttemptResult(
        address=address, channel=channel, opened=delegate.was_opened,
        open_status=delegate.open_status, phase=phase,
    )
