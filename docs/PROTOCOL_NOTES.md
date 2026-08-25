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
- **"Never write flash that's already correct" is enforced at two layers**,
  by explicit user request (protect ESC flash memory from needless wear):
  1. Batch-level pre-filter (`_auto_select_flash_targets`, only when
     `target_indices=None` - the normal button/auto-flash-mode path): skips
     any ESC whose *already-known* (from the last scan) settings match,
     before even attempting `cmd_DeviceInitFlash` on it. Pure optimization -
     avoids the round-trip entirely for the common case.
  2. **Hard guarantee inside `_flash_one` itself**: right before every
     single write, it does a *fresh* `cmd_DeviceRead` and compares against
     the target config; if they already match, `cmd_DeviceWrite` is never
     called, no matter how the ESC ended up in the target list (including
     an explicit list that bypasses layer 1). This is the actual invariant -
     layer 1 is just a fast path on top of it.
  Real-world motivation: a batch flash on 2026-08-22 rewrote 3 ESCs that
  already matched byte-for-byte and only 1 that actually needed it.
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
- Persistent technical log file (`~/.fast_reconfig_esc/logs/fast_reconfig_esc.log`,
  `file_logger.py`, rotating 5×5MB): every MSP response value, full hex
  dumps of each ESC's settings before/after a write, per-ESC identity
  breakdown - a superset of what the GUI panel shows, for after-the-fact
  debugging. Reviewing this log against real captures also caught and fixed
  a real bug: `MSP_API_VERSION` was displayed as `"{protocol}.{major}.{minor}"`
  when it should be `"{major}.{minor}.{protocol}"` (raw `00 02 05` is
  `"2.5.0"`, not `"0.2.5"`) - confirmed against the real FC's own bytes.
  Also added a confirmed-correct `MSP_BATTERY_STATE` voltage decode
  (0.01V uint16 at payload offset 9-10, matching a real 25.31V reading).
- Full decoded-settings display (`eeprom_values.py`): every scaling
  factor/offset/enum for the ~29 "Base" tab fields (Timing advance, Motor
  KV, PWM Frequency, servo thresholds, current/temperature limits, brake,
  sinusoidal startup, etc) transcribed from am32-configurator's
  `pages/configurator.vue` (the actual `<SettingField ... />` props driving
  the site's own UI) - not guessed. Verified field-by-field against a real
  ESC capture where every decoded value matched the site's own display
  exactly (see Protocol facts below for the source citation and the full
  field list). Logged as one line per ESC on every discovery, and once per
  `Flash All` for the target config being written.
- **Real-hardware end-to-end validation (2026-08-22, same FC on COM16,
  this time with ESCs actually wired) - all three previously-open items
  below are now confirmed:**
  - Discovery found 4 real ESCs (of 12 outputs) repeatably across multiple
    rescans, each correctly identified: `MCU=ARM64K`, `type=12S33mV`,
    `firmware=ESCV_12S33mV`, `fw_version=2.16`, `boot_pin=PB4`,
    `boot_version=7`, `eeprom_version=1` - exact match to the reference
    am32.ca session log captured earlier on the same hardware.
  - `Flash All` targeted at just the 4 found ESCs (auto-selected, indices
    `[0,1,2,7]`) wrote and read back all 4 successfully (`4/4 succeeded`);
    per-ESC log shows the correct MCU-specific `eeprom_offset=0xF800`
    (ARM64K) was used, not a hardcoded/wrong address.
  - The mixed-batch fallback (4 responsive + 8 returning
    `ACK_D_GENERAL_ERROR`) ran cleanly with no crash, matching the
    mocked-hardware tests' behavior exactly.

- **Config tab: view / edit / compare (2026-08-24)** — a second tab
  (`QTabWidget`, top bar and log panel stay outside it, always visible)
  alongside the original "Main" flashing tab:
  - `eeprom_fields.py` is now the single source of truth for every editable
    field's raw-byte ↔ real-world-value math (offset, scaling
    factor/offset/`only_display_factor`, min/max/step, unit, enum options,
    eeprom-version gating, and the two-field visibility dependency
    `LOW_VOLTAGE_THRESHOLD`/`ABSOLUTE_VOLTAGE_CUTOFF` follow off
    `LOW_VOLTAGE_CUTOFF`) — transcribed from `pages/configurator.vue`'s
    `<SettingField>` props, same source `eeprom_values.py` was already built
    from. `eeprom_values.decode_settings()` (used by the technical log) was
    refactored to a thin formatter on top of this shared table instead of
    its own hardcoded branches, so the log and the editor can never disagree
    about a formula. The refactor incidentally fixed a latent bug: the old
    decoder showed "Active brake power" unconditionally, including reading
    garbage `0xFF` from an eeprom-v1 buffer where that byte range isn't
    populated (offset 0x0C is only valid `eeprom_version>=3` per the JS
    source's own `minEepromVersion` annotation) — now correctly gated like
    every other v3+ field.
  - `decode_field`/`encode_field` round-trip-tested exhaustively (every raw
    byte 0-255, for every one of the ~39 field specs) in
    `tests/test_eeprom_fields.py` — the correctness net for a feature that
    writes directly to ESC flash.
  - `EscManager.flash_all(path, ...)` is now a thin wrapper: load the file,
    then call the new `EscManager.flash_bytes(config_bytes, target_indices)`,
    which holds the actual per-ESC loop. The editor's "Apply to ESC(s)"
    button drives `flash_bytes` directly with its in-memory buffer — same
    `_flash_one`/`_auto_select_flash_targets` code path either way, so the
    skip-if-already-matches guarantee and mandatory readback-verify apply
    identically whether the source is a file or the editor.
  - `config_editor_widget.py` (`ConfigEditorPage`) builds the entire form
    generically from `eeprom_fields.FIELD_SPECS`, grouped into the same
    sections as the site (Essentials/Motor/Extended/Limits/Current
    control/Sinusoidal Startup/Brake/Servo). Spin boxes instead of the
    site's slider widgets - same min/max/step/unit, simpler for a desktop
    app, not a pixel-match. Edits write live into an in-memory
    `bytearray` (`self._working_config`) - nothing touches the loaded file
    or hardware until the user clicks **Save to file** or **Apply to
    ESC(s)**. Can be seeded either from **Load from file** or **Read from
    ESC #N** (a dropdown of currently-scanned ESCs, sourced from
    `EscInfo.settings` added to the existing `esc_status_changed` signal
    payload - no new cross-thread call needed, MainWindow just caches what
    already flows through that signal).
  - `esc_comparison_widget.py` (`EscComparisonList`) is a compact table
    (#, ESC, status) fed from the exact same `esc_status_changed` stream the
    Main tab's big status cards already consume - purely a different
    rendering of already-existing state, no new EscManager logic.
  - `STARTUP_MELODY` (128-byte RTTTL tune) and `CAN_SETTINGS` (16 bytes) are
    deliberately not editable - excluded from `FIELD_SPECS` entirely, so
    they're always copied through unchanged from whatever buffer the editor
    was seeded from and can never be corrupted by editing other fields.
- **Config tab visual redesign to match am32.ca's own "Base" tab (2026-08-24,
  `am32.ca_configurator.png`)** — the user said people are already used to
  that exact look and won't want anything different, so the field grid was
  reworked from a plain stacked `QFormLayout` to match it: each group is a
  bordered `QGroupBox` with the title on the border line, boolean fields
  stack as a left-hand checkbox column, and every enum/number field sits in
  a 3-wide grid as label+decorative-dots → `QSlider` → value-text-below,
  same shape as the site. Sliders deliberately operate over the **raw
  EEPROM byte domain** (`encode_field(spec, min_display)`/`max_display` for
  the range, direct raw-byte writes on drag) rather than display units,
  because a field's display "step" doesn't always evenly divide its actual
  raw-byte resolution (e.g. `TIMING_ADVANCE` v3+: step=1° but each raw
  increment is really 0.9375°) - a display-stepped slider could skip
  achievable raw values entirely; the raw-domain slider can always reach
  every byte, same guarantee the old typeable spin box had. `FieldSpec.step`
  became dead weight once the spin box was gone and was removed.
  `eeprom_values.py` gained a public `format_field_value(spec, settings)`
  extracted from `decode_settings`'s loop, so the editor's value labels and
  the technical log share the exact same formatting call, same rationale as
  `decode_field`/`encode_field` already being shared.
- **Standalone `.exe` build (2026-08-24)** — `scripts/build_exe.py` runs
  PyInstaller (`--onefile --windowed`, pinned in `requirements-dev.txt`) and
  produces `dist/AM32Flasher.exe`, a single ~38MB file with PyQt6/pyserial
  bundled in - no Python install needed to run it. Build with:
  `.venv\Scripts\python.exe scripts\build_exe.py`. `build/` and `dist/` are
  gitignored (regenerated output, not source); rerun the build script after
  any code change to get a fresh exe. Verified: launches standalone, window/
  Cyrillic text/COM-port list/persistent config path/log file path all
  render exactly like running from source - `~/.fast_reconfig_esc/` (config
  path + log file) lives outside the exe/install location either way, so
  packaging doesn't change where state is kept. A desktop shortcut
  ("AM32 Flasher.lnk") points at `dist\AM32Flasher.exe` for double-click
  launch.
  - **Known gotcha - Windows Smart App Control can block it outright**:
    confirmed on this machine (`VerifiedAndReputablePolicyState=1` in
    `HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy`, i.e. SAC on in
    evaluation mode) - a freshly rebuilt, unsigned exe has no cloud
    reputation yet, so SAC can refuse to run it at all ("An Application
    Control policy has blocked this file"), with no click-through like
    SmartScreen offers. This is a stock Windows 11 22H2+ feature (default
    ON on clean installs), so any other target PC may hit the same wall.
    Decision (2026-08-24): not worth code-signing for this use case: leave
    it as-is and warn whoever receives the exe. If someone hits the block,
    they need to either disable Smart App Control themselves (Settings ->
    Privacy & security -> Windows Security -> App & browser control ->
    Smart App Control - a system security setting, so this app can't do it
    for them) or get a code-signing certificate reputable enough to satisfy
    SAC (not done here, costs money and doesn't fully guarantee passing
    immediately either).
- **Accidental-edit protection in the Config tab (2026-08-24)**:
  - Sliders/dropdowns (`_ClickToWheelSlider`/`_ClickToWheelComboBox` in
    `config_editor_widget.py`) only respond to the mouse wheel once
    actually clicked (focused) - wheel-scrolling the long settings page
    routinely passes the cursor over a field, and an unfocused control
    ignores the wheel event so it bubbles up to the surrounding
    `QScrollArea` instead of silently nudging a value. Moving the mouse off
    a focused control clears its focus again (`leaveEvent`), so scrolling
    past it a second time without re-clicking is safe; an active slider
    drag that momentarily leaves the widget's rect is not interrupted
    (`isSliderDown()` guards that). First cut of this (committed
    `bc1c1ae`) blocked the wheel unconditionally; refined the same day to
    this click-to-arm version instead, so wheel-adjustment is still
    available once a field is deliberately selected.
  - `ConfigEditorPage.diff_text()` compares the working buffer against the
    baseline captured at the last Load/Read/Save/Apply and renders every
    changed field as "Label: old -> new". **Apply to ESC** shows this and
    requires an explicit confirm before writing to real hardware; **Save**
    shows it as a post-save summary; closing the window with unsaved edits
    (`MainWindow.closeEvent`) prompts Save/Discard/Cancel with the same
    diff, and a cancelled/failed Save aborts the close instead of losing
    the edits silently.
- **"Хотспот" tab: hands-off hot-plug flashing station (2026-08-24)** - a
  third tab (`hotspot_widget.py`, `HotspotFlashPage`) for a non-technical
  operator reflashing many ESCs back-to-back as fast as possible: pick a
  config file and a port once, click Старт, then just plug devices in one
  after another - no further clicks. A `QTimer` (1s) polls
  `list_fc_ports()` for the chosen port's presence and drives a small state
  machine (waiting_device -> connecting -> checking -> flashing ->
  waiting_removal, plus warning/error branches) entirely through the
  existing `EscManager` signals - no protocol changes, this is pure
  orchestration on top of what already existed. Calling `flash_bytes`
  unconditionally after every scan (same as the Main tab's "Режим
  автопрошивки" checkbox already does) is what makes "flash only what's
  wrong, skip what's already correct" work for free - the skip-if-matches
  guarantee lives in `_flash_one`, not here. A big color banner
  (`ACCENT_ERROR` red "НЕ ВІДКЛЮЧАЙТЕ" while writing, `ACCENT_GREEN` green
  "Можна відключати" once verified, `QApplication.beep()` on done) is the
  only thing the operator needs to watch. After a finished cycle it
  disconnects immediately and waits for the port to actually *disappear*
  before re-arming - reconnecting to the same still-plugged device would
  otherwise spam reconnect/reflash loops. Session counters (flashed/already
  -ok/errors) are a pure throughput readout, no effect on behavior.
  `MainWindow` enforces mutual exclusion: while Хотспот is armed, the top
  bar's manual Connect/port controls and the Main/Config tabs' action
  buttons are disabled (only one workflow may hold the single `EscManager`
  connection at a time), and symmetrically Старт is disabled while a manual
  connection is already active elsewhere.
- **Хотспот fixes after real-hardware use (2026-08-25):**
  - **"Старт does nothing" turned out not to be a bug** - the button was
    correctly disabled because the app was still manually connected from
    an earlier Main-tab test (confirmed from the log file: a successful
    manual connect + discovery, no subsequent disconnect, then the report
    came in). The real gap was that the reason was only ever shown as a
    tooltip - easy to miss entirely. Fixed: the same reason now also shows
    as always-visible text next to the button
    (`HotspotFlashPage._set_start_hint`), not just on hover. Also: the tab
    now auto-loads the same last-used config path the Main tab remembers
    (`settings_store.load_last_config_path`) instead of always starting
    with no config and silently blocking Старт for that reason too.
  - **The original "wait for the port to disappear before starting the
    next round" design (2026-08-24) was wrong for the actual physical
    setup**: the user clarified the FC stays connected over USB the whole
    time - only individual ESC signal wires get swapped, and neither MSP
    nor FourWay has any way to notice that ("FC не скаже коли пропадає
    підключення"). So the port never disappears between ESCs, and the
    tool would just sit at the "done" banner forever after the first one.
    Fixed: after a round finishes (done/nothing-found/write-error), the
    connection now stays open and the next `discover_all` fires
    automatically on the following poll tick (~1s later) - continuous
    scan → flash → scan → flash, no clicks, no reconnects between rounds.
    Re-scanning an ESC that hasn't changed is harmless (still no
    unnecessary write, same `_flash_one` guarantee as always). The only
    thing that still triggers an actual disconnect is the FC's own USB
    link genuinely disappearing from `list_fc_ports()` - a real, if rare,
    event distinct from an ESC wire changing.

**Explicitly out of scope for now:** full `.hex` firmware flashing (only the
184-byte EEPROM/settings block is read/written); editing `STARTUP_MELODY`/
`CAN_SETTINGS` (see above); "Send default config" (no default byte tables
available); watching/flashing more than one port at once in the Хотспот tab
(confirmed with the user - normal case is one port per FC, and the whole
app is single-connection by design, so true parallel multi-port flashing
would need a much larger redesign). The architecture leaves room for
firmware flashing later via the same `FourWayClient.read`/`write`
primitives.

## Protocol facts (sourced — re-verify against upstream if anything here
stops matching real-device behavior)

Sources: `am32-firmware/am32-configurator` (`src/communication/four_way.ts`,
`src/communication/msp.ts`, `@am32/serial-msp` npm package source,
`components/SerialDevice.vue`, `src/eeprom.ts`, `src/mcu.ts`,
`pages/configurator.vue`, `components/SettingField.vue`,
`utils/buffer-to-settings.ts`), `am32-firmware/Offline-Configurator`
(`fourwayif.cpp`), `iNavFlight/inav` (`src/main/fc/fc_msp.c`,
`src/main/io/serial_4way.c`).

- **Settings display formulas** (`eeprom_values.py`): `pages/configurator.vue`
  passes `display-factor`/`offset`/`only-display-factor`/`unit`/`options`
  props into each `<SettingField>`; `SettingField.vue`'s actual formula is
  `display_value = raw*displayFactor + offset`, then final text is
  `display_value * onlyDisplayFactor` (only `TIMING_ADVANCE` uses the
  latter). Notable ones: `MOTOR_KV = raw*40+20`; `TIMING_ADVANCE = raw*7.5`
  for `eeprom_version<3`, `(raw-10)*0.9375` for `>=3`; `SERVO_LOW/HIGH =
  raw*2+750/1750`; `SERVO_NEUTRAL = raw+1374`; `CURRENT_LIMIT = raw*2`,
  shown as `"DISABLED"` when `>=202`; `TEMPERATURE_LIMIT` shown as
  `"DISABLED"` when `raw>=141`; `ESC_PROTOCOL` is an enum (0=Auto,
  1=DShot, 2=Servo, 3=Serial, 4=EDT ARM); most other fields (poles, beep
  volume, PWM frequency in kHz, brake/sine levels, servo dead band) are
  shown as the raw byte with no scaling. All independently confirmed
  against a real ESC where every one of ~20 numeric fields matched the
  site's own display exactly (24kHz, KV 380, servo 1006/2006/1502/50,
  current limit DISABLED, timing 22.5°, etc).

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
    eeprom_fields.py          declarative editable-field specs + decode_field/encode_field (source of truth for the editor AND the log formatter)
    eeprom_values.py          decode_settings(): thin log/summary formatter built on eeprom_fields.py
    serial_transport.py     pyserial wrapper, FC-port VID hinting
    eeprom_config.py        184-byte config file load/validate
    settings_store.py       persistent app settings (~/.fast_reconfig_esc/settings.ini)
    file_logger.py           persistent technical log file (~/.fast_reconfig_esc/logs/)
    models.py               EscState enum, ProbeResult, EscInfo (incl. raw settings bytes for "Read from ESC")
    esc_manager.py           discovery/flash orchestration; flash_all (file) + flash_bytes (buffer) share one per-ESC loop
    connection_worker.py     QThread hosting EscManager
    i18n.py                 en/uk translations
    flag_icons.py            QPainter-drawn language-switcher flags
    theme.py                 dark QSS palette
    main_window.py            QMainWindow: top bar + log panel (always visible) + QTabWidget(Main, Config, Хотспот)
    esc_status_widget.py      Main tab's per-ESC status card
    config_editor_widget.py   Config tab: generic form built from eeprom_fields.FIELD_SPECS, in-memory working buffer, load/read/save/apply
    esc_comparison_widget.py  Config tab: compact per-ESC comparison table, fed by the same esc_status_changed stream as the Main tab
    hotspot_widget.py         Хотспот tab: hands-off hot-plug flashing station, port-presence poll + state machine on top of the existing EscManager signals
tests/
    test_protocol.py        CRC/framing unit tests, known-vector checks
    test_eeprom_fields.py     decode_field/encode_field round-trip tests (every raw byte, every field spec)
    test_eeprom_values.py     decode_settings() vs. a real ESC capture's am32.ca-displayed values
    test_esc_manager.py      mocked-hardware orchestration/fallback/decoding tests
    test_file_logger.py       confirms detailed traces actually land in the log file
```

## Next steps

All of the original plan's hardware verification steps are now done (see
Status above - real discovery, real flash+verify, real mixed-batch fallback,
all confirmed 2026-08-22). Remaining open items:

1. Investigate why the other 8/12 outputs return `ACK_D_GENERAL_ERROR` on
   this FC - likely just nothing physically wired to those outputs, but
   worth a deliberate check (e.g. wire one of them and confirm it then
   shows up) rather than assuming.
2. If the power-cycle hint theory holds up in practice, consider surfacing
   it more prominently (not just a log line).
3. Full `.hex` firmware flashing remains explicitly out of scope; revisit
   only if asked for.
4. **Config tab click-through on the live app is still owed**: everything
   was verified via the automated test suite (74 tests, incl. an exhaustive
   encode/decode round-trip for every field) plus an offscreen
   `QT_QPA_PLATFORM=offscreen` integration run that exercised the real
   signal-driven flow (tab switch → Read from ESC → edit a field → Apply →
   flash_finished re-enabling the button) end-to-end - Claude has no tool to
   send real mouse clicks to a native Win32 window (only Chrome browser
   automation is available), so a human click-through of Load/Read/edit/
   Save/Apply on the actual running app, plus a real Apply-to-ESC hardware
   write, hasn't happened yet.
