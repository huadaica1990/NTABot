"""
GUI - Main application window with all tabs.
"""
import subprocess
import threading
import time
import json
import os
import io
import math
import copy
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from typing import Optional, List, Callable
from datetime import datetime

from config import (
    BotConfig, BuildConfig, AttackPoint, WaveGroup, State,
    C, STATE_COLOR, GRID_COLS, GRID_ROWS,
    BASE_DIR, ASSETS_DIR, DEBUG_DIR, CONFIG_DIR, CONFIG_PATH,
    DEFAULT_ADB, HAS_CV2, HAS_PIL, _WAVE_ADB_LOCK, ADB_LOCK,
)
from adb_controller import ADB
from ollama_vision import OllamaVision
from detector import Detector
from brain import TroopBrain
from troop_selector import TroopSelector
from bot_engine import BotEngine
from build_engine import BuildEngine
from wave_engine import WaveGroupEngine
from scout_engine import ScoutEngine
from spin_engine import SpinEngine
from grid_picker import GridPicker
from tile_ocr import analyze_tile_image
from telegram_bot import TelegramBot

if HAS_CV2:
    import cv2
    import numpy as np

if HAS_PIL:
    from PIL import Image, ImageTk


class GUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Nine Acres  —  Grid Attack Bot")
        self.configure(bg=C["bg"])
        self.geometry("920x740")
        self.minsize(720, 600)

        self._current_profile = "default"

        self.cfg  = self._load_cfg()
        self._autosave_after_id = None
        self._tree_refresh_after_id = None
        self._build_refresh_after_id = None
        self.bot  = BotEngine(self.cfg, self._log)
        self.bot.on_refresh = self._schedule_refresh_tree
        self.bot.get_pending_points = lambda: [p for p in self.pts if p.status == "waiting"]
        self.pts: List[AttackPoint] = []
        self._in_city = threading.Event()
        self.build_cfg = BuildConfig()
        self.build_eng = BuildEngine(self.bot.adb, self._log, self.build_cfg, det=self.bot.det)
        self.build_eng.in_city = self._in_city
        self.bot.in_city       = self._in_city
        self._raw: Optional[bytes] = None
        self._adb_w = self.cfg.screen_w
        self._adb_h = self.cfg.screen_h
        self.wave_groups: List[WaveGroup] = []
        self._wave_engines: List[WaveGroupEngine] = []
        self.spin_eng = SpinEngine(self.bot.adb, self._log, det=self.bot.det)
        self.spin_eng.in_city = self._in_city
        self.tg_bot = TelegramBot(self.cfg.telegram_token, self.cfg.telegram_chat_id, self._log)
        self.tg_bot.on_command = self._telegram_on_command
        self.tg_bot.on_tap = self._telegram_on_tap

        self.bot.on_state = self._on_state
        self.bot.on_captcha = self._stop_all_captcha
        self.bot.on_error = lambda msg: self.after(0, lambda: self._tg_notify_error("Tab Tấn công", msg))
        self._build()
        self._style()
        self._tick()
        # Callback cap nhat Heal X/Y tren UI khi bot thay doi
        def _sync_heal_ui():
            try:
                self.e_heal_x.delete(0, "end"); self.e_heal_x.insert(0, str(self.cfg.heal_x))
                self.e_heal_y.delete(0, "end"); self.e_heal_y.insert(0, str(self.cfg.heal_y))
            except Exception: pass
        self.bot.on_heal_update = lambda: self.after(0, _sync_heal_ui)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Phím tắt Ctrl+S = Save all
        self.bind_all("<Control-s>", lambda e: self._hotkey_save())
        # Load scout config + map sau khi UI đã sẵn sàng
        self.after(200, self._startup_load_scout)
        # Auto-start Telegram nếu đã cấu hình
        if self.cfg.telegram_enabled and self.cfg.telegram_token:
            self.after(500, self._telegram_start)

    def _startup_load_scout(self):
        """Load scout cfg + map từ file default khi khởi động."""
        try:
            import json as _json
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    d = _json.load(f)
                if "_scout_cfg" in d:
                    sc = d["_scout_cfg"]
                    for sv, key in [(self._sv_ox,"ox"),(self._sv_oy,"oy"),(self._sv_up,"up"),
                                    (self._sv_down,"down"),(self._sv_left,"left"),(self._sv_right,"right")]:
                        if sc.get(key): sv.set(sc[key])
        except Exception:
            pass
        self._scout_load_map()
        self._spy_load()

    def _on_close(self):
        """Luu truoc khi thoat."""
        try: self._autosave()
        except Exception: pass
        try: self._spy_save()   # Lưu spy data độc lập khi đóng app
        except Exception: pass
        try: self.tg_bot.stop()
        except Exception: pass
        self.destroy()

    def _update_title(self, name: str = ""):
        """Cập nhật title cửa sổ theo profile."""
        name = name or self._current_profile or "default"
        self._current_profile = name
        if name == "default":
            self.title("Nine Acres  —  Grid Attack Bot")
        else:
            self.title(f"Nine Acres  —  [{name}]")
        # Sync Telegram device label
        if hasattr(self, 'tg_bot'):
            self.tg_bot.device_label = f"{name} @ {self.cfg.device_serial}"

    # ── Config ─────────────────────────────────

    def _profile_path(self, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        return os.path.join(CONFIG_DIR, f"settings_{safe}.json")

    def _scout_map_path(self, profile_name: str = None) -> str:
        """Trả về đường dẫn file JSON bản đồ theo profile."""
        if profile_name is None:
            profile_name = getattr(self, '_profile_var', None)
            profile_name = profile_name.get().strip() if profile_name else "default"
        safe = "".join(c for c in profile_name if c.isalnum() or c in "-_")
        return os.path.join(CONFIG_DIR, f"scout_map_{safe}.json")

    def _scout_save_map(self):
        """Lưu toàn bộ tile bản đồ ra file riêng theo profile."""
        tiles = getattr(self, "_scout_tiles", None)
        if not tiles:
            return  # Chưa init hoặc rỗng — không ghi đè file
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            path = self._scout_map_path()
            names = getattr(self, "_scout_tile_names", {})
            data = {
                f"{k[0]},{k[1]}": {
                    "label": v,
                    "name": names.get(k, "")
                }
                for k, v in tiles.items()
            }
            # Lưu origin vào _meta để restore khi load
            ox, oy = getattr(self, "_scout_origin", (None, None))
            if ox is not None:
                data["_meta"] = {"ox": ox, "oy": oy}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log(f"[Scout] ⚠️ Lưu bản đồ lỗi: {e}")

    def _scout_load_map(self, profile_name: str = None):
        """Load tile bản đồ từ file riêng theo profile."""
        try:
            path = self._scout_map_path(profile_name)
            self._log(f"[Scout] 📂 Đọc bản đồ: {os.path.basename(path)}")
            if not os.path.exists(path):
                self._log(f"[Scout] ℹ️ Chưa có file bản đồ")
                return
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            # raw có thể là dict tiles hoặc có key "_meta"
            meta = raw.pop("_meta", {}) if isinstance(raw, dict) else {}
            data = raw
            if not hasattr(self, "_scout_tiles"):
                self._scout_tiles = {}
            else:
                self._scout_tiles.clear()
            if not hasattr(self, "_scout_tile_names"):
                self._scout_tile_names = {}
            else:
                self._scout_tile_names.clear()
            for k, v in data.items():
                parts = k.split(",")
                if len(parts) != 2: continue
                coord = (int(parts[0]), int(parts[1]))
                # Tương thích ngược: v có thể là string (cũ) hoặc dict (mới)
                if isinstance(v, dict):
                    self._scout_tiles[coord] = v.get("label", "?")
                    name = v.get("name", "")
                    if name:
                        self._scout_tile_names[coord] = name
                else:
                    self._scout_tiles[coord] = str(v)
            # Khôi phục origin nếu có trong meta
            if meta.get("ox") and meta.get("oy"):
                self._scout_origin = (int(meta["ox"]), int(meta["oy"]))
            if hasattr(self, "_scout_draw_map"):
                self._scout_draw_map()
            if hasattr(self, "_scout_refresh_table"):
                self._scout_refresh_table()
            self._log(f"[Scout] ✅ Load bản đồ: {len(self._scout_tiles)} ô từ {os.path.basename(path)}")
        except Exception as e:
            self._log(f"[Scout] ⚠️ Load bản đồ lỗi: {e}")

    def _spy_data_path(self, profile_name: str = None) -> str:
        """Đường dẫn file JSON do thám theo profile."""
        if profile_name is None:
            profile_name = getattr(self, "_profile_var", None)
            profile_name = profile_name.get().strip() if profile_name else "default"
        safe = "".join(c for c in profile_name if c.isalnum() or c in "-_")
        return os.path.join(CONFIG_DIR, f"spy_data_{safe}.json")

    def _spy_save(self):
        """Lưu danh sách toạ độ + kết quả do thám ra file riêng."""
        if not getattr(self, "_spy_loaded", False):
            return  # Chưa load xong — không ghi đè file
        coords = getattr(self, "_spy_coords", None)
        if coords is None:
            return
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            path = self._spy_data_path()
            results = getattr(self, "_spy_results", {})
            data = {
                "coords": [[x, y, note] for x, y, note in coords],
                "results": {
                    f"{k[0]},{k[1]}": v
                    for k, v in results.items()
                }
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log(f"[Spy] ⚠️ Lưu dữ liệu lỗi: {e}")

    def _spy_load(self, profile_name: str = None):
        """Load danh sách toạ độ + kết quả do thám từ file."""
        try:
            path = self._spy_data_path(profile_name)
            self._log(f"[Spy] 📂 Đọc: {os.path.basename(path)}")
            if not os.path.exists(path):
                self._log(f"[Spy] ℹ️ Chưa có file do thám")
                self._spy_loaded = True  # Cho phép save coords mới
                return
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not hasattr(self, "_spy_coords"):
                self._spy_coords = []
            else:
                self._spy_coords.clear()
            if not hasattr(self, "_spy_results"):
                self._spy_results = {}
            else:
                self._spy_results.clear()
            for row in data.get("coords", []):
                if len(row) >= 2:
                    self._spy_coords.append([int(row[0]), int(row[1]),
                                              row[2] if len(row) > 2 else ""])
            for k, v in data.get("results", {}).items():
                parts = k.split(",")
                if len(parts) == 2:
                    self._spy_results[(int(parts[0]), int(parts[1]))] = v
            if hasattr(self, "_spy_refresh_tree"):
                self._spy_refresh_tree()
            self._spy_loaded = True
            self._log(f"[Spy] ✅ Load {len(self._spy_coords)} toạ độ, "
                      f"{len(self._spy_results)} kết quả từ {os.path.basename(path)}")
        except Exception as e:
            self._log(f"[Spy] ⚠️ Load dữ liệu lỗi: {e}")

    def _list_profiles(self):
        files = []
        for f in os.listdir(CONFIG_DIR):
            if f.startswith("settings_") and f.endswith(".json"):
                files.append(f[9:-5])  # strip "settings_" and ".json"
        return sorted(files)

    def _load_cfg_from(self, path: str) -> BotConfig:
        cfg = BotConfig()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                for k in vars(cfg):
                    if k in d:
                        try: setattr(cfg, k, d[k])
                        except Exception: pass
            except Exception: pass
        return cfg

    def _save_cfg_to(self, path: str):
        import dataclasses
        d = dataclasses.asdict(self.cfg)
        d["_points"] = [
            {"idx": p.idx, "game_x": p.game_x, "game_y": p.game_y,
             "label": p.label, "status": p.status}
            for p in self.pts
        ]
        d["_build_tasks"] = self.build_cfg.tasks
        # Đồng bộ UI → WaveGroup trước khi lưu
        if hasattr(self, '_wave_group_widgets'):
            self._wave_sync_ui()
        d["_wave_groups"] = [
            {"name": g.name, "troop_names": g.troop_names,
             "points": g.points, "enabled": g.enabled,
             "heal_every": g.heal_every, "heal_step": g.heal_step,
             "heal_x": g.heal_x, "heal_y": g.heal_y,
             "heal_on_done": g.heal_on_done,
             "cooldown_sec": g.cooldown_sec, "lv2_cooldown_sec": g.lv2_cooldown_sec,
             "lv3_cooldown_sec": g.lv3_cooldown_sec, "lv4_cooldown_sec": g.lv4_cooldown_sec}
            for g in self.wave_groups
        ]
        # Scout config (chỉ lưu cấu hình, không lưu tiles - tiles lưu file riêng)
        d["_scout_cfg"] = {
            "ox":    getattr(self, '_sv_ox',    None) and self._sv_ox.get(),
            "oy":    getattr(self, '_sv_oy',    None) and self._sv_oy.get(),
            "up":    getattr(self, '_sv_up',    None) and self._sv_up.get(),
            "down":  getattr(self, '_sv_down',  None) and self._sv_down.get(),
            "left":  getattr(self, '_sv_left',  None) and self._sv_left.get(),
            "right": getattr(self, '_sv_right', None) and self._sv_right.get(),
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        # Lưu tiles bản đồ ra file riêng
        self._scout_save_map()

    def _load_cfg(self) -> BotConfig:
        return self._load_cfg_from(CONFIG_PATH)

    def _save_cfg(self):
        self._save_cfg_to(CONFIG_PATH)

    # ── Build ───────────────────────────────────

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=C["panel"], height=54)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Label(hdr, text="⚔  Nine Acres  —  Grid Attack",
                 font=("Segoe UI", 14, "bold"),
                 bg=C["panel"], fg=C["accent"]).pack(
            side="left", padx=18, pady=14)
        self.lbl_state = tk.Label(hdr, text="● IDLE",
                                   font=("Consolas", 11, "bold"),
                                   bg=C["panel"], fg=C["muted"])
        self.lbl_state.pack(side="right", padx=18)



        # Notebook tabs
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        # Tab 1: Attack
        tab_atk = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_atk, text="⚔  Tấn công")
        self._note_bar(tab_atk)
        paned = ttk.PanedWindow(tab_atk, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left  = tk.Frame(paned, bg=C["bg"])
        right = tk.Frame(paned, bg=C["bg"])
        paned.add(left,  weight=1)
        paned.add(right, weight=3)
        self._build_left(left)
        self._build_right(right)

        # Tab 2: Build
        tab_bld = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_bld, text="🏗  Xây dựng")
        self._note_bar(tab_bld)
        self._build_build_tab(tab_bld)

        # Tab 3: Resources
        tab_res = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_res, text="📦  Tài nguyên")
        self._note_bar(tab_res)
        self._build_resource_tab(tab_res)

        # Tab 4: Wave (multi-group attack)
        tab_wave = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_wave, text="🌊  Nhiều đợt")
        self._note_bar(tab_wave)
        self._build_wave_tab(tab_wave)

        # Tab 5: Map Scout
        tab_scout = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_scout, text="🗺  Dò bản đồ")
        self._note_bar(tab_scout)
        self._build_scout_tab(tab_scout)

        # Tab 6: Spy
        tab_spy = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_spy, text="🔍  Do thám")
        self._note_bar(tab_spy)
        self._build_spy_tab(tab_spy)

        # Tab 7: Auto Update Lv
        tab_autolv = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_autolv, text="⬆  Auto Lv")
        self._note_bar(tab_autolv)
        self._build_autolv_tab(tab_autolv)

        # Tab 8: Quay thuong
        tab_spin = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_spin, text="🎰  Quay thưởng")
        self._note_bar(tab_spin)
        self._build_spin_tab(tab_spin)

        # Tab 9: Debug
        tab_debug = tk.Frame(nb, bg=C["bg"])
        nb.add(tab_debug, text="🐛  Debug")
        self._note_bar(tab_debug)
        self._build_debug_tab(tab_debug)

    # ────────────────────────────────────────────────────────
    # TAB: TÀI NGUYÊN
    # ────────────────────────────────────────────────────────
    _RES_BUILDINGS = [
        "Capital", "Smithy", "Barrack", "Hall of Heroes",
        "Clinic", "Drill Ground", "Barn", "Warehouse",
        "Ting System", "Frontier Garrison", "Embassy", "Union Market",
        "City Wall",
    ]
    _RES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources_data.json")

    def _res_load_file(self):
        try:
            with open(self._RES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return self._res_default_data()

    def _res_save_file(self):
        try:
            with open(self._RES_FILE, "w", encoding="utf-8") as f:
                json.dump(self._res_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log(f"[Res] Lỗi lưu: {e}")

    def _res_default_data(self):
        return {b: [{"wood": 0, "stone": 0, "done": False} for _ in range(20)]
                for b in self._RES_BUILDINGS}

    def _build_resource_tab(self, p):
        self._res_data = self._res_load_file()
        # Ensure all buildings exist
        for b in self._RES_BUILDINGS:
            if b not in self._res_data:
                self._res_data[b] = [{"wood": 0, "stone": 0, "done": False} for _ in range(20)]
        self._res_vars = {}
        self._res_sum_lbls = {}

        # Global sum bar at top
        top = tk.Frame(p, bg=C["panel"], pady=4)
        top.pack(fill="x", padx=4, pady=(4,0))
        tk.Label(top, text="📦 Tổng cần:", bg=C["panel"],
                 fg=C["accent"], font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
        self._res_global_wood = tk.Label(top, text="🪵 0", bg=C["panel"],
                                          fg="#d4a84b", font=("Segoe UI", 10, "bold"))
        self._res_global_wood.pack(side="left", padx=6)
        self._res_global_stone = tk.Label(top, text="🪨 0", bg=C["panel"],
                                           fg="#aaaaaa", font=("Segoe UI", 10, "bold"))
        self._res_global_stone.pack(side="left", padx=6)
        tk.Button(top, text="Uncheck tất cả", bg=C["muted"], fg="white",
                  relief="flat", font=("Segoe UI", 8),
                  command=self._res_reset_all).pack(side="right", padx=8)
        tk.Button(top, text="✅ Check tất cả", bg=C["green"], fg="#111",
                  relief="flat", font=("Segoe UI", 8, "bold"),
                  command=self._res_check_all).pack(side="right", padx=4)

        # Row 2: check current resources + deficit
        row2 = tk.Frame(p, bg=C["card"], pady=4)
        row2.pack(fill="x", padx=4, pady=(0,2))
        tk.Button(row2, text="💰 Check tài nguyên hiện tại",
                  bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                  command=self._res_check_current).pack(side="left", padx=8)
        tk.Label(row2, text="Có:", bg=C["card"], fg=C["muted"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(8,2))
        tk.Label(row2, text="🪵", bg=C["card"], fg="#d4a84b",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self._res_cur_wood_var = tk.StringVar(value="")
        self._res_cur_wood_entry = tk.Entry(row2, textvariable=self._res_cur_wood_var,
                                            width=9, bg=C["entry"], fg="#d4a84b",
                                            insertbackground="#d4a84b", relief="flat",
                                            font=("Consolas", 9, "bold"))
        self._res_cur_wood_entry.pack(side="left", padx=3)
        tk.Label(row2, text="🪨", bg=C["card"], fg="#aaaaaa",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4,0))
        self._res_cur_stone_var = tk.StringVar(value="")
        self._res_cur_stone_entry = tk.Entry(row2, textvariable=self._res_cur_stone_var,
                                             width=9, bg=C["entry"], fg="#aaaaaa",
                                             insertbackground="#aaaaaa", relief="flat",
                                             font=("Consolas", 9, "bold"))
        self._res_cur_stone_entry.pack(side="left", padx=3)
        tk.Button(row2, text="↻", bg=C["muted"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=4, pady=1,
                  command=self._res_recalc_deficit).pack(side="left", padx=2)
        self._res_cur_wood_var.trace_add("write", lambda *_: self.after(100, self._res_recalc_deficit))
        self._res_cur_stone_var.trace_add("write", lambda *_: self.after(100, self._res_recalc_deficit))
        tk.Label(row2, text="  Còn thiếu:", bg=C["card"], fg=C["muted"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(10,2))
        self._res_deficit_wood  = tk.Label(row2, text="🪵 —", bg=C["card"],
                                            fg=C["red"], font=("Segoe UI", 9, "bold"))
        self._res_deficit_wood.pack(side="left", padx=3)
        self._res_deficit_stone = tk.Label(row2, text="🪨 —", bg=C["card"],
                                            fg=C["red"], font=("Segoe UI", 9, "bold"))
        self._res_deficit_stone.pack(side="left", padx=3)
        self._res_check_status = tk.Label(row2, text="", bg=C["card"],
                                           fg=C["muted"], font=("Segoe UI", 8))
        self._res_check_status.pack(side="left", padx=8)

        # Sub-notebook: 1 tab per building
        nb_res = ttk.Notebook(p)
        nb_res.pack(fill="both", expand=True, padx=4, pady=4)

        for i, bld in enumerate(self._RES_BUILDINGS):
            tab = tk.Frame(nb_res, bg=C["panel"])
            nb_res.add(tab, text=f"{i+1}. {bld}")
            self._res_build_section(tab, bld)

        self._res_populate()

    def _res_build_section(self, parent, bld):
        # Scrollable inner
        outer = tk.Frame(parent, bg=C["panel"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["panel"], highlightthickness=0)
        sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C["panel"])
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        # Header
        hdr = tk.Frame(inner, bg=C["card"])
        hdr.pack(fill="x", pady=(0,2))
        for col, w, txt in [(0,6,"Lv"),(1,12,"🪵 Gỗ"),(2,12,"🪨 Đá"),(3,8,"✓ Xong")]:
            tk.Label(hdr, text=txt, bg=C["card"], fg=C["muted"],
                     font=("Segoe UI", 8, "bold"), width=w, anchor="center").grid(row=0, column=col, padx=3, pady=3)

        vars_list = []
        for lv in range(20):
            bg = C["panel"] if lv % 2 == 0 else C["card"]
            row = tk.Frame(inner, bg=bg)
            row.pack(fill="x")
            tk.Label(row, text=f"Lv{lv+1}", bg=bg, fg=C["accent"] if lv < 5 else C["text"],
                     font=("Consolas", 8, "bold"), width=6, anchor="center").grid(row=0, column=0, padx=3, pady=1)
            wv = tk.StringVar(value="0")
            sv = tk.StringVar(value="0")
            dv = tk.BooleanVar(value=False)
            we = tk.Entry(row, textvariable=wv, width=12, bg=C["entry"], fg="#d4a84b",
                          insertbackground="white", relief="flat", font=("Consolas", 8))
            se = tk.Entry(row, textvariable=sv, width=12, bg=C["entry"], fg="#aaaaaa",
                          insertbackground="white", relief="flat", font=("Consolas", 8))
            dc = tk.Checkbutton(row, variable=dv, bg=bg,
                                activebackground=bg, command=lambda b=bld: self._res_on_change(b))
            we.grid(row=0, column=1, padx=3, pady=1)
            se.grid(row=0, column=2, padx=3, pady=1)
            dc.grid(row=0, column=3, padx=3)
            we.bind("<FocusOut>", lambda e, b=bld: self._res_on_change(b))
            se.bind("<FocusOut>", lambda e, b=bld: self._res_on_change(b))
            we.bind("<Return>", lambda e, b=bld: self._res_on_change(b))
            se.bind("<Return>", lambda e, b=bld: self._res_on_change(b))
            vars_list.append((wv, sv, dv))
        self._res_vars[bld] = vars_list

        # Sum row
        sep = tk.Frame(inner, bg=C["accent"], height=1)
        sep.pack(fill="x", pady=3)
        sum_row = tk.Frame(inner, bg=C["card"])
        sum_row.pack(fill="x")
        tk.Label(sum_row, text="Tổng cần:", bg=C["card"], fg=C["accent"],
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=0, padx=8, pady=4)
        wlbl = tk.Label(sum_row, text="🪵 0", bg=C["card"], fg="#d4a84b",
                        font=("Segoe UI", 9, "bold"))
        slbl = tk.Label(sum_row, text="🪨 0", bg=C["card"], fg="#aaaaaa",
                        font=("Segoe UI", 9, "bold"))
        wlbl.grid(row=0, column=1, padx=10)
        slbl.grid(row=0, column=2, padx=10)
        self._res_sum_lbls[bld] = (wlbl, slbl)

    def _res_on_change(self, bld):
        for lv, (wv, sv, dv) in enumerate(self._res_vars[bld]):
            try: w = int(wv.get())
            except: w = 0
            try: s = int(sv.get())
            except: s = 0
            self._res_data[bld][lv] = {"wood": w, "stone": s, "done": dv.get()}
        self._res_update_sums()
        self._res_save_file()

    def _res_update_sums(self):
        total_w = total_s = 0
        for bld in self._RES_BUILDINGS:
            w = s = 0
            for row in self._res_data.get(bld, []):
                if not row.get("done", False):
                    w += row.get("wood", 0)
                    s += row.get("stone", 0)
            if bld in self._res_sum_lbls:
                self._res_sum_lbls[bld][0].config(text=f"🪵 {w:,}")
                self._res_sum_lbls[bld][1].config(text=f"🪨 {s:,}")
            total_w += w
            total_s += s
        if hasattr(self, '_res_global_wood'):
            self._res_global_wood.config(text=f"🪵 {total_w:,}")
            self._res_global_stone.config(text=f"🪨 {total_s:,}")

    def _res_populate(self):
        if not hasattr(self, '_res_vars'): return
        for bld in self._RES_BUILDINGS:
            if bld not in self._res_vars: continue
            rows = self._res_data.get(bld, [{"wood":0,"stone":0,"done":False}]*20)
            for lv, (wv, sv, dv) in enumerate(self._res_vars[bld]):
                row = rows[lv] if lv < len(rows) else {"wood":0,"stone":0,"done":False}
                wv.set(str(row.get("wood", 0)))
                sv.set(str(row.get("stone", 0)))
                dv.set(row.get("done", False))
        self._res_update_sums()

    def _res_reset_all(self):
        """Chỉ uncheck tất cả, giữ nguyên giá trị tài nguyên."""
        if not messagebox.askyesno("Uncheck tất cả", "Bỏ chọn tất cả các ô đã xây?"):
            return
        for bld in self._RES_BUILDINGS:
            for row in self._res_data.get(bld, []):
                row["done"] = False
            if bld in self._res_vars:
                for _, _, dv in self._res_vars[bld]:
                    dv.set(False)
        self._res_update_sums()
        self._res_save_file()

    def _res_check_all(self):
        """Check tất cả các ô (đánh dấu đã xây hết)."""
        if not messagebox.askyesno("Check tất cả", "Đánh dấu tất cả các ô là đã xây?"):
            return
        for bld in self._RES_BUILDINGS:
            for row in self._res_data.get(bld, []):
                row["done"] = True
            if bld in self._res_vars:
                for _, _, dv in self._res_vars[bld]:
                    dv.set(True)
        self._res_update_sums()
        self._res_save_file()

    def _res_recalc_deficit(self):
        """Tính lại deficit từ Entry hiện có (gọi tự động khi sửa số)."""
        try: cur_w = int(self._res_cur_wood_var.get().replace(",","").strip())
        except: cur_w = None
        try: cur_s = int(self._res_cur_stone_var.get().replace(",","").strip())
        except: cur_s = None
        need_w = need_s = 0
        for bld in self._RES_BUILDINGS:
            for wv, sv, dv in self._res_vars.get(bld, []):
                if not dv.get():
                    try: need_w += int(wv.get() or 0)
                    except: pass
                    try: need_s += int(sv.get() or 0)
                    except: pass
        if cur_w is not None:
            def_w = max(0, need_w - cur_w)
            self._res_deficit_wood.config(
                text=f"Gỗ -{def_w:,}" if def_w > 0 else "Gỗ ✓ Đủ",
                fg=C["red"] if def_w > 0 else C["green"])
        else:
            self._res_deficit_wood.config(text="Gỗ —", fg=C["muted"])
        if cur_s is not None:
            def_s = max(0, need_s - cur_s)
            self._res_deficit_stone.config(
                text=f"Đá -{def_s:,}" if def_s > 0 else "Đá ✓ Đủ",
                fg=C["red"] if def_s > 0 else C["green"])
        else:
            self._res_deficit_stone.config(text="Đá —", fg=C["muted"])
        if cur_w is not None or cur_s is not None:
            parts = []
            if cur_w is not None and need_w > 0:
                parts.append(f"Gỗ {min(100,int(cur_w/need_w*100))}%")
            if cur_s is not None and need_s > 0:
                parts.append(f"Đá {min(100,int(cur_s/need_s*100))}%")
            self._res_check_status.config(
                text=" | ".join(parts) if parts else "Du tai nguyen", fg=C["green"])

    def _res_check_current(self):
        if not self.bot.adb.ok:
            self._res_check_status.config(text="Chua ket noi ADB", fg=C["red"])
            return
        self._res_check_status.config(text="Dang doc...", fg=C["yellow"])
        def _run():
            res = self._ocr_read_resources()
            cur_w = res.get("wood")  if res else None
            cur_s = res.get("stone") if res else None
            def _update():
                if cur_w is not None:
                    self._res_cur_wood_var.set(str(cur_w))
                if cur_s is not None:
                    self._res_cur_stone_var.set(str(cur_s))
                need_w = need_s = 0
                for bld in self._RES_BUILDINGS:
                    for lv, (wv, sv, dv) in enumerate(self._res_vars.get(bld, [])):
                        if not dv.get():
                            try: need_w += int(wv.get() or 0)
                            except: pass
                            try: need_s += int(sv.get() or 0)
                            except: pass
                if cur_w is not None:
                    def_w = max(0, need_w - cur_w)
                    self._res_deficit_wood.config(
                        text=f"Thieu Go: {def_w:,}" if def_w > 0 else "Go: Du",
                        fg=C["red"] if def_w > 0 else C["green"])
                else:
                    self._res_deficit_wood.config(text="Go: ?", fg=C["muted"])
                if cur_s is not None:
                    def_s = max(0, need_s - cur_s)
                    self._res_deficit_stone.config(
                        text=f"Thieu Da: {def_s:,}" if def_s > 0 else "Da: Du",
                        fg=C["red"] if def_s > 0 else C["green"])
                else:
                    self._res_deficit_stone.config(text="Da: ?", fg=C["muted"])
                if cur_w is None and cur_s is None:
                    self._res_check_status.config(text="Khong doc duoc", fg=C["red"])
                else:
                    parts = []
                    if cur_w is not None and need_w > 0:
                        pct = min(100, int(cur_w / need_w * 100))
                        parts.append(f"Go {pct}%")
                    if cur_s is not None and need_s > 0:
                        pct = min(100, int(cur_s / need_s * 100))
                        parts.append(f"Da {pct}%")
                    msg = " | ".join(parts) if parts else "Du tai nguyen"
                    self._res_check_status.config(text=msg, fg=C["green"])
            self.after(0, _update)
        threading.Thread(target=_run, daemon=True).start()

    # ────────────────────────────────────────────────────────
    # TAB: NHIỀU ĐỢT (WAVE / MULTI-GROUP ATTACK)
    # ────────────────────────────────────────────────────────

    def _build_wave_tab(self, p):
        """Tab cho tính năng tấn công nhiều nhóm độc lập."""
        # Top bar: Add group + Start/Stop all
        top = tk.Frame(p, bg=C["panel"], pady=4)
        top.pack(fill="x", padx=4, pady=(4,0))
        tk.Label(top, text="🌊 Tấn công nhiều đợt:", bg=C["panel"],
                 fg=C["accent"], font=("Segoe UI", 10, "bold")).pack(side="left", padx=8)
        tk.Button(top, text="➕ Thêm nhóm", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"),
                  command=self._wave_add_group).pack(side="left", padx=4)
        tk.Button(top, text="▶ Chạy tất cả", bg=C["green"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"),
                  command=self._wave_start_all).pack(side="left", padx=4)
        tk.Button(top, text="⏹ Dừng tất cả", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"),
                  command=self._wave_stop_all).pack(side="left", padx=4)

        # Sub-notebook: 1 tab per group
        self._wave_group_nb = ttk.Notebook(p)
        self._wave_group_nb.pack(fill="both", expand=True, padx=4, pady=4)
        self._wave_group_frames = {}   # group_name → frame
        self._wave_group_widgets = {}  # group_name → {widgets}

        # Log box
        log_sec = self._sec(p, "Log")
        self._wave_log_txt = tk.Text(log_sec, height=7, bg=C["entry"], fg=C["text"],
                                     font=("Consolas", 8), state="disabled", wrap="word")
        self._wave_log_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self._wave_log_txt.tag_configure("ok",   foreground="#55efc4")
        self._wave_log_txt.tag_configure("warn", foreground="#ffeaa7")
        self._wave_log_txt.tag_configure("err",  foreground="#ff7675")
        self._wave_log_txt.tag_configure("info", foreground=C["muted"])
        tk.Button(log_sec, text="🗑 Xóa log", command=self._wave_clear_log,
                  bg=C["muted"], fg="white", relief="flat",
                  font=("Segoe UI", 8), padx=6, pady=2,
                  cursor="hand2", bd=0).pack(anchor="e", padx=4, pady=(0,4))

        # Render existing groups (after load)
        for g in self.wave_groups:
            self._wave_render_group(g)

    def _wave_add_group(self):
        idx = len(self.wave_groups) + 1
        g = WaveGroup(name=f"Nhóm {idx}")
        self.wave_groups.append(g)
        self._wave_render_group(g)
        try: self._autosave()
        except: pass

    def _wave_log(self, msg: str):
        """Ghi log vào cả log chính lẫn log box Nhiều đợt."""
        self._log(msg)
        def _upd():
            try:
                import datetime
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                line = f"[{ts}] {msg}\n"
                ml = msg.lower()
                if any(x in ml for x in ["✅", "xong", "▶", "bắt đầu"]):
                    tag = "ok"
                elif any(x in ml for x in ["⚠", "warn", "cooldown", "hành quân", "⏳"]):
                    tag = "warn"
                elif any(x in ml for x in ["❌", "lỗi", "error", "dừng", "⏹"]):
                    tag = "err"
                else:
                    tag = "info"
                self._wave_log_txt.configure(state="normal")
                self._wave_log_txt.insert("end", line, tag)
                self._wave_log_txt.see("end")
                self._wave_log_txt.configure(state="disabled")
            except Exception: pass
        self.after(0, _upd)

    def _wave_clear_log(self):
        try:
            self._wave_log_txt.configure(state="normal")
            self._wave_log_txt.delete("1.0", "end")
            self._wave_log_txt.configure(state="disabled")
        except Exception: pass

    def _wave_render_group(self, g: WaveGroup):
        nb = self._wave_group_nb
        frame = tk.Frame(nb, bg=C["bg"])
        nb.add(frame, text=f"🗂 {g.name}")
        self._wave_group_frames[g.name] = frame
        self._wave_build_group_ui(frame, g)
        # Switch to new tab
        nb.select(frame)

    def _wave_build_group_ui(self, frame, g: WaveGroup):
        """Build UI for 1 group inside its tab frame."""
        w = {}  # widget dict

        # ── Header row ──
        hdr = tk.Frame(frame, bg=C["panel"], pady=4)
        hdr.pack(fill="x", padx=4, pady=(4,2))

        tk.Label(hdr, text="Tên nhóm:", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,2))
        w["e_name"] = tk.Entry(hdr, width=14, bg=C["entry"], fg=C["text"],
                               insertbackground="white", relief="flat")
        w["e_name"].insert(0, g.name)
        w["e_name"].pack(side="left", padx=2)

        tk.Button(hdr, text="✏ Đổi tên", bg=C["muted"], fg="white",
                  relief="flat", font=("Segoe UI", 8),
                  command=lambda: self._wave_rename_group(g, w)).pack(side="left", padx=4)

        # Status label
        w["lbl_status"] = tk.Label(hdr, text="● IDLE", bg=C["panel"],
                                    fg=C["muted"], font=("Consolas", 9, "bold"))
        w["lbl_status"].pack(side="left", padx=8)

        tk.Button(hdr, text="▶ Chạy", bg=C["green"], fg="white",
                  relief="flat", font=("Segoe UI", 8, "bold"),
                  command=lambda: self._wave_start_group(g, w)).pack(side="right", padx=4)
        tk.Button(hdr, text="⏭️ Tiếp", bg=C["yellow"], fg="white",
                  relief="flat", font=("Segoe UI", 8, "bold"),
                  command=lambda: self._wave_skip_group(g)).pack(side="right", padx=4)
        tk.Button(hdr, text="⏹ Dừng", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 8, "bold"),
                  command=lambda: self._wave_stop_group(g)).pack(side="right", padx=4)
        tk.Button(hdr, text="🗑 Xóa nhóm", bg=C["muted"], fg="white",
                  relief="flat", font=("Segoe UI", 8),
                  command=lambda: self._wave_delete_group(g)).pack(side="right", padx=4)

        # ── PanedWindow: troops left, points right ──
        paned = ttk.PanedWindow(frame, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        # LEFT: troops
        lf = tk.Frame(paned, bg=C["panel"])
        paned.add(lf, weight=1)
        lh = tk.Frame(lf, bg=C["panel"])
        lh.pack(fill="x", pady=2)
        tk.Label(lh, text="🎖 Quân (tên đầu tiên = quân chính):", bg=C["panel"],
                 fg=C["accent"], font=("Segoe UI", 8, "bold")).pack(side="left", padx=4)
        tk.Button(lh, text="+ Thêm", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 7),
                  command=lambda: self._wave_add_troop(g, w)).pack(side="right", padx=2)

        w["troop_list"] = tk.Listbox(lf, bg=C["entry"], fg=C["text"],
                                      selectbackground=C["accent"],
                                      font=("Consolas", 9), height=8, relief="flat")
        w["troop_list"].pack(fill="both", expand=True, padx=4, pady=2)
        for t in g.troop_names:
            w["troop_list"].insert("end", t)

        lb = tk.Frame(lf, bg=C["panel"])
        lb.pack(fill="x", pady=2)
        tk.Button(lb, text="✏ Sửa", bg=C["muted"], fg="white",
                  relief="flat", font=("Segoe UI", 7),
                  command=lambda: self._wave_edit_troop(g, w)).pack(side="left", padx=2)
        tk.Button(lb, text="🗑 Xóa", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 7),
                  command=lambda: self._wave_del_troop(g, w)).pack(side="left", padx=2)

        # ── Heal settings ──
        heal_sec = tk.Frame(lf, bg=C["panel"]); heal_sec.pack(fill="x", padx=4, pady=(6,2))
        tk.Label(heal_sec, text="🩸 Hồi máu", bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")

        hr1 = tk.Frame(lf, bg=C["panel"]); hr1.pack(fill="x", padx=4, pady=1)
        tk.Label(hr1, text="Heal mỗi:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
        w["e_heal_every"] = tk.Entry(hr1, width=4, bg=C["entry"], fg=C["text"],
                                      insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_heal_every"].insert(0, str(g.heal_every))
        w["e_heal_every"].pack(side="left", padx=2)
        tk.Label(hr1, text="lần (0=tắt)", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left")

        hr1b = tk.Frame(lf, bg=C["panel"]); hr1b.pack(fill="x", padx=4, pady=1)
        tk.Label(hr1b, text="Step đặt:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
        w["e_heal_step"] = tk.Entry(hr1b, width=4, bg=C["entry"], fg=C["text"],
                                     insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_heal_step"].insert(0, str(g.heal_step))
        w["e_heal_step"].pack(side="left", padx=2)
        tk.Label(hr1b, text="step (0=tắt)", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 7)).pack(side="left")

        hr2 = tk.Frame(lf, bg=C["panel"]); hr2.pack(fill="x", padx=4, pady=1)
        tk.Label(hr2, text="Heal X:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
        w["e_heal_x"] = tk.Entry(hr2, width=6, bg=C["entry"], fg=C["text"],
                                  insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_heal_x"].insert(0, str(g.heal_x))
        w["e_heal_x"].pack(side="left", padx=2)
        tk.Label(hr2, text="Y:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_heal_y"] = tk.Entry(hr2, width=6, bg=C["entry"], fg=C["text"],
                                  insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_heal_y"].insert(0, str(g.heal_y))
        w["e_heal_y"].pack(side="left", padx=2)

        hr3 = tk.Frame(lf, bg=C["panel"]); hr3.pack(fill="x", padx=4, pady=1)
        w["var_heal_on_done"] = tk.BooleanVar(value=g.heal_on_done)
        tk.Checkbutton(hr3, text="Hồi máu khi hết điểm cuối", variable=w["var_heal_on_done"],
                        bg=C["panel"], fg=C["text"], selectcolor=C["entry"],
                        activebackground=C["panel"], activeforeground=C["text"],
                        font=("Segoe UI", 8)).pack(side="left")

        # ── Cooldown settings (per group) ──
        cd_sec = tk.Frame(lf, bg=C["panel"]); cd_sec.pack(fill="x", padx=4, pady=(6,2))
        tk.Label(cd_sec, text="⏱ Cooldown (0=dùng chung)", bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")

        cr1 = tk.Frame(lf, bg=C["panel"]); cr1.pack(fill="x", padx=4, pady=1)
        tk.Label(cr1, text="CD(s):", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
        w["e_cd"] = tk.Entry(cr1, width=5, bg=C["entry"], fg=C["text"],
                             insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_cd"].insert(0, str(g.cooldown_sec))
        w["e_cd"].pack(side="left", padx=2)
        tk.Label(cr1, text="Lv2:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_cd_lv2"] = tk.Entry(cr1, width=5, bg=C["entry"], fg=C["text"],
                                  insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_cd_lv2"].insert(0, str(g.lv2_cooldown_sec))
        w["e_cd_lv2"].pack(side="left", padx=2)

        cr2 = tk.Frame(lf, bg=C["panel"]); cr2.pack(fill="x", padx=4, pady=1)
        tk.Label(cr2, text="Lv3:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8), width=9, anchor="w").pack(side="left")
        w["e_cd_lv3"] = tk.Entry(cr2, width=5, bg=C["entry"], fg=C["text"],
                                  insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_cd_lv3"].insert(0, str(g.lv3_cooldown_sec))
        w["e_cd_lv3"].pack(side="left", padx=2)
        tk.Label(cr2, text="Lv4:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_cd_lv4"] = tk.Entry(cr2, width=5, bg=C["entry"], fg=C["text"],
                                  insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_cd_lv4"].insert(0, str(g.lv4_cooldown_sec))
        w["e_cd_lv4"].pack(side="left", padx=2)

        # RIGHT: points
        rf = tk.Frame(paned, bg=C["panel"])
        paned.add(rf, weight=2)
        rh = tk.Frame(rf, bg=C["panel"])
        rh.pack(fill="x", pady=2)
        tk.Label(rh, text="📍 Điểm tấn công:", bg=C["panel"],
                 fg=C["accent"], font=("Segoe UI", 8, "bold")).pack(side="left", padx=4)

        # Row 1: Inline add X, Y, Label, + Thêm
        inp = tk.Frame(rf, bg=C["panel"]); inp.pack(fill="x", padx=4, pady=2)
        tk.Label(inp, text="X:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left")
        w["e_add_x"] = tk.Entry(inp, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_add_x"].pack(side="left", padx=2)
        tk.Label(inp, text="Y:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_add_y"] = tk.Entry(inp, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_add_y"].pack(side="left", padx=2)
        tk.Label(inp, text="Nhãn:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_add_label"] = tk.Entry(inp, width=9, bg=C["entry"], fg=C["text"],
                                     insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_add_label"].pack(side="left", padx=2)
        tk.Button(inp, text="➕ Thêm", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 8, "bold"), padx=6, pady=2,
                  command=lambda: self._wave_add_point(g, w)).pack(side="left", padx=4)

        # Row 2: Dãy X cố định, Y chạy
        rx_row = tk.Frame(rf, bg=C["panel"]); rx_row.pack(fill="x", padx=4, pady=1)
        tk.Label(rx_row, text="Dãy X cố: X=", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left")
        w["e_rx"] = tk.Entry(rx_row, width=6, bg=C["entry"], fg=C["text"],
                              insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_rx"].pack(side="left", padx=2)
        tk.Label(rx_row, text="Y từ", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_ry_from"] = tk.Entry(rx_row, width=6, bg=C["entry"], fg=C["text"],
                                   insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_ry_from"].pack(side="left", padx=2)
        tk.Label(rx_row, text="→", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left")
        w["e_ry_to"] = tk.Entry(rx_row, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_ry_to"].pack(side="left", padx=2)
        tk.Label(rx_row, text="Nhãn:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_rlabel"] = tk.Entry(rx_row, width=7, bg=C["entry"], fg=C["text"],
                                  insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_rlabel"].pack(side="left", padx=2)
        tk.Button(rx_row, text="➕ Thêm dãy", bg=C["green"], fg="#111",
                  relief="flat", font=("Segoe UI", 8, "bold"),
                  command=lambda: self._wave_add_range_x(g, w)).pack(side="left", padx=4)

        # Row 3: Dãy Y cố định, X chạy
        ry_row = tk.Frame(rf, bg=C["panel"]); ry_row.pack(fill="x", padx=4, pady=1)
        tk.Label(ry_row, text="Dãy Y cố: Y=", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left")
        w["e_ry"] = tk.Entry(ry_row, width=6, bg=C["entry"], fg=C["text"],
                              insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_ry"].pack(side="left", padx=2)
        tk.Label(ry_row, text="X từ", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_rx_from"] = tk.Entry(ry_row, width=6, bg=C["entry"], fg=C["text"],
                                   insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_rx_from"].pack(side="left", padx=2)
        tk.Label(ry_row, text="→", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left")
        w["e_rx_to"] = tk.Entry(ry_row, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_rx_to"].pack(side="left", padx=2)
        tk.Label(ry_row, text="Nhãn:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(side="left", padx=(4,0))
        w["e_rlabel2"] = tk.Entry(ry_row, width=7, bg=C["entry"], fg=C["text"],
                                   insertbackground="white", relief="flat", font=("Segoe UI", 8))
        w["e_rlabel2"].pack(side="left", padx=2)
        tk.Button(ry_row, text="➕ Thêm dãy", bg=C["green"], fg="#111",
                  relief="flat", font=("Segoe UI", 8, "bold"),
                  command=lambda: self._wave_add_range_y(g, w)).pack(side="left", padx=4)

        # Row 4: Import nhiều dòng
        paste_row = tk.Frame(rf, bg=C["panel"]); paste_row.pack(fill="x", padx=4, pady=1)
        tk.Label(paste_row, text="Paste nhiều dòng (X,Y mỗi dòng):", bg=C["panel"],
                 fg=C["text"], font=("Segoe UI", 8)).pack(side="left")
        tk.Button(paste_row, text="📋 Import", bg=C["card"], fg=C["accent"],
                  relief="flat", font=("Segoe UI", 8),
                  command=lambda: self._wave_paste_import(g, w)).pack(side="left", padx=4)

        # Points tree
        cols = ("#", "Game X", "Game Y", "Nhan", "Status", "✅", "⏳")
        tv = ttk.Treeview(rf, columns=cols, show="headings", height=8, selectmode="browse")
        for col, cw in zip(cols, (30, 60, 60, 100, 60, 40, 40)):
            tv.heading(col, text=col); tv.column(col, width=cw, anchor="center")
        tv.tag_configure("s_wait",    foreground=C["muted"])
        tv.tag_configure("s_going",   foreground=C["yellow"])
        tv.tag_configure("s_process", foreground=C["accent"])
        tv.tag_configure("s_done",    foreground=C["green"])
        tv.tag_configure("s_current", foreground=C["yellow"], background=C["card"])
        tv.pack(fill="both", expand=True, padx=4, pady=2)
        tv.bind("<ButtonRelease-1>", lambda e, _g=g, _w=w: self._wave_tree_click(e, _g, _w))
        w["pt_tree"] = tv

        # Tree controls
        tc = tk.Frame(rf, bg=C["panel"]); tc.pack(pady=(0,4))
        for text, cmd, color in [
            ("⬆ Len",    lambda: self._wave_move_point(g, w, -1),  C["muted"]),
            ("⬇ Xuong",  lambda: self._wave_move_point(g, w,  1),  C["muted"]),
            ("✏ Sua",    lambda: self._wave_edit_point(g, w),       C["accent"]),
            ("🗑 Xoa",   lambda: self._wave_del_point(g, w),        C["red"]),
            ("Xoa het",  lambda: self._wave_clear_points(g, w),     "#333355"),
            ("🧹 Xoa done", lambda: self._wave_clear_done(g, w),   "#553333"),
        ]:
            tk.Button(tc, text=text, command=cmd,
                      bg=color, fg="white", relief="flat",
                      font=("Segoe UI", 8, "bold"), padx=6, pady=3,
                      cursor="hand2", bd=0,
                      activebackground=color).pack(side="left", padx=2)

        # Import từ tab Tấn công
        tk.Button(tc, text="📋 Import từ tab Tấn công", bg=C["card"], fg=C["accent"],
                  relief="flat", font=("Segoe UI", 8),
                  command=lambda: self._wave_import_points(g, w)).pack(side="left", padx=8)

        # Current point indicator
        w["lbl_cur"] = tk.Label(rf, text="Điểm hiện tại: —", bg=C["panel"],
                                 fg=C["yellow"], font=("Segoe UI", 8))
        w["lbl_cur"].pack(pady=2)

        self._wave_group_widgets[g.name] = w
        self._wave_refresh_points(g, w)

    # ── Wave helpers ──

    def _wave_refresh_points(self, g: WaveGroup, w: dict, cur_idx: int = -1):
        tv = w["pt_tree"]
        tv.delete(*tv.get_children())
        _STATUS_TAG = {"waiting": "s_wait", "going": "s_going",
                       "process": "s_process", "done": "s_done"}
        for i, pt in enumerate(g.points):
            st = pt.get("status", "waiting")
            tag = "s_current" if i == cur_idx else _STATUS_TAG.get(st, "s_wait")
            tv.insert("", "end", tags=(tag,),
                      values=(i+1, pt.get("x",""), pt.get("y",""),
                              pt.get("label",""), st, "Done", "Wait"))

    def _wave_tree_click(self, event, g: WaveGroup, w: dict):
        """Handle click on ✅ Done / ⏳ Wait columns in wave point treeview."""
        tv = w["pt_tree"]
        row_id = tv.identify_row(event.y)
        col_id = tv.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_num = int(col_id.replace("#", ""))
        if col_num not in (6, 7):
            return  # not an action column
        idx = tv.index(row_id)
        if idx < 0 or idx >= len(g.points):
            return
        new_status = "done" if col_num == 6 else "waiting"
        g.points[idx]["status"] = new_status
        self._wave_refresh_points(g, w)
        # Re-select
        children = tv.get_children()
        if idx < len(children):
            tv.selection_set(children[idx])
        try: self._autosave()
        except: pass

    def _wave_refresh_all(self):
        """Re-render all groups after load."""
        if not hasattr(self, '_wave_group_nb'): return
        # Clear existing tabs
        for tab in self._wave_group_nb.tabs():
            self._wave_group_nb.forget(tab)
        self._wave_group_frames.clear()
        self._wave_group_widgets.clear()
        for g in self.wave_groups:
            self._wave_render_group(g)

    def _wave_rename_group(self, g: WaveGroup, w: dict):
        new_name = w["e_name"].get().strip()
        if not new_name: return
        old_name = g.name
        g.name = new_name
        # Update tab title
        nb = self._wave_group_nb
        frame = self._wave_group_frames.get(old_name)
        if frame:
            try: nb.tab(frame, text=f"🗂 {new_name}")
            except: pass
            self._wave_group_frames[new_name] = frame
            if old_name != new_name:
                self._wave_group_frames.pop(old_name, None)
                ww = self._wave_group_widgets.pop(old_name, None)
                if ww: self._wave_group_widgets[new_name] = ww
        try: self._autosave()
        except: pass

    def _wave_add_troop(self, g: WaveGroup, w: dict):
        from tkinter.simpledialog import askstring
        name = askstring("Thêm quân", "Tên quân (vd: digdef2, auto3,auto4):",
                         parent=self)
        if not name or not name.strip(): return
        # Hỗ trợ nhập nhiều tên cách nhau bởi dấu phẩy
        names = [n.strip() for n in name.split(",") if n.strip()]
        for n in names:
            g.troop_names.append(n)
            w["troop_list"].insert("end", n)
        try: self._autosave()
        except: pass

    def _wave_edit_troop(self, g: WaveGroup, w: dict):
        from tkinter.simpledialog import askstring
        sel = w["troop_list"].curselection()
        if not sel: return
        idx = sel[0]
        old = g.troop_names[idx]
        new = askstring("Sửa quân", "Tên mới:", initialvalue=old, parent=self)
        if not new or not new.strip(): return
        g.troop_names[idx] = new.strip()
        w["troop_list"].delete(idx); w["troop_list"].insert(idx, new.strip())
        try: self._autosave()
        except: pass

    def _wave_del_troop(self, g: WaveGroup, w: dict):
        sel = w["troop_list"].curselection()
        if not sel: return
        idx = sel[0]
        g.troop_names.pop(idx)
        w["troop_list"].delete(idx)
        try: self._autosave()
        except: pass

    def _wave_add_point(self, g: WaveGroup, w: dict):
        try:
            x = int(w["e_add_x"].get().strip())
            y = int(w["e_add_y"].get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "X, Y phải là số nguyên"); return
        # Check trùng
        existing = {(p["x"], p["y"]) for p in g.points}
        if (x, y) in existing:
            self._log(f"[Wave/{g.name}] Bỏ qua ({x},{y}) → đã tồn tại")
            return
        label = w["e_add_label"].get().strip() or f"pt{len(g.points)+1}"
        g.points.append({"x": x, "y": y, "label": label})
        self._wave_refresh_points(g, w)
        w["e_add_x"].delete(0, "end")
        w["e_add_y"].delete(0, "end")
        w["e_add_label"].delete(0, "end")
        try: self._autosave()
        except: pass

    def _wave_edit_point(self, g: WaveGroup, w: dict):
        tv = w["pt_tree"]
        sel = tv.selection()
        if not sel: return
        idx = tv.index(sel[0])
        pt = g.points[idx]
        dlg = tk.Toplevel(self)
        dlg.title("Sửa điểm")
        dlg.configure(bg=C["bg"]); dlg.grab_set()
        def _lbl(txt): tk.Label(dlg, text=txt, bg=C["bg"], fg=C["text"],
                                 font=("Segoe UI",9)).pack(anchor="w", padx=10, pady=2)
        def _ent(val):
            e = tk.Entry(dlg, bg=C["entry"], fg=C["text"], insertbackground="white",
                         relief="flat", width=20)
            e.insert(0, str(val)); e.pack(padx=10, pady=2); return e
        _lbl("X:"); ex = _ent(pt.get("x",""))
        _lbl("Y:"); ey = _ent(pt.get("y",""))
        _lbl("Nhãn:"); el = _ent(pt.get("label",""))
        _lbl("Status:")
        status_var = tk.StringVar(value=pt.get("status", "waiting"))
        status_cb = ttk.Combobox(dlg, textvariable=status_var, width=18, state="readonly",
                                  values=["waiting", "going", "process", "done"])
        status_cb.pack(padx=10, pady=2)
        def _ok():
            try: nx = int(ex.get()); ny = int(ey.get())
            except: messagebox.showerror("Lỗi", "X,Y phải là số"); return
            # Check trùng (bỏ qua chính nó)
            for i2, p2 in enumerate(g.points):
                if i2 == idx: continue
                if p2["x"] == nx and p2["y"] == ny:
                    messagebox.showwarning("Trùng", f"Điểm ({nx},{ny}) đã tồn tại"); return
            pt["x"] = nx; pt["y"] = ny
            pt["label"] = el.get().strip()
            pt["status"] = status_var.get()
            self._wave_refresh_points(g, w)
            try: self._autosave()
            except: pass
            dlg.destroy()
        tk.Button(dlg, text="✅ Lưu", bg=C["green"], fg="white",
                  relief="flat", command=_ok).pack(pady=8)
        dlg.wait_window()

    def _wave_del_point(self, g: WaveGroup, w: dict):
        tv = w["pt_tree"]
        sel = tv.selection()
        if not sel: return
        idx = tv.index(sel[0])
        g.points.pop(idx)
        self._wave_refresh_points(g, w)
        try: self._autosave()
        except: pass

    def _wave_move_point(self, g: WaveGroup, w: dict, delta: int):
        tv = w["pt_tree"]
        sel = tv.selection()
        if not sel: return
        idx = tv.index(sel[0])
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(g.points): return
        g.points[idx], g.points[new_idx] = g.points[new_idx], g.points[idx]
        self._wave_refresh_points(g, w)
        # Reselect moved item
        children = tv.get_children()
        if new_idx < len(children):
            tv.selection_set(children[new_idx])
        try: self._autosave()
        except: pass

    def _wave_import_points(self, g: WaveGroup, w: dict):
        """Import all points from main attack tab into this group."""
        if not self.pts:
            messagebox.showinfo("Thông báo", "Tab Tấn công chưa có điểm nào"); return
        existing = {(p["x"], p["y"]) for p in g.points}
        added = skipped = 0
        for pt in self.pts:
            if (pt.game_x, pt.game_y) in existing:
                skipped += 1; continue
            g.points.append({"x": pt.game_x, "y": pt.game_y, "label": pt.label})
            existing.add((pt.game_x, pt.game_y)); added += 1
        self._wave_refresh_points(g, w)
        try: self._autosave()
        except: pass
        msg = f"Đã import {added} điểm"
        if skipped: msg += f" (bỏ qua {skipped} trùng)"
        messagebox.showinfo("Thành công", msg)

    def _wave_add_range_x(self, g: WaveGroup, w: dict):
        """X cố định, Y chạy."""
        try:
            rx      = int(w["e_rx"].get().strip())
            ry_from = int(w["e_ry_from"].get().strip())
            ry_to   = int(w["e_ry_to"].get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "X, Y từ, Y đến phải là số nguyên"); return
        base_label = w["e_rlabel"].get().strip()
        step = 1 if ry_to >= ry_from else -1
        existing = {(p["x"], p["y"]) for p in g.points}
        added = skipped = 0
        for y in range(ry_from, ry_to + step, step):
            if (rx, y) in existing:
                skipped += 1; continue
            label = base_label if base_label else f"pt{len(g.points)+1}"
            g.points.append({"x": rx, "y": y, "label": label})
            existing.add((rx, y)); added += 1
        self._wave_refresh_points(g, w)
        msg = f"[Wave/{g.name}] Thêm {added} điểm X={rx}, Y={ry_from}→{ry_to}"
        if skipped: msg += f" (bỏ qua {skipped} trùng)"
        self._log(msg)
        try: self._autosave()
        except: pass

    def _wave_add_range_y(self, g: WaveGroup, w: dict):
        """Y cố định, X chạy."""
        try:
            ry      = int(w["e_ry"].get().strip())
            rx_from = int(w["e_rx_from"].get().strip())
            rx_to   = int(w["e_rx_to"].get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "Y, X từ, X đến phải là số nguyên"); return
        base_label = w["e_rlabel2"].get().strip()
        step = 1 if rx_to >= rx_from else -1
        existing = {(p["x"], p["y"]) for p in g.points}
        added = skipped = 0
        for x in range(rx_from, rx_to + step, step):
            if (x, ry) in existing:
                skipped += 1; continue
            label = base_label if base_label else f"pt{len(g.points)+1}"
            g.points.append({"x": x, "y": ry, "label": label})
            existing.add((x, ry)); added += 1
        self._wave_refresh_points(g, w)
        msg = f"[Wave/{g.name}] Thêm {added} điểm Y={ry}, X={rx_from}→{rx_to}"
        if skipped: msg += f" (bỏ qua {skipped} trùng)"
        self._log(msg)
        try: self._autosave()
        except: pass

    def _wave_paste_import(self, g: WaveGroup, w: dict):
        """Paste nhiều dòng X,Y hoặc X,Y,Nhan."""
        dlg = tk.Toplevel(self)
        dlg.title("Paste tọa độ (mỗi dòng: X,Y hoặc X,Y,Nhan)")
        dlg.configure(bg=C["bg"]); dlg.grab_set()
        tk.Label(dlg, text="Mỗi dòng: X,Y  hoặc  X,Y,Nhan",
                 bg=C["bg"], fg=C["muted"], font=("Segoe UI", 8)).pack(padx=10, pady=(6,2))
        ta = tk.Text(dlg, width=32, height=12, bg=C["entry"], fg=C["text"],
                     insertbackground="white", relief="flat", font=("Consolas", 9))
        ta.pack(padx=10, pady=4)
        def _ok():
            lines = ta.get("1.0","end").strip().splitlines()
            added = skipped = 0
            existing = {(p["x"], p["y"]) for p in g.points}
            for line in lines:
                parts = [s.strip() for s in line.split(",")]
                if len(parts) < 2: continue
                try: x, y = int(parts[0]), int(parts[1])
                except: continue
                if (x, y) in existing: skipped += 1; continue
                label = parts[2] if len(parts) >= 3 else f"pt{len(g.points)+1}"
                g.points.append({"x": x, "y": y, "label": label})
                existing.add((x, y)); added += 1
            self._wave_refresh_points(g, w)
            msg = f"[Wave/{g.name}] Import {added} điểm"
            if skipped: msg += f" (bỏ qua {skipped} trùng)"
            self._log(msg)
            try: self._autosave()
            except: pass
            dlg.destroy()
        tk.Button(dlg, text="✅ Import", bg=C["green"], fg="white",
                  relief="flat", command=_ok).pack(pady=6)
        dlg.wait_window()

    def _wave_clear_points(self, g: WaveGroup, w: dict):
        if not messagebox.askyesno("Xóa hết", f"Xóa tất cả điểm trong nhóm '{g.name}'?"): return
        g.points.clear()
        self._wave_refresh_points(g, w)
        try: self._autosave()
        except: pass

    def _wave_clear_done(self, g: WaveGroup, w: dict):
        """Xóa nhanh tất cả điểm có status = done."""
        count = sum(1 for p in g.points if p.get("status") == "done")
        if count == 0:
            messagebox.showinfo("Thông báo", "Không có điểm nào đã done."); return
        g.points[:] = [p for p in g.points if p.get("status") != "done"]
        self._wave_refresh_points(g, w)
        self._wave_log(f"[Wave] 🧹 Nhóm '{g.name}': xóa {count} điểm done, còn {len(g.points)} điểm")
        try: self._autosave()
        except: pass

    def _wave_delete_group(self, g: WaveGroup):
        if not messagebox.askyesno("Xóa", f"Xóa nhóm '{g.name}'?"): return
        # Stop engine if running
        for eng in self._wave_engines:
            if eng.group is g: eng.stop()
        frame = self._wave_group_frames.get(g.name)
        if frame:
            try: self._wave_group_nb.forget(frame)
            except: pass
            self._wave_group_frames.pop(g.name, None)
            self._wave_group_widgets.pop(g.name, None)
        if g in self.wave_groups:
            self.wave_groups.remove(g)
        try: self._autosave()
        except: pass

    def _wave_start_group(self, g: WaveGroup, w: dict):
        if not self.bot.adb.ok:
            if not self.bot.adb.connect():
                self._wave_log("[Wave] Kết nối ADB trước!"); return
        # Check phai co it nhat 1 quan
        if not g.troop_names or not g.troop_names[0].strip():
            try:
                import winsound
                for _ in range(3): winsound.Beep(880, 400); time.sleep(0.1)
            except Exception: pass
            messagebox.showerror("Lỗi", f"Nhóm '{g.name}' chưa có tên quân!")
            return
        if not g.points:
            messagebox.showerror("Lỗi", f"Nhóm '{g.name}' chưa có điểm tấn công!"); return
        # Đọc heal settings từ UI
        try:
            g.heal_every = int(w.get("e_heal_every") and w["e_heal_every"].get() or 0)
            g.heal_step  = int(w.get("e_heal_step") and w["e_heal_step"].get() or 0)
            g.heal_x     = int(w.get("e_heal_x") and w["e_heal_x"].get() or 500)
            g.heal_y     = int(w.get("e_heal_y") and w["e_heal_y"].get() or 300)
        except (ValueError, TypeError):
            pass
        # Đọc heal_on_done checkbox
        try:
            g.heal_on_done = w.get("var_heal_on_done") and w["var_heal_on_done"].get() or False
        except (TypeError):
            pass
        # Đọc cooldown settings từ UI
        try:
            g.cooldown_sec     = int(w.get("e_cd") and w["e_cd"].get() or 0)
            g.lv2_cooldown_sec = int(w.get("e_cd_lv2") and w["e_cd_lv2"].get() or 0)
            g.lv3_cooldown_sec = int(w.get("e_cd_lv3") and w["e_cd_lv3"].get() or 0)
            g.lv4_cooldown_sec = int(w.get("e_cd_lv4") and w["e_cd_lv4"].get() or 0)
        except (ValueError, TypeError):
            pass
        if g.cooldown_sec > 0 or g.lv2_cooldown_sec > 0 or g.lv3_cooldown_sec > 0 or g.lv4_cooldown_sec > 0:
            self._wave_log(f"[Wave] ⏱ Nhóm '{g.name}': CD={g.cooldown_sec}s Lv2={g.lv2_cooldown_sec}s Lv3={g.lv3_cooldown_sec}s Lv4={g.lv4_cooldown_sec}s")
        if g.heal_every > 0:
            self._wave_log(f"[Wave] 🩸 Nhóm '{g.name}': heal mỗi {g.heal_every} lần @ ({g.heal_x},{g.heal_y})")
        if g.heal_step > 0:
            self._wave_log(f"[Wave] 🏗 Nhóm '{g.name}': đặt điểm hồi máu mỗi {g.heal_step} step")
        # Tạm dừng bot tấn công chính nếu đang chạy
        if self.bot.state != State.IDLE:
            self._wave_log("[Wave] ⏸ Tạm dừng bot tấn công chính để nhường ADB...")
            self.bot.stop()
        # Stop existing engine for this group
        for eng in self._wave_engines:
            if eng.group is g: eng.stop()
        eng = WaveGroupEngine(g, self.cfg, self.bot.adb, self._wave_log)
        def _refresh():
            self.after(0, lambda: self._wave_update_status(g, w, eng))
        eng.on_refresh = lambda: self.after(0, _refresh)
        eng.on_captcha = self._stop_all_captcha
        eng.on_error = lambda name, msg: self.after(0, lambda: self._tg_notify_error(name, msg))
        self._wave_engines.append(eng)
        eng.start()
        self._wave_log(f"[Wave] ▶ Bắt đầu nhóm '{g.name}'")

    def _wave_stop_group(self, g: WaveGroup):
        for eng in self._wave_engines:
            if eng.group is g:
                eng.stop()
                self._wave_log(f"[Wave] ⏹ Dừng nhóm '{g.name}'")

    def _wave_skip_group(self, g: WaveGroup):
        for eng in self._wave_engines:
            if eng.group is g:
                eng.skip_cooldown()
                self._wave_log(f"[Wave] ⏭️ Skip nhóm '{g.name}' → điểm tiếp theo")

    def _wave_start_all(self):
        # Tạm dừng bot tấn công chính nếu đang chạy
        if self.bot.state != State.IDLE:
            self._wave_log("[Wave] ⏸ Tạm dừng bot tấn công chính để nhường ADB...")
            self.bot.stop()
        for g in self.wave_groups:
            if g.enabled:
                w = self._wave_group_widgets.get(g.name, {})
                self._wave_start_group(g, w)

    def _wave_stop_all(self):
        for eng in self._wave_engines:
            eng.stop()
        self._wave_log("[Wave] ⏹ Dừng tất cả nhóm")

    def _wave_sync_ui(self):
        """Đồng bộ giá trị từ UI entries về WaveGroup objects (gọi trước khi save)."""
        for g in self.wave_groups:
            w = self._wave_group_widgets.get(g.name, {})
            if not w:
                continue
            try:
                g.heal_every = int(w["e_heal_every"].get() or 0)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.heal_step = int(w["e_heal_step"].get() or 0)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.heal_x = int(w["e_heal_x"].get() or 500)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.heal_y = int(w["e_heal_y"].get() or 300)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.cooldown_sec = int(w["e_cd"].get() or 0)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.lv2_cooldown_sec = int(w["e_cd_lv2"].get() or 0)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.lv3_cooldown_sec = int(w["e_cd_lv3"].get() or 0)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.lv4_cooldown_sec = int(w["e_cd_lv4"].get() or 0)
            except (KeyError, ValueError, TypeError): pass
            try:
                g.heal_on_done = w["var_heal_on_done"].get()
            except (KeyError, TypeError): pass

    def _wave_update_status(self, g: WaveGroup, w: dict, eng: WaveGroupEngine):
        STATUS_COLOR = {"idle": C["muted"], "running": C["yellow"],
                        "waiting": C["accent"], "done": C["green"], "error": C["red"]}
        STATUS_TEXT  = {"idle": "● IDLE", "running": "▶ ĐANG CHẠY",
                        "waiting": "⏳ CHỜ", "done": "✅ XONG", "error": "❌ LỖI"}
        s = eng.status
        w["lbl_status"].config(text=STATUS_TEXT.get(s, s),
                                fg=STATUS_COLOR.get(s, C["text"]))
        # Update point statuses: done for past, current for cur_idx, waiting for future
        changed = False
        for i, pt in enumerate(g.points):
            old = pt.get("status", "waiting")
            if i < eng.cur_idx:
                new = "done"
            elif i == eng.cur_idx:
                new = "going" if s == "running" else "process" if s == "waiting" else old
            else:
                new = "waiting"
            if new != old:
                pt["status"] = new
                changed = True
        self._wave_refresh_points(g, w, cur_idx=eng.cur_idx)
        w["lbl_cur"].config(text=f"Điểm hiện tại: {eng.cur_idx+1}/{len(g.points)}")
        if changed:
            try: self._autosave()
            except: pass

    # ────────────────────────────────────────────────────────
    # TAB: DÒ BẢN ĐỒ
    # ────────────────────────────────────────────────────────
    def _build_scout_tab(self, p):
        self._scout_engine  = None
        self._scout_tiles   = {}   # (gx,gy) -> label
        self._scout_origin  = (574, 310)

        # ── Paned: left=config+log, right=map ──
        paned = ttk.PanedWindow(p, orient="horizontal")
        paned.pack(fill="both", expand=True)

        lf = tk.Frame(paned, bg=C["bg"])
        rf = tk.Frame(paned, bg=C["bg"])
        paned.add(lf, weight=1)
        paned.add(rf, weight=2)

        # ── LEFT: Cấu hình ──
        cfg_sec = self._sec(lf, "Cấu hình dò bản đồ")

        def _erow(parent, lbl, var, w=7):
            f = tk.Frame(parent, bg=C["panel"]); f.pack(fill="x", pady=2)
            tk.Label(f, text=lbl, bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
            e = tk.Entry(f, textvariable=var, width=w, bg=C["entry"], fg=C["text"],
                         insertbackground="white", relief="flat", font=("Segoe UI", 9))
            e.pack(side="left", padx=4); return e

        self._sv_ox    = tk.StringVar(value="574")
        self._sv_oy    = tk.StringVar(value="310")
        self._sv_up    = tk.StringVar(value="5")
        self._sv_down  = tk.StringVar(value="5")
        self._sv_left  = tk.StringVar(value="5")
        self._sv_right = tk.StringVar(value="5")

        _erow(cfg_sec, "Mốc X:", self._sv_ox)
        _erow(cfg_sec, "Mốc Y:", self._sv_oy)
        _erow(cfg_sec, "Lên (ô):", self._sv_up)
        _erow(cfg_sec, "Xuống (ô):", self._sv_down)
        _erow(cfg_sec, "Trái (ô):", self._sv_left)
        _erow(cfg_sec, "Phải (ô):", self._sv_right)

        # ── Tự dò đường thực tế ────────────────────────────────────────
        auto_sec = self._sec(lf, "🤖 Tự dò đường thực tế")

        def _arow(parent, lbl):
            f = tk.Frame(parent, bg=C["panel"]); f.pack(fill="x", pady=2)
            tk.Label(f, text=lbl, bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
            sv = tk.StringVar()
            tk.Entry(f, textvariable=sv, width=7, bg=C["entry"], fg=C["text"],
                     insertbackground="white", relief="flat",
                     font=("Segoe UI", 9)).pack(side="left", padx=4)
            return sv

        self._sv_ap_x1 = _arow(auto_sec, "Điểm đầu X:")
        self._sv_ap_y1 = _arow(auto_sec, "Điểm đầu Y:")
        self._sv_ap_x2 = _arow(auto_sec, "Điểm cuối X:")
        self._sv_ap_y2 = _arow(auto_sec, "Điểm cuối Y:")

        ap_mode_row = tk.Frame(auto_sec, bg=C["panel"]); ap_mode_row.pack(fill="x", pady=2)
        tk.Label(ap_mode_row, text="Chế độ:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
        self._sv_ap_mode = tk.StringVar(value="fast")
        tk.Radiobutton(ap_mode_row, text="⚡ Nhanh", variable=self._sv_ap_mode,
                       value="fast", bg=C["panel"], fg=C["text"],
                       selectcolor=C["card"], font=("Segoe UI", 9),
                       activebackground=C["panel"]).pack(side="left", padx=4)
        tk.Radiobutton(ap_mode_row, text="🛡 An toàn", variable=self._sv_ap_mode,
                       value="safe", bg=C["panel"], fg=C["text"],
                       selectcolor=C["card"], font=("Segoe UI", 9),
                       activebackground=C["panel"]).pack(side="left", padx=4)

        ap_btn_row = tk.Frame(auto_sec, bg=C["panel"]); ap_btn_row.pack(pady=5)
        self._ap_btn_start = tk.Button(ap_btn_row, text="▶ Bắt đầu tự dò",
                                        bg=C["green"], fg="#111", relief="flat",
                                        font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                                        command=self._auto_path_start)
        self._ap_btn_start.pack(side="left", padx=4)
        tk.Button(ap_btn_row, text="⏹ Dừng", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=8, pady=4,
                  command=self._auto_path_stop).pack(side="left", padx=4)

        self._ap_status_lbl = tk.Label(auto_sec, text="● Chờ", bg=C["panel"],
                                        fg=C["muted"], font=("Segoe UI", 8, "bold"))
        self._ap_status_lbl.pack(pady=3)
        self._ap_progress_lbl = tk.Label(auto_sec, text="", bg=C["panel"],
                                          fg=C["yellow"], font=("Segoe UI", 8),
                                          wraplength=195, justify="left")
        self._ap_progress_lbl.pack(pady=2, padx=4)

        self._ap_stop_evt = threading.Event()
        self._ap_running  = False

        # ── Pattern: Xoắn ốc ───────────────────────────────────────────
        spiral_sec = self._sec(lf, "🌀 Pattern Dò Xoắn Ốc")

        def _srow(parent, lbl, w=7):
            f = tk.Frame(parent, bg=C["panel"]); f.pack(fill="x", pady=2)
            tk.Label(f, text=lbl, bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
            sv = tk.StringVar()
            tk.Entry(f, textvariable=sv, width=w, bg=C["entry"], fg=C["text"],
                     insertbackground="white", relief="flat",
                     font=("Segoe UI", 9)).pack(side="left", padx=4)
            return sv

        self._sv_sp_x    = _srow(spiral_sec, "Gốc X:")
        self._sv_sp_y    = _srow(spiral_sec, "Gốc Y:")
        self._sv_sp_rings = _srow(spiral_sec, "Số vòng:")
        self._sv_sp_rings.set("3")

        dir_row = tk.Frame(spiral_sec, bg=C["panel"]); dir_row.pack(fill="x", pady=2)
        tk.Label(dir_row, text="Hướng đầu:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
        self._sv_sp_dir = tk.StringVar(value="down")
        for txt, val in [("↑ Lên","up"),("↓ Xuống","down"),("← Trái","left"),("→ Phải","right")]:
            tk.Radiobutton(dir_row, text=txt, variable=self._sv_sp_dir, value=val,
                           bg=C["panel"], fg=C["text"], selectcolor=C["card"],
                           font=("Segoe UI", 8), activebackground=C["panel"]).pack(side="left", padx=2)

        # Tính nhanh không cần ADB
        calc_btn_row = tk.Frame(spiral_sec, bg=C["panel"]); calc_btn_row.pack(pady=3)
        tk.Button(calc_btn_row, text="📋 Tính nhanh (không mở map)",
                  bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 8, "bold"), padx=8, pady=3,
                  command=self._spiral_calc_only).pack(side="left", padx=4)

        sp_btn_row = tk.Frame(spiral_sec, bg=C["panel"]); sp_btn_row.pack(pady=5)
        self._sp_btn_start = tk.Button(sp_btn_row, text="▶ Bắt đầu",
                                        bg=C["green"], fg="#111", relief="flat",
                                        font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                                        command=self._spiral_scout_start)
        self._sp_btn_start.pack(side="left", padx=4)
        tk.Button(sp_btn_row, text="⏹ Dừng", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=8, pady=4,
                  command=self._spiral_scout_stop).pack(side="left", padx=4)
        tk.Button(sp_btn_row, text="👁 Xem trước", bg=C["muted"], fg="white",
                  relief="flat", font=("Segoe UI", 9), padx=8, pady=4,
                  command=self._spiral_preview).pack(side="left", padx=4)

        self._sp_status_lbl = tk.Label(spiral_sec, text="● Chờ", bg=C["panel"],
                                        fg=C["muted"], font=("Segoe UI", 8, "bold"))
        self._sp_status_lbl.pack(pady=2)
        self._sp_progress_lbl = tk.Label(spiral_sec, text="", bg=C["panel"],
                                          fg=C["yellow"], font=("Segoe UI", 8),
                                          wraplength=195, justify="left")
        self._sp_progress_lbl.pack(pady=2, padx=4)

        self._sp_stop_evt = threading.Event()
        self._sp_running  = False

        # ── Buttons ──
        bf = tk.Frame(cfg_sec, bg=C["panel"]); bf.pack(pady=6)
        self._scout_btn_start = tk.Button(
            bf, text="▶ Bắt đầu dò", bg=C["green"], fg="#111",
            relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
            command=self._scout_start)
        self._scout_btn_start.pack(side="left", padx=4)
        tk.Button(bf, text="⏹ Dừng", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  command=self._scout_stop).pack(side="left", padx=4)

        bf2 = tk.Frame(cfg_sec, bg=C["panel"]); bf2.pack(pady=2)
        tk.Button(bf2, text="🗑 Xóa bản đồ", bg="#333355", fg="white",
                  relief="flat", font=("Segoe UI", 9), padx=8, pady=4,
                  command=self._scout_clear).pack(side="left", padx=4)
        tk.Button(bf2, text="📤 Xuất CSV", bg=C["muted"], fg="white",
                  relief="flat", font=("Segoe UI", 9), padx=8, pady=4,
                  command=self._scout_export).pack(side="left", padx=4)

        bf3 = tk.Frame(cfg_sec, bg=C["panel"]); bf3.pack(pady=2)
        def _save_scout_cfg():
            try:
                self._autosave()
                self._scout_status_lbl.config(
                    text="✅ Đã lưu cấu hình", fg=C["green"])
                self.after(2000, lambda: self._scout_status_lbl.config(
                    text="● Chờ", fg=C["muted"]))
                self._scout_log(f"[Scout] 💾 Đã lưu cấu hình: "
                                f"Mốc ({self._sv_ox.get()},{self._sv_oy.get()}) "
                                f"↑{self._sv_up.get()} ↓{self._sv_down.get()} "
                                f"←{self._sv_left.get()} →{self._sv_right.get()}")
            except Exception as e:
                self._scout_status_lbl.config(text=f"❌ Lỗi lưu: {e}", fg=C["red"])
        tk.Button(bf3, text="💾 Lưu cấu hình", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=12, pady=4,
                  command=_save_scout_cfg).pack(side="left", padx=4)

        self._scout_status_lbl = tk.Label(cfg_sec, text="● Chờ", bg=C["panel"],
                                           fg=C["muted"], font=("Segoe UI", 9, "bold"))
        self._scout_status_lbl.pack(pady=4)

        # ── Ước tính thời gian ──
        self._scout_estimate_lbl = tk.Label(cfg_sec, text="", bg=C["panel"],
                                             fg=C["muted"], font=("Segoe UI", 8),
                                             justify="center")
        self._scout_estimate_lbl.pack(pady=2)

        def _update_estimate(*_):
            try:
                up    = int(self._sv_up.get()    or 0)
                down  = int(self._sv_down.get()  or 0)
                left  = int(self._sv_left.get()  or 0)
                right = int(self._sv_right.get() or 0)
                total = (up + down + 1) * (left + right + 1)
                secs  = total * 30
                h, rem = divmod(secs, 3600)
                m, s   = divmod(rem, 60)
                if h > 0:
                    time_str = f"{h}g {m}p {s}s"
                elif m > 0:
                    time_str = f"{m}p {s}s"
                else:
                    time_str = f"{s}s"
                self._scout_estimate_lbl.config(
                    text=f"📐 {total} ô  ·  ⏱ ~{time_str} (30s/ô)",
                    fg=C["yellow"])
            except (ValueError, tk.TclError):
                self._scout_estimate_lbl.config(text="")

        for sv in [self._sv_up, self._sv_down, self._sv_left, self._sv_right]:
            sv.trace_add("write", _update_estimate)
        _update_estimate()

        # ── Progress label ──
        self._scout_progress_lbl = tk.Label(cfg_sec, text="", bg=C["panel"],
                                             fg=C["muted"], font=("Segoe UI", 8))
        self._scout_progress_lbl.pack()

        # ── Log box ──
        log_sec = self._sec(lf, "Log dò bản đồ")
        log_frame = tk.Frame(log_sec, bg=C["panel"])
        log_frame.pack(fill="both", expand=True, padx=2, pady=2)
        self._scout_log_txt = tk.Text(
            log_frame, bg="#0a0a14", fg=C["text"], relief="flat",
            font=("Consolas", 8), wrap="word", state="disabled", height=16)
        sb = tk.Scrollbar(log_frame, command=self._scout_log_txt.yview,
                          bg=C["panel"], troughcolor=C["card"])
        self._scout_log_txt.configure(yscrollcommand=sb.set)
        self._scout_log_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # Tag colours
        self._scout_log_txt.tag_configure("info",  foreground=C["muted"])
        self._scout_log_txt.tag_configure("ok",    foreground=C["green"])
        self._scout_log_txt.tag_configure("warn",  foreground=C["yellow"])
        self._scout_log_txt.tag_configure("error", foreground=C["red"])
        self._scout_log_txt.tag_configure("ocr",   foreground="#888")
        tk.Button(log_sec, text="🗑 Xóa log", bg=C["card"], fg=C["muted"],
                  relief="flat", font=("Segoe UI", 8),
                  command=self._scout_clear_log).pack(anchor="e", padx=4, pady=2)

        # ── RIGHT: Bản đồ canvas ──
        map_sec = self._sec(rf, "Bản đồ phác thảo")
        self._scout_canvas = tk.Canvas(map_sec, bg="#0d0d1a",
                                        highlightthickness=1, highlightbackground=C["border"])
        self._scout_canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self._scout_cell = 14
        self._scout_canvas.bind("<Configure>", lambda e: self._scout_draw_map())
        self._scout_canvas.bind("<Button-1>", self._scout_canvas_click)

        # ── Legend ──
        leg = tk.Frame(map_sec, bg=C["bg"]); leg.pack(pady=2)
        for label, color, txt in [
            ("lv1",       "#55efc4", "Wasteland"),
            ("lv2",       "#00b894", "Lv.2"),
            ("lv3",       "#00cec9", "Lv.3"),
            ("lv4",       "#ffeaa7", "Lv.4"),
            ("lv5",       "#fdcb6e", "Lv.5"),
            ("ally",      "#0984e3", "Đồng minh"),
            ("enemy_city","#d63031", "Thành địch"),
            ("enemy",     "#ff7675", "Địch"),
            ("terrain",   "#dfe6e9", "Địa hình"),
            ("?",         "#b2bec3", "Chưa rõ"),
            ("",          "#636e72", "Chưa dò"),
        ]:
            f = tk.Frame(leg, bg=C["bg"]); f.pack(side="left", padx=4)
            tk.Label(f, bg=color, width=2, relief="flat").pack(side="left")
            tk.Label(f, text=txt, bg=C["bg"], fg=C["muted"],
                     font=("Segoe UI", 8)).pack(side="left", padx=2)

        # ── Bảng tọa độ đã dò ──
        tbl_sec = self._sec(rf, "Danh sách ô đã dò")

        # Toolbar lọc + copy
        tbar = tk.Frame(tbl_sec, bg=C["panel"]); tbar.pack(fill="x", pady=2, padx=2)
        tk.Label(tbar, text="Lọc:", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left")
        self._scout_filter_var = tk.StringVar(value="Tất cả")
        filter_opts = ["Tất cả", "lv1", "lv2", "lv3", "lv4", "lv5",
                        "ally", "enemy_city", "terrain", "?"]
        flt_cb = ttk.Combobox(tbar, textvariable=self._scout_filter_var,
                               values=filter_opts, width=10, state="readonly")
        flt_cb.pack(side="left", padx=4)
        flt_cb.bind("<<ComboboxSelected>>", lambda e: self._scout_refresh_table())

        tk.Button(tbar, text="📋 Copy danh sách", bg=C["card"], fg=C["text"],
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=2,
                  command=self._scout_copy_table).pack(side="right", padx=4)
        self._scout_tbl_count = tk.Label(tbar, text="0 ô", bg=C["panel"],
                                          fg=C["muted"], font=("Segoe UI", 8))
        self._scout_tbl_count.pack(side="right", padx=6)

        # Treeview
        tbl_frame = tk.Frame(tbl_sec, bg=C["panel"])
        tbl_frame.pack(fill="both", expand=True, padx=2, pady=2)
        cols_tbl = ("X", "Y", "Loại", "Mô tả")
        self._scout_tree = ttk.Treeview(tbl_frame, columns=cols_tbl,
                                         show="headings", height=10)
        _LABEL_DESC = {
            "lv1": "Wasteland", "lv2": "Lv.2", "lv3": "Lv.3",
            "lv4": "Lv.4",      "lv5": "Lv.5",
            "ally": "Đồng minh", "enemy_city": "Thành địch",
            "enemy": "Địch",    "terrain": "Địa hình", "?": "Chưa rõ",
        }
        for col, w, anchor in [("X",50,"center"),("Y",50,"center"),
                                 ("Loại",70,"center"),("Mô tả",90,"center")]:
            self._scout_tree.heading(col, text=col,
                command=lambda c=col: self._scout_sort_table(c))
            self._scout_tree.column(col, width=w, anchor=anchor)
        # Tag màu theo loại
        _SCOUT_TAG_COLORS = {
            "lv1":"#55efc4","lv2":"#00b894","lv3":"#00cec9",
            "lv4":"#ffeaa7","lv5":"#fdcb6e","ally":"#74b9ff",
            "enemy_city":"#ff7675","enemy":"#fab1a0",
            "terrain":"#dfe6e9","?":"#b2bec3",
        }
        for tag, fg in _SCOUT_TAG_COLORS.items():
            self._scout_tree.tag_configure(tag, foreground=fg)
        vsb = ttk.Scrollbar(tbl_frame, orient="vertical",
                             command=self._scout_tree.yview)
        self._scout_tree.configure(yscrollcommand=vsb.set)
        self._scout_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._LABEL_DESC = _LABEL_DESC
        self._scout_sort_col = "X"
        self._scout_sort_rev = False
        # Double-click để sửa
        self._scout_tree.bind("<Double-1>", self._scout_edit_from_tree)

        # Nút sửa bên dưới bảng
        tbl_btn_row = tk.Frame(tbl_sec, bg=C["panel"])
        tbl_btn_row.pack(fill="x", padx=2, pady=2)
        tk.Button(tbl_btn_row, text="✏️ Sửa ô đã chọn",
                  bg=C["card"], fg=C["text"], relief="flat",
                  font=("Segoe UI", 8), padx=8, pady=3,
                  command=self._scout_edit_from_tree).pack(side="left", padx=4)

        # ── Tìm đường ──────────────────────────────────────────────────
        path_sec = self._sec(rf, "Tìm đường tới tọa độ")

        def _prow(label_text, sv_x, sv_y):
            r = tk.Frame(path_sec, bg=C["panel"]); r.pack(fill="x", pady=2, padx=2)
            tk.Label(r, text=label_text, bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 9), width=8, anchor="e").pack(side="left")
            tk.Entry(r, textvariable=sv_x, width=6,
                     bg=C["entry"], fg=C["text"], insertbackground="white",
                     relief="flat", font=("Segoe UI", 9)).pack(side="left", padx=2)
            tk.Label(r, text="Y:", bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 9)).pack(side="left", padx=(6,2))
            tk.Entry(r, textvariable=sv_y, width=6,
                     bg=C["entry"], fg=C["text"], insertbackground="white",
                     relief="flat", font=("Segoe UI", 9)).pack(side="left", padx=2)

        self._sv_path_sx = tk.StringVar()   # start X (để trống = dùng điểm mốc)
        self._sv_path_sy = tk.StringVar()   # start Y
        self._sv_path_x  = tk.StringVar()   # end X
        self._sv_path_y  = tk.StringVar()   # end Y
        _prow("Từ X:", self._sv_path_sx, self._sv_path_sy)
        tk.Label(path_sec, text="  (để trống = dùng điểm mốc)", bg=C["panel"],
                 fg=C["muted"], font=("Segoe UI", 7)).pack(anchor="w", padx=12)
        _prow("Đến X:", self._sv_path_x, self._sv_path_y)

        pr2 = tk.Frame(path_sec, bg=C["panel"]); pr2.pack(fill="x", pady=2, padx=2)
        tk.Label(pr2, text="Chế độ:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9), width=8, anchor="e").pack(side="left")
        self._sv_path_mode = tk.StringVar(value="fast")
        tk.Radiobutton(pr2, text="⚡ Nhanh", variable=self._sv_path_mode, value="fast",
                       bg=C["panel"], fg=C["text"], selectcolor=C["card"],
                       font=("Segoe UI", 9), activebackground=C["panel"]).pack(side="left", padx=4)
        tk.Radiobutton(pr2, text="🛡 An toàn", variable=self._sv_path_mode, value="safe",
                       bg=C["panel"], fg=C["text"], selectcolor=C["card"],
                       font=("Segoe UI", 9), activebackground=C["panel"]).pack(side="left", padx=4)

        pr3 = tk.Frame(path_sec, bg=C["panel"]); pr3.pack(fill="x", pady=3, padx=2)
        tk.Button(pr3, text="🔍 Tìm đường", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  command=self._scout_find_path).pack(side="left", padx=4)
        tk.Button(pr3, text="✖ Xóa đường", bg=C["card"], fg=C["muted"],
                  relief="flat", font=("Segoe UI", 9), padx=8, pady=4,
                  command=self._scout_clear_path).pack(side="left", padx=4)
        self._scout_path_lbl = tk.Label(path_sec, text="", bg=C["panel"],
                                         fg=C["yellow"], font=("Segoe UI", 8),
                                         wraplength=300, justify="left")
        self._scout_path_lbl.pack(fill="x", padx=4, pady=2)
        self._scout_path = []   # list of (gx,gy)

    _SCOUT_COLORS = {
        "lv1":        "#55efc4",
        "lv2":        "#00b894",
        "lv3":        "#00cec9",
        "lv4":        "#ffeaa7",
        "lv5":        "#fdcb6e",
        "ally":       "#0984e3",
        "enemy_city": "#d63031",
        "enemy":      "#ff7675",
        "terrain":    "#dfe6e9",
        "?":          "#b2bec3",
        "empty":      "#636e72",
    }

    def _scout_log(self, msg: str):
        """Append to scout log box with colour coding."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        # Choose tag
        ml = msg.lower()
        if "ocr" in ml:
            tag = "ocr"
        elif any(x in ml for x in ["✅", "xong", "→ nhãn"]):
            tag = "ok"
        elif any(x in ml for x in ["⚠️", "bỏ qua", "skip", "không"]):
            tag = "warn"
        elif any(x in ml for x in ["❌", "lỗi", "dừng"]):
            tag = "error"
        else:
            tag = "info"
        txt = self._scout_log_txt
        txt.configure(state="normal")
        txt.insert("end", line, tag)
        txt.see("end")
        txt.configure(state="disabled")

    def _scout_clear_log(self):
        self._scout_log_txt.configure(state="normal")
        self._scout_log_txt.delete("1.0", "end")
        self._scout_log_txt.configure(state="disabled")

    def _scout_start(self):
        try:
            ox    = int(self._sv_ox.get())
            oy    = int(self._sv_oy.get())
            up    = int(self._sv_up.get())
            down  = int(self._sv_down.get())
            left  = int(self._sv_left.get())
            right = int(self._sv_right.get())
        except ValueError:
            messagebox.showerror("Lỗi", "Các trường phải là số nguyên"); return

        # Dừng tất cả bot khác
        if hasattr(self, 'bot') and self.bot._thread and self.bot._thread.is_alive():
            self.bot.stop()
            self._log("[Scout] ⏸ Dừng Bot tấn công")
        for eng in getattr(self, '_wave_engines', []):
            if eng._thread and eng._thread.is_alive():
                eng.stop()
        if getattr(self, 'build_eng', None):
            try: self.build_eng.stop()
            except: pass

        self._scout_origin = (ox, oy)
        self._scout_status_lbl.config(text="▶ Đang dò...", fg=C["yellow"])
        self._scout_progress_lbl.config(text="")
        self._scout_btn_start.config(state="disabled")

        def _on_log(msg):
            self._log(msg)
            self.after(0, lambda m=msg: self._scout_log(m))

        self._scout_engine = ScoutEngine(
            bot=self.bot,
            origin_x=ox, origin_y=oy,
            up=up, down=down, left=left, right=right,
            on_log=_on_log,
            on_tile=self._scout_on_tile,
            on_done=self._scout_on_done,
        )
        self._scout_engine.start()

    def _scout_stop(self):
        if self._scout_engine:
            self._scout_engine.stop()
        self._scout_status_lbl.config(text="⏹ Dừng", fg=C["red"])
        self._scout_btn_start.config(state="normal")

    def _scout_on_tile(self, gx, gy, label, tile_name=""):
        self._scout_tiles[(gx, gy)] = label
        if not hasattr(self, "_scout_tile_names"):
            self._scout_tile_names = {}
        if tile_name:
            self._scout_tile_names[(gx, gy)] = tile_name
        done  = len(self._scout_tiles)
        eng   = self._scout_engine
        total = (eng.up + eng.down + 1) * (eng.left + eng.right + 1) if eng else done
        def _upd():
            disp = tile_name if tile_name else label
            self._scout_progress_lbl.config(
                text=f"Đã dò: {done}/{total} ô  |  ({gx},{gy}) = {disp}",
                fg=C["yellow"])
            self._scout_draw_map()
            self._scout_save_map()
            self._scout_refresh_table()
        self.after(0, _upd)

    def _scout_on_done(self):
        # Tap nút Back để đóng popup cuối cùng
        def _tap_back():
            try:
                self.bot.adb.tap(342, 1216)
                self._scout_log("[Scout] ↩ Tap Back (342,1216)")
            except Exception as e:
                self._scout_log(f"[Scout] ⚠️ Tap back lỗi: {e}")
        threading.Thread(target=_tap_back, daemon=True).start()

        def _upd():
            total = len(self._scout_tiles)
            self._scout_status_lbl.config(text="✅ Xong", fg=C["green"])
            self._scout_progress_lbl.config(text=f"Hoàn tất: {total} ô", fg=C["green"])
            self._scout_btn_start.config(state="normal")
        self.after(0, _upd)

    def _scout_clear(self):
        if not messagebox.askyesno("Xóa", "Xóa toàn bộ dữ liệu bản đồ?"): return
        self._scout_tiles.clear()
        getattr(self, "_scout_tile_names", {}).clear()
        self._scout_draw_map()
        self._scout_refresh_table()
        self._scout_status_lbl.config(text="● Chờ", fg=C["muted"])

    def _scout_export(self):
        if not self._scout_tiles:
            messagebox.showinfo("", "Chưa có dữ liệu"); return
        from tkinter.filedialog import asksaveasfilename
        path = asksaveasfilename(defaultextension=".csv",
                                  filetypes=[("CSV", "*.csv")],
                                  initialfile="scout_map.csv")
        if not path: return
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["X", "Y", "Label"])
            for (gx, gy), lbl in sorted(self._scout_tiles.items()):
                w.writerow([gx, gy, lbl])
        self._log(f"[Scout] 💾 Đã xuất {len(self._scout_tiles)} ô → {path}")

    _LABEL_OPTS = ["lv1","lv2","lv3","lv4","lv5","ally","enemy_city","enemy","terrain","?"]

    def _scout_edit_from_tree(self, event=None):
        """Sửa ô được chọn trong bảng."""
        sel = self._scout_tree.selection()
        if not sel: return
        vals = self._scout_tree.item(sel[0], "values")
        gx, gy = int(vals[0]), int(vals[1])
        self._scout_edit_tile_dialog(gx, gy)

    def _scout_canvas_click(self, event):
        """Click trên bản đồ → tính ô tương ứng → mở dialog."""
        if not self._scout_tiles and not hasattr(self, "_scout_origin"):
            return
        ox, oy = self._scout_origin
        cell = self._scout_cell
        c    = self._scout_canvas
        cw   = c.winfo_width()  or 600
        ch   = c.winfo_height() or 300

        xs = [gx for gx, gy in self._scout_tiles] or [ox]
        ys = [gy for gx, gy in self._scout_tiles] or [oy]
        min_x = min(xs + [ox]); max_x = max(xs + [ox])
        min_y = min(ys + [oy]); max_y = max(ys + [oy])

        total_w = (max_x - min_x + 1) * cell
        total_h = (max_y - min_y + 1) * cell
        off_x   = (cw - total_w) // 2
        off_y   = (ch - total_h) // 2

        gx = min_x + (event.x - off_x) // cell
        gy = max_y - (event.y - off_y) // cell

        if (gx, gy) in self._scout_tiles or (gx, gy) == (ox, oy):
            self._scout_edit_tile_dialog(gx, gy)

    def _scout_edit_tile_dialog(self, gx, gy):
        """Dialog sửa Loại + Mô tả cho ô (gx, gy)."""
        ox, oy     = getattr(self, "_scout_origin", (None, None))
        name_store = getattr(self, "_scout_tile_names", {})
        cur_label  = self._scout_tiles.get((gx, gy), "?")
        cur_name   = name_store.get((gx, gy), "")
        is_origin  = (gx, gy) == (ox, oy)

        dlg = tk.Toplevel(self)
        dlg.title(f"Sửa ô ({gx}, {gy})")
        dlg.configure(bg=C["bg"])
        dlg.resizable(False, False)
        dlg.grab_set()

        pad = dict(padx=10, pady=5)

        # Header
        header = f"({gx}, {gy})"
        if is_origin: header += "  ★ Điểm mốc"
        tk.Label(dlg, text=header, bg=C["bg"], fg=C["accent"],
                 font=("Segoe UI", 11, "bold")).grid(row=0, column=0,
                 columnspan=2, sticky="w", **pad)

        # Loại
        tk.Label(dlg, text="Loại:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="e", **pad)
        lbl_var = tk.StringVar(value=cur_label)
        cb = ttk.Combobox(dlg, textvariable=lbl_var,
                          values=self._LABEL_OPTS, state="readonly", width=14)
        cb.grid(row=1, column=1, sticky="w", **pad)

        # Màu preview
        color_lbl = tk.Label(dlg, bg=self._SCOUT_COLORS.get(cur_label,"#636e72"),
                              width=3, relief="flat")
        color_lbl.grid(row=1, column=2, padx=(0,10))
        def _upd_color(*_):
            color_lbl.config(bg=self._SCOUT_COLORS.get(lbl_var.get(),"#636e72"))
        lbl_var.trace_add("write", _upd_color)

        # Mô tả
        tk.Label(dlg, text="Mô tả:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="e", **pad)
        name_var = tk.StringVar(value=cur_name)
        tk.Entry(dlg, textvariable=name_var, width=20,
                 bg=C["entry"], fg=C["text"], insertbackground="white",
                 relief="flat", font=("Segoe UI", 9)).grid(row=2, column=1,
                 columnspan=2, sticky="ew", **pad)

        # Buttons
        btn_row = tk.Frame(dlg, bg=C["bg"]); btn_row.grid(
            row=3, column=0, columnspan=3, pady=(4,10))

        def _save():
            new_lbl  = lbl_var.get()
            new_name = name_var.get().strip()
            self._scout_tiles[(gx, gy)] = new_lbl
            if not hasattr(self, "_scout_tile_names"):
                self._scout_tile_names = {}
            if new_name:
                self._scout_tile_names[(gx, gy)] = new_name
            elif (gx, gy) in self._scout_tile_names:
                del self._scout_tile_names[(gx, gy)]
            self._scout_save_map()
            self._scout_refresh_table()
            self._scout_draw_map()
            self._scout_log(f"[Scout] ✏️ Sửa ({gx},{gy}): {new_lbl} | {new_name}")
            dlg.destroy()

        tk.Button(btn_row, text="💾 Lưu", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"),
                  padx=14, pady=4, command=_save).pack(side="left", padx=6)
        tk.Button(btn_row, text="Huỷ", bg=C["card"], fg=C["muted"],
                  relief="flat", font=("Segoe UI", 9),
                  padx=10, pady=4, command=dlg.destroy).pack(side="left", padx=4)

        # Center dialog
        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - dlg.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    def _scout_refresh_table(self):
        """Cập nhật bảng danh sách ô đã dò."""
        if not hasattr(self, "_scout_tree"): return
        tree = self._scout_tree
        flt  = getattr(self, "_scout_filter_var", None)
        flt_val = flt.get() if flt else "Tất cả"

        # Lọc và sort
        items = list(self._scout_tiles.items())
        if flt_val != "Tất cả":
            items = [(k, v) for k, v in items if v == flt_val]

        col = getattr(self, "_scout_sort_col", "X")
        rev = getattr(self, "_scout_sort_rev", False)
        if col == "X":
            items.sort(key=lambda i: i[0][0], reverse=rev)
        elif col == "Y":
            items.sort(key=lambda i: i[0][1], reverse=rev)
        elif col in ("Loại", "Mô tả"):
            items.sort(key=lambda i: i[1], reverse=rev)

        # Xóa và vẽ lại
        tree.delete(*tree.get_children())
        desc_map   = getattr(self, "_LABEL_DESC", {})
        name_store = getattr(self, "_scout_tile_names", {})
        ox, oy     = getattr(self, "_scout_origin", (None, None))
        for (gx, gy), lbl in items:
            tile_name = name_store.get((gx, gy), "")
            desc = tile_name if tile_name else desc_map.get(lbl, lbl)
            if (gx, gy) == (ox, oy):
                desc = f"{desc} (Điểm mốc)" if desc else "Điểm mốc"
            tree.insert("", "end", values=(gx, gy, lbl, desc),
                        tags=(lbl,))

        # Update count
        if hasattr(self, "_scout_tbl_count"):
            total_all = len(self._scout_tiles)
            shown = len(items)
            if flt_val == "Tất cả":
                self._scout_tbl_count.config(text=f"{total_all} ô")
            else:
                self._scout_tbl_count.config(text=f"{shown}/{total_all} ô")

    def _scout_sort_table(self, col):
        """Toggle sort direction khi click header."""
        if getattr(self, "_scout_sort_col", None) == col:
            self._scout_sort_rev = not getattr(self, "_scout_sort_rev", False)
        else:
            self._scout_sort_col = col
            self._scout_sort_rev = False
        self._scout_refresh_table()

    def _scout_copy_table(self):
        """Copy danh sách ô đã dò vào clipboard dạng X,Y,Loại."""
        if not self._scout_tiles:
            messagebox.showinfo("", "Chưa có dữ liệu"); return
        flt_val = getattr(self, "_scout_filter_var", None)
        flt_val = flt_val.get() if flt_val else "Tất cả"
        items = list(self._scout_tiles.items())
        if flt_val != "Tất cả":
            items = [(k, v) for k, v in items if v == flt_val]
        items.sort(key=lambda i: (i[0][0], i[0][1]))
        lines = ["X,Y,Loại,Mô tả"]
        desc_map  = getattr(self, "_LABEL_DESC", {})
        name_store = getattr(self, "_scout_tile_names", {})
        for (gx, gy), lbl in items:
            tile_name = name_store.get((gx, gy), "")
            desc = tile_name if tile_name else desc_map.get(lbl, lbl)
            lines.append(f"{gx},{gy},{lbl},{desc}")
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._scout_log(f"[Scout] 📋 Đã copy {len(items)} ô vào clipboard")

    # ── BLOCKED sets cho từng mode ──────────────────────────────────
    _PATH_BLOCKED_FAST = frozenset({"enemy_city","enemy","ally","terrain","?","empty",""})
    _PATH_BLOCKED_SAFE = frozenset({"enemy_city","enemy","ally","lv3","lv4","lv5","terrain","?","empty",""})

    # ─────────────────────────────────────────────────────────────
    # PATTERN: SPIRAL SCOUT
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _gen_spiral(ox, oy, rings, start_dir="down"):
        """
        Sinh toạ độ xoắn ốc CW từ (ox,oy).
        Pattern cạnh: D1, L3, U5, R5, D7, L7, U9, R9...
        Công thức: side_len(n) = 1 nếu n=0, còn lại = 2*(n//2+1)+1
        Game coords: Y giảm = đi xuống map.
        """
        DIR = {"down":(0,-1),"up":(0,1),"left":(-1,0),"right":(1,0)}
        CW_ORDER = {
            "down":  ["down","left","up","right"],
            "up":    ["up","right","down","left"],
            "left":  ["left","up","right","down"],
            "right": ["right","down","left","up"],
        }
        order = CW_ORDER.get(start_dir, CW_ORDER["down"])
        coords = []
        x, y = ox, oy
        max_segs = rings * 4 + 1

        for n in range(max_segs):
            side_len = 1 if n == 0 else 2*(n//2 + 1) + 1
            d = order[n % 4]
            dx, dy = DIR[d]
            for _ in range(side_len):
                x += dx; y += dy
                coords.append((x, y))
            # Dừng sau khi hoàn thành cạnh thứ 4 của vòng cuối
            if n > 0 and n % 4 == 3 and (n + 1) // 4 >= rings:
                break

        return coords

    def _spiral_calc_only(self):
        """Tính toạ độ xoắn ốc và hiển thị dialog — không cần ADB."""
        try:
            ox = int(self._sv_sp_x.get() or self._sv_ox.get())
            oy = int(self._sv_sp_y.get() or self._sv_oy.get())
            rings = max(1, int(self._sv_sp_rings.get() or 3))
        except ValueError:
            self._sp_status_lbl.config(text="❌ Nhập gốc X/Y và số vòng", fg=C["red"])
            return

        d = self._sv_sp_dir.get()
        coords = self._gen_spiral(ox, oy, rings, d)
        total  = len(coords)
        secs   = total * 30
        h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)

        # Build text list
        lines = [f"{x},{y}" for x, y in coords]
        text_body = "\n".join(lines)
        d30 = chr(8212)*30
        summary = f"Goc ({ox},{oy}) | Huong: {d} | {rings} vong\nTong: {total} o ~{h}g {m}p {s}s\n{d30}\n"






        win = tk.Toplevel(self)
        win.title(f"🌀 Tọa độ xoắn ốc ({total} ô)")
        win.configure(bg=C["bg"])
        win.geometry("300x480")
        win.resizable(True, True)

        tk.Label(win, text=summary, bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 8), justify="left",
                 anchor="w", pady=4, padx=8).pack(fill="x")

        txt_frame = tk.Frame(win, bg=C["bg"]); txt_frame.pack(fill="both", expand=True, padx=6, pady=4)
        sb = tk.Scrollbar(txt_frame); sb.pack(side="right", fill="y")
        txt = tk.Text(txt_frame, bg=C["entry"], fg=C["text"], font=("Consolas", 9),
                      relief="flat", yscrollcommand=sb.set, width=28)
        txt.pack(side="left", fill="both", expand=True)
        sb.config(command=txt.yview)
        txt.insert("1.0", text_body)
        txt.config(state="disabled")

        btn_row = tk.Frame(win, bg=C["bg"]); btn_row.pack(pady=6)

        def _copy():
            win.clipboard_clear()
            win.clipboard_append(text_body)
            copy_btn.config(text="✅ Đã copy!")
            win.after(1500, lambda: copy_btn.config(text="📋 Copy"))

        def _import_to_attack():
            added = 0
            for x, y in coords:
                pt = AttackPoint(idx=len(self.pts), game_x=x, game_y=y, label="", status="waiting")
                self.pts.append(pt)
                added += 1
            self._refresh_tree()
            import_btn.config(text=f"✅ Đã import {added} điểm!")
            win.after(2000, lambda: import_btn.config(text="📥 Import → Tab Tấn công"))

        def _import_to_spy():
            added = 0
            for x, y in coords:
                if not any(c[0]==x and c[1]==y for c in self._spy_coords):
                    self._spy_coords.append([x, y, f"Spiral {d}"])
                    added += 1
            self._spy_refresh_tree()
            spy_btn.config(text=f"✅ Import {added} → Spy!")
            win.after(2000, lambda: spy_btn.config(text="🔍 Import → Tab Do thám"))

        copy_btn = tk.Button(btn_row, text="📋 Copy", bg=C["muted"], fg="white",
                             relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                             command=_copy)
        copy_btn.pack(side="left", padx=4)

        import_btn = tk.Button(btn_row, text="📥 Import → Tab Tấn công",
                               bg=C["accent"], fg="white", relief="flat",
                               font=("Segoe UI", 8, "bold"), padx=8, pady=4,
                               command=_import_to_attack)
        import_btn.pack(side="left", padx=4)

        btn_row2 = tk.Frame(win, bg=C["bg"]); btn_row2.pack(pady=2)
        spy_btn = tk.Button(btn_row2, text="🔍 Import → Tab Do thám",
                            bg=C["green"], fg="#111", relief="flat",
                            font=("Segoe UI", 8, "bold"), padx=8, pady=4,
                            command=_import_to_spy)
        spy_btn.pack(side="left", padx=4)

        tk.Button(btn_row2, text="Đóng", bg=C["card"], fg=C["muted"],
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=4,
                  command=win.destroy).pack(side="left", padx=4)

        self._sp_status_lbl.config(
            text=f"📋 {total} ô | ~{h}g{m}p{s}s", fg=C["accent"])

    def _spiral_preview(self):
        """Vẽ preview xoắn ốc lên bản đồ không cần ADB."""
        try:
            ox = int(self._sv_sp_x.get() or self._sv_ox.get())
            oy = int(self._sv_sp_y.get() or self._sv_oy.get())
            rings = max(1, int(self._sv_sp_rings.get() or 3))
        except ValueError:
            self._sp_status_lbl.config(text="❌ Nhập gốc X/Y và số vòng", fg=C["red"])
            return
        d = self._sv_sp_dir.get()
        coords = self._gen_spiral(ox, oy, rings, d)
        # Đánh dấu lên bản đồ như "empty" để thấy pattern
        for i, (gx, gy) in enumerate(coords):
            if (gx, gy) not in self._scout_tiles:
                self._scout_tiles[(gx, gy)] = "empty"
        self._scout_origin = (ox, oy)
        self._scout_draw_map()
        total = len(coords)
        secs = total * 30
        h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
        self._sp_status_lbl.config(
            text=f"👁 Preview: {total} ô · ~{h}g{m}p{s}s", fg=C["accent"])
        self._sp_progress_lbl.config(
            text=f"Vòng {rings}: {total} ô theo hướng {d}", fg=C["muted"])

    def _spiral_scout_start(self):
        if self._sp_running: return
        try:
            ox = int(self._sv_sp_x.get() or self._sv_ox.get())
            oy = int(self._sv_sp_y.get() or self._sv_oy.get())
            rings = max(1, int(self._sv_sp_rings.get() or 3))
        except ValueError:
            self._sp_status_lbl.config(text="❌ Nhập gốc X/Y và số vòng", fg=C["red"])
            return
        if not self.bot.adb.ok:
            self._sp_status_lbl.config(text="❌ Chưa kết nối ADB", fg=C["red"]); return
        self._sp_stop_evt.clear()
        self._sp_running = True
        self._sp_btn_start.config(state="disabled")
        self._sp_status_lbl.config(text="🌀 Đang dò xoắn ốc...", fg=C["yellow"])
        d = self._sv_sp_dir.get()
        threading.Thread(target=self._spiral_scout_run,
                         args=(ox, oy, rings, d), daemon=True).start()

    def _spiral_scout_stop(self):
        self._sp_stop_evt.set()

    def _spiral_scout_run(self, ox, oy, rings, start_dir):
        coords = self._gen_spiral(ox, oy, rings, start_dir)
        total  = len(coords)
        tiles  = self._scout_tiles
        names  = getattr(self, "_scout_tile_names", {})
        log    = self._scout_log
        first_tap = True

        log(f"[Spiral] 🌀 {start_dir} | gốc ({ox},{oy}) | {rings} vòng | {total} ô")
        self.after(0, lambda: setattr(self, "_scout_origin", (ox, oy)))

        for i, (gx, gy) in enumerate(coords):
            if self._sp_stop_evt.is_set():
                log("[Spiral] ⏹ Dừng")
                break

            step_txt = f"{i+1}/{total}: ({gx},{gy})"
            self.after(0, lambda t=step_txt: self._sp_progress_lbl.config(
                text=t, fg=C["yellow"]))
            log(f"[Spiral] {step_txt}")

            try:
                force = 1 if first_tap else 2
                first_tap = False
                self.bot.navigate_to(gx, gy, force_tap=force)
                if self._sp_stop_evt.is_set(): break
                import time as _t; _t.sleep(0.5)

                raw = self.bot.adb.screenshot_bytes()
                label, tile_name = "?", ""
                if raw:
                    import numpy as np, cv2, re as _re
                    try: import pytesseract as _pt
                    except ImportError: _pt = None
                    if _pt:
                        arr = np.frombuffer(raw, np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        h2, w2 = img.shape[:2]
                        crop = img[int(h2*0.25):int(h2*0.62), int(w2*0.15):int(w2*0.90)]
                        rh, rw = crop.shape[:2]
                        cr3  = cv2.resize(crop, (rw*3, rh*3), interpolation=cv2.INTER_CUBIC)
                        gray = cv2.cvtColor(cr3, cv2.COLOR_BGR2GRAY)
                        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
                        text = _pt.image_to_string(th, config="--psm 6")
                        tlow = text.lower()
                        if   "march"   in tlow:                            label = "ally"
                        elif "info"    in tlow and "conquer" in tlow:      label = "enemy_city"
                        elif "conquer" in tlow and "enter" not in tlow:    label = "enemy"
                        elif any(k in tlow for k in ["lake","hills","mountains","river","bridge"]):
                            label = "terrain"
                        elif "wasteland" in tlow: label = "lv1"
                        else:
                            for lv in [5,4,3,2]:
                                if _re.search(rf"lv\.?{lv}\b", text, _re.IGNORECASE):
                                    label = f"lv{lv}"; break
                        for _line in text.splitlines():
                            _line = _line.strip()
                            if len(_line) >= 4 and not _re.match(r"^\d", _line):
                                tile_name = _line; break

                tiles[(gx, gy)]  = label
                if tile_name: names[(gx, gy)] = tile_name
                self.after(0, lambda gx=gx, gy=gy, lb=label, tn=tile_name:
                    self._scout_on_tile(gx, gy, lb, tn))
                log(f"[Spiral]   → {label}" + (f" | {tile_name}" if tile_name else ""))

            except Exception as e:
                log(f"[Spiral] ❌ Lỗi ({gx},{gy}): {e}")

            import time as _t; _t.sleep(0.2)

        else:
            log(f"[Spiral] ✅ Hoàn tất {total} ô")
            self._scout_save_map()

        self._sp_running = False
        self.after(0, lambda: (
            self._sp_status_lbl.config(text="✅ Xong", fg=C["green"]),
            self._sp_btn_start.config(state="normal")
        ))

        # ─────────────────────────────────────────────────────────────
    # TỰ DÒ ĐƯỜNG THỰC TẾ (online BFS + ADB navigate)
    # ─────────────────────────────────────────────────────────────

    def _auto_path_start(self):
        if self._ap_running:
            return
        try:
            x1 = int(self._sv_ap_x1.get()); y1 = int(self._sv_ap_y1.get())
            x2 = int(self._sv_ap_x2.get()); y2 = int(self._sv_ap_y2.get())
        except ValueError:
            self._ap_status_lbl.config(text="❌ Nhập đủ 4 toạ độ", fg=C["red"]); return
        if not self.bot.adb.ok:
            self._ap_status_lbl.config(text="❌ Chưa kết nối ADB", fg=C["red"]); return
        self._ap_stop_evt.clear()
        self._ap_running = True
        self._ap_btn_start.config(state="disabled")
        self._ap_status_lbl.config(text="🔍 Đang dò...", fg=C["yellow"])
        mode = self._sv_ap_mode.get()
        threading.Thread(target=self._auto_path_run,
                         args=(x1, y1, x2, y2, mode), daemon=True).start()

    def _auto_path_stop(self):
        self._ap_stop_evt.set()

    def _auto_path_run(self, x1, y1, x2, y2, mode):
        """
        Online A*: dò từng ô bằng ADB, OCR, cập nhật bản đồ và tìm đường.
        - Frontier = min-heap ưu tiên ô gần goal nhất (Manhattan)
        - Nếu ô bị chặn (theo mode) → bỏ qua, thử ô khác
        - Dừng khi reach goal hoặc frontier rỗng
        """
        import heapq
        log   = self._scout_log
        tiles = self._scout_tiles
        names = getattr(self, "_scout_tile_names", {})

        blocked_fast = frozenset({"enemy_city","enemy","ally","terrain"})
        blocked_safe = frozenset({"enemy_city","enemy","ally","terrain","lv3","lv4","lv5"})
        blocked = blocked_safe if mode == "safe" else blocked_fast
        mode_txt = "🛡 An toàn" if mode == "safe" else "⚡ Nhanh"

        def manhattan(ax, ay): return abs(ax - x2) + abs(ay - y2)
        def update_ui(msg, color=None):
            self.after(0, lambda: self._ap_progress_lbl.config(
                text=msg, fg=color or C["yellow"]))

        goal  = (x2, y2)
        start = (x1, y1)
        heap  = []                # (priority, (gx,gy))
        came_from = {}            # (gx,gy) -> parent (gx,gy) or None
        ocr_cache = {}            # tiles we've personally OCR'd this run
        first_tap = True

        heapq.heappush(heap, (manhattan(x1,y1), start))
        came_from[start] = None
        step = 0

        log(f"[AutoPath] {mode_txt} từ ({x1},{y1}) đến ({x2},{y2})")

        while heap and not self._ap_stop_evt.is_set():
            _, cur = heapq.heappop(heap)
            gx, gy = cur

            # Nếu ô này đã biết label và bị chặn → skip
            known = tiles.get(cur, "")
            if known in blocked:
                continue

            # Dò ô này bằng ADB nếu chưa OCR trong run này
            if cur not in ocr_cache:
                step += 1
                update_ui(f"Bước {step}: ({gx},{gy}) → đang OCR...")
                log(f"[AutoPath] {step}: navigate ({gx},{gy})")

                try:
                    force = 1 if first_tap else 2
                    first_tap = False
                    self.bot.navigate_to(gx, gy, force_tap=force)
                    if self._ap_stop_evt.is_set(): break
                    time.sleep(0.5)

                    # OCR bằng ScoutEngine._ocr_tile logic (inline)
                    raw = self.bot.adb.screenshot_bytes()
                    label, tile_name = "?", ""
                    if raw:
                        import numpy as np, cv2, re as _re
                        try: import pytesseract as _pt
                        except ImportError: _pt = None
                        if _pt:
                            arr = np.frombuffer(raw, np.uint8)
                            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            h, w = img.shape[:2]
                            crop = img[int(h*0.25):int(h*0.62),
                                       int(w*0.15):int(w*0.90)]
                            rh, rw = crop.shape[:2]
                            cr3  = cv2.resize(crop, (rw*3, rh*3),
                                              interpolation=cv2.INTER_CUBIC)
                            gray = cv2.cvtColor(cr3, cv2.COLOR_BGR2GRAY)
                            _, th = cv2.threshold(gray, 0, 255,
                                                  cv2.THRESH_BINARY+cv2.THRESH_OTSU)
                            text = _pt.image_to_string(th, config="--psm 6")
                            tlow = text.lower()
                            # classify
                            if   "march"   in tlow:                       label = "ally"
                            elif "info"    in tlow and "conquer" in tlow: label = "enemy_city"
                            elif "conquer" in tlow and "enter" not in tlow: label = "enemy"
                            elif any(k in tlow for k in ["lake","hills","mountains","river","bridge"]):
                                label = "terrain"
                            elif "wasteland" in tlow: label = "lv1"
                            else:
                                for lv in [5,4,3,2]:
                                    if _re.search(rf"lv\.?{lv}\b", text, _re.IGNORECASE):
                                        label = f"lv{lv}"; break
                            # extract name (inline)
                            for _line in text.splitlines():
                                _line = _line.strip()
                                if len(_line) >= 4 and not _re.match(r'^\d', _line):
                                    tile_name = _line; break

                    ocr_cache[cur] = label
                    tiles[cur]      = label
                    if tile_name: names[cur] = tile_name
                    # Update map + table
                    self.after(0, lambda gx=gx, gy=gy, lb=label, tn=tile_name:
                        self._scout_on_tile(gx, gy, lb, tn))

                    log(f"[AutoPath]   → {label}" + (f" | {tile_name}" if tile_name else ""))
                    danger_txt = ""
                    if label in blocked:
                        danger_txt = f" ⛔ {label} — thử đường khác"
                    update_ui(f"Bước {step}: ({gx},{gy}) = {label}{danger_txt}",
                              C["red"] if label in blocked else C["yellow"])

                except Exception as e:
                    log(f"[AutoPath] ❌ Lỗi ({gx},{gy}): {e}")
                    ocr_cache[cur] = "?"
                    continue
            else:
                label = ocr_cache[cur]

            if self._ap_stop_evt.is_set(): break

            # Nếu bị chặn → không mở rộng
            if label in blocked:
                continue

            # Đến đích!
            if cur == goal:
                # Reconstruct path
                path = []
                node = goal
                while node is not None:
                    path.append(node)
                    node = came_from[node]
                path.reverse()
                steps = len(path) - 1
                log(f"[AutoPath] ✅ Tìm được đường! {steps} bước")
                self.after(0, lambda p=path, s=steps, m=mode_txt: (
                    self._ap_status_lbl.config(
                        text=f"✅ Tìm được đường! {s} bước", fg=C["green"]),
                    self._ap_progress_lbl.config(
                        text=" → ".join(f"({x},{y})" for x,y in p[:5]) +
                             (f" ...({p[-1][0]},{p[-1][1]})" if len(p)>5 else ""),
                        fg=C["green"]),
                    setattr(self, "_scout_path", p),
                    self._scout_draw_map(),
                    self._scout_path_import_dialog(p)
                ))
                self._ap_running = False
                self.after(0, lambda: self._ap_btn_start.config(state="normal"))
                return

            # Thêm hàng xóm vào frontier theo thứ tự ưu tiên Manhattan
            for dx, dy in [(0,-1),(0,1),(-1,0),(1,0)]:
                nb = (gx+dx, gy+dy)
                if nb not in came_from:
                    nb_lbl = tiles.get(nb, "")
                    if nb_lbl not in blocked:
                        came_from[nb] = cur
                        heapq.heappush(heap, (manhattan(gx+dx, gy+dy), nb))

            time.sleep(0.1)

        # Hết frontier hoặc bị dừng
        if self._ap_stop_evt.is_set():
            log("[AutoPath] ⏹ Dừng")
            self.after(0, lambda: self._ap_status_lbl.config(
                text="⏹ Đã dừng", fg=C["muted"]))
        else:
            log(f"[AutoPath] ⛔ Không tìm được đường từ ({x1},{y1}) đến ({x2},{y2})")
            self.after(0, lambda: self._ap_status_lbl.config(
                text=f"⛔ Không tìm được đường", fg=C["red"]))

        self._ap_running = False
        self.after(0, lambda: self._ap_btn_start.config(state="normal"))

    def _scout_find_path(self):
        """BFS tìm đường ngắn nhất từ điểm mốc tới tọa độ đích."""
        try:
            tx = int(self._sv_path_x.get())
            ty = int(self._sv_path_y.get())
        except ValueError:
            self._scout_path_lbl.config(text="❌ Tọa độ đích không hợp lệ", fg=C["red"])
            return

        # Điểm xuất phát: dùng ô nhập "Từ X/Y" nếu có, không thì dùng điểm mốc
        sx_str = self._sv_path_sx.get().strip() if hasattr(self, "_sv_path_sx") else ""
        sy_str = self._sv_path_sy.get().strip() if hasattr(self, "_sv_path_sy") else ""
        if sx_str and sy_str:
            try:
                ox, oy = int(sx_str), int(sy_str)
            except ValueError:
                self._scout_path_lbl.config(text="❌ Toạ độ xuất phát không hợp lệ", fg=C["red"])
                return
        else:
            ox, oy = getattr(self, "_scout_origin", (None, None))
            if ox is None:
                self._scout_path_lbl.config(text="❌ Chưa có điểm mốc", fg=C["red"])
                return

        mode = getattr(self, "_sv_path_mode", None)
        mode = mode.get() if mode else "fast"
        blocked = self._PATH_BLOCKED_SAFE if mode == "safe" else self._PATH_BLOCKED_FAST
        tiles   = self._scout_tiles

        # BFS
        from collections import deque
        queue   = deque()
        visited = {}
        start   = (ox, oy)
        goal    = (tx, ty)
        queue.append(start)
        visited[start] = None   # parent map

        found = False
        while queue:
            cur = queue.popleft()
            if cur == goal:
                found = True
                break
            cx, cy = cur
            for dx, dy in [(0,1),(0,-1),(1,0),(-1,0)]:
                nb = (cx+dx, cy+dy)
                if nb in visited:
                    continue
                lbl = tiles.get(nb, "")   # "" = chưa dò
                if lbl in blocked:
                    continue
                visited[nb] = cur
                queue.append(nb)

        if not found:
            self._scout_path = []
            self._scout_path_lbl.config(
                text=f"⛔ Không tìm được đường tới ({tx},{ty}) — [{mode}]",
                fg=C["red"])
            self._scout_draw_map()
            return

        # Trace back path
        path = []
        node = goal
        while node is not None:
            path.append(node)
            node = visited[node]
        path.reverse()
        self._scout_path = path

        mode_txt = "⚡ Nhanh" if mode == "fast" else "🛡 An toàn"
        steps = len(path) - 1
        coords_str = " → ".join(f"({x},{y})" for x,y in path[:6])
        if len(path) > 6: coords_str += f" ... ({path[-1][0]},{path[-1][1]})"
        self._scout_path_lbl.config(
            text=f"\u2705 {mode_txt} | {steps} b\u01b0\u1edbc\n{coords_str}", fg=C["green"])
        start_lbl = f"({ox},{oy})" if (sx_str and sy_str) else f"mốc({ox},{oy})"
        self._scout_log(f"[Path] {mode_txt} {start_lbl}→({tx},{ty}): {steps} bước")
        self._scout_draw_map()
        self._scout_path_import_dialog(path)

    def _scout_path_import_dialog(self, path):
        """Dialog xuất danh sách tọa độ đường đi, cho phép import vào Tab Tấn công."""
        name_store = getattr(self, "_scout_tile_names", {})
        desc_map   = getattr(self, "_LABEL_DESC", {})
        ox, oy     = getattr(self, "_scout_origin", (None, None))

        # Tạo danh sách dòng x,y,mô tả (bỏ điểm mốc xuất phát)
        lines_data = []
        for gx, gy in path:
            tile_name = name_store.get((gx, gy), "")
            lbl       = self._scout_tiles.get((gx, gy), "")
            desc      = tile_name if tile_name else desc_map.get(lbl, lbl)
            if (gx, gy) == (ox, oy):
                desc = (desc + " (Điểm mốc)").strip()
            lines_data.append((gx, gy, desc))

        dlg = tk.Toplevel(self)
        dlg.title(f"Xuất đường đi — {len(path)} điểm")
        dlg.configure(bg=C["bg"])
        dlg.resizable(True, True)
        dlg.geometry("380x420")
        dlg.grab_set()

        # Header
        hdr = tk.Frame(dlg, bg=C["bg"]); hdr.pack(fill="x", padx=10, pady=(10,4))
        tk.Label(hdr, text=f"📍 {len(path)} điểm  |  {len(path)-1} bước",
                 bg=C["bg"], fg=C["accent"],
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(hdr, text="(chỉnh sửa tuỳ ý trước khi import)",
                 bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left", padx=8)

        # Text box
        txt_frame = tk.Frame(dlg, bg=C["bg"])
        txt_frame.pack(fill="both", expand=True, padx=10, pady=4)
        txt = tk.Text(txt_frame, bg=C["entry"], fg=C["text"],
                      insertbackground="white", relief="flat",
                      font=("Consolas", 9), wrap="none")
        sb_y = tk.Scrollbar(txt_frame, command=txt.yview)
        txt.configure(yscrollcommand=sb_y.set)
        sb_y.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        content_str = "\n".join(f"{gx},{gy},{desc}" for gx, gy, desc in lines_data)
        txt.insert("1.0", content_str)

        # Buttons
        btn_row = tk.Frame(dlg, bg=C["bg"]); btn_row.pack(fill="x", padx=10, pady=(4,10))

        def _import():
            raw = txt.get("1.0", "end").strip()
            imported = 0
            errors   = []
            new_pts  = list(self.pts)
            existing = {(p.game_x, p.game_y) for p in new_pts}
            for ln_no, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line: continue
                parts = line.split(",", 2)
                if len(parts) < 2:
                    errors.append(f"Dòng {ln_no}: '{line}'"); continue
                try:
                    gx_i, gy_i = int(parts[0]), int(parts[1])
                    lbl_i = parts[2].strip() if len(parts) > 2 else ""
                except ValueError:
                    errors.append(f"Dòng {ln_no}: '{line}'"); continue
                if (gx_i, gy_i) in existing: continue
                new_pts.append(AttackPoint(
                    idx=len(new_pts)+1, game_x=gx_i, game_y=gy_i, label=lbl_i))
                existing.add((gx_i, gy_i))
                imported += 1
            self.pts = new_pts
            self._refresh_tree()
            msg = f"✅ Import {imported} điểm vào Tab Tấn công"
            if errors:
                msg += f"\n\u26a0\ufe0f B\u1ecf qua {len(errors)} d\u00f2ng l\u1ed7i"
            self._log(f"[Path→Attack] {msg}")
            messagebox.showinfo("Import xong", msg)
            dlg.destroy()

        def _copy():
            self.clipboard_clear()
            self.clipboard_append(txt.get("1.0", "end").strip())
            self._scout_log("[Path] 📋 Đã copy tọa độ đường đi")

        tk.Button(btn_row, text="📥 Import vào Tab Tấn công",
                  bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=12, pady=5,
                  command=_import).pack(side="left", padx=(0,6))
        tk.Button(btn_row, text="📋 Copy",
                  bg=C["card"], fg=C["text"], relief="flat",
                  font=("Segoe UI", 9), padx=8, pady=5,
                  command=_copy).pack(side="left", padx=4)
        tk.Button(btn_row, text="Đóng",
                  bg=C["card"], fg=C["muted"], relief="flat",
                  font=("Segoe UI", 9), padx=8, pady=5,
                  command=dlg.destroy).pack(side="right")

        # Center
        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - dlg.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    def _scout_clear_path(self):
        self._scout_path = []
        self._scout_path_lbl.config(text="", fg=C["yellow"])
        self._scout_draw_map()

    def _scout_draw_map(self):
        c = self._scout_canvas
        c.delete("all")
        if not self._scout_tiles and not hasattr(self, '_scout_origin'):
            return
        ox, oy = self._scout_origin
        cell   = self._scout_cell
        cw     = c.winfo_width()  or 600
        ch     = c.winfo_height() or 300

        # Compute min/max extent
        xs = [gx for gx, gy in self._scout_tiles] or [ox]
        ys = [gy for gx, gy in self._scout_tiles] or [oy]
        min_x, max_x = min(xs + [ox]), max(xs + [ox])
        min_y, max_y = min(ys + [oy]), max(ys + [oy])

        # Offset so map is centred
        total_w = (max_x - min_x + 1) * cell
        total_h = (max_y - min_y + 1) * cell
        off_x   = (cw - total_w) // 2
        off_y   = (ch - total_h) // 2

        def _px(gx, gy):
            return (off_x + (gx - min_x) * cell,
                    off_y + (max_y - gy) * cell)

        # Draw known tiles
        for (gx, gy), lbl in self._scout_tiles.items():
            px, py = _px(gx, gy)
            color  = self._SCOUT_COLORS.get(lbl, self._SCOUT_COLORS["?"])
            c.create_rectangle(px, py, px+cell-1, py+cell-1,
                                fill=color, outline="#111", width=1)
            if cell >= 14:
                txt = lbl.replace("lv","") if lbl != "?" else "?"
                c.create_text(px + cell//2, py + cell//2,
                              text=txt, fill="white",
                              font=("Segoe UI", max(6, cell//2 - 1)))

        # Draw origin marker
        px, py = _px(ox, oy)
        c.create_rectangle(px, py, px+cell-1, py+cell-1,
                            fill="", outline="#ffcc00", width=2)
        c.create_text(px + cell//2, py + cell//2, text="★",
                      fill="#ffcc00", font=("Segoe UI", max(7, cell//2)))

        # Draw path overlay
        path = getattr(self, "_scout_path", [])
        if len(path) >= 2:
            # Tô màu từng ô trên đường đi
            for step, (gx, gy) in enumerate(path):
                px2, py2 = _px(gx, gy)
                if step == 0:          # start (mốc)
                    color = "#ffcc00"
                elif step == len(path)-1:  # đích
                    color = "#fd79a8"
                else:
                    color = "#a29bfe"  # tím nhạt = đường đi
                c.create_rectangle(px2, py2, px2+cell-1, py2+cell-1,
                                   fill=color, outline="#6c5ce7", width=1,
                                   stipple="gray50")
                if cell >= 12 and 0 < step < len(path)-1:
                    c.create_text(px2+cell//2, py2+cell//2, text=str(step),
                                  fill="white", font=("Segoe UI", max(5, cell//2-2)))
            # Vẽ destination marker
            dx, dy = path[-1]
            px2, py2 = _px(dx, dy)
            c.create_rectangle(px2, py2, px2+cell-1, py2+cell-1,
                               fill="#fd79a8", outline="#e84393", width=2)
            c.create_text(px2+cell//2, py2+cell//2, text="★",
                          fill="white", font=("Segoe UI", max(7, cell//2)))

    # ────────────────────────────────────────────────────────
    # TAB: XÂY DỰNG
    # ────────────────────────────────────────────────────────
    # ────────────────────────────────────────────────────────
    # TAB: DO THÁM
    # ────────────────────────────────────────────────────────
    def _build_spy_tab(self, p):
        self._spy_coords    = []   # list of [x, y, note]
        self._spy_results   = {}   # (x,y) -> {"label","note","raw_title","raw_btn"}
        self._spy_running   = False
        self._spy_loaded    = False  # True sau khi _spy_load chạy xong

        paned = ttk.PanedWindow(p, orient="horizontal")
        paned.pack(fill="both", expand=True)
        lf = tk.Frame(paned, bg=C["bg"]); paned.add(lf, weight=1)
        rf = tk.Frame(paned, bg=C["bg"]); paned.add(rf, weight=2)

        # ── LEFT: Cấu hình + danh sách toạ độ ──
        cfg_sec = self._sec(lf, "Danh sách toạ độ do thám")

        # Add row
        add_row = tk.Frame(cfg_sec, bg=C["panel"]); add_row.pack(fill="x", padx=4, pady=4)
        tk.Label(add_row, text="X:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        self._spy_ex = tk.Entry(add_row, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground="white", relief="flat",
                                 font=("Segoe UI", 9))
        self._spy_ex.pack(side="left", padx=2)
        tk.Label(add_row, text="Y:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(6,2))
        self._spy_ey = tk.Entry(add_row, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground="white", relief="flat",
                                 font=("Segoe UI", 9))
        self._spy_ey.pack(side="left", padx=2)
        tk.Label(add_row, text="Ghi chú:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(6,2))
        self._spy_enote = tk.Entry(add_row, width=10, bg=C["entry"], fg=C["text"],
                                    insertbackground="white", relief="flat",
                                    font=("Segoe UI", 9))
        self._spy_enote.pack(side="left", padx=2)
        tk.Button(add_row, text="➕", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=6, pady=2,
                  command=self._spy_add_coord).pack(side="left", padx=4)

        # Treeview danh sách toạ độ
        tbl_frame = tk.Frame(cfg_sec, bg=C["panel"])
        tbl_frame.pack(fill="both", expand=True, padx=4, pady=2)
        spy_cols = ("#", "X", "Y", "Ghi chú", "Loại", "Bảo vệ", "🗺")
        self._spy_tree = ttk.Treeview(tbl_frame, columns=spy_cols,
                                       show="headings", height=12)
        for col, w in [("#",30),("X",50),("Y",50),("Ghi chú",80),("Loại",60),("Bảo vệ",120),("🗺",36)]:
            self._spy_tree.heading(col, text=col)
            self._spy_tree.column(col, width=w, anchor="center")
        sb = ttk.Scrollbar(tbl_frame, orient="vertical",
                            command=self._spy_tree.yview)
        self._spy_tree.configure(yscrollcommand=sb.set)
        self._spy_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._spy_tree.bind("<Delete>",       lambda e: self._spy_del_coord())
        self._spy_tree.bind("<Double-1>",     self._spy_edit_row)
        self._spy_tree.bind("<Button-1>",     self._spy_tree_click)

        # Tag màu kết quả
        for tag, color in [("ok","#55efc4"),("enemy","#ff7675"),
                            ("running","#fdcb6e"),("err","#b2bec3")]:
            self._spy_tree.tag_configure(tag, foreground=color)

        # Toolbar dưới bảng
        tb2 = tk.Frame(cfg_sec, bg=C["panel"]); tb2.pack(fill="x", padx=4, pady=2)
        tk.Button(tb2, text="🗑 Xóa đã chọn", bg=C["card"], fg=C["muted"],
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=3,
                  command=self._spy_del_coord).pack(side="left", padx=2)
        tk.Button(tb2, text="🗑 Xóa tất cả", bg="#333355", fg="white",
                  relief="flat", font=("Segoe UI", 8, "bold"), padx=6, pady=3,
                  command=self._spy_clear_all).pack(side="left", padx=2)
        tk.Button(tb2, text="📋 Import CSV", bg=C["card"], fg=C["text"],
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=3,
                  command=self._spy_import_csv).pack(side="left", padx=2)
        tk.Button(tb2, text="💾 Xuất CSV", bg=C["card"], fg=C["text"],
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=3,
                  command=self._spy_export_csv).pack(side="left", padx=2)

        # ── Nút chạy ──
        run_sec = self._sec(lf, "Điều khiển")
        run_row = tk.Frame(run_sec, bg=C["panel"]); run_row.pack(pady=6)
        self._spy_btn_start = tk.Button(run_row, text="▶ Bắt đầu do thám",
                                         bg=C["green"], fg="#111", relief="flat",
                                         font=("Segoe UI", 9, "bold"), padx=12, pady=5,
                                         command=self._spy_start)
        self._spy_btn_start.pack(side="left", padx=4)
        tk.Button(run_row, text="⏹ Dừng", bg=C["red"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=5,
                  command=self._spy_stop).pack(side="left", padx=4)

        save_row = tk.Frame(run_sec, bg=C["panel"]); save_row.pack(pady=2)
        def _spy_save_btn():
            self._spy_save()
            self._spy_status_lbl.config(text="✅ Đã lưu", fg=C["green"])
            self.after(2000, lambda: self._spy_status_lbl.config(
                text="● Chờ", fg=C["muted"]))
        tk.Button(save_row, text="💾 Lưu danh sách", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=12, pady=4,
                  command=_spy_save_btn).pack(side="left", padx=4)

        self._spy_status_lbl = tk.Label(run_sec, text="● Chờ", bg=C["panel"],
                                         fg=C["muted"], font=("Segoe UI", 9, "bold"))
        self._spy_status_lbl.pack(pady=4)
        self._spy_progress_lbl = tk.Label(run_sec, text="", bg=C["panel"],
                                           fg=C["yellow"], font=("Segoe UI", 8))
        self._spy_progress_lbl.pack()

        # ── RIGHT: Log + chi tiết kết quả ──
        detail_sec = self._sec(rf, "Chi tiết ô đang xem")
        self._spy_detail_lbl = tk.Label(detail_sec, text="— Click vào hàng để xem —",
                                         bg=C["panel"], fg=C["muted"],
                                         font=("Segoe UI", 9), justify="left",
                                         wraplength=320, anchor="w")
        self._spy_detail_lbl.pack(fill="x", padx=8, pady=6)

        log_sec = self._sec(rf, "Log")
        log_frame = tk.Frame(log_sec, bg=C["panel"])
        log_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self._spy_log_txt = tk.Text(log_frame, bg=C["card"], fg=C["text"],
                                     state="disabled", relief="flat",
                                     font=("Consolas", 8), height=16, wrap="word")
        sb2 = tk.Scrollbar(log_frame, command=self._spy_log_txt.yview,
                            bg=C["card"], troughcolor=C["bg"])
        self._spy_log_txt.configure(yscrollcommand=sb2.set)
        self._spy_log_txt.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        self._spy_log_txt.tag_configure("ok",   foreground=C["green"])
        self._spy_log_txt.tag_configure("warn",  foreground=C["yellow"])
        self._spy_log_txt.tag_configure("error", foreground=C["red"])

        # Bind click trên tree → hiện detail
        self._spy_tree.bind("<<TreeviewSelect>>", self._spy_show_detail)

    # ── Spy helpers ──────────────────────────────────────────
    def _spy_log(self, msg: str, level="info"):
        import datetime
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        txt = self._spy_log_txt
        tag = {"ok":"ok","warn":"warn","error":"error"}.get(level, "")
        self.after(0, lambda: self._spy_log_append(f"[{ts}] {msg}\n", tag))

    def _spy_log_append(self, line, tag):
        t = self._spy_log_txt
        t.configure(state="normal")
        t.insert("end", line, tag)
        t.see("end")
        t.configure(state="disabled")

    def _spy_refresh_tree(self):
        tree = self._spy_tree
        tree.delete(*tree.get_children())
        for i, (x, y, note) in enumerate(self._spy_coords):
            res = self._spy_results.get((x, y), {})
            lbl = res.get("label", "")
            tag = "ok" if lbl and lbl not in ("?","") else ("err" if lbl == "?" else "")
            protection = res.get("protection", "")
            expiry = self._calc_truce_expiry(res.get("truce",""))
            prot_disp = f"{protection} → {expiry}" if expiry and "Còn" in protection else protection
            tree.insert("", "end", iid=str(i),
                        values=(i+1, x, y, note, lbl, prot_disp, "🗺"), tags=(tag,))

    def _spy_add_coord(self):
        try:
            x = int(self._spy_ex.get())
            y = int(self._spy_ey.get())
        except ValueError:
            messagebox.showerror("Lỗi", "X, Y phải là số nguyên"); return
        note = self._spy_enote.get().strip()
        # Tránh trùng
        if any(c[0] == x and c[1] == y for c in self._spy_coords):
            messagebox.showinfo("", f"({x},{y}) đã có trong danh sách"); return
        self._spy_coords.append([x, y, note])
        self._spy_refresh_tree()
        self._spy_ex.delete(0, "end"); self._spy_ey.delete(0, "end")
        self._spy_enote.delete(0, "end")

    def _spy_del_coord(self):
        sel = self._spy_tree.selection()
        if not sel: return
        indices = sorted([int(s) for s in sel], reverse=True)
        for i in indices:
            self._spy_coords.pop(i)
        self._spy_refresh_tree()

    def _spy_clear_all(self):
        if not messagebox.askyesno("Xóa", "Xóa toàn bộ danh sách do thám?"): return
        self._spy_coords.clear()
        self._spy_results.clear()
        self._spy_refresh_tree()

    def _spy_edit_row(self, event=None):
        sel = self._spy_tree.selection()
        if not sel: return
        idx = int(sel[0])
        x, y, note = self._spy_coords[idx]
        dlg = tk.Toplevel(self)
        dlg.title(f"Sửa toạ độ #{idx+1}")
        dlg.configure(bg=C["bg"]); dlg.resizable(False, False); dlg.grab_set()
        pad = dict(padx=10, pady=5)
        tk.Label(dlg, text="X:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI",9)).grid(row=0,column=0,sticky="e",**pad)
        ex = tk.Entry(dlg, width=8, bg=C["entry"], fg=C["text"],
                      insertbackground="white", relief="flat"); ex.insert(0,str(x))
        ex.grid(row=0,column=1,**pad)
        tk.Label(dlg, text="Y:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI",9)).grid(row=1,column=0,sticky="e",**pad)
        ey = tk.Entry(dlg, width=8, bg=C["entry"], fg=C["text"],
                      insertbackground="white", relief="flat"); ey.insert(0,str(y))
        ey.grid(row=1,column=1,**pad)
        tk.Label(dlg, text="Ghi chú:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI",9)).grid(row=2,column=0,sticky="e",**pad)
        en = tk.Entry(dlg, width=16, bg=C["entry"], fg=C["text"],
                      insertbackground="white", relief="flat"); en.insert(0,note)
        en.grid(row=2,column=1,**pad)
        def _save():
            try: nx,ny = int(ex.get()), int(ey.get())
            except ValueError: messagebox.showerror("Lỗi","X,Y phải là số"); return
            self._spy_coords[idx] = [nx, ny, en.get().strip()]
            self._spy_refresh_tree(); dlg.destroy()
        tk.Button(dlg, text="💾 Lưu", bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI",9,"bold"), padx=12, pady=4,
                  command=_save).grid(row=3,column=0,columnspan=2,pady=8)
        dlg.update_idletasks()
        x0 = self.winfo_x()+(self.winfo_width()-dlg.winfo_width())//2
        y0 = self.winfo_y()+(self.winfo_height()-dlg.winfo_height())//2
        dlg.geometry(f"+{x0}+{y0}")

    def _calc_truce_expiry(self, truce_str: str) -> str:
        """Tính ngày giờ hết bảo vệ dựa vào thời gian còn lại + ngày bắt đầu game."""
        if not truce_str:
            return ""
        try:
            import datetime
            # Parse "HH:MM:SS" hoặc "D:HH:MM:SS" (nếu còn nhiều ngày)
            parts = [int(p) for p in re.split(r"[:\s]+", truce_str.strip())]
            if len(parts) == 3:
                h, m, s = parts
                d = 0
            elif len(parts) == 4:
                d, h, m, s = parts
            else:
                return ""
            remaining = datetime.timedelta(days=d, hours=h, minutes=m, seconds=s)

            # Thời điểm chụp ảnh ≈ now
            now = datetime.datetime.now()
            expiry = now + remaining

            # Nếu có ngày bắt đầu game, hiển thị thêm context
            game_start_str = getattr(self.cfg, "game_start", "").strip()
            if game_start_str:
                try:
                    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
                        try:
                            gs = datetime.datetime.strptime(game_start_str, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        gs = None
                    if gs:
                        days_from_start = (expiry - gs).days
                        return expiry.strftime("%d/%m/%Y %H:%M") + f" (ngày {days_from_start} của game)"
                except Exception:
                    pass

            return expiry.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return ""

    def _spy_tree_click(self, event):
        """Bấm vào cột 🗺 → mở bản đồ tới toạ độ đó."""
        region = self._spy_tree.identify_region(event.x, event.y)
        if region != "cell": return
        col_id = self._spy_tree.identify_column(event.x)
        # Cột #7 (index 6) là cột 🗺
        if col_id != "#7": return
        row_id = self._spy_tree.identify_row(event.y)
        if not row_id: return
        try:
            idx = int(row_id)
            x, y, note = self._spy_coords[idx]
        except (ValueError, IndexError): return

        if self._spy_running:
            messagebox.showinfo("", "Dừng do thám trước khi mở bản đồ"); return
        if not self.bot.adb.ok:
            messagebox.showinfo("", "Kết nối ADB trước"); return

        self._spy_status_lbl.config(text=f"🗺 Đang mở ({x},{y})...", fg=C["yellow"])
        def _nav():
            try:
                self.bot.navigate_to(x, y, force_tap=1)
                self.after(0, lambda: self._spy_status_lbl.config(
                    text=f"✅ Đã mở ({x},{y})", fg=C["green"]))
                self._spy_log(f"[Spy] 🗺 Mở bản đồ → ({x},{y}) {note}", "ok")
            except Exception as e:
                self.after(0, lambda: self._spy_status_lbl.config(
                    text=f"❌ Lỗi: {e}", fg=C["red"]))
        threading.Thread(target=_nav, daemon=True).start()

    def _spy_show_detail(self, event=None):
        sel = self._spy_tree.selection()
        if not sel: return
        idx = int(sel[0])
        x, y, note = self._spy_coords[idx]
        res = self._spy_results.get((x, y), {})
        truce_str = res.get("truce", "")
        expiry    = self._calc_truce_expiry(truce_str)
        expiry_line = f"Hết bảo vệ lúc: {expiry}" if expiry else ""
        lines = [f"📍 ({x}, {y})  {note}",
                 f"Loại: {res.get('label','-')}",
                 f"Tên:  {res.get('name','-')}",
                 f"Bảo vệ: {res.get('protection','-')}"]
        if expiry_line:
            lines.append(expiry_line)
        if res.get("raw_title"):
            lines.append(f"--- Title OCR ---")
            lines.append(res["raw_title"][:200])
        if res.get("raw_btn"):
            lines.append(f"--- Btn OCR ---")
            lines.append(res["raw_btn"][:200])
        self._spy_detail_lbl.config(text="\n".join(lines), fg=C["text"])

    def _spy_import_csv(self):
        from tkinter.filedialog import askopenfilename
        path = askopenfilename(filetypes=[("CSV","*.csv"),("Text","*.txt")])
        if not path: return
        import csv
        added = 0
        with open(path, encoding="utf-8") as f:
            for row in csv.reader(f):
                if not row or row[0].strip().lower() in ("x","#"): continue
                try:
                    x, y = int(row[0]), int(row[1])
                    note = row[2].strip() if len(row) > 2 else ""
                    if not any(c[0]==x and c[1]==y for c in self._spy_coords):
                        self._spy_coords.append([x, y, note]); added += 1
                except (ValueError, IndexError): continue
        self._spy_refresh_tree()
        self._spy_log(f"Import {added} toạ độ từ CSV", "ok")

    def _spy_export_csv(self):
        if not self._spy_coords: messagebox.showinfo("","Chưa có toạ độ"); return
        from tkinter.filedialog import asksaveasfilename
        import csv
        path = asksaveasfilename(defaultextension=".csv",
                                  filetypes=[("CSV","*.csv")],
                                  initialfile="spy_coords.csv")
        if not path: return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["X","Y","Ghi chú","Loại","Tên","Truce","Bảo vệ"])
            for x, y, note in self._spy_coords:
                res = self._spy_results.get((x,y),{})
                w.writerow([x, y, note, res.get("label",""), res.get("name",""),
                            res.get("truce",""), res.get("protection","")])
        self._spy_log(f"Xuất {len(self._spy_coords)} dòng → {path}", "ok")

    def _spy_start(self):
        if not self._spy_coords:
            messagebox.showinfo("","Chưa có toạ độ nào"); return
        if self._spy_running: return

        # Dừng tất cả tính năng đang chạy
        if hasattr(self, "bot") and self.bot._thread and self.bot._thread.is_alive():
            self.bot.stop(); self._log("[Spy] ⏸ Dừng Bot tấn công")
        for eng in getattr(self, "_wave_engines", []):
            if eng._thread and eng._thread.is_alive(): eng.stop()
        if getattr(self, "_scout_engine", None):
            try: self._scout_engine.stop()
            except: pass
        if getattr(self, "build_eng", None):
            try: self.build_eng.stop()
            except: pass

        self._spy_running = True
        self._spy_stop_evt = threading.Event()
        self._spy_btn_start.config(state="disabled")
        self._spy_status_lbl.config(text="🔍 Đang do thám...", fg=C["yellow"])
        threading.Thread(target=self._spy_run, daemon=True).start()

    def _spy_stop(self):
        self._spy_stop_evt.set()
        self._spy_running = False
        self.after(0, lambda: self._spy_status_lbl.config(text="⏹ Dừng", fg=C["red"]))
        self.after(0, lambda: self._spy_btn_start.config(state="normal"))

    def _spy_run(self):
        import cv2, numpy as np, re
        coords = list(self._spy_coords)
        total  = len(coords)
        back_x, back_y = 342, 1216

        for i, (x, y, note) in enumerate(coords):
            if self._spy_stop_evt.is_set(): break

            self.after(0, lambda i=i,x=x,y=y: (
                self._spy_progress_lbl.config(
                    text=f"Đang dò: {i+1}/{total}  ({x},{y})",
                    fg=C["yellow"]),
                self._spy_tree.item(str(i), tags=("running",))
            ))
            self._spy_log(f"[Spy] {i+1}/{total} → ({x},{y}) {note}")

            try:
                adb = self.bot.adb

                # Điểm đầu tiên: tap 1 lần; các điểm sau: back rồi tap
                if i > 0:
                    adb.tap(back_x, back_y)
                    time.sleep(0.5)

                # Tap vào toạ độ game
                self.bot.navigate_to(x, y, force_tap=1)
                if self._spy_stop_evt.is_set(): break
                time.sleep(0.6)

                # Chụp màn hình và OCR
                raw_bytes = adb.screenshot_bytes()
                if not raw_bytes:
                    self._spy_log(f"[Spy] ⚠️ Không chụp được ô ({x},{y})", "warn")
                    self._spy_results[(x,y)] = {"label":"?","name":"","raw_title":"","raw_btn":""}
                    self._spy_refresh_tree_row(i, x, y)
                    continue

                arr = np.frombuffer(raw_bytes, np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

                info = analyze_tile_image(img)
                text_title = info.get('raw_title', '')
                text_btn = info.get('raw_btn', '')
                label = info.get('label', '?')
                name = info.get('name', '')

                # ── Phát hiện Truce (bảo vệ) ──────────────────────────
                truce_time = ""
                truce_m = re.search(
                    r'truce\s+(\d{1,2}:\d{2}:\d{2})',
                    text_title, re.IGNORECASE)
                if not truce_m:
                    # OCR đôi khi đọc sai "Truce" → thử tìm pattern giờ gần từ "truce"
                    truce_m = re.search(
                        r'(?:truce|truee|iruce|lruce)\s+(\d{1,3}[:\s]\d{2}[:\s]\d{2})',
                        text_title, re.IGNORECASE)
                if re.search(r'\b(quit|out)\b', text_title, re.IGNORECASE):
                    truce_time = ""
                    protection = "🏳️ Already quit the battle"
                elif truce_m:
                    truce_time = truce_m.group(1).strip()
                    protection = f"🛡 Còn bảo vệ {truce_time}"
                else:
                    protection = "⚠️ Hết bảo vệ"

                self._spy_results[(x,y)] = {
                    "label": label, "name": name,
                    "truce": truce_time,
                    "protection": protection,
                    "raw_title": text_title.strip()[:300], "raw_btn": text_btn.strip()[:300]
                }
                log_msg = f"[Spy] ({x},{y}) = {label} | {name} | {protection}"
                self._spy_log(log_msg, "ok")
                self.after(0, self._spy_save)  # auto-save sau mỗi kết quả

            except Exception as e:
                self._spy_log(f"[Spy] ❌ Lỗi ({x},{y}): {e}", "error")
                self._spy_results[(x,y)] = {"label":"?","name":"","raw_title":str(e),"raw_btn":""}

            self.after(0, lambda i=i,x=x,y=y: self._spy_refresh_tree_row(i, x, y))
            time.sleep(0.3)

        # Back lần cuối
        if not self._spy_stop_evt.is_set():
            try: self.bot.adb.tap(back_x, back_y)
            except: pass

        self._spy_running = False
        done = len(self._spy_results)
        self.after(0, lambda: (
            self._spy_status_lbl.config(text=f"✅ Xong ({done}/{total})", fg=C["green"]),
            self._spy_progress_lbl.config(text=f"Hoàn tất: {total} toạ độ", fg=C["green"]),
            self._spy_btn_start.config(state="normal")
        ))
        self._spy_log(f"[Spy] ✅ Hoàn tất {done}/{total} ô", "ok")
        self._spy_save()

    def _spy_refresh_tree_row(self, idx, x, y):
        res = self._spy_results.get((x,y), {})
        lbl = res.get("label","")
        note = self._spy_coords[idx][2] if idx < len(self._spy_coords) else ""
        tag = "ok" if lbl not in ("?","","enemy","enemy_city") else (
              "error" if lbl in ("?","") else "warn")
        try:
            protection = res.get("protection", "")
            expiry = self._calc_truce_expiry(res.get("truce",""))
            prot_disp = f"{protection} → {expiry}" if expiry and "Còn" in protection else protection
            self._spy_tree.item(str(idx), values=(idx+1, x, y, note, lbl, prot_disp, "🗺"), tags=(tag,))
        except Exception: pass

    # ────────────────────────────────────────────────────────
    # TAB: AUTO UPDATE LV
    # ────────────────────────────────────────────────────────

    def _build_autolv_tab(self, p):
        left = tk.Frame(p, bg=C["bg"], width=220); left.pack(side="left", fill="y", padx=4, pady=4)
        left.pack_propagate(False)
        right = tk.Frame(p, bg=C["bg"]); right.pack(side="left", fill="both", expand=True, padx=4, pady=4)

        s = self._sec(left, "Auto Update Lv")
        tk.Label(s, text="Delay giua buoc (ms):", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(anchor="w", padx=4)
        self._alv_delay_var = tk.StringVar(value="800")
        tk.Entry(s, textvariable=self._alv_delay_var, width=8,
                 bg=C["entry"], fg=C["text"], insertbackground="white",
                 relief="flat", font=("Segoe UI", 9)).pack(anchor="w", padx=4, pady=2)

        tk.Label(s, text="Delay sau confirm (ms):", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 8)).pack(anchor="w", padx=4, pady=(6,0))
        self._alv_confirm_delay_var = tk.StringVar(value="1200")
        tk.Entry(s, textvariable=self._alv_confirm_delay_var, width=8,
                 bg=C["entry"], fg=C["text"], insertbackground="white",
                 relief="flat", font=("Segoe UI", 9)).pack(anchor="w", padx=4, pady=2)

        btn_row = tk.Frame(s, bg=C["panel"]); btn_row.pack(pady=8)
        self._alv_btn_start = tk.Button(btn_row, text="▶ Bắt đầu",
                                         bg=C["green"], fg="#111", relief="flat",
                                         font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                                         command=self._autolv_start)
        self._alv_btn_start.pack(side="left", padx=4)
        tk.Button(btn_row, text="⏹ Dừng", bg=C["red"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=8, pady=4,
                  command=self._autolv_stop).pack(side="left", padx=4)

        self._alv_status_lbl = tk.Label(s, text="● Chờ", bg=C["panel"],
                                         fg=C["muted"], font=("Segoe UI", 8, "bold"))
        self._alv_status_lbl.pack(pady=4)
        self._alv_next_lbl = tk.Label(s, text="", bg=C["panel"],
                                       fg=C["yellow"], font=("Segoe UI", 8),
                                       wraplength=190, justify="left")
        self._alv_next_lbl.pack(pady=2, padx=4)

        self._alv_stop_evt = threading.Event()
        self._alv_running  = False

        # Log panel
        log_sec = self._sec(right, "Log")
        self._alv_log_txt = tk.Text(log_sec, bg=C["card"], fg=C["text"],
                                     font=("Consolas", 8), relief="flat",
                                     state="disabled", height=30)
        sb = tk.Scrollbar(log_sec, command=self._alv_log_txt.yview)
        self._alv_log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._alv_log_txt.pack(fill="both", expand=True)

    def _alv_log(self, msg):
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._log(msg)
        try:
            self._alv_log_txt.configure(state="normal")
            self._alv_log_txt.insert("end", line)
            self._alv_log_txt.see("end")
            self._alv_log_txt.configure(state="disabled")
        except Exception: pass

    def _autolv_start(self):
        if self._alv_running: return
        if not self.bot.adb.ok:
            self._alv_status_lbl.config(text="❌ Chưa kết nối ADB", fg=C["red"]); return
        self._alv_stop_evt.clear()
        self._alv_running = True
        self._alv_btn_start.config(state="disabled")
        self._alv_status_lbl.config(text="⚙ Đang chạy...", fg=C["yellow"])
        threading.Thread(target=self._autolv_run, daemon=True).start()

    def _autolv_stop(self):
        self._alv_stop_evt.set()
        self._alv_status_lbl.config(text="⏹ Đã dừng", fg=C["muted"])

    def _autolv_run(self):
        import time, re
        adb = self.bot.adb
        delay     = int(self._alv_delay_var.get() or 800) / 1000.0
        c_delay   = int(self._alv_confirm_delay_var.get() or 1200) / 1000.0
        CONFIRM   = (558, 789)
        ROW1      = [(198,490),(306,490),(414,490),(522,490)]
        ROW2      = [(270,490),(378,490),(486,490),(594,490)]

        def _tap(x, y, wait=None):
            adb.tap(x, y)
            time.sleep(wait if wait else delay)

        def _check_stop():
            return self._alv_stop_evt.is_set()

        cycle = 0
        while not _check_stop():
            cycle += 1
            self._alv_log(f"=== Chu ky {cycle} ===")
            self.after(0, lambda: self._alv_status_lbl.config(
                text=f"⚙ Chu kỳ {cycle}", fg=C["yellow"]))

            # === Lấy ADB lock cho toàn bộ thao tác ADB ===
            ADB_LOCK.acquire()
            self._alv_log("[AutoLv] 🔓 Đã lấy ADB lock")
            wait_secs = 0
            try:
                # Check CAPTCHA truoc moi chu ky
                sc = adb.screenshot_cv2()
                if sc is not None and self.bot.det.has_captcha_popup(sc):
                    self._alv_log("🚨 CAPTCHA phát hiện! Dừng Auto Lv!")
                    self._stop_all_captcha()
                    return

                # 1. Vao thanh neu dang o ngoai
                if not adb.is_in_city():
                    self._alv_log("Dang o ngoai thanh → tap vao thanh")
                    _tap(54, 1216, 2.0)
                    if not adb.is_in_city():
                        self._alv_log("Khong vao duoc thanh → thu lai sau 3s")
                        continue
                if _check_stop(): break
                self._alv_log("Da trong thanh")

                # 2. Mo menu Training
                self._alv_log("Tap (414, 704)")
                _tap(414, 704, 1.5)
                if _check_stop(): break
                self._alv_log("Tap (522, 234)")
                _tap(522, 234, 1.5)
                if _check_stop(): break

                # 2.5. Scroll ngang sang phai truoc Row 1 (keo trai→phai)
                w, h = adb.get_screen_size()
                self._alv_log(f"Scroll sang phai (keo 10%→85%) {w}px")
                adb.swipe(int(w*0.10), 490, int(w*0.85), 490, ms=1500)
                time.sleep(1.0)
                if _check_stop(): break

                # 3. Row 1: tap confirm truc tiep, roi 4 nut con lai
                self._alv_log("Tap thang confirm (558, 789)")
                _tap(CONFIRM[0], CONFIRM[1], c_delay)
                if _check_stop(): break
                for x, y in ROW1:
                    if _check_stop(): break
                    self._alv_log(f"Tap ({x},{y}) → confirm {CONFIRM}")
                    _tap(x, y, delay)
                    _tap(CONFIRM[0], CONFIRM[1], c_delay)

                if _check_stop(): break

                # 4. Scroll ngang sang trai truoc Row 2 (keo phai→trai)
                w, h = adb.get_screen_size()
                self._alv_log(f"Scroll sang trai (keo 85%→10%) {w}px")
                adb.swipe(int(w*0.85), 490, int(w*0.10), 490, ms=1500)
                time.sleep(1.0)
                time.sleep(1.0)
                if _check_stop(): break

                # 5. Row 2: 4 nut
                for x, y in ROW2:
                    if _check_stop(): break
                    self._alv_log(f"Tap ({x},{y}) → confirm {CONFIRM}")
                    _tap(x, y, delay)
                    _tap(CONFIRM[0], CONFIRM[1], c_delay)

                if _check_stop(): break

                # 6. OCR lay thoi gian hoan thanh
                try:
                    import cv2, numpy as np, pytesseract as pt
                    raw = adb.screenshot_bytes()
                    if raw:
                        arr = np.frombuffer(raw, np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        hh, ww = img.shape[:2]
                        crop = img[int(hh*0.60):int(hh*0.85), 0:ww]
                        cr3  = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
                        gray = cv2.cvtColor(cr3, cv2.COLOR_BGR2GRAY)
                        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
                        text = pt.image_to_string(th, config="--psm 6")
                        self._alv_log(f"OCR: {text.strip()[:120]}")
                        m = re.search(r'requires[:\s]+(\d+):(\d{2}):(\d{2})', text, re.IGNORECASE)
                        if not m:
                            m = re.search(r'(\d+):(\d{2}):(\d{2})', text)
                        if m:
                            parts = [int(x) for x in m.groups()]
                            if len(parts) == 3:
                                wait_secs = parts[0]*3600 + parts[1]*60 + parts[2]
                            self._alv_log(f"Thoi gian cho: {m.group(0)} = {wait_secs}s")
                        else:
                            self._alv_log("Khong doc duoc thoi gian")
                except Exception as e:
                    self._alv_log(f"OCR loi: {e}")

                if _check_stop(): break

                # 7. Thoat ra ngoai thanh
                self._alv_log("Back (342, 1216)")
                _tap(342, 1216, 1.0)
                if adb.is_in_city():
                    self._alv_log("Van trong thanh → tap thoat")
                    _tap(54, 1216, 1.5)
                self._alv_log("Da thoat thanh")
            finally:
                ADB_LOCK.release()
                self._alv_log("[AutoLv] 🔒 Trả ADB lock")

            # === Chờ hết thời gian — KHÔNG cần ADB lock ===
            if wait_secs > 10:
                import datetime
                wake_at = datetime.datetime.now() + datetime.timedelta(seconds=wait_secs)
                self._alv_log(f"Cho {wait_secs}s → thuc day luc {wake_at.strftime('%H:%M:%S')}")
                deadline = time.time() + wait_secs
                while time.time() < deadline:
                    if _check_stop(): break
                    rem = int(deadline - time.time())
                    h2, r2 = divmod(rem, 3600); m2, s2 = divmod(r2, 60)
                    self.after(0, lambda t=f"{h2}g{m2:02d}p{s2:02d}s": (
                        self._alv_status_lbl.config(text=f"⏳ Còn {t}", fg=C["accent"]),
                        self._alv_next_lbl.config(text=f"Lần tiếp: {wake_at.strftime('%H:%M:%S')}")
                    ))
                    # Check captcha moi 30s (cần ADB ngắn)
                    if rem % 30 == 0:
                        with ADB_LOCK:
                            sc = adb.screenshot_cv2()
                            if sc is not None and self.bot.det.has_captcha_popup(sc):
                                self._alv_log("🚨 CAPTCHA phát hiện! Dừng Auto Lv!")
                                self._stop_all_captcha()
                                return
                    time.sleep(5)
            else:
                self._alv_log("Khong co thoi gian cho → lap lai ngay")
                time.sleep(2)

        self._alv_running = False
        self.after(0, lambda: (
            self._alv_btn_start.config(state="normal"),
            self._alv_status_lbl.config(text="● Dừng", fg=C["muted"]),
            self._alv_next_lbl.config(text="")
        ))
        self._alv_log("=== Đã dừng ===")

    # ────────────────────────────────────────────────────────
    # TAB: QUAY THƯỞNG
    # ────────────────────────────────────────────────────────

    def _build_spin_tab(self, p):
        """Tab quay thưởng tự động."""

        # ── Thông tin ──
        s1 = self._sec(p, "Thông tin")
        info_f = tk.Frame(s1, bg=C["panel"]); info_f.pack(fill="x", padx=6, pady=4)

        tk.Label(info_f, text="Trạng thái:", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.spin_lbl_status = tk.Label(info_f, text="● IDLE", bg=C["panel"],
                                         fg=C["muted"], font=("Consolas", 10, "bold"))
        self.spin_lbl_status.grid(row=0, column=1, sticky="w", padx=4, pady=2)

        tk.Label(info_f, text="Đã quay:", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="e", padx=4, pady=2)
        self.spin_e_done = tk.Entry(info_f, bg=C["entry"], fg=C["text"],
                                     insertbackground=C["text"], font=("Consolas", 10, "bold"), width=5)
        self.spin_e_done.insert(0, "0")
        self.spin_e_done.grid(row=1, column=1, sticky="w", padx=4, pady=2)

        tk.Label(info_f, text="Còn lại:", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="e", padx=4, pady=2)
        self.spin_e_left = tk.Entry(info_f, bg=C["entry"], fg=C["text"],
                                     insertbackground=C["text"], font=("Consolas", 10, "bold"), width=5)
        self.spin_e_left.insert(0, "—")
        self.spin_e_left.grid(row=2, column=1, sticky="w", padx=4, pady=2)

        # ── Hướng dẫn ──
        s2 = self._sec(p, "Luồng hoạt động")
        guide_text = (
            "1. Thoát thành nếu đang trong thành\n"
            "2. Bấm mở popup quay thưởng (414, 1216)\n"
            "3. Kiểm tra popup = Lucky Wheel (nếu sai → beep + dừng)\n"
            "4. Đọc số lượt còn lại: Today's chance(s) left: x\n"
            "5. Nếu Take a break mm:ss → chờ hết timer rồi quay\n"
            "6. Nếu Come back tomorrow → hết lượt, dừng\n"
            "7. Bấm quay (234, 1130) → cooldown → lặp lại"
        )
        tk.Label(s2, text=guide_text, bg=C["panel"], fg=C["muted"],
                 font=("Consolas", 8), justify="left", anchor="w").pack(
            fill="x", padx=8, pady=4)

        # ── Điều khiển ──
        s3 = self._sec(p, "Điều khiển")
        ctrl = tk.Frame(s3, bg=C["panel"]); ctrl.pack(pady=8)
        tk.Button(ctrl, text="▶  Bắt đầu quay", command=self._start_spin,
                  bg=C["green"], fg="white", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=18, pady=7,
                  cursor="hand2", bd=0,
                  activebackground=C["green"]).pack(side="left", padx=5)
        tk.Button(ctrl, text="⏹  Dừng", command=self._stop_spin,
                  bg=C["red"], fg="white", relief="flat",
                  font=("Segoe UI", 11, "bold"), padx=18, pady=7,
                  cursor="hand2", bd=0,
                  activebackground=C["red"]).pack(side="left", padx=5)

        # ── Log ──
        s4 = self._sec(p, "Log")
        self.spin_log_txt = tk.Text(s4, height=12, bg="#080a12", fg=C["text"],
                                     font=("Consolas", 8), state="disabled",
                                     wrap="word")
        self.spin_log_txt.pack(fill="both", expand=True, padx=6, pady=(4, 8))

    def _spin_log(self, msg):
        """Ghi log vào text widget của tab Quay thưởng."""
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        if hasattr(self, 'spin_log_txt'):
            def _append():
                self.spin_log_txt.config(state="normal")
                self.spin_log_txt.insert("end", line)
                self.spin_log_txt.see("end")
                self.spin_log_txt.config(state="disabled")
            self.after(0, _append)

    def _spin_refresh_ui(self):
        """Cap nhat labels trang thai."""
        def _update():
            status_map = {
                "idle":    ("● IDLE",    C["muted"]),
                "running": ("● RUNNING", C["green"]),
                "waiting": ("● WAITING", C["yellow"]),
                "done":    ("● DONE",    C["accent"]),
                "stopped": ("● STOPPED", C["red"]),
            }
            txt, color = status_map.get(self.spin_eng.status, ("● ???", C["muted"]))
            self.spin_lbl_status.config(text=txt, fg=color)
            # Cập nhật entry Đã quay
            self.spin_e_done.delete(0, "end")
            self.spin_e_done.insert(0, str(self.spin_eng.spins_done))
            # Cập nhật entry Còn lại
            left = self.spin_eng.spins_left
            self.spin_e_left.delete(0, "end")
            self.spin_e_left.insert(0, str(left) if left >= 0 else "—")
        self.after(0, _update)

    def _start_spin(self):
        if not self.bot.adb.ok:
            if not self.bot.adb.connect():
                self._spin_log("[Spin] Kết nối ADB trước!"); return
        self.spin_eng.adb = self.bot.adb
        self.spin_eng.det = self.bot.det
        self.spin_eng.log = self._spin_log
        self.spin_eng.on_refresh = self._spin_refresh_ui
        # Đọc giá trị đã quay / còn lại từ UI (user có thể chỉnh tay)
        try:
            self.spin_eng.spins_done = int(self.spin_e_done.get())
        except (ValueError, AttributeError):
            self.spin_eng.spins_done = 0
        try:
            val = self.spin_e_left.get().strip()
            self.spin_eng.spins_left = int(val) if val not in ("—", "") else -1
        except (ValueError, AttributeError):
            self.spin_eng.spins_left = -1
        self._spin_log(f"[Spin] ▶ Bắt đầu (đã quay={self.spin_eng.spins_done}, còn lại={self.spin_eng.spins_left})...")
        self._spin_refresh_ui()
        self.spin_eng.start()

    def _stop_spin(self):
        self.spin_eng.stop()
        self._spin_log("[Spin] ⏹ Đã gửi lệnh dừng")
        self._spin_refresh_ui()

    # ────────────────────────────────────────────────────────
    # TAB: DEBUG
    # ────────────────────────────────────────────────────────

    def _build_debug_tab(self, p):
        """Tab gom các công cụ kiểm tra & debug."""

        # ── Header ──
        hdr = tk.Frame(p, bg=C["panel"], height=42)
        hdr.pack(fill="x", padx=4, pady=(4, 0))
        hdr.pack_propagate(False)
        tk.Label(hdr, text="🐛  Công cụ Debug & Kiểm tra",
                 bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 11, "bold")).pack(
            side="left", padx=12, pady=8)

        # ── Main content: 2 columns ──
        body = tk.Frame(p, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=4, pady=4)

        left = tk.Frame(body, bg=C["bg"], width=340)
        left.pack(side="left", fill="y", padx=(0, 4))
        left.pack_propagate(False)
        left_inner = self._make_scrollable(left, bg=C["bg"])

        right = tk.Frame(body, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True)

        # ── Left: Buttons ──
        s1 = self._sec(left_inner, "Kiểm tra ADB & Màn hình")
        debug_btns_1 = [
            ("⚔️ Check icon ATK",   self._check_atk_icon,    C["card"],   "Tìm icon ATK trên màn hình bằng template matching"),
            ("🧩 Check CAPTCHA",    self._check_captcha_debug, "#7c5cff", "Chụp ảnh hiện tại và kiểm tra popup CAPTCHA / Random Test"),
            ("🪟 Detect Popup",     self._detect_popup_type,  "#7755aa", "Nhận 5 loại: all_armies / army_selection / territory_map / captcha / lucky_wheel; còn lại = other"),
            ("🏙 Test City",        self._test_city,         "#445566",   "Kiểm tra đang trong thành hay ngoài thành"),
            ("🪖 Check quân",       self._check_army,        "#556688",   "Mở All Armies, OCR đọc trạng thái từng quân"),
            ("🧠 Check máu quân",   self._check_army_health, C["accent"], "Đọc thanh máu + chạy Brain rule engine"),
        ]
        for text, cmd, color, tooltip in debug_btns_1:
            bf = tk.Frame(s1, bg=C["panel"])
            bf.pack(fill="x", padx=4, pady=2)
            tk.Button(bf, text=text, command=cmd,
                      bg=color, fg="white", relief="flat",
                      font=("Segoe UI", 9, "bold"),
                      padx=12, pady=6, cursor="hand2", bd=0,
                      activebackground=color).pack(side="left", fill="x", expand=True)
            tk.Label(bf, text=tooltip, bg=C["panel"], fg=C["muted"],
                     font=("Segoe UI", 7), anchor="w").pack(side="left", padx=(6, 4))

        s2 = self._sec(left_inner, "AI & OCR")
        debug_btns_2 = [
            ("🤖 Test AI (Ollama)",  self._test_ai,   C["accent"],  "Chụp ảnh → gửi Ollama → log kết quả"),
            ("🔍 Test OCR",          self._test_ocr,  "#557755",    "Chụp ảnh → pytesseract → log text"),
        ]
        for text, cmd, color, tooltip in debug_btns_2:
            bf = tk.Frame(s2, bg=C["panel"])
            bf.pack(fill="x", padx=4, pady=2)
            tk.Button(bf, text=text, command=cmd,
                      bg=color, fg="white", relief="flat",
                      font=("Segoe UI", 9, "bold"),
                      padx=12, pady=6, cursor="hand2", bd=0,
                      activebackground=color).pack(side="left", fill="x", expand=True)
            tk.Label(bf, text=tooltip, bg=C["panel"], fg=C["muted"],
                     font=("Segoe UI", 7), anchor="w").pack(side="left", padx=(6, 4))

        # ── Debug mode toggle ──
        s3 = self._sec(left_inner, "Tuỳ chọn")
        self._dbg_tab_var = tk.BooleanVar(value=self.cfg.debug_mode)
        def _toggle_debug_tab():
            self.cfg.debug_mode = self._dbg_tab_var.get()
            # Sync với checkbox bên tab Tấn công nếu có
            if hasattr(self, '_dbg_var'):
                self._dbg_var.set(self._dbg_tab_var.get())
        tk.Checkbutton(s3,
                       text="Lưu ảnh debug (debug/)",
                       variable=self._dbg_tab_var,
                       command=_toggle_debug_tab,
                       bg=C["panel"], fg=C["text"],
                       activebackground=C["panel"], selectcolor=C["card"],
                       font=("Segoe UI", 9)).pack(anchor="w", padx=4, pady=2)
        tk.Label(s3,
                 text="Khi bật: lưu screenshot, OCR result,\ntemplate match vào thư mục debug/",
                 bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 7), justify="left").pack(anchor="w", padx=4, pady=(0, 4))

        # ── Right: Debug log ──
        s4 = self._sec(right, "Debug Log")
        top = tk.Frame(s4, bg=C["panel"])
        top.pack(fill="x", padx=4, pady=(4, 2))
        tk.Label(top,
                 text="Các dòng [Popup], [CAPTCHA], [ATK], [OCR], [AI], [Army], [City] sẽ hiện ở đây.",
                 bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8), justify="left").pack(side="left", anchor="w")
        tk.Button(top, text="🗑 Xóa log debug", command=self._clear_debug_log,
                  bg=C["muted"], fg="white", relief="flat",
                  font=("Segoe UI", 8, "bold"), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="right")
        self.debug_log_txt = tk.Text(s4, height=18, bg=C["entry"], fg=C["text"],
                                     font=("Consolas", 8), state="disabled",
                                     wrap="word")
        self.debug_log_txt.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        # Quick info
        info = self._sec(right, "Thông tin nhanh")
        info_items = [
            ("assets/",  "Chứa template PNG (btn_atk.png, btn_capture.png, ...)"),
            ("debug/",   "Ảnh debug được lưu khi bật Debug mode"),
            ("config/",  "File cấu hình settings.json, scout map, spy data"),
        ]
        for folder, desc in info_items:
            row = tk.Frame(info, bg=C["panel"])
            row.pack(fill="x", padx=4, pady=1)
            tk.Label(row, text=folder, bg=C["panel"], fg=C["accent"],
                     font=("Consolas", 9, "bold"), width=10, anchor="w").pack(side="left")
            tk.Label(row, text=desc, bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 8), anchor="w").pack(side="left", padx=(4, 0))

    def _build_build_tab(self, p):
        # Config section
        sc = self._sec(p, "Cấu hình Xây dựng")
        def _erow2(parent, lbl, val, w=6):
            f = tk.Frame(parent, bg=C["panel"]); f.pack(fill="x", pady=2)
            tk.Label(f, text=lbl, bg=C["panel"], fg=C["text"],
                     font=("Segoe UI", 9), width=18, anchor="e").pack(side="left")
            e = tk.Entry(f, bg=C["entry"], fg=C["text"],
                         insertbackground=C["text"], font=("Segoe UI", 9), width=w)
            e.insert(0, str(val)); e.pack(side="left", padx=4)
            return e
        self.be_enter_x  = _erow2(sc, "Vào thành X:",     self.build_cfg.city_enter_x)
        self.be_enter_y  = _erow2(sc, "Vào thành Y:",     self.build_cfg.city_enter_y)
        self.be_info_x   = _erow2(sc, "Info btn X:",      self.build_cfg.info_btn_x)
        self.be_info_y   = _erow2(sc, "Info btn Y:",      self.build_cfg.info_btn_y)
        self.be_build_x  = _erow2(sc, "Xây btn X:",       self.build_cfg.build_btn_x)
        self.be_build_y  = _erow2(sc, "Xây btn Y:",       self.build_cfg.build_btn_y)
        self.be_max      = _erow2(sc, "Max xây cùng lúc:", self.build_cfg.max_builders)
        self.be_interval = _erow2(sc, "Check interval(s):", self.build_cfg.check_interval, 6)
        self.be_speed    = _erow2(sc, "Tốc độ xây (%):", self.build_cfg.speed_pct, 6)

        # Hẹn giờ: checkbox + phút
        hg_f = tk.Frame(sc, bg=C["panel"]); hg_f.pack(fill="x", pady=2)
        self._be_timer_var = tk.BooleanVar(value=False)
        tk.Checkbutton(hg_f, text="⏰ Hẹn giờ:", variable=self._be_timer_var,
                       bg=C["panel"], fg=C["text"], selectcolor=C["card"],
                       activebackground=C["panel"],
                       font=("Segoe UI", 9)).pack(side="left", padx=(4, 0))
        self.be_timer_min = tk.Entry(hg_f, bg=C["entry"], fg=C["text"],
                                      insertbackground=C["text"], font=("Segoe UI", 9), width=6)
        self.be_timer_min.insert(0, "0")
        self.be_timer_min.pack(side="left", padx=4)
        tk.Label(hg_f, text="phút", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left")
        self._be_timer_lbl = tk.Label(hg_f, text="", bg=C["panel"],
                                       fg=C["yellow"], font=("Consolas", 9, "bold"))
        self._be_timer_lbl.pack(side="left", padx=8)

        # Tasks section
        st = self._sec(p, "Danh sách nhà")
        cols = ("#", "Tên nhà", "Lv", "Mục tiêu", "Tap X", "Tap Y", "Bật", "Hết xây", "🔨")
        self.build_tree = ttk.Treeview(st, columns=cols, show="headings", height=6)
        widths = [30, 100, 40, 50, 50, 50, 35, 120, 40]
        for c, w in zip(cols, widths):
            self.build_tree.heading(c, text=c)
            self.build_tree.column(c, width=w, minwidth=w, anchor="center", stretch=True)
        self.build_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.build_tree.bind("<ButtonRelease-1>", self._on_build_tree_click)
        self._refresh_build_tree()

        # Buttons
        bf = tk.Frame(st, bg=C["panel"]); bf.pack(fill="x", pady=4)
        for txt, cmd, color in [
            ("➕ Thêm",    self._add_build_task,        C["green"]),
            ("✏ Sửa",     self._edit_build_task,        C["accent"]),
            ("🗑 Xóa",    self._del_build_task,         C["red"]),
            ("🔄 Reset",  self._reset_build_task,       C["muted"]),
            ("⬆ Lên",    self._build_move_up,          C["muted"]),
            ("⬇ Xuống",  self._build_move_down,        C["muted"]),
        ]:
            tk.Button(bf, text=txt, command=cmd, bg=color, fg="white",
                      relief="flat", font=("Segoe UI", 9, "bold"),
                      padx=8, pady=4, cursor="hand2", bd=0).pack(side="left", padx=3)

        # Control
        cf = self._sec(p, "Điều khiển")
        ctrl = tk.Frame(cf, bg=C["panel"]); ctrl.pack(pady=6)
        tk.Button(ctrl, text="▶ Bắt đầu xây", command=self._start_build,
                  bg=C["green"], fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=16, pady=6,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(ctrl, text="⏹ Dừng", command=self._stop_build,
                  bg=C["red"], fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=16, pady=6,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(ctrl, text="💰 Check tài nguyên", command=self._check_resources,
                  bg="#5a3e00", fg="#ffcc44", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=12, pady=6,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(ctrl, text="🔄 Cài lại Lv1", command=self._reset_all_build_levels,
                  bg=C["muted"], fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=12, pady=6,
                  cursor="hand2", bd=0).pack(side="left", padx=4)

        # Log (shared)
        sl = self._sec(p, "Log")
        self.build_log_txt = tk.Text(sl, height=8, bg=C["entry"], fg=C["text"],
                                      font=("Consolas", 8), state="disabled",
                                      wrap="word")
        self.build_log_txt.pack(fill="both", expand=True, padx=4, pady=4)

    def _refresh_build_tree(self):
        try: self._autosave()
        except Exception: pass
        self.build_tree.delete(*self.build_tree.get_children())
        import datetime as _dt
        for i, t in enumerate(self.build_cfg.tasks, 1):
            bd = t.get("build_done", 0)
            if bd > time.time():
                remaining = int(bd - time.time())
                h, r = divmod(remaining, 3600); m, s2 = divmod(r, 60)
                done_str = f"còn {h:02d}:{m:02d}:{s2:02d}"
            elif bd > 0:
                done_str = "✅ Xong"
            else:
                done_str = "-"
            self.build_tree.insert("", "end", values=(
                i,
                t.get("name",""),
                f"Lv{t.get('level', 1)}",
                f"Lv{t.get('target_lv', 20)}",
                t.get("tap_x",""),
                t.get("tap_y",""),
                "✓" if t.get("enabled", True) else "✗",
                done_str,
                "Xây",
            ))

    def _on_build_tree_click(self, event):
        """Handle click on action column (🔨 Xây) — build that single house."""
        row_id = self.build_tree.identify_row(event.y)
        col_id = self.build_tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_num = int(col_id.replace("#", ""))
        if col_num != 9:
            return  # not the 🔨 column
        idx = self.build_tree.index(row_id)
        if idx < 0 or idx >= len(self.build_cfg.tasks):
            return
        if not self.bot.adb.ok:
            messagebox.showinfo("", "Kết nối ADB trước!"); return
        task = self.build_cfg.tasks[idx]
        self.build_eng.adb = self.bot.adb
        self.build_eng.on_refresh = self._schedule_build_refresh
        self._log(f"[Build] 🔨 Xây nhanh: {task.get('name','')}...")
        def _run_single():
            self.build_eng.build_single(task)
        threading.Thread(target=_run_single, daemon=True).start()

    def _build_move_up(self):
        sel = self.build_tree.selection()
        if not sel: return
        idx = self.build_tree.index(sel[0])
        if idx <= 0: return
        t = self.build_cfg.tasks
        t[idx-1], t[idx] = t[idx], t[idx-1]
        self._refresh_build_tree()
        children = self.build_tree.get_children()
        if idx-1 < len(children):
            self.build_tree.selection_set(children[idx-1])

    def _build_move_down(self):
        sel = self.build_tree.selection()
        if not sel: return
        idx = self.build_tree.index(sel[0])
        t = self.build_cfg.tasks
        if idx >= len(t) - 1: return
        t[idx], t[idx+1] = t[idx+1], t[idx]
        self._refresh_build_tree()
        children = self.build_tree.get_children()
        if idx+1 < len(children):
            self.build_tree.selection_set(children[idx+1])

    def _add_build_task(self):
        self._build_task_dialog()

    def _edit_build_task(self):
        sel = self.build_tree.selection()
        if not sel: return
        idx = self.build_tree.index(sel[0])
        self._build_task_dialog(idx)

    def _del_build_task(self):
        sel = self.build_tree.selection()
        if not sel: return
        idx = self.build_tree.index(sel[0])
        self.build_cfg.tasks.pop(idx)
        self._refresh_build_tree()

    def _reset_build_task(self):
        sel = self.build_tree.selection()
        if not sel:
            # Reset tat ca neu khong chon gi
            for t in self.build_cfg.tasks:
                t["build_done"] = 0.0
        else:
            idx = self.build_tree.index(sel[0])
            self.build_cfg.tasks[idx]["build_done"] = 0.0
        self._refresh_build_tree()

    def _build_task_dialog(self, idx=None):
        task = self.build_cfg.tasks[idx] if idx is not None else {"name":"","tap_x":0,"tap_y":0,"enabled":True,"build_done":0.0,"target_lv":20}
        dlg  = tk.Toplevel(self)
        dlg.title("Nhà" if idx is None else f"Sửa {task['name']}")
        dlg.configure(bg=C["bg"]); dlg.grab_set(); dlg.geometry("260x260")
        fields = [("Tên:", "name", 0), ("Tap X:", "tap_x", 1), ("Tap Y:", "tap_y", 2), ("Level (1-20):", "level", 3), ("Lv mục tiêu:", "target_lv", 4)]
        entries = {}
        for lbl, key, row in fields:
            tk.Label(dlg, text=lbl, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 9), width=8, anchor="e").grid(row=row, column=0, padx=8, pady=6)
            e = tk.Entry(dlg, bg=C["entry"], fg=C["text"],
                         insertbackground=C["text"], font=("Segoe UI", 9), width=14)
            e.insert(0, str(task.get(key, 20 if key == "target_lv" else "")))
            e.grid(row=row, column=1, padx=4); entries[key] = e
        en_var = tk.BooleanVar(value=task.get("enabled", True))
        tk.Checkbutton(dlg, text="Bật", variable=en_var,
                       bg=C["bg"], fg=C["text"], selectcolor=C["entry"],
                       font=("Segoe UI", 9)).grid(row=5, column=1, sticky="w", padx=4)
        def save():
            t = {"name": entries["name"].get().strip(),
                 "tap_x": int(entries["tap_x"].get() or 0),
                 "tap_y": int(entries["tap_y"].get() or 0),
                 "level": max(1, min(20, int(entries["level"].get() or 1))),
                 "target_lv": max(1, min(20, int(entries["target_lv"].get() or 20))),
                 "enabled": en_var.get(),
                 "build_done": task.get("build_done", 0.0)}
            if idx is None: self.build_cfg.tasks.append(t)
            else: self.build_cfg.tasks[idx] = t
            self._refresh_build_tree(); dlg.destroy()
        tk.Button(dlg, text="Lưu", command=save, bg=C["green"], fg="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), pady=5,
                  cursor="hand2", bd=0).grid(row=6, columnspan=2, pady=8, sticky="ew", padx=8)

    def _start_build(self):
        if not self.bot.adb.ok:
            if not self.bot.adb.connect():
                self._log("[Build] Kết nối ADB trước!"); return
        try:
            self.build_cfg.max_builders  = int(self.be_max.get())
            self.build_cfg.check_interval = int(self.be_interval.get())
            self.build_cfg.speed_pct     = float(self.be_speed.get())
        except Exception: pass

        # Hẹn giờ: nếu check + phút > 0 → đếm ngược trước, rồi mới start engine
        if self._be_timer_var.get():
            try:
                timer_min = int(self.be_timer_min.get())
            except ValueError:
                timer_min = 0
            if timer_min > 0:
                self._log(f"[Build] ⏰ Hẹn giờ: chờ {timer_min} phút trước khi xây...")
                def _timer_run():
                    secs = timer_min * 60
                    deadline = time.time() + secs
                    while time.time() < deadline:
                        if self.build_eng._stop.is_set():
                            self.after(0, lambda: self._be_timer_lbl.config(text=""))
                            return
                        rem = int(deadline - time.time())
                        m, s = divmod(rem, 60)
                        self.after(0, lambda t=f"⏰ {m:02d}:{s:02d}": self._be_timer_lbl.config(text=t))
                        time.sleep(1)
                    # Hết hẹn → uncheck + xoá label + start engine
                    self.after(0, lambda: (
                        self._be_timer_var.set(False),
                        self._be_timer_lbl.config(text=""),
                        self._log("[Build] ⏰ Hết hẹn giờ → bắt đầu xây!")
                    ))
                    self.build_eng.adb = self.bot.adb
                    self.build_eng.on_refresh = self._schedule_build_refresh
                    self.build_eng.start()
                self.build_eng._stop.clear()
                threading.Thread(target=_timer_run, daemon=True).start()
                return

        self.build_eng.adb = self.bot.adb
        self.build_eng.on_refresh = self._schedule_build_refresh
        self.build_eng.start()

    def _stop_build(self):
        self.build_eng.stop()
        self._be_timer_lbl.config(text="")
        self._log("[Build] ⏹ Đã gửi lệnh dừng → chờ bước hiện tại hoàn thành...")

    def _reset_all_build_levels(self):
        if not self.build_cfg.tasks:
            messagebox.showinfo("", "Danh sách nhà trống!"); return
        if not messagebox.askyesno("Xác nhận",
                f"Cài lại level tất cả {len(self.build_cfg.tasks)} nhà về Lv1?"):
            return
        for t in self.build_cfg.tasks:
            t["level"] = 1
            t["build_done"] = 0
        self._refresh_build_tree()
        self._log(f"[Build] 🔄 Đã cài lại {len(self.build_cfg.tasks)} nhà về Lv1")

    def _ocr_read_resources(self) -> dict:
        """Đọc 3 số tài nguyên góc trái bằng thuật toán crop từng dòng riêng."""
        import re
        try:
            import cv2, numpy as np, pytesseract
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                return {}
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]

            rows = {
                "food":  (0.014, 0.040),
                "wood":  (0.055, 0.073),
                "stone": (0.094, 0.113),
            }
            x0, x1 = int(w * 0.08), int(w * 0.23)

            results = {}
            raw_txts = []

            for name, (ry0, ry1) in rows.items():
                y0r, y1r = int(h * ry0), int(h * ry1)
                roi = img[y0r:y1r, x0:x1]
                roi = cv2.resize(roi, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
                gray_r = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                _, th = cv2.threshold(gray_r, 180, 255, cv2.THRESH_BINARY)
                kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
                th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)

                t = pytesseract.image_to_string(
                    th, config=r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789+')
                m = re.match(r'(\d+)', t.strip())
                results[name] = int(m.group(1)) if m else None
                raw_txts.append(f"{name}={repr(t.strip())}")

            self._log(f"[💰 OCR] {' | '.join(raw_txts)}")
            return results
        except Exception as e:
            self._log(f"[OCR] Lỗi đọc tài nguyên: {e}")
            return {}

    def _check_resources(self):
        def _blog(msg):
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {msg}\n"
            self._log(msg)
            try:
                self.build_log_txt.configure(state="normal")
                self.build_log_txt.insert("end", line)
                self.build_log_txt.see("end")
                self.build_log_txt.configure(state="disabled")
            except Exception: pass
        def _run():
            _blog("[💰] Đang đọc tài nguyên...")
            res = self._ocr_read_resources()
            if res:
                food  = res.get("food",  "?")
                wood  = res.get("wood",  "?")
                stone = res.get("stone", "?")
                _blog(f"[💰] Thức ăn: {food}  |  Gỗ: {wood}  |  Đá: {stone}")
            else:
                _blog("[💰] Không đọc được tài nguyên")
        threading.Thread(target=_run, daemon=True).start()

    def _make_scrollable(self, parent, bg=None):
        """
        Tao vung scroll doc cho parent frame.
        Tra ve inner frame de pack widget vao.
        Inner frame tu dong gian ra full width cua canvas.
        """
        if bg is None:
            bg = C["bg"]
        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=bg)

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Khi canvas thay doi kich thuoc -> inner frame gian ra full width
        def _on_canvas_configure(event):
            canvas.itemconfig(win_id, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Pack: scrollbar ben phai, canvas fill con lai
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # Bind mousewheel
        def _on_mousewheel(event):
            # Windows: event.delta, Linux: event.num
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")

        def _bind_wheel(e):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_wheel(e):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)

        # Giữ reference tránh GC
        parent._scroll_canvas = canvas
        parent._scroll_inner = inner

        return inner

    def _sec(self, p, title):
        f = tk.LabelFrame(p, text=f"  {title}  ",
                          bg=C["panel"], fg=C["accent"],
                          font=("Segoe UI", 9, "bold"),
                          bd=1, relief="groove", padx=6, pady=6)
        f.pack(fill="x", padx=4, pady=4)
        return f

    def _erow(self, p, label, defval, width=14):
        row = tk.Frame(p, bg=C["panel"]); row.pack(fill="x", pady=2)
        tk.Label(row, text=label, width=16, anchor="w",
                 bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=4)
        e = tk.Entry(row, bg=C["entry"], fg=C["text"],
                     insertbackground=C["text"], relief="flat",
                     width=width, font=("Segoe UI", 9))
        e.insert(0, str(defval)); e.pack(side="left", padx=4)
        return e

    def _note_bar(self, parent):
        """Hiển thị note nhắc nhở ở đầu mỗi tab."""
        bar = tk.Frame(parent, bg="#2a1a00", pady=4)
        bar.pack(fill="x", padx=0, pady=(0, 4))
        tk.Label(bar,
                 text="⚠️  Vui lòng đảm bảo đang ở màn hình chính của game (không trong thành) "
                      "và không tap bất kỳ điểm nào trước khi chạy tác vụ",
                 bg="#2a1a00", fg="#ffcc44",
                 font=("Segoe UI", 8, "bold"),
                 wraplength=860, justify="left").pack(padx=10)

    def _build_left(self, p):

        # Scrollable wrapper
        inner = self._make_scrollable(p, bg=C["bg"])

        # ADB
        s = self._sec(inner, "ADB Connection")
        self.e_adb = self._erow(s, "ADB path:", self.cfg.adb_path, 22)
        self.e_dev = self._erow(s, "Device:", self.cfg.device_serial, 18)
        br = tk.Frame(s, bg=C["panel"]); br.pack(pady=4)
        for text, cmd, color in [
            ("Connect",      self._connect,      C["accent"]),
            ("Scan",         self._scan,          C["muted"]),
        ]:
            tk.Button(br, text=text, command=cmd, bg=color, fg="white",
                      relief="flat", font=("Segoe UI", 9, "bold"),
                      padx=10, pady=4, cursor="hand2", bd=0,
                      activebackground=color).pack(side="left", padx=3)

        # Settings
        s2 = self._sec(inner, "Cài đặt bot")
        # Profile
        pf = tk.Frame(s2, bg=C["panel"]); pf.pack(fill="x", pady=(0,6))
        tk.Label(pf, text="Profile:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        self._profile_var = tk.StringVar(value="default")
        self._profile_cb  = ttk.Combobox(pf, textvariable=self._profile_var,
                                          width=12, font=("Segoe UI", 9))
        self._profile_cb.pack(side="left", padx=4)
        self._profile_cb.bind("<Button-1>", lambda e: self._refresh_profiles())
        tk.Button(pf, text="📂 Load", command=self._load_profile,
                  bg=C["muted"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=2)
        try: self._refresh_profiles()
        except Exception: pass
        self.e_troops   = self._erow(
            s2, "Tên quân (,):",
            ", ".join(self.cfg.troop_names), 24)
        self.e_max      = self._erow(s2, "Số quân:",    self.cfg.max_troops,   5)
        self.e_cool     = self._erow(s2, "Cooldown(s):",    self.cfg.cooldown_sec,     5)
        self.e_cool_lv2 = self._erow(s2, "CD lv2(s):", self.cfg.lv2_cooldown_sec, 5)
        self.e_cool_lv3 = self._erow(s2, "CD lv3(s):", self.cfg.lv3_cooldown_sec, 5)
        self.e_cool_lv4 = self._erow(s2, "CD lv4(s):", self.cfg.lv4_cooldown_sec, 5)
        self.e_pop_to   = self._erow(s2, "Popup TOut:", self.cfg.popup_timeout,5)
        # Ngày bắt đầu game với placeholder
        _gs_row = tk.Frame(s2, bg=C["panel"]); _gs_row.pack(fill="x", pady=2)
        tk.Label(_gs_row, text="Ngày bắt đầu:", width=16, anchor="w",
                 bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=4)
        self.e_game_start = tk.Entry(_gs_row, bg=C["entry"], fg=C["muted"],
                                      insertbackground=C["text"], relief="flat",
                                      width=16, font=("Segoe UI", 9))
        _PLACEHOLDER = "YYYY-MM-DD HH:MM"
        def _gs_focus_in(e):
            if self.e_game_start.get() == _PLACEHOLDER:
                self.e_game_start.delete(0, "end")
                self.e_game_start.config(fg=C["text"])
        def _gs_focus_out(e):
            val = self.e_game_start.get().strip()
            if not val:
                self.e_game_start.insert(0, _PLACEHOLDER)
                self.e_game_start.config(fg=C["muted"])
                self.cfg.game_start = ""
            elif val != _PLACEHOLDER:
                self.cfg.game_start = val
                try: self._autosave()
                except Exception: pass
        if self.cfg.game_start:
            self.e_game_start.insert(0, self.cfg.game_start)
            self.e_game_start.config(fg=C["text"])
        else:
            self.e_game_start.insert(0, _PLACEHOLDER)
        self.e_game_start.bind("<FocusIn>",  _gs_focus_in)
        self.e_game_start.bind("<FocusOut>", _gs_focus_out)
        self.e_game_start.pack(side="left", padx=4)
        tk.Button(s2, text="💾  Lưu cài đặt",
                  command=self._save_all,
                  bg=C["green"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=12, pady=5,
                  cursor="hand2", bd=0,
                  activebackground=C["green"]).pack(
            anchor="w", padx=4, pady=6)

        # Nav button config (an giao dien, gia tri van giu nguyen trong cfg)

        # Hoi mau
        # Ollama Vision
        s_ol = self._sec(inner, "AI Vision (Ollama)")
        ol_f = tk.Frame(s_ol, bg=C["panel"]); ol_f.pack(fill="x", pady=(0,4))
        self._ol_var = tk.BooleanVar(value=self.cfg.ollama_enabled)
        def _toggle_ol():
            self.cfg.ollama_enabled = self._ol_var.get()
            lbl = "🤖 AI: ON" if self.cfg.ollama_enabled else "🤖 AI: OFF"
            ol_btn.config(text=lbl, bg=C["yellow"] if self.cfg.ollama_enabled else C["muted"])
            self.bot._ollama = OllamaVision(self.cfg.ollama_url, self.cfg.ollama_model)
        ol_btn = tk.Checkbutton(ol_f,
                                text="🤖 AI: ON" if self.cfg.ollama_enabled else "🤖 AI: OFF",
                                variable=self._ol_var, command=_toggle_ol,
                                bg=C["yellow"] if self.cfg.ollama_enabled else C["muted"],
                                fg="white", relief="flat",
                                selectcolor=C["yellow"],
                                font=("Segoe UI", 9, "bold"),
                                padx=8, pady=3, cursor="hand2",
                                activebackground=C["muted"], bd=0,
                                indicatoron=False)
        ol_btn.pack(side="left", padx=2)
        self.e_ol_url   = self._erow(s_ol, "URL:",   self.cfg.ollama_url,   20)
        self.e_ol_model = self._erow(s_ol, "Model:", self.cfg.ollama_model, 12)

        s_heal = self._sec(inner, "Auto Heal")
        self.e_heal_every = self._erow(s_heal, "Tấn công/lần:", self.cfg.heal_every, 5)
        self.e_heal_step  = self._erow(s_heal, "Step đặt điểm:", self.cfg.heal_step, 5)
        self.e_heal_x     = self._erow(s_heal, "Heal X:",       self.cfg.heal_x,     6)
        self.e_heal_y     = self._erow(s_heal, "Heal Y:",       self.cfg.heal_y,     6)
        tk.Label(s_heal,
                 text="(Sau N lan tan cong bot tu dong\nnhap toa do nay va chon quan hoi mau)\n"
                      "Step: cu X diem thi tu dong dat diem\nhoi mau = diem vua chiem (0=tat)",
                 bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 7), justify="left").pack(anchor="w", padx=4)
        tk.Button(s_heal, text="🩸 Hồi màu ngay",
                  bg="#8b1a1a", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  command=self._manual_heal).pack(pady=(6,2), fill="x", padx=4)

        s_brain = self._sec(inner, "Army Health Brain")
        self._brain_var = tk.BooleanVar(value=self.cfg.army_health_enabled)
        tk.Checkbutton(s_brain,
                       text="Bật phân tích máu quân trước khi đánh",
                       variable=self._brain_var,
                       bg=C["panel"], fg=C["text"],
                       activebackground=C["panel"], selectcolor=C["card"],
                       font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=4, pady=(0,4))
        self.e_hwarn = self._erow(s_brain, "Warn avg HP:", self.cfg.health_warn_avg, 6)
        self.e_hheal = self._erow(s_brain, "Heal avg HP:", self.cfg.health_force_heal_avg, 6)
        self.e_hcrit_ratio = self._erow(s_brain, "Crit unit HP:", self.cfg.health_critical_ratio, 6)
        self.e_hcrit_count = self._erow(s_brain, "Crit count:", self.cfg.health_force_heal_critical_units, 6)
        tk.Label(s_brain,
                 text="Rule: avg HP thấp hoặc nhiều unit đỏ -> heal trước.\n"
                      "Nếu quân yếu mà target lv3/lv4 -> bot heal rồi mới đánh.",
                 bg=C["panel"], fg=C["muted"], justify="left",
                 font=("Segoe UI", 7)).pack(anchor="w", padx=4)
        tk.Button(s_brain, text="🧠 Check máu quân",
                  bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  command=self._check_army_health).pack(pady=(6,2), fill="x", padx=4)

        # Telegram
        s_tg = self._sec(inner, "Telegram")
        self._tg_var = tk.BooleanVar(value=self.cfg.telegram_enabled)
        def _toggle_tg():
            self.cfg.telegram_enabled = self._tg_var.get()
            lbl = "📱 Telegram: ON" if self.cfg.telegram_enabled else "📱 Telegram: OFF"
            tg_btn.config(text=lbl, bg=C["yellow"] if self.cfg.telegram_enabled else C["muted"])
            if self.cfg.telegram_enabled:
                self._telegram_start()
            else:
                self._telegram_stop()
        tg_btn = tk.Checkbutton(s_tg,
                                text="📱 Telegram: ON" if self.cfg.telegram_enabled else "📱 Telegram: OFF",
                                variable=self._tg_var, command=_toggle_tg,
                                bg=C["yellow"] if self.cfg.telegram_enabled else C["muted"],
                                fg="white", selectcolor=C["entry"],
                                activebackground=C["panel"],
                                font=("Segoe UI", 9, "bold"), relief="flat",
                                padx=8, pady=3)
        tg_btn.pack(fill="x", padx=4, pady=2)
        self.e_tg_token = self._erow(s_tg, "Token:", self.cfg.telegram_token, 28)
        self.e_tg_chat  = self._erow(s_tg, "Chat ID:", self.cfg.telegram_chat_id, 14)
        tk.Button(s_tg, text="📩 Test gửi tin", bg=C["accent"], fg="white",
                  relief="flat", font=("Segoe UI", 8),
                  command=self._telegram_test).pack(fill="x", padx=4, pady=2)

        # State machine
        s3 = self._sec(inner, "State Machine")
        self._sdots = {}
        for st in State:
            row = tk.Frame(s3, bg=C["panel"])
            row.pack(fill="x", padx=2, pady=1)
            dot = tk.Label(row, text="○", width=2, bg=C["panel"],
                           fg=C["muted"], font=("Segoe UI", 10))
            dot.pack(side="left")
            lbl = tk.Label(row, text=st.name, anchor="w",
                           bg=C["panel"], fg=C["muted"],
                           font=("Consolas", 9))
            lbl.pack(side="left", padx=4)
            self._sdots[st] = (dot, lbl)

        # Progress
        s4 = self._sec(inner, "Tiến độ")
        self.lbl_prog = tk.Label(s4, text="—",
                                  bg=C["panel"], fg=C["text"],
                                  font=("Consolas", 9),
                                  justify="left")
        self.lbl_prog.pack(padx=6, pady=4, anchor="w")

    def _build_right(self, p):
        # ── Nhap toa do X,Y ──
        s1 = self._sec(p, "Danh sách tọa độ tấn công")

        # Input row: X, Y, Label, Add
        inp = tk.Frame(s1, bg=C["panel"]); inp.pack(fill="x", padx=4, pady=4)
        tk.Label(inp, text="X:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.e_gx = tk.Entry(inp, width=6, bg=C["entry"], fg=C["text"],
                             insertbackground=C["text"], relief="flat",
                             font=("Segoe UI", 9))
        self.e_gx.pack(side="left", padx=2)

        tk.Label(inp, text="Y:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(6,0))
        self.e_gy = tk.Entry(inp, width=6, bg=C["entry"], fg=C["text"],
                             insertbackground=C["text"], relief="flat",
                             font=("Segoe UI", 9))
        self.e_gy.pack(side="left", padx=2)

        tk.Label(inp, text="Nhan:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(6,0))
        self.e_glabel = tk.Entry(inp, width=10, bg=C["entry"], fg=C["text"],
                                  insertbackground=C["text"], relief="flat",
                                  font=("Segoe UI", 9))
        self.e_glabel.pack(side="left", padx=2)

        tk.Button(inp, text="➕ Them",
                  command=self._add_coord,
                  bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=8, pady=3,
                  cursor="hand2", bd=0,
                  activebackground=C["accent"]).pack(side="left", padx=4)

        # Day so dong 1: co dinh X, chay Y
        rng_f = tk.Frame(s1, bg=C["panel"]); rng_f.pack(fill="x", padx=4, pady=(0,2))
        tk.Label(rng_f, text="Dãy X cố: X=", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.e_rx = tk.Entry(rng_f, width=6, bg=C["entry"], fg=C["text"],
                             insertbackground=C["text"], relief="flat",
                             font=("Segoe UI", 9))
        self.e_rx.pack(side="left", padx=2)
        tk.Label(rng_f, text="Y từ", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(4,0))
        self.e_ry_from = tk.Entry(rng_f, width=6, bg=C["entry"], fg=C["text"],
                                   insertbackground=C["text"], relief="flat",
                                   font=("Segoe UI", 9))
        self.e_ry_from.pack(side="left", padx=2)
        tk.Label(rng_f, text="→", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.e_ry_to = tk.Entry(rng_f, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground=C["text"], relief="flat",
                                 font=("Segoe UI", 9))
        self.e_ry_to.pack(side="left", padx=2)
        tk.Label(rng_f, text="Nhãn:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(4,0))
        self.e_rlabel = tk.Entry(rng_f, width=8, bg=C["entry"], fg=C["text"],
                                  insertbackground=C["text"], relief="flat",
                                  font=("Segoe UI", 9))
        self.e_rlabel.pack(side="left", padx=2)
        tk.Button(rng_f, text="➕ Thêm dãy",
                  command=self._add_range_x_fixed,
                  bg=C["green"], fg="#111", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=4)

        # Day so dong 2: co dinh Y, chay X
        rng_f2 = tk.Frame(s1, bg=C["panel"]); rng_f2.pack(fill="x", padx=4, pady=(0,2))
        tk.Label(rng_f2, text="Dãy Y cố: Y=", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.e_ry = tk.Entry(rng_f2, width=6, bg=C["entry"], fg=C["text"],
                              insertbackground=C["text"], relief="flat",
                              font=("Segoe UI", 9))
        self.e_ry.pack(side="left", padx=2)
        tk.Label(rng_f2, text="X từ", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(4,0))
        self.e_rx_from = tk.Entry(rng_f2, width=6, bg=C["entry"], fg=C["text"],
                                   insertbackground=C["text"], relief="flat",
                                   font=("Segoe UI", 9))
        self.e_rx_from.pack(side="left", padx=2)
        tk.Label(rng_f2, text="→", bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 9)).pack(side="left")
        self.e_rx_to = tk.Entry(rng_f2, width=6, bg=C["entry"], fg=C["text"],
                                 insertbackground=C["text"], relief="flat",
                                 font=("Segoe UI", 9))
        self.e_rx_to.pack(side="left", padx=2)
        tk.Label(rng_f2, text="Nhãn:", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(4,0))
        self.e_rlabel2 = tk.Entry(rng_f2, width=8, bg=C["entry"], fg=C["text"],
                                   insertbackground=C["text"], relief="flat",
                                   font=("Segoe UI", 9))
        self.e_rlabel2.pack(side="left", padx=2)
        tk.Button(rng_f2, text="➕ Thêm dãy",
                  command=self._add_range_y_fixed,
                  bg=C["green"], fg="#111", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=4)

        # Paste nhieu dong: "X,Y" moi dong
        paste_f = tk.Frame(s1, bg=C["panel"]); paste_f.pack(fill="x", padx=4, pady=(0,4))
        tk.Label(paste_f, text="Paste nhieu dong (X,Y moi dong):",
                 bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Button(paste_f, text="📋 Import",
                  command=self._import_coords,
                  bg=C["muted"], fg="white", relief="flat",
                  font=("Segoe UI", 8, "bold"), padx=6, pady=2,
                  cursor="hand2", bd=0,
                  activebackground=C["muted"]).pack(side="left", padx=4)

        # Tree
        s2 = self._sec(p, "Điểm đã chọn")
        cols = ("#", "Game X", "Game Y", "Nhan", "Status", "✅", "⏳")
        self.tree = ttk.Treeview(
            s2, columns=cols, show="headings",
            height=5, selectmode="browse")
        for c, w in zip(cols, [30, 65, 65, 110, 60, 40, 40]):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="x", padx=6, pady=(2,2))
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        # Tree controls
        tc = tk.Frame(s2, bg=C["panel"]); tc.pack(pady=(0,4))
        for text, cmd, color in [
            ("⬆ Len",      self._move_up,     C["muted"]),
            ("⬇ Xuong",   self._move_down,   C["muted"]),
            ("✏ Sua",      self._edit_coord,  C["accent"]),
            ("🗑 Xoa",     self._del_coord,   C["red"]),
            ("Xoa het",   self._clear_pts,    "#333355"),
            ("🧹 Xoa done", self._clear_done_pts, "#553333"),
        ]:
            tk.Button(tc, text=text, command=cmd,
                      bg=color, fg="white", relief="flat",
                      font=("Segoe UI", 8, "bold"), padx=6, pady=3,
                      cursor="hand2", bd=0,
                      activebackground=color).pack(side="left", padx=2)

        # Also keep screenshot button for reference
        br1 = tk.Frame(s1, bg=C["panel"]); br1.pack(pady=2)
        tk.Button(br1, text="📷 Chup man hinh (xem toa do)",
                  command=self._take_screenshot_only,
                  bg="#2a2d45", fg=C["muted"], relief="flat",
                  font=("Segoe UI", 8), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(br1, text="🗺 Grid Picker",
                  command=self._take_and_pick,
                  bg="#2a2d45", fg=C["muted"], relief="flat",
                  font=("Segoe UI", 8), padx=8, pady=3,
                  cursor="hand2", bd=0).pack(side="left", padx=4)

        # Run controls
        s3 = self._sec(p, "Điều khiển")
        br2 = tk.Frame(s3, bg=C["panel"]); br2.pack(pady=8)
        for text, cmd, color, size in [
            ("▶  RUN tất cả điểm", self._run,  C["green"],  11),
            ("■  STOP",            self._stop, C["red"],    10),
            ("⏭️ Điểm tiếp theo",  self._skip_next,  C["yellow"], 9),
        ]:
            tk.Button(br2, text=text, command=cmd,
                      bg=color, fg="white", relief="flat",
                      font=("Segoe UI", size, "bold"),
                      padx=14, pady=7, cursor="hand2", bd=0,
                      activebackground=color).pack(
                side="left", padx=5)

        # Log
        s4 = self._sec(p, "Log")
        dbg_f = tk.Frame(s4, bg=C["panel"]); dbg_f.pack(fill="x", padx=6, pady=(2,0))
        self._dbg_var = tk.BooleanVar(value=self.cfg.debug_mode)
        def _toggle_debug():
            self.cfg.debug_mode = self._dbg_var.get()
            lbl = "📸 Lưu debug: ON" if self.cfg.debug_mode else "📸 Lưu debug: OFF"
            dbg_btn.config(text=lbl, bg=C["yellow"] if self.cfg.debug_mode else C["muted"])
        dbg_btn = tk.Checkbutton(dbg_f, text="📸 Lưu debug: OFF",
                                  variable=self._dbg_var, command=_toggle_debug,
                                  bg=C["muted"], fg="white", relief="flat",
                                  selectcolor=C["yellow"],
                                  font=("Segoe UI", 8, "bold"),
                                  padx=8, pady=3, cursor="hand2",
                                  activebackground=C["muted"], bd=0,
                                  indicatoron=False)
        dbg_btn.pack(side="left", padx=2, pady=2)
        self.txt = scrolledtext.ScrolledText(
            s4, height=11, bg="#080a12", fg=C["text"],
            font=("Consolas", 8), relief="flat",
            insertbackground=C["text"], state="disabled")
        self.txt.pack(fill="both", expand=True, padx=6, pady=(4,8))

    def _style(self):
        s = ttk.Style(); s.theme_use("clam")
        s.configure("Treeview",
                    background=C["card"], foreground=C["text"],
                    fieldbackground=C["card"],
                    rowheight=22, font=("Consolas", 9))
        s.configure("Treeview.Heading",
                    background=C["panel"], foreground=C["accent"],
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview",
              background=[("selected", C["accent"])])

    # ── Actions ─────────────────────────────────

    def _connect(self):
        self.cfg.adb_path      = self.e_adb.get().strip()
        self.cfg.device_serial = self.e_dev.get().strip()
        self.bot.adb.cfg       = self.cfg
        self.bot.adb.device    = self.cfg.device_serial
        self.bot.adb.ok        = False
        def run():
            if self.bot.adb.connect():
                w, h = self.bot.adb.get_screen_size()
                self._adb_w = w; self._adb_h = h
                self.cfg.screen_w = w; self.cfg.screen_h = h
                self._log(f"[ADB] Screen: {w}×{h}")
        threading.Thread(target=run, daemon=True).start()

    def _scan(self):
        def run():
            devs = self.bot.adb.list_devices()
            self._log(f"[Scan] {devs}")
            if devs:
                self.after(0, lambda: (
                    self.e_dev.delete(0,"end"),
                    self.e_dev.insert(0, devs[0])))
        threading.Thread(target=run, daemon=True).start()


    def _save_cfg_ui(self):
        raw = self.e_troops.get().strip()
        self.cfg.troop_names = [
            n.strip() for n in raw.split(",") if n.strip()]
        try:
            self.cfg.max_troops    = int(self.e_max.get())
            self.cfg.cooldown_sec      = int(self.e_cool.get())
            self.cfg.lv2_cooldown_sec = int(self.e_cool_lv2.get())
            self.cfg.lv3_cooldown_sec = int(self.e_cool_lv3.get())
            self.cfg.lv4_cooldown_sec = int(self.e_cool_lv4.get())
            self.cfg.ollama_url       = self.e_ol_url.get().strip()
            self.cfg.ollama_model     = self.e_ol_model.get().strip()
            self.cfg.ollama_enabled   = self._ol_var.get()
            self.bot._ollama = OllamaVision(self.cfg.ollama_url, self.cfg.ollama_model)
            self.cfg.popup_timeout = float(self.e_pop_to.get())
            _gs_val = self.e_game_start.get().strip()
            self.cfg.game_start = "" if _gs_val == "YYYY-MM-DD HH:MM" else _gs_val
            self.cfg.heal_every    = int(self.e_heal_every.get())
            self.cfg.heal_step     = int(self.e_heal_step.get())
            self.cfg.heal_x        = int(self.e_heal_x.get())
            self.cfg.heal_y        = int(self.e_heal_y.get())
            self.cfg.army_health_enabled = self._brain_var.get() if hasattr(self, '_brain_var') else self.cfg.army_health_enabled
            if hasattr(self, 'e_hwarn'):
                self.cfg.health_warn_avg = float(self.e_hwarn.get())
                self.cfg.health_force_heal_avg = float(self.e_hheal.get())
                self.cfg.health_critical_ratio = float(self.e_hcrit_ratio.get())
                self.cfg.health_force_heal_critical_units = int(self.e_hcrit_count.get())
            # Telegram
            if hasattr(self, 'e_tg_token'):
                self.cfg.telegram_enabled  = self._tg_var.get()
                self.cfg.telegram_token    = self.e_tg_token.get().strip()
                self.cfg.telegram_chat_id  = self.e_tg_chat.get().strip()
        except ValueError as e:
            messagebox.showerror("Lỗi", str(e)); return
        self.bot.adb.cfg = self.cfg
        self.bot.cfg     = self.cfg
        self.bot.sel.cfg = self.cfg
        self.bot.brain.cfg = self.cfg
        self._save_cfg()
        self._log(f"[CFG] Lưu OK | X=({self.cfg.x_field_x},{self.cfg.x_field_y}) "
                  f"Y=({self.cfg.y_field_x},{self.cfg.y_field_y}) "
                  f"Xem=({self.cfg.xem_x},{self.cfg.xem_y})")

    def _take_and_pick(self):
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[SS] Kết nối ADB trước!"); return
            self._log("[SS] Chụp màn hình...")
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                self._log("[SS] Thất bại"); return
            self._raw = raw
            w, h = self.bot.adb.get_screen_size()
            self._adb_w = w; self._adb_h = h
            self._log(f"[SS] OK {w}×{h} → mở Grid Picker")
            self.after(0, self._open_picker)
        threading.Thread(target=run, daemon=True).start()

    def _open_picker(self):
        if not self._raw:
            messagebox.showinfo("", "Chụp màn hình trước!"); return
        GridPicker(self, self._raw, self._adb_w, self._adb_h,
                   self.pts, on_confirm=self._on_confirmed)

    def _on_confirmed(self, pts):
        self.pts = pts
        self._refresh_tree()
        self._draw_preview()
        if autosave:
            try: self._autosave()
            except Exception: pass
        self._log(f"[Map] {len(pts)} điểm: "
                  + ", ".join(f"({p.game_x},{p.game_y})" for p in pts))

    def _add_range_x_fixed(self):
        """X co dinh, Y chay."""
        try:
            rx      = int(self.e_rx.get().strip())
            ry_from = int(self.e_ry_from.get().strip())
            ry_to   = int(self.e_ry_to.get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "X, Y từ, Y đến phải là số nguyên"); return
        base_label = self.e_rlabel.get().strip()
        step = 1 if ry_to >= ry_from else -1
        added = skipped = 0
        existing = {(p.game_x, p.game_y) for p in self.pts}
        for y in range(ry_from, ry_to + step, step):
            if (rx, y) in existing:
                skipped += 1; continue
            idx   = len(self.pts) + 1
            label = base_label if base_label else f"Diem {idx}"
            self.pts.append(AttackPoint(idx=idx, game_x=rx, game_y=y, label=label))
            existing.add((rx, y))
            self._refresh_tree()
            added += 1
        msg = f"[Coord] Thêm {added} điểm: X={rx}, Y={ry_from}→{ry_to}"
        if skipped: msg += f" (bỏ qua {skipped} trùng)"
        self._log(msg)

    def _add_range_y_fixed(self):
        """Y co dinh, X chay."""
        try:
            ry      = int(self.e_ry.get().strip())
            rx_from = int(self.e_rx_from.get().strip())
            rx_to   = int(self.e_rx_to.get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "Y, X từ, X đến phải là số nguyên"); return
        base_label = self.e_rlabel2.get().strip()
        step = 1 if rx_to >= rx_from else -1
        added = skipped = 0
        existing = {(p.game_x, p.game_y) for p in self.pts}
        for x in range(rx_from, rx_to + step, step):
            if (x, ry) in existing:
                skipped += 1; continue
            idx   = len(self.pts) + 1
            label = base_label if base_label else f"Diem {idx}"
            self.pts.append(AttackPoint(idx=idx, game_x=x, game_y=ry, label=label))
            existing.add((x, ry))
            self._refresh_tree()
            added += 1
        msg = f"[Coord] Thêm {added} điểm: Y={ry}, X={rx_from}→{rx_to}"
        if skipped: msg += f" (bỏ qua {skipped} trùng)"
        self._log(msg)

    def _add_coord(self):
        try:
            gx = int(self.e_gx.get().strip())
            gy = int(self.e_gy.get().strip())
        except ValueError:
            messagebox.showinfo("", "X va Y phai la so nguyen!"); return
        if any(p.game_x == gx and p.game_y == gy for p in self.pts):
            self._log(f"[Coord] ⚠️ ({gx},{gy}) đã tồn tại, bỏ qua"); return
        label = self.e_glabel.get().strip() or f"Diem {len(self.pts)+1}"
        idx   = len(self.pts) + 1
        self.pts.append(AttackPoint(idx=idx, game_x=gx, game_y=gy, label=label))
        self._refresh_tree()
        self.e_gx.delete(0, "end"); self.e_gy.delete(0, "end")
        self.e_glabel.delete(0, "end")
        self._log(f"[Coord] Them ({gx},{gy}) {label}")

    def _import_coords(self):
        """Mo cua so paste nhieu dong X,Y."""
        dlg = tk.Toplevel(self)
        dlg.title("Paste toa do (moi dong: X,Y hoac X,Y,Nhan)")
        dlg.configure(bg=C["bg"]); dlg.grab_set()
        dlg.geometry("360x280")
        tk.Label(dlg, text="Moi dong: X,Y  hoac  X,Y,Nhan",
                 bg=C["bg"], fg=C["muted"],
                 font=("Segoe UI", 9)).pack(pady=6)
        txt = tk.Text(dlg, bg=C["entry"], fg=C["text"],
                      insertbackground=C["text"],
                      font=("Consolas", 10), height=10)
        txt.pack(fill="both", expand=True, padx=8)
        # Pre-fill vi du
        txt.insert("end", "564,267,Diem 1\n576,313,Diem 2\n")
        def do_import():
            lines = txt.get("1.0","end").strip().splitlines()
            added = 0
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = [p.strip() for p in line.split(",")]
                try:
                    gx = int(parts[0]); gy = int(parts[1])
                    label = parts[2] if len(parts) > 2 else f"Diem {len(self.pts)+1}"
                    idx   = len(self.pts) + 1
                    self.pts.append(AttackPoint(
                        idx=idx, game_x=gx, game_y=gy, label=label))
                    added += 1
                except (ValueError, IndexError):
                    pass
            self._refresh_tree()
            self._log(f"[Import] Da them {added} toa do")
            dlg.destroy()
        tk.Button(dlg, text=f"✓  Import",
                  command=do_import,
                  bg=C["green"], fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), pady=6,
                  cursor="hand2", bd=0,
                  activebackground=C["green"]).pack(fill="x", padx=8, pady=6)

    def _del_coord(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])["values"]
        idx  = vals[0]
        self.pts = [p for p in self.pts if p.idx != idx]
        for i, p in enumerate(self.pts, 1): p.idx = i
        self._refresh_tree()

    def _move_up(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])["values"]
        idx  = vals[0] - 1   # 0-based
        if idx <= 0: return
        self.pts[idx-1], self.pts[idx] = self.pts[idx], self.pts[idx-1]
        for i, p in enumerate(self.pts, 1): p.idx = i
        self._refresh_tree()
        # Re-select
        children = self.tree.get_children()
        if idx-1 < len(children):
            self.tree.selection_set(children[idx-1])

    def _move_down(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])["values"]
        idx  = vals[0] - 1   # 0-based
        if idx >= len(self.pts) - 1: return
        self.pts[idx], self.pts[idx+1] = self.pts[idx+1], self.pts[idx]
        for i, p in enumerate(self.pts, 1): p.idx = i
        self._refresh_tree()
        children = self.tree.get_children()
        if idx+1 < len(children):
            self.tree.selection_set(children[idx+1])

    def _edit_coord(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])["values"]
        idx  = vals[0] - 1   # 0-based
        pt   = self.pts[idx]
        dlg  = tk.Toplevel(self)
        dlg.title(f"Sua diem {pt.idx}")
        dlg.configure(bg=C["bg"]); dlg.grab_set()
        dlg.geometry("280x190")
        for label, attr, row in [
            ("Game X:", "game_x", 0),
            ("Game Y:", "game_y", 1),
            ("Nhan:",   "label",  2),
        ]:
            tk.Label(dlg, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 9), width=8, anchor="e").grid(
                row=row, column=0, padx=8, pady=6)
            e = tk.Entry(dlg, bg=C["entry"], fg=C["text"],
                         insertbackground=C["text"],
                         font=("Segoe UI", 9), width=14)
            e.insert(0, str(getattr(pt, attr)))
            e.grid(row=row, column=1, padx=4)
            setattr(dlg, f"e_{attr}", e)
        # Status dropdown
        tk.Label(dlg, text="Status:", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 9), width=8, anchor="e").grid(
            row=3, column=0, padx=8, pady=6)
        status_var = tk.StringVar(value=pt.status)
        status_cb  = ttk.Combobox(dlg, textvariable=status_var, width=12,
                                   values=["waiting","going","process","done"],
                                   state="readonly", font=("Segoe UI", 9))
        status_cb.grid(row=3, column=1, padx=4)
        def save():
            try:
                pt.game_x = int(dlg.e_game_x.get())
                pt.game_y = int(dlg.e_game_y.get())
            except ValueError:
                messagebox.showinfo("", "X,Y phai la so!"); return
            pt.label  = dlg.e_label.get().strip()
            pt.status = status_var.get()
            self._refresh_tree(); dlg.destroy()
        tk.Button(dlg, text="Luu", command=save,
                  bg=C["green"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), pady=5,
                  cursor="hand2", bd=0).grid(
            row=4, columnspan=2, pady=8, sticky="ew", padx=8)

    def _take_screenshot_only(self):
        """Chi chup man hinh, hien thi preview."""
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[SS] Ket noi ADB truoc!"); return
            self._log("[SS] Chup...")
            raw = self.bot.adb.screenshot_bytes()
            if not raw: self._log("[SS] That bai"); return
            self._raw = raw
            w, h = self.bot.adb.get_screen_size()
            self._adb_w = w; self._adb_h = h
            self._log(f"[SS] OK {w}x{h}")
            self.after(0, self._draw_preview)
        threading.Thread(target=run, daemon=True).start()

    def _clear_pts(self):
        self.pts.clear()
        self._refresh_tree()
        self._draw_preview()

    def _clear_done_pts(self):
        """Xóa nhanh tất cả điểm có status = done."""
        count = sum(1 for p in self.pts if p.status == "done")
        if count == 0:
            messagebox.showinfo("Thông báo", "Không có điểm nào đã done."); return
        self.pts[:] = [p for p in self.pts if p.status != "done"]
        # Re-index
        for i, p in enumerate(self.pts):
            p.idx = i
        self._refresh_tree()
        self._draw_preview()
        self._log(f"🧹 Xóa {count} điểm done, còn {len(self.pts)} điểm")
        try: self._autosave()
        except: pass

    def _on_tree_click(self, event):
        """Handle click on action columns (✅ Done / ⏳ Wait) in treeview."""
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        # col_id is like '#6' or '#7'  (1-based)
        col_num = int(col_id.replace("#", ""))
        if col_num not in (6, 7):
            return  # not an action column
        vals = self.tree.item(row_id)["values"]
        idx = vals[0] - 1  # 0-based
        if idx < 0 or idx >= len(self.pts):
            return
        new_status = "done" if col_num == 6 else "waiting"
        self.pts[idx].status = new_status
        self._refresh_tree()
        # Re-select
        children = self.tree.get_children()
        if idx < len(children):
            self.tree.selection_set(children[idx])

    _STATUS_TAG = {"waiting":"s_wait","going":"s_go","process":"s_proc","done":"s_done"}

    def _refresh_tree(self, autosave=True):
        self.tree.delete(*self.tree.get_children())
        self.tree.tag_configure("s_wait", foreground=C["muted"])
        self.tree.tag_configure("s_go",   foreground=C["yellow"])
        self.tree.tag_configure("s_proc", foreground=C["accent"])
        self.tree.tag_configure("s_done", foreground=C["green"])
        for pt in self.pts:
            tag = self._STATUS_TAG.get(pt.status, "s_wait")
            self.tree.insert("", "end", tags=(tag,), values=(
                pt.idx, pt.game_x, pt.game_y, pt.label, pt.status,
                "Done", "Wait"))
        self._draw_preview()
        if autosave:
            try: self._autosave()
            except Exception: pass

    def _draw_preview(self):
        if not hasattr(self, 'cv_map'): return
        if not self._raw or not HAS_PIL: return
        img = Image.open(io.BytesIO(self._raw)).convert("RGB")
        img.thumbnail((320, 210))
        ph = ImageTk.PhotoImage(img)
        self.cv_map._ph = ph
        self.cv_map.create_image(0, 0, anchor="nw", image=ph)
        sw = img.width  / self._adb_w
        sh = img.height / self._adb_h
        COLORS = ["#f87171","#fb923c","#fbbf24","#4ade80",
                  "#60a5fa","#c084fc","#f472b6","#34d399"]
        for pt in self.pts:
            dx = pt.game_x * sw
            dy = pt.game_y * sh
            col = COLORS[(pt.idx-1) % len(COLORS)]
            self.cv_map.create_oval(
                dx-7, dy-7, dx+7, dy+7,
                fill=col, outline="white", width=1)
            self.cv_map.create_text(
                dx, dy, text=str(pt.idx),
                font=("Segoe UI", 7, "bold"), fill="white")

    def _run(self):
        if not self.pts:
            messagebox.showinfo("", "Chưa chọn điểm nào!"); return
        if not self.bot.adb.ok:
            messagebox.showinfo("", "Kết nối ADB trước!"); return
        to_run = [p for p in self.pts if p.status == "waiting"]
        if not to_run:
            messagebox.showinfo("", "Không có điểm nào ở trạng thái waiting!"); return
        self._log(f"[Run] ▶ Bắt đầu {len(to_run)}/{len(self.pts)} điểm (waiting)")
        self.bot.start(to_run)

    def _stop(self):
        self.bot.stop()
        self._log("[Run] ■ Đã dừng")

    def _stop_all_captcha(self):
        """Dung TAT CA khi phat hien CAPTCHA: Bot, Wave, Auto Lv, Build."""
        self._log("[CAPTCHA] 🚨🚨🚨 CAPTCHA phát hiện! Dừng MỌI hoạt động!")
        # 1. Stop BotEngine (tab Tan cong)
        self.bot.stop()
        # 2. Stop tat ca WaveGroupEngine (tab Nhieu dot)
        for eng in getattr(self, '_wave_engines', []):
            eng.stop()
        # 3. Stop Auto Lv
        if hasattr(self, '_alv_stop_evt'):
            self._alv_stop_evt.set()
        # 4. Stop Build
        if hasattr(self, 'build_eng'):
            self.build_eng.stop()
        # 5. Beep canh bao
        def _alarm():
            try:
                import winsound
                for _ in range(15):
                    winsound.Beep(1200, 300)
                    time.sleep(0.15)
                    winsound.Beep(800, 300)
                    time.sleep(0.15)
            except Exception:
                pass
        threading.Thread(target=_alarm, daemon=True).start()
        # 6. Telegram notify
        if self.tg_bot.is_running:
            def _tg_captcha():
                try:
                    raw = self.bot.adb.screenshot_bytes()
                    png = None
                    if raw:
                        import cv2, numpy as np
                        arr = np.frombuffer(raw, np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        _, buf = cv2.imencode(".png", img)
                        png = buf.tobytes()
                    self.tg_bot.notify_captcha(png_bytes=png)
                except Exception:
                    self.tg_bot.notify_captcha()
            threading.Thread(target=_tg_captcha, daemon=True).start()

    def _check_atk_icon(self):
        """Chup man hinh va kiem tra icon ATK bang template matching."""
        if not self.bot.adb.ok:
            messagebox.showwarning("", "Chua ket noi ADB!"); return
        def _run():
            self._log("[ATK] Chup man hinh...")
            screen = self.bot.adb.screenshot_cv2()
            if screen is None:
                self._log("[ATK] Khong chup duoc man hinh"); return
            pos = self.bot.det.find_atk_btn(screen)
            if pos:
                self._log(f"[ATK] Tim thay icon ATK tai: {pos}")
            else:
                self._log("[ATK] Khong tim thay icon ATK (threshold 0.65)")
        threading.Thread(target=_run, daemon=True).start()

    def _manual_heal(self):
        """Dat co heal - bot se hoi mau sau khi xong diem hien tai."""
        if not self.bot.adb.ok:
            messagebox.showwarning("", "Chua ket noi ADB!"); return
        bot_running = self.bot._thread and self.bot._thread.is_alive()
        if bot_running:
            # Bot dang chay -> dat co, doi sau khi xong diem hien tai
            self.bot._heal_requested.set()
            self._log("[Heal] Co hoi mau da dat - se hoi sau khi xong diem hien tai")
        else:
            # Bot khong chay -> heal ngay roi attack diem tiep theo
            self._save_cfg()
            def _run():
                # FIX BUG 2: Clear _stop truoc khi heal, neu khong while loop
                # ben trong _do_heal se thoat ngay vi _stop van dang set tu lan chay truoc
                self.bot._stop.clear()
                self._log("[Heal] Hoi mau thu cong...")
                self.bot._do_heal()
                self._log("[Heal] Xong")
                pending = [p for p in self.pts if p.status == "waiting"]
                if pending:
                    pt = pending[0]
                    self._log(f"[Heal] Tiep theo: ({pt.game_x},{pt.game_y})")
                    pt.status = "going"
                    self.after(0, self._refresh_tree)
                    # FIX BUG 1: _attack_one khong ton tai, dung _attack
                    ok = self.bot._attack(pt)
                    pt.status = "done" if ok else "waiting"
                    self._log(f"[Heal] Ket qua: {'OK' if ok else 'FAIL'}")
                    self.after(0, self._refresh_tree)
                else:
                    self._log("[Heal] Khong con diem nao dang cho")
            threading.Thread(target=_run, daemon=True).start()

    def _test(self):
        if not self.pts:
            messagebox.showinfo("", "Chưa chọn điểm!"); return
        pt = self.pts[0]
        def run():
            self._log(f"[Test] Navigate to ({pt.game_x},{pt.game_y})")
            self.bot.navigate_to(pt.game_x, pt.game_y)
            time.sleep(0.5)
            cap = self.bot.det.find_capture_btn(
                self.bot.adb.screenshot_cv2())
            self._log(f"[Test] Nút Chiếm: {cap}")
        threading.Thread(target=run, daemon=True).start()

    def _test_nav(self):
        """Test chi phan nhap toa do - khong can diem trong list."""
        # Hoi user nhap X,Y de test
        dlg = tk.Toplevel(self)
        dlg.title("Test Nav - Nhap toa do")
        dlg.configure(bg=C["bg"]); dlg.grab_set()
        dlg.geometry("280x180")
        for row, (lbl, val) in enumerate([("Game X:", "574"), ("Game Y:", "317")]):
            tk.Label(dlg, text=lbl, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 10), width=9, anchor="e").grid(
                row=row, column=0, padx=8, pady=8)
            e = tk.Entry(dlg, bg=C["entry"], fg=C["text"],
                         insertbackground=C["text"],
                         font=("Segoe UI", 11), width=10)
            e.insert(0, val)
            e.grid(row=row, column=1, padx=4, pady=8)
            setattr(dlg, f"e{row}", e)
        status = tk.Label(dlg, text="San sang...",
                          bg=C["bg"], fg=C["muted"],
                          font=("Segoe UI", 8))
        status.grid(row=2, columnspan=2, pady=4)
        def do_test():
            try:
                gx = int(dlg.e0.get()); gy = int(dlg.e1.get())
            except ValueError:
                status.config(text="X,Y phai la so!"); return
            status.config(text=f"Dang test ({gx},{gy})...")
            dlg.update()
            def run():
                if not self.bot.adb.ok:
                    if not self.bot.adb.connect():
                        self.after(0, lambda: status.config(
                            text="ADB chua ket noi!")); return
                ok = self.bot.navigate_to(gx, gy)
                self.after(0, lambda: status.config(
                    text=f"{'OK' if ok else 'FAIL'} ({gx},{gy})"))
                self._log(f"[TestNav] navigate_to({gx},{gy}) = {ok}")
            threading.Thread(target=run, daemon=True).start()
        tk.Button(dlg, text="▶  Chay test",
                  command=do_test,
                  bg=C["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 10, "bold"), pady=7,
                  cursor="hand2", bd=0).grid(
            row=3, columnspan=2, sticky="ew", padx=8, pady=4)

    # ── Callbacks ────────────────────────────────

    def _on_state(self, s: State):
        def upd():
            color = STATE_COLOR.get(s, C["muted"])
            self.lbl_state.config(text=f"● {s.name}", fg=color)
            if self.bot.cur_pt:
                pt = self.bot.cur_pt
                n  = len(self.pts)
                self.lbl_prog.config(
                    text=f"Điểm {pt.idx} / {n}\n"
                         f"Game ({pt.game_x},{pt.game_y})\n"
                         f"State: {s.name}")
        self.after(0, upd)

    def _schedule_refresh_tree(self):
        try:
            if self._tree_refresh_after_id:
                self.after_cancel(self._tree_refresh_after_id)
            self._tree_refresh_after_id = self.after(80, self._flush_refresh_tree)
        except Exception:
            self.after(0, self._refresh_tree)

    def _flush_refresh_tree(self):
        self._tree_refresh_after_id = None
        self._refresh_tree()

    def _schedule_build_refresh(self):
        try:
            if self._build_refresh_after_id:
                self.after_cancel(self._build_refresh_after_id)
            self._build_refresh_after_id = self.after(120, self._flush_build_refresh)
        except Exception:
            self.after(0, self._refresh_build_tree)

    def _flush_build_refresh(self):
        self._build_refresh_after_id = None
        self._refresh_build_tree()

    def _queue_autosave(self, delay_ms: int = 600):
        try:
            if self._autosave_after_id:
                self.after_cancel(self._autosave_after_id)
            self._autosave_after_id = self.after(delay_ms, self._autosave_now)
        except Exception:
            self._autosave_now()

    def _autosave_now(self):
        self._autosave_after_id = None
        try:
            name = self._profile_var.get().strip() or "default"
            path = CONFIG_PATH if name == "default" else self._profile_path(name)
            self._save_cfg_to(path)
        except Exception:
            pass

    def _refresh_profiles(self):
        profiles = ["default"] + self._list_profiles()
        self._profile_cb["values"] = profiles

    def _autosave(self):
        """Tu dong luu khi diem thay doi."""
        self._queue_autosave()

    def _save_all(self):
        """Ap dung cai dat + luu vao profile dang chon."""
        self._save_cfg_ui()
        self._save_profile()

    def _hotkey_save(self):
        """Ctrl+S: lưu tất cả cài đặt + profile hiện tại."""
        try:
            self._save_all()
            self._log("💾 [Ctrl+S] Đã lưu tất cả!")
        except Exception as e:
            self._log(f"💾 [Ctrl+S] Lỗi lưu: {e}")

    # ── Telegram ─────────────────────────────────

    def _telegram_start(self):
        """Khởi động Telegram bot."""
        token = self.e_tg_token.get().strip() if hasattr(self, 'e_tg_token') else self.cfg.telegram_token
        chat_id = self.e_tg_chat.get().strip() if hasattr(self, 'e_tg_chat') else self.cfg.telegram_chat_id
        self.cfg.telegram_token = token
        self.cfg.telegram_chat_id = chat_id
        self.tg_bot.token = token
        self.tg_bot.chat_id = chat_id
        # Gán label: profile + device serial
        profile = self._current_profile or "default"
        serial = self.cfg.device_serial
        self.tg_bot.device_label = f"{profile} @ {serial}"
        self.tg_bot.start()

    def _telegram_stop(self):
        """Dừng Telegram bot."""
        self.tg_bot.stop()

    def _telegram_test(self):
        """Gửi tin nhắn test."""
        token = self.e_tg_token.get().strip()
        chat_id = self.e_tg_chat.get().strip()
        if not token or not chat_id:
            messagebox.showerror("Lỗi", "Nhập Token và Chat ID!"); return
        self.cfg.telegram_token = token
        self.cfg.telegram_chat_id = chat_id
        self.tg_bot.token = token
        self.tg_bot.chat_id = chat_id
        self.tg_bot.send_message("🧪 Test từ NTA Bot — kết nối OK!")
        self._log("[Telegram] 📩 Đã gửi tin nhắn test")

    def _telegram_on_command(self, cmd: str, args: str) -> str:
        """Xử lý lệnh từ Telegram. Trả về reply text."""
        if cmd == "/status":
            lines = ["📊 <b>Trạng thái NTA Bot</b>\n"]
            # Device info
            profile = self._current_profile or "default"
            serial = self.cfg.device_serial
            adb_ok = "✅" if self.bot.adb.ok else "❌"
            lines.append(f"📱 Profile: <b>{profile}</b>")
            lines.append(f"🔌 ADB: {serial} {adb_ok}\n")
            # Wave groups
            for g in self.wave_groups:
                eng = None
                for e in self._wave_engines:
                    if e.group is g:
                        eng = e; break
                status = eng.status if eng else "idle"
                done = sum(1 for p in g.points if p.get("status") == "done")
                total = len(g.points)
                lines.append(f"🌊 {g.name}: {status} ({done}/{total} done)")
            if not self.wave_groups:
                lines.append("🌊 Chưa có nhóm wave")
            return "\n".join(lines)

        elif cmd == "/screenshot":
            try:
                raw = self.bot.adb.screenshot_bytes()
                if raw:
                    import cv2, numpy as np
                    arr = np.frombuffer(raw, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    _, png = cv2.imencode(".png", img)
                    self.tg_bot.send_photo(png.tobytes(), caption="📷 Screenshot")
                    return ""
                else:
                    return "⚠️ Không chụp được màn hình"
            except Exception as e:
                return f"⚠️ Lỗi screenshot: {e}"

        elif cmd == "/wave_start":
            if not args:
                return "⚠️ Cần tên nhóm: /wave_start Nhóm 1"
            for g in self.wave_groups:
                if g.name.lower() == args.lower():
                    w = self._wave_group_widgets.get(g.name, {})
                    self.after(0, lambda _g=g, _w=w: self._wave_start_group(_g, _w))
                    return f"▶ Đang chạy nhóm '{g.name}'..."
            return f"⚠️ Không tìm thấy nhóm '{args}'"

        elif cmd == "/wave_stop":
            if not args:
                return "⚠️ Cần tên nhóm: /wave_stop Nhóm 1"
            for g in self.wave_groups:
                if g.name.lower() == args.lower():
                    self.after(0, lambda _g=g: self._wave_stop_group(_g))
                    return f"⏹ Đã dừng nhóm '{g.name}'"
            return f"⚠️ Không tìm thấy nhóm '{args}'"

        elif cmd == "/wave_skip":
            if not args:
                return "⚠️ Cần tên nhóm: /wave_skip Nhóm 1"
            for g in self.wave_groups:
                if g.name.lower() == args.lower():
                    self.after(0, lambda _g=g: self._wave_skip_group(_g))
                    return f"⏭ Skip cooldown nhóm '{g.name}'"
            return f"⚠️ Không tìm thấy nhóm '{args}'"

        elif cmd == "/stop_all":
            self.after(0, self._wave_stop_all)
            return "⏹ Đã dừng tất cả!"

        elif cmd == "/tap" and args:
            # /tap X Y — tap toa do cu the
            try:
                parts = args.split()
                x, y = int(parts[0]), int(parts[1])
                png = self._telegram_on_tap(x, y)
                if png:
                    self.tg_bot.send_photo(png, caption=f"👆 Tap ({x},{y})")
                    return ""
                else:
                    return f"👆 Tap ({x},{y}) — không chụp được screenshot"
            except (ValueError, IndexError):
                return "⚠️ Sai cú pháp. Dùng: /tap 270 480"

        else:
            return f"⚠️ Lệnh không hợp lệ. Gõ /help để xem danh sách."

    def _telegram_on_tap(self, x, y) -> Optional[bytes]:
        """Tap ADB tại (x,y) rồi chụp screenshot. x=None → chỉ chụp."""
        try:
            if not self.bot.adb.ok:
                return None
            if x is not None and y is not None:
                self.bot.adb.tap(x, y)
                time.sleep(0.8)
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                return None
            import cv2, numpy as np
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            _, png = cv2.imencode(".png", img)
            return png.tobytes()
        except Exception as e:
            self._log(f"[Telegram] ⚠️ Tap/screenshot lỗi: {e}")
            return None

    def _tg_notify_error(self, group_name: str, msg: str):
        """Gửi screenshot + thông báo lỗi qua Telegram khi engine dừng."""
        if not self.tg_bot.is_running:
            return
        def _send():
            try:
                png = self._telegram_on_tap(None, None)  # chỉ chụp
                self.tg_bot.notify_error(group_name, msg)
                if png:
                    self.tg_bot.send_photo(png, caption=f"❌ {group_name}: {msg}")
            except Exception:
                self.tg_bot.notify_error(group_name, msg)
        threading.Thread(target=_send, daemon=True).start()

    def _save_profile(self):
        name = self._profile_var.get().strip()
        if not name:
            messagebox.showerror("Lỗi", "Nhập tên profile!"); return
        self._save_cfg_ui()  # sync UI -> cfg first
        if name == "default":
            self._save_cfg()
            self._log(f"[Profile] 💾 Lưu default")
        else:
            path = self._profile_path(name)
            self._save_cfg_to(path)
            self._log(f"[Profile] 💾 Lưu '{name}' → {os.path.basename(path)}")
        self._refresh_profiles()
        self._update_title(name)

    def _load_profile(self):
        name = self._profile_var.get().strip()
        if not name:
            messagebox.showerror("Lỗi", "Chọn hoặc nhập tên profile!"); return
        if name == "default":
            path = CONFIG_PATH
        else:
            path = self._profile_path(name)
        if not os.path.exists(path):
            messagebox.showerror("Lỗi", f"Không tìm thấy '{os.path.basename(path)}'"); return
        self.cfg = self._load_cfg_from(path)
        self.bot.cfg = self.cfg
        self.bot.adb.cfg = self.cfg
        self.bot.sel.cfg = self.cfg
        self._populate_ui()
        # Load points if saved
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if "_points" in d:
                self.pts = [
                    AttackPoint(idx=p["idx"], game_x=p["game_x"],
                                game_y=p["game_y"], label=p.get("label",""),
                                status=p.get("status","waiting"))
                    for p in d["_points"]
                ]
                self._refresh_tree()
                self._log(f"[Profile] 📂 Load '{name}' ({len(self.pts)} điểm)")
            if "_build_tasks" in d:
                self.build_cfg.tasks = d["_build_tasks"]
                self._refresh_build_tree()
            # Load resources từ file chung (dùng chung mọi profile)
            self._res_data = self._res_load_file()
            for b in self._RES_BUILDINGS:
                if b not in self._res_data:
                    self._res_data[b] = [{"wood": 0, "stone": 0, "done": False} for _ in range(20)]
            if hasattr(self, '_res_vars'): self._res_populate()
            if "_wave_groups" in d:
                self.wave_groups = [
                    WaveGroup(name=g.get("name",""), troop_names=g.get("troop_names",[]),
                              points=g.get("points",[]), enabled=g.get("enabled",True),
                              heal_every=g.get("heal_every",0), heal_step=g.get("heal_step",0),
                              heal_x=g.get("heal_x",500), heal_y=g.get("heal_y",300),
                              heal_on_done=g.get("heal_on_done",False),
                              cooldown_sec=g.get("cooldown_sec",0), lv2_cooldown_sec=g.get("lv2_cooldown_sec",0),
                              lv3_cooldown_sec=g.get("lv3_cooldown_sec",0), lv4_cooldown_sec=g.get("lv4_cooldown_sec",0))
                    for g in d["_wave_groups"]
                ]
                if hasattr(self, '_wave_group_nb'): self._wave_refresh_all()
            if "_scout_cfg" in d:
                sc = d["_scout_cfg"]
                for sv, key in [(self._sv_ox,"ox"),(self._sv_oy,"oy"),(self._sv_up,"up"),
                                (self._sv_down,"down"),(self._sv_left,"left"),(self._sv_right,"right")]:
                    if sc.get(key): sv.set(sc[key])
            # Load tiles bản đồ + dữ liệu do thám theo profile
            self._scout_load_map(name)
            self._spy_load(name)
        except Exception as e:
            self._log(f"[Profile] 📂 Load '{name}' (lỗi đọc điểm: {e})")
        self._update_title(name)

    def _populate_ui(self):
        """Dien lai tat ca o UI tu self.cfg sau khi load profile."""
        def _set(e, v):
            e.delete(0, "end"); e.insert(0, str(v))
        _set(self.e_adb,       self.cfg.adb_path)
        if hasattr(self, 'e_game_start'):
            if self.cfg.game_start:
                self.e_game_start.delete(0, "end")
                self.e_game_start.insert(0, self.cfg.game_start)
                self.e_game_start.config(fg=C["text"])
            else:
                self.e_game_start.delete(0, "end")
                self.e_game_start.insert(0, "YYYY-MM-DD HH:MM")
                self.e_game_start.config(fg=C["muted"])
        _set(self.e_dev,       self.cfg.device_serial)
        _set(self.e_cool,      self.cfg.cooldown_sec)
        _set(self.e_cool_lv2,  self.cfg.lv2_cooldown_sec)
        _set(self.e_cool_lv3,  self.cfg.lv3_cooldown_sec)
        _set(self.e_cool_lv4,  self.cfg.lv4_cooldown_sec)
        _set(self.e_ol_url,   self.cfg.ollama_url)
        _set(self.e_ol_model, self.cfg.ollama_model)
        self._ol_var.set(self.cfg.ollama_enabled)
        _toggle_ol() if hasattr(self, '_toggle_ol_fn') else None
        if hasattr(self, 'e_hwarn'):
            _set(self.e_hwarn, self.cfg.health_warn_avg)
            _set(self.e_hheal, self.cfg.health_force_heal_avg)
            _set(self.e_hcrit_ratio, self.cfg.health_critical_ratio)
            _set(self.e_hcrit_count, self.cfg.health_force_heal_critical_units)
            self._brain_var.set(self.cfg.army_health_enabled)
        if hasattr(self, 'e_tg_token'):
            _set(self.e_tg_token, self.cfg.telegram_token)
            _set(self.e_tg_chat,  self.cfg.telegram_chat_id)
            self._tg_var.set(self.cfg.telegram_enabled)

    def _skip_next(self):
        self.bot.skip_cooldown()

    def _check_army(self):
        """Bam mo man hinh All Armies, OCR doc trang thai, bam back."""
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[Army] Kết nối ADB trước!"); return
            self._log("[Army] 🔍 Mở màn hình quân...")
            self.bot.adb.tap(666, 1216)   # mo All Armies
            time.sleep(1.5)
            armies = self.bot.adb.read_army_status(required_names=self.cfg.troop_names)
            self.bot.adb.tap(342, 1216)  # back
            time.sleep(0.8)
            if not armies:
                self._log("[Army] Không đọc được trạng thái quân"); return
            for a in armies:
                self._log(f"[Army] {a['name']:12s} → {a['status']}")
            summary = {}
            for a in armies:
                summary[a["status"]] = summary.get(a["status"], 0) + 1
            parts = " | ".join(f"{k}:{v}" for k,v in summary.items())
            self._log(f"[Army] Tổng: {len(armies)} | {parts}")
        threading.Thread(target=run, daemon=True).start()

    def _check_army_health(self):
        """Mở All Armies, đọc thanh máu và chạy Brain rule engine."""
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[ArmyHP] Kết nối ADB trước!"); return
            self._save_cfg_ui()
            self._log("[ArmyHP] 🔍 Mở màn hình quân để đọc thanh máu...")
            self.bot.adb.tap(666, 1216)
            time.sleep(1.5)
            troops = self.bot.adb.read_army_health(required_names=self.cfg.troop_names)
            self.bot.adb.tap(342, 1216)
            time.sleep(0.8)
            if not troops:
                self._log("[ArmyHP] Không đọc được máu quân"); return
            summary = self.bot.brain.summarize(troops, next_label='')
            for a in summary.get('troops', []):
                self._log(
                    f"[ArmyHP] {a['name']:10s} | HP {a['avg_hp']*100:5.1f}% | min {a['min_hp']*100:5.1f}% | "
                    f"crit {a['critical_units']}/{a['unit_count']} | risk {a['risk_score']} | {a['action']}")
            if summary.get('force_heal'):
                self._log('[ArmyHP] 🩸 Khuyến nghị: HEAL NOW')
            elif summary.get('low_risk_only'):
                self._log('[ArmyHP] ⚠️ Khuyến nghị: chỉ đánh mục tiêu nhẹ')
            else:
                self._log('[ArmyHP] ✅ Khuyến nghị: tiếp tục bình thường')
        threading.Thread(target=run, daemon=True).start()

    def _test_ai(self):
        """Chup anh hien tai, gui len Ollama, log ket qua + luu anh debug."""
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[AI Test] Kết nối ADB trước!"); return
            self._log("[AI Test] Chụp ảnh...")
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                self._log("[AI Test] Chụp ảnh thất bại"); return
            # Luu anh de kiem tra
            import datetime as _dt
            ts  = _dt.datetime.now().strftime("%H%M%S")
            img_path = os.path.join(DEBUG_DIR, f"ai_test_{ts}.png")
            with open(img_path, "wb") as f: f.write(raw)
            self._log(f"[AI Test] Ảnh lưu: {img_path}")
            # Gui len Ollama
            self._log(f"[AI Test] Gửi lên {self.cfg.ollama_model}...")
            ollama = OllamaVision(self.cfg.ollama_url, self.cfg.ollama_model)
            result = ollama.is_occupying(raw)
            raw_ans = ollama._last_response if hasattr(ollama, "_last_response") else "?"
            if result is True:
                self._log(f"[AI Test] ✅ KẾT QUẢ: Đang chiếm (YES)")
            elif result is False:
                self._log(f"[AI Test] ✅ KẾT QUẢ: Không chiếm (NO)")
            else:
                self._log(f"[AI Test] ⚠️ KẾT QUẢ: Không xác định")
            self._log(f"[AI Test] Raw: {raw_ans}")
        threading.Thread(target=run, daemon=True).start()

    def _test_ocr(self):
        """Chup anh hien tai, chay pytesseract, log ket qua thu cong."""
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[OCR Test] Kết nối ADB trước!"); return
            self._log("[OCR Test] Chụp ảnh...")
            import cv2, numpy as np, pytesseract, datetime as _dt
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                self._log("[OCR Test] Chụp ảnh thất bại"); return
            arr  = np.frombuffer(raw, np.uint8)
            img  = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            img2  = cv2.resize(img, (w*2, h*2), interpolation=cv2.INTER_CUBIC)
            gray  = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
            _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text  = pytesseract.image_to_string(th, config="--psm 6")
            # Luu file debug
            ts = _dt.datetime.now().strftime("%H%M%S")
            os.makedirs(DEBUG_DIR, exist_ok=True)
            cv2.imwrite(os.path.join(DEBUG_DIR, f"ocr_test_{ts}.png"), th)
            with open(os.path.join(DEBUG_DIR, f"ocr_test_{ts}.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            # Log tung dong
            lines = [l for l in text.splitlines() if l.strip()]
            self._log(f"[OCR Test] 📄 {len(lines)} dòng:")
            for l in lines:
                self._log(f"[OCR Test]   {l}")
        threading.Thread(target=run, daemon=True).start()
        self.after(0, self._refresh_tree)

    def _clear_debug_log(self):
        try:
            self.debug_log_txt.configure(state="normal")
            self.debug_log_txt.delete("1.0", "end")
            self.debug_log_txt.configure(state="disabled")
        except Exception:
            pass

    def _is_debug_msg(self, msg: str) -> bool:
        tags = (
            "[Popup]", "[CAPTCHA]", "[ATK]", "[Army]", "[ArmyHP]",
            "[City]", "[AI Test]", "[OCR Test]", "[OCR]", "[💰 OCR]",
            "[Test]", "[TestNav]"
        )
        return any(t in msg for t in tags)

    def _detect_popup_type(self):
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[Popup] Kết nối ADB trước!")
                    return
            self._log("[Popup] Chụp màn hình để detect popup...")
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                self._log("[Popup] Chụp ảnh thất bại")
                return
            try:
                import cv2, numpy as np, datetime as _dt
                arr = np.frombuffer(raw, np.uint8)
                screen = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if screen is None:
                    self._log("[Popup] Không giải mã được ảnh màn hình")
                    return
                ptype = self.bot.det.detect_popup_type(screen)
                if ptype:
                    self._log(f"[Popup] ✅ detect_popup_type = {ptype}")
                else:
                    self._log("[Popup] ℹ Không thấy popup")
                os.makedirs(DEBUG_DIR, exist_ok=True)
                ts = _dt.datetime.now().strftime("%H%M%S")
                cv2.imwrite(os.path.join(DEBUG_DIR, f"popup_type_{ts}.png"), screen)
            except Exception as e:
                self._log(f"[Popup] Lỗi detect: {e}")
        threading.Thread(target=run, daemon=True).start()

    def _test_city(self):
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[City] Kết nối ADB trước!"); return
            result = self.bot.adb.is_in_city()
            if result:
                self._log("[City] 🏰 Đang TRONG thành (nút xanh lá)")
            else:
                self._log("[City] 🗺 Đang NGOÀI thành (nút nhiều màu)")
        threading.Thread(target=run, daemon=True).start()

    def _check_captcha_debug(self):
        """Chụp màn hình hiện tại và kiểm tra popup CAPTCHA / Random Test."""
        def run():
            if not self.bot.adb.ok:
                if not self.bot.adb.connect():
                    self._log("[CAPTCHA] Kết nối ADB trước!"); return
            self._log("[CAPTCHA] Chụp màn hình để kiểm tra...")
            raw = self.bot.adb.screenshot_bytes()
            if not raw:
                self._log("[CAPTCHA] Chụp ảnh thất bại")
                return
            try:
                import cv2, numpy as np, datetime as _dt
                arr = np.frombuffer(raw, np.uint8)
                screen = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if screen is None:
                    self._log("[CAPTCHA] Không giải mã được ảnh màn hình")
                    return
                has_captcha = self.bot.det.has_captcha_popup(screen)

                # Lưu vùng OCR để debug khi cần
                h, w = screen.shape[:2]
                y0, y1 = int(h * 0.15), int(h * 0.55)
                x0, x1 = int(w * 0.10), int(w * 0.90)
                roi = screen[y0:y1, x0:x1]
                os.makedirs(DEBUG_DIR, exist_ok=True)
                ts = _dt.datetime.now().strftime("%H%M%S")
                cv2.imwrite(os.path.join(DEBUG_DIR, f"captcha_check_{ts}.png"), roi)

                if has_captcha:
                    self._log("[CAPTCHA] 🚨 PHÁT HIỆN popup CAPTCHA / Random Test")
                else:
                    self._log("[CAPTCHA] ✅ Không thấy popup CAPTCHA")
                self._log(f"[CAPTCHA] ROI debug: {os.path.join(DEBUG_DIR, f'captcha_check_{ts}.png')}")
            except Exception as e:
                self._log(f"[CAPTCHA] Lỗi kiểm tra: {e}")
        threading.Thread(target=run, daemon=True).start()

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        def a():
            line = f"[{ts}] {msg}\n"
            self.txt.configure(state="normal")
            self.txt.insert("end", line)
            self.txt.see("end")
            self.txt.configure(state="disabled")
            if self._is_debug_msg(msg):
                try:
                    self.debug_log_txt.configure(state="normal")
                    self.debug_log_txt.insert("end", line)
                    self.debug_log_txt.see("end")
                    self.debug_log_txt.configure(state="disabled")
                except Exception:
                    pass
            # Ghi them vao build log neu la [Build]
            if "[Build]" in msg:
                try:
                    self.build_log_txt.configure(state="normal")
                    self.build_log_txt.insert("end", line)
                    self.build_log_txt.see("end")
                    self.build_log_txt.configure(state="disabled")
                except Exception:
                    pass
        self.after(0, a)

    def _tick(self):
        cur = self.bot.state
        for s, (dot, lbl) in self._sdots.items():
            c = STATE_COLOR.get(s, C["muted"])
            if s == cur:
                dot.config(fg=c, text="●")
                lbl.config(fg=c, font=("Consolas", 9, "bold"))
            else:
                dot.config(fg=C["muted"], text="○")
                lbl.config(fg=C["muted"], font=("Consolas", 9))
        self.after(500, self._tick)


# ──────────────────────────────────────────────
#  ENTRY
# ──────────────────────────────────────────────

if __name__ == "__main__":
    print(f"ADB: {DEFAULT_ADB}")
    if not HAS_CV2: print("WARN: pip install opencv-python")
