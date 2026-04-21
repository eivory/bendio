# Phase 2 — BLE traffic during FM RX

## Test setup

- BTECH UV-PRO, firmware 0x92 (146), hw 0x01, paired over BLE to macOS 26.4
- Tool: `bendio sniff` on our scratch BLE stack (this repo)
- Registered notification classes: `HT_STATUS_CHANGED`, `RADIO_STATUS_CHANGED`,
  `USER_ACTION`, `SYSTEM_EVENT`, `POSITION_CHANGE`
- Sequence run during capture:
  1. Idle, baseline
  2. Second radio keys on the same frequency for ~7 s
  3. Idle
  4. Channel knob rotated one detent
  5. PTT pressed briefly on the UV-PRO, then released
  6. Idle

## Raw trace

```
[   4.743s]   9B BASIC.EVENT_NOTIFICATION          000200090184210000
[   4.805s]   9B BASIC.EVENT_NOTIFICATION          00020009084000254e
[   4.923s]   5B BASIC.REGISTER_NOTIFICATION[reply] 0002800605     (INVALID_PARAMETER)
[   4.982s]   5B BASIC.REGISTER_NOTIFICATION[reply] 0002800605     (INVALID_PARAMETER)
[  12.708s]   9B BASIC.EVENT_NOTIFICATION          0002000901b4c1f03c    ← RX started
[  19.459s]   9B BASIC.EVENT_NOTIFICATION          000200090184c1003c    ← RX ended
[  30.830s]  27B <unparsed>                        00020009062d112606221e00a06101940000000078000000000000
                                                   (event_type=06 HT_SETTINGS_CHANGED)
[  36.588s]   9B BASIC.EVENT_NOTIFICATION          0002000901c4210000    ← TX started
[  42.379s]   9B BASIC.EVENT_NOTIFICATION          000200090184210000    ← TX ended
```

## Decoding

Every event frame starts with `00 02 00 09 <event_type> <event_body>`:

- `00 02` = BASIC group
- `00 09` = EVENT_NOTIFICATION (cmd 9, is_reply=0)
- next byte = `EventType` (1=HT_STATUS_CHANGED, 6=HT_SETTINGS_CHANGED, 8=RADIO_STATUS_CHANGED)

`HT_STATUS_CHANGED` payload is a 4-byte `Status` bitfield. The first byte is
the interesting one:

| bit | meaning           |
|-----|-------------------|
|  7  | is_power_on       |
|  6  | is_in_tx          |
|  5  | is_sq (squelch)   |
|  4  | is_in_rx          |
| 3–2 | double_channel    |
|  1  | is_scan           |
|  0  | is_radio          |

Cross-referencing each observed status byte:

| Hex | Binary   | Decoded flags                              | Event         |
|-----|----------|--------------------------------------------|---------------|
|`84` | 10000100 | power_on, double_channel bit 2, radio=off  | idle (baseline)|
|`b4` | 10110100 | power_on, **is_sq=1, is_in_rx=1**, radio   | RX active     |
|`c4` | 11000100 | power_on, **is_in_tx=1**, radio            | TX active     |

## Finding

**Audio is not on BLE — at least not on the Benshi service.**

During 6.75 s of active reception (12.71 → 19.46), the radio sent **exactly
one** BLE indication on `…1102-d102-11e1-9b23-00025b00a5a5` — the state-change
edge. No continuous byte stream, no PCM, no SBC frames. This is the
empirical corroboration of the original spec doc §8.

## Mystery service — investigated

GATT discovery at connect time revealed a **second, undocumented** proprietary
service on the UV-PRO that neither `benshi_ble_confirmed.md` nor benlink nor
HTCommander mentions:

```
00000001-BA2A-46C9-AE49-01B0961F68BB  service
├── 00000003-BA2A-46C9-AE49-01B0961F68BB  [notify, read]
└── 00000002-BA2A-46C9-AE49-01B0961F68BB  [write, write-without-response]
```

The `write-without-response` property on `…0002` is a streaming-grade write
pattern (atypical for passive config/status). That made it a plausible
candidate for a hidden audio path.

We ran a follow-up capture with `bendio sniff-all`, which subscribes to every
notify characteristic on every service (including `…0003` above) and registers
for `HT_STATUS_CHANGED` so the RX edges are in the same log:

```
[  18.255s] svc=00001100 ch=00001102   9B 000200090184c1003c     idle baseline
[  27.685s] svc=00001100 ch=00001102   9B 0002000901b4c1603c     RX STARTED
[  27.864s] svc=00001100 ch=00001102   9B 0002000901b4c1f03c     RX state refresh (RSSI)
[  38.607s] svc=00001100 ch=00001102   9B 000200090184c1003c     RX ENDED
```

**During 10.9 s of active RX, `svc=00000001 ch=00000003` emitted zero bytes.**
Whatever that service is for — firmware update, companion-app handshake, or
something else — it is not an active audio path on this firmware.

## Conclusion

**Audio is not on BLE on the BTECH UV-PRO.** Phase 3 must use the separate
Bluetooth Classic RFCOMM SPP channel (`IOBluetooth` / PyObjC on macOS).

## REGISTER_NOTIFICATION reply

Unrelated to audio, but captured here for the record: the UV-PRO firmware
does respond to `REGISTER_NOTIFICATION` with a 1-byte status (contrary to
benlink's assumption that there is no reply). `05` = `INVALID_PARAMETER`
was observed for `USER_ACTION` and `SYSTEM_EVENT` — these event classes
are not subscribable on this firmware. `HT_STATUS_CHANGED`,
`RADIO_STATUS_CHANGED`, and `POSITION_CHANGE` produced no visible reply
(presumably success is silent) and produced push events as expected.
