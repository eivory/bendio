"""JSON-RPC server over stdin/stdout for hosting integrations.

Designed for subprocess-based consumers (e.g., HTCommander-X's Flutter
app on macOS) that want bendio to handle BLE transport but keep the
GAIA protocol parsing in their own code. The server exposes bendio at
the byte level: the client writes raw BLE bytes, the server pushes
raw BLE indications back.

Protocol
--------

Line-delimited JSON. Each message is exactly one line (no embedded
newlines), ``\\n`` terminated. Follows JSON-RPC 2.0:

* **Request** (client → server)::

    {"jsonrpc":"2.0","id":N,"method":"NAME","params":{...}}

* **Success response** (server → client)::

    {"jsonrpc":"2.0","id":N,"result":{...}}

* **Error response** (server → client)::

    {"jsonrpc":"2.0","id":N,"error":{"code":N,"message":"..."}}

* **Notification** (server → client, no ``id``; server-initiated event)::

    {"jsonrpc":"2.0","method":"NAME","params":{...}}

Methods (phase A — BLE control only)
------------------------------------

* ``scan(timeout=5.0, only_benshi=False)`` → list of
  ``{"address": str, "name": str, "rssi": int|null}``.

* ``connect(address)`` → ``{"connected": true}``. Closes any prior
  connection. ``address`` is a BT identifier understood by bleak —
  on macOS that's the CoreBluetooth per-device UUID; on Linux/Windows
  it's a MAC address.

* ``ble_write(bytes)`` → ``{}``. ``bytes`` is a hex-encoded string.
  Writes one complete GAIA frame to the radio's write characteristic
  (whatever the caller put in it — the server does no protocol
  parsing).

* ``disconnect()`` → ``{}``.

* ``shutdown()`` → ``{}``, then the server exits cleanly. Also exits
  on stdin EOF.

Events (phase A)
----------------

* ``ble_indication`` — one per BLE indication received on the radio's
  indicate characteristic. Params ``{"bytes": hex-string}``.

Error codes
-----------

Standard JSON-RPC 2.0 codes for parse / protocol errors; application
errors use ``-32000``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any, Optional

from .link import BleLink, scan

# JSON-RPC 2.0 error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_APPLICATION = -32000


class Server:
    """Stateful JSON-RPC server. One instance per subprocess lifetime."""

    def __init__(self, stdin=None, stdout=None):
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._link: Optional[BleLink] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._write_lock = asyncio.Lock()
        self._shutdown_requested = False

    async def run(self) -> int:
        """Read requests from stdin, process, write responses to stdout.
        Exits on EOF or after a ``shutdown`` method call is acknowledged."""
        self._loop = asyncio.get_running_loop()

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await self._loop.connect_read_pipe(lambda: protocol, self._stdin)

        while not self._shutdown_requested:
            line = await reader.readline()
            if not line:
                break  # EOF
            line = line.decode("utf-8").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                await self._send_error(None, ERR_PARSE, f"parse error: {exc}")
                continue
            if not isinstance(req, dict):
                await self._send_error(None, ERR_INVALID_REQUEST,
                                       "request must be a JSON object")
                continue
            await self._handle(req)

        # Clean up on exit
        if self._link is not None:
            try:
                await self._link.disconnect()
            except Exception:
                pass
            self._link = None
        return 0

    async def _handle(self, req: dict) -> None:
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if not isinstance(method, str):
            await self._send_error(rid, ERR_INVALID_REQUEST,
                                   "method must be a string")
            return
        if not isinstance(params, dict):
            await self._send_error(rid, ERR_INVALID_PARAMS,
                                   "params must be an object")
            return

        handler = _DISPATCH.get(method)
        if handler is None:
            await self._send_error(rid, ERR_METHOD_NOT_FOUND,
                                   f"unknown method: {method}")
            return

        try:
            result = await handler(self, params)
            if rid is not None:  # suppress response for pure notifications
                await self._send_result(rid, result)
        except _AppError as exc:
            await self._send_error(rid, ERR_APPLICATION, str(exc))
        except Exception as exc:
            detail = traceback.format_exc(limit=4)
            await self._send_error(
                rid, ERR_INTERNAL,
                f"{type(exc).__name__}: {exc}\n{detail}",
            )

    # --------------------------------------------------------------- writers

    async def _write_line(self, obj: dict) -> None:
        """Serialize and write one JSON line. Thread-safe-ish via asyncio lock."""
        async with self._write_lock:
            self._stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self._stdout.flush()

    async def _send_result(self, rid: Any, result: Any) -> None:
        await self._write_line({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _send_error(self, rid: Any, code: int, message: str) -> None:
        await self._write_line({
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message},
        })

    async def _send_notification(self, method: str, params: dict) -> None:
        await self._write_line({
            "jsonrpc": "2.0", "method": method, "params": params,
        })

    # ----------------------------------------------------------- BLE callback

    def _on_ble_frame(self, frame: bytes) -> None:
        """bleak delivers indications on the asyncio loop thread, so it's
        safe to schedule the notification write as a task."""
        if self._loop is None:
            return
        self._loop.create_task(
            self._send_notification("ble_indication", {"bytes": frame.hex()})
        )


class _AppError(RuntimeError):
    """Raised by method handlers for user-visible application errors.
    Translated to JSON-RPC error with code ERR_APPLICATION."""


# --------------------------------------------------------- method handlers


async def _m_scan(server: Server, params: dict) -> list[dict]:
    timeout = float(params.get("timeout", 5.0))
    only_benshi = bool(params.get("only_benshi", False))
    results = await scan(timeout=timeout, only_benshi=only_benshi)
    return [
        {
            "address": str(dev.address),
            "name": str(dev.name or ""),
            "rssi": int(rssi) if rssi is not None else None,
        }
        for dev, _uuids, rssi in results
    ]


async def _m_connect(server: Server, params: dict) -> dict:
    address = params.get("address")
    if not isinstance(address, str) or not address:
        raise _AppError("params.address (string) is required")
    # Clean up any prior link first
    if server._link is not None:
        try:
            await server._link.disconnect()
        except Exception:
            pass
        server._link = None
    link = BleLink(address)
    try:
        await link.connect(server._on_ble_frame)
    except Exception as exc:
        raise _AppError(f"connect failed: {exc}") from exc
    server._link = link
    return {"connected": True}


async def _m_disconnect(server: Server, params: dict) -> dict:
    if server._link is not None:
        try:
            await server._link.disconnect()
        finally:
            server._link = None
    return {}


async def _m_ble_write(server: Server, params: dict) -> dict:
    hex_str = params.get("bytes")
    if not isinstance(hex_str, str):
        raise _AppError("params.bytes (hex string) is required")
    try:
        payload = bytes.fromhex(hex_str)
    except ValueError as exc:
        raise _AppError(f"invalid hex: {exc}") from exc
    if server._link is None or not server._link.is_connected():
        raise _AppError("not connected")
    await server._link.write_frame(payload)
    return {}


async def _m_shutdown(server: Server, params: dict) -> dict:
    server._shutdown_requested = True
    return {}


_DISPATCH = {
    "scan": _m_scan,
    "connect": _m_connect,
    "disconnect": _m_disconnect,
    "ble_write": _m_ble_write,
    "shutdown": _m_shutdown,
}


# --------------------------------------------------------------- entry point

def main() -> int:
    try:
        return asyncio.run(Server().run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
