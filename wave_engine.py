"""
WaveGroupEngine - Multi-group parallel attack engine.
"""
import threading
import time
import copy
import re
import json
import numpy as np
from typing import Optional, List, Callable

from config import (
    BotConfig, WaveGroup, ASSETS_DIR, HAS_CV2,
    _WAVE_ADB_LOCK,
)
from adb_controller import ADB
from detector import Detector
from troop_selector import TroopSelector
from tile_ocr import analyze_tile_image

if HAS_CV2:
    import cv2


class WaveGroupEngine:
    """
    Chay 1 nhom tan cong doc lap trong 1 thread rieng.
    Moi nhom co danh sach quan rieng + danh sach diem rieng.
    Khi het thoi gian hanh quan cua diem hien tai -> chuyen sang diem tiep theo.
    """
    def __init__(self, group: WaveGroup, cfg: BotConfig, adb: 'ADB', log: Callable):
        self.group   = group
        self.cfg     = cfg
        self.adb     = adb
        self.log     = lambda msg: log(f"[{group.name}] {msg}")
        self.det     = Detector(ASSETS_DIR, log)
        self._stop   = threading.Event()
        self._skip_cd = threading.Event()
        self._thread = None
        self.cur_idx = 0        # index trong group.points
        self.status  = "idle"   # idle | running | waiting | done | error
        self.on_refresh: Optional[Callable] = None
        self.on_captcha: Optional[Callable] = None  # callback khi phat hien CAPTCHA
        self.on_error:   Optional[Callable] = None  # callback(group_name, msg) khi error

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._skip_cd.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "idle"

    def skip_cooldown(self):
        """Bỏ qua cooldown hiện tại, chuyển sang điểm tiếp theo."""
        self._skip_cd.set()

    def _make_selector(self) -> TroopSelector:
        cfg_copy = copy.copy(self.cfg)
        cfg_copy.troop_names = self.group.troop_names
        return TroopSelector(self.adb, self.det, cfg_copy, self.log)

    def _check_captcha(self) -> bool:
        """Chup man hinh, kiem tra CAPTCHA. Neu co → beep + stop."""
        sc = self.adb.screenshot_cv2()
        if sc is not None and self.det.has_captcha_popup(sc):
            self.log("🚨 CAPTCHA phát hiện! Dừng nhóm!")
            try:
                import winsound
                for _ in range(10):
                    winsound.Beep(1200, 300)
                    time.sleep(0.15)
                    winsound.Beep(800, 300)
                    time.sleep(0.15)
            except Exception:
                pass
            self._stop.set()
            self.status = "error"
            if self.on_captcha:
                self.on_captcha()
            return True
        return False

    def _fire_error(self, msg: str):
        """Gọi on_error callback khi engine gặp lỗi."""
        if self.on_error:
            try:
                self.on_error(self.group.name, msg)
            except Exception:
                pass

    def _navigate_to(self, x: int, y: int) -> bool:
        """Navigate den toa do (x, y). Tra ve True neu thanh cong."""
        cfg = self.cfg
        w_s = cfg.screen_w; h_s = cfg.screen_h
        self.log(f"→ Navigate ({x},{y})")
        self.adb.tap(cfg.nav_btn_x, cfg.nav_btn_y); time.sleep(1.0)
        sc = self.adb.screenshot_cv2()
        w_s = sc.shape[1] if sc is not None else cfg.screen_w
        h_s = sc.shape[0] if sc is not None else cfg.screen_h
        self.adb.clear_and_type(cfg.x_field_x, cfg.x_field_y, str(x))
        self.adb._run("shell", "input", "keyevent", "KEYCODE_BACK"); time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y)); time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y)); time.sleep(0.35)
        self.adb._run("shell", "input", "keyevent", "KEYCODE_MOVE_END"); time.sleep(0.1)
        for _ in range(12): self.adb._run("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.15)
        for ch in str(y):
            kc = self.adb._DIGIT_KC.get(ch)
            if kc: self.adb._run("shell", "input", "keyevent", kc); time.sleep(0.06)
        time.sleep(0.2)
        self.adb._run("shell", "input", "tap", str(w_s//2), str(int(h_s*0.35))); time.sleep(0.6)
        self.adb._run("shell", "input", "tap", str(cfg.xem_x), str(cfg.xem_y)); time.sleep(0.4)
        self.log(f"Chờ {cfg.nav_wait}s bản đồ di chuyển...")
        time.sleep(cfg.nav_wait)
        return True

    def _do_heal(self):
        """Hồi máu: navigate đến heal point → tap March → chọn quân → chờ hành quân."""
        g = self.group
        if g.heal_x <= 0 and g.heal_y <= 0:
            self.log("[Heal] Chưa đặt toạ độ hồi máu → bỏ qua"); return

        self.log(f"[Heal] 🩸 Di chuyển đến ({g.heal_x},{g.heal_y})")
        # Thoat thanh neu can
        if self.adb.is_in_city():
            self.adb.tap(54, 1216); time.sleep(1.5)

        nav_ok = self._navigate_to(g.heal_x, g.heal_y)
        if not nav_ok:
            self.log("[Heal] Navigate thất bại → bỏ qua"); return

        # Tap March
        self.log("[Heal] Tap March (486, 746)")
        time.sleep(3.0)
        self.adb.tap(486, 746)
        time.sleep(0.5)

        # Chon quan
        sel = self._make_selector()
        ok = sel.select()
        self.log(f"[Heal] Chọn quân: {'OK' if ok else 'FAIL'}")
        if not ok: return

        # Cho hanh quan
        march_secs = sel.last_march_seconds
        march_start = sel.march_start_time
        if march_secs > 0 and march_start > 0:
            march_deadline = march_start + march_secs
            remaining = march_deadline - time.time()
            if remaining > 0:
                m, s = int(remaining) // 60, int(remaining) % 60
                self.log(f"[Heal] ⏳ Chờ quân đi hồi máu: {m}p{s:02d}s...")
                while time.time() < march_deadline:
                    if self._stop.is_set(): return
                    time.sleep(1.0)
            self.log("[Heal] ✅ Quân đã đến điểm hồi máu → tiếp tục tấn công")
        else:
            self.log("[Heal] ✅ Hồi máu xong → tiếp tục tấn công")

    def _setup_heal_point(self, pt: dict):
        """
        Đặt điểm hồi máu: navigate đến toạ độ → tap giữa → tap Xây → confirm.
        Cập nhật group.heal_x, heal_y.
        """
        g = self.group
        cfg = self.cfg
        x, y = pt.get("x", 0), pt.get("y", 0)
        self.log(f"[HealStep] 🏗 Đặt điểm hồi máu tại ({x},{y})...")

        # Thoat thanh neu can
        if self.adb.is_in_city():
            self.adb.tap(54, 1216); time.sleep(1.5)

        # Navigate
        self._navigate_to(x, y)

        # Tap giua man hinh
        sc = self.adb.screenshot_cv2()
        w_s = sc.shape[1] if sc is not None else cfg.screen_w
        h_s = sc.shape[0] if sc is not None else cfg.screen_h
        cx, cy = w_s // 2, h_s // 2
        self.log(f"[HealStep] Tap giữa ({cx},{cy})")
        self.adb.tap(cx, cy)
        time.sleep(1.0)

        # Tap nut Xay (488, 746)
        self.log("[HealStep] Tap nút Xây (488, 746)")
        self.adb.tap(488, 746)
        time.sleep(1.0)

        # Tap xac nhan (558, 490)
        self.log("[HealStep] Tap xác nhận (558, 490)")
        self.adb.tap(558, 490)
        time.sleep(0.8)

        # Cap nhat heal_x, heal_y
        g.heal_x = x
        g.heal_y = y
        self.log(f"[HealStep] ✅ Heal point → ({x},{y})")
        if self.on_refresh: self.on_refresh()

    def _navigate_and_attack(self, pt: dict) -> int:
        """Returns march_seconds or 0 on failure."""
        import re as _re

        # Exit city if needed
        if self.adb.is_in_city():
            self.adb.tap(54, 1216); time.sleep(1.5)

        # --- Navigate ---
        cfg = self.cfg
        w_s = cfg.screen_w; h_s = cfg.screen_h
        self.log(f"→ Navigate ({pt['x']},{pt['y']})")
        # Open nav panel
        self.adb.tap(cfg.nav_btn_x, cfg.nav_btn_y); time.sleep(1.0)
        sc = self.adb.screenshot_cv2()
        w_s = sc.shape[1] if sc is not None else cfg.screen_w
        h_s = sc.shape[0] if sc is not None else cfg.screen_h
        # Enter X
        self.adb.clear_and_type(cfg.x_field_x, cfg.x_field_y, str(pt['x']))
        self.adb._run("shell", "input", "keyevent", "KEYCODE_BACK"); time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y)); time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y)); time.sleep(0.35)
        self.adb._run("shell", "input", "keyevent", "KEYCODE_MOVE_END"); time.sleep(0.1)
        for _ in range(12): self.adb._run("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.15)
        for ch in str(pt['y']):
            kc = self.adb._DIGIT_KC.get(ch)
            if kc: self.adb._run("shell", "input", "keyevent", kc); time.sleep(0.06)
        time.sleep(0.2)
        # Dismiss keyboard, tap Xem
        self.adb._run("shell", "input", "tap", str(w_s//2), str(int(h_s*0.35))); time.sleep(0.6)
        self.adb._run("shell", "input", "tap", str(cfg.xem_x), str(cfg.xem_y)); time.sleep(0.4)
        self.log(f"Chờ {cfg.nav_wait}s bản đồ di chuyển...")
        time.sleep(cfg.nav_wait)

        # OCR before center tap
        cx = w_s // 2; cy = h_s // 2
        _tap_twice = False
        try:
            import cv2 as _cv2, numpy as _np, pytesseract as _tess, re as _re2
            _raw = self.adb.screenshot_bytes()
            if _raw:
                _arr = _np.frombuffer(_raw, _np.uint8)
                _img = _cv2.imdecode(_arr, _cv2.IMREAD_COLOR)
                _h2, _w2 = _img.shape[:2]
                _img2 = _cv2.resize(_img, (_w2*2, _h2*2), interpolation=_cv2.INTER_CUBIC)
                _gray = _cv2.cvtColor(_img2, _cv2.COLOR_BGR2GRAY)
                _, _th = _cv2.threshold(_gray, 0, 255, _cv2.THRESH_BINARY + _cv2.THRESH_OTSU)
                _text = _tess.image_to_string(_th, config="--psm 6")
                _keywords = ["Wasteland", "Lv.2", "Lv.3", "Lv.4", "Lv.5"]
                _found = [k for k in _keywords if k in _text]
                self.log(f"OCR raw: {repr(_text[:200])}")
                if _found:
                    _tap_twice = True
                    self.log(f"OCR thấy {_found} → cập nhật nhãn")
                else:
                    self.log("OCR không thấy Wasteland/Lv")
        except Exception: pass

        self.log(f"Tap giữa ({cx},{cy})")
        if _tap_twice:
            time.sleep(0.6); self.adb.tap(cx, cy)

        # OCR sau tap - detect label chinh xac hon (giong tab Tan cong)
        _detected_label = None
        try:
            time.sleep(0.5)
            _rawb = self.adb.screenshot_bytes()
            if _rawb and HAS_CV2:
                _arrb = np.frombuffer(_rawb, np.uint8)
                _imgb = cv2.imdecode(_arrb, cv2.IMREAD_COLOR)
                _info = analyze_tile_image(_imgb)
                _textb = (_info.get('raw_title', '') + '\n' + _info.get('raw_btn', '')).strip()
                self.log(f"OCR sau tap: {repr(_textb[:200])}")
                if _info.get('label', '').startswith('lv'):
                    _detected_label = _info['label']
                    self.log(f"🏷 Phát hiện nhãn: {_detected_label}")
        except Exception as _eb:
            self.log(f"OCR sau tap lỗi: {_eb}")

        # Cap nhat nhan neu OCR phat hien duoc
        if _detected_label:
            old_lbl = pt.get("label", "")
            pt["label"] = _detected_label
            self.log(f"🏷 Cập nhật nhãn: '{old_lbl}' → '{_detected_label}'")

        # --- Wait popup ---
        time.sleep(0.5)
        # Simple: wait up to 8s for popup
        deadline = time.time() + 8.0
        cap = None
        while time.time() < deadline:
            sc2 = self.adb.screenshot_cv2()
            cap_coord = self.det.find_capture_btn(sc2)
            if cap_coord: cap = cap_coord; break
            time.sleep(0.4)
        if not cap:
            self.log("Popup không hiện"); return 0

        # --- Click Chiếm ---
        time.sleep(1.0)
        self.adb.tap(486, 704); time.sleep(1.0)

        # Kiem tra popup army_selection da hien chua (toi da 3 lan)
        MAX_RETRIES = 3
        for attempt in range(1, MAX_RETRIES + 1):
            sc3 = self.adb.screenshot_cv2()
            ptype = self.det.detect_popup_type(sc3) if sc3 is not None else ""
            self.log(f"detect_popup_type = '{ptype}' (lần {attempt}/{MAX_RETRIES})")
            if ptype == "army_selection":
                break

            self.log(f"⏳ Chưa thấy army_selection → chờ tối đa 30s... (lần {attempt}/{MAX_RETRIES})")
            deadline_popup = time.time() + 30
            found = False
            while time.time() < deadline_popup and not self._stop.is_set():
                time.sleep(1.0)
                sc3 = self.adb.screenshot_cv2()
                ptype = self.det.detect_popup_type(sc3) if sc3 is not None else ""
                if ptype == "army_selection":
                    self.log("✅ Đã thấy army_selection")
                    found = True
                    break
            if found:
                break

            if attempt < MAX_RETRIES:
                self.log(f"⚠️ Thử Click Chiếm lại (lần {attempt + 1}/{MAX_RETRIES})...")
                self.adb.tap(486, 704); time.sleep(1.5)
            else:
                self.log(f"❌ Đã thử {MAX_RETRIES} lần, popup = '{ptype}' → bỏ qua điểm này")
                return 0

        # --- Select troops ---
        sel = self._make_selector()
        ok = sel.select()
        if not ok:
            self.log("Chọn quân thất bại"); return 0
        # Kiểm tra tên quân không khớp config → dừng nhóm
        if getattr(sel, "_no_match_alert", False):
            names_str = ", ".join(self.group.troop_names)
            self.log(
                f"🛑 DỪNG nhóm '{self.group.name}': Không có quân nào khớp tên "
                f"[{names_str}]. Kiểm tra lại tên quân!")
            self.status = "error"
            self._fire_error(f"Không có quân nào khớp tên [{names_str}]")
            return 0

        march_s = sel.last_march_seconds
        self.log(f"✅ Tấn công thành công, hanh quan {march_s}s")
        return march_s

    def _run(self):
        self.status = "running"
        pts = self.group.points
        if not pts:
            self.log("Không có điểm nào"); self.status = "done"; return

        # Validate: must have at least 1 troop name
        required = [n.strip() for n in self.group.troop_names if n.strip()]
        if not required:
            self.log("❌ Nhóm chưa có tên quân → dừng"); self.status = "error"
            self._fire_error("Nhóm chưa có tên quân"); return
        main_troop = required[0].lower()

        # Kiểm tra quân Standby (giống tab Tấn công)
        if required:
            with _WAVE_ADB_LOCK:
                self.adb.tap(666, 1216); time.sleep(1.5)
                armies = self.adb.read_army_status(required_names=required)
                # Chỉ đóng popup nếu có popup đang mở
                sc = self.adb.screenshot_cv2()
                ptype = self.det.detect_popup_type(sc) if sc is not None else ""
                if ptype:
                    self.adb.tap(342, 1216); time.sleep(0.8)
            standby = [a for a in armies if a["status"] == "Standby"]
            if standby:
                self.log(f"✅ Quân Standby: {', '.join(a['name'] for a in standby)}")
            else:
                self.log("⚠️ Không có quân nào Standby")
            all_names_lower = [a["name"].lower() for a in armies]
            main_found = any(main_troop in n for n in all_names_lower)
            if not main_found:
                self.log(f"🚨 Không tìm thấy '{required[0]}' → dừng!")
                try:
                    import winsound
                    for _ in range(5): winsound.Beep(880, 500); time.sleep(0.15)
                except Exception: pass
                self.status = "error"
                self._fire_error(f"Không tìm thấy quân '{required[0]}'"); return

        idx = 0
        attack_count = 0
        while not self._stop.is_set():
            if idx >= len(pts):
                # Kiểm tra còn điểm waiting nào (có thể user thêm mới)
                new_waiting = [(i, p) for i, p in enumerate(pts) if p.get("status") != "done"]
                if new_waiting:
                    idx = new_waiting[0][0]
                    self.log(f"🔄 Tìm thấy {len(new_waiting)} điểm chưa done → tiếp tục từ điểm {idx+1}")
                    continue

                # Hết điểm thật sự → chờ xem có điểm mới không
                if self.status != "done":
                    self.log("✅ Hết điểm → chờ thêm điểm mới...")
                    self.status = "done"
                    if self.on_refresh: self.on_refresh()

                    # Heal on done (chỉ chạy 1 lần khi vừa done)
                    g = self.group
                    if g.heal_on_done and g.heal_x > 0 and g.heal_y > 0:
                        self.log(f"[Heal] 🩸 Hết điểm cuối → hồi máu tại ({g.heal_x},{g.heal_y})")
                        with _WAVE_ADB_LOCK:
                            self._do_heal()

                # Poll mỗi 2s xem có điểm mới
                time.sleep(2)
                continue

            pt = pts[idx]
            # Bỏ qua điểm đã done
            if pt.get("status") == "done":
                self.log(f"⏭ Bỏ qua điểm {idx+1} (done): ({pt['x']},{pt['y']}) {pt.get('label','')}")
                idx += 1; continue
            # Check CAPTCHA truoc moi diem
            if self._check_captcha(): break
            self.cur_idx = idx
            self.log(f"== Điểm {idx+1}/{len(pts)}: ({pt['x']},{pt['y']}) {pt.get('label','')}")
            self.status = "running"
            if self.on_refresh: self.on_refresh()

            # Chờ lấy lock ADB (hàng chờ)
            if not _WAVE_ADB_LOCK.acquire(blocking=False):
                self.log("⏳ Đang chờ hàng ADB (nhóm khác đang dùng)...")
                self.status = "waiting"
                if self.on_refresh: self.on_refresh()
                _WAVE_ADB_LOCK.acquire()   # block cho đến khi lock rảnh

            try:
                if self._stop.is_set():
                    _WAVE_ADB_LOCK.release(); break
                self.log("🔓 Đã lấy lock ADB → bắt đầu tấn công")
                march_s = self._navigate_and_attack(pt)
            finally:
                _WAVE_ADB_LOCK.release()
                self.log("🔒 Trả lock ADB")

            if march_s > 0:
                attack_count += 1
                pt["status"] = "process"
                self.status = "waiting"
                if self.on_refresh: self.on_refresh()
                # Check CAPTCHA ngay sau khi tấn công, trước khi chờ hành quân
                if self._check_captcha(): break
                self.log(f"⏳ Hành quân {march_s}s... (lần tấn công thứ {attack_count})")
                deadline = time.time() + march_s
                self._skip_cd.clear()
                while time.time() < deadline and not self._stop.is_set() and not self._skip_cd.is_set():
                    time.sleep(1)
                if self._stop.is_set(): break

                is_last = (idx >= len(pts) - 1)

                # Cooldown theo nhãn (giống tab tấn công) - bỏ qua điểm cuối
                if not is_last:
                    import re as _re
                    lbl = pt.get("label", "")
                    grp = self.group
                    def _has_lv(n): return bool(_re.search(rf'lv\.?{n}\b', lbl, _re.IGNORECASE))
                    if _has_lv(4):
                        cd = grp.lv4_cooldown_sec if grp.lv4_cooldown_sec > 0 else self.cfg.lv4_cooldown_sec
                    elif _has_lv(3):
                        cd = grp.lv3_cooldown_sec if grp.lv3_cooldown_sec > 0 else self.cfg.lv3_cooldown_sec
                    elif _has_lv(2):
                        cd = grp.lv2_cooldown_sec if grp.lv2_cooldown_sec > 0 else self.cfg.lv2_cooldown_sec
                    else:
                        cd = grp.cooldown_sec if grp.cooldown_sec > 0 else self.cfg.cooldown_sec
                    if cd > 0:
                        tag = "lv4" if _has_lv(4) else "lv3" if _has_lv(3) else "lv2" if _has_lv(2) else None
                        if tag:
                            self.log(f"Nhãn '{lbl}' có {tag} → Cooldown {cd}s")
                        self.log(f"⏳ Cooldown {cd}s...")
                        cd_deadline = time.time() + cd
                        logged_cd = set()
                        self._skip_cd.clear()
                        while time.time() < cd_deadline and not self._stop.is_set() and not self._skip_cd.is_set():
                            left = int(cd_deadline - time.time())
                            mark = left - (left % 30)
                            if mark not in logged_cd:
                                logged_cd.add(mark)
                                self.log(f"Cooldown còn {left}s...")
                            time.sleep(1)
                        if self._skip_cd.is_set():
                            self._skip_cd.clear()
                            self.log("⏭️ Skip cooldown → điểm tiếp theo")
                        if self._stop.is_set(): break

                pt["status"] = "done"
                idx += 1
                if self.on_refresh: self.on_refresh()

                # Heal step: đặt điểm hồi máu mỗi N step
                g = self.group
                if not is_last and g.heal_step > 0 and attack_count % g.heal_step == 0:
                    self.log(f"[HealStep] 🏗 Đủ {g.heal_step} step → đặt điểm hồi máu")
                    with _WAVE_ADB_LOCK:
                        self._setup_heal_point(pt)

                # Heal: auto heal sau N lần tấn công
                if not is_last and g.heal_every > 0 and attack_count % g.heal_every == 0:
                    self.log(f"[Heal] 🩸 Đủ {g.heal_every} lần → hồi máu")
                    with _WAVE_ADB_LOCK:
                        self._do_heal()

                if is_last:
                    self.log("✅ Điểm cuối cùng hoàn thành")
            else:
                self.log(f"Điểm {idx+1} thất bại → thử lại sau 30s")
                self.status = "waiting"
                for _ in range(30):
                    if self._stop.is_set(): break
                    time.sleep(1)

        if self.status != "done":
            self.status = "idle"
        if self.on_refresh: self.on_refresh()

