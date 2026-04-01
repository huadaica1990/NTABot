"""
ScoutEngine - Map tile scanning and classification.
"""
import threading
import time
import numpy as np
from typing import Callable

from config import HAS_CV2
from tile_ocr import analyze_tile_image

if HAS_CV2:
    import cv2


TILE_LABELS = {
    "wasteland": "lv1",
    "lv.2": "lv2", "lv2": "lv2",
    "lv.3": "lv3", "lv3": "lv3",
    "lv.4": "lv4", "lv4": "lv4",
    "lv.5": "lv5", "lv5": "lv5",
}

class ScoutEngine:
    """Dò từng ô bản đồ xung quanh điểm mốc, lưu nhãn tile."""
    def __init__(self, bot, origin_x, origin_y,
                 up, down, left, right,
                 on_log, on_tile, on_done):
        self.bot      = bot
        self.ox       = origin_x
        self.oy       = origin_y
        self.up       = up
        self.down     = down
        self.left     = left
        self.right    = right
        self.on_log   = on_log    # fn(str)
        self.on_tile  = on_tile   # fn(gx, gy, label)
        self.on_done  = on_done   # fn()
        self._stop    = threading.Event()
        self._thread  = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _ocr_tile(self, gx, gy) -> str:
        """Navigate to (gx, gy), tap centre, OCR tile type. Returns label string."""
        import numpy as np
        try:
            import cv2
        except ImportError:
            self.on_log(f"[Scout] ❌ Thiếu cv2/pytesseract")
            return "?"

        bot = self.bot
        force_tap = 1 if self._first_tap else 2
        self._first_tap = False
        bot.navigate_to(gx, gy, force_tap=force_tap)
        if self._stop.is_set():
            return ""

        time.sleep(0.4)
        raw = bot.adb.screenshot_bytes()
        if not raw:
            self.on_log(f"[Scout] ⚠️ Không chụp được màn hình")
            return "?"
        arr = np.frombuffer(raw, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        info = analyze_tile_image(img)

        short_title = info.get('raw_title', '').replace("\n", "|").strip()[:120]
        short_btn = info.get('raw_btn', '').replace("\n", "|").strip()[:120]
        self.on_log(f"[Scout] OCR ({gx},{gy}) title: {repr(short_title)}")
        self.on_log(f"[Scout] OCR ({gx},{gy}) btn:   {repr(short_btn)}")
        self.on_log(f"[Scout] → nhãn: {info['label']} | tên: {info.get('name','')}")
        return info['label'], info.get('name', '')

    def _run(self):
        import itertools
        ox, oy = self.ox, self.oy
        self._first_tap = True   # lần đầu tap 1, các lần sau tap 2

        # Pre-check: đóng popup + thoát thành
        self.on_log("[Scout] 🔄 Pre-check: đóng popup (342,1216)")
        self.bot.adb.tap(342, 1216); time.sleep(0.8)
        if self.bot.adb.is_in_city():
            self.on_log("[Scout] 🏰 Đang trong thành → thoát ra...")
            self.bot.adb.tap(54, 1216); time.sleep(1.5)

        # Build list of coords to scout (spiral: origin + all offsets)
        coords = []
        for dy in range(-self.up, self.down + 1):
            for dx in range(-self.left, self.right + 1):
                coords.append((ox + dx, oy - dy))  # Y giảm = lên map

        total = len(coords)
        self.on_log(f"[Scout] Bắt đầu dò {total} ô, mốc ({ox},{oy})")

        for i, (gx, gy) in enumerate(coords):
            if self._stop.is_set():
                self.on_log("[Scout] ⏹ Dừng")
                break
            self.on_log(f"[Scout] {i+1}/{total} → ({gx},{gy})")
            result = self._ocr_tile(gx, gy)
            label, tile_name = result if isinstance(result, tuple) else (result, "")
            if self._stop.is_set(): break
            if label == "skip":
                time.sleep(0.3); continue
            self.on_log(f"[Scout] ({gx},{gy}) = {label}" + (f" | {tile_name}" if tile_name else ""))
            self.on_tile(gx, gy, label, tile_name)
            time.sleep(0.3)
        else:
            self.on_log("[Scout] ✅ Dò xong!")

        self.on_done()
