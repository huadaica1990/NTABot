"""
SpinEngine - Automatic lucky wheel spin engine.

Flow:
1. Exit city if in city
2. Tap spin popup button (414, 1216)
3. OCR check "Today's chance(s) left: x"
4. Check "Take a break mm:ss" timer → wait
5. Check "Come back tomorrow" → stop
6. Tap spin button (234, 1130)
7. Loop until done
"""
import threading
import time
import re
from typing import Optional, Callable

from config import HAS_CV2, ADB_LOCK

if HAS_CV2:
    import cv2
    import numpy as np


class SpinEngine:
    # Toa do co dinh
    POPUP_BTN   = (414, 1216)   # Nut mo popup quay
    SPIN_BTN    = (234, 1130)   # Nut quay
    CLOSE_BTN   = (360, 200)    # Dong popup (tap vung ngoai)

    def __init__(self, adb, log: Callable, det=None):
        self.adb  = adb
        self.log  = log
        self.det  = det          # Detector (detect_popup_type)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.on_refresh: Optional[Callable] = None
        self.in_city: Optional[threading.Event] = None

        # Config
        self.max_spins     = 10        # Tối đa lượt/ngày
        self.cooldown_sec  = 10 * 60   # 10 phút cooldown

        # Stats
        self.spins_done  = 0
        self.spins_left  = 0
        self.status      = "idle"   # idle | running | waiting | done | stopped

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # spins_done/spins_left đã được GUI đọc từ entry trước khi gọi start()
        self.status = "running"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "stopped"

    def _tap(self, x, y, delay=0.8):
        self.adb.tap(x, y)
        time.sleep(delay)

    def _screenshot_cv2(self):
        """Chup man hinh, tra ve cv2 image."""
        try:
            raw = self.adb.screenshot_bytes()
            if not raw:
                return None
            arr = np.frombuffer(raw, np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            return None

    def _has_popup(self) -> bool:
        """Check có popup đang mở không."""
        if not self.det:
            return True
        try:
            sc = self._screenshot_cv2()
            ptype = self.det.detect_popup_type(sc) if sc is not None else ""
            return bool(ptype)
        except Exception:
            return True

    def _ocr_region(self, img, y0_pct, y1_pct, x0_pct=0.0, x1_pct=1.0) -> str:
        """OCR mot vung cu the tren anh (% toa do)."""
        try:
            import pytesseract
            h, w = img.shape[:2]
            y0 = int(h * y0_pct); y1 = int(h * y1_pct)
            x0 = int(w * x0_pct); x1 = int(w * x1_pct)
            roi = img[y0:y1, x0:x1]
            # Scale up 3x + grayscale + threshold cho OCR tot hon
            roi = cv2.resize(roi, (roi.shape[1] * 3, roi.shape[0] * 3),
                             interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 0, 255,
                                      cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(thresh, config="--psm 6")
            return text.strip()
        except Exception as e:
            self.log(f"[Spin] ⚠️ OCR lỗi: {e}")
            return ""

    def _read_spin_status(self) -> dict:
        """
        Chup 1 lan, OCR 2 vung rieng biet:
        - Vung 1 (82-90% H): "Today's chance(s) left: x"
        - Vung 2 (90-98% H): "Take a break mm:ss" / "Skip" / nut quay
        Tra ve dict voi cac key: chances, break_secs, come_back, raw_top, raw_bot
        """
        img = self._screenshot_cv2()
        if img is None:
            return {"chances": -1, "break_secs": 0, "come_back": False,
                    "raw_top": "", "raw_bot": ""}

        # Vung "Today's chance(s) left: x"  — giua-duoi popup
        text_top = self._ocr_region(img, 0.80, 0.90, 0.05, 0.95)
        # Vung nut "Take a break mm:ss" / "Skip" / "Come back tomorrow"
        text_bot = self._ocr_region(img, 0.90, 0.99, 0.0, 0.75)

        chances = -1
        m = re.search(r"chance.*?left\s*[:\.]?\s*(\d+)", text_top, re.IGNORECASE)
        if m:
            chances = int(m.group(1))

        break_secs = 0
        mb = re.search(r"(\d{1,2}):(\d{2})", text_bot)
        if mb and re.search(r"[Tt]ake|break", text_bot, re.IGNORECASE):
            break_secs = int(mb.group(1)) * 60 + int(mb.group(2))

        come_back = bool(re.search(r"[Cc]ome\s*back|tomorrow", text_bot, re.IGNORECASE)
                         or re.search(r"[Cc]ome\s*back|tomorrow", text_top, re.IGNORECASE))

        return {
            "chances": chances,
            "break_secs": break_secs,
            "come_back": come_back,
            "raw_top": text_top,
            "raw_bot": text_bot,
        }

    def _run(self):
        self.log("[Spin] ▶ Bắt đầu quay thưởng...")

        # === Pre-check: Thoát thành + Mở popup + Detect lucky_wheel ===
        with ADB_LOCK:
            self.log("[Spin] 🔓 Đã lấy ADB lock")

            # 1. Thoát thành nếu đang trong thành
            if self.adb.is_in_city():
                self.log("[Spin] 🏰 Đang trong thành → thoát ra...")
                self.adb.tap(54, 1216)
                time.sleep(1.5)

            # 2. Mở popup Lucky Wheel
            self.log(f"[Spin] 📌 Bấm mở popup quay @ {self.POPUP_BTN}")
            self._tap(*self.POPUP_BTN, delay=2.0)

            # 3. Detect popup type — phải là lucky_wheel
            if self.det:
                sc = self._screenshot_cv2()
                ptype = self.det.detect_popup_type(sc) if sc is not None else ""
                self.log(f"[Spin] 🪟 detect_popup_type = '{ptype}'")
                if ptype != "lucky_wheel":
                    self.log("[Spin] ❌ Popup không phải Lucky Wheel → dừng!")
                    try:
                        import winsound
                        for _ in range(5):
                            winsound.Beep(880, 400)
                            time.sleep(0.1)
                    except Exception:
                        pass
                    self.status = "stopped"
                    if self.on_refresh: self.on_refresh()
                    return
                self.log("[Spin] ✅ Xác nhận popup Lucky Wheel → tiếp tục")
            else:
                self.log("[Spin] ⚠️ Không có detector → bỏ qua kiểm tra popup")

            self.log("[Spin] 🔒 Trả ADB lock")

        while not self._stop.is_set():
            # === Lấy ADB lock để OCR + quay + đóng popup ===
            ADB_LOCK.acquire()
            self.log("[Spin] 🔓 Đã lấy ADB lock")
            try:
                # Chup 1 lan, OCR 2 vung rieng
                info = self._read_spin_status()
                self.log(f"[Spin] 🔍 OCR top: '{info['raw_top']}'")
                self.log(f"[Spin] 🔍 OCR bot: '{info['raw_bot']}'")

                # Step 5: Het luot?
                if info["come_back"]:
                    self.log("[Spin] ✅ Hết lượt quay hôm nay (Come back tomorrow) → dừng")
                    self.spins_done = self.max_spins
                    self.spins_left = 0
                    # Đóng popup trước khi dừng
                    if self._has_popup():
                        self.log("[Spin] 🚪 Đóng popup Lucky Wheel (342, 1216)")
                        self._tap(342, 1216, delay=1.0)
                    self.status = "done"
                    if self.on_refresh: self.on_refresh()
                    break

                # Step 3: Doc so luot con lai → tinh so lan da quay
                if info["chances"] >= 0:
                    self.spins_left = info["chances"]
                    self.spins_done = self.max_spins - info["chances"]
                    self.log(f"[Spin] 🎰 Đã quay: {self.spins_done}/{self.max_spins} | Còn lại: {info['chances']}")
                    if self.on_refresh: self.on_refresh()
                    if info["chances"] <= 0:
                        self.log("[Spin] ✅ Hết lượt quay → dừng")
                        if self._has_popup():
                            self.log("[Spin] 🚪 Đóng popup Lucky Wheel (342, 1216)")
                            self._tap(342, 1216, delay=1.0)
                        self.status = "done"
                        if self.on_refresh: self.on_refresh()
                        break

                # Step 4: Take a break timer (backup khi vao giua chu ky)
                if info["break_secs"] > 0:
                    m, s = divmod(info["break_secs"], 60)
                    self.log(f"[Spin] ⏳ Take a break — chờ {m:02d}:{s:02d}...")
                    self.status = "waiting"
                    if self.on_refresh:
                        self.on_refresh()
                    ADB_LOCK.release()
                    self.log("[Spin] 🔒 Trả ADB lock (chờ break)")
                    for _ in range(info["break_secs"] + 5):
                        if self._stop.is_set():
                            return
                        time.sleep(1)
                    self.status = "running"
                    # Mo lai popup (cần ADB)
                    with ADB_LOCK:
                        self.log(f"[Spin] 📌 Mở lại popup quay @ {self.POPUP_BTN}")
                        self._tap(*self.POPUP_BTN, delay=2.0)
                    continue

                # Step 6: Quay!
                self.log(f"[Spin] 🎯 Quay! Tap @ {self.SPIN_BTN}")
                self.status = "running"
                self._tap(*self.SPIN_BTN, delay=3.0)
                self.spins_done += 1
                self.spins_left = max(0, self.spins_left - 1)
                self.log(f"[Spin] ✅ Đã quay lần {self.spins_done}/{self.max_spins}")

                if self.on_refresh:
                    self.on_refresh()

                # Cho animation xong, dong popup
                time.sleep(5.0)
                if self._has_popup():
                    self.log("[Spin] 🚪 Đóng popup quay (342, 1216)")
                    self._tap(342, 1216, delay=1.5)
            finally:
                try:
                    ADB_LOCK.release()
                    self.log("[Spin] 🔒 Trả ADB lock")
                except RuntimeError:
                    pass  # Đã release trong branch Take a break

            # === Cooldown 10 phút — KHÔNG cần ADB lock ===
            cooldown = self.cooldown_sec
            m, s = divmod(cooldown, 60)
            self.log(f"[Spin] ⏳ Chờ cooldown {m:02d}:{s:02d} trước lần quay tiếp...")
            self.status = "waiting"
            if self.on_refresh:
                self.on_refresh()
            for remaining in range(cooldown, 0, -1):
                if self._stop.is_set():
                    return
                if remaining % 60 == 0:
                    mr, sr = divmod(remaining, 60)
                    self.log(f"[Spin] ⏳ Còn {mr:02d}:{sr:02d}...")
                time.sleep(1)
            self.status = "running"

            # Mo lai popup (cần ADB)
            with ADB_LOCK:
                self.log(f"[Spin] 📌 Mở lại popup quay @ {self.POPUP_BTN}")
                self._tap(*self.POPUP_BTN, delay=2.0)

        if self.status != "done":
            self.status = "stopped"
        self.log(f"[Spin] ⏹ Kết thúc. Đã quay {self.spins_done} lần.")
        if self.on_refresh:
            self.on_refresh()
