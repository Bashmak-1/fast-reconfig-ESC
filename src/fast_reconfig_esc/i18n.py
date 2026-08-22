"""Minimal i18n layer: English and Ukrainian only.

A plain module-level dict + a tiny Qt signal for "language changed" so
widgets can retranslate themselves in place, without a Qt Linguist / .ts
pipeline - overkill for a two-language tool like this one.
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

# (code, flag emoji, short label shown in the switcher, full name)
LANGUAGES: list[tuple[str, str, str, str]] = [
    ("en", "🇬🇧", "En", "English"),
    ("uk", "🇺🇦", "Ua", "Українська"),
]

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "window_title": {"en": "AM32 Multi-ESC Flasher", "uk": "AM32 Мульти-ESC Прошивальник"},
    "port_label": {"en": "Port:", "uk": "Порт:"},
    "baud_label": {"en": "Baud:", "uk": "Бод:"},
    "connect": {"en": "Connect", "uk": "Підключити"},
    "disconnect": {"en": "Disconnect", "uk": "Відключити"},
    "refresh_tooltip": {"en": "Refresh port list", "uk": "Оновити список портів"},
    "config_label": {"en": "Config:", "uk": "Конфіг:"},
    "config_placeholder": {"en": "No config file selected...", "uk": "Файл конфігу не обрано..."},
    "browse": {"en": "Browse...", "uk": "Огляд..."},
    "auto_flash_mode": {"en": "Auto-flash mode", "uk": "Режим автопрошивки"},
    "auto_flash_tooltip": {
        "en": "When enabled, a successful Rescan/Connect immediately flashes the loaded config to every detected ESC.",
        "uk": "Коли увімкнено, успішний Rescan/Connect одразу прошиває завантажений конфіг на кожен знайдений ESC.",
    },
    "esc_retries_label": {"en": "ESC retries:", "uk": "Спроби ESC:"},
    "esc_retries_tooltip": {
        "en": (
            "How many times to retry connecting to one ESC index before marking it red.\n"
            "Lower = faster scans when some outputs have no ESC wired; higher = more\n"
            "tolerant of a slow/flaky ESC that's actually there."
        ),
        "uk": (
            "Скільки разів пробувати підключитись до одного ESC перед тим, як позначити його червоним.\n"
            "Менше = швидший скан, коли до частини виходів нічого не підключено; більше =\n"
            "толерантніше до повільного/нестабільного ESC, який реально є."
        ),
    },
    "rescan": {"en": "Rescan", "uk": "Пересканувати"},
    "flash_all": {"en": "Flash All", "uk": "Прошити всі"},
    "no_port_selected": {"en": "No serial port selected.", "uk": "COM-порт не обрано."},
    "no_config_selected": {"en": "No config file selected.", "uk": "Файл конфігу не обрано."},
    "connecting": {"en": "Connecting to {port} @ {baud}...", "uk": "Підключення до {port} @ {baud}..."},
    "select_config_dialog_title": {"en": "Select ESC config file", "uk": "Оберіть файл конфігу ESC"},
    "binary_config_filter": {
        "en": "Binary config (*.bin);;All files (*)",
        "uk": "Бінарний конфіг (*.bin);;Всі файли (*)",
    },
    "esc_card_title": {"en": "ESC #{n}", "uk": "ESC №{n}"},
    "state_idle": {"en": "Not connected", "uk": "Не підключено"},
    "state_probing": {"en": "Probing...", "uk": "Опитування..."},
    "state_ok": {"en": "OK", "uk": "OK"},
    "state_blank": {"en": "Blank config", "uk": "Порожній конфіг"},
    "state_error": {"en": "Error", "uk": "Помилка"},
    "state_diff": {"en": "Different config", "uk": "Інший конфіг"},
    "config_matches": {"en": "{mcu} — config matches", "uk": "{mcu} — конфіг співпадає"},
    "config_differs": {"en": "{mcu} — different config", "uk": "{mcu} — інший конфіг"},
    "power_cycle_hint": {
        "en": (
            "Getting zero ESCs on repeated scans? Some ESC bootloaders can lock up after "
            "many back-to-back connection attempts - try power-cycling the FC/ESC "
            "(unplug and reconnect power) before scanning again."
        ),
        "uk": (
            "Постійно 0 знайдених ESC при повторних сканах? Деякі бутлоадери ESC можуть "
            "\"зависати\" після багатьох підряд спроб підключення — спробуй перезапустити "
            "живлення FC/ESC (від'єднати й підключити повторно) перед новим сканом."
        ),
    },
    "not_connected_log": {"en": "Not connected.", "uk": "Не підключено."},
    "disconnected_log": {"en": "Disconnected.", "uk": "Відключено."},
    "could_not_open_port": {"en": "Could not open {port}: {error}", "uk": "Не вдалося відкрити {port}: {error}"},
    "connect_failed": {"en": "Connect failed: {error}", "uk": "Не вдалося підключитись: {error}"},
    "connected_report": {
        "en": "Connected. FC reports {count} ESC output(s){cap}.",
        "uk": "Підключено. FC повідомляє про {count} вихід(ів) ESC{cap}.",
    },
    "capped_suffix": {"en": " (capped to {max})", "uk": " (обмежено до {max})"},
    "fourway_exit_retry": {
        "en": "{error}, trying to exit fourway and try again.",
        "uk": "{error}, пробую вийти з fourway і спробувати ще раз.",
    },
    "discovery_summary": {"en": "Discovery: {found}/{expected} found", "uk": "Пошук: знайдено {found}/{expected}"},
    "esc_probe_error": {"en": "ESC #{n}: {error}", "uk": "ESC №{n}: {error}"},
    "flash_summary": {
        "en": "Flash: {success}/{attempted} succeeded",
        "uk": "Прошивка: успішно {success}/{attempted}",
    },
    "esc_flash_error": {"en": "ESC #{n} flash failed: {error}", "uk": "ESC №{n} помилка прошивки: {error}"},
    "flashed_verified": {"en": "flashed + verified", "uk": "прошито + перевірено"},
    "writing": {"en": "writing...", "uk": "запис..."},
    "config_file_error": {"en": "Config file error: {error}", "uk": "Помилка файлу конфігу: {error}"},
    "retries_set": {"en": "ESC connect retries set to {value}", "uk": "Кількість спроб ESC встановлено на {value}"},
    "info_mcu": {"en": "MCU: {value}", "uk": "MCU: {value}"},
    "info_firmware": {"en": "Firmware: {value}", "uk": "Прошивка: {value}"},
    "info_bootloader": {"en": "Bootloader: {value}", "uk": "Бутлоадер: {value}"},
    "info_eeprom": {"en": "EEPROM layout: v{value}", "uk": "Версія EEPROM: v{value}"},
    "info_reversed": {"en": "Reversed: {value}", "uk": "Реверс: {value}"},
    "info_3d_mode": {"en": "3D mode: {value}", "uk": "3D-режим: {value}"},
    "yes": {"en": "Yes", "uk": "Так"},
    "no": {"en": "No", "uk": "Ні"},
    "remembered_config_missing": {
        "en": "Remembered config file no longer exists: {path}",
        "uk": "Запам'ятований файл конфігу більше не існує: {path}",
    },
}

_current_language = "uk"


class _I18nSignals(QObject):
    language_changed = pyqtSignal(str)


signals = _I18nSignals()


def set_language(code: str) -> None:
    global _current_language
    if code not in {c for c, *_ in LANGUAGES}:
        raise ValueError(f"unsupported language: {code}")
    if code == _current_language:
        return
    _current_language = code
    signals.language_changed.emit(code)


def current_language() -> str:
    return _current_language


def tr(key: str, **kwargs) -> str:
    entry = _TRANSLATIONS.get(key)
    if entry is None:
        return key
    text = entry.get(_current_language) or entry.get("en") or key
    return text.format(**kwargs) if kwargs else text
