"""
GridPicker - Interactive map grid for selecting attack points.
"""
import io
import math
import tkinter as tk
from tkinter import messagebox
from typing import List, Callable

from config import (
    AttackPoint, C, GRID_COLS, GRID_ROWS,
    HAS_PIL,
)

if HAS_PIL:
    from PIL import Image, ImageTk


class GridPicker(tk.Toplevel):
    DISP_W = 480
    DISP_H = 720

    def __init__(self, parent, raw_bytes: bytes,
                 adb_w: int, adb_h: int,
                 existing: List[AttackPoint],
                 on_confirm: Callable):
        super().__init__(parent)
        self.title("Chọn điểm tấn công")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()

        self.adb_w      = adb_w
        self.adb_h      = adb_h
        self.on_confirm = on_confirm
        self.points: List[AttackPoint] = list(existing)
        self._next_idx  = (max((p.idx for p in existing), default=0) + 1)

        # Scale ADB → display
        self.sx = self.DISP_W / adb_w
        self.sy = self.DISP_H / adb_h

        if HAS_PIL:
            img_full = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            self._img_disp = img_full.resize(
                (self.DISP_W, self.DISP_H), Image.LANCZOS)
        else:
            self._img_disp = None

        self._hover_cell = (-1, -1)
        self._build()
        self._draw()

    def _build(self):
        # Header
        hdr = tk.Frame(self, bg=C["panel"], height=46)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Label(hdr,
                 text="🗺  Click để thêm điểm  •  Click lại vào điểm để xóa",
                 bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 10, "bold")).pack(
            side="left", padx=14, pady=12)

        # Canvas
        cf = tk.Frame(self, bg=C["border"], bd=1)
        cf.pack(padx=10, pady=(6,0))
        self.cv = tk.Canvas(cf,
                            width=self.DISP_W, height=self.DISP_H,
                            bg="#080a12", cursor="crosshair",
                            highlightthickness=0)
        self.cv.pack()
        self.cv.bind("<Button-1>", self._on_click)
        self.cv.bind("<Motion>",   self._on_hover)

        # Bottom
        bot = tk.Frame(self, bg=C["bg"])
        bot.pack(fill="x", padx=10, pady=8)

        # List
        lf = tk.LabelFrame(bot, text=" Thứ tự tấn công ",
                           bg=C["panel"], fg=C["accent"],
                           font=("Segoe UI", 9, "bold"), bd=1)
        lf.pack(side="left", fill="both", expand=True, padx=(0,8))
        self.lst = tk.Listbox(lf, bg=C["card"], fg=C["text"],
                              font=("Consolas", 9), height=6,
                              selectbackground=C["accent"],
                              relief="flat", bd=0)
        self.lst.pack(fill="both", expand=True, padx=4, pady=4)

        # Buttons
        rf = tk.Frame(bot, bg=C["bg"])
        rf.pack(side="right")
        for text, cmd, color in [
            ("Xóa cuối",  self._pop,    C["red"]),
            ("Xóa hết",   self._clear,  "#4a5580"),
            ("✓ Xác nhận", self._ok,    C["green"]),
        ]:
            tk.Button(rf, text=text, command=cmd,
                      bg=color, fg="white", relief="flat",
                      font=("Segoe UI", 9, "bold"),
                      padx=14, pady=6, cursor="hand2", bd=0,
                      activebackground=color).pack(
                fill="x", pady=3)

        # Coord bar
        self.lbl_coord = tk.Label(
            self, text="Di chuột lên bản đồ...",
            bg=C["bg"], fg=C["muted"],
            font=("Consolas", 8))
        self.lbl_coord.pack(pady=(0,4))

    def _draw(self):
        self.cv.delete("all")
        # Background
        if HAS_PIL and self._img_disp:
            ph = ImageTk.PhotoImage(self._img_disp)
            self.cv._ph = ph
            self.cv.create_image(0, 0, anchor="nw", image=ph)

        cell_w = self.DISP_W / GRID_COLS
        cell_h = self.DISP_H / GRID_ROWS

        # Hover highlight
        hc, hr = self._hover_cell
        if hc >= 0:
            self.cv.create_rectangle(
                hc * cell_w, hr * cell_h,
                (hc+1) * cell_w, (hr+1) * cell_h,
                fill="", outline="#aaaacc", width=1)

        # Grid lines (Tkinter chi ho tro #rrggbb, khong co alpha)
        for i in range(GRID_COLS + 1):
            x = i * cell_w
            self.cv.create_line(x, 0, x, self.DISP_H,
                                fill="#2a2d50", width=1)
        for j in range(GRID_ROWS + 1):
            y = j * cell_h
            self.cv.create_line(0, y, self.DISP_W, y,
                                fill="#2a2d50", width=1)

        # Points
        COLORS = ["#f87171","#fb923c","#fbbf24","#4ade80",
                  "#60a5fa","#c084fc","#f472b6","#34d399"]
        # Mau glow nhat hon (thay the alpha)
        GLOW   = ["#7a3838","#7a4918","#7a5e10","#1f6b3e",
                  "#2d4f7a","#5c3a7a","#7a2258","#1a6b4e"]
        for pt in self.points:
            dx = pt.game_x * self.sx
            dy = pt.game_y * self.sy
            col  = COLORS[(pt.idx - 1) % len(COLORS)]
            glow = GLOW[(pt.idx - 1) % len(GLOW)]
            # Outer glow (dung mau toi thay alpha)
            self.cv.create_oval(dx-16, dy-16, dx+16, dy+16,
                                fill=glow, outline="", width=0)
            # Circle
            self.cv.create_oval(dx-11, dy-11, dx+11, dy+11,
                                fill=col, outline="white", width=1)
            # Number
            self.cv.create_text(dx, dy, text=str(pt.idx),
                                font=("Segoe UI", 8, "bold"),
                                fill="white")
            # Connector line to next
            nxt = next((p for p in self.points
                        if p.idx == pt.idx + 1), None)
            if nxt:
                nx = nxt.game_x * self.sx
                ny = nxt.game_y * self.sy
                self.cv.create_line(dx, dy, nx, ny,
                                    fill=col, width=1,
                                    dash=(4, 3))

        # Update list
        self.lst.delete(0, "end")
        for pt in self.points:
            self.lst.insert("end",
                f"  {pt.idx}. ({pt.game_x:4d},{pt.game_y:4d})")

    def _on_click(self, e):
        ax = int(e.x / self.sx)
        ay = int(e.y / self.sy)
        # Xóa nếu click gần điểm cũ
        for pt in self.points:
            ddx = pt.game_x * self.sx - e.x
            ddy = pt.game_y * self.sy - e.y
            if math.hypot(ddx, ddy) < 16:
                self.points.remove(pt)
                for i, p in enumerate(self.points, 1): p.idx = i
                self._next_idx = len(self.points) + 1
                self._draw(); return
        # Snap vào tâm ô grid
        cw = self.adb_w / GRID_COLS
        ch = self.adb_h / GRID_ROWS
        snap_x = int((ax // cw + 0.5) * cw)
        snap_y = int((ay // ch + 0.5) * ch)
        self.points.append(AttackPoint(
            idx=self._next_idx,
            game_x=snap_x, game_y=snap_y,
            label=f"Điểm {self._next_idx}"))
        self._next_idx += 1
        self._draw()

    def _on_hover(self, e):
        cw = self.DISP_W / GRID_COLS
        ch = self.DISP_H / GRID_ROWS
        col = int(e.x / cw)
        row = int(e.y / ch)
        if (col, row) != self._hover_cell:
            self._hover_cell = (col, row)
            self._draw()
        ax = int(e.x / self.sx)
        ay = int(e.y / self.sy)
        self.lbl_coord.config(
            text=f"ADB: ({ax},{ay})  Grid: [{col},{row}]")

    def _pop(self):
        if self.points:
            self.points.pop()
            self._next_idx = len(self.points) + 1
            for i, p in enumerate(self.points, 1): p.idx = i
            self._draw()

    def _clear(self):
        self.points.clear(); self._next_idx = 1; self._draw()

    def _ok(self):
        if not self.points:
            messagebox.showinfo("", "Chưa chọn điểm nào!",
                                parent=self); return
        self.on_confirm(list(self.points))
        self.destroy()

