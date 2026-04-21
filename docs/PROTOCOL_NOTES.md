# Protocol notes

Empirical findings from developing this library against a BTECH UV-PRO,
captured here because they disagree with published references.

## BLE framing is not GAIA-framed

The upstream protocol reference (`benshi_ble_confirmed.md`, §3) states
that every BLE write and every indication carries one full GAIA frame
beginning with `FF 01 <flags> <n_pay> ...`. That is **not** how the
UV-PRO actually behaves on BLE. On the BLE control characteristics the
radio accepts and emits raw `Message` bytes — the 4-byte command header
plus optional body — with no `FF 01` wrapper. GAIA framing is an
RFCOMM-transport concern.

`benlink`'s `BleCommandLink` got this right from the start; we follow it.
Tested end-to-end against UV-PRO firmware 0x92 (146). If a different
Benshi-family radio ever turns out to require the `FF 01` wrapper on
BLE, `benshi.radio.Radio._wrap` would need to branch on device.

## REGISTER_NOTIFICATION does get a reply

`benlink/src/benlink/protocol/command/message.py`'s `body_disc()`
asserts that `REGISTER_NOTIFICATION` (BASIC cmd 6) cannot be a reply,
and raises `ValueError` if one arrives.

Empirically the UV-PRO firmware **does** reply to
`REGISTER_NOTIFICATION` with a 1-byte status (for example `0x05 =
INVALID_PARAMETER` when the requested event class is not supported on
this firmware). `USER_ACTION` and `SYSTEM_EVENT` are two event classes
that produce this error response on UV-PRO; `HT_STATUS_CHANGED`,
`HT_CH_CHANGED`, `HT_SETTINGS_CHANGED`, `DATA_RXD`, and others are
accepted and produce no visible reply (silent success).

Our patch in `benshi/protocol/command/message.py` `body_disc()`
returns `bf_bytes(n // 8)` for the reply case instead of raising, so
the reply parses as raw bytes rather than crashing the decoder. Noted
inline in the file per Apache-2.0 §4(b).

## The radio exposes an undocumented BLE service

Neither `benshi_ble_confirmed.md`, benlink, nor HTCommander describes
the vendor service at:

```
00000001-BA2A-46C9-AE49-01B0961F68BB  service
├── 00000002-BA2A-46C9-AE49-01B0961F68BB  characteristic (write, write-without-response)
└── 00000003-BA2A-46C9-AE49-01B0961F68BB  characteristic (notify, read)
```

We subscribed to every notify characteristic during a full FM-RX burst
and saw zero bytes flow. So this service does not carry audio. What it
*does* carry is unknown — plausibly a firmware-update or companion-app
channel. Flagged here for future exploration.

## Classic RFCOMM service "BS AOC" is the audio channel

On the UV-PRO's SDP the services are named:

| # | Service name   | ServiceClass         | RFCOMM ch | Role                      |
|---|----------------|----------------------|-----------|---------------------------|
| 0 | Voice Gateway  | `0x1203` + HFP-AG    | 3         | HFP audio gateway         |
| 1 | SPP Dev        | `0x1101` (SPP)       | 4         | **GAIA control channel**  |
| 2 | **BS AOC**     | custom 128-bit UUID  | 2         | **Audio data channel**    |
| 5 | Voice Gateway  | `0x111f` (HFP HF)    | 3         | HFP hands-free            |

This library **only** opens channel 2 (BS AOC). Opening channel 4
("SPP Dev" / GAIA control) while a BLE control session is active
causes the radio's dual-mode state machine to seize up and refuse both
further RFCOMM connections and BLE indications until the radio's
Bluetooth is power-cycled. Avoided at all costs.

This finding came from `htcommander_flutter/lib/platform/linux/
linux_audio_transport.dart` which matches audio channels by
`record.contains(_genericAudioUuid) || record.contains('BS AOC')`.

## Audio codec parameters

Fixed, byte-for-byte repeating in every SBC frame:

- 32 kHz sampling rate
- 16 blocks
- mono channel mode
- loudness allocation method
- 8 subbands
- bitpool 18

Frame length: exactly 44 bytes. Packet structure on the audio
RFCOMM channel: `0x7E <cmd 0x00> <44-byte SBC frame, escaped> 0x7E`,
with a fixed 7 SBC frames per 309-byte transport packet during RX.
TX uses 1 SBC frame per transport packet.

The header byte pattern — `9c 71 12` — is what ffmpeg's SBC encoder
produces by default when fed 32 kHz mono PCM at ~88 kbps. Lucky
alignment: we didn't have to tune the encoder, and didn't have to
port the Dart SBC codec or link against `libsbc`.

## End-of-transmission packet

Fixed 11-byte sequence the radio sends to the host at the end of
every RX burst, and which the host must send to the radio at the
end of every TX:

```
7E 01 00 01 00 00 00 00 00 00 7E
```

Without this packet following a TX, the radio stays wedged in TX
mode. We send it 3 times with 50 ms between and then wait 1.5 s
before closing the channel, to make sure the radio has received and
acted on it before the BT stack tears down.
