# benshi

A macOS Python library for two-way audio with BTech UV-Pro and other
Benshi-family handheld radios (GA-5WB, VR-N76, VR-N7500, GMRS-Pro).

**Status:** Phase 1 — BLE control link. Audio (Phase 3) is not implemented yet.

## Hardware / OS requirements

- macOS **12.4 or newer.** Earlier 12.x releases have a known `IOBluetooth`
  RFCOMM bug that breaks the audio path; not an issue for the BLE-only work
  in Phase 1 but we set the bar here to avoid surprises later.
- Radio must be **paired at the OS level first:**
  1. Power the radio on, enable Bluetooth, make it discoverable.
  2. On the Mac: System Settings → Bluetooth → connect.
  3. Confirm pairing on the radio (usually a button press).

  Without an OS-level bond, BLE indications get dropped silently and you'll
  see writes succeed but no replies arrive.
- Python 3.10+.

## Install (editable, for development)

```bash
cd benshi_mac
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# One-time macOS fix: create a patched Python.app inside the venv with
# NSBluetoothAlwaysUsageDescription set. Without this, macOS kills the
# process with SIGABRT the instant bleak touches CoreBluetooth.
python scripts/mac_bluetooth_setup.py

# Audio extras come later; not needed for Phase 1:
# pip install -e '.[audio]'
```

### About the Bluetooth permission hack

macOS requires every process that touches Bluetooth to ship an
`NSBluetoothAlwaysUsageDescription` key in its bundle `Info.plist`. Neither
Homebrew Python nor python.org Python declares one, so the first `bleak`
call aborts with:

> This app has crashed because it attempted to access privacy-sensitive
> data without a usage description.

`scripts/mac_bluetooth_setup.py` copies the interpreter's `Python.app`
bundle into `.venv/Python.app`, patches its `Info.plist` (adding the usage
description and assigning a unique `CFBundleIdentifier`), re-signs it
ad-hoc, re-links `.venv/bin/python` at the patched copy, and clears any
stale TCC decision for the new bundle ID. Idempotent and local to the venv.

After that, the first `benshi scan` invocation should produce a macOS
permission prompt. If no prompt appears and the process still crashes,
your **terminal / IDE** also needs Bluetooth permission:

> System Settings → Privacy & Security → Bluetooth → toggle on for
> Terminal / iTerm / VS Code / whatever you're launching from.

## Quickstart — Phase 1

Scan for nearby radios:

```bash
benshi scan
```

Connect, fetch device info, dump first 32 channels, and tail notifications:

```bash
benshi connect AA:BB:CC:DD:EE:FF
```

Dump the full channel table (tab-separated):

```bash
benshi channels AA:BB:CC:DD:EE:FF --count 200
```

Sniff every inbound GAIA frame as timestamped hex (Phase 2 tool for
observing BLE traffic while the radio is in FM RX):

```bash
# Register all notification classes so the radio pushes everything it has.
benshi sniff AA:BB:CC:DD:EE:FF \
  --register HT_STATUS_CHANGED \
  --register HT_CH_CHANGED \
  --register HT_SETTINGS_CHANGED \
  --register DATA_RXD \
  --register RADIO_STATUS_CHANGED
```

## Library usage

```python
import asyncio
from benshi import Radio
from benshi import protocol as p

async def main():
    async with Radio("AA:BB:CC:DD:EE:FF") as radio:
        info = await radio.device_info()
        print(info)

        radio.on_notification(lambda ev: print("event:", ev))
        await radio.register_notification(p.EventType.HT_STATUS_CHANGED)
        await asyncio.sleep(60)

asyncio.run(main())
```

## Project layout

```
benshi_mac/
├── benshi/
│   ├── protocol/       # vendored from benlink (Apache-2.0) — see NOTICE.md
│   ├── link.py         # BLE transport via bleak + CoreBluetooth
│   ├── radio.py        # high-level async API, command/reply matching
│   ├── cli.py          # scan/connect/channels/sniff subcommands
│   └── audio/          # Phase 3 — RFCOMM SPP via PyObjC + IOBluetooth
├── examples/
├── docs/
│   └── ble_fm_rx_trace.md   # Phase 2 deliverable
├── pyproject.toml
├── LICENSE.benlink
├── NOTICE.md
└── README.md
```

## Roadmap

- **Phase 1 (done-ish):** BLE control — device info, channel dump, settings,
  notifications. Validate against `benshi_ble_confirmed.md`.
- **Phase 2 (next):** Put the radio in FM RX, run `benshi sniff`, commit the
  trace to `docs/ble_fm_rx_trace.md`. Confirms audio is not on BLE.
- **Phase 3:** RFCOMM SPP audio via PyObjC + `IOBluetoothRFCOMMChannel`.
  SBC codec, 0x7E framing, sounddevice for I/O.
- **Phase 4:** Ergonomic public API, examples, packaging.

## References

- [`../benshi_ble_confirmed.md`](../benshi_ble_confirmed.md) — authoritative
  protocol doc. The plan and this library follow its §2–§7 verbatim.
- [benlink](https://github.com/khusmann/benlink) — Python reference whose
  `protocol/` subtree we vendor.
- [HTCommander](https://github.com/Ylianst/HTCommander) — C# reference,
  particularly `src/radio/MacBluetoothBle.cs` for CoreBluetooth patterns.
- [`../htcommander_flutter/lib/radio/`](../htcommander_flutter/lib/radio/) —
  sibling Dart/Flutter implementation (Linux/Windows) used for cross-validation.

## Licensing

The vendored `benshi/protocol/` subtree is Apache-2.0 (see `LICENSE.benlink`
and `NOTICE.md`). New code in this repo is under the parent project's
license unless/until we add a top-level `LICENSE` here.
