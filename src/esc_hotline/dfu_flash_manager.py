"""Pure (no Qt) wrapper around the external STM32CubeProgrammer CLI
(STM32_Programmer_CLI.exe), used by fc_flash_station.py to detect a
DFU-mode flight controller, flash its firmware, verify the write, and
reset it back into normal (FourWay-ready) run mode.

This deliberately does NOT implement the STM32 USB DFU protocol itself (no
pyusb/libusb dependency) - ST's own CLI tool already does this correctly
for every STM32 part, and it's the exact same tool already used by hand
for this job today. All this module does is shell out to it and interpret
its output - same "never trust a write without an explicit check" stance
as esc_manager.py's own readback-verify for the ESC side of this app.

KNOWN UNKNOWN, flag for whoever next has the real tool installed: the
exact text STM32_Programmer_CLI.exe prints for `-l` (list connected
ST-Link/USB-DFU interfaces) and for a successful `-w ... -v` (write +
verify) has NOT been verified against a real run (the tool isn't
installed on the machine this was written on). The parsing below follows
ST's documented CLI output shape - confirm against a real run and adjust
`list_devices`/`flash_and_verify` if the real text differs. If the parsing
is wrong, prefer failing safe: report "not found"/"not verified" rather
than guessing success.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .file_logger import get_logger

# Plain logging.Logger singleton, no Qt involved - safe to use from this
# otherwise Qt-free module. Every CLI invocation is logged verbosely here
# (exact command, full raw stdout/stderr, exit code, verdict) - this is
# the ONLY record of what the external tool actually said, and since its
# real output has never been seen on the machine this was written on (see
# module docstring), the first real run's log is what will actually
# explain whether the parsing assumptions below hold up.

# STMicroelectronics' own USB DFU bootloader VID:PID - the same one Windows
# Device Manager shows as "STM Device in DFU Mode". This is what tells a
# "FC sitting in DFU, ready to flash" line apart from an ST-Link debug
# probe or anything else STM32_Programmer_CLI's own device list might show.
DFU_VID_PID = "0483:df11"

DEFAULT_CLI_CANDIDATES = (
    r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
    r"C:\Program Files (x86)\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
)

LIST_TIMEOUT_S = 10.0
FLASH_TIMEOUT_S = 120.0
RESET_TIMEOUT_S = 10.0


@dataclass
class DfuDeviceInfo:
    port_id: str  # the "-c port=<id>" value to address this device, e.g. "usb1"
    raw_line: str  # the exact CLI output line this was parsed from, kept for the technical log


@dataclass
class FlashResult:
    success: bool
    message: str


def find_cli_path(override: Optional[str] = None) -> Optional[Path]:
    """Locate STM32_Programmer_CLI.exe: an explicit override first (operator
    -configured, settings_store's fc_flash_cli_path_override), then the
    usual install locations. Returns None rather than raising - the caller
    shows a clear, dismissable error in the GUI instead of crashing the app
    over a missing external tool (same style as build_exe.bat's "venv not
    found" message)."""
    flog = get_logger()
    if override:
        path = Path(override)
        if path.is_file():
            flog.info("DFU: using CLI path override: %s", path)
            return path
        flog.warning("DFU: CLI path override does not exist: %s", path)
        return None
    for candidate in DEFAULT_CLI_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            flog.info("DFU: found STM32_Programmer_CLI.exe at %s", path)
            return path
    flog.warning("DFU: STM32_Programmer_CLI.exe not found - tried override=%r and %s", override, DEFAULT_CLI_CANDIDATES)
    return None


def _run(cli_path: Path, args: list[str], timeout_s: float) -> subprocess.CompletedProcess:
    flog = get_logger()
    cmd = [str(cli_path), *args]
    flog.info("DFU: running %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    flog.info(
        "DFU: %s -> exit=%d, stdout=%r, stderr=%r",
        args, result.returncode, result.stdout, result.stderr,
    )
    return result


def list_devices(cli_path: Path) -> list[DfuDeviceInfo]:
    """Run `-l` and return only the DFU-mode entries (VID:PID 0483:df11) -
    an ST-Link debug probe can show up in the same list and must never be
    mistaken for a DFU-ready FC. Any failure to even run the tool is
    reported as "no devices", not an exception - this is called from a
    poll loop, where a single bad tick must not break the loop."""
    flog = get_logger()
    try:
        result = _run(cli_path, ["-l"], LIST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        flog.warning("DFU: `-l` failed to run: %s", e)
        return []

    devices: list[DfuDeviceInfo] = []
    for line in result.stdout.splitlines():
        if DFU_VID_PID not in line.lower():
            continue
        match = re.search(r"\b(usb\d+)\b", line, re.IGNORECASE)
        port_id = match.group(1).lower() if match else "usb1"
        devices.append(DfuDeviceInfo(port_id=port_id, raw_line=line.strip()))
    flog.info("DFU: `-l` parsed %d DFU device(s): %s", len(devices), [d.raw_line for d in devices])
    return devices


def flash_and_verify(
    cli_path: Path, port_id: str, firmware_path: str, timeout_s: float = FLASH_TIMEOUT_S
) -> FlashResult:
    """Write `firmware_path` (.hex or .bin) to the device at `port_id` and
    verify the write (-v) - the same operation the operator currently does
    by hand in the STM32CubeProgrammer GUI. Exit code alone is not trusted
    - success also requires an explicit "verified" marker in stdout.
    """
    flog = get_logger()
    if not Path(firmware_path).is_file():
        flog.error("DFU: firmware file not found: %s", firmware_path)
        return FlashResult(False, f"Файл прошивки не знайдено: {firmware_path}")
    flog.info("DFU: --- flashing FC: port=%s, firmware=%s ---", port_id, firmware_path)
    try:
        result = _run(cli_path, ["-c", f"port={port_id}", "-w", firmware_path, "-v"], timeout_s)
    except subprocess.TimeoutExpired:
        flog.error("DFU: flash timed out after %.0fs", timeout_s)
        return FlashResult(False, f"Прошивка не завершилась за {timeout_s:.0f}с (timeout).")
    except OSError as e:
        flog.error("DFU: could not launch %s: %s", cli_path, e)
        return FlashResult(False, f"Не вдалось запустити {cli_path.name}: {e}")

    output = (result.stdout or "") + (result.stderr or "")
    success = result.returncode == 0 and "verified successfully" in output.lower()
    flog.info("DFU: flash verdict: success=%s (exit=%d)", success, result.returncode)
    if success:
        return FlashResult(True, "Прошивку FC записано й перевірено.")
    return FlashResult(False, _extract_error(output) or f"Прошивка не підтверджена (exit code {result.returncode}).")


def reset_to_app(cli_path: Path, port_id: str, timeout_s: float = RESET_TIMEOUT_S) -> bool:
    """Reset the device out of DFU mode back into normal firmware
    execution. Call ONLY after flash_and_verify() has reported success -
    see fc_flash_station.py."""
    flog = get_logger()
    flog.info("DFU: --- resetting FC: port=%s ---", port_id)
    try:
        result = _run(cli_path, ["-c", f"port={port_id}", "-rst"], timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        flog.error("DFU: reset failed to run: %s", e)
        return False
    ok = result.returncode == 0
    flog.info("DFU: reset verdict: success=%s (exit=%d)", ok, result.returncode)
    return ok


def _extract_error(output: str) -> Optional[str]:
    for line in output.splitlines():
        if "error" in line.lower():
            return line.strip()
    return None
