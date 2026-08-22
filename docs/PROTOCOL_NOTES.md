# AM32 Multi-ESC Flasher — Project Notes

Working notes for `fast-reconfig-ESC`: an offline Python/PyQt6 replacement for
`am32.ca/configurator`, so ESC flashing/config doesn't depend on that
third-party website. Kept here so decisions and protocol facts don't have to
be re-derived from scratch later.

## Status

**Done:**
- Full MSP + FourWay protocol stack, implemented and unit-tested (17 tests,
  incl. standard CRC-16/XMODEM and CRC-8/DVB-S2 catalogue check values)
- PyQt6 GUI: single Connect/Disconnect toggle button, COM-port auto-hint by
  FC vendor ID, ESC status grid (up to 12 cards, color-coded), log panel,
  config file picker, auto-flash mode, adjustable per-ESC connect-retry count
- i18n: English + Ukrainian (no other languages), flag icons drawn with
  QPainter (Qt on Windows doesn't render color flag-emoji reliably in normal
  widgets, regardless of font)
- Config-match detection: on scan, each ESC's current settings are compared
  byte-for-byte against the loaded config file → green "matches" / amber
  "different config" / amber "blank" / red "error", updated live even
  without a rescan when a new config file is loaded
- "Power-cycle hint": after 2 consecutive all-empty scans, logs a warning
  suggesting a power cycle (community-reported ESC bootloader quirk — see
  Known gotchas below; not independently verified, presented as a hint only)
- Real-hardware validation (2026-08-22, user's actual FC on COM16, 12 motor
  outputs, no ESCs physically wired): full scan correctly returned
  `ACK_D_GENERAL_ERROR` on all 12 without crashing — confirms the
  per-ESC try/except isolation works on real hardware, not just in mocked
  tests
- ESC identity/firmware display: a second FourWay read (32-byte name block
  at `eeprom_offset - 32`) plus decoded settings fields now surface MCU
  type, firmware name/version, bootloader pin/version, EEPROM layout
  version, and reversed/3D-mode flags per ESC — compact label on the card,
  full breakdown on hover. Field offsets and the pin-decoding algorithm are
  sourced from am32-configurator's `src/eeprom.ts`/`src/mcu.ts` (see below);
  cross-checked structurally against a real am32.ca session log the user
  captured on 2026-08-22 showing e.g. `PIN: PB4`, `MCU Type: 12S33mV`,
  `Firmware: ESCV_12S33mV v2.16` for real responding ESCs (that session was
  against the reference site, not yet against this tool with a live ESC).
- Last-used config file path now persists across restarts, in a real `.ini`
  file (`~/.fast_reconfig_esc/settings.ini`, `settings_store.py`) — not the
  registry, not inside the git-tracked project directory.

**Not yet done — blocked on having a real ESC physically wired to this
tool (the am32.ca session above used the reference site, not this app):**
- Confirm a genuinely present ESC shows green with a correct MCU name and
  the new firmware/bootloader detail fields populated (currently only
  verified via mocked `FourWayClient` in `tests/test_esc_manager.py`,
  using field values transcribed from the real am32.ca session log)
- Confirm a real `Flash All` write + readback-verify round-trip on one ESC
- Confirm the fallback behavior with a *mixed* real batch (some ESCs wired,
  one deliberately unplugged) rather than the all-missing case already
  tested

**Explicitly out of scope for now:** full `.hex` firmware flashing (only the
184-byte EEPROM/settings block is read/written). The architecture leaves
room for it later via the same `FourWayClient.read`/`write` primitives.

## Protocol facts (sourced — re-verify against upstream if anything here
stops matching real-device behavior)

Sources: `am32-firmware/am32-configurator` (`src/communication/four_way.ts`,
`src/communication/msp.ts`, `@am32/serial-msp` npm package source,
`components/SerialDevice.vue`, `src/eeprom.ts`, `src/mcu.ts`),
`am32-firmware/Offline-Configurator` (`fourwayif.cpp`), `iNavFlight/inav`
(`src/main/fc/fc_msp.c`, `src/main/io/serial_4way.c`).

- **MSP passthrough trigger**: `MSP_SET_PASSTHROUGH` (code `0xF5`/245) sent
  with an **empty payload** → INAV's "legacy format" path, auto-selects
  `MSP_PASSTHROUGH_ESC_4WAY`. The single response byte is the **authoritative
  ESC count** (not `MSP_MOTOR`).
- **Per-ESC addressing** is at the FourWay layer only: `cmd_DeviceInitFlash`
  (0x37) / `cmd_DeviceReset` (0x35) take the 0-based ESC index as their
  single param byte, address field = 0. The bootloader remembers the
  selected ESC for subsequent Read/Write until the next InitFlash/Reset.
- **Config read/write uses `cmd_DeviceRead`(0x3A)/`cmd_DeviceWrite`(0x3B)**
  at an MCU-specific `eeprom_offset` — **not**
  `cmd_DeviceReadEEprom`(0x3D)/`cmd_DeviceWriteEEprom`(0x3E), which are
  vestigial BLHeli-EEPROM commands AM32's bootloader doesn't use for this.
  This was an early wrong assumption, corrected before any code was written.
- **MCU table** (signature → eeprom_offset/flash_offset/page_size):
  `0x1F06` STM32F051 (`0x7C00`), `0x3506` ARM64K (`0xF800`), `0x1506` NXP
  ESC_8KB_PAGE (`0xE000`); all `flash_offset=0x08000000`, `page_size=1024`.
  Resolved **per-ESC** from that ESC's own `DeviceInitFlash` signature —
  never hardcoded globally, since different ESCs on one aircraft can differ.
- **Settings block = 184 bytes exactly** (`LAYOUT_SIZE = 0xB8`), no embedded
  checksum → treated as an opaque blob, written back byte-for-byte.
- **FourWay frame**: `0x2F`(host→ESC)/`0x2E`(ESC→host) start, cmd, 16-bit BE
  address, length byte (0=256), params, **[response only: 1 ACK byte]**, then
  2-byte CRC-16/XMODEM (poly `0x1021`, init 0) over everything except the
  CRC bytes themselves. The ACK byte sits *inside* the CRC-covered region.
- **ACK `0x0F D_GENERAL_ERROR`** and **`0x08 I_INVALID_CHANNEL`** are the
  two "clean not-found" outcomes for a missing ESC — mapped to a red status,
  not treated as a crash-worthy error.
- **MSP v1 only** is actually exercised (every command used is ≤254); v2
  implemented too for completeness.
- **Retry/timing**: FourWay default 10 retries / 200ms timeout / 250ms
  between attempts (app default lowered to **2 retries** for the
  ESC-presence-probe use case — see below). ~2s delay after
  `MSP_SET_PASSTHROUGH` before FourWay traffic starts.
- **Name block**: a *second* FourWay read, 32 bytes at `eeprom_offset - 32`,
  NUL-terminated ASCII (e.g. `"ESCV_12S33mV"`). `mcu_type` = the substring
  after the last `_` (e.g. `"12S33mV"`). Best-effort in this tool: a failed
  name read does not fail the whole probe (upstream's `getInfo()` does
  fail the probe on this - a deliberate divergence).
- **Settings-block field offsets** (full table in `eeprom_layout.py`,
  sourced from `EepromLayout` in `src/eeprom.ts`): `LAYOUT_REVISION`@0x01,
  `BOOT_LOADER_REVISION`@0x02, `MAIN_REVISION`@0x03, `SUB_REVISION`@0x04
  (firmware version = `"{MAIN_REVISION}.{SUB_REVISION}"`), `MOTOR_DIRECTION`
  @0x11 (reversed), `BIDIRECTIONAL_MODE`@0x12 (3D mode). Many other fields
  (current/brake/servo/etc. tuning) are catalogued but not yet surfaced in
  the UI - this app only *displays* identity info, it doesn't edit settings
  field-by-field.
- **Bootloader pin decoding** (`Mcu.parseBootLoaderPin` in `src/mcu.ts`):
  from the raw `DeviceInitFlash` response's `input` byte (already captured
  as `DeviceInfo.input_pin`) - `port = pin >> 4` indexes `A..Z`,
  `pin_number = pin & 0xF` is used directly (0-15) → e.g. `0x14` → `"PB4"`.
  No extra read needed, it's decoded from data already fetched.
- **Serial port hints**: FC USB VIDs incl. `0x0483` (STMicro native
  CDC-ACM — what most Betaflight/INAV FCs enumerate as). `0x10C4` (Silicon
  Labs CP210x) is listed under *direct-ESC-connect* mode in the reference
  app, **not** the FC passthrough path — confirmed unrelated to an earlier,
  separate CP210x driver question in this project's history.
- **Write safety**: the reference web app has readback compare code but it's
  commented out (logs success unconditionally). This tool does **not** skip
  it — every write is read back and compared, with a distinct
  "readback mismatch" error if it fails.

## Why default ESC-connect retries = 2, not 10

A missing ESC (nothing wired to that output) returns `ACK_D_GENERAL_ERROR`
*every single retry* — there's nothing to wait out. With the reference
site's defaults (10 retries × ~450ms) a 12-output scan with few ESCs
attached took up to ~54s in real testing. Lowered the app default to 2 and
exposed it as an adjustable "ESC retries" spinner (1–20) in the UI.

## Known gotchas / troubleshooting

- **Repeated failed connection attempts can wedge an ESC bootloader.** This
  is a community-reported behavior for BLHeli/AM32-style bootloaders (not
  independently verified against AM32 firmware source in this project) — if
  scans keep returning 0 ESCs found, a power cycle of the FC/ESC often
  clears it. The app now logs a warning hint after 2 consecutive all-empty
  scans (`EMPTY_SCANS_BEFORE_POWER_CYCLE_HINT` in `esc_manager.py`).
- **CP210x USB driver** (separately investigated earlier in this project,
  `C:\Users\Comp\Downloads\CP210x_Windows_Drivers`) is Silicon Labs' official,
  WHQL-signed VCP driver — legitimate, but per the am32-configurator source
  it's associated with *direct single-ESC* connections, not the FC
  passthrough path this tool uses (FCs typically enumerate as native STM32
  CDC-ACM, VID `0x0483`).
- Windows Qt widgets (QLabel/QPushButton/QComboBox) do not reliably render
  color flag emoji regardless of font — the language switcher draws real
  flag icons with QPainter instead (`flag_icons.py`).
- `objectName`-based QSS styling does not auto-refresh when `objectName` is
  changed at runtime — must call `style().unpolish()`/`style().polish()`
  after (used for the Connect/Disconnect toggle button's color swap).

## Module map

```
src/fast_reconfig_esc/
    msp.py                MSP v1/v2 framing, CRC8-DVB-S2
    fourway.py             FourWay framing, CRC16-XMODEM, FourWayClient
    mcu_profiles.py         MCU signature -> eeprom_offset table
    eeprom_layout.py         184-byte settings field offsets, name-block/boot-pin decoding
    serial_transport.py     pyserial wrapper, FC-port VID hinting
    eeprom_config.py        184-byte config file load/validate
    settings_store.py       persistent app settings (~/.fast_reconfig_esc/settings.ini)
    models.py               EscState enum, ProbeResult, EscInfo
    esc_manager.py           discovery/flash orchestration, per-ESC isolation
    connection_worker.py     QThread hosting EscManager
    i18n.py                 en/uk translations
    flag_icons.py            QPainter-drawn language-switcher flags
    theme.py                 dark QSS palette
    main_window.py, esc_status_widget.py   PyQt6 UI
tests/
    test_protocol.py        CRC/framing unit tests, known-vector checks
    test_esc_manager.py      mocked-hardware orchestration/fallback/decoding tests
```

## Next steps

1. Get a real ESC wired to the FC and re-run the plan's hardware
   verification steps 3–5 (single-ESC discovery, single-ESC flash+verify,
   mixed-batch fallback) — including checking that the new firmware/
   bootloader detail fields populate correctly against a real device, not
   just against the am32.ca session log values used in tests.
2. If the power-cycle hint theory holds up in practice, consider surfacing
   it more prominently (not just a log line).
