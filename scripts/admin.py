"""EscHotlineAdmin: edit the list of users allowed through the launcher.

Local helper, never published. Type one name per line; "Зберегти" writes
  users.names.txt  (the names, so you can edit them later)  and
  users.sha256     (their hashes; put it next to EscHotlineLauncher.exe)
into the folder of this exe (or private/ when run from source). The right pane
shows today's password for each name (name + MMDD). See src/esc_hotline/session_gate.py.
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from esc_hotline import session_gate as gate  # noqa: E402

OUT_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else HERE.parent / "private"
NAMES = OUT_DIR / "users.names.txt"
HASHES = OUT_DIR / "users.sha256"


def read_names(text: str) -> list[str]:
    seen: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip():
            seen.setdefault(line.strip().casefold(), line.strip())
    return list(seen.values())


def passwords(names: list[str]) -> str:
    return "\n".join(
        f"{n} -> {gate.password_for(n)}" if gate.is_valid_name(n) else f"{n} -> ! лише латиниця й цифри" for n in names
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.title("EscHotline — користувачі")
    tk.Label(root, text="Імена (по одному на рядок)", padx=8).grid(row=0, column=0, sticky="w")
    tk.Label(root, text="Пароль на сьогодні", padx=8).grid(row=0, column=1, sticky="w")
    box = tk.Text(root, width=34, height=16)
    box.grid(row=1, column=0, padx=8, pady=4)
    shown = tk.Text(root, width=34, height=16, state="disabled", bg="#f0f0f0")
    shown.grid(row=1, column=1, padx=8, pady=4)
    if NAMES.is_file():
        box.insert("1.0", NAMES.read_text(encoding="utf-8"))

    def refresh(_event=None) -> None:
        shown.config(state="normal")
        shown.delete("1.0", "end")
        shown.insert("1.0", passwords(read_names(box.get("1.0", "end"))))
        shown.config(state="disabled")

    def save() -> None:
        names = read_names(box.get("1.0", "end"))
        bad = [n for n in names if not gate.is_valid_name(n)]
        if bad:
            messagebox.showwarning("Не збережено", "Лише латиниця й цифри, без пробілів і спецсимволів:\n" + "\n".join(bad))
            return
        NAMES.write_text("\n".join(names) + "\n", encoding="utf-8")
        HASHES.write_text("\n".join(gate.name_hash(n) for n in names) + "\n", encoding="utf-8")
        messagebox.showinfo("Збережено", f"{len(names)} користувачів.\n{HASHES}")

    box.bind("<KeyRelease>", refresh)
    tk.Button(root, text="Зберегти", width=16, command=save).grid(row=2, column=0, columnspan=2, pady=8)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
