# Changelog

All notable changes to this project are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/),
and versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Top-level `LICENSE` (Apache-2.0) and `NOTICE` file.
- `CHANGELOG.md`.
- `tests/` — offline pytest suite covering HDLC framing, SBC encode/decode
  round-trip, and GAIA message parsing.
- `.github/workflows/ci.yml` — lint + offline tests on every push.
- `pyproject.toml` now declares project URLs, classifiers, and optional
  `dev` extras for test tooling.

### Changed
- `NOTICE.md` was split: license-required attributions moved to `NOTICE`
  (plain text, Apache convention); dev-oriented empirical findings moved
  to `docs/PROTOCOL_NOTES.md`.

## [0.1.0] — initial working release

### Added
- BLE control (GAIA protocol) via `bleak`
  - `bendio.Radio`: async context manager with command/reply matching
  - `bendio.BleLink`: low-level BLE transport
  - Vendored `bendio/protocol/` from benlink (Apache-2.0)
- Classic Bluetooth RFCOMM audio on macOS
  - `bendio.audio.macos_rfcomm.dump_rfcomm` — read-only streaming
  - `bendio.audio.macos_rfcomm.transmit_rfcomm` — batch write
  - `bendio.audio.macos_rfcomm.RfcommTxSession` — streaming write
  - `bendio.audio.macos_rfcomm.RfcommChannel` discovery + SDP inspect
  - macOS TCC workaround script (`scripts/mac_bluetooth_setup.py`)
- Audio framing + codec
  - `bendio.audio.framing` — HDLC-style 0x7E/0x7D deframer + builder
  - `bendio.audio.sbc.SbcStream` — ffmpeg-backed streaming SBC decode
  - `bendio.audio.sbc.SbcEncodeStream` — ffmpeg-backed streaming SBC encode
- CLI: `bendio scan | connect | channels | sniff | sniff-all |
  rfcomm-probe | rfcomm-inspect | rfcomm-dump | rfcomm-sbc-dump |
  rfcomm-play | rfcomm-tx-tone | rfcomm-tx-mic | audio-devices`
- Examples: `01_device_info.py`, `02_sniff_fm_rx.py`, `03_listen.py`,
  `04_ptt.py`
- Docs: README, `docs/ble_fm_rx_trace.md` (Phase 2 capture + analysis)

### Protocol corrections vs. upstream references
- BLE uses raw `Message` bytes, not GAIA-framed `FF 01 …` — benlink was
  right, upstream reference `benshi_ble_confirmed.md` §3 was wrong.
- `REGISTER_NOTIFICATION` receives a 1-byte status reply on the UV-PRO
  (contradicts benlink's `body_disc` assertion that it cannot be a reply).

[Unreleased]: https://github.com/eivory/bendio/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/eivory/bendio/releases/tag/v0.1.0
