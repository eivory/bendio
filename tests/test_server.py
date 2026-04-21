"""Offline tests for bendio.server — no Bluetooth hardware required.

We monkeypatch BleLink and ``scan`` so the server's method handlers
exercise real code paths (JSON parsing, dispatch, response formatting,
error translation) without touching the BT stack.
"""
from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import patch

import pytest

from bendio import server as srv

# --------------------------------------------------------------- fake bleak


class _FakeBleLink:
    """Minimal stand-in for bendio.link.BleLink used by the server.

    Captures written frames for assertions and invokes the on_frame
    callback when ``simulate_indication()`` is called.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        self._connected = False
        self._on_frame = None
        self.written: list[bytes] = []

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, on_frame) -> None:
        self._on_frame = on_frame
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def write_frame(self, frame: bytes) -> None:
        if not self._connected:
            raise RuntimeError("not connected")
        self.written.append(frame)

    def simulate_indication(self, frame: bytes) -> None:
        if self._on_frame:
            self._on_frame(frame)


# ----------------------------------------------------------- server driver


class _ServerDriver:
    """Run a Server instance with programmatic stdin/stdout and assert on
    the JSON messages that come back."""

    def __init__(self):
        # Feed lines by writing to _stdin_writer; server reads them.
        self._stdin_bytes = bytearray()
        self._stdout = io.StringIO()
        self._server: srv.Server | None = None
        self._request_id = 0

    def _make_stdin(self):
        # The server's asyncio StreamReader is fed by connect_read_pipe
        # on sys.stdin, which only works with a real file descriptor. In
        # tests we just feed lines directly into the server's handler.
        raise NotImplementedError

    async def call(self, method: str, **params) -> dict:
        """Send one request through the dispatcher and return the response."""
        if self._server is None:
            self._server = srv.Server(stdout=self._stdout)
            self._server._loop = asyncio.get_running_loop()
        self._request_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        before = self._stdout.tell()
        await self._server._handle(req)
        after = self._stdout.tell()
        self._stdout.seek(before)
        raw = self._stdout.read()
        self._stdout.seek(after)
        lines = [line for line in raw.strip().split("\n") if line]
        assert len(lines) == 1, f"expected one response, got {len(lines)}: {lines}"
        return json.loads(lines[0])

    def pop_events(self) -> list[dict]:
        """Return and clear any server-initiated notifications written since
        the last call / pop. Parses all JSON lines, filters to notifications."""
        pos = self._stdout.tell()
        self._stdout.seek(0)
        raw = self._stdout.read()
        self._stdout.seek(pos)
        events = []
        for line in raw.strip().split("\n"):
            if not line:
                continue
            msg = json.loads(line)
            if "method" in msg and "id" not in msg:
                events.append(msg)
        # Rewind stdout so subsequent reads see a clean buffer
        self._stdout.seek(0)
        self._stdout.truncate()
        return events


# --------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found():
    driver = _ServerDriver()
    resp = await driver.call("no_such_method")
    assert resp["id"] == 1
    assert "error" in resp
    assert resp["error"]["code"] == srv.ERR_METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_ble_write_requires_connect_first():
    driver = _ServerDriver()
    resp = await driver.call("ble_write", bytes="deadbeef")
    assert "error" in resp
    assert resp["error"]["code"] == srv.ERR_APPLICATION
    assert "not connected" in resp["error"]["message"].lower()


@pytest.mark.asyncio
async def test_connect_then_write_and_indication_round_trip():
    driver = _ServerDriver()
    with patch.object(srv, "BleLink", _FakeBleLink):
        resp = await driver.call("connect", address="FAKE-UUID-1234")
        assert resp["result"] == {"connected": True}

        # Write something — the fake link captures it.
        resp = await driver.call("ble_write", bytes="0002000403")
        assert resp["result"] == {}
        assert driver._server._link.written == [bytes.fromhex("0002000403")]

        # Simulate a BLE indication flowing back.
        driver._server._link.simulate_indication(bytes.fromhex("0002800400"))
        # Give the event-loop task queued by _on_ble_frame a chance to run.
        await asyncio.sleep(0.01)
        events = driver.pop_events()
        assert len(events) == 1, f"expected 1 notification, got {events}"
        assert events[0]["method"] == "ble_indication"
        assert events[0]["params"]["bytes"] == "0002800400"


@pytest.mark.asyncio
async def test_disconnect_closes_link_and_is_idempotent():
    driver = _ServerDriver()
    with patch.object(srv, "BleLink", _FakeBleLink):
        await driver.call("connect", address="FAKE")
        assert driver._server._link is not None

        resp = await driver.call("disconnect")
        assert resp["result"] == {}
        assert driver._server._link is None

        # Double-disconnect is safe.
        resp = await driver.call("disconnect")
        assert resp["result"] == {}


@pytest.mark.asyncio
async def test_connect_closes_prior_link_first():
    driver = _ServerDriver()
    with patch.object(srv, "BleLink", _FakeBleLink):
        await driver.call("connect", address="FIRST")
        first_link = driver._server._link
        await driver.call("connect", address="SECOND")
        assert driver._server._link is not first_link
        assert driver._server._link.address == "SECOND"
        # The first link should have been asked to disconnect.
        assert first_link.is_connected() is False


@pytest.mark.asyncio
async def test_connect_missing_address_returns_app_error():
    driver = _ServerDriver()
    resp = await driver.call("connect")
    assert "error" in resp
    assert resp["error"]["code"] == srv.ERR_APPLICATION


@pytest.mark.asyncio
async def test_ble_write_invalid_hex_returns_app_error():
    driver = _ServerDriver()
    with patch.object(srv, "BleLink", _FakeBleLink):
        await driver.call("connect", address="FAKE")
        resp = await driver.call("ble_write", bytes="not-hex!")
        assert "error" in resp
        assert resp["error"]["code"] == srv.ERR_APPLICATION


@pytest.mark.asyncio
async def test_scan_returns_list_shape():
    """Patch the bendio.server.scan function to avoid real BLE. Assert
    the server transforms bleak's tuple-of-tuples output into the
    documented dict-list shape."""
    class _D:
        address = "FAKE-ADDR"
        name = "UV-PRO"

    async def _fake_scan(timeout=5.0, only_benshi=False):
        return [(_D(), ["00001100-d102-11e1-9b23-00025b00a5a5"], -42)]

    driver = _ServerDriver()
    with patch.object(srv, "scan", _fake_scan):
        resp = await driver.call("scan", timeout=1.0)
    assert resp["result"] == [
        {"address": "FAKE-ADDR", "name": "UV-PRO", "rssi": -42}
    ]


@pytest.mark.asyncio
async def test_shutdown_flips_flag_and_returns_ok():
    driver = _ServerDriver()
    resp = await driver.call("shutdown")
    assert resp["result"] == {}
    assert driver._server._shutdown_requested is True
