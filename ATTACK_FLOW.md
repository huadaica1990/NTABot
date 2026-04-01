# Luồng hoạt động Tab Tấn công (Attack Flow)

> **Phiên bản**: 2.1 — Modular (updated)  
> **Modules liên quan**: `gui.py`, `bot_engine.py`, `adb_controller.py`, `detector.py`, `troop_selector.py`, `brain.py`, `tile_ocr.py`, `ollama_vision.py`  
> **Entry point**: `GUI._run()` → `BotEngine.start(points)` → `BotEngine._run(points)`

---

## Mục lục

1. [Tổng quan luồng chính](#1-tổng-quan-luồng-chính)
2. [Khởi tạo & Validate (GUI)](#2-khởi-tạo--validate-gui)
3. [Pre-checks (BotEngine)](#3-pre-checks-botengine)
4. [Vòng lặp tấn công](#4-vòng-lặp-tấn-công)
   - 4.0 [Brain: Quét máu quân](#40-brain-quét-máu-quân)
   - 4.1 [Navigate đến toạ độ](#41-navigate-đến-toạ-độ)
   - 4.2 [Chờ popup Chiếm](#42-chờ-popup-chiếm)
   - 4.3 [Click nút Chiếm](#43-click-nút-chiếm)
   - 4.4 [Chọn quân](#44-chọn-quân)
   - 4.5 [Chờ hành quân](#45-chờ-hành-quân)
5. [Giữa hai điểm: Cooldown → Heal](#5-giữa-hai-điểm-cooldown--heal)
   - 5.1 [Xác định cooldown theo level](#51-xác-định-cooldown-theo-level)
   - 5.2 [Vòng lặp cooldown](#52-vòng-lặp-cooldown)
   - 5.3 [Chờ Build ra thành](#53-chờ-build-ra-thành)
   - 5.4 [Heal step — đặt điểm hồi máu](#54-heal-step--đặt-điểm-hồi-máu)
   - 5.5 [Quyết định hồi máu](#55-quyết-định-hồi-máu)
   - 5.6 [_do_heal() — thực hiện hồi máu](#56-_do_heal--thực-hiện-hồi-máu)
6. [Re-check điểm mới](#6-re-check-điểm-mới)
7. [Kết thúc](#7-kết-thúc)
8. [State Machine](#8-state-machine)
9. [Safety Gates](#9-safety-gates)
10. [Module Dependency Map](#10-module-dependency-map)
11. [Toạ độ cố định (hardcoded)](#11-toạ-độ-cố-định-hardcoded)
12. [Config ảnh hưởng đến luồng](#12-config-ảnh-hưởng-đến-luồng)

---

## 1. Tổng quan luồng chính

```
User bấm ▶ RUN
  │
  ├─ Validate (GUI)
  │    ├─ Có điểm nào không?
  │    ├─ ADB đã kết nối?
  │    └─ Có điểm nào ở trạng thái "waiting"?
  │
  ├─ BotEngine.start(points)  →  Thread mới
  │    │
  │    ├─ Pre-checks
  │    │    ├─ Thoát thành nếu đang trong thành
  │    │    └─ Kiểm tra quân chính có tồn tại không
  │    │
  │    ├─ FOR mỗi điểm (waiting):
  │    │    ├─ Check CAPTCHA
  │    │    ├─ _attack(pt)
  │    │    │    ├─ Phase 0: Brain scan HP
  │    │    │    ├─ Phase 1: Navigate đến toạ độ
  │    │    │    ├─ Phase 2: Chờ popup Chiếm
  │    │    │    ├─ Phase 3: Click Chiếm
  │    │    │    ├─ Phase 4: Chọn quân (OCR + checkbox)
  │    │    │    └─ Phase 5: Chờ hành quân
  │    │    │
  │    │    ├─ OK?
  │    │    │    ├─ Cooldown (theo level nhãn)
  │    │    │    ├─ Heal step? → đặt điểm hồi máu
  │    │    │    └─ Heal? (manual / auto / skip)
  │    │    │
  │    │    └─ FAIL? → Beep + STOP bot
  │    │
  │    ├─ Re-check: user thêm điểm mới? → lặp lại
  │    │
  │    └─ State = DONE, beep 3 lần
```

---

## 2. Khởi tạo & Validate (GUI)

**File**: `gui.py` — method `GUI._run()` (line ~4750)

Khi user bấm nút **▶ RUN tất cả điểm**, GUI thực hiện 3 bước validate:

| # | Kiểm tra | Điều kiện | Nếu fail |
|---|----------|-----------|----------|
| 1 | Có điểm? | `self.pts` không rỗng | MessageBox "Chưa chọn điểm nào!" |
| 2 | ADB OK? | `self.bot.adb.ok == True` | MessageBox "Kết nối ADB trước!" |
| 3 | Có waiting? | Ít nhất 1 điểm có `status == "waiting"` | MessageBox "Không có điểm waiting!" |

Sau validate, GUI lọc ra danh sách `to_run` (chỉ các điểm waiting) và gọi:

```python
self.bot.start(to_run)
```

**BotEngine.start()** (`bot_engine.py` line 81) tạo thread daemon mới:

```python
def start(self, points):
    if self._thread and self._thread.is_alive(): return  # đang chạy rồi
    self._stop.clear()
    self._skip_cd.clear()
    self._thread = threading.Thread(target=self._run, args=(points,), daemon=True)
    self._thread.start()
```

> **Lưu ý**: Thread daemon — tự tắt khi cửa sổ chính đóng.

---

## 3. Pre-checks (BotEngine)

**File**: `bot_engine.py` — method `BotEngine._run()` (line 154)

### 3.1. Thoát thành

```python
if self.adb.is_in_city():
    self.adb.tap(54, 1216)   # nút thoát thành
    time.sleep(1.5)
```

**is_in_city()** (`adb_controller.py` line 328):
- Chụp screenshot
- Crop vùng 52×46 pixel quanh toạ độ (54, 1216)
- Chuyển sang HSV, đếm pixel xanh lá (H=35-85, S>80, V>80)
- Nếu > 5% pixel xanh lá → đang trong thành

### 3.2. Kiểm tra quân

**_check_required_armies()** (`bot_engine.py` line 93):

```
1. Tap (666, 1216) → mở màn hình All Armies
2. Chờ 1.5s
3. adb.read_army_status(required_names) → OCR scroll qua danh sách
4. Tap (342, 1216) → đóng
5. Chờ 0.8s
```

**Bước kiểm tra**:

| # | Kiểm tra | Hành động nếu fail |
|---|----------|--------------------|
| 1 | Có quân nào Standby? | Chỉ log cảnh báo, KHÔNG dừng |
| 2 | Tìm thấy quân chính (tên đầu tiên trong `troop_names`)? | Beep 5 lần + `_stop.set()` + return False |

**read_army_status()** (`adb_controller.py` line 353):
- Scroll tối đa 8 lần qua danh sách quân
- Mỗi lần scroll: chụp ảnh → scale 2x → grayscale → threshold → pytesseract OCR
- Parse mỗi dòng: tách tên quân + trạng thái (Battling/Standby/Healing/Marching/...)
- Dừng sớm khi không tìm thấy gì mới VÀ đã thấy quân chính

---

## 4. Vòng lặp tấn công

**File**: `bot_engine.py` — method `BotEngine._run()` (line 165-309)

Với mỗi điểm trong danh sách `points`:

```python
for i, pt in enumerate(points):
    if self._stop.is_set(): break
    if self._check_captcha(): break   # Safety gate
    self.cur_pt = pt
    pt.status = "going"
    ok = self._attack(pt)
    ...
```

### 4.0. Brain: Quét máu quân

**File**: `bot_engine.py` line 430-441, `brain.py`  
**Điều kiện**: `cfg.army_health_enabled == True`

```
_scan_army_health():
  1. Tap (666, 1216) → mở All Armies
  2. adb.read_army_health() → OCR thanh máu đỏ
  3. Tap (342, 1216) → đóng
  4. brain.summarize() → phân tích + quyết định
```

**read_army_health()** (`adb_controller.py` line 440):
- Chụp ảnh → chuyển HSV
- Filter thanh máu đỏ: `H(0-20 ∪ 155-180), S>45, V>65`
- Morphology open/close để làm sạch
- findContours → group theo hàng Y (lệch ≤38px = cùng hàng)
- Tính ratio chiều rộng: `ratio = width / max_width_in_row`
- OCR tên quân gần mỗi hàng bar

**TroopBrain.analyze()** (`brain.py` line 17) — cho mỗi đạo quân:

```
risk = (1 - avg_hp) × 45
     + (critical_units / unit_count) × 30
     + max(0, -hp_trend) × 35
     + lv_penalty (lv4=18, lv3=10, lv2=4)
```

**Quyết định**:

| Điều kiện | Action |
|-----------|--------|
| `avg_hp ≤ 0.30` HOẶC `critical_units ≥ 2` | `heal_now` |
| `min_hp ≤ 0.25` VÀ target là lv3/lv4 | `heal_now` |
| `avg_hp ≤ 0.55` | `low_risk_only` |
| Còn lại | `continue` |

**TroopBrain.summarize()** tổng hợp tất cả quân:
- `force_heal = True` nếu bất kỳ quân nào `heal_now`
- `low_risk_only = True` nếu không force_heal nhưng có quân `low_risk_only`

**Xử lý kết quả trong _attack()**:

| Kết quả Brain | Hành động |
|---------------|-----------|
| `force_heal` | Gọi `_do_heal()` ngay trước khi tấn công |
| `low_risk_only` VÀ target là lv3/lv4 | Gọi `_do_heal()` ngay |
| Khác | Tiếp tục bình thường |

---

### 4.1. Navigate đến toạ độ

**File**: `bot_engine.py` — method `navigate_to()` (line 313-428)  
**State**: `TAPPING`

Chuỗi thao tác ADB chi tiết:

```
 Bước │ Thao tác                          │ Toạ độ / Giá trị      │ Delay
──────┼────────────────────────────────────┼───────────────────────┼──────
  1   │ Tap nút bản đồ                    │ (nav_btn_x, nav_btn_y)│ 1.0s
      │                                    │ mặc định: (558, 1216) │
  2   │ Nhập X vào ô input                │ (x_field_x, x_field_y)│
      │  - Tap focus                       │ mặc định: (378, 1002) │ 0.35s
      │  - KEYCODE_MOVE_END                │                       │ 0.1s
      │  - KEYCODE_DEL × 12               │                       │ 0.15s
      │  - Gõ từng digit (KEYCODE_0-9)    │                       │ 0.06s/digit
  3   │ Tắt keyboard                       │ KEYCODE_BACK          │ 0.35s
  4   │ Tap ô Y 2 lần (đảm bảo focus)     │ (y_field_x, y_field_y)│ 0.35s × 2
      │                                    │ mặc định: (486, 1002) │
  5   │ Nhập Y (giống bước 2)             │                       │
  6   │ Tắt keyboard bằng tap bản đồ      │ (giữa, 35% chiều cao) │ 0.6s
  7   │ Tap nút Xem                        │ (xem_x, xem_y)       │ 0.4s
      │                                    │ mặc định: (630, 1002) │
  8   │ Chờ bản đồ di chuyển              │                       │ nav_wait (1.5s)
```

**OCR trước tap giữa** (line 371-397):
```
  9   │ Chụp ảnh → scale 2x → gray → threshold → pytesseract
      │ Tìm keywords: ["Wasteland", "Lv.2", "Lv.3", "Lv.4", "Lv.5"]
      │ Nếu tìm thấy → _tap_twice = True (cần tap 2 lần vì overlay Wasteland)
```

**Tap giữa màn hình** (line 399-410):
```
  10  │ force_tap = 1: tap 1 lần (cx, cy)
      │ force_tap = 2: tap (342, 1216) trước rồi tap (cx, cy)
      │ force_tap = 0: tap 1 lần, nếu OCR thấy keyword → tap thêm lần 2
```

**OCR sau tap** (line 412-428) — `tile_ocr.analyze_tile_image()`:
```
  11  │ Chụp ảnh → OCR vùng title (25%-62% chiều cao)
      │ OCR vùng buttons (tìm nút trắng, OCR text bên trong)
      │ _classify_tile_from_text():
      │   - "march" → ally (ô đồng minh)
      │   - "conquer" + no "enter" → enemy
      │   - "wasteland" → lv1
      │   - regex "lv.?N" → lv2/lv3/lv4/lv5
      │ Nếu nhận ra lvN → ghi self._detected_label
```

**Cập nhật nhãn** (back in `_attack()` line 449-453):
```python
if self._detected_label:
    pt.label = self._detected_label  # OCR tự cập nhật → cooldown sẽ chính xác hơn
```

---

### 4.2. Chờ popup Chiếm

**File**: `bot_engine.py` line 456-467  
**State**: `WAIT_POPUP`

```python
def _wait_capture(self):
    dead = time.time() + self.cfg.popup_timeout  # mặc định 10s
    while time.time() < dead:
        if self._stop.is_set(): return None
        pos = self.det.find_capture_btn(self.adb.screenshot_cv2())
        if pos: return pos
        time.sleep(0.5)
    return None
```

**Detector.find_capture_btn()** (`detector.py` line 968-1028):

Tìm nút Chiếm/Conquer theo 3 phương pháp (fallback):

| Thứ tự | Phương pháp | Chi tiết |
|--------|-------------|----------|
| 1 | Template matching | `btn_capture.png` trong assets/, threshold giảm dần 0.85 → 0.75 → 0.65 |
| 2 | Color: icon X đỏ | BGR filter `[0,30,160]–[80,110,255]`, tìm contour lớn nhất ở nửa phải |
| 3 | Color: nút trắng | BGR filter `[210,210,195]–[255,255,240]`, dilate, tìm rect ngang (w>h×1.5) |

**Retry nếu không tìm thấy** (line 460-467):
```python
if not cap:
    self.adb.tap(w//2 + 30, h//2)   # tap lệch phải
    time.sleep(0.8)
    cap = self._wait_capture()       # thử lần 2
    if not cap:
        self.adb.tap(50, 200)        # đóng popup rác
        return False
```

---

### 4.3. Click nút Chiếm

**File**: `bot_engine.py` line 469-498  
**Toạ độ cố định**: `(486, 704)`

```python
self.adb.tap(486, 704)
time.sleep(1.0)

# Kiểm tra popup army_selection đã hiện chưa
ptype = self.det.detect_popup_type(screen)
if ptype != "army_selection":
    # Chờ tối đa 30s, kiểm tra mỗi 1s
    # Nếu vẫn không thấy → Click Chiếm lại 1 lần
    # Nếu vẫn fail → return False (bỏ qua điểm)
```

**Luồng chi tiết:**
1. Tap (486, 704) → chờ 1s
2. `detect_popup_type()` → nếu `army_selection` → sang bước 4.4
3. Nếu chưa → chờ tối đa 30s, poll mỗi 1s
4. Nếu 30s hết vẫn chưa → Click Chiếm lại (486, 704), chờ 1.5s
5. Lặp lại bước 2-4, tối đa **3 lần**
6. Sau 3 lần vẫn không phải `army_selection` → `return False`

---

### 4.4. Chọn quân

**File**: `troop_selector.py` — method `TroopSelector.select()` (line 27-60)  
**State**: `SELECTING`

#### 4.4.1. Chờ màn hình chọn quân

```python
def _wait_troop_screen(self):
    dead = time.time() + self.cfg.troop_timeout  # mặc định 8s
    while time.time() < dead:
        if self.det.is_troop_screen(self.adb.screenshot_cv2()):
            return True
        time.sleep(0.5)
    return False
```

`is_troop_screen()`: Kiểm tra nút OK có tồn tại và nằm ở nửa dưới màn hình.

#### 4.4.2. Vòng lặp chọn quân — _do_select()

**File**: `troop_selector.py` line 62-184

Mỗi scan (tối đa 8 lần):

```
  Bước │ Thao tác
───────┼──────────────────────────────────────────────────
  1    │ Chụp screenshot
  2    │ detect_checkboxes(screen) → danh sách Y positions
  3    │ Với mỗi checkbox tại y:
       │   a. OCR read_troop_name(screen, cb_x, y)
       │      - Crop vùng 11%-50% ngang, ±13px quanh y
       │      - Scale 5x + threshold (155) + pytesseract --psm 7
       │      - Strip garbage chars đầu dòng
       │   b. fuzzy_match_name(ocr_text, troop_names)
       │      - exact match → 100%
       │      - suffix match (≥2 chars) → score = len/len
       │      - substring match (≥3 chars) → score = len/len
       │      - Threshold: score ≥ 0.5
       │   c. Nếu khớp + chưa chọn + chưa check:
       │      → tap checkbox, ghi nhận chosen[name] = True
       │   d. Đọc TG hành quân (chỉ lần đầu sau click):
       │      - OCR vùng checkbox+200px
       │      - Regex: H:MM:SS hoặc MM:SS
       │      - Parse thành giây → last_march_seconds
  4    │ Nếu chưa đủ max_troops → scroll xuống (swipe 56%→44%)
  5    │ Nếu OCR fail liên tục 2+ lần → tắt OCR, fallback positional
```

**detect_checkboxes()** (`detector.py` line 1204-1236):
- Scan cột 8%-12% chiều ngang (vùng checkbox)
- Color filter BGR `[100,125,148]–[148,172,198]` (màu nâu checkbox)
- Projection theo trục Y → tìm peaks (run-length)
- Dedup peaks lệch <35px

**is_checked()** (`detector.py` line 1242-1257):
- Crop 14×14 pixel quanh checkbox
- Color filter xanh lá: G cao, R+B thấp
- Nếu ≥4 pixel xanh → đã check

#### 4.4.3. Tap OK + ghi thời gian

```python
ok = self.det.find_ok_btn(screen)  # → (342, 1045) cố định
self.adb.tap(*ok)
self.march_start_time = time.time()   # mốc tính TG hành quân
```

#### 4.4.4. Xử lý lỗi tên quân

Nếu tất cả key đã chọn đều là fallback positional (`_pXsX`), không key nào là tên thật:
```python
self._no_match_alert = True
→ TroopSelector.select() return False
→ _attack() return False
→ BOT DỪNG
```

---

### 4.5. Chờ hành quân

**File**: `bot_engine.py` line 491-504  
**State**: `WAIT_DONE`

```python
march_secs  = self.sel.last_march_seconds   # từ OCR
march_start = self.sel.march_start_time     # time.time() lúc tap OK

if march_secs > 0 and march_start > 0:
    march_deadline = march_start + march_secs
else:
    march_deadline = 0

# Sleep đến deadline
while time.time() < march_deadline:
    if self._stop.is_set(): return
    time.sleep(1.0)
```

Sau khi hết TG hành quân:
```python
pt.status = "process"   # quân đã đến nơi
```

---

## 5. Giữa hai điểm: Cooldown → Heal

**File**: `bot_engine.py` line 194-295

Flow thực hiện tuần tự sau khi `_attack()` return True:

```
_attack() OK
  │
  ├─ Điểm cuối cùng? → KHÔNG cooldown, nhảy đến Re-check
  │
  ├─ 5.1 Xác định cooldown theo level nhãn
  ├─ 5.2 Vòng lặp cooldown (sleep + check CAPTCHA)
  ├─ 5.3 Chờ Build ra thành (nếu cần)
  ├─ 5.4 Heal step? → đặt điểm hồi máu
  └─ 5.5 Quyết định heal:
       ├─ Manual heal (user bấm "Hồi máu ngay")? → _do_heal()
       ├─ Auto heal (count % heal_every == 0)? → _do_heal()
       └─ Không heal → điểm tiếp theo
```

### 5.1. Xác định cooldown theo level

```python
import re as _re
lbl = pt.label   # nhãn điểm, VD: "lv3", "Lv.4 Stone Plot", "Wasteland"

def _has_lv(n):
    return bool(_re.search(rf'lv\.?{n}\b', lbl, _re.IGNORECASE))

if   _has_lv(4): cd = cfg.lv4_cooldown_sec   # mặc định 1200s (20 phút)
elif _has_lv(3): cd = cfg.lv3_cooldown_sec   # mặc định 900s  (15 phút)
elif _has_lv(2): cd = cfg.lv2_cooldown_sec   # mặc định 600s  (10 phút)
else:            cd = cfg.cooldown_sec       # mặc định 300s  (5 phút)
```

> **Lưu ý**: Nhãn được tự động cập nhật từ OCR ở bước 4.1, nên kể cả user nhập sai nhãn ban đầu, bot vẫn chọn đúng cooldown sau khi navigate đến.

### 5.2. Vòng lặp cooldown

**File**: `bot_engine.py` line 524-549

```python
def _cooldown(self, secs):
    self._set(State.COOLDOWN, f"{secs}s")
    self._skip_cd.clear()
    dead = time.time() + secs
    logged = set()

    while time.time() < dead:
        # User bấm STOP?
        if self._stop.is_set(): return

        # User bấm ⏭️ Điểm tiếp theo?
        if self._skip_cd.is_set():
            self._skip_cd.clear()
            return

        # Log mỗi 30s + check CAPTCHA
        left = int(dead - time.time())
        mark = left - (left % 30)
        if mark not in logged:
            logged.add(mark)
            self.log(f"Cooldown còn {left}s...")
            if self._check_captcha(): return

        time.sleep(1.0)
```

### 5.3. Chờ Build ra thành

```python
if self.in_city and self.in_city.is_set():
    # BuildEngine đang xây trong thành → chờ ra
    while self.in_city.is_set():
        if self._stop.is_set(): return
        time.sleep(1)
```

> `in_city` là `threading.Event()` được share giữa `BotEngine` và `BuildEngine`. Khi Build vào thành → set(), ra thành → clear().

### 5.4. Heal step — đặt điểm hồi máu

**Điều kiện**: `cfg.heal_step > 0` VÀ `attack_count % heal_step == 0`

**_setup_heal_point(pt)** (`bot_engine.py` line 551-631):

```
 Bước │ Thao tác                          │ Toạ độ              │ Delay
──────┼────────────────────────────────────┼─────────────────────┼──────
  1   │ Tap nút bản đồ                    │ (nav_btn_x, nav_btn_y)│ 1.0s
  2   │ Nhập X = pt.game_x               │ (x_field_x, x_field_y)│
  3   │ Nhập Y = pt.game_y               │ (y_field_x, y_field_y)│
  4   │ Tắt keyboard (tap bản đồ)        │ (giữa, 35%)         │ 0.6s
  5   │ Tap Xem                            │ (xem_x, xem_y)     │ nav_wait
  6   │ Tap giữa (chọn điểm)              │ (cx, cy)            │ 1.0s
  7   │ Tap nút Xây                        │ (488, 746)          │ 1.0s
  8   │ Tap xác nhận                       │ (558, 490)          │ 0.8s
  9   │ Cập nhật cfg.heal_x/y             │                     │
```

### 5.5. Quyết định hồi máu

3 nhánh quyết định, theo thứ tự ưu tiên:

| Ưu tiên | Điều kiện | Trigger |
|---------|-----------|---------|
| 1 | `_heal_requested.is_set()` | User bấm "🩸 Hồi màu ngay" trên GUI |
| 2 | `cfg.heal_every > 0` VÀ `attack_count % heal_every == 0` | Auto heal mỗi N lần |
| 3 | Không thoả mãn 1 hoặc 2 | Bỏ qua, sang điểm tiếp |

### 5.6. _do_heal() — thực hiện hồi máu

**File**: `bot_engine.py` line 627-669  
**State**: `HEALING`

```
 Bước │ Thao tác                          │ Chi tiết
──────┼────────────────────────────────────┼────────────────────────
  1   │ navigate_to(heal_x, heal_y)       │ Giống bước 4.1 (đã tap giữa)
  2   │ Chờ 1s                             │ Chờ popup sẵn sàng
  3   │ Tap nút March                      │ (486, 746) cố định
  4   │ Chờ 0.5s                           │
  5   │ TroopSelector.select()            │ Giống bước 4.4 (OCR + checkbox)
  6   │ Chờ TG hành quân                   │ march_deadline = start + secs
  7   │ Log "Quân đã đến → tiếp tục"     │
```

> Sau heal, bot tiếp tục vòng lặp tấn công với điểm tiếp theo.

---

## 6. Re-check điểm mới

**File**: `bot_engine.py` line 224-295

Sau khi hết danh sách points ban đầu, bot kiểm tra xem user có thêm điểm mới không:

```python
while not self._stop.is_set() and self.get_pending_points:
    new_pts = self.get_pending_points()   # callback từ GUI
    new_waiting = [p for p in new_pts
                   if id(p) not in already_done
                   and p.status == "waiting"]
    if not new_waiting:
        break
    # Lặp lại toàn bộ vòng lặp tấn công cho điểm mới
    points = new_waiting
    for i, pt in enumerate(points):
        ...  # giống hệt vòng lặp chính
```

> `get_pending_points` là callback được GUI gán: `lambda: [p for p in self.pts if p.status == "waiting"]`

---

## 7. Kết thúc

```python
self._set(State.DONE)

# Đánh dấu điểm cuối cùng done
if self.cur_pt and self.cur_pt.status == "process":
    self.cur_pt.status = "done"
    if self.on_refresh: self.on_refresh()

self.log("[Bot] Hoan thanh tat ca diem!")

# Beep hoàn thành
import winsound
for _ in range(3):
    winsound.Beep(1000, 300)
    time.sleep(0.15)
```

---

## 8. State Machine

Bot có 8 trạng thái, chuyển đổi tuần tự:

```
IDLE → TAPPING → WAIT_POPUP → SELECTING → WAIT_DONE → COOLDOWN ─┐
  ↑                                                                │
  │         ┌──────────────────────────────────────────────────────┘
  │         ↓
  │      HEALING (nếu cần)
  │         │
  │         ↓
  │      Quay lại TAPPING (điểm tiếp)
  │
  └──── DONE (hết tất cả điểm)
```

| State | Ý nghĩa | Trigger |
|-------|---------|---------|
| `IDLE` | Không làm gì | Khởi tạo, user STOP, lỗi |
| `TAPPING` | Đang navigate đến toạ độ | Bắt đầu _attack() |
| `WAIT_POPUP` | Chờ popup Chiếm xuất hiện | Sau navigate, trước click |
| `SELECTING` | Đang chọn quân | Sau click Chiếm, trước OK |
| `WAIT_DONE` | Chờ hành quân hoàn tất | Sau tap OK |
| `COOLDOWN` | Đợi giữa 2 điểm | Sau _attack OK, trước điểm tiếp |
| `HEALING` | Đang hồi máu | Khi cần heal |
| `DONE` | Hoàn thành tất cả | Hết danh sách điểm |

GUI hiển thị state qua panel "State Machine" ở cột trái: ● cho state hiện tại, ○ cho các state khác. Mỗi state có màu riêng (xem `STATE_COLOR` trong `config.py`).

---

## 9. Safety Gates

### 9.1. CAPTCHA Detection

**Kiểm tra tại**: Trước mỗi điểm + mỗi 30s cooldown

**Detector.has_captcha_popup()** (`detector.py` line 1139-1167):
```
1. Crop vùng 15%-55% cao, 10%-90% ngang
2. Scale 2x + grayscale + threshold + pytesseract
3. Tìm keywords: "random test", "please select", "from the pictures", "countdown"
4. Nếu ≥ 2 keywords → có CAPTCHA
```

**Khi phát hiện CAPTCHA** (`gui.py` `_stop_all_captcha`):
```
1. Dừng BotEngine (tab Tấn công)
2. Dừng tất cả WaveGroupEngine (tab Nhiều đợt)
3. Dừng Auto Lv
4. Dừng BuildEngine
5. Beep cảnh báo 15 lần (1200Hz + 800Hz xen kẽ)
```

### 9.2. Popup không đúng sau Click Chiếm

Sau click Chiếm, bot dùng `detect_popup_type()` kiểm tra popup `army_selection`:
- Chờ tối đa 30s (poll mỗi 1s)
- Nếu không thấy → retry click Chiếm, tối đa **3 lần**
- Sau 3 lần vẫn fail → `return False` → bot DỪNG

### 9.3. Tên quân không khớp

Nếu TroopSelector chọn toàn bộ bằng fallback positional (không nhận ra tên nào) → `_no_match_alert = True` → bot DỪNG + beep.

### 9.4. Navigate thất bại

Nếu `navigate_to()` return False → `_attack()` return False → bot DỪNG.

### 9.5. Popup không hiện

Nếu sau 2 lần chờ popup (10s mỗi lần) vẫn không thấy → return False → bot DỪNG.

---

## 10. Module Dependency Map

```
gui.py
 └─ bot_engine.py (BotEngine)
     ├─ adb_controller.py (ADB)
     │   ├─ subprocess: adb commands
     │   ├─ cv2: screenshot decode
     │   ├─ pytesseract: OCR army status/health
     │   └─ numpy: image processing
     │
     ├─ detector.py (Detector)
     │   ├─ cv2: template matching, color filtering
     │   ├─ pytesseract: OCR troop names, march time
     │   └─ assets/*.png: template images
     │
     ├─ troop_selector.py (TroopSelector)
     │   ├─ ADB: tap, swipe, screenshot
     │   └─ Detector: checkbox, OCR, fuzzy match
     │
     ├─ brain.py (TroopBrain)
     │   └─ Rule engine thuần (không I/O)
     │
     ├─ tile_ocr.py
     │   ├─ cv2 + pytesseract: OCR tile popup
     │   └─ regex: classify lv1-lv5
     │
     └─ ollama_vision.py (OllamaVision)
         └─ urllib: gọi Ollama API local
```

---

## 11. Toạ độ cố định (hardcoded)

Các toạ độ pixel được hardcode trong bot, phụ thuộc vào resolution emulator:

| Toạ độ | Mục đích | File | Dòng |
|--------|----------|------|------|
| `(54, 1216)` | Nút thoát thành | `bot_engine.py` | 159, 776 |
| `(342, 1216)` | Nút back / đóng popup | `bot_engine.py` | 104, 402 |
| `(666, 1216)` | Nút mở All Armies | `bot_engine.py` | 102, 132 |
| `(486, 704)` | Nút Chiếm (Conquer) | `bot_engine.py` | 470 |
| `(342, 1045)` | Nút OK (chọn quân) | `detector.py` | 1042 |
| `(486, 746)` | Nút March (hồi máu) | `bot_engine.py` | 650 |
| `(488, 746)` | Nút Xây (heal step) | `bot_engine.py` | 615 |
| `(558, 490)` | Nút xác nhận xây | `bot_engine.py` | 620 |
| `(558, 1216)` | Nút bản đồ (nav) | `config.py` | nav_btn_x/y |
| `(378, 1002)` | Ô nhập X | `config.py` | x_field_x/y |
| `(486, 1002)` | Ô nhập Y | `config.py` | y_field_x/y |
| `(630, 1002)` | Nút Xem | `config.py` | xem_x/y |
| `(414, 1216)` | Nút mở popup Quay thưởng | `spin_engine.py` | POPUP_BTN |
| `(234, 1130)` | Nút Quay (Spin) | `spin_engine.py` | SPIN_BTN |
| `(342, 1216)` | Nút đóng popup Quay | `spin_engine.py` | (shared) |

---

## 12. Config ảnh hưởng đến luồng

| Config key | Mặc định | Ảnh hưởng |
|------------|----------|-----------|
| `troop_names` | `["auto1",...,"auto5"]` | Tên đầu tiên = quân chính (bắt buộc). Các tên khác = auto select |
| `max_troops` | `5` | Số quân tối đa chọn mỗi lần |
| `cooldown_sec` | `300` | Cooldown mặc định (lv1) |
| `lv2_cooldown_sec` | `600` | Cooldown cho điểm Lv.2 |
| `lv3_cooldown_sec` | `900` | Cooldown cho điểm Lv.3 |
| `lv4_cooldown_sec` | `1200` | Cooldown cho điểm Lv.4 |
| `popup_timeout` | `10.0` | Timeout chờ popup Chiếm (giây) |
| `troop_timeout` | `8.0` | Timeout chờ màn hình chọn quân |
| `nav_wait` | `1.5` | Chờ sau tap Xem (bản đồ di chuyển) |
| `tap_delay` | `0.4` | Delay sau mỗi ADB tap |
| `heal_every` | `5` | Auto heal sau mỗi N lần tấn công (0=tắt) |
| `heal_step` | `0` | Đặt điểm hồi máu mỗi N step (0=tắt) |
| `heal_x`, `heal_y` | `500`, `300` | Toạ độ game điểm hồi máu |
| `army_health_enabled` | `True` | Bật Brain quét máu trước mỗi điểm |
| `health_force_heal_avg` | `0.30` | HP trung bình ≤ 30% → heal ngay |
| `health_warn_avg` | `0.55` | HP ≤ 55% → chỉ đánh mục tiêu nhẹ |
| `health_critical_ratio` | `0.25` | HP unit ≤ 25% = unit "critical" |
| `health_force_heal_critical_units` | `2` | ≥ 2 unit critical → heal ngay |
| `ollama_enabled` | `False` | Bật AI Ollama (chưa dùng trong attack flow) |
| `debug_mode` | `False` | Lưu ảnh debug vào thư mục debug/ |
| `game_start` | `""` | Ngày bắt đầu game (display only) |

---

## Phụ lục: Trạng thái điểm tấn công

Mỗi `AttackPoint` có trường `status` thay đổi theo flow:

```
waiting → going → process → done
                     ↑
                  (quân đến nơi)
```

| Status | Ý nghĩa | Khi nào |
|--------|---------|---------|
| `waiting` | Chưa xử lý | Mới thêm, hoặc attack fail |
| `going` | Đang navigate | Bắt đầu _attack() |
| `process` | Quân đang hành quân / chiếm | Sau tap OK, chờ march |
| `done` | Hoàn thành | Sau cooldown xong |
