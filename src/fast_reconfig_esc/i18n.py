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
    "connected_fc_info": {"en": "FC: MSP API {api}, {variant}", "uk": "FC: MSP API {api}, {variant}"},
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
    "config_bytes_wrong_length": {
        "en": "Config data is the wrong size: expected {expected} bytes, got {actual}.",
        "uk": "Невірний розмір даних конфігу: очікувалось {expected} байт, отримано {actual}.",
    },
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
    "log_file_location": {"en": "Technical log file: {path}", "uk": "Файл технічних логів: {path}"},
    "skipped_matching": {
        "en": "Skipping {n} ESC(s) that already have this exact config - no rewrite needed.",
        "uk": "Пропускаю {n} ESC, які вже мають саме цей конфіг — перезапис не потрібен.",
    },
    # ---- tabs ----
    "tab_main": {"en": "Main", "uk": "Головна"},
    "tab_config": {"en": "Config", "uk": "Конфіг"},
    "tab_hotspot": {"en": "Hotspot", "uk": "Хотспот"},
    "tab_hotspot_multi": {"en": "Hotspot (Line)", "uk": "Хотспот (лінія)"},
    # ---- config editor: field groups ----
    "group_essentials": {"en": "Essentials", "uk": "Основне"},
    "group_motor": {"en": "Motor", "uk": "Мотор"},
    "group_extended": {"en": "Extended settings", "uk": "Розширені налаштування"},
    "group_limits": {"en": "Limits", "uk": "Обмеження"},
    "group_current_control": {"en": "Current control", "uk": "Керування струмом"},
    "group_sinusoidal": {"en": "Sinusoidal Startup", "uk": "Синусоїдальний старт"},
    "group_brake": {"en": "Brake", "uk": "Гальмо"},
    "group_servo": {"en": "Servo settings", "uk": "Налаштування серво"},
    # ---- config editor: field labels ----
    "field_protocol": {"en": "Protocol", "uk": "Протокол"},
    "field_disable_stick_calibration": {"en": "Disable stick calibration", "uk": "Вимкнути калібрування стіків"},
    "field_timing_advance": {"en": "Timing advance", "uk": "Випередження таймінгу"},
    "field_startup_power": {"en": "Startup power", "uk": "Стартова потужність"},
    "field_motor_kv": {"en": "Motor KV", "uk": "KV мотора"},
    "field_motor_poles": {"en": "Motor poles", "uk": "Кількість полюсів"},
    "field_beep_volume": {"en": "Beeper volume", "uk": "Гучність зумера"},
    "field_pwm_frequency": {"en": "PWM Frequency", "uk": "Частота ШІМ"},
    "field_stuck_rotor_protection": {"en": "Stuck rotor protection", "uk": "Захист від заклинювання ротора"},
    "field_stall_protection": {"en": "Stall protection", "uk": "Захист від зупинки"},
    "field_use_hall_sensors": {"en": "Use hall sensors", "uk": "Використовувати датчики Холла"},
    "field_interval_telemetry": {"en": "30ms interval telemetry", "uk": "Телеметрія кожні 30мс"},
    "field_variable_pwm": {"en": "Variable PWM", "uk": "Змінний ШІМ"},
    "field_complementary_pwm": {"en": "Complementary PWM", "uk": "Комплементарний ШІМ"},
    "field_auto_advance": {"en": "Auto timing advance", "uk": "Автовипередження таймінгу"},
    "field_reversed": {"en": "Reversed", "uk": "Реверс"},
    "field_3d_mode": {"en": "3D mode", "uk": "3D-режим"},
    "field_ramp_rate": {"en": "Ramp rate", "uk": "Швидкість наростання"},
    "field_minimum_duty_cycle": {"en": "Minimum duty cycle", "uk": "Мінімальний робочий цикл"},
    "field_temperature_limit": {"en": "Temperature limit", "uk": "Ліміт температури"},
    "field_current_limit": {"en": "Current limit", "uk": "Ліміт струму"},
    "field_low_voltage_cutoff": {"en": "Low voltage cutoff mode", "uk": "Режим відсічки низької напруги"},
    "field_low_voltage_threshold": {"en": "Low voltage cut off threshold", "uk": "Поріг відсічки низької напруги"},
    "field_absolute_voltage_cutoff": {"en": "Absolute voltage cutoff", "uk": "Абсолютна відсічка напруги"},
    "field_current_p": {"en": "Current P", "uk": "Струм P"},
    "field_current_i": {"en": "Current I", "uk": "Струм I"},
    "field_current_d": {"en": "Current D", "uk": "Струм D"},
    "field_sinusoidal_startup": {"en": "Sinusoidal startup", "uk": "Синусоїдальний старт"},
    "field_sine_mode_range": {"en": "Sine Mode Range", "uk": "Діапазон синус-режиму"},
    "field_sine_mode_power": {"en": "Sine Mode Power", "uk": "Потужність синус-режиму"},
    "field_brake_on_stop": {"en": "Brake on stop", "uk": "Гальмо при зупинці"},
    "field_rc_car_reversing": {"en": "Car type reverse braking", "uk": "Реверсивне гальмо (авто-тип)"},
    "field_brake_strength": {"en": "Brake strength", "uk": "Сила гальма"},
    "field_running_brake_level": {"en": "Running brake level", "uk": "Рівень гальма на ходу"},
    "field_active_brake_power": {"en": "Active brake power", "uk": "Потужність активного гальма"},
    "field_servo_low_threshold": {"en": "Servo low threshold", "uk": "Нижній поріг серво"},
    "field_servo_high_threshold": {"en": "Servo high threshold", "uk": "Верхній поріг серво"},
    "field_servo_neutral": {"en": "Servo neutral", "uk": "Нейтраль серво"},
    "field_servo_dead_band": {"en": "Servo dead band", "uk": "Мертва зона серво"},
    # ---- config editor: chrome ----
    "config_no_data_loaded": {
        "en": "No config loaded yet - use \"Load from file\" or \"Read from ESC\" below.",
        "uk": "Конфіг ще не завантажено — скористайтесь \"Завантажити з файлу\" або \"Прочитати з ESC\" нижче.",
    },
    "config_load_file": {"en": "Load from file...", "uk": "Завантажити з файлу..."},
    "config_read_from_esc": {"en": "Read", "uk": "Прочитати"},
    "config_read_esc_placeholder": {"en": "Select ESC...", "uk": "Оберіть ESC..."},
    "config_save_to_file": {"en": "Save to file...", "uk": "Зберегти у файл..."},
    "config_apply_to_esc": {"en": "Apply to ESC(s)", "uk": "Записати на ESC"},
    "config_disabled_checkbox": {"en": "Disabled", "uk": "Вимкнено"},
    "config_working_from": {"en": "Editing: {source}", "uk": "Редагується: {source}"},
    "config_working_from_file": {"en": "file {path}", "uk": "файл {path}"},
    "config_working_from_esc": {"en": "ESC #{n}", "uk": "ESC №{n}"},
    "config_working_unsaved": {"en": "(unsaved edits)", "uk": "(незбережені зміни)"},
    "config_no_esc_to_read": {"en": "No scanned ESC available to read from yet.", "uk": "Ще немає сканованого ESC, з якого можна прочитати."},
    "config_saved_to_file": {"en": "Config saved to {path}", "uk": "Конфіг збережено у {path}"},
    "config_save_failed": {"en": "Failed to save config: {error}", "uk": "Не вдалося зберегти конфіг: {error}"},
    "config_apply_no_config": {"en": "No config in the editor to apply yet.", "uk": "В редакторі ще немає конфігу для запису."},
    "config_diff_no_changes": {"en": "No changes.", "uk": "Змін не було."},
    "config_diff_line": {"en": "{label}: {old} -> {new}", "uk": "{label}: {old} -> {new}"},
    "config_save_summary_title": {"en": "Config saved", "uk": "Конфіг збережено"},
    "config_save_summary_body": {"en": "Saved to {path}\n\nChanges:\n{diff}", "uk": "Збережено у {path}\n\nЗмінено:\n{diff}"},
    "config_apply_confirm_title": {"en": "Confirm write to ESC", "uk": "Підтвердження запису на ESC"},
    "config_apply_confirm_body": {
        "en": "The following will be written to the ESC(s):\n\n{diff}\n\nContinue?",
        "uk": "На ESC буде записано такі зміни:\n\n{diff}\n\nПродовжити?",
    },
    "config_apply_confirm_yes": {"en": "Write", "uk": "Записати"},
    "config_apply_confirm_cancel": {"en": "Cancel", "uk": "Скасувати"},
    "config_exit_unsaved_title": {"en": "Unsaved config changes", "uk": "Незбережені зміни конфігу"},
    "config_exit_unsaved_body": {
        "en": "The config editor has unsaved changes:\n\n{diff}\n\nSave before exiting?",
        "uk": "У редакторі конфігу є незбережені зміни:\n\n{diff}\n\nЗберегти перед виходом?",
    },
    "dialog_save": {"en": "Save", "uk": "Зберегти"},
    "dialog_discard": {"en": "Discard", "uk": "Не зберігати"},
    "dialog_cancel": {"en": "Cancel", "uk": "Скасувати"},
    # ---- comparison list ----
    "comparison_title": {"en": "Config comparison", "uk": "Порівняння конфігу"},
    "comparison_column_index": {"en": "#", "uk": "#"},
    "comparison_column_esc": {"en": "ESC", "uk": "ESC"},
    "comparison_column_status": {"en": "Status", "uk": "Статус"},
    "comparison_status_not_scanned": {"en": "Not scanned", "uk": "Не скановано"},
    # ---- hotspot tab ----
    "hotspot_load_config": {"en": "Load config...", "uk": "Завантажити конфіг..."},
    "hotspot_no_config": {"en": "No config loaded", "uk": "Конфіг не завантажено"},
    "hotspot_config_loaded": {"en": "Config: {path}", "uk": "Конфіг: {path}"},
    "hotspot_port_label": {"en": "Watch port:", "uk": "Слідкувати за портом:"},
    "hotspot_esc_label": {"en": "ESC:", "uk": "ESC:"},
    "hotspot_start": {"en": "Start", "uk": "Старт"},
    "hotspot_stop": {"en": "Stop", "uk": "Стоп"},
    "hotspot_retry": {"en": "Try again", "uk": "Спробувати ще раз"},
    "hotspot_reset_counters": {"en": "Reset", "uk": "Скинути"},
    "hotspot_counters": {
        "en": "Flashed: {flashed} - Already OK: {ok} - Errors: {errors}",
        "uk": "Прошито: {flashed} - Вже коректно: {ok} - Помилок: {errors}",
    },
    "hotspot_start_disabled_no_config": {
        "en": "Load a config file first",
        "uk": "Спочатку завантажте файл конфігу",
    },
    "hotspot_start_disabled_no_port": {"en": "Select a port first", "uk": "Спочатку оберіть порт"},
    "hotspot_start_disabled_manual": {
        "en": "Disconnect on the Main tab first",
        "uk": "Спочатку відключіться на Головній вкладці",
    },
    "hotspot_stop_disabled_flashing": {
        "en": "Wait for the write to finish",
        "uk": "Зачекайте, завершується прошивка",
    },
    "hotspot_manual_disabled_hotspot_active": {
        "en": "Hotspot mode is active on the Hotspot tab",
        "uk": "Активний режим Хотспот на вкладці Хотспот",
    },
    "hotspot_banner_idle": {"en": "Not started", "uk": "Не запущено"},
    "hotspot_banner_waiting_device": {"en": "Waiting for an ESC...", "uk": "Очікуємо ESC..."},
    "hotspot_banner_connecting": {"en": "Connecting...", "uk": "Підключення..."},
    "hotspot_banner_checking": {"en": "Checking configuration...", "uk": "Перевірка конфігурації..."},
    "hotspot_banner_watching_esc": {
        "en": "Waiting for an ESC on the selected output...",
        "uk": "Очікуємо ESC на обраному виході...",
    },
    "hotspot_banner_flashing": {
        "en": "Flashing - DO NOT UNPLUG",
        "uk": "Прошивається - НЕ ВІДКЛЮЧАЙТЕ",
    },
    "hotspot_banner_done": {"en": "Done! Safe to unplug", "uk": "Готово! Можна відключати"},
    "hotspot_banner_warning_not_found": {
        "en": "No ESCs found - check the wiring",
        "uk": "ESC не знайдено - перевірте підключення",
    },
    "hotspot_banner_error_connect": {
        "en": "Could not connect - check the cable",
        "uk": "Не вдалося підключитися - перевірте кабель",
    },
    "hotspot_banner_error_flash": {
        "en": "Write error - DO NOT UNPLUG, call a technician",
        "uk": "Помилка запису - НЕ ВІДКЛЮЧАЙТЕ, покличте техніка",
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
