#!/usr/bin/env python3
"""
File Organizer — сортировка файлов по типу
Установка зависимости: pip install customtkinter
Запуск: python file_organizer.py
"""
from __future__ import annotations

import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ─── Группы файлов ─────────────────────────────────────────────────────────
GROUPS: list[dict] = [
    {"name": "Torrent", "folder": "Torrent", "icon": "⬇",
     "exts": [".torrent"]},
    {"name": "PDF",     "folder": "PDF",     "icon": "📄",
     "exts": [".pdf"]},
    {"name": "Images",  "folder": "Images",  "icon": "🖼",
     "exts": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]},
    {"name": "Office",  "folder": "Office",  "icon": "📊",
     "exts": [".docx", ".xlsx", ".pptx", ".doc", ".xls", ".csv"]},
    {"name": "EXE",     "folder": "EXE",     "icon": "⚙",
     "exts": [".exe", ".msi"]},
    {"name": "Video",   "folder": "Video",   "icon": "🎬",
     "exts": [".mp4", ".avi", ".mkv", ".mov", ".wmv"]},
    {"name": "TXT",     "folder": "TXT",     "icon": "📝",
     "exts": [".txt", ".log", ".md"]},
    {"name": "Archive", "folder": "Archive", "icon": "🗜",
     "exts": [".zip", ".rar", ".7z", ".tar", ".gz"]},
    {"name": "Audio",   "folder": "Audio",   "icon": "🎵",
     "exts": [".mp3", ".wav", ".flac", ".aac", ".ogg"]},
    {"name": "Code",    "folder": "Code",    "icon": "💻",
     "exts": [".py", ".js", ".ts", ".html", ".css", ".json"]},
]

# ─── Цвета ─────────────────────────────────────────────────────────────────
C_HEADER_BG  = "#0f172a"
C_HEADER_ACC = "#60a5fa"
C_HEADER_SUB = "#94a3b8"
C_GREEN      = "#16a34a"
C_GREEN_HOV  = "#15803d"
C_TILE_DARK  = "#1e293b"
C_TILE_CHECK = "#334155"


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("File Organizer")
        self.geometry("940x720")
        self.minsize(800, 600)
        self.iconbitmap()  # убираем дефолтную иконку tkinter

        # Состояние
        self.folder_var    = tk.StringVar(value="")
        self.status_var    = tk.StringVar(value="Выберите папку для начала работы")
        self.select_all_var = tk.BooleanVar(value=False)
        self.group_vars:   dict[str, tk.BooleanVar] = {}
        self.ext_vars:     dict[str, dict[str, tk.BooleanVar]] = {}

        for g in GROUPS:
            self.group_vars[g["name"]] = tk.BooleanVar(value=False)
            self.ext_vars[g["name"]]   = {e: tk.BooleanVar(value=False)
                                           for e in g["exts"]}
        self._build()

    # ── Построение UI ───────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_body()
        self._build_footer()

    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, corner_radius=0, height=80,
                           fg_color=C_HEADER_BG)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(hdr,
                     text="⚡  File Organizer",
                     font=ctk.CTkFont("Segoe UI", 26, "bold"),
                     text_color=C_HEADER_ACC
                     ).grid(row=0, column=0, pady=(12, 0))

        ctk.CTkLabel(hdr,
                     text="Автоматическая сортировка файлов по категориям в папку DEL",
                     font=ctk.CTkFont("Segoe UI", 11),
                     text_color=C_HEADER_SUB
                     ).grid(row=1, column=0, pady=(2, 12))

    def _build_body(self) -> None:
        body = ctk.CTkScrollableFrame(self, corner_radius=0,
                                      fg_color=("gray93", "#111827"))
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure((0, 1, 2), weight=1)

        self._folder_card(body)
        self._types_card(body)

    def _build_footer(self) -> None:
        ftr = ctk.CTkFrame(self, corner_radius=0, height=112,
                           fg_color=("gray86", "#1e293b"))
        ftr.grid(row=2, column=0, sticky="ew")
        ftr.grid_propagate(False)
        ftr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ftr,
                     textvariable=self.status_var,
                     font=ctk.CTkFont("Segoe UI", 11),
                     text_color=("gray40", "#94a3b8"),
                     anchor="w"
                     ).grid(row=0, column=0, padx=20, pady=(10, 2), sticky="w")

        self.pbar = ctk.CTkProgressBar(ftr, height=8)
        self.pbar.grid(row=1, column=0, padx=20, pady=(0, 8), sticky="ew")
        self.pbar.set(0)

        btn_row = ctk.CTkFrame(ftr, fg_color="transparent")
        btn_row.grid(row=2, column=0, padx=16, pady=(0, 12), sticky="e")

        ctk.CTkButton(btn_row,
                      text="📁  Выбрать папку",
                      width=170, height=40,
                      font=ctk.CTkFont("Segoe UI", 13),
                      command=self._pick_folder
                      ).pack(side="left", padx=(0, 10))

        self.move_btn = ctk.CTkButton(
            btn_row,
            text="▶  Переместить файлы",
            width=200, height=40,
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            fg_color=C_GREEN,
            hover_color=C_GREEN_HOV,
            command=self._confirm_move)
        self.move_btn.pack(side="left")

    # ── Карточка: выбор папки ───────────────────────────────────────────────

    def _folder_card(self, parent: ctk.CTkScrollableFrame) -> None:
        card = ctk.CTkFrame(parent, corner_radius=14,
                            fg_color=("white", C_TILE_DARK))
        card.grid(row=0, column=0, columnspan=3,
                  sticky="ew", padx=16, pady=(16, 8))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card,
                     text="📁  Папка для обработки",
                     font=ctk.CTkFont("Segoe UI", 13, "bold"),
                     anchor="w"
                     ).grid(row=0, column=0, padx=18, pady=(14, 6), sticky="w")

        box = ctk.CTkFrame(card, corner_radius=8,
                           fg_color=("gray88", "#0f172a"))
        box.grid(row=1, column=0, padx=18, pady=(0, 16), sticky="ew")
        box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(box,
                     textvariable=self.folder_var,
                     font=ctk.CTkFont("Segoe UI", 11),
                     text_color=("gray35", "#94a3b8"),
                     anchor="w",
                     wraplength=750
                     ).grid(row=0, column=0, padx=14, pady=10, sticky="w")

    # ── Карточка: типы файлов ───────────────────────────────────────────────

    def _types_card(self, parent: ctk.CTkScrollableFrame) -> None:
        card = ctk.CTkFrame(parent, corner_radius=14,
                            fg_color=("white", C_TILE_DARK))
        card.grid(row=1, column=0, columnspan=3,
                  sticky="ew", padx=16, pady=(0, 16))
        card.grid_columnconfigure((0, 1, 2), weight=1)

        # Заголовок
        tr = ctk.CTkFrame(card, fg_color="transparent")
        tr.grid(row=0, column=0, columnspan=3,
                padx=18, pady=(14, 6), sticky="ew")
        tr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(tr,
                     text="🗂  Типы файлов",
                     font=ctk.CTkFont("Segoe UI", 13, "bold"),
                     anchor="w"
                     ).grid(row=0, column=0, sticky="w")

        ctk.CTkCheckBox(tr,
                        text="Выбрать все",
                        variable=self.select_all_var,
                        font=ctk.CTkFont("Segoe UI", 12),
                        command=self._toggle_all
                        ).grid(row=0, column=1)

        sep = ctk.CTkFrame(card, height=1, fg_color=("gray75", "#334155"))
        sep.grid(row=1, column=0, columnspan=3,
                 sticky="ew", padx=18, pady=(0, 10))

        # Тайлы 3 в ряд
        for i, g in enumerate(GROUPS):
            self._group_tile(card, g, row=2 + i // 3, col=i % 3)

        # Нижний отступ
        ctk.CTkFrame(card, height=12, fg_color="transparent").grid(
            row=2 + (len(GROUPS) - 1) // 3 + 1, column=0)

    def _group_tile(self, parent: ctk.CTkFrame,
                    g: dict, row: int, col: int) -> None:
        name = g["name"]
        tile = ctk.CTkFrame(parent, corner_radius=10,
                            fg_color=("gray90", "#263548"),
                            border_width=1,
                            border_color=("gray80", "#334155"))
        tile.grid(row=row, column=col, padx=8, pady=6, sticky="nsew")

        # Заголовок группы (кликабельный чекбокс)
        ctk.CTkCheckBox(
            tile,
            text=f"{g['icon']}  {name}",
            variable=self.group_vars[name],
            font=ctk.CTkFont("Segoe UI", 13, "bold"),
            command=lambda n=name: self._toggle_group(n)
        ).grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")

        # Разделитель внутри тайла
        ctk.CTkFrame(tile, height=1,
                     fg_color=("gray78", "#334155")).grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0, 4))

        # Расширения
        for j, ext in enumerate(g["exts"]):
            ctk.CTkCheckBox(
                tile,
                text=ext,
                variable=self.ext_vars[name][ext],
                font=ctk.CTkFont("Segoe UI", 11),
                checkbox_width=16, checkbox_height=16,
                text_color=("gray30", "#94a3b8"),
                command=lambda n=name: self._on_ext_toggle(n)
            ).grid(row=j + 2, column=0,
                   padx=(28, 12), pady=2, sticky="w")

        ctk.CTkFrame(tile, height=10, fg_color="transparent").grid(
            row=len(g["exts"]) + 2, column=0)

    # ── Callbacks ───────────────────────────────────────────────────────────

    def _toggle_group(self, name: str) -> None:
        state = self.group_vars[name].get()
        for v in self.ext_vars[name].values():
            v.set(state)
        self._sync_select_all()

    def _on_ext_toggle(self, name: str) -> None:
        all_on = all(v.get() for v in self.ext_vars[name].values())
        self.group_vars[name].set(all_on)
        self._sync_select_all()

    def _toggle_all(self) -> None:
        state = self.select_all_var.get()
        for name in self.group_vars:
            self.group_vars[name].set(state)
            for v in self.ext_vars[name].values():
                v.set(state)

    def _sync_select_all(self) -> None:
        all_on = all(
            all(v.get() for v in evs.values())
            for evs in self.ext_vars.values()
        )
        self.select_all_var.set(all_on)

    def _pick_folder(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку для сортировки")
        if path:
            self.folder_var.set(path)
            self.status_var.set(f"Папка выбрана: {path}")
            self.pbar.set(0)

    def _confirm_move(self) -> None:
        folder = self.folder_var.get()
        if not folder:
            messagebox.showwarning("Нет папки", "Сначала выберите папку!")
            return

        ext_map = self._build_ext_map()
        if not ext_map:
            messagebox.showwarning("Нет типов",
                                   "Выберите хотя бы один тип файлов!")
            return

        # Считаем сколько файлов попадёт под перемещение
        count = sum(
            1 for f in Path(folder).iterdir()
            if f.is_file() and f.suffix.lower() in ext_map
        )
        if count == 0:
            messagebox.showinfo("Нечего перемещать",
                                "В выбранной папке нет файлов "
                                "выбранных типов.")
            return

        if messagebox.askyesno(
            "Подтверждение",
            f"Найдено файлов: {count}\n\n"
            f"Переместить в:\n{folder}\\DEL\\<категория>\\",
            icon="question"
        ):
            self._run_move(folder, ext_map)

    def _build_ext_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for g in GROUPS:
            for ext, var in self.ext_vars[g["name"]].items():
                if var.get():
                    result[ext] = g["folder"]
        return result

    def _run_move(self, folder: str, ext_map: dict[str, str]) -> None:
        self.move_btn.configure(state="disabled")
        self.pbar.configure(mode="indeterminate")
        self.pbar.start()
        self.status_var.set("Перемещение файлов…")
        threading.Thread(
            target=self._worker,
            args=(folder, ext_map),
            daemon=True
        ).start()

    def _worker(self, folder: str, ext_map: dict[str, str]) -> None:
        moved, errors = 0, 0
        del_root = Path(folder) / "DEL"
        try:
            files = [
                f for f in Path(folder).iterdir()
                if f.is_file() and f.suffix.lower() in ext_map
            ]
            for file in files:
                dest_dir = del_root / ext_map[file.suffix.lower()]
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / file.name

                # Разрешение конфликтов имён
                if dest.exists():
                    stem, suf, n = file.stem, file.suffix, 1
                    while dest.exists():
                        dest = dest_dir / f"{stem}_{n}{suf}"
                        n += 1
                try:
                    shutil.move(str(file), str(dest))
                    moved += 1
                    self.after(0, self.status_var.set,
                               f"Перемещено: {moved}…")
                except Exception:
                    errors += 1
        except Exception as exc:
            self.after(0, self._done, 0, 0, str(exc))
            return

        self.after(0, self._done, moved, errors, None)

    def _done(self, moved: int, errors: int,
              err: Optional[str]) -> None:
        self.pbar.stop()
        self.pbar.configure(mode="determinate")
        self.pbar.set(1.0 if not err else 0.0)
        self.move_btn.configure(state="normal")

        if err:
            self.status_var.set(f"Ошибка: {err}")
            messagebox.showerror("Ошибка", err)
        else:
            tail = f"  |  Ошибок: {errors}" if errors else ""
            self.status_var.set(f"Готово! Перемещено: {moved} файл(ов){tail}")
            messagebox.showinfo(
                "Готово ✓",
                f"Перемещено: {moved} файл(ов)"
                + (f"\nОшибок:     {errors}" if errors else "")
            )


if __name__ == "__main__":
    App().mainloop()
