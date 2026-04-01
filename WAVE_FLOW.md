# Luồng hoạt động Tab Nhiều đợt (Wave Attack Flow)

> **Phiên bản**: 2.3  
> **Modules liên quan**: `wave_engine.py`, `gui.py`, `adb_controller.py`, `detector.py`, `troop_selector.py`, `tile_ocr.py`  
> **Entry point**: `GUI._wave_start_group(g, w)` → `WaveGroupEngine.start()` → `WaveGroupEngine._run()`

---

## Mục lục

1. [Tổng quan](#1-tổng-quan)
2. [Kiến trúc](#2-kiến-trúc)
   - 2.1 [WaveGroup (data)](#21-wavegroup-data)
   - 2.2 [WaveGroupEngine (engine)](#22-wavegroupengine-engine)
   - 2.3 [ADB Lock](#23-adb-lock)
3. [Giao diện (GUI)](#3-giao-diện-gui)
   - 3.1 [Layout tổng quan](#31-layout-tổng-quan)
   - 3.2 [UI mỗi nhóm](#32-ui-mỗi-nhóm)
   - 3.3 [Nút điều khiển](#33-nút-điều-khiển)
   - 3.4 [Lưu/Đồng bộ UI](#34-lưuđồng-bộ-ui)
4. [Luồng khởi tạo & Validate](#4-luồng-khởi-tạo--validate)
5. [Vòng lặp tấn công](#5-vòng-lặp-tấn-công)
   - 5.1 [Pre-checks](#51-pre-checks)
   - 5.2 [Chờ ADB Lock](#52-chờ-adb-lock)
   - 5.3 [Navigate & Attack](#53-navigate--attack)
   - 5.4 [Chờ hành quân](#54-chờ-hành-quân)
   - 5.5 [Cooldown theo nhãn](#55-cooldown-theo-nhãn)
   - 5.6 [Skip cooldown](#56-skip-cooldown)
6. [Chi tiết _navigate_and_attack()](#6-chi-tiết-_navigate_and_attack)
   - 6.1 [Thoát thành](#61-thoát-thành)
   - 6.2 [Navigate đến toạ độ](#62-navigate-đến-toạ-độ)
   - 6.3 [OCR trước tap](#63-ocr-trước-tap)
   - 6.4 [Tap giữa + OCR nhãn](#64-tap-giữa--ocr-nhãn)
   - 6.5 [Chờ popup Chiếm](#65-chờ-popup-chiếm)
   - 6.6 [Click Chiếm](#66-click-chiếm)
   - 6.7 [Chọn quân](#67-chọn-quân)
7. [Trạng thái điểm](#7-trạng-thái-điểm)
8. [Safety Gates](#8-safety-gates)
9. [Toạ độ cố định](#9-toạ-độ-cố-định)
10. [So sánh với Tab Tấn công](#10-so-sánh-với-tab-tấn-công)

---

## 1. Tổng quan

Tab Nhiều đợt cho phép tạo nhiều nhóm tấn công, mỗi nhóm có danh sách quân riêng + danh sách điểm riêng. Các nhóm **chạy song song** trong các thread riêng, nhưng **chia sẻ ADB** qua lock (`_WAVE_ADB_LOCK`).

```
Nhóm A ──► Thread A ──┐
Nhóm B ──► Thread B ──┼──► _WAVE_ADB_LOCK ──► ADB (1 lệnh tại 1 thời điểm)
Nhóm C ──► Thread C ──┘
```

---

## 2. Kiến trúc

### 2.1. WaveGroup (data)

**File**: `config.py` line 85

```python
@dataclass
class WaveGroup:
    name:        str        = ""
    troop_names: List[str]  = []     # Tên quân, phần tử đầu = quân chính
    points:      List[dict] = []     # [{x, y, label, status}, ...]
    enabled:     bool       = True
    heal_every:  int        = 0      # Auto heal sau N lần tấn công (0=tắt)
    heal_step:   int        = 0      # Đặt điểm hồi máu mỗi N step (0=tắt)
    heal_x:      int        = 500    # Toạ độ X điểm hồi máu
    heal_y:      int        = 300    # Toạ độ Y điểm hồi máu
    heal_on_done: bool      = False  # Tự hồi máu khi hoàn thành điểm cuối
    cooldown_sec:     int   = 0      # Cooldown Lv1 riêng nhóm (0=dùng global)
    lv2_cooldown_sec: int   = 0      # Cooldown Lv2 riêng nhóm (0=dùng global)
    lv3_cooldown_sec: int   = 0      # Cooldown Lv3 riêng nhóm (0=dùng global)
    lv4_cooldown_sec: int   = 0      # Cooldown Lv4 riêng nhóm (0=dùng global)
```

Mỗi điểm trong `points` là dict:

| Key | Kiểu | Mô tả |
|-----|------|-------|
| `x` | int | Toạ độ game X |
| `y` | int | Toạ độ game Y |
| `label` | str | Nhãn (vd: "Lv.2", "farm") |
| `status` | str | `waiting` / `going` / `process` / `done` |

### 2.2. WaveGroupEngine (engine)

**File**: `wave_engine.py`

```python
class WaveGroupEngine:
    group:      WaveGroup    # Data
    cfg:        BotConfig    # Config chung
    adb:        ADB          # Chia sẻ với tất cả engine
    det:        Detector     # Riêng mỗi engine
    _stop:      Event        # Dừng engine
    _skip_cd:   Event        # Skip cooldown/hành quân
    cur_idx:    int          # Index điểm hiện tại
    status:     str          # idle | running | waiting | done | error
    on_refresh: Callable     # Callback cập nhật UI
    on_captcha: Callable     # Callback khi phát hiện CAPTCHA
    on_error:   Callable     # Callback(group_name, msg) khi error → Telegram notify
```

### 2.3. ADB Lock

**File**: `config.py` — `_WAVE_ADB_LOCK = threading.Lock()`

- Tất cả `WaveGroupEngine` chia sẻ chung 1 lock
- Chỉ 1 nhóm được dùng ADB tại 1 thời điểm (navigate + attack)
- Sau khi attack xong → trả lock → nhóm khác dùng
- Chờ hành quân / cooldown **không cần lock** (không dùng ADB)

---

## 3. Giao diện (GUI)

### 3.1. Layout tổng quan

```
┌─────────────────────────────────────────────────────┐
│ 🌊 Tấn công nhiều đợt:                              │
│ [➕ Thêm nhóm] [▶ Chạy tất cả] [⏹ Dừng tất cả]    │
├─────────────────────────────────────────────────────┤
│ ┌──────┬──────┬──────┐                              │
│ │Nhóm 1│Nhóm 2│Nhóm 3│  ← Sub-notebook (1 tab/nhóm)│
│ └──────┴──────┴──────┘                              │
│ ┌───────────────────────────────────────────┐       │
│ │ [Nội dung nhóm đang chọn]                │       │
│ │ Header: tên, trạng thái, nút điều khiển  │       │
│ │ Left: danh sách quân    │ Right: danh sách│       │
│ │                         │ điểm tấn công   │       │
│ └───────────────────────────────────────────┘       │
├─────────────────────────────────────────────────────┤
│ Log                                                  │
│ [HH:MM:SS] [Nhóm 1] ✅ Tấn công thành công...      │
│ [HH:MM:SS] [Nhóm 2] ⏳ Cooldown 300s...            │
│ [🗑 Xóa log]                                        │
└─────────────────────────────────────────────────────┘
```

### 3.2. UI mỗi nhóm

```
┌─────────────────────────────────────────────────────────────┐
│ Tên nhóm: [Nhóm 1  ] [✏ Đổi tên]  ● IDLE                 │
│        [🗑 Xóa nhóm] [⏹ Dừng] [⏭️ Tiếp] [▶ Chạy]        │
├────────────────────────┬────────────────────────────────────┤
│ Quân                   │ Điểm tấn công                              │
│ ┌────────────────────┐ │ ┌───────────────────────────────────────┐   │
│ │ # │ Tên quân       │ │ │ # │ X   │ Y   │ Nhãn│Status│ ✅│ ⏳│   │
│ │ 1 │ auto1          │ │ │ 1 │ 500 │ 300 │     │ wait │Done│Wait│   │
│ │ 2 │ auto2          │ │ │ 2 │ 600 │ 400 │ Lv2 │ wait │Done│Wait│   │
│ │ 3 │ auto3          │ │ │ 3 │ 700 │ 500 │     │ done │Done│Wait│   │
│ └────────────────────┘ │ └───────────────────────────────────────┘   │
│ [➕][✏][🗑][⬆][⬇]     │ [➕][✏][🗑][⬆][⬇][Xoá hết][🧹 Xoá done]  │
│                        │ [Import từ Tab Tấn công]                    │
│ 🩸 Hồi máu            │ [Thêm dải X] [Thêm dải Y]                  │
│ Heal mỗi: [5] lần     │                                             │
│ Step đặt: [0] step     │                                             │
│ Heal X: [500] Y: [300] │                                             │
│ ☑ Hồi máu khi hết điểm cuối│                                        │
│                        │                                             │
│ ⏱ Cooldown (0=dùng chung)│ Click ✅ = done, ⏳ = waiting (nhanh)    │
│ CD(s): [0] Lv2: [0]   │                                             │
│ Lv3:   [0] Lv4: [0]   │                                             │
└────────────────────────┴─────────────────────────────────────────────┘
```

### 3.3. Nút điều khiển

| Cấp | Nút | Hành động |
|-----|-----|-----------|
| Toàn tab | ▶ Chạy tất cả | Chạy tất cả nhóm enabled |
| Toàn tab | ⏹ Dừng tất cả | Dừng tất cả engine |
| Mỗi nhóm | ▶ Chạy | Tạo `WaveGroupEngine`, start thread |
| Mỗi nhóm | ⏭️ Tiếp | `skip_cooldown()` — bỏ qua cooldown/hành quân |
| Mỗi nhóm | ⏹ Dừng | `stop()` — set `_stop` event |
| Mỗi nhóm | 🗑 Xóa nhóm | Xóa nhóm khỏi danh sách |

### 3.4. Lưu/Đồng bộ UI

Các giá trị heal và cooldown được nhập trong UI entries nhưng chỉ là widget — chưa ghi vào `WaveGroup` cho đến khi:

1. **Bấm ▶ Chạy** — `_wave_start_group()` đọc UI → ghi vào `WaveGroup`
2. **Autosave / Tắt app** — `_wave_sync_ui()` được gọi trong `_save_cfg_to()` trước khi serialize, đồng bộ tất cả UI entries (heal + cooldown) về `WaveGroup` objects

Điều này đảm bảo user thay đổi giá trị trong UI mà chưa bấm Chạy vẫn được lưu khi tắt app.

---

## 4. Luồng khởi tạo & Validate

**File**: `gui.py` — `_wave_start_group()` (line ~1307)

```
User bấm ▶ Chạy (nhóm)
  │
  ├─ ADB đã kết nối? → Nếu chưa → thử connect → fail → return
  │
  ├─ Nhóm có tên quân? → Nếu chưa → beep + messagebox → return
  │
  ├─ Nhóm có điểm? → Nếu chưa → messagebox → return
  │
  ├─ Đọc heal settings từ UI (heal_every, heal_step, heal_x, heal_y)
  │
  ├─ Đọc cooldown settings từ UI (cooldown_sec, lv2/3/4_cooldown_sec)
  │
  ├─ Bot tấn công chính đang chạy? → Dừng nó (nhường ADB)
  │
  ├─ Engine cũ của nhóm này còn chạy? → Dừng nó
  │
  └─ Tạo WaveGroupEngine mới → start()
```

---

## 5. Vòng lặp tấn công

**File**: `wave_engine.py` — `_run()` (line ~213)

```
_run() bắt đầu
  │
  ├─ Pre-checks
  │    ├─ Có điểm? → Không → done
  │    ├─ Có tên quân? → Không → error
  │    └─ Check quân Standby (dùng ADB Lock)
  │         ├─ Mở All Armies → OCR → Back
  │         ├─ Quân chính có tồn tại? → Không → error + beep
  │         └─ Log danh sách Standby
  │
  ├─ WHILE !stop:
  │    │
  │    ├─ idx >= len(pts)?
  │    │    ├─ Scan điểm chưa done → có → reset idx, tiếp tục
  │    │    └─ Không còn → status="done", heal_on_done (1 lần)
  │    │         └─ Poll mỗi 2s chờ điểm mới (không thoát thread)
  │    │
  │    ├─ Bỏ qua điểm đã done
  │    ├─ Check CAPTCHA → có → dừng tất cả
  │    │
  │    ├─ Chờ ADB Lock
  │    │    ├─ acquire(non-blocking) → OK → tiếp
  │    │    └─ Không được → status="waiting", acquire(blocking)
  │    │
  │    ├─ _navigate_and_attack(pt) → march_s
  │    ├─ Trả ADB Lock
  │    │
  │    ├─ march_s > 0 (thành công):
  │    │    ├─ attack_count++
  │    │    ├─ pt.status = "process"
  │    │    ├─ Check CAPTCHA → có → dừng tất cả
  │    │    ├─ Chờ hành quân (march_s giây)
  │    │    ├─ Cooldown theo nhãn (per-group override, nếu không phải điểm cuối)
  │    │    ├─ pt.status = "done"
  │    │    ├─ Heal step? (attack_count % heal_step == 0)
  │    │    │    └─ _setup_heal_point(): navigate → tap giữa → Xây → confirm → cập nhật heal_x,y
  │    │    ├─ Auto heal? (attack_count % heal_every == 0)
  │    │    │    └─ _do_heal(): navigate heal_x,heal_y → 3s → March → chọn quân → chờ
  │    │    └─ idx++
  │    │
  │    └─ march_s == 0 (thất bại):
  │         └─ Chờ 30s → thử lại điểm này
  │
  └─ Kết thúc: chỉ khi user bấm ⏹ Dừng (status = "idle")
     Thread KHÔNG tự thoát khi hết điểm — poll chờ điểm mới mỗi 2s
```

### 5.1. Pre-checks

```python
# 1. Validate
if not pts: status = "done"; return
if not troop_names: status = "error"; return

# 2. Check quân (cần ADB Lock)
with _WAVE_ADB_LOCK:
    adb.tap(666, 1216)         # Mở All Armies
    time.sleep(1.5)
    armies = adb.read_army_status(required_names=troop_names)
    adb.tap(342, 1216)         # Back
    time.sleep(0.8)

# 3. Quân chính có tồn tại?
main_found = any(main_troop in a["name"].lower() for a in armies)
if not main_found: status = "error"; beep; return
```

### 5.2. Chờ ADB Lock

```python
if not _WAVE_ADB_LOCK.acquire(blocking=False):
    # Lock đang bị nhóm khác giữ
    log("⏳ Đang chờ hàng ADB...")
    status = "waiting"
    _WAVE_ADB_LOCK.acquire()   # block cho đến khi rảnh

try:
    march_s = _navigate_and_attack(pt)
finally:
    _WAVE_ADB_LOCK.release()   # LUÔN trả lock
```

**Quan trọng**: Lock chỉ bao quanh phần dùng ADB (navigate + attack). Chờ hành quân và cooldown **nằm ngoài lock** để nhóm khác có thể dùng ADB.

### 5.3. Navigate & Attack

→ Xem [§6. Chi tiết _navigate_and_attack()](#6-chi-tiết-_navigate_and_attack)

### 5.4. Chờ hành quân

```python
if march_s > 0:
    pt["status"] = "process"       # ← cập nhật trạng thái
    status = "waiting"

    # Check CAPTCHA ngay sau attack, trước khi chờ
    if _check_captcha(): break

    _skip_cd.clear()
    deadline = time.time() + march_s
    while time.time() < deadline and not _stop and not _skip_cd:
        time.sleep(1)
```

- Không cần ADB Lock (không tương tác ADB)
- Hỗ trợ `_skip_cd` để user bỏ qua
- **Check CAPTCHA ngay sau tấn công** — phát hiện sớm nhất có thể
- Trong lúc chờ hành quân và cooldown **không check CAPTCHA** (sẽ check lại trước điểm tiếp theo)

### 5.5. Cooldown theo nhãn (per-group override)

```python
is_last = (idx >= len(pts) - 1)
if not is_last:                     # Điểm cuối → không cooldown
    lbl = pt.get("label", "")
    grp = self.group
    # Ưu tiên cooldown riêng nhóm, fallback về global config nếu = 0
    if "lv4" in lbl:   cd = grp.lv4_cooldown_sec if grp.lv4_cooldown_sec > 0 else cfg.lv4_cooldown_sec
    elif "lv3" in lbl: cd = grp.lv3_cooldown_sec if grp.lv3_cooldown_sec > 0 else cfg.lv3_cooldown_sec
    elif "lv2" in lbl: cd = grp.lv2_cooldown_sec if grp.lv2_cooldown_sec > 0 else cfg.lv2_cooldown_sec
    else:              cd = grp.cooldown_sec     if grp.cooldown_sec > 0     else cfg.cooldown_sec

    # Chờ cooldown (không check CAPTCHA — sẽ check trước điểm tiếp theo)
    while time.time() < cd_deadline and not _stop and not _skip_cd:
        time.sleep(1)
```

**Quy tắc ưu tiên**: Nếu nhóm có `cooldown_sec > 0` (hoặc `lv2/lv3/lv4_cooldown_sec > 0`) → dùng giá trị riêng của nhóm. Nếu = 0 → fallback về giá trị global trong `BotConfig`. Backward compatible — nhóm cũ (tất cả = 0) hoạt động giống như trước.

Logic nhận dạng nhãn dùng regex: `re.search(r'lv\.?{n}\b', label, IGNORECASE)`

| Nhãn mẫu | Match | Cooldown (ưu tiên nhóm → global) |
|-----------|-------|----------------------------------|
| `Lv.4`, `lv4`, `LV.4 farm` | lv4 | `grp.lv4_cooldown_sec` → `cfg.lv4_cooldown_sec` |
| `Lv.3`, `lv3 east` | lv3 | `grp.lv3_cooldown_sec` → `cfg.lv3_cooldown_sec` |
| `Lv.2` | lv2 | `grp.lv2_cooldown_sec` → `cfg.lv2_cooldown_sec` |
| `farm`, ``, `resource` | none | `grp.cooldown_sec` → `cfg.cooldown_sec` |

### 5.6. Skip cooldown

```python
def skip_cooldown(self):
    self._skip_cd.set()
```

`_skip_cd` event được check trong 2 vòng lặp:
1. **Chờ hành quân** — thoát ngay khi set
2. **Chờ cooldown** — thoát + log "⏭️ Skip cooldown → điểm tiếp theo"

Sau khi thoát loop, `_skip_cd.clear()` để reset cho điểm tiếp theo.

---

## 6. Chi tiết _navigate_and_attack()

**File**: `wave_engine.py` line 86-211  
**Trả về**: `march_seconds` (int > 0 = thành công, 0 = thất bại)

### 6.1. Thoát thành

```python
if adb.is_in_city():
    adb.tap(54, 1216)       # Nút thoát thành
    time.sleep(1.5)
```

### 6.2. Navigate đến toạ độ

```
 Bước │ Thao tác                           │ Toạ độ / Chi tiết
──────┼────────────────────────────────────┼────────────────────────
  1   │ Tap nút bản đồ                    │ (nav_btn_x, nav_btn_y)
  2   │ Nhập X                             │ clear_and_type(x_field_x/y, X)
  3   │ Dismiss keyboard                   │ KEYCODE_BACK
  4   │ Tap ô Y (2 lần)                   │ (y_field_x, y_field_y)
  5   │ Xóa text cũ                        │ KEYCODE_MOVE_END + 12x DEL
  6   │ Nhập Y                             │ keyevent từng digit
  7   │ Dismiss keyboard                   │ tap giữa (w/2, h*0.35)
  8   │ Tap Xem                            │ (xem_x, xem_y)
  9   │ Chờ bản đồ di chuyển              │ nav_wait giây
```

### 6.3. OCR trước tap

```python
# Chụp ảnh → scale 2x → grayscale → OTSU → OCR
# Tìm keyword: "Wasteland", "Lv.2", "Lv.3", "Lv.4", "Lv.5"
# Nếu thấy → _tap_twice = True (tap 2 lần vào giữa)
```

### 6.4. Tap giữa + OCR nhãn

```python
adb.tap(cx, cy)                      # Tap giữa màn hình
if _tap_twice:
    time.sleep(0.6); adb.tap(cx, cy) # Tap lần 2

# OCR sau tap: analyze_tile_image() → phát hiện nhãn (vd: "lv2")
# Cập nhật pt["label"] nếu nhận diện được
```

### 6.5. Chờ popup Chiếm

```python
deadline = time.time() + 8.0
while time.time() < deadline:
    sc = adb.screenshot_cv2()
    cap_coord = det.find_capture_btn(sc)
    if cap_coord: break
    time.sleep(0.4)
# Không tìm thấy → return 0
```

### 6.6. Click Chiếm

```python
adb.tap(486, 704)
time.sleep(1.0)

# Kiểm tra popup army_selection (tối đa 3 lần)
for attempt in range(1, 4):
    ptype = det.detect_popup_type(sc)
    if ptype == "army_selection": break
    # Chờ tối đa 30s, poll mỗi 1s
    # Nếu vẫn không thấy → Click Chiếm lại
    # Sau 3 lần → return 0 (bỏ qua điểm)
```

Luồng giống Tab Tấn công: chờ 1s → detect popup → nếu chưa có army_selection → chờ 30s → retry, tối đa **3 lần** → fail → return 0.

### 6.7. Chọn quân

```python
sel = _make_selector()    # TroopSelector với troop_names của nhóm
ok = sel.select()
if not ok: return 0

# Kiểm tra tên quân không khớp → dừng nhóm
if sel._no_match_alert:
    status = "error"; return 0

march_s = sel.last_march_seconds
return march_s
```

**`_make_selector()`**: Copy config, ghi đè `troop_names` bằng danh sách quân của nhóm → TroopSelector chọn đúng quân cho nhóm.

---

## 7. Trạng thái điểm

```
waiting → going → process → done
                     ↑
              (quân đang hành quân)
```

| Status | Ý nghĩa | Khi nào | Màu UI |
|--------|---------|---------|--------|
| `waiting` | Chưa xử lý | Mới thêm, hoặc chờ | muted |
| `going` | Đang navigate | Bắt đầu attack | yellow |
| `process` | Quân đang hành quân | Sau khi attack OK, chờ march | accent |
| `done` | Hoàn thành | Sau march + cooldown xong | green |

### Trạng thái Engine

| Status | Label UI | Khi nào |
|--------|----------|---------|
| `idle` | ● IDLE | Khởi tạo, sau khi dừng |
| `running` | ▶ ĐANG CHẠY | Đang navigate/attack |
| `waiting` | ⏳ CHỜ | Chờ ADB lock / hành quân / cooldown |
| `done` | ✅ XONG | Tất cả điểm hoàn thành |
| `error` | ❌ LỖI | CAPTCHA / quân không tìm thấy / tên không khớp |

---

## 8. Safety Gates

### 8.1. CAPTCHA

- Check **trước mỗi điểm** (ngoài lock)
- Check **ngay sau tấn công thành công** (trước khi chờ hành quân)
- Phát hiện → beep 10 lần → dừng nhóm → gọi `on_captcha()` (dừng tất cả)

### 8.2. Quân chính không tìm thấy

Pre-check: mở All Armies → OCR → kiểm tra popup trước khi Back → nếu quân chính không có trong danh sách → beep + `error` + `on_error()` → Telegram screenshot + thông báo.

### 8.3. Tên quân không khớp

Sau khi `TroopSelector.select()`, nếu `_no_match_alert = True` → dừng nhóm (engine status = error) + `on_error()` → Telegram screenshot + thông báo.

### 8.4. Popup không hiện

Chờ tối đa 8s tìm nút Chiếm. Không thấy → `return 0` → engine chờ 30s rồi thử lại.

### 8.5. Popup không đúng sau Click Chiếm

Sau click Chiếm, dùng `detect_popup_type()` kiểm tra `army_selection`:
- Chờ tối đa 30s (poll mỗi 1s)
- Nếu không thấy → retry click Chiếm, tối đa **3 lần**
- Sau 3 lần vẫn fail → `return 0` → engine chờ 30s rồi thử lại điểm

### 8.6. Attack thất bại

`_navigate_and_attack()` return 0 → engine chờ 30s rồi thử lại **cùng điểm** (không skip).

### 8.7. Đóng popup an toàn

Trước khi tap Back (342, 1216), luôn kiểm tra có popup đang mở bằng `detect_popup_type()`. Chỉ tap khi có popup → tránh tap nhầm.

### 8.8. Telegram notify

Tất cả lỗi dẫn tới `status = "error"` đều gọi `on_error(group_name, msg)` → GUI gửi screenshot + thông báo qua Telegram (nếu đang bật). Xem chi tiết tại `TELEGRAM_FLOW.md`.

---

## 9. Toạ độ cố định

| Toạ độ | Mục đích | Dùng ở đâu |
|--------|----------|------------|
| `(54, 1216)` | Thoát thành | `_navigate_and_attack()` |
| `(666, 1216)` | Mở All Armies | Pre-check quân |
| `(342, 1216)` | Nút Back | Pre-check, sau attack |
| `(486, 704)` | Nút Chiếm | Click Chiếm |
| `(cx, cy)` | Giữa màn hình | Tap vào tile sau navigate |
| `(nav_btn_x/y)` | Nút bản đồ | Navigate (từ config) |
| `(x_field_x/y)` | Ô nhập X | Navigate (từ config) |
| `(y_field_x/y)` | Ô nhập Y | Navigate (từ config) |
| `(xem_x/y)` | Nút Xem | Navigate (từ config) |

---

## 10. So sánh với Tab Tấn công

| Tính năng | Tab Tấn công | Tab Nhiều đợt |
|-----------|-------------|---------------|
| Engine | `BotEngine` | `WaveGroupEngine` |
| Số nhóm | 1 | Nhiều (song song) |
| ADB | Dùng trực tiếp | Qua `_WAVE_ADB_LOCK` |
| Quân | 1 bộ chung | Mỗi nhóm riêng |
| Điểm | `List[AttackPoint]` | `List[dict]` |
| Cooldown | Dùng global `BotConfig` | Per-group (fallback global nếu = 0) |
| Navigate | `navigate_to()` method | Inline trong `_navigate_and_attack()` |
| Popup Chiếm | `detect_popup_type("army_selection")` + retry | `detect_popup_type("army_selection")` + retry |
| Error popup | Đã bỏ `has_error_popup` | Đã bỏ `has_error_popup` |
| Heal | `_do_heal()` tự động | `_do_heal()` tự động (heal_every per group) |
| Brain (HP scan) | Có | Không có |
| OCR nhãn | Có (cập nhật nhãn) | Có (cập nhật nhãn) |
| Skip cooldown | `⏭️ Điểm tiếp theo` | `⏭️ Tiếp` (mỗi nhóm) |
| Trạng thái điểm | `waiting→going→process→done` | `waiting→going→process→done` |

---

## Phụ lục: Sequence Diagram (1 điểm)

```
GUI Thread          WaveEngine Thread       ADB Lock        ADB Device
    │                      │                    │                │
    │  ▶ Chạy nhóm        │                    │                │
    │─────────────────────►│                    │                │
    │                      │  Pre-checks        │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  ◄─────────────────│                │
    │                      │  tap(666,1216)──────────────────────►│ All Armies
    │                      │  OCR quân──────────────────────────►│
    │                      │  tap(342,1216)──────────────────────►│ Back
    │                      │  release(lock)────►│                │
    │                      │                    │                │
    │                      │  === Điểm 1 ===    │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  ◄─────────────────│                │
    │                      │  navigate──────────────────────────►│
    │                      │  tap giữa──────────────────────────►│
    │                      │  chờ popup─────────────────────────►│
    │                      │  tap Chiếm─────────────────────────►│
    │                      │  chọn quân─────────────────────────►│
    │                      │  release(lock)────►│                │
    │                      │                    │  (lock rảnh)   │
    │  ◄─ on_refresh ──── │                    │                │
    │  update UI           │  check CAPTCHA     │                │
    │                      │  ⏳ hành quân      │                │
    │                      │  (không cần lock)  │                │
    │                      │  ........          │                │
    │                      │  ⏳ cooldown       │                │
    │                      │  ........          │                │
    │  ◄─ on_refresh ──── │  pt.status="done"  │                │
    │  update tree         │                    │                │
    │                      │  === Điểm 2 ===    │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  ...               │                │
```
