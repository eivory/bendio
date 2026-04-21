# benshi

A macOS Python library for two-way audio with BTech UV-Pro and other
Benshi-family handheld radios (GA-5WB, VR-N76, VR-N7500, GMRS-Pro).

**Status:** full-duplex audio working. BLE control, RX (radio → Mac speaker)
and TX (Mac mic → radio) all proven end-to-end against a UV-Pro.

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

## Known caveats

### IOBluetooth is deprecated

The Classic Bluetooth RFCOMM path this library uses for audio depends on
Apple's `IOBluetooth` framework. Apple deprecated `IOBluetooth` in favour
of `CoreBluetooth`, but `CoreBluetooth` is BLE-only and provides no
replacement for RFCOMM / SPP. There is no supported alternative today.

`IOBluetooth` still works on every shipping macOS release (including
macOS 15 Sequoia and later), but Apple has signalled it won't receive new
development. If Apple eventually removes it, the audio path here will
need to be re-implemented — likely via a `DriverKit`-based RFCOMM shim,
or by giving up on Classic Bluetooth for these radios and pushing the
vendor to ship a BLE audio characteristic.

Today: fine. Long-term: a ticking risk on the Classic BT half of the
library. BLE control (`benshi/link.py`) is unaffected.

## Roadmap

- **Phase 1 (done):** BLE control — device info, channel dump, settings,
  notifications. Validated against `benshi_ble_confirmed.md`.
- **Phase 2 (done):** Put the radio in FM RX, ran `benshi sniff` and
  `benshi sniff-all`, committed the trace to `docs/ble_fm_rx_trace.md`.
  Confirmed audio is not on BLE on any service, including an undocumented
  vendor service this library is the first to inspect.
- **Phase 3 (done):** RFCOMM SPP audio via PyObjC + `IOBluetoothRFCOMMChannel`.
  SBC codec (ffmpeg subprocess — homebrew dropped the standalone `sbc`
  formula, so `libsbc` via ctypes wasn't feasible), 0x7E framing,
  sounddevice for I/O. Breakdown:
    - 3a: open/close RFCOMM cleanly
    - 3b: dump raw bytes; confirm SBC framing on channel 2 ("BS AOC")
    - 3c: HDLC deframer + SBC frame splitter
    - 3d: live RX audio to default output (~200 ms latency)
    - 3e: TX test tone → live mic TX, full duplex
- **Phase 4 (partial):** Ergonomic API + examples + packaging.
  Low-level building blocks (`Radio`, `RfcommTxSession`, `SbcStream`,
  `SbcEncodeStream`, `Deframer`, `build_audio_packet`) are stable and
  composable; `examples/` covers device info, sniff, listen, and PTT.
  TODO: a top-level `BenshiRadio` facade that unifies BLE control +
  audio behind one async context manager, in the shape of benlink's
  `RadioController` but extended to expose `.audio.start_rx()` /
  `.audio.start_tx()` (which benlink never finished).

## Future work

- **Cross-check against HTCommander-X on Linux/Windows.** Run the
  reference Dart implementation against the same physical radio,
  compare: does it see the same `GET_DEV_INFO` bytes? Does it dump
  the same channel table byte-for-byte? Any protocol nuances we
  handle differently would surface here. Not a blocker for using the
  library, but a high-value correctness check once a Linux/Windows
  host with the radio is at hand.
- **Swift or Dart port** of the Python library, so the `benshi_mac`
  code becomes reusable from a native Mac app (Swift/SwiftUI) or from
  HTCommander-X Flutter. The Python version stays as the executable
  spec and keeps the reverse-engineering loop tight.
- **Spacebar PTT UX** for the CLI — the plan originally called for
  `benshi ptt` as hold-to-talk. Currently implemented as
  `benshi rfcomm-tx-mic --duration N`, which works but is
  duration-bounded rather than interactive.
- **IOBluetooth fallback plan.** See the caveat above — Apple has
  deprecated the framework with no CoreBluetooth equivalent for
  RFCOMM. Worth scoping a DriverKit-based shim or other alternative
  before Apple removes the API, not after.

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
