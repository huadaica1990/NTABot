"""
BuildEngine - Automatic building upgrade engine.
"""
import threading
import time
import os
from typing import Optional, Callable

from config import BuildConfig, HAS_CV2, ADB_LOCK

if HAS_CV2:
    import cv2
    import numpy as np


class BuildEngine:
    def __init__(self, adb: "ADB", log: Callable, cfg: BuildConfig, det=None):
        self.adb   = adb
        self.log   = log
        self.cfg   = cfg
        self.det   = det           # Detector (detect_popup_type)
        self._stop = threading.Event()
        self._pause = threading.Event()  # True = tam dung (dang trong thanh)
        self._thread = None
        self.on_refresh: Optional[Callable] = None
        self.in_city: Optional[threading.Event] = None

    def _has_popup(self) -> bool:
        """Chụp ảnh, check có popup đang mở không."""
        if not self.det:
            return True  # không có detector → giả sử có popup (an toàn)
        try:
            raw = self.adb.screenshot_bytes()
            if not raw:
                return True
            import numpy as np
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            ptype = self.det.detect_popup_type(img) if img is not None else ""
            return bool(ptype)
        except Exception:
            return True

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def _tap(self, x, y, delay=0.8):
        self.adb.tap(x, y)
        time.sleep(delay)

    def _enter_city(self):
        self.log("[Build] 🏰 Vào thành...")
        if self.in_city: self.in_city.set()
        self._tap(self.cfg.city_enter_x, self.cfg.city_enter_y, 1.5)

    def _exit_city(self):
        self.log("[Build] 🚪 Ra thành...")
        self._tap(self.cfg.city_exit_x, self.cfg.city_exit_y, 1.5)
        if self.in_city: self.in_city.clear()

    def _read_build_time(self) -> int:
        """Chup anh, OCR lay thoi gian xay (giay). Neu khong doc duoc tra 0."""
        try:
            import cv2, numpy as np, re, pytesseract
            raw = self.adb.screenshot_bytes()
            if not raw: return 0
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            # Vung chua timer: toan chieu ngang, 200px phia tren nut Upgrade
            x1 = 0
            x2 = w
            y1 = max(0, self.cfg.build_btn_y - 200)
            y2 = max(0, self.cfg.build_btn_y - 20)
            crop = img[y1:y2, x1:x2]
            # Scale up 2x + grayscale de OCR ro hon
            crop = cv2.resize(crop, (crop.shape[1]*2, crop.shape[0]*2),
                              interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(thresh, config="--psm 6")
            self.log(f"[Build] 🔍 OCR: '{text.strip()}'")
            m3 = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', text)
            if m3:
                return int(m3.group(1))*3600 + int(m3.group(2))*60 + int(m3.group(3))
            m2 = re.search(r'(\d{1,2}):(\d{2})', text)
            if m2:
                return int(m2.group(1))*60 + int(m2.group(2))
            return 0
        except Exception as e:
            self.log(f"[Build] ⚠️ OCR lỗi: {e}")
            return 0

    def _try_build(self, task: dict) -> int:
        """
        Vao nha, bam info, bam build, lay thoi gian.
        Tra ve so giay xay (>0 = thanh cong), 0 = khong xay duoc.
        """
        self._tap(task["tap_x"], task["tap_y"], self.cfg.delay_tap_house)
        self._tap(self.cfg.info_btn_x, self.cfg.info_btn_y, self.cfg.delay_tap_info)
        secs = self._read_build_time()
        self._tap(self.cfg.build_btn_x, self.cfg.build_btn_y, self.cfg.delay_tap_build)
        if secs > 0:
            h, r = divmod(secs, 3600); m, s2 = divmod(r, 60)
            lv = task.get('level', 1)
            self.log(f"[Build] 🔨 {task['name']} Lv{lv}: đang xây {h:02d}:{m:02d}:{s2:02d}")
        else:
            self.log(f"[Build] 🔨 {task['name']}: bấm xây (không đọc được TG)")
        return secs

    def build_single(self, task: dict):
        """Xây một nhà duy nhất: vào thành → xây → ra thành."""
        lv = task.get("level", 1)
        target = task.get("target_lv", 20)
        if lv >= target:
            self.log(f"[Build] 🏆 {task.get('name','')} đã đạt Lv{lv} (mục tiêu Lv{target}), bỏ qua.")
            if self.on_refresh: self.on_refresh()
            return

        self.log(f"[Build] 🔨 Xây nhanh: {task.get('name','')} (Lv{lv} → Lv{target})...")
        with ADB_LOCK:
            self.log("[Build] 🔓 Đã lấy ADB lock")
            if self.adb.is_in_city():
                self.log("[Build] ⚠️ Đã trong thành rồi")
            else:
                self._enter_city()

            secs = self._try_build(task)
            if secs > 0:
                if self.cfg.speed_pct > 0:
                    secs = int(secs * (1 - self.cfg.speed_pct / 100))
                task["build_done"] = time.time() + secs
                lv = task.get("level", 1)
                target = task.get("target_lv", 20)
                if lv < target:
                    task["level"] = lv + 1
                h, r = divmod(secs, 3600); m, s2 = divmod(r, 60)
                self.log(f"[Build] ✅ {task['name']} Lv{lv}→Lv{lv+1}: xây {h:02d}:{m:02d}:{s2:02d}")
            else:
                self.log(f"[Build] ⚠️ {task['name']}: không đọc được TG xây")

            # Back + ra thành
            if self._has_popup():
                self._tap(342, 1216, self.cfg.delay_back)
            self._exit_city()
            self.log("[Build] 🔒 Trả ADB lock")
        if self.on_refresh: self.on_refresh()

    def _run(self):
        self.log("[Build] ▶ Bắt đầu tự động xây...")
        while not self._stop.is_set():
            enabled = [t for t in self.cfg.tasks if t.get("enabled", True)]
            if not enabled:
                self.log("[Build] ✅ Không có nhà nào được bật.")
                break

            # === Lấy ADB lock để vào thành + xây + ra thành ===
            ADB_LOCK.acquire()
            self.log("[Build] 🔓 Đã lấy ADB lock")
            try:
                # --- Buoc 1: Vao thanh (chi vao neu dang o ngoai) ---
                if self.adb.is_in_city():
                    self.log("[Build] ⚠️ Đã trong thành rồi, bỏ qua bước vào thành")
                else:
                    self._enter_city()
                if self._stop.is_set(): break

                # --- Buoc 2-4: Duyet tung nha, xay toi da max_builders ---
                builders = 0
                total_wait = 0

                for idx, task in enumerate(enabled):
                    if self._stop.is_set(): break
                    if builders >= self.cfg.max_builders:
                        self.log(f"[Build] ⏸ Đủ {self.cfg.max_builders} hàng xây, dừng thêm.")
                        break

                    lv = task.get("level", 1)
                    target = task.get("target_lv", 20)
                    # Kiểm tra đã đạt mục tiêu TRƯỚC khi xây
                    if lv >= target:
                        self.log(f"[Build] 🏆 {task['name']} đã đạt Lv{lv} (mục tiêu Lv{target}), bỏ qua.")
                        if self.on_refresh: self.on_refresh()
                        continue

                    self.log(f"[Build] 🏠 Xây {task['name']} (Lv{lv} → Lv{target})...")
                    secs = self._try_build(task)

                    if secs > 0:
                        if self.cfg.speed_pct > 0:
                            secs = int(secs * (1 - self.cfg.speed_pct / 100))
                            self.log(f"[Build] ⚡ Sau buff {self.cfg.speed_pct}%: {secs//3600:02d}:{(secs%3600)//60:02d}:{secs%60:02d}")
                        task["build_done"] = time.time() + secs
                        total_wait += secs
                        h_s2, r_s2 = divmod(secs, 3600); m_s2, s_s2 = divmod(r_s2, 60)
                        self.log(f"[Build] ⏱ {task['name']}: {h_s2:02d}:{m_s2:02d}:{s_s2:02d}")
                        builders += 1
                        task["level"] = lv + 1
                    else:
                        self.log(f"[Build] ⚠️ {task['name']}: không đọc được TG, bỏ qua.")

                    if self.on_refresh: self.on_refresh()

                    has_next = any(
                        enabled[j].get("enabled", True)
                        for j in range(idx + 1, len(enabled))
                        if builders < self.cfg.max_builders
                    )
                    if self._has_popup():
                        self._tap(342, 1216, self.cfg.delay_back)
                    if not has_next:
                        break

                # --- Buoc 5: Ra thanh ---
                self._exit_city()
                if self.on_refresh: self.on_refresh()
            finally:
                ADB_LOCK.release()
                self.log("[Build] 🔒 Trả ADB lock")

            if self._stop.is_set(): break

            # --- Cho het tat ca nha xay xong ---
            if total_wait > 0:
                h, r = divmod(int(total_wait), 3600); m, s2 = divmod(r, 60)
                self.log(f"[Build] ⏳ Chờ {h:02d}:{m:02d}:{s2:02d} đến khi tất cả xây xong...")
                deadline = time.time() + total_wait
                while time.time() < deadline:
                    if self._stop.is_set(): return
                    remaining = int(deadline - time.time())
                    if remaining <= 10 and remaining > 0:
                        self.log(f"[Build] ⏳ Còn {remaining}s...")
                        time.sleep(1)
                    elif remaining % 300 == 0 and remaining > 0:
                        hr2, r2 = divmod(remaining, 3600); mn2, s3 = divmod(r2, 60)
                        self.log(f"[Build] ⏳ Còn {hr2:02d}:{mn2:02d}:{s3:02d}...")
                        time.sleep(1)
                    else:
                        time.sleep(1)
                self.log("[Build] 🔄 Hết TG xây → bắt đầu vòng tiếp theo!")
            else:
                self.log("[Build] ⚠️ Không có nhà nào được xây, thử lại sau 60s...")
                for _ in range(60):
                    if self._stop.is_set(): return
                    time.sleep(1)

        self.log("[Build] ⏹ Dừng tự động xây.")



