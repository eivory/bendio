"""BLE transport for the Benshi radio control channel.

One BLE write = one complete GAIA frame (no framing, no checksum). Indications
from the indicate characteristic are delivered the same way. The radio requires
an OS-level Classic-Bluetooth bond before indications flow reliably; pair it in
System Settings first.
"""
from __future__ import annotations

import typing as t

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from . import protocol as p

RADIO_SERVICE_UUID = "00001100-d102-11e1-9b23-00025b00a5a5"
RADIO_WRITE_UUID = "00001101-d102-11e1-9b23-00025b00a5a5"
RADIO_INDICATE_UUID = "00001102-d102-11e1-9b23-00025b00a5a5"


FrameCallback = t.Callable[[bytes], None]
MessageCallback = t.Callable[[p.Message], None]


async def scan(
    timeout: float = 5.0,
    *,
    only_benshi: bool = False,
) -> list[tuple[BLEDevice, list[str], int | None]]:
    """Scan for BLE devices.

    Returns a list of ``(device, advertised_service_uuids, rssi)`` tuples.

    Benshi-family radios typically do **not** include the 128-bit Benshi
    service UUID in their advertising data — the UUID only becomes visible
    after you connect and enumerate GATT services. So by default we return
    every device we saw advertising, and you identify the radio by its
    name (e.g. "UV-PRO", "GA-5WB") or by the address you paired it with
    in System Settings. Pass ``only_benshi=True`` to filter strictly.
    """
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out: list[tuple[BLEDevice, list[str], int | None]] = []
    for dev, adv in devices.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if only_benshi and RADIO_SERVICE_UUID not in uuids:
            continue
        out.append((dev, uuids, adv.rssi))
    return out


class BleLink:
    """Low-level BLE link. Emits raw frame bytes and accepts raw frame bytes.

    Prefer :class:`bendio.Radio` for normal use; this class is for tests,
    sniffing, and custom integrations that want the raw GAIA frame stream.
    """

    def __init__(self, address: str):
        self._address = address
        self._client = BleakClient(address)
        self._on_frame: FrameCallback | None = None

    @property
    def address(self) -> str:
        return self._address

    def is_connected(self) -> bool:
        return self._client.is_connected

    async def connect(self, on_frame: FrameCallback) -> None:
        self._on_frame = on_frame
        await self._client.connect()

        def _notify(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            if self._on_frame is not None:
                self._on_frame(bytes(data))

        await self._client.start_notify(RADIO_INDICATE_UUID, _notify)

    async def disconnect(self) -> None:
        if self._client.is_connected:
            try:
                await self._client.stop_notify(RADIO_INDICATE_UUID)
            except Exception:
                pass
            await self._client.disconnect()

    async def write_frame(self, frame: bytes) -> None:
        """Write one complete GAIA frame to the radio (with response)."""
        await self._client.write_gatt_char(RADIO_WRITE_UUID, frame, response=True)

    async def __aenter__(self) -> "BleLink":
        # User must attach an on_frame callback before connecting; provided for
        # the case where the caller only wants to write (e.g. one-shot commands).
        await self._client.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()
