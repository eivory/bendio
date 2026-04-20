"""High-level async API for a Benshi-family radio over BLE.

The transport is the GATT link in :mod:`benshi.link`. Every BLE write carries
one GAIA frame (``FF 01 flags n_pay <4-byte cmd header> <payload>``); every
indication is the same. We wrap outgoing :class:`~benshi.protocol.Message`
objects into :class:`~benshi.protocol.GaiaFrame` and unwrap incoming frames
the same way before parsing as a Message.

Command/reply pairing uses the ``is_reply`` bit convention: the radio replies
with the same ``(command_group, command)`` but ``is_reply=True``. Pending
commands wait on an asyncio Future keyed by ``(group, command)``.
"""
from __future__ import annotations

import asyncio
import logging
import typing as t

from . import protocol as p
from .link import BleLink

log = logging.getLogger(__name__)


NotificationHandler = t.Callable[[p.EventNotificationBody], None]


class RadioError(RuntimeError):
    pass


class Radio:
    """Async client for a Benshi-family radio over BLE."""

    def __init__(self, address: str):
        self._link = BleLink(address)
        self._pending: dict[tuple[int, int], asyncio.Future[p.Message]] = {}
        self._notif_handlers: list[NotificationHandler] = []
        self._raw_frame_handler: t.Callable[[bytes], None] | None = None

    @property
    def address(self) -> str:
        return self._link.address

    def is_connected(self) -> bool:
        return self._link.is_connected()

    async def connect(self) -> None:
        await self._link.connect(self._on_frame)

    async def disconnect(self) -> None:
        await self._link.disconnect()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RadioError("disconnected"))
        self._pending.clear()

    async def __aenter__(self) -> "Radio":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------ raw

    def set_raw_frame_handler(
        self, handler: t.Callable[[bytes], None] | None
    ) -> None:
        """Install a callback that receives every inbound GAIA frame verbatim.

        Useful for sniff/trace tools. Does not interfere with normal
        command/reply or notification dispatch.
        """
        self._raw_frame_handler = handler

    # ----------------------------------------------------------- commands

    async def send(
        self,
        msg: p.Message,
        *,
        timeout: float = 5.0,
        expect_reply: bool = True,
    ) -> p.Message | None:
        """Send a Message. If ``expect_reply``, wait for the matching reply."""
        key = (int(msg.command_group), int(msg.command))
        fut: asyncio.Future[p.Message] | None = None
        if expect_reply:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            if key in self._pending:
                raise RadioError(
                    f"command {key} already in flight; serialize your sends"
                )
            self._pending[key] = fut

        try:
            frame = msg.to_bytes()
            log.debug("tx msg: %s", frame.hex())
            await self._link.write_frame(frame)
            if fut is None:
                return None
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            if expect_reply:
                self._pending.pop(key, None)

    def _on_frame(self, frame: bytes) -> None:
        log.debug("rx frame: %s", frame.hex())
        if self._raw_frame_handler is not None:
            try:
                self._raw_frame_handler(frame)
            except Exception:
                log.exception("raw frame handler raised")

        # On BLE, benlink sends/receives raw Message bytes (no GAIA FF-01
        # wrapper). The benshi_ble_confirmed doc claims BLE uses GAIA frames,
        # but empirically benlink's approach is what talks to real radios, so
        # we try raw-Message parsing first and fall back to GAIA-wrapped.
        msg: p.Message | None = None
        try:
            msg = p.Message.from_bytes(frame)
        except Exception:
            try:
                gf = p.GaiaFrame.from_bytes(frame)
                msg = p.Message.from_bytes(gf.data)
            except Exception:
                log.debug("unrecognized frame: %s", frame.hex())
                return

        key = (int(msg.command_group), int(msg.command))

        if msg.is_reply:
            fut = self._pending.get(key)
            if fut is not None and not fut.done():
                fut.set_result(msg)
                return
            log.debug("unmatched reply %s", key)
            return

        # Inbound event/notification from radio.
        if (
            msg.command_group == p.CommandGroup.BASIC
            and msg.command == p.BasicCommand.EVENT_NOTIFICATION
        ):
            body = msg.body
            if isinstance(body, p.EventNotificationBody):
                for h in list(self._notif_handlers):
                    try:
                        h(body)
                    except Exception:
                        log.exception("notification handler raised")

    # --------------------------------------------------- convenience API

    async def device_info(self) -> p.GetDevInfoReplyBody:
        reply = await self.send(
            p.Message(
                command_group=p.CommandGroup.BASIC,
                is_reply=False,
                command=p.BasicCommand.GET_DEV_INFO,
                body=p.GetDevInfoBody(),
            )
        )
        assert reply is not None and isinstance(reply.body, p.GetDevInfoReplyBody)
        return reply.body

    async def read_settings(self) -> p.ReadSettingsReplyBody:
        reply = await self.send(
            p.Message(
                command_group=p.CommandGroup.BASIC,
                is_reply=False,
                command=p.BasicCommand.READ_SETTINGS,
                body=p.ReadSettingsBody(),
            )
        )
        assert reply is not None and isinstance(reply.body, p.ReadSettingsReplyBody)
        return reply.body

    async def read_rf_ch(self, index: int) -> p.ReadRFChReplyBody:
        reply = await self.send(
            p.Message(
                command_group=p.CommandGroup.BASIC,
                is_reply=False,
                command=p.BasicCommand.READ_RF_CH,
                body=p.ReadRFChBody(channel_id=index),
            )
        )
        assert reply is not None and isinstance(reply.body, p.ReadRFChReplyBody)
        return reply.body

    async def ht_status(self) -> p.GetHtStatusReplyBody:
        reply = await self.send(
            p.Message(
                command_group=p.CommandGroup.BASIC,
                is_reply=False,
                command=p.BasicCommand.GET_HT_STATUS,
                body=p.GetHtStatusBody(),
            )
        )
        assert reply is not None and isinstance(reply.body, p.GetHtStatusReplyBody)
        return reply.body

    async def register_notification(self, event_type: p.EventType) -> None:
        """Subscribe to a notification class. Radio will push EVENT_NOTIFICATION
        frames whenever that class of event fires.

        REGISTER_NOTIFICATION has no reply in the protocol catalog.
        """
        await self.send(
            p.Message(
                command_group=p.CommandGroup.BASIC,
                is_reply=False,
                command=p.BasicCommand.REGISTER_NOTIFICATION,
                body=p.RegisterNotificationBody(event_type=event_type),
            ),
            expect_reply=False,
        )

    def on_notification(
        self, handler: NotificationHandler
    ) -> t.Callable[[], None]:
        """Install an event notification handler. Returns a detach function."""
        self._notif_handlers.append(handler)

        def _detach() -> None:
            try:
                self._notif_handlers.remove(handler)
            except ValueError:
                pass

        return _detach
