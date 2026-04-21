"""GAIA message encode/decode smoke tests.

The protocol module is vendored from benlink; these tests mostly
confirm our wrappers + modifications behave correctly.
"""
from __future__ import annotations

from bendio import protocol as p


def test_get_dev_info_command_round_trip():
    msg = p.Message(
        command_group=p.CommandGroup.BASIC,
        is_reply=False,
        command=p.BasicCommand.GET_DEV_INFO,
        body=p.GetDevInfoBody(),
    )
    raw = msg.to_bytes()
    # On BLE the wire format is raw bytes: [group:16] [is_reply:1 cmd:15]
    # [body]. For GET_DEV_INFO that's 00 02 | 00 04 | 03.
    assert raw == bytes.fromhex("00 02 00 04 03".replace(" ", ""))

    parsed = p.Message.from_bytes(raw)
    assert parsed.command_group == p.CommandGroup.BASIC
    assert parsed.command == p.BasicCommand.GET_DEV_INFO
    assert parsed.is_reply is False


def test_read_rf_ch_with_channel_index():
    msg = p.Message(
        command_group=p.CommandGroup.BASIC,
        is_reply=False,
        command=p.BasicCommand.READ_RF_CH,
        body=p.ReadRFChBody(channel_id=29),
    )
    raw = msg.to_bytes()
    # 00 02 | 00 0D | 1D  (BASIC, READ_RF_CH=13, channel_id=29=0x1D)
    assert raw == bytes.fromhex("00 02 00 0D 1D".replace(" ", ""))


def test_reply_with_high_bit_set():
    # A reply frame has the high bit of command set to 1. Construct the
    # wire bytes for a READ_RF_CH reply with status=INVALID_PARAMETER.
    wire = bytes.fromhex("00 02 80 0D 05".replace(" ", ""))
    parsed = p.Message.from_bytes(wire)
    assert parsed.is_reply is True
    assert parsed.command == p.BasicCommand.READ_RF_CH
    body = parsed.body
    assert body.reply_status == p.ReplyStatus.INVALID_PARAMETER
    assert body.rf_ch is None  # no channel data for failure status


def test_register_notification_reply_survives_our_patch():
    """Our local patch to message.py's body_disc() accepts
    REGISTER_NOTIFICATION replies (benlink upstream raises). This must
    keep working or the sniff-all command will crash on every subscribe."""
    wire = bytes.fromhex("00 02 80 06 05".replace(" ", ""))
    parsed = p.Message.from_bytes(wire)
    assert parsed.is_reply is True
    assert parsed.command == p.BasicCommand.REGISTER_NOTIFICATION
    # body parsed as raw bytes rather than raising
    assert isinstance(parsed.body, (bytes, bytearray))
    assert parsed.body == b"\x05"


def test_event_notification_ht_status_changed_parses():
    """Check a captured HT_STATUS_CHANGED event frame deframes to the
    right structure. Bytes from Phase 2 of the project journal."""
    # event_type=1 (HT_STATUS_CHANGED), status bitfield follows.
    # 00 02 | 00 09 | 01 | 84 21 00 00
    wire = bytes.fromhex("00 02 00 09 01 84 21 00 00".replace(" ", ""))
    parsed = p.Message.from_bytes(wire)
    assert parsed.command == p.BasicCommand.EVENT_NOTIFICATION
    body = parsed.body
    assert isinstance(body, p.EventNotificationBody)
    assert body.event_type == p.EventType.HT_STATUS_CHANGED
    event = body.event
    status = event.status
    # Baseline idle status: power_on, radio flags on, not in_tx/rx/sq.
    assert status.is_power_on is True
    assert status.is_in_tx is False
    assert status.is_sq is False
    assert status.is_in_rx is False
