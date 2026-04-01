"""
TroopSelector - Automatic troop selection via OCR and checkbox detection.
"""
import time
import os
from typing import Optional, List, Callable
from datetime import datetime

from config import BotConfig, DEBUG_DIR, HAS_CV2
from adb_controller import ADB
from detector import Detector

if HAS_CV2:
    import cv2


class TroopSelector:
    def __init__(self, adb: ADB, det: Detector, cfg: BotConfig,
                 log: Callable):
        self.adb = adb
        self.det = det
        self.cfg = cfg
        self.log = log
        self.last_march_seconds: int = 0  # thoi gian hanh quan (giay) lan cuoi
        self.march_start_time:   float = 0.0  # thoi diem bat dau hanh quan

    def select(self) -> bool:
        self.log("[Troop] Chờ màn hình chọn quân...")
        if not self._wait_troop_screen():
            self.log("[Troop] Màn hình không hiện"); return False
        time.sleep(0.6)
        selected = self._do_select()
        self.log(f"[Troop] Đã chọn {selected} quân")
        if getattr(self, "_no_match_alert", False):
            # Tên quân không khớp → báo lỗi và trả về False để bot dừng
            names_str = ", ".join(self.cfg.troop_names)
            self.log(
                f"[Troop] 🛑 DỪNG: Không có quân nào khớp tên trong config "
                f"[{names_str}]. Kiểm tra lại tên quân!")
            return False
        if selected == 0:
            self.log("[Troop] ⚠️ Chọn 0 quân → scroll lên đầu và thử lại...")
            # Scroll len dau
            w = self.adb.cfg.screen_w or 720
            h = self.adb.cfg.screen_h or 1280
            for _ in range(4):
                self.adb.swipe(w//2, int(h*0.3), w//2, int(h*0.7), ms=400)
                time.sleep(0.4)
            time.sleep(0.8)
            selected = self._do_select()
            self.log(f"[Troop] Sau retry: Đã chọn {selected} quân")
        time.sleep(0.3)
        ok = self.det.find_ok_btn(self.adb.screenshot_cv2())
        if ok:
            self.log(f"[Troop] Tap OK {ok}")
            self.adb.tap(*ok)
            self.march_start_time = time.time()  # bat dau tinh TG hanh quan
            time.sleep(0.5)
            return True
        self.log("[Troop] Không tìm thấy OK"); return False

    def _do_select(self) -> int:
        """
        OCR doc ten checkbox + fuzzy match + fallback positional.
        Vi du: 'uto5' -> 'auto5', 'aid1' -> 'raid1'.
        """
        targets      = [n.lower().strip() for n in self.cfg.troop_names]
        chosen       = {}
        max_sel      = self.cfg.max_troops
        no_new       = 0
        ocr_on       = True
        march_logged = False   # Chi log TG hanh quan 1 lan sau click dau tien

        for scan in range(8):
            if len(chosen) >= max_sel:
                break
            screen = self.adb.screenshot_cv2()
            self._save_debug(f"troop_scan_{scan}", screen)
            cb_ys = self.det.detect_checkboxes(screen)
            cb_x  = self.det.checkbox_x(screen)
            self.log(f"[Troop] Scan {scan}: {len(cb_ys)} hang @ y={cb_ys}")
            clicked = 0
            ocr_fails = 0
            for i, y in enumerate(cb_ys):
                if len(chosen) >= max_sel:
                    break
                matched = None
                raw_name = ""
                if ocr_on:
                    raw_name = self.det.read_troop_name(screen, cb_x, y)
                    matched  = self.det.fuzzy_match_name(raw_name, targets)
                    self.log(f"[Troop]   [{i}] y={y} ocr='{raw_name}' -> {matched}")
                    if not raw_name:
                        ocr_fails += 1
                already = self.det.is_checked(screen, cb_x, y)
                if matched:
                    if matched in chosen:
                        self.log(f"[Troop]   skip '{matched}' (da chon)")
                        continue
                    if already:
                        self.log(f"[Troop]   '{matched}' da check san -> tinh")
                        chosen[matched] = True
                        continue
                    self.log(f"[Troop] -> Click '{matched}' y={y}")
                    self.adb.tap(cb_x, y)
                    time.sleep(0.55)
                    chosen[matched] = True
                    clicked += 1
                    # Doc TG hanh quan ngay sau click dau tien
                    if not march_logged:
                        sc_t = self.adb.screenshot_cv2()
                        mt = self.det.read_march_time(sc_t, near_y=y)
                        if mt:
                            self.log(f"[Troop] ⏱ TG hành quân: {mt}")
                            march_logged = True
                            # Parse sang giay
                            parts = mt.split(":")
                            try:
                                if len(parts) == 3:
                                    secs = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
                                else:
                                    secs = int(parts[0])*60 + int(parts[1])
                                self.last_march_seconds = secs
                            except Exception:
                                self.last_march_seconds = 0
                        else:
                            self.log("[Troop] TG hành quân: (không đọc được)")
                else:
                    slot = f"_p{i}s{scan}"
                    if already:
                        self.log(f"[Troop]   hang {i} da check (no name)")
                        chosen[slot] = True
                        continue
                    # Chi fallback khi OCR da tat hoan toan
                    # Neu OCR dang bat ma khong doc duoc ten → bo qua hang nay
                    if ocr_on:
                        self.log(
                            f"[Troop]   hang {i} OCR fail -> skip "
                            f"(khong click bua)")
                        continue
                    need = max_sel - len(chosen)
                    if i < need:
                        self.log(f"[Troop] -> Fallback click hang {i} y={y}")
                        self.adb.tap(cb_x, y)
                        time.sleep(0.55)
                        chosen[slot] = True
                        clicked += 1
            if cb_ys and ocr_fails >= len(cb_ys):
                no_new += 1
                if no_new >= 2 and ocr_on:
                    self.log("[Troop] OCR fail lien tuc -> fallback positional")
                    ocr_on = False
            self.log(f"[Troop] Scan {scan}: clicked={clicked} chosen={list(chosen.keys())}")
            if len(chosen) >= max_sel:
                break
            if clicked == 0:
                no_new += 1
                if no_new >= 3:
                    self.log("[Troop] Khong co gi moi -> dung")
                    break
            else:
                no_new = 0
            self.log("[Troop] Scroll xuong...")
            self._scroll(screen)
            time.sleep(1.3)

        chosen_keys = list(chosen.keys())
        self._last_chosen_keys = chosen_keys
        self.log(f"[Troop] Xong: {chosen_keys}")

        # Kiểm tra: nếu không có key nào là tên thật (tất cả đều là _pXsX fallback)
        # → không khớp tên quân nào trong config → cảnh báo
        named = [k for k in chosen_keys if not k.startswith("_p")]
        if chosen_keys and not named:
            self.log(
                f"[Troop] ⚠️ Không nhận ra tên quân nào! "
                f"Đã chọn theo vị trí: {chosen_keys}. "
                f"Tên quân trong config: {self.cfg.troop_names}"
            )
            self._no_match_alert = True  # cờ để bot engine xử lý dừng
        else:
            self._no_match_alert = False

        return len(chosen)

    def _scroll(self, screen):
        if screen is None: return
        h, w = screen.shape[:2]
        # Scroll cham hon: ms=700 (truoc la 450), khoang cach ngan hon
        self.adb.swipe(w//2, int(h*0.56), w//2, int(h*0.44), ms=700)

    def _wait_troop_screen(self) -> bool:
        dead = time.time() + self.cfg.troop_timeout
        while time.time() < dead:
            if self.det.is_troop_screen(self.adb.screenshot_cv2()):
                return True
            time.sleep(0.5)
        return False

    def _save_debug(self, tag, screen=None):
        if not self.cfg.debug_mode: return
        try:
            raw = self.adb.screenshot_bytes()
            if raw:
                ts = datetime.now().strftime("%H%M%S")
                p  = os.path.join(DEBUG_DIR, f"{tag}_{ts}.png")
                with open(p, "wb") as f: f.write(raw)
        except Exception:
            pass


# ──────────────────────────────────────────────
