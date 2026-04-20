# Third-Party Notices

## benlink

The `benshi/protocol/` subtree is vendored from [benlink](https://github.com/khusmann/benlink)
by Kenneth Husmann, used under the Apache License 2.0. A copy of that license is in
[`LICENSE.benlink`](LICENSE.benlink).

Local modifications to vendored files:

- `benshi/protocol/command/message.py` — in `body_disc()`, the
  `REGISTER_NOTIFICATION` branch no longer raises `ValueError` when
  `is_reply=True`. Empirically the BTECH UV-PRO firmware responds to
  `REGISTER_NOTIFICATION` with a status byte (e.g. `0x05 = INVALID_PARAMETER`
  when the requested event class isn't supported). Accept it as raw bytes
  instead of crashing the parser.

## Empirical protocol corrections

The parent repo's `benshi_ble_confirmed.md` §3 states that every BLE write
and indication is "one GAIA frame" starting with `FF 01 flags n_pay ...`.
That is **not** how the radio actually behaves on BLE. On the BLE control
characteristics, the radio accepts and emits raw `Message` bytes with no
`FF 01` wrapper — the GAIA framing is an RFCOMM-transport concern only.
benlink's `BleCommandLink` had this right all along; we follow it.

## HTCommander-X

The parent repository provides [`benshi_ble_confirmed.md`](../benshi_ble_confirmed.md)
as the authoritative empirical protocol reference, and reference implementations under
[`../htcommander_flutter/lib/radio/`](../htcommander_flutter/lib/radio/) used for
cross-validation of GAIA framing and SBC audio handling.
