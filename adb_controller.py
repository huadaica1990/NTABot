"""
ADB Controller - Communication with Android device via ADB.
"""
import subprocess
import time
import os
import re
import json
import numpy as np
from typing import Optional, List, Callable
from datetime import datetime

from config import (BotConfig, DEBUG_DIR, HAS_CV2)

if HAS_CV2:
    import cv2


class ADB:
    def __init__(self, cfg: BotConfig, log: Callable):
        self.cfg    = cfg
        self.log    = log
        self.ok     = False
        self.device = cfg.device_serial
        self._screen_size_cache = None

    def _run(self, *args, timeout=12):
        cmd = [self.cfg.adb_path]
        if self.device:
            cmd += ["-s", self.device]
        cmd += list(args)
        try:
            r = subprocess.run(cmd, capture_output=True,
                               text=True, timeout=timeout,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            return r.stdout.strip(), r.returncode
        except Exception:
            return "", 1

    def connect(self) -> bool:
        self._run("connect", "127.0.0.1:5555", timeout=5)
        out, _ = self._run("devices")
        devs = [l.split("\t")[0]
                for l in out.splitlines() if "\tdevice" in l]
        if not devs:
            self.log("[ADB] Không tìm thấy thiết bị"); return False
        if self.device not in devs:
            self.device = devs[0]
        self.ok = True
        self.log(f"[ADB] Kết nối: {self.device}")
        return True

    def list_devices(self):
        out, _ = self._run("devices")
        return [l.split("\t")[0]
                for l in out.splitlines() if "\tdevice" in l]

    def screenshot_bytes(self) -> Optional[bytes]:
        try:
            cmd = [self.cfg.adb_path, "-s", self.device,
                   "exec-out", "screencap", "-p"]
            r = subprocess.run(cmd, capture_output=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception as e:
            self.log(f"[SS] {e}")
        return None

    def screenshot_cv2(self):
        if not HAS_CV2: return None
        raw = self.screenshot_bytes()
        if not raw: return None
        arr = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def tap(self, x, y):
        self._run("shell", "input", "tap",
                  str(int(x)), str(int(y)))
        time.sleep(self.cfg.tap_delay)

    def swipe(self, x1, y1, x2, y2, ms=400):
        self._run("shell", "input", "swipe",
                  str(x1), str(y1), str(x2), str(y2), str(ms))
        time.sleep(0.3)

    def hold(self, x, y, ms=500):
        """
        Long press tai (x, y) trong ms milliseconds.
        Dung swipe cung toa do - Android khong co lenh longpress rieng.
        ms=120-350 -> nhu nguoi binh thuong nhan nut
        ms=500+    -> ro rang la long press
        """
        self._run("shell", "input", "swipe",
                  str(int(x)), str(int(y)),
                  str(int(x)), str(int(y)),
                  str(int(ms)))
        time.sleep(0.2)


    # Map digit -> Android keycode
    _DIGIT_KC = {
        '0': 'KEYCODE_0', '1': 'KEYCODE_1', '2': 'KEYCODE_2',
        '3': 'KEYCODE_3', '4': 'KEYCODE_4', '5': 'KEYCODE_5',
        '6': 'KEYCODE_6', '7': 'KEYCODE_7', '8': 'KEYCODE_8',
        '9': 'KEYCODE_9',
    }

    def clear_and_type(self, field_x: int, field_y: int, text: str):
        """
        Tap vao o nhap so, xoa het, go tung digit bang keyevent.
        Dung keyevent thay vi 'input text' de tuong thich LDPlayer.
        """
        # 1. Tap focus
        self._run("shell", "input", "tap", str(field_x), str(field_y))
        time.sleep(0.35)

        # 2. Di den cuoi text
        self._run("shell", "input", "keyevent", "KEYCODE_MOVE_END")
        time.sleep(0.1)

        # 3. Xóa tung ki tu bang backspace (xoa nhieu de chac chan sach)
        for _ in range(12):
            self._run("shell", "input", "keyevent", "KEYCODE_DEL")
        time.sleep(0.15)

        # 4. Go tung digit
        for ch in str(text):
            kc = self._DIGIT_KC.get(ch)
            if kc:
                self._run("shell", "input", "keyevent", kc)
                time.sleep(0.06)
        time.sleep(0.15)

    def is_in_city(self) -> bool:
        """
        Kiem tra co dang trong thanh khong bang mau pixel tai (54, 1216).
        Nut trong thanh: co mau xanh la (HSV green).
        Nut ngoai thanh: khong co xanh la.
        Nguong: >5% pixel la xanh la HSV (H=35-85, S>80, V>80).
        """
        try:
            import cv2, numpy as np
            raw = self.screenshot_bytes()
            if not raw: return False
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            x, y = 54, 1216
            x1, x2 = max(0, x-26), min(w, x+26)
            y1, y2 = max(0, y-23), min(h, y+23)
            region = img[y1:y2, x1:x2]
            hsv  = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (35, 80, 80), (85, 255, 255))
            ratio = float(np.sum(mask > 0)) / max(mask.size, 1)
            return ratio > 0.05  # >5% pixel xanh la
        except Exception:
            return False

    def read_army_status(self, required_names: list = None) -> list:
        """
        Scroll qua man hinh All Armies, OCR doc ten quan + trang thai.
        required_names: chi kiem tra cac quan co ten trong list nay.
        Tra ve list dict: [{"name": "auto1", "status": "Battling"}, ...]
        """
        import cv2, numpy as np, pytesseract
        statuses = {"Battling", "Recruiting", "Standby", "Training", "Healing", "Marching", "Returning"}
        found = {}  # name -> status (dedup)
        required_lower = [n.lower().strip() for n in (required_names or [])]
        # Keywords chi la vi tri/location, khong phai ten quan
        loc_keywords = ("Capital", "Fortress", "Plot", "Mine", "Camp", "Village", "Town")

        def _parse_name_from_line(line: str) -> str:
            """Tach ten quan tu dong 'xe3 Capital' -> 'xe3', 'auto2 Stone Plot(314,499)' -> 'auto2'"""
            import re
            # Bo phan co dau ngoac
            line = re.sub(r'\(.*?\)', '', line).strip()
            # Tach theo khoang trang, lay phan truoc keyword location
            parts = line.split()
            name_parts = []
            for p in parts:
                if any(kw in p for kw in loc_keywords):
                    break
                name_parts.append(p)
            return " ".join(name_parts).strip()

        def _ocr_screen():
            raw = self.screenshot_bytes()
            if not raw: return []
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            img2 = cv2.resize(img, (w*2, h*2), interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(thresh, config="--psm 6")
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            results = []
            for i, line in enumerate(lines):
                matched_status = next((s for s in statuses if line.startswith(s)), None)
                if matched_status and i > 0:
                    # Tim dong truoc co chua ten quan (co the la dong icons, bo qua)
                    for offset in range(1, min(4, i+1)):
                        prev = lines[i - offset]
                        # Bo qua dong icon/garbage (khong co chu cai thuong)
                        import re
                        if not re.search(r'[a-zA-Z]', prev): continue
                        # Bo qua neu la header
                        if any(kw in prev for kw in ("All Armies","Territory","Battle","Owned","e we")): continue
                        name = _parse_name_from_line(prev)
                        if name and len(name) <= 20:
                            results.append({"name": name, "status": matched_status})
                            break
            return results

        prev_names = set()
        for scroll_i in range(8):  # scroll toi da 8 lan
            batch = _ocr_screen()
            self.log(f"[Army] Scroll {scroll_i}: OCR thấy {[b['name'] for b in batch]}")
            new_found = 0
            for item in batch:
                n = item["name"]
                nl = n.lower()
                # Luôn capture quân chính (tên đầu tiên) + các tên trong required
                main_troop = required_lower[0] if required_lower else ""
                is_main = main_troop and (main_troop in nl)
                if is_main or not required_lower or any(r in nl for r in required_lower):
                    if n not in found:
                        found[n] = item["status"]
                        new_found += 1
            cur_names = set(found.keys())
            # Dừng sớm chỉ khi không còn gì mới VÀ đã tìm thấy quân chính (hoặc không cần tìm)
            _main_troop = required_lower[0] if required_lower else ""
            _need_main = bool(_main_troop)
            _found_main = any(_main_troop in n.lower() for n in cur_names) if _main_troop else True
            if scroll_i > 0 and cur_names and cur_names == prev_names:
                if not _need_main or _found_main:
                    break
            prev_names = cur_names
            # Scroll xuong de xem them
            w2, h2 = self.get_screen_size()
            self.swipe(w2//2, int(h2*0.60), w2//2, int(h2*0.50), ms=1200)
            import time; time.sleep(4.0)

        return [{"name": k, "status": v} for k, v in found.items()]

    def read_army_health(self, required_names: list = None, max_scrolls: int = 5) -> list:
        """
        Phan tich thanh mau do cua tung dao quan tren man hinh All Armies.
        Tra ve list dict: [{name, status, avg_hp, min_hp, critical_units, unit_count}]
        avg_hp/min_hp nam trong [0..1].
        """
        try:
            import cv2, numpy as np, pytesseract
        except Exception:
            return []
        import re

        required_lower = [n.lower().strip() for n in (required_names or []) if n.strip()]
        statuses = {"Battling", "Recruiting", "Standby", "Training", "Healing", "Marching", "Returning"}
        status_tokens = {s.lower() for s in statuses}
        found = {}

        def _normalize_name(s: str) -> str:
            s = (s or '').strip().lower()
            return re.sub(r'[^a-z0-9]', '', s)

        def _best_target(raw_name: str):
            rn = _normalize_name(raw_name)
            if not rn:
                return None
            best = None
            best_score = 0.0
            for t in required_lower:
                tn = _normalize_name(t)
                if not tn:
                    continue
                score = 0.0
                if rn == tn:
                    score = 1.0
                elif rn in tn or tn in rn:
                    score = min(len(rn), len(tn)) / max(len(rn), len(tn))
                elif (required_lower and required_lower[0] in rn and required_lower[0] in tn) or (len(tn) >= 3 and rn.endswith(tn[-3:])):
                    score = 0.8
                if score > best_score:
                    best_score = score
                    best = t
            return best if best_score >= 0.35 else raw_name

        def _match_required_name(raw_text: str):
            raw_n = _normalize_name(raw_text)
            if not raw_n:
                return None
            best = None
            best_score = 0.0
            for t in required_lower:
                tn = _normalize_name(t)
                if not tn:
                    continue
                score = 0.0
                if raw_n == tn:
                    score = 1.0
                elif raw_n in tn or tn in raw_n:
                    score = min(len(raw_n), len(tn)) / max(len(raw_n), len(tn))
                elif required_lower and required_lower[0] in raw_n and required_lower[0] in tn:
                    score = 0.9
                elif len(raw_n) >= 3 and len(tn) >= 3 and (raw_n.endswith(tn[-3:]) or tn.endswith(raw_n[-3:])):
                    score = 0.55
                if score > best_score:
                    best_score = score
                    best = t
            return best if best_score >= 0.45 else None

        def _build_line_texts(words):
            if not words:
                return []
            groups = []
            for w0 in sorted(words, key=lambda z: z['cy']):
                if not groups or abs(w0['cy'] - groups[-1]['cy']) > 18:
                    groups.append({'cy': w0['cy'], 'words': [w0]})
                else:
                    groups[-1]['words'].append(w0)
                    ws = groups[-1]['words']
                    groups[-1]['cy'] = sum(x['cy'] for x in ws) / len(ws)
            lines = []
            for g in groups:
                ws = sorted(g['words'], key=lambda z: z['x'])
                txt = ' '.join(x['text'] for x in ws if x['text']).strip()
                if txt:
                    lines.append({'cy': g['cy'], 'text': txt})
            return lines

        def _parse_row_name(words, row_y: float, row_x_min: float = 0.0) -> tuple[str, str, str]:
            nearby = []
            for w0 in words:
                cy = w0['cy']
                if row_y - 145 <= cy <= row_y + 70 and w0['x'] < w_limit and w0['x'] <= max(w_limit, row_x_min + 260):
                    nearby.append(w0)
            nearby.sort(key=lambda z: (round(z['cy'] / 18.0), z['x']))
            text_line = ' '.join(x['text'] for x in nearby if x['text']).strip()
            if not text_line:
                return '', '', ''
            status = next((s for s in statuses if s.lower() in text_line.lower()), '')
            # Ưu tiên match trực tiếp với danh sách required nếu có
            if required_lower:
                m = _match_required_name(text_line)
                if m:
                    return m, status, text_line
            tokens = [tok for tok in re.split(r'\s+', text_line) if tok.strip()]
            candidates = []
            for tok in tokens:
                clean = re.sub(r'[^A-Za-z0-9_-]', '', tok)
                if not clean:
                    continue
                low = clean.lower()
                if low in status_tokens:
                    continue
                if any(k.lower() in low for k in ('Capital','Fortress','Plot','Mine','Camp','Village','Town','Marching','Returning','Battling')):
                    continue
                if len(clean) < 3:
                    continue
                candidates.append(clean)
            if not candidates:
                return '', status, text_line
            preferred = next((c for c in candidates if re.search(r'[A-Za-z]+\d+', c) or (required_lower and required_lower[0] in c.lower())), candidates[0])
            return preferred, status, text_line

        def _save_debug(scroll_i: int, img, mask, rows, words):
            if not getattr(self.cfg, 'debug_mode', False):
                return
            try:
                dbg = img.copy()
                for row in rows:
                    color = (0, 255, 255)
                    for b in row['bars']:
                        cv2.rectangle(dbg, (b['x'], b['y']), (b['x'] + b['w'], b['y'] + b['h']), (0, 0, 255), 1)
                    cv2.putText(dbg, f"cy={int(row['cy'])} n={len(row['bars'])}", (5, int(row['cy'])), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
                ts = datetime.now().strftime('%H%M%S')
                cv2.imwrite(os.path.join(DEBUG_DIR, f'armyhp_scroll_{scroll_i}_{ts}.png'), dbg)
                cv2.imwrite(os.path.join(DEBUG_DIR, f'armyhp_mask_{scroll_i}_{ts}.png'), mask)
                with open(os.path.join(DEBUG_DIR, f'armyhp_words_{scroll_i}_{ts}.txt'), 'w', encoding='utf-8') as f:
                    for w0 in words:
                        f.write(f"{w0['text']} | x={w0['x']:.1f} cy={w0['cy']:.1f}\n")
            except Exception:
                pass

        prev_keys = set()
        unknown_counter = 0
        for scroll_i in range(max_scrolls):
            raw = self.screenshot_bytes()
            if not raw:
                break
            arr = np.frombuffer(raw, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                break
            h, w = img.shape[:2]
            w_limit = int(w * 0.80)

            # OCR words
            img2 = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
            _, thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            data = pytesseract.image_to_data(thr, config='--psm 6', output_type=pytesseract.Output.DICT)
            words = []
            n = len(data.get('text', []))
            for i in range(n):
                txt = (data['text'][i] or '').strip()
                if not txt:
                    continue
                try:
                    conf = float(data['conf'][i])
                except Exception:
                    conf = -1
                if conf < -1 and not re.search(r'[A-Za-z0-9]', txt):
                    continue
                x = data['left'][i] / 2.0
                y = data['top'][i] / 2.0
                ww = data['width'][i] / 2.0
                hh = data['height'][i] / 2.0
                words.append({'text': txt, 'x': x, 'y': y, 'w': ww, 'h': hh, 'cy': y + hh / 2.0})
            line_texts = _build_line_texts(words)
            required_hints = {}
            for line in line_texts:
                m = _match_required_name(line['text'])
                if m and m not in required_hints:
                    required_hints[m] = line['cy']

            # Detect red HP bars - thoang hon de bat duoc bar mong / anti-aliasing
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            mask1 = cv2.inRange(hsv, (0, 45, 65), (20, 255, 255))
            mask2 = cv2.inRange(hsv, (155, 45, 65), (180, 255, 255))
            mask = cv2.bitwise_or(mask1, mask2)
            kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1))
            kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (4, 2))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_open)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_close)
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            bars = []
            for c in cnts:
                x, y, bw, bh = cv2.boundingRect(c)
                if x < int(w * 0.02) or x > int(w * 0.98):
                    continue
                if y < int(h * 0.18) or y > int(h * 0.96):
                    continue
                if bw < 3 or bw > 140 or bh < 1 or bh > 14:
                    continue
                if bw < bh * 1.1:
                    continue
                bars.append({'x': x, 'y': y, 'w': bw, 'h': bh, 'cy': y + bh / 2.0})
            bars.sort(key=lambda b: b['cy'])

            # Group bars by row Y - cho phep lech cao hon
            rows = []
            for b in bars:
                if not rows or abs(b['cy'] - rows[-1]['cy']) > 38:
                    rows.append({'cy': b['cy'], 'bars': [b]})
                else:
                    rows[-1]['bars'].append(b)
                    bs = rows[-1]['bars']
                    rows[-1]['cy'] = sum(x['cy'] for x in bs) / len(bs)
            for row in rows:
                row['bars'].sort(key=lambda z: z['x'])

            _save_debug(scroll_i, img, mask, rows, words)
            self.log(f"[ArmyHP] Debug Scroll {scroll_i}: words={len(words)} bars={len(bars)} rows={len(rows)} hints={list(required_hints.keys())}")

            pending_rows = []
            visible_names = []
            for row_idx, row in enumerate(rows):
                widths = sorted([max(1, b['w']) for b in row['bars']])
                if not widths:
                    continue
                max_w = max(widths)
                # Bo qua row o sat tren cung / nhiễu icon tài nguyên
                if row['cy'] < h * 0.22:
                    self.log(f"[ArmyHP] Row {scroll_i}.{row_idx}: cy={int(row['cy'])} bars={len(widths)} -> skip top-noise")
                    continue
                if len(widths) == 1 and max_w < 8:
                    continue
                ratios = [min(1.0, w0 / max_w) for w0 in widths]
                row_x_min = min(b['x'] for b in row['bars']) if row['bars'] else 0
                name, status, raw_text = _parse_row_name(words, row['cy'], row_x_min=row_x_min)
                raw_name = name
                if name:
                    name = _best_target(name) or name
                item = {
                    'row_idx': row_idx,
                    'cy': row['cy'],
                    'ratios': ratios,
                    'status': status or 'Unknown',
                    'raw_text': raw_text,
                    'raw_name': raw_name,
                    'name': name,
                }
                if not name:
                    pending_rows.append(item)
                    self.log(f"[ArmyHP] Row {scroll_i}.{row_idx}: cy={int(row['cy'])} bars={len(ratios)} name='' raw={raw_text!r} -> pending")
                    continue
                nl = _normalize_name(name)
                is_req = (not required_lower) or any(_normalize_name(r) in nl or nl in _normalize_name(r) for r in required_lower) or (required_lower and required_lower[0] in nl)
                if not is_req and raw_name:
                    raw_nl = _normalize_name(raw_name)
                    is_req = any(_normalize_name(r) in raw_nl or raw_nl in _normalize_name(r) for r in required_lower)
                if not is_req:
                    # Van giu lai de fallback assign theo hint/order
                    item['name'] = ''
                    pending_rows.append(item)
                    self.log(f"[ArmyHP] Row {scroll_i}.{row_idx}: cy={int(row['cy'])} bars={len(ratios)} name={name!r} -> pending/skip-required")
                    continue
                visible_names.append(name)
                avg_hp = sum(ratios) / len(ratios)
                min_hp = min(ratios)
                critical_units = sum(1 for r in ratios if r <= self.cfg.health_critical_ratio)
                obj = {
                    'name': name,
                    'status': item['status'],
                    'avg_hp': round(avg_hp, 3),
                    'min_hp': round(min_hp, 3),
                    'critical_units': int(critical_units),
                    'unit_count': int(len(ratios)),
                }
                self.log(
                    f"[ArmyHP] Row {scroll_i}.{row_idx}: cy={int(row['cy'])} bars={len(ratios)} "
                    f"name={name!r} raw={raw_text!r} avg={obj['avg_hp']:.2f} min={obj['min_hp']:.2f} crit={obj['critical_units']}"
                )
                prev = found.get(name)
                if prev is None or obj['unit_count'] > prev.get('unit_count', 0):
                    found[name] = obj

            # Fallback: gan row chua co ten vao danh sach required con thieu theo vi tri OCR / thu tu hien thi
            if required_lower and pending_rows:
                missing = [t for t in required_lower if t not in found]
                assigned = []
                # Buoc 1: dua theo hint OCR toan dong
                for item in sorted(pending_rows, key=lambda z: z['cy']):
                    if not missing:
                        break
                    best = None
                    best_dist = 10**9
                    for t in missing:
                        hint_cy = required_hints.get(t)
                        if hint_cy is None:
                            continue
                        dist = abs(item['cy'] - hint_cy)
                        if dist < best_dist:
                            best = t
                            best_dist = dist
                    if best is not None and best_dist <= 180:
                        item['name'] = best
                        assigned.append(item)
                        missing.remove(best)
                        self.log(f"[ArmyHP] Fallback hint: cy={int(item['cy'])} -> {best!r} (dist={int(best_dist)})")
                # Buoc 2: neu van thieu, gan theo thu tu row con lai tu tren xuong
                for item in sorted(pending_rows, key=lambda z: z['cy']):
                    if not missing:
                        break
                    if item.get('name'):
                        continue
                    if len(item['ratios']) < 2:
                        continue
                    best = missing.pop(0)
                    item['name'] = best
                    assigned.append(item)
                    self.log(f"[ArmyHP] Fallback order: cy={int(item['cy'])} -> {best!r}")
                for item in assigned:
                    name = item['name']
                    if not name:
                        continue
                    ratios = item['ratios']
                    avg_hp = sum(ratios) / len(ratios)
                    min_hp = min(ratios)
                    critical_units = sum(1 for r in ratios if r <= self.cfg.health_critical_ratio)
                    obj = {
                        'name': name,
                        'status': item['status'],
                        'avg_hp': round(avg_hp, 3),
                        'min_hp': round(min_hp, 3),
                        'critical_units': int(critical_units),
                        'unit_count': int(len(ratios)),
                    }
                    visible_names.append(name)
                    prev = found.get(name)
                    if prev is None or obj['unit_count'] > prev.get('unit_count', 0):
                        found[name] = obj
                    self.log(
                        f"[ArmyHP] Row {scroll_i}.{item['row_idx']}: cy={int(item['cy'])} bars={len(ratios)} "
                        f"name={name!r} raw={item['raw_text']!r} avg={obj['avg_hp']:.2f} min={obj['min_hp']:.2f} crit={obj['critical_units']} [fallback]"
                    )
            elif not required_lower:
                for item in pending_rows:
                    if len(item['ratios']) >= 3:
                        unknown_counter += 1
                        name = f'unknown_{unknown_counter}'
                        ratios = item['ratios']
                        obj = {
                            'name': name,
                            'status': item['status'],
                            'avg_hp': round(sum(ratios) / len(ratios), 3),
                            'min_hp': round(min(ratios), 3),
                            'critical_units': int(sum(1 for r in ratios if r <= self.cfg.health_critical_ratio)),
                            'unit_count': int(len(ratios)),
                        }
                        visible_names.append(name)
                        found[name] = obj
                        self.log(f"[ArmyHP] Row {scroll_i}.{item['row_idx']}: cy={int(item['cy'])} bars={len(ratios)} name={name!r} [unknown-fallback]")

            self.log(f"[ArmyHP] Scroll {scroll_i}: {visible_names}")
            cur_keys = set(found.keys())
            if scroll_i > 0 and cur_keys and cur_keys == prev_keys:
                break
            prev_keys = cur_keys
            w2, h2 = self.get_screen_size()
            self.swipe(w2//2, int(h2*0.60), w2//2, int(h2*0.50), ms=1200)
            time.sleep(3.0)

        return list(found.values())

    def get_screen_size(self, refresh: bool = False):
        if self._screen_size_cache and not refresh:
            return self._screen_size_cache
        out, _ = self._run("shell", "wm", "size")
        for part in out.split():
            if "x" in part:
                try:
                    w, h = part.split("x")
                    self._screen_size_cache = (int(w), int(h))
                    return self._screen_size_cache
                except Exception:
                    pass
        self._screen_size_cache = (self.cfg.screen_w, self.cfg.screen_h)
