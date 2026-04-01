"""
=================================================================
  Nine Acres Bot  —  Grid Attack Edition
  Python 3.10+  |  pip install opencv-python pillow
  LDPlayer9: Settings → Others → Enable ADB (port 5555)
=================================================================
  WORKFLOW:
    1. Connect ADB
    2. Screenshot → hiện lên với grid
    3. Click chọn điểm 1, 2, 3... trên bản đồ
    4. Nhấn RUN → bot tự tap từng điểm:
         tap → chờ popup Chiếm → chọn quân → OK
         → chờ 5 phút → điểm tiếp theo
=================================================================
"""

import subprocess, threading, time, json, os, io, math, copy

# Lock dùng chung cho tất cả WaveGroupEngine - chỉ 1 nhóm được thao tác ADB cùng lúc
_WAVE_ADB_LOCK = threading.Lock()
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from dataclasses import dataclass, field
from typing import Optional, List, Callable
from datetime import datetime
from enum import Enum, auto

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ──────────────────────────────────────────────
#  PATHS & DEFAULTS
# ──────────────────────────────────────────────

def _find_adb() -> str:
    for p in [
        r"E:\LDPlayer\LDPlayer9\adb.exe",
        r"C:\LDPlayer\LDPlayer9\adb.exe",
        r"D:\LDPlayer\LDPlayer9\adb.exe",
        r"C:\Program Files\LDPlayer\LDPlayer9\adb.exe",
        "adb",
    ]:
        if p == "adb" or os.path.isfile(p):
            return p
    return "adb"


BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR  = os.path.join(BASE_DIR, "assets")
DEBUG_DIR   = os.path.join(BASE_DIR, "debug")
CONFIG_DIR  = os.path.join(BASE_DIR, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")  # default
DEFAULT_ADB = _find_adb()

os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR,  exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)


# ──────────────────────────────────────────────
#  STATE MACHINE
# ──────────────────────────────────────────────

class State(Enum):
    IDLE            = auto()
    TAPPING         = auto()
    WAIT_POPUP      = auto()
    SELECTING       = auto()
    WAIT_DONE       = auto()
    COOLDOWN        = auto()
    HEALING         = auto()
    DONE            = auto()


# ──────────────────────────────────────────────
#  DATA
# ──────────────────────────────────────────────

@dataclass
class AttackPoint:
    idx:    int
    game_x: int    # toa do game world X
    game_y: int    # toa do game world Y
    label:  str = ""
    status: str = "waiting"  # waiting | going | process | done

@dataclass
class WaveGroup:
    """Nhom tan cong: quan chinh (ten dau tien) + nhieu auto, danh theo thu tu cac diem."""
    name:        str       = ""        # ten nhom, vd: "Nhóm 1"
    troop_names: List[str] = field(default_factory=list)  # ten dau tien = quan chinh (bat buoc)
    points:      List[dict]= field(default_factory=list)  # [{"x":..,"y":..,"label":..}]
    enabled:     bool      = True

@dataclass
class BotConfig:
    adb_path:       str   = DEFAULT_ADB
    device_serial:  str   = "emulator-5554"
    screen_w:      int   = 540
    screen_h:      int   = 960
    tap_delay:     float = 0.4
    troop_names:   List[str] = field(
        default_factory=lambda: ["auto1","auto2","auto3","auto4","auto5"])
    max_troops:    int   = 5
    cooldown_sec:     int   = 300
    lv2_cooldown_sec: int   = 600
    lv3_cooldown_sec: int   = 900
    lv4_cooldown_sec: int   = 1200
    popup_timeout: float = 10.0
    game_start:    str   = ""   # ISO: YYYY-MM-DD HH:MM
    troop_timeout: float = 8.0
    done_timeout:  float = 3600.0
    debug_mode:       bool  = False
    ollama_enabled:   bool  = False
    ollama_url:       str   = "http://localhost:11434"
    ollama_model:     str   = "qwen2-vl:7b"
    # Nav button (nut ban do co dinh goc duoi phai)
    nav_btn_x:   int   = 558   # pixel man hinh
    nav_btn_y:   int   = 1216
    # Vi tri o nhap X: va Y: (sau khi bam nav button)
    x_field_x:   int   = 378
    x_field_y:   int   = 1002
    y_field_x:   int   = 486
    y_field_y:   int   = 1002
    # Nut Xem
    xem_x:       int   = 630
    xem_y:       int   = 1002
    nav_wait:    float = 1.5
    # Hoi mau
    heal_every:  int   = 5     # sau bao nhieu lan tan cong thi hoi mau
    heal_x:      int   = 500   # toa do X hoi mau
    heal_y:      int   = 300   # toa do Y hoi mau
    heal_step:   int   = 0     # cu X diem thi dat diem hoi mau (0 = tat)
    # Army health brain
    army_health_enabled: bool = True
    health_warn_avg: float = 0.55
    health_force_heal_avg: float = 0.30
    health_critical_ratio: float = 0.25
    health_force_heal_critical_units: int = 2
    anthropic_api_key: str  = ""


# ──────────────────────────────────────────────
#  OLLAMA VISION
# ──────────────────────────────────────────────

class OllamaVision:
    def __init__(self, url="http://localhost:11434", model="qwen2-vl:7b"):
        self.url   = url.rstrip("/")
        self.model = model

    def is_occupying(self, image_bytes: bytes):
        """True=dang chiem, False=het, None=loi"""
        import base64, urllib.request
        # Crop vung trung tam man hinh truoc khi gui AI
        try:
            import cv2, numpy as np
            arr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            # Crop 30%-70% chieu cao, 25%-75% chieu ngang
            y1, y2 = int(h * 0.30), int(h * 0.70)
            x1, x2 = int(w * 0.25), int(w * 0.75)
            crop = img[y1:y2, x1:x2]
            # Scale up 3x de AI nhin ro hon
            crop = cv2.resize(crop, (crop.shape[1]*3, crop.shape[0]*3),
                              interpolation=cv2.INTER_NEAREST)
            _, buf = cv2.imencode(".png", crop)
            image_bytes = buf.tobytes()
        except Exception:
            pass  # fallback: dung anh goc
        b64 = base64.b64encode(image_bytes).decode()
        prompt = (
            "Look at this image. "
            "Do you see a small pixel art icon of a sword or a shield (or both) anywhere in the image? "
            "It may appear as just a sword or just a shield since it is animated. "
            "Answer only YES or NO."
        )
        payload = json.dumps({
            "model": self.model, "prompt": prompt,
            "images": [b64], "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{self.url}/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ans = json.loads(r.read()).get("response","").strip()
                self._last_response = ans
                u = ans.upper()
                if "YES" in u: return True
                if "NO"  in u: return False
                return None
        except Exception as e:
            self._last_response = str(e)
            return None


# ──────────────────────────────────────────────
#  ADB
# ──────────────────────────────────────────────

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
                               text=True, timeout=timeout)
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
            r = subprocess.run(cmd, capture_output=True, timeout=15)
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
        return self._screen_size_cache


class TroopBrain:
    """Rule engine phan tich mau dao quan va de xuat hanh dong."""
    def __init__(self, cfg: BotConfig, log: Callable):
        self.cfg = cfg
        self.log = log
        self.history = {}

    def analyze(self, troop: dict, next_label: str = "") -> dict:
        name = troop.get('name', '?')
        avg_hp = float(troop.get('avg_hp', 1.0) or 1.0)
        min_hp = float(troop.get('min_hp', avg_hp) or avg_hp)
        critical_units = int(troop.get('critical_units', 0) or 0)
        unit_count = max(1, int(troop.get('unit_count', 1) or 1))
        hist = self.history.setdefault(name, [])
        prev_avg = hist[-1]['avg_hp'] if hist else avg_hp
        hp_trend = round(avg_hp - prev_avg, 3)
        hist.append({'ts': time.time(), 'avg_hp': avg_hp, 'min_hp': min_hp})
        if len(hist) > 8:
            del hist[:-8]

        lv_penalty = 0
        ll = (next_label or '').lower()
        if 'lv4' in ll or 'lv.4' in ll:
            lv_penalty = 18
        elif 'lv3' in ll or 'lv.3' in ll:
            lv_penalty = 10
        elif 'lv2' in ll or 'lv.2' in ll:
            lv_penalty = 4

        risk = int((1.0 - avg_hp) * 45 + (critical_units / unit_count) * 30 + max(0.0, -hp_trend) * 35 + lv_penalty)

        if avg_hp <= self.cfg.health_force_heal_avg or critical_units >= self.cfg.health_force_heal_critical_units:
            action = 'heal_now'
        elif min_hp <= self.cfg.health_critical_ratio and lv_penalty >= 10:
            action = 'heal_now'
        elif avg_hp <= self.cfg.health_warn_avg:
            action = 'low_risk_only'
        else:
            action = 'continue'

        return {
            'name': name,
            'avg_hp': avg_hp,
            'min_hp': min_hp,
            'critical_units': critical_units,
            'unit_count': unit_count,
            'hp_trend': hp_trend,
            'risk_score': risk,
            'action': action,
            'status': troop.get('status', 'Unknown'),
        }

    def summarize(self, troops: list, next_label: str = '') -> dict:
        analyses = [self.analyze(t, next_label=next_label) for t in troops]
        force_heal = any(a['action'] == 'heal_now' for a in analyses)
        low_risk_only = (not force_heal) and any(a['action'] == 'low_risk_only' for a in analyses)
        return {
            'troops': analyses,
            'force_heal': force_heal,
            'low_risk_only': low_risk_only,
        }


# ──────────────────────────────────────────────
#  UI DETECTOR
# ──────────────────────────────────────────────

class Detector:
    def __init__(self, assets_dir: str, log: Callable):
        self.assets_dir = assets_dir
        self.log        = log
        self.templates  = {}
        self.reload()

    def reload(self):
        self.templates = {}
        if not HAS_CV2 or not os.path.isdir(self.assets_dir):
            return
        n = 0
        for f in os.listdir(self.assets_dir):
            if f.lower().endswith(".png"):
                img = cv2.imread(os.path.join(self.assets_dir, f))
                if img is not None:
                    self.templates[f[:-4].lower()] = img
                    n += 1
        if n: self.log(f"[Det] {n} templates")

    def _load_atk_templates(self):
        """Load multi-frame ATK templates tu assets/.
        Ho tro: btn_atk.png, btn_atk_0.png, btn_atk_1.png, ...
        """
        loaded = 0
        # Frame co so
        base = os.path.join(self.assets_dir, "btn_atk.png")
        img = cv2.imread(base) if os.path.exists(base) else None
        if img is not None:
            self.templates["btn_atk_f0"] = img
            loaded += 1
        # Frame danh so: btn_atk_0.png, btn_atk_1.png, ...
        i = 0
        while True:
            path = os.path.join(self.assets_dir, f"btn_atk_{i}.png")
            if not os.path.exists(path): break
            img = cv2.imread(path)
            if img is not None:
                self.templates[f"btn_atk_f{i+1}"] = img
                loaded += 1
            i += 1
        self._atk_templates_loaded = True
        if loaded:
            self.log(f"[Det] btn_atk: {loaded} frame(s) loaded")
        else:
            self.log(f"[Det] btn_atk: khong co file nao trong assets/")

    def find_atk_btn(self, screen, thr=0.65):
        """Tim icon ATK - thu tat ca frame template."""
        if not getattr(self, "_atk_templates_loaded", False):
            self._load_atk_templates()
        keys = [k for k in self.templates if k.startswith("btn_atk_f")]
        for key in keys:
            for t in [thr, thr - 0.10]:
                p = self._match(screen, key, t)
                if p:
                    self.log(f"[Det] ATK match: {key} thr={t:.2f} pos={p}")
                    return p
        return None

    def _match(self, screen, key, thr=0.72):
        if not HAS_CV2 or screen is None: return None
        if key not in self.templates: return None
        t = self.templates[key]
        th, tw = t.shape[:2]
        sh, sw = screen.shape[:2]
        if tw >= sw or th >= sh: return None
        res = cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        if mv >= thr:
            return (ml[0]+tw//2, ml[1]+th//2)
        return None

    def find_capture_btn(self, screen):
        """
        Tim nut Chiem / Conquer.
        Nut nay la hop trang/kem co chu 'Chiem' hoac 'Conquer' + icon X do.
        Tim o nua phai man hinh, phan giua-duoi.
        """
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_capture", thr)
            if p: return p
        if not HAS_CV2 or screen is None: return None
        h, w = screen.shape[:2]

        # --- Pass 1: icon X do (mau do tuoi BGR) ---
        lo_r = np.array([0,  30, 160], np.uint8)
        hi_r = np.array([80, 110, 255], np.uint8)
        mask_r = cv2.inRange(screen, lo_r, hi_r)
        cnts_r, _ = cv2.findContours(mask_r, cv2.RETR_EXTERNAL,
                                      cv2.CHAIN_APPROX_SIMPLE)
        best_a, best_p = 0, None
        for c in cnts_r:
            a = cv2.contourArea(c)
            if a > 200 and a > best_a:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = int(M["m10"]/M["m00"])
                    cy = int(M["m01"]/M["m00"])
                    # Phai nam o nua phai man hinh
                    if cx > w * 0.45:
                        best_a = a; best_p = (cx, cy)
        if best_p:
            # Shift sang phai de tap vao giua nut (icon nam ben trai)
            return (min(best_p[0] + 60, w - 10), best_p[1])

        # --- Pass 2: nut trang (Conquer/Chiem) - hop kem vien tron ---
        # Tim vung trang lon o nua phai, nua duoi man hinh
        roi_x0 = w // 2
        roi = screen[int(h*0.3):int(h*0.85), roi_x0:]
        lo_w = np.array([210, 210, 195], np.uint8)
        hi_w = np.array([255, 255, 240], np.uint8)
        mask_w = cv2.inRange(roi, lo_w, hi_w)
        # Dilate de lien ket cac phan
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 8))
        mask_w = cv2.dilate(mask_w, kernel)
        cnts_w, _ = cv2.findContours(mask_w, cv2.RETR_EXTERNAL,
                                      cv2.CHAIN_APPROX_SIMPLE)
        btn_rects = []
        for c in cnts_w:
            a = cv2.contourArea(c)
            x, y, bw, bh = cv2.boundingRect(c)
            # Nut phai co ti le ngang (rong > cao) va du lon
            if a > 1500 and bw > bh * 1.5 and bw > 60:
                btn_rects.append((y, x, bw, bh))  # sort by y
        if btn_rects:
            # Lay nut thap nhat (Conquer/Chiem thuong o duoi Enter)
            btn_rects.sort(key=lambda r: r[0], reverse=True)
            y, x, bw, bh = btn_rects[0]
            cx = roi_x0 + x + bw // 2
            cy = int(h*0.3) + y + bh // 2
            return (cx, cy)

        return None

    def find_build_btn(self, screen):
        """
        Tim nut Build bang template matching (assets/btn_build.png).
        Logic giong find_capture_btn: thu nhieu threshold giam dan.
        """
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_build", thr)
            if p: return p
        return None

    def find_ok_btn(self, screen):
        # Vi tri co dinh cua nut OK
        return (342, 1045)

    def has_capture_popup(self, screen) -> bool:
        return self.find_capture_btn(screen) is not None

    def is_troop_screen(self, screen) -> bool:
        p = self.find_ok_btn(screen)
        if p is None: return False
        if screen is None: return False
        return p[1] > screen.shape[0] * 0.5

    def popup_closed(self, screen) -> bool:
        return not self.has_capture_popup(screen)

    def find_xem_btn(self, screen):
        """Tim nut Xem (vang/cam) o cuoi man hinh."""
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_xem", thr)
            if p: return p
        if not HAS_CV2 or screen is None: return None
        h, w = screen.shape[:2]
        # Nut Xem: mau vang dam BGR ~ [50,180,220]
        lo = np.array([30,  160, 195], np.uint8)
        hi = np.array([120, 220, 255], np.uint8)
        # Chi tim nua duoi man hinh
        roi  = screen[int(h*0.85):, :]
        mask = cv2.inRange(roi, lo, hi)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
        best_a, best_p = 0, None
        for c in cnts:
            a = cv2.contourArea(c)
            if a > 800 and a > best_a:
                best_a = a
                M = cv2.moments(c)
                if M["m00"] > 0:
                    best_p = (int(M["m10"]/M["m00"]),
                              int(M["m01"]/M["m00"]) + int(h*0.85))
        return best_p


    def find_hanh_quan_btn(self, screen):
        """Tim nut Hanh Quan (vang/nau vang) xuat hien sau khi tap vao o tren ban do."""
        for thr in [0.85, 0.75, 0.65]:
            p = self._match(screen, "btn_hanh_quan", thr)
            if p: return p
        if not HAS_CV2 or screen is None: return None
        h, w = screen.shape[:2]
        # Nut Hanh Quan: mau vang nhat BGR ~ [30,160,200] den [120,215,255]
        lo = np.array([30,  155, 185], np.uint8)
        hi = np.array([130, 220, 255], np.uint8)
        # Tim o nua duoi man hinh (thuong xuat hien giua-duoi)
        roi  = screen[int(h*0.55):, :]
        mask = cv2.inRange(roi, lo, hi)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
        best_a, best_p = 0, None
        for c in cnts:
            a = cv2.contourArea(c)
            if a > 1200 and a > best_a:
                best_a = a
                M = cv2.moments(c)
                if M["m00"] > 0:
                    best_p = (int(M["m10"]/M["m00"]),
                              int(M["m01"]/M["m00"]) + int(h*0.55))
        return best_p

    def has_hanh_quan_btn(self, screen) -> bool:
        return self.find_hanh_quan_btn(screen) is not None


    def has_error_popup(self, screen) -> bool:
        """
        Kiem tra co popup loi (hop trang vien tron, chu den) khong.
        Vi du: 'Chi co the chiem manh dat lien ke'.
        Popup nay thuong xuat hien giua man hinh, nen vung nen la mau trang.
        """
        if not HAS_CV2 or screen is None: return False
        h, w = screen.shape[:2]
        # Scan vung giua man hinh (30%-70% cao, 10%-90% ngang)
        y0 = int(h * 0.30); y1 = int(h * 0.65)
        x0 = int(w * 0.10); x1 = int(w * 0.90)
        roi = screen[y0:y1, x0:x1]
        # Popup loi: nen trang (R>220, G>220, B>220) chiem nhieu
        lo = np.array([210, 210, 210], np.uint8)
        hi = np.array([255, 255, 255], np.uint8)
        mask  = cv2.inRange(roi, lo, hi)
        white_ratio = mask.sum() / 255 / (roi.shape[0] * roi.shape[1])
        return white_ratio > 0.35  # >35% la trang -> co popup

    def dismiss_error_popup(self, screen):
        """Tra ve toa do de tap dong popup loi (vung giua-duoi popup)."""
        if screen is None: return None
        h, w = screen.shape[:2]
        # Tap vao giua man hinh de dong
        return (w // 2, int(h * 0.55))

    def has_captcha_popup(self, screen) -> bool:
        """
        Kiem tra co popup CAPTCHA 'Random Test' khong.
        Popup hien giua man hinh voi text 'Random Test'.
        Dung OCR de phat hien.
        """
        if not HAS_CV2 or screen is None: return False
        try:
            import pytesseract
            h, w = screen.shape[:2]
            # Crop vung giua-tren man hinh (20%-55% cao, 10%-90% ngang)
            y0, y1 = int(h * 0.15), int(h * 0.55)
            x0, x1 = int(w * 0.10), int(w * 0.90)
            roi = screen[y0:y1, x0:x1]
            # Scale up + grayscale + threshold cho OCR tot hon
            roi2 = cv2.resize(roi, (roi.shape[1]*2, roi.shape[0]*2),
                              interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(roi2, cv2.COLOR_BGR2GRAY)
            _, th = cv2.threshold(gray, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(th, config="--psm 6")
            text_lower = text.lower()
            # Tim keywords cua CAPTCHA popup
            captcha_kw = ["random test", "please select", "from the pictures",
                          "countdown"]
            found = sum(1 for kw in captcha_kw if kw in text_lower)
            return found >= 2  # Tim thay it nhat 2 keywords
        except Exception:
            return False
    def find_xy_fields(self, screen):
        """
        Tra ve (x_field, y_field) ADB pixel khi popup ban do mo.
        Scan vung duoi cung tim 2 o trang (input fields) truoc nut Xem.
        """
        if not HAS_CV2 or screen is None: return None, None
        h, w = screen.shape[:2]
        xem = self.find_xem_btn(screen)
        if xem is None: return None, None
        xem_x, xem_y = xem

        # Scan hang ngang tai xem_y, tim cac vung trang (o nhap)
        bar_y = xem_y
        row   = screen[bar_y, :]
        in_w  = False; start = 0; fields = []
        for x in range(0, xem_x - 20):
            px = row[x]
            is_w = (int(px[0]) > 195 and int(px[1]) > 195
                    and int(px[2]) > 195)
            if is_w and not in_w:
                in_w = True; start = x
            elif not is_w and in_w:
                in_w = False
                if x - start > 25:   # o nhap phai rong
                    fields.append((start + x) // 2)
        if len(fields) >= 2:
            return (fields[0], bar_y), (fields[1], bar_y)
        elif len(fields) == 1:
            # Chi tim duoc 1: gia su X field, Y = Xem - 130
            return (fields[0], bar_y), (xem_x - 130, bar_y)
        return None, None

    def has_nav_popup(self, screen) -> bool:
        """Kiem tra popup ban do da mo chua (co nut Xem)."""
        return self.find_xem_btn(screen) is not None

    def detect_checkboxes(self, screen) -> List[int]:
        """Y-positions của các checkbox hiển thị."""
        if not HAS_CV2 or screen is None: return []
        h, w = screen.shape[:2]
        x0 = int(w * 0.080)
        x1 = int(w * 0.120)
        lo = np.array([100, 125, 148], np.uint8)
        hi = np.array([148, 172, 198], np.uint8)
        roi  = screen[:, x0:x1]
        mask = cv2.inRange(roi, lo, hi)
        proj = (mask.sum(axis=1) / 255).astype(float)
        roi_w = x1 - x0
        P_THR = int(roi_w * 0.35)
        H_THR = int(roi_w * 0.45)
        y_min = int(h * 0.22)
        y_max = int(h * 0.88)
        peaks = []
        in_r, start = False, 0
        for y in range(y_min, y_max):
            v = proj[y]
            if v >= P_THR and not in_r:
                in_r, start = True, y
            elif v < P_THR and in_r:
                in_r = False
                if float(proj[start:y].max()) >= H_THR:
                    peaks.append((start + y) // 2)
        ys = []
        for cy in peaks:
            if not ys or cy - ys[-1] > 35:
                ys.append(cy)
            else:
                ys[-1] = (ys[-1] + cy) // 2
        return ys

    def checkbox_x(self, screen) -> int:
        if screen is None: return 71
        return int(screen.shape[1] * 0.100)

    def is_checked(self, screen, cb_x: int, cb_y: int) -> bool:
        """
        Kiem tra checkbox tai (cb_x, cb_y) da duoc check chua.
        Checked = co dau check mau xanh la ben trong o vuong.
        """
        if not HAS_CV2 or screen is None: return False
        h, w = screen.shape[:2]
        x0 = max(0, cb_x - 7); x1 = min(w, cb_x + 7)
        y0 = max(0, cb_y - 7); y1 = min(h, cb_y + 7)
        region = screen[y0:y1, x0:x1]
        if region.size == 0: return False
        # Mau xanh la: G cao, B va R thap
        lo = np.array([20,  100,  20], np.uint8)
        hi = np.array([130, 230, 130], np.uint8)
        green_px = int(cv2.inRange(region, lo, hi).sum() / 255)
        return green_px >= 4

    def read_troop_name(self, screen, cb_x: int, cb_y: int) -> str:
        """
        OCR doc ten linh tai hang co checkbox o cb_y.
        Do checkbox che khuat 1-2 ky tu dau, tra ve partial text.
        Dung ket hop fuzzy_match_name de tim ten dung.
        """
        if not HAS_CV2 or screen is None: return ""
        try:
            import pytesseract
            from PIL import Image as PILImage
        except ImportError:
            return ""
        import re
        h, w = screen.shape[:2]
        # Name region: sau checkbox (~11%), truoc location text (~50%)
        nx0 = int(w * 0.11)
        nx1 = int(w * 0.50)
        ny0 = max(0, cb_y - 13)
        ny1 = min(h, cb_y + 13)
        crop = screen[ny0:ny1, nx0:nx1]
        if crop.size == 0: return ""
        # Upscale x5 + threshold
        big  = cv2.resize(crop, None, fx=5, fy=5,
                          interpolation=cv2.INTER_LANCZOS4)
        gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 155, 255, cv2.THRESH_BINARY)
        pil  = PILImage.fromarray(thr)
        try:
            raw = pytesseract.image_to_string(
                pil, lang="vie+eng", config="--psm 7 --oem 3")
        except Exception:
            try:
                raw = pytesseract.image_to_string(
                    pil, lang="eng", config="--psm 7 --oem 3")
            except Exception:
                return ""
        # Lay word dau tien, bo leading garbage chars
        raw  = raw.strip()
        parts = raw.split()
        word  = parts[0] if parts else ""
        word  = re.sub(r"^[lI|!\[\](){}\-]+", "", word)
        word  = re.sub(r"[^\w\u00c0-\u1ef9]", "", word)
        return word

    @staticmethod
    def fuzzy_match_name(ocr_word: str,
                         targets: List[str]) -> Optional[str]:
        """
        Khop partial OCR voi danh sach ten muc tieu.
        OCR mat 1-2 ky tu dau => dung suffix/substring match.
        Vi du: 'uto5' khop 'auto5', 'aid1' khop 'raid1'.
        """
        import re
        def norm(s: str) -> str:
            s = s.lower().strip()
            s = re.sub(r"[^a-z0-9\u00e0-\u1ef9]", "", s)
            return s
        got = norm(ocr_word)
        if len(got) < 2: return None
        best_name = None; best_score = 0.0
        for t in targets:
            tn = norm(t)
            if not tn: continue
            if tn == got: return t                         # exact
            if tn.endswith(got) and len(got) >= 2:         # suffix
                score = len(got) / len(tn)
                if score > best_score:
                    best_score = score; best_name = t
            if got in tn and len(got) >= 3:                # substring
                score = len(got) / len(tn)
                if score > best_score:
                    best_score = score; best_name = t
        return best_name if best_score >= 0.5 else None



    def read_march_time(self, screen, near_y: int = -1) -> str:
        """
        OCR doc dong 'TG hanh quan H:MM:SS'.
        near_y: y pixel cua checkbox vua click, scan vung phia duoi no.
        Neu near_y=-1 thi scan toan man hinh.
        Tra ve chuoi thoi gian, vi du '0:12:09', hoac '' neu khong doc duoc.
        """
        if not HAS_CV2 or screen is None: return ""
        try:
            import pytesseract
            from PIL import Image as PILImage
        except ImportError:
            return ""
        import re
        h, w = screen.shape[:2]
        if near_y > 0:
            # Scan vung tu checkbox den checkbox+200px
            y0 = min(near_y, h - 10)
            y1 = min(near_y + 200, h)
        else:
            # Scan toan man hinh
            y0, y1 = 0, h
        roi = screen[y0:y1, :]
        if roi.size == 0: return ""
        big  = cv2.resize(roi, None, fx=3, fy=3,
                          interpolation=cv2.INTER_LANCZOS4)
        gray = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
        pil = PILImage.fromarray(thr)
        try:
            raw = pytesseract.image_to_string(
                pil, lang="vie+eng", config="--psm 6 --oem 3")
        except Exception:
            try:
                raw = pytesseract.image_to_string(
                    pil, lang="eng", config="--psm 6 --oem 3")
            except Exception:
                return ""
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})", raw)
        return m.group(0) if m else ""


# ──────────────────────────────────────────────
#  TROOP SELECTOR
# ──────────────────────────────────────────────

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
#  BOT ENGINE
# ──────────────────────────────────────────────

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
                self.adb.tap(342, 1216)
            except Exception:
                pass
            return {'troops': [], 'force_heal': False, 'low_risk_only': False}

    def _run(self, points: List[AttackPoint]):
        self.log(f"[Bot] Bat dau {len(points)} diem (idx: {[p.idx for p in points]})")
        # Neu dang trong thanh thi thoat ra truoc
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
            ok = self._attack(pt)
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
                    self._setup_heal_point(pt)
                # Hoi mau sau khi cooldown xong: manual request hoac auto heal_every
                if self._heal_requested.is_set():
                    self.log("[Bot] 🩸 Heal thủ công theo yêu cầu → HOI MAU")
                    self._heal_requested.clear()
                    self._do_heal()
                elif ok and self.cfg.heal_every > 0 and attack_count % self.cfg.heal_every == 0:
                    self.log(f"[Bot] Đủ {self.cfg.heal_every} lần → HOI MAU")
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
                ok = self._attack(pt)
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
                        self._setup_heal_point(pt)
                    if self._heal_requested.is_set():
                        self.log("[Bot] 🩸 Heal thủ công theo yêu cầu → HOI MAU")
                        self._heal_requested.clear()
                        self._do_heal()
                    elif ok and self.cfg.heal_every > 0 and attack_count % self.cfg.heal_every == 0:
                        self.log(f"[Bot] Đủ {self.cfg.heal_every} lần → HOI MAU")
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
        time.sleep(0.7)
        sc_check = self.adb.screenshot_cv2()
        if self.det.has_error_popup(sc_check):
            dismiss = self.det.dismiss_error_popup(sc_check)
            self.log(f"[Bot] ⚠️ Popup lỗi 'liền kề' -> đóng, bỏ qua điểm này")
            if dismiss: self.adb.tap(*dismiss)
            time.sleep(0.5)
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

# ──────────────────────────────────────────────
#  THEME
# ──────────────────────────────────────────────

C = dict(
    bg     = "#12141f",
    panel  = "#1c1f30",
    card   = "#232640",
    border = "#2e3356",
    accent = "#6c8ef5",
    green  = "#4ade80",
    yellow = "#fbbf24",
    red    = "#f87171",
    text   = "#e2e8f0",
    muted  = "#4a5580",
    entry  = "#2a2d45",
)

STATE_COLOR = {
    State.IDLE:       "#4a5580",
    State.TAPPING:    "#fbbf24",
    State.WAIT_POPUP: "#fbbf24",
    State.SELECTING:  "#6c8ef5",
    State.WAIT_DONE:  "#6c8ef5",
    State.COOLDOWN:   "#fb923c",
    State.HEALING:    "#a78bfa",
    State.DONE:       "#4ade80",
}

GRID_COLS = 20
GRID_ROWS = 30


# ──────────────────────────────────────────────
#  GRID PICKER
# ──────────────────────────────────────────────

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


# ──────────────────────────────────────────────
#  MAIN GUI
# ──────────────────────────────────────────────



# ──────────────────────────────────────────────
#  BUILD ENGINE
# ──────────────────────────────────────────────

@dataclass
class BuildTask:
    name:       str
    tap_x:      int
    tap_y:      int
    enabled:    bool  = True
    build_done: float = 0.0   # timestamp khi het thoi gian xay

@dataclass
class BuildConfig:
    city_enter_x:   int   = 54
    city_enter_y:   int   = 1216
    city_exit_x:    int   = 54
    city_exit_y:    int   = 1216
    info_btn_x:     int   = 198
    info_btn_y:     int   = 192
    build_btn_x:    int   = 378
    build_btn_y:    int   = 1045
    max_builders:   int   = 2
    check_interval: int   = 60
    speed_pct:      float = 0.0   # % toc do xay (0 = khong co buff)
    delay_tap_house: float = 1.5  # delay sau khi tap vao nha
    delay_tap_info:  float = 1.5  # delay sau khi tap info
    delay_tap_build: float = 2.5  # delay sau khi tap build
    delay_back:      float = 1.2  # delay sau khi back ve thanh
    ollama_url:     str   = "http://localhost:11434"
    ollama_model:   str   = "qwen2-vl:7b"
    tasks: list = None

    def __post_init__(self):
        if self.tasks is None:
            self.tasks = [
                {"name": "Nha chinh", "tap_x": 342, "tap_y": 490, "enabled": True, "build_done": 0.0, "level": 1, "target_lv": 20},
                {"name": "Nha ren",   "tap_x": 234, "tap_y": 704, "enabled": True, "build_done": 0.0, "level": 1, "target_lv": 20},
            ]


class BuildEngine:
    def __init__(self, adb: "ADB", log: Callable, cfg: BuildConfig):
        self.adb   = adb
        self.log   = log
        self.cfg   = cfg
        self._stop = threading.Event()
        self._pause = threading.Event()  # True = tam dung (dang trong thanh)
        self._thread = None
        self.on_refresh: Optional[Callable] = None
        self.in_city: Optional[threading.Event] = None

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

    def _run(self):
        self.log("[Build] ▶ Bắt đầu tự động xây...")
        while not self._stop.is_set():
            enabled = [t for t in self.cfg.tasks if t.get("enabled", True)]
            if not enabled:
                self.log("[Build] ✅ Không có nhà nào được bật.")
                break

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

                self.log(f"[Build] 🏠 Xây {task['name']} (Lv{task.get('level',1)} → Lv{task.get('target_lv',20)})...")
                secs = self._try_build(task)

                if secs > 0:
                    # Ap dung buff toc do xay
                    if self.cfg.speed_pct > 0:
                        secs = int(secs * (1 - self.cfg.speed_pct / 100))
                        self.log(f"[Build] ⚡ Sau buff {self.cfg.speed_pct}%: {secs//3600:02d}:{(secs%3600)//60:02d}:{secs%60:02d}")
                    task["build_done"] = time.time() + secs
                    total_wait += secs
                    h_s2, r_s2 = divmod(secs, 3600); m_s2, s_s2 = divmod(r_s2, 60)
                    self.log(f"[Build] ⏱ {task['name']}: {h_s2:02d}:{m_s2:02d}:{s_s2:02d}")
                    builders += 1
                    lv = task.get("level", 1)
                    target = task.get("target_lv", 20)
                    if lv < target:
                        task["level"] = lv + 1
                    else:
                        task["enabled"] = False
                        self.log(f"[Build] 🏆 {task['name']} đạt Lv{lv} (mục tiêu Lv{target}), tắt xây → chuyển nhà tiếp.")
                else:
                    self.log(f"[Build] ⚠️ {task['name']}: không đọc được TG, bỏ qua.")

                if self.on_refresh: self.on_refresh()

                # Back ve man hinh thanh (luon back, ke ca nha cuoi, de exit_city dung)
                has_next = any(
                    enabled[j].get("enabled", True)
                    for j in range(idx + 1, len(enabled))
                    if builders < self.cfg.max_builders
                )
                self._tap(342, 1216, self.cfg.delay_back)
                if not has_next:
                    break

            # --- Buoc 5: Ra thanh ---
            self._exit_city()
            if self.on_refresh: self.on_refresh()

            if self._stop.is_set(): break

            # --- Cho het tat ca nha xay xong (tong thoi gian) ---
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




# ──────────────────────────────────────────────
#  WAVE ENGINE  – multi-group parallel attack
# ──────────────────────────────────────────────

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
        self._thread = None
        self.cur_idx = 0        # index trong group.points
        self.status  = "idle"   # idle | running | waiting | done | error
        self.on_refresh: Optional[Callable] = None
        self.on_captcha: Optional[Callable] = None  # callback khi phat hien CAPTCHA

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.status = "idle"

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
        self.adb.tap(cx, cy)
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
        self.adb.tap(486, 704); time.sleep(0.7)
        sc3 = self.adb.screenshot_cv2()
        if self.det.has_error_popup(sc3):
            dismiss = self.det.dismiss_error_popup(sc3)
            self.log("⚠️ Popup lỗi → đóng, bỏ qua")
            if dismiss: self.adb.tap(*dismiss)
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
            self.log("❌ Nhóm chưa có tên quân → dừng"); self.status = "error"; return
        main_troop = required[0].lower()

        # Kiểm tra quân Standby (giống tab Tấn công)
        if required:
            with _WAVE_ADB_LOCK:
                self.adb.tap(666, 1216); time.sleep(1.5)
                armies = self.adb.read_army_status(required_names=required)
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
                self.status = "error"; return

        idx = 0
        while not self._stop.is_set():
            if idx >= len(pts):
                self.log("✅ Hết điểm trong nhóm → dừng")
                self.status = "done"; break

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
                self.status = "waiting"
                if self.on_refresh: self.on_refresh()
                self.log(f"⏳ Hành quân {march_s}s...")
                deadline = time.time() + march_s
                while time.time() < deadline and not self._stop.is_set():
                    time.sleep(1)
                if self._stop.is_set(): break

                is_last = (idx >= len(pts) - 1)

                # Cooldown theo nhãn (giống tab tấn công) - bỏ qua điểm cuối
                if not is_last:
                    import re as _re
                    lbl = pt.get("label", "")
                    def _has_lv(n): return bool(_re.search(rf'lv\.?{n}\b', lbl, _re.IGNORECASE))
                    if _has_lv(4):
                        cd = self.cfg.lv4_cooldown_sec
                    elif _has_lv(3):
                        cd = self.cfg.lv3_cooldown_sec
                    elif _has_lv(2):
                        cd = self.cfg.lv2_cooldown_sec
                    else:
                        cd = self.cfg.cooldown_sec
                    if cd > 0:
                        tag = "lv4" if _has_lv(4) else "lv3" if _has_lv(3) else "lv2" if _has_lv(2) else None
                        if tag:
                            self.log(f"Nhãn '{lbl}' có {tag} → Cooldown {cd}s")
                        self.log(f"⏳ Cooldown {cd}s...")
                        cd_deadline = time.time() + cd
                        logged_cd = set()
                        while time.time() < cd_deadline and not self._stop.is_set():
                            left = int(cd_deadline - time.time())
                            mark = left - (left % 30)
                            if mark not in logged_cd:
                                logged_cd.add(mark)
                                self.log(f"Cooldown còn {left}s...")
                                # Check captcha moi 30s
                                if self._check_captcha(): break
                            time.sleep(1)
                        if self._stop.is_set(): break

                pt["status"] = "done"
                idx += 1
                if self.on_refresh: self.on_refresh()
                if is_last:
                    self.log("✅ Điểm cuối cùng hoàn thành → done")
            else:
                self.log(f"Điểm {idx+1} thất bại → thử lại sau 30s")
                self.status = "waiting"
                for _ in range(30):
                    if self._stop.is_set(): break
                    time.sleep(1)

        self.status = "idle"
        if self.on_refresh: self.on_refresh()


# ──────────────────────────────────────────────
#  TILE OCR HELPERS
# ──────────────────────────────────────────────

def _tile_extract_name(raw: str) -> str:
    import re
    patterns = [
        r'Lv\.?\s*\d+\s+[\w][\w\s]+(?:Plot|Mine|Camp|Fort|Tower|Mill|Farm)',
        r'Wasteland',
        r'Sawmill',
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    for line in raw.splitlines():
        line = line.strip()
        if len(line) < 3:
            continue
        if re.search(r'\(\d+,\d+\)', line):
            continue
        if len(re.findall(r'[a-zA-Z]', line)) >= 4:
            clean = re.sub(r'[^\w\s\.\-/+]', '', line).strip()
            if len(clean) >= 4:
                return clean
    return ''


def _tile_ocr_region(img, y1p, y2p, x1p=0.15, x2p=0.90, scale=3, psm=6):
    try:
        import cv2, pytesseract
    except ImportError:
        return ''
    h, w = img.shape[:2]
    crop = img[int(h * y1p):int(h * y2p), int(w * x1p):int(w * x2p)]
    if crop.shape[0] < 4 or crop.shape[1] < 4:
        return ''
    up = cv2.resize(crop, (crop.shape[1] * scale, crop.shape[0] * scale),
                    interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return pytesseract.image_to_string(th, config=f'--psm {psm}')


def _tile_ocr_buttons(img) -> str:
    try:
        import cv2
    except ImportError:
        return ''
    fh, fw = img.shape[:2]
    roi_y1 = int(fh * 0.25)
    roi_y2 = int(fh * 0.92)
    roi_x1 = int(fw * 0.20)
    roi = img[roi_y1:roi_y2, roi_x1:]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, white = cv2.threshold(gray, 220, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 4))
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, kernel)
    cnts, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    texts = []
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw * bh < 5000 or bw < 100 or bh < 30 or bw / max(bh, 1) < 1.2:
            continue
        crop = roi[max(0, by - 4):by + bh + 4, bx:bx + bw]
        t = _tile_ocr_region(crop, 0.0, 1.0, 0.0, 1.0, scale=3, psm=7)
        t = t.strip().replace('\n', ' ')
        if t:
            texts.append(t)
    return '\n'.join(texts)


def _classify_tile_from_text(text_title: str, text_btn: str = ''):
    import re
    text_all_low = (text_title + '\n' + text_btn).lower()
    tile_name = _tile_extract_name(text_title)
    if 'march' in text_all_low:
        return 'ally', tile_name
    if 'info' in text_all_low and 'conquer' in text_all_low:
        return 'enemy_city', tile_name
    if 'conquer' in text_all_low and 'enter' not in text_all_low:
        return 'enemy', tile_name
    terrain_kws = ['lake', 'hills', 'mountains', 'bridge', 'river']
    if any(k in text_all_low for k in terrain_kws):
        return 'terrain', tile_name
    if 'wasteland' in text_title.lower():
        return 'lv1', tile_name
    for lv in [5, 4, 3, 2]:
        if re.search(rf'lv\.?{lv}\b', text_title, re.IGNORECASE):
            return f'lv{lv}', tile_name
    for lv in [5, 4, 3, 2]:
        if re.search(rf'lv\.?{lv}\b', text_btn, re.IGNORECASE):
            return f'lv{lv}', tile_name
    return '?', tile_name


def analyze_tile_image(img):
    text_title = _tile_ocr_region(img, 0.25, 0.62)
    text_btn = _tile_ocr_buttons(img)
    label, tile_name = _classify_tile_from_text(text_title, text_btn)
    return {
        'label': label,
        'name': tile_name,
        'raw_title': text_title.strip(),
        'raw_btn': text_btn.strip(),
    }


# ─── Scout Engine ─────────────────────────────────────────────────────────────
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


class GUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Nine Acres  —  Grid Attack Bot")
        self.configure(bg=C["bg"])
        self.geometry("920x740")
        self.minsize(720, 600)

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
        self.build_eng = BuildEngine(self.bot.adb, self._log, self.build_cfg)
        self.build_eng.in_city = self._in_city
        self.bot.in_city       = self._in_city
        self._raw: Optional[bytes] = None
        self._adb_w = self.cfg.screen_w
        self._adb_h = self.cfg.screen_h
        self.wave_groups: List[WaveGroup] = []
        self._wave_engines: List[WaveGroupEngine] = []

        self.bot.on_state = self._on_state
        self.bot.on_captcha = self._stop_all_captcha
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
        # Load scout config + map sau khi UI đã sẵn sàng
        self.after(200, self._startup_load_scout)

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
        self.destroy()

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
        d["_wave_groups"] = [
            {"name": g.name, "troop_names": g.troop_names,
             "points": g.points, "enabled": g.enabled}
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
        cols = ("#", "Game X", "Game Y", "Nhan", "Status")
        tv = ttk.Treeview(rf, columns=cols, show="headings", height=8, selectmode="browse")
        for col, cw in zip(cols, (30, 65, 65, 120, 70)):
            tv.heading(col, text=col); tv.column(col, width=cw, anchor="center")
        tv.tag_configure("s_wait",    foreground=C["muted"])
        tv.tag_configure("s_going",   foreground=C["yellow"])
        tv.tag_configure("s_process", foreground=C["accent"])
        tv.tag_configure("s_done",    foreground=C["green"])
        tv.tag_configure("s_current", foreground=C["yellow"], background=C["card"])
        tv.pack(fill="both", expand=True, padx=4, pady=2)
        w["pt_tree"] = tv

        # Tree controls
        tc = tk.Frame(rf, bg=C["panel"]); tc.pack(pady=(0,4))
        for text, cmd, color in [
            ("⬆ Lên",    lambda: self._wave_move_point(g, w, -1),  C["muted"]),
            ("⬇ Xuống",  lambda: self._wave_move_point(g, w,  1),  C["muted"]),
            ("✏ Sửa",    lambda: self._wave_edit_point(g, w),       C["accent"]),
            ("🗑 Xóa",   lambda: self._wave_del_point(g, w),        C["red"]),
            ("Xóa hết",  lambda: self._wave_clear_points(g, w),     "#333355"),
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
                              pt.get("label",""), st))
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
        self._wave_engines.append(eng)
        eng.start()
        self._wave_log(f"[Wave] ▶ Bắt đầu nhóm '{g.name}'")

    def _wave_stop_group(self, g: WaveGroup):
        for eng in self._wave_engines:
            if eng.group is g:
                eng.stop()
                self._wave_log(f"[Wave] ⏹ Dừng nhóm '{g.name}'")

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
        tk.Button(tb2, text="🗑 Xóa tất cả", bg=C["card"], fg=C["muted"],
                  relief="flat", font=("Segoe UI", 8), padx=6, pady=3,
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
        ROW2      = [(162,490),(270,490),(378,490),(486,490),(594,490)]

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
                    time.sleep(3); continue
            if _check_stop(): break
            self._alv_log("Da trong thanh")

            # 2. Mo menu Training
            self._alv_log("Tap (414, 704)")
            _tap(414, 704, 1.5)
            if _check_stop(): break
            self._alv_log("Tap (522, 234)")
            _tap(522, 234, 1.5)
            if _check_stop(): break

            # 2.5. Scroll ngang sang phai truoc Row 1
            w, h = adb.get_screen_size()
            self._alv_log(f"Scroll ngang sang phai {w}px")
            adb.swipe(int(w*0.85), 490, int(w*0.10), 490, ms=1500)
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

            # 4. Scroll ngang 100% chieu rong
            w, h = adb.get_screen_size()
            self._alv_log(f"Scroll ngang {w}px")
            adb.swipe(int(w*0.85), 490, int(w*0.10), 490, ms=1500)
            time.sleep(1.0)
            time.sleep(1.0)
            if _check_stop(): break

            # 5. Row 2: 5 nut
            for x, y in ROW2:
                if _check_stop(): break
                self._alv_log(f"Tap ({x},{y}) → confirm {CONFIRM}")
                _tap(x, y, delay)
                _tap(CONFIRM[0], CONFIRM[1], c_delay)

            if _check_stop(): break

            # 6. OCR lay thoi gian hoan thanh
            wait_secs = 0
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
                    # Tim pattern: H:MM:SS hoac D:HH:MM:SS
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

            # 8. Cho den khi het thoi gian
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
                    # Check captcha moi 30s
                    if rem % 30 == 0:
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

        # Tasks section
        st = self._sec(p, "Danh sách nhà")
        cols = ("#", "Tên nhà", "Lv", "Mục tiêu", "Tap X", "Tap Y", "Bật", "Hết xây")
        self.build_tree = ttk.Treeview(st, columns=cols, show="headings", height=6)
        widths = [30, 110, 45, 55, 55, 55, 35, 130]
        for c, w in zip(cols, widths):
            self.build_tree.heading(c, text=c)
            self.build_tree.column(c, width=w, anchor="center")
        self.build_tree.pack(fill="x", padx=4, pady=4)
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
            ))

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
        self.build_eng.adb = self.bot.adb
        self.build_eng.on_refresh = self._schedule_build_refresh
        self.build_eng.start()

    def _stop_build(self):
        self.build_eng.stop()
        self._log("[Build] ⏹ Đã gửi lệnh dừng → chờ bước hiện tại hoàn thành...")

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

        # ADB
        s = self._sec(p, "ADB Connection")
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
        s2 = self._sec(p, "Cài đặt bot")
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
        s_ol = self._sec(p, "AI Vision (Ollama)")
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

        s_heal = self._sec(p, "Auto Heal")
        self.e_heal_every = self._erow(s_heal, "Tấn công/lần:", self.cfg.heal_every, 5)
        self.e_heal_step  = self._erow(s_heal, "Step đặt điểm:", self.cfg.heal_step, 5)
        self.e_heal_x     = self._erow(s_heal, "Heal X:",       self.cfg.heal_x,     6)
        self.e_heal_y     = self._erow(s_heal, "Heal Y:",       self.cfg.heal_y,     6)
        tk.Label(s_heal,
                 text="(Sau N lan tan cong bot tu dong\nnhap toa do nay va chon quan hoi mau)\n"
                      "Step: cu X diem thi tu dong dat diem\nhoi mau = diem vua chiem (0=tat)",
                 bg=C["panel"], fg=C["muted"],
                 font=("Segoe UI", 7), justify="left").pack(anchor="w", padx=4)
        tk.Button(s_heal, text="🩸 Hồi máu ngay",
                  bg="#8b1a1a", fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=10, pady=4,
                  command=self._manual_heal).pack(pady=(6,2), fill="x", padx=4)
        tk.Button(s_heal, text="⚔️ Check icon ATK",
                  bg=C["card"], fg=C["text"], relief="flat",
                  font=("Segoe UI", 9), padx=10, pady=3,
                  command=self._check_atk_icon).pack(pady=(2,4), fill="x", padx=4)

        s_brain = self._sec(p, "Army Health Brain")
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

        # State machine
        s3 = self._sec(p, "State Machine")
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
        s4 = self._sec(p, "Tiến độ")
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
        cols = ("#", "Game X", "Game Y", "Nhan", "Status")
        self.tree = ttk.Treeview(
            s2, columns=cols, show="headings",
            height=5, selectmode="browse")
        for c, w in zip(cols, [30, 65, 65, 120, 70]):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="x", padx=6, pady=(2,2))

        # Tree controls
        tc = tk.Frame(s2, bg=C["panel"]); tc.pack(pady=(0,4))
        for text, cmd, color in [
            ("⬆ Lên",      self._move_up,     C["muted"]),
            ("⬇ Xuống",   self._move_down,   C["muted"]),
            ("✏ Sửa",      self._edit_coord,  C["accent"]),
            ("🗑 Xóa",     self._del_coord,   C["red"]),
            ("Xóa hết",   self._clear_pts,    "#333355"),
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
            ("🤖 Test AI",           self._test_ai,    C["accent"],  9),
            ("🔍 Test OCR",          self._test_ocr,   "#557755",    9),
            ("🏙 Test City",         self._test_city,  "#445566",    9),
            ("🪖 Check quân",        self._check_army, "#556688",    9),
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
        dlg.title(f"Sửa diem {pt.idx}")
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
                pt.idx, pt.game_x, pt.game_y, pt.label, pt.status))
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
                              points=g.get("points",[]), enabled=g.get("enabled",True))
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

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        def a():
            line = f"[{ts}] {msg}\n"
            self.txt.configure(state="normal")
            self.txt.insert("end", line)
            self.txt.see("end")
            self.txt.configure(state="disabled")
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
    if not HAS_PIL: print("WARN: pip install pillow")
    GUI().mainloop()

