"""
BotEngine - Main attack automation engine.
"""
import threading
import time
import os
import copy
import re
import json
import numpy as np
from typing import Optional, List, Callable

from config import (
    BotConfig, State, AttackPoint, ASSETS_DIR, DEBUG_DIR,
    HAS_CV2, ADB_LOCK,
)
from adb_controller import ADB
from detector import Detector
from troop_selector import TroopSelector
from ollama_vision import OllamaVision
from brain import TroopBrain
from tile_ocr import analyze_tile_image

if HAS_CV2:
    import cv2


class BotEngine:
    def __init__(self, cfg: BotConfig, log: Callable):
        self.cfg     = cfg
        self.log     = log
        self.adb     = ADB(cfg, log)
        self.det     = Detector(ASSETS_DIR, log)
        self.sel     = TroopSelector(self.adb, self.det, cfg, log)
        self.state   = State.IDLE
        self._stop   = threading.Event()
        self._skip_cd = threading.Event()  # skip cooldown -> diem tiep theo
        self._heal_requested = threading.Event()  # heal ngay sau khi xong diem hien tai
        self._thread = None
        self.cur_pt: Optional[AttackPoint] = None
        self.on_state:   Optional[Callable] = None
        self.on_refresh: Optional[Callable] = None
        self.on_heal_update: Optional[Callable] = None  # callback khi heal_x/y thay doi
        self.get_pending_points: Optional[Callable] = None  # callback lấy điểm waiting mới
        self._ollama = OllamaVision(cfg.ollama_url, cfg.ollama_model)
        self.brain   = TroopBrain(cfg, log)
        self._last_army_health = {}
        self.in_city: Optional[threading.Event] = None
        self.on_captcha: Optional[Callable] = None  # callback khi phat hien CAPTCHA
        self.on_error:   Optional[Callable] = None  # callback(msg) khi error dung bot

    def _check_captcha(self) -> bool:
        """Chup man hinh, kiem tra CAPTCHA. Neu co → beep + stop + callback."""
        sc = self.adb.screenshot_cv2()
        if sc is not None and self.det.has_captcha_popup(sc):
            self.log("[Bot] 🚨 CAPTCHA phát hiện! Dừng mọi hoạt động!")
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
            if self.on_captcha:
                self.on_captcha()
            return True
        return False

    def skip_cooldown(self):
        """Tat cooldown hien tai, di chuyen den diem tiep theo."""
        self._skip_cd.set()

    def _set(self, s: State, reason=""):
        self.state = s
        msg = f"[Bot] ── {s.name}" + (f" ({reason})" if reason else "")
        self.log(msg)
        if self.on_state: self.on_state(s)

    def start(self, points: List[AttackPoint]):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._skip_cd.clear()
        self._thread = threading.Thread(
            target=self._run, args=(points,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._set(State.IDLE, "user stop")

    def _check_required_armies(self) -> bool:
        """
        1. Kiem tra co it nhat 1 quan trong troop_names co trang thai Standby.
        2. Kiem tra bat buoc phai co quan dau tien trong troop_names (bat ke trang thai).
           Neu khong co -> beep + dung bot.
        Chi log ket qua, khong dung bot o buoc 1.
        """
        required = [n.strip() for n in self.cfg.troop_names if n.strip()]
        if not required: return True
        self.adb.tap(666, 1216); import time as _t; _t.sleep(1.5)
        armies = self.adb.read_army_status(required_names=required)
        # Chỉ đóng popup nếu có popup đang mở
        sc = self.adb.screenshot_cv2()
        ptype = self.det.detect_popup_type(sc) if sc is not None else ""
        if ptype:
            self.adb.tap(342, 1216); _t.sleep(0.8)

        # Buoc 1: check Standby (chi log)
        standby = [a for a in armies if a["status"] == "Standby"]
        if standby:
            self.log(f"[Bot] ✅ Quân Standby: {', '.join(a['name'] for a in standby)}")
        else:
            self.log(f"[Bot] ⚠️ Không có quân nào Standby")

        # Buoc 2: bat buoc phai co quan dau tien trong danh sach
        main_troop = required[0].lower()
        all_names_lower = [a["name"].lower() for a in armies]
        if not any(main_troop in n for n in all_names_lower):
            self.log(f"[Bot] 🚨 Không tìm thấy '{required[0]}' → DỪNG!")
            try:
                import winsound
                for _ in range(5): winsound.Beep(880, 500); _t.sleep(0.15)
            except Exception: pass
            self._stop.set()
            if self.on_error:
                try: self.on_error(f"Không tìm thấy quân '{required[0]}'")
                except: pass
            return False
        return True

    def _scan_army_health(self, required_names: list = None, next_label: str = "") -> dict:
        """Mo All Armies, doc mau + trang thai, chay rule engine."""
        names = required_names or self.cfg.troop_names
        if not self.cfg.army_health_enabled:
            return {'troops': [], 'force_heal': False, 'low_risk_only': False}
        try:
            self.adb.tap(666, 1216)
            time.sleep(1.5)
            hp_rows = self.adb.read_army_health(required_names=names)
            # Chỉ đóng popup nếu có popup đang mở
            sc = self.adb.screenshot_cv2()
            ptype = self.det.detect_popup_type(sc) if sc is not None else ""
            if ptype:
                self.adb.tap(342, 1216)
                time.sleep(0.8)
            summary = self.brain.summarize(hp_rows, next_label=next_label)
            for a in summary.get('troops', []):
                self._last_army_health[a['name']] = a
                trend = a['hp_trend'] * 100.0
                self.log(
                    f"[Brain] {a['name']:10s} | HP {a['avg_hp']*100:5.1f}% | min {a['min_hp']*100:5.1f}% | "
                    f"crit {a['critical_units']}/{a['unit_count']} | trend {trend:+.1f}% | "
                    f"risk {a['risk_score']} | {a['action']}")
            return summary
        except Exception as e:
            self.log(f"[Brain] ⚠️ Doc mau that bai: {e}")
            try:
                sc = self.adb.screenshot_cv2()
                ptype = self.det.detect_popup_type(sc) if sc is not None else ""
                if ptype:
                    self.adb.tap(342, 1216)
            except Exception:
                pass
            return {'troops': [], 'force_heal': False, 'low_risk_only': False}

    def _run(self, points: List[AttackPoint]):
        self.log(f"[Bot] Bat dau {len(points)} diem (idx: {[p.idx for p in points]})")
        # Neu dang trong thanh thi thoat ra truoc
        with ADB_LOCK:
            if self.adb.is_in_city():
                self.log("[Bot] 🏰 Đang trong thành → bấm thoát ra ngoài...")
                self.adb.tap(54, 1216)
                import time as _t; _t.sleep(1.5)
            # Kiem tra quan truoc khi chay
            if not self._check_required_armies():
                self.log("[Bot] ❌ Kiểm tra quân thất bại → dừng"); return
        attack_count = 0
        for i, pt in enumerate(points):
            if self._stop.is_set(): break
            # Check CAPTCHA truoc moi diem
            if self._check_captcha(): break
            self.cur_pt = pt
            self.log(f"\n[Bot] == Diem {pt.idx}/{len(points)}: "
                     f"game({pt.game_x},{pt.game_y}) {pt.label}")
            pt.status = "going"
            if self.on_refresh: self.on_refresh()
            # Lấy lock ADB để tấn công
            ADB_LOCK.acquire()
            self.log("[Bot] 🔓 Đã lấy ADB lock")
            try:
                ok = self._attack(pt)
            finally:
                ADB_LOCK.release()
                self.log("[Bot] 🔒 Trả ADB lock")
            if ok:
                attack_count += 1
                pt.status = "process"
                if self.on_refresh: self.on_refresh()
                self.log(f"[Bot] Lan tan cong thu {attack_count}")
            else:
                pt.status = "waiting"
                if self.on_refresh: self.on_refresh()
                self.log(f"[Bot] 🛑 Diem {pt.idx} that bai -> DỪNG BOT!")
                try:
                    import winsound
                    for _ in range(5):
                        winsound.Beep(800, 400)
                        time.sleep(0.1)
                except Exception:
                    pass
                self._stop.set()
                if self.on_error:
                    try: self.on_error(f"Điểm {pt.idx} thất bại")
                    except: pass
                break

            if i < len(points) - 1 and not self._stop.is_set():
                import re as _re
                lbl = pt.label
                def _has_lv(n): return bool(_re.search(rf'lv\.?{n}\b', lbl, _re.IGNORECASE))
                if _has_lv(4):
                    cd = self.cfg.lv4_cooldown_sec
                elif _has_lv(3):
                    cd = self.cfg.lv3_cooldown_sec
                elif _has_lv(2):
                    cd = self.cfg.lv2_cooldown_sec
                else:
                    cd = self.cfg.cooldown_sec
                tag = "lv4" if _has_lv(4) else "lv3" if _has_lv(3) else "lv2" if _has_lv(2) else None
                if tag:
                    self.log(f"[Bot] Nhãn '{pt.label}' có {tag} → Cooldown {cd}s")
                self._cooldown(cd)
                pt.status = "done"
                if self.on_refresh: self.on_refresh()
                # Heal step: dat diem hoi mau moi sau moi X lan tan cong
                if ok and self.cfg.heal_step > 0 and attack_count % self.cfg.heal_step == 0:
                    self.log(f"[Bot] 🏗 Đủ {self.cfg.heal_step} step → đặt điểm hồi máu")
                    with ADB_LOCK:
                        self._setup_heal_point(pt)
                # Hoi mau sau khi cooldown xong: manual request hoac auto heal_every
                if self._heal_requested.is_set():
                    self.log("[Bot] 🩸 Heal thủ công theo yêu cầu → HOI MAU")
                    self._heal_requested.clear()
                    with ADB_LOCK:
                        self._do_heal()
                elif ok and self.cfg.heal_every > 0 and attack_count % self.cfg.heal_every == 0:
                    self.log(f"[Bot] Đủ {self.cfg.heal_every} lần → HOI MAU")
                    with ADB_LOCK:
                        self._do_heal()
        # --- Re-check: kiểm tra điểm waiting mới được thêm ---
        while not self._stop.is_set() and self.get_pending_points:
            # Đánh dấu điểm cuối cùng done
            if self.cur_pt and self.cur_pt.status == "process":
                self.cur_pt.status = "done"
                if self.on_refresh: self.on_refresh()
            new_pts = self.get_pending_points()
            # Lọc bỏ những điểm đã chạy rồi (chỉ lấy waiting thực sự)
            already_done = {id(p) for p in points}
            new_waiting = [p for p in new_pts if id(p) not in already_done and p.status == "waiting"]
            if not new_waiting:
                break
            self.log(f"[Bot] 🔄 Phát hiện {len(new_waiting)} điểm mới → tiếp tục!")
            points = new_waiting
            for i, pt in enumerate(points):
                if self._stop.is_set(): break
                # Check CAPTCHA truoc moi diem
                if self._check_captcha(): break
                self.cur_pt = pt
                self.log(f"\n[Bot] == Diem {pt.idx}/{len(points)}: "
                         f"game({pt.game_x},{pt.game_y}) {pt.label}")
                pt.status = "going"
                if self.on_refresh: self.on_refresh()
                ADB_LOCK.acquire()
                self.log("[Bot] 🔓 Đã lấy ADB lock")
                try:
                    ok = self._attack(pt)
                finally:
                    ADB_LOCK.release()
                    self.log("[Bot] 🔒 Trả ADB lock")
                if ok:
                    attack_count += 1
                    pt.status = "process"
                    if self.on_refresh: self.on_refresh()
                    self.log(f"[Bot] Lan tan cong thu {attack_count}")
                else:
                    pt.status = "waiting"
                    if self.on_refresh: self.on_refresh()
                    self.log(f"[Bot] 🛑 Diem {pt.idx} that bai -> DỪNG BOT!")
                    try:
                        import winsound
                        for _ in range(5):
                            winsound.Beep(800, 400)
                            time.sleep(0.1)
                    except Exception:
                        pass
                    self._stop.set()
                    if self.on_error:
                        try: self.on_error(f"Điểm {pt.idx} thất bại")
                        except: pass
                    break

                if i < len(points) - 1 and not self._stop.is_set():
                    import re as _re
                    lbl = pt.label
                    def _has_lv(n): return bool(_re.search(rf'lv\.?{n}\b', lbl, _re.IGNORECASE))
                    if _has_lv(4):
                        cd = self.cfg.lv4_cooldown_sec
                    elif _has_lv(3):
                        cd = self.cfg.lv3_cooldown_sec
                    elif _has_lv(2):
                        cd = self.cfg.lv2_cooldown_sec
                    else:
                        cd = self.cfg.cooldown_sec
                    tag = "lv4" if _has_lv(4) else "lv3" if _has_lv(3) else "lv2" if _has_lv(2) else None
                    if tag:
                        self.log(f"[Bot] Nhãn '{pt.label}' có {tag} → Cooldown {cd}s")
                    self._cooldown(cd)
                    pt.status = "done"
                    if self.on_refresh: self.on_refresh()
                    # Heal step: dat diem hoi mau moi sau moi X lan tan cong
                    if ok and self.cfg.heal_step > 0 and attack_count % self.cfg.heal_step == 0:
                        self.log(f"[Bot] 🏗 Đủ {self.cfg.heal_step} step → đặt điểm hồi máu")
                        with ADB_LOCK:
                            self._setup_heal_point(pt)
                    if self._heal_requested.is_set():
                        self.log("[Bot] 🩸 Heal thủ công theo yêu cầu → HOI MAU")
                        self._heal_requested.clear()
                        with ADB_LOCK:
                            self._do_heal()
                    elif ok and self.cfg.heal_every > 0 and attack_count % self.cfg.heal_every == 0:
                        self.log(f"[Bot] Đủ {self.cfg.heal_every} lần → HOI MAU")
                        with ADB_LOCK:
                            self._do_heal()

        self._set(State.DONE)
        # Diem cuoi cung cung phai done
        if self.cur_pt and self.cur_pt.status == "process":
            self.cur_pt.status = "done"
            if self.on_refresh: self.on_refresh()
        self.log("[Bot] Hoan thanh tat ca diem!")
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1000, 300)
                time.sleep(0.15)
        except Exception:
            pass



    def navigate_to(self, game_x: int, game_y: int, force_tap: int = 0) -> bool:
        """
        Dieu huong den toa do game (game_x, game_y):
        force_tap: 0=auto(OCR), 1=tap 1 lần, 2=tap 2 lần
        """
        cfg = self.cfg
        self.log(f"[Nav] Di chuyen den ({game_x},{game_y})")

        # 1. Tap nav button
        self.log(f"[Nav] Tap nav btn ({cfg.nav_btn_x},{cfg.nav_btn_y})")
        self.adb.tap(cfg.nav_btn_x, cfg.nav_btn_y)
        time.sleep(1.0)

        # 2. Dung toa do tu cfg (chinh xac nhat, khong auto-detect)
        sc  = self.adb.screenshot_cv2()
        w_s = sc.shape[1] if sc is not None else cfg.screen_w
        h_s = sc.shape[0] if sc is not None else cfg.screen_h
        self.log(f"[Nav] Dung cfg: X=({cfg.x_field_x},{cfg.x_field_y}) "
                 f"Y=({cfg.y_field_x},{cfg.y_field_y}) "
                 f"Xem=({cfg.xem_x},{cfg.xem_y})")

        # 4. Nhap X
        self.log(f"[Nav] Nhap X={game_x} vao ({cfg.x_field_x},{cfg.x_field_y})")
        self.adb.clear_and_type(cfg.x_field_x, cfg.x_field_y, str(game_x))

        # 5. Tat keyboard, tap chinh xac vao o Y
        self.log(f"[Nav] Tat keyboard, tap Y ({cfg.y_field_x},{cfg.y_field_y})")
        self.adb._run("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y))
        time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y))
        time.sleep(0.35)
        self.adb._run("shell", "input", "keyevent", "KEYCODE_MOVE_END")
        time.sleep(0.1)
        for _ in range(12):
            self.adb._run("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.15)
        for ch in str(game_y):
            kc = self.adb._DIGIT_KC.get(ch)
            if kc:
                self.adb._run("shell", "input", "keyevent", kc)
                time.sleep(0.06)
        time.sleep(0.2)
        self.log(f"[Nav] Da nhap Y={game_y}")

        # 6. Tat keyboard bang cach tap vung ban do (tranh BACK dong popup)
        self.log(f"[Nav] Tap ban do de tat keyboard")
        self.adb._run("shell", "input", "tap", str(w_s // 2), str(int(h_s * 0.35)))
        time.sleep(0.6)

        # 7. Tap Xem 1 lan
        self.log(f"[Nav] Tap Xem ({cfg.xem_x},{cfg.xem_y})")
        self.adb._run("shell", "input", "tap", str(cfg.xem_x), str(cfg.xem_y))
        time.sleep(0.4)
        self.log(f"[Nav] Cho {cfg.nav_wait}s ban do di chuyen...")
        time.sleep(cfg.nav_wait)

        # 7. OCR truoc khi tap trung tam - kiem tra Wasteland/Lv.2-5
        cx = w_s // 2
        cy = h_s // 2
        try:
            import cv2 as _cv2, numpy as _np, pytesseract as _tess
            _raw = self.adb.screenshot_bytes()
            if _raw:
                _arr = _np.frombuffer(_raw, _np.uint8)
                _img = _cv2.imdecode(_arr, _cv2.IMREAD_COLOR)
                _h, _w = _img.shape[:2]
                _img2 = _cv2.resize(_img, (_w*2, _h*2), interpolation=_cv2.INTER_CUBIC)
                _gray = _cv2.cvtColor(_img2, _cv2.COLOR_BGR2GRAY)
                _, _th = _cv2.threshold(_gray, 0, 255, _cv2.THRESH_BINARY + _cv2.THRESH_OTSU)
                _text = _tess.image_to_string(_th, config="--psm 6")
                _keywords = ["Wasteland", "Lv.2", "Lv.3", "Lv.4", "Lv.5"]
                _found = [k for k in _keywords if k in _text]
                self.log(f"[Nav] OCR raw: {repr(_text[:200])}")
                _tap_twice = bool(_found)
                if _found:
                    self.log(f"[Nav] OCR thấy {_found} → cập nhật nhãn")
                else:
                    self.log(f"[Nav] OCR không thấy Wasteland/Lv")
            else:
                _tap_twice = False
        except Exception as _e:
            self.log(f"[Nav] OCR lỗi: {_e}")
            _tap_twice = False

        self.log(f"[Nav] Tap trung tam ({cx},{cy})")
        # force_tap: 1=single tap centre, 2=tap back(342,1216) then centre, 0=follow OCR result
        if force_tap == 2:
            self.adb.tap(342, 1216)
            time.sleep(0.5)
            self.adb.tap(cx, cy)
        else:
            self.adb.tap(cx, cy)
            do_second = (force_tap == 0 and _tap_twice)
            if do_second:
                time.sleep(0.6)
                self.adb.tap(cx, cy)

        # OCR sau tap - xem popup hien thi gi
        self._detected_label = None
        try:
            time.sleep(0.5)
            _rawb = self.adb.screenshot_bytes()
            if _rawb and HAS_CV2:
                _arrb = np.frombuffer(_rawb, np.uint8)
                _imgb = cv2.imdecode(_arrb, cv2.IMREAD_COLOR)
                _info = analyze_tile_image(_imgb)
                _textb = (_info.get('raw_title', '') + '\n' + _info.get('raw_btn', '')).strip()
                self.log(f"[Nav] OCR sau tap: {repr(_textb[:200])}")
                if _info.get('label', '').startswith('lv'):
                    self._detected_label = _info['label']
                    self.log(f"[Nav] 🏷 Phát hiện nhãn: {self._detected_label}")
        except Exception as _eb:
            self.log(f"[Nav] OCR sau tap lỗi: {_eb}")
        return True

    def _attack(self, pt: AttackPoint) -> bool:
        # 0. Brain: quet mau quân trước khi đánh
        if self.cfg.army_health_enabled:
            health_summary = self._scan_army_health(required_names=self.cfg.troop_names, next_label=pt.label)
            if health_summary.get('force_heal'):
                self.log(f"[Brain] 🩸 Máu thấp trước điểm {pt.idx} ({pt.label}) -> hồi màu trước")
                self._do_heal()
            elif health_summary.get('low_risk_only'):
                ll = (pt.label or '').lower()
                if ('lv4' in ll or 'lv.4' in ll or 'lv3' in ll or 'lv.3' in ll):
                    self.log(f"[Brain] ⚠️ Quân đang yếu, gặp điểm mạnh '{pt.label}' -> hồi màu trước")
                    self._do_heal()

        # 1. Navigate den toa do game
        self._set(State.TAPPING, f"diem {pt.idx}")
        nav_ok = self.navigate_to(pt.game_x, pt.game_y, force_tap=1)
        if not nav_ok:
            self.log("[Bot] Navigate that bai")
            return False
        # Cap nhat nhan neu OCR phat hien duoc
        if getattr(self, '_detected_label', None):
            old_lbl = pt.label
            pt.label = self._detected_label
            self.log(f"[Bot] 🏷 Cập nhật nhãn: '{old_lbl}' → '{pt.label}'")


        # 3. Cho popup Chiem
        self._set(State.WAIT_POPUP)
        cap = self._wait_capture()
        if not cap:
            self.log("[Bot] Popup khong hien - thu tap them 1 lan")
            w = self.cfg.screen_w; h = self.cfg.screen_h
            self.adb.tap(w//2 + 30, h//2)
            time.sleep(0.8)
            cap = self._wait_capture()
            if not cap:
                self.adb.tap(50, 200)   # dong popup rac
                return False

        # 3. Click Chiem - toa do co dinh
        self.log(f"[Bot] Click Chiem @ (486, 704)")
        self.adb.tap(486, 704)
        time.sleep(1.0)

        # 3.5 Kiem tra popup army_selection da hien chua
        MAX_RETRIES = 3
        for attempt in range(1, MAX_RETRIES + 1):
            sc = self.adb.screenshot_cv2()
            ptype = self.det.detect_popup_type(sc) if sc is not None else ""
            self.log(f"[Bot] detect_popup_type = '{ptype}' (lần {attempt}/{MAX_RETRIES})")
            if ptype == "army_selection":
                break

            self.log(f"[Bot] ⏳ Chưa thấy army_selection → chờ tối đa 30s... (lần {attempt}/{MAX_RETRIES})")
            deadline = time.time() + 30
            found = False
            while time.time() < deadline and not self._stop.is_set():
                time.sleep(1.0)
                sc = self.adb.screenshot_cv2()
                ptype = self.det.detect_popup_type(sc) if sc is not None else ""
                if ptype == "army_selection":
                    self.log(f"[Bot] ✅ Đã thấy army_selection")
                    found = True
                    break
            if found:
                break

            # Chưa thấy → thử Click Chiếm lại
            if attempt < MAX_RETRIES:
                self.log(f"[Bot] ⚠️ Thử Click Chiếm lại (lần {attempt + 1}/{MAX_RETRIES})...")
                self.adb.tap(486, 704)
                time.sleep(1.5)
            else:
                self.log(f"[Bot] ❌ Đã thử {MAX_RETRIES} lần, popup = '{ptype}' → bỏ qua điểm này")
                return False

        # 4. Chon quan
        self._set(State.SELECTING)
        ok = self.sel.select()
        if not ok:
            self.log("[Bot] Chon quan that bai"); return False

        # Log so quan da chon (max_troops la toi da, khong phai toi thieu)
        _selected = len([k for k in getattr(self.sel, "_last_chosen_keys", []) if k])
        self.log(f"[Bot] ✅ Đã chọn {_selected}/{self.cfg.max_troops} quân")

        # 5. Cho xong (dung march_deadline neu co)
        self._set(State.WAIT_DONE)
        march_secs  = self.sel.last_march_seconds
        march_start = self.sel.march_start_time
        if march_secs > 0 and march_start > 0:
            march_deadline = march_start + march_secs
        else:
            march_deadline = 0
        self._wait_done(march_deadline=march_deadline)
        # process = quan da den noi (sau khi het TG hanh quan)
        if self.cur_pt:
            self.cur_pt.status = "process"
            if self.on_refresh: self.on_refresh()
        return True

    def _wait_capture(self):
        dead = time.time() + self.cfg.popup_timeout
        while time.time() < dead:
            if self._stop.is_set(): return None
            pos = self.det.find_capture_btn(self.adb.screenshot_cv2())
            if pos: return pos
            time.sleep(0.5)
        return None

    def _wait_done(self, march_deadline: float = 0):
        """Cho het TG hanh quan."""
        if march_deadline > 0:
            while time.time() < march_deadline:
                if self._stop.is_set(): return
                time.sleep(1.0)
            self.log("[Bot] ✅ Hết TG hành quân")


    def _cooldown(self, secs: int):
        self._set(State.COOLDOWN, f"{secs}s")
        self._skip_cd.clear()
        dead = time.time() + secs
        logged = set()
        while time.time() < dead:
            if self._stop.is_set(): return
            if self._skip_cd.is_set():
                self.log("[Bot] ⏭️ Bỏ qua Cooldown → điểm tiếp theo")
                self._skip_cd.clear()
                return
            left = int(dead - time.time())
            mark = left - (left % 30)
            if mark not in logged:
                logged.add(mark)
                self.log(f"[Bot] Cooldown còn {left}s...")
                # Check captcha moi 30s
                if self._check_captcha(): return
            time.sleep(1.0)
        # Sau cooldown: neu dang trong thanh (Build) thi cho ra moi tiep
        if self.in_city and self.in_city.is_set():
            self.log("[Bot] 🏰 Đang trong thành (Build) → chờ ra thành rồi tiếp...")
            while self.in_city.is_set():
                if self._stop.is_set(): return
                time.sleep(1)
            self.log("[Bot] ✅ Build ra thành → tiếp tục tấn công")

    def _setup_heal_point(self, pt: AttackPoint):
        """
        Dat diem hoi mau: mo ban do, nhap toa do, di chuyen, tap giua, bam xay.
        1. Tap nav button mo ban do
        2. Nhap toa do X, Y
        3. Tap Xem de di chuyen ban do
        4. Tap giua man hinh (chon diem)
        5. Tap nut Xay (488, 746)
        6. Cap nhat heal_x, heal_y
        """
        cfg = self.cfg
        self.log(f"[HealStep] 🏗 Đặt điểm hồi máu tại ({pt.game_x},{pt.game_y})...")

        # 1. Tap nav button mo ban do
        self.log(f"[HealStep] Tap nav btn ({cfg.nav_btn_x},{cfg.nav_btn_y})")
        self.adb.tap(cfg.nav_btn_x, cfg.nav_btn_y)
        time.sleep(1.0)

        # 2. Nhap toa do X
        sc = self.adb.screenshot_cv2()
        w_s = sc.shape[1] if sc is not None else cfg.screen_w
        h_s = sc.shape[0] if sc is not None else cfg.screen_h
        self.log(f"[HealStep] Nhap X={pt.game_x}")
        self.adb.clear_and_type(cfg.x_field_x, cfg.x_field_y, str(pt.game_x))

        # 3. Tat keyboard, nhap toa do Y
        self.log(f"[HealStep] Nhap Y={pt.game_y}")
        self.adb._run("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y))
        time.sleep(0.35)
        self.adb._run("shell", "input", "tap", str(cfg.y_field_x), str(cfg.y_field_y))
        time.sleep(0.35)
        self.adb._run("shell", "input", "keyevent", "KEYCODE_MOVE_END")
        time.sleep(0.1)
        for _ in range(12):
            self.adb._run("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.15)
        for ch in str(pt.game_y):
            kc = self.adb._DIGIT_KC.get(ch)
            if kc:
                self.adb._run("shell", "input", "keyevent", kc)
                time.sleep(0.06)
        time.sleep(0.2)

        # 4. Tat keyboard bang tap vung ban do
        self.adb._run("shell", "input", "tap", str(w_s // 2), str(int(h_s * 0.35)))
        time.sleep(0.6)

        # 5. Tap Xem de di chuyen ban do
        self.log(f"[HealStep] Tap Xem ({cfg.xem_x},{cfg.xem_y})")
        self.adb._run("shell", "input", "tap", str(cfg.xem_x), str(cfg.xem_y))
        time.sleep(0.4)
        self.log(f"[HealStep] Cho {cfg.nav_wait}s ban do di chuyen...")
        time.sleep(cfg.nav_wait)

        # 6. Tap giua man hinh de chon diem
        cx, cy = w_s // 2, h_s // 2
        self.log(f"[HealStep] Tap giua ({cx},{cy})")
        self.adb.tap(cx, cy)
        time.sleep(1.0)

        # 7. Tap nut Xay (488, 746)
        self.log("[HealStep] Tap nut Xay (488, 746)")
        self.adb.tap(488, 746)
        time.sleep(1.0)

        # 8. Tap xac nhan (558, 490)
        self.log("[HealStep] Tap xác nhận (558, 490)")
        self.adb.tap(558, 490)
        time.sleep(0.8)

        # 9. Cap nhat heal_x, heal_y
        cfg.heal_x = pt.game_x
        cfg.heal_y = pt.game_y
        self.log(f"[HealStep] ✅ Heal point → ({pt.game_x},{pt.game_y})")

        # Cap nhat UI
        if self.on_heal_update: self.on_heal_update()
        if self.on_refresh: self.on_refresh()
        return True


    def _do_heal(self):
        """
        Hoi mau:
        1. Navigate den toa do heal
        2. Tap Hanh Quan + chon quan
        3. Cho TG hanh quan (march time) - quan di den diem hoi mau
        4. Sau khi het march time -> tiep tuc tan cong
        """
        self._set(State.HEALING, f"({self.cfg.heal_x},{self.cfg.heal_y})")
        self.log(f"[Heal] Di chuyen den ({self.cfg.heal_x},{self.cfg.heal_y})")
        nav_ok = self.navigate_to(self.cfg.heal_x, self.cfg.heal_y)
        if not nav_ok:
            self.log("[Heal] Navigate that bai -> bo qua hoi mau"); return

        # Tap nut March co dinh (486, 746)
        self.log("[Heal] Tap March (486, 746)")
        time.sleep(3.0)
        self.adb.tap(486, 746)
        time.sleep(0.5)

        # Chon quan + tap OK
        self._set(State.SELECTING)
        ok = self.sel.select()
        self.log(f"[Heal] Chon quan: {'OK' if ok else 'FAIL'}")
        if not ok: return

        # Cho TG hanh quan den diem hoi mau (tinh tu luc tap OK)
        march_secs  = self.sel.last_march_seconds
        march_start = self.sel.march_start_time
        if march_secs > 0 and march_start > 0:
            march_deadline = march_start + march_secs
            remaining = march_deadline - time.time()
            if remaining > 0:
                m, s = int(remaining)//60, int(remaining)%60
                self.log(f"[Heal] ⏳ Chờ quân đi hồi máu: {m}p{s:02d}s...")
                self._set(State.HEALING, f"hanh quan {int(remaining)}s")
                while time.time() < march_deadline:
                    if self._stop.is_set(): return
                    time.sleep(1.0)
            self.log("[Heal] ✅ Quân đã đến điểm hồi máu → tiếp tục tấn công")
        else:
            self.log("[Heal] ✅ Hồi máu xong → tiếp tục tấn công")
