# Luồng hoạt động Tab Quay thưởng (Spin / Lucky Wheel Flow)

> **Phiên bản**: 2.1  
> **Modules liên quan**: `spin_engine.py`, `gui.py`  
> **Entry point**: `GUI._start_spin()` → `SpinEngine.start()` → `SpinEngine._run()`

---

## Mục lục

1. [Tổng quan](#1-tổng-quan)
2. [Kiến trúc](#2-kiến-trúc)
   - 2.1 [SpinEngine](#21-spinengine)
   - 2.2 [Hằng số](#22-hằng-số)
3. [Giao diện (GUI)](#3-giao-diện-gui)
4. [Luồng chính](#4-luồng-chính)
   - 4.1 [Step 1: Thoát thành](#41-step-1-thoát-thành)
   - 4.2 [Step 2: Mở popup quay](#42-step-2-mở-popup-quay)
   - 4.3 [Step 3: OCR đọc lượt còn lại](#43-step-3-ocr-đọc-lượt-còn-lại)
   - 4.4 [Step 4: Kiểm tra Take a break](#44-step-4-kiểm-tra-take-a-break)
   - 4.5 [Step 5: Kiểm tra Come back tomorrow](#45-step-5-kiểm-tra-come-back-tomorrow)
   - 4.6 [Step 6: Quay](#46-step-6-quay)
   - 4.7 [Cooldown 10 phút](#47-cooldown-10-phút)
5. [OCR Strategy](#5-ocr-strategy)
   - 5.1 [Vùng crop](#51-vùng-crop)
   - 5.2 [_read_spin_status()](#52-_read_spin_status)
   - 5.3 [Regex patterns](#53-regex-patterns)
6. [ADB Lock](#6-adb-lock)
7. [Đếm số lần quay](#7-đếm-số-lần-quay)
8. [Trạng thái](#8-trạng-thái)
9. [Toạ độ cố định](#9-toạ-độ-cố-định)
10. [Safety & Edge Cases](#10-safety--edge-cases)

---

## 1. Tổng quan

Tab Quay thưởng tự động quay Lucky Wheel trong game. Mỗi ngày có tối đa 10 lượt, mỗi lượt cách nhau 10 phút (cooldown trong game hiển thị "Take a break mm:ss").

```
User bấm ▶ Bắt đầu quay
  │
  ├─ Đọc config từ UI (max_spins, cooldown)
  │
  ├─ [ADB Lock] Pre-checks:
  │    ├─ Thoát thành nếu đang trong thành
  │    ├─ Mở popup Lucky Wheel (414, 1216)
  │    └─ detect_popup_type() == "lucky_wheel"?
  │         ├─ ✅ → tiếp tục vòng lặp
  │         └─ ❌ → beep 5 lần → status="stopped" → RETURN
  │
  └─ WHILE không dừng:
       │
       ├─ [ADB Lock] OCR đọc trạng thái popup
       │    ├─ Come back tomorrow? → DONE
       │    ├─ chance(s) left: 0? → DONE
       │    ├─ Take a break mm:ss? → [Trả lock] chờ → [Lock] mở lại popup
       │    └─ Sẵn sàng quay
       │
       ├─ [ADB Lock] Tap quay → chờ 5s → đóng popup
       ├─ [Trả lock]
       │
       ├─ Cooldown (KHÔNG cần lock)
       │
       └─ [ADB Lock] Mở lại popup → lặp lại
```

---

## 2. Kiến trúc

### 2.1. SpinEngine

**File**: `spin_engine.py`

```python
class SpinEngine:
    adb:         ADB              # Chia sẻ với các engine khác
    log:         Callable         # Callback ghi log
    det:         Detector         # Detector (detect_popup_type) — có thể None
    _stop:       threading.Event  # Dừng engine
    _thread:     Thread           # Thread chạy _run()
    on_refresh:  Callable         # Callback cập nhật UI
    in_city:     threading.Event  # Trạng thái trong/ngoài thành

    # Config (chỉnh từ UI)
    max_spins:    int   = 10     # Tối đa lượt/ngày
    cooldown_sec: int   = 600    # Cooldown giữa mỗi lần quay (giây)

    # Stats
    spins_done:  int   = 0       # Số lần đã quay (sync từ OCR)
    spins_left:  int   = 0       # Số lượt còn lại
    status:      str   = "idle"  # idle | running | waiting | done | stopped
```

### 2.2. Hằng số & Config

**Toạ độ cố định (class-level):**

| Hằng số | Giá trị | Mô tả |
|---------|---------|-------|
| `POPUP_BTN` | `(414, 1216)` | Toạ độ nút mở popup Lucky Wheel |
| `SPIN_BTN` | `(234, 1130)` | Toạ độ nút quay |
| `CLOSE_BTN` | `(360, 200)` | Toạ độ đóng popup (tap ngoài) |

**Config (hằng số trong engine):**

| Config | Giá trị | Mô tả |
|--------|---------|-------|
| `max_spins` | `10` | Tối đa 10 lượt/ngày |
| `cooldown_sec` | `600` | 10 phút (600 giây) giữa mỗi lần quay |

**Stats (chỉnh được từ UI entry trước khi bấm ▶):**

| Field | Entry | Mô tả |
|-------|-------|-------|
| `spins_done` | `spin_e_done` | Số lần đã quay — OCR sẽ sync lại sau mỗi vòng |
| `spins_left` | `spin_e_left` | Số lượt còn lại — "—" = chưa biết, OCR sẽ sync |

---

## 3. Giao diện (GUI)

**File**: `gui.py` — `_build_spin_tab()`, `_start_spin()`, `_stop_spin()`

### Layout

```
┌─────────────────────────────────────────────┐
│ Thông tin                                    │
│   Trạng thái: ● RUNNING                     │
│   Đã quay:    [6  ] ← entry, chỉnh được     │
│   Còn lại:    [4  ] ← entry, chỉnh được     │
├─────────────────────────────────────────────┤
│ Luồng hoạt động                              │
│   1. Thoát thành nếu đang trong thành       │
│   2. Bấm mở popup quay thưởng (414, 1216)   │
│   3. Kiểm tra popup = lucky_wheel            │
│   4. Đọc số lượt: Today's chance(s) left: x │
│   5. Take a break mm:ss → chờ hết rồi quay  │
│   6. Come back tomorrow → hết lượt, dừng    │
│   7. Bấm quay (234, 1130) → cooldown → lặp  │
├─────────────────────────────────────────────┤
│ Điều khiển                                   │
│   [▶  Bắt đầu quay]  [⏹  Dừng]             │
├─────────────────────────────────────────────┤
│ Log                                          │
│   [HH:MM:SS] [Spin] 🪟 detect = lucky_wheel │
│   [HH:MM:SS] [Spin] 🎯 Quay! Tap @ ...     │
│   [HH:MM:SS] [Spin] ✅ Đã quay lần 6/10    │
│   [HH:MM:SS] [Spin] ⏳ Chờ cooldown 10:00  │
└─────────────────────────────────────────────┘
```

### Widgets

| Widget | Biến | Mô tả |
|--------|------|-------|
| Label trạng thái | `spin_lbl_status` | ● IDLE / RUNNING / WAITING / DONE / STOPPED |
| Entry đã quay | `spin_e_done` | Số lần đã quay — user có thể chỉnh trước khi bấm ▶, OCR sẽ sync lại |
| Entry còn lại | `spin_e_left` | Số lượt còn lại — user có thể chỉnh, "—" = chưa biết |
| Button bắt đầu | — | Gọi `_start_spin()` — đọc done/left từ entry |
| Button dừng | — | Gọi `_stop_spin()` |
| Text log | `spin_log_txt` | Log chi tiết từng bước |

---

## 4. Luồng chính

**File**: `spin_engine.py` — `_run()` (line ~134)

### 4.0. Pre-checks (trước vòng lặp)

Tất cả nằm trong 1 block `with ADB_LOCK`:

```python
with ADB_LOCK:
    # 1. Thoát thành
    if self.adb.is_in_city():
        self.adb.tap(54, 1216)
        time.sleep(1.5)

    # 2. Mở popup Lucky Wheel
    self._tap(414, 1216, delay=2.0)

    # 3. Detect popup type — phải là lucky_wheel
    if self.det:
        sc = self._screenshot_cv2()
        ptype = self.det.detect_popup_type(sc)
        if ptype != "lucky_wheel":
            # Beep 5 lần → status="stopped" → RETURN
            winsound.Beep(880, 400) × 5
            return
```

**Luồng chi tiết:**

```
[ADB Lock]
  ├─ is_in_city()? → tap (54, 1216) thoát thành
  ├─ tap (414, 1216) mở popup → chờ 2s
  └─ detect_popup_type(screenshot)
       ├─ "lucky_wheel" → ✅ log + tiếp tục vòng lặp
       └─ khác → ❌ beep 5 lần → status="stopped" → return
[Trả lock]
```

**Tại sao cần detect?** Nếu toạ độ POPUP_BTN bị sai, hoặc game lag không mở được popup → bot sẽ OCR trên màn hình bản đồ (rác) → quay nhầm. Detect `lucky_wheel` đảm bảo popup đúng trước khi tiếp tục.

### 4.1. Step 1 (vòng lặp): OCR đọc lượt còn lại

```python
info = self._read_spin_status()
# info["chances"] = số lượt còn lại từ "Today's chance(s) left: x"
# spins_done = MAX_SPINS - chances = 10 - x
```

OCR vùng top (80-90% H) để tìm text `Today's chance(s) left: x`.  
`spins_done` được **sync từ OCR** (không đếm nội bộ): `10 - x`.

Nếu `chances <= 0` → dừng.

### 4.2. Step 2 (vòng lặp): Kiểm tra Take a break

```python
if info["break_secs"] > 0:
    # Trả ADB lock
    ADB_LOCK.release()
    # Chờ break_secs + 5s buffer
    time.sleep(break_secs + 5)
    # Lấy lock lại, mở popup
    with ADB_LOCK:
        self._tap(*self.POPUP_BTN, delay=2.0)
    continue   # quay lại đầu vòng lặp
```

OCR vùng bot (90-99% H) tìm `Take a break mm:ss`. Nếu có → trả lock, chờ hết timer, mở lại popup.

> Đây là backup cho trường hợp bot restart giữa chừng. Luồng chính dùng cooldown cứng 10 phút.

### 4.3. Step 3 (vòng lặp): Kiểm tra Come back tomorrow

```python
if info["come_back"]:
    self.spins_done = self.max_spins   # = 10
    self.spins_left = 0
    self._tap(342, 1216, delay=1.0)    # Đóng popup
    status = "done"
    on_refresh()   # → UI: Đã quay=10, Còn lại=0
    break
```

OCR tìm `Come back tomorrow` trong **cả 2 vùng** (top + bot). Nếu có → set done=10, left=0 → đóng popup → dừng.

> **Lưu ý**: text "Come back tomorrow" có thể xuất hiện ở vùng top (80-90% H) thay vì bot, nên phải check cả 2.

### 4.4. Step 4 (vòng lặp): Quay

```python
# Vẫn trong ADB lock từ đầu vòng lặp
self._tap(*self.SPIN_BTN, delay=3.0)     # (234, 1130)
self.spins_done += 1
self.spins_left = max(0, self.spins_left - 1)

# Chờ animation 5s
time.sleep(5.0)

# Đóng popup
self._tap(342, 1216, delay=1.5)
# Trả ADB lock (finally block)
```

Thứ tự:
1. Tap nút quay (234, 1130) → chờ 3s
2. Cập nhật counter
3. Chờ animation 5s
4. Đóng popup (342, 1216) → chờ 1.5s
5. Trả ADB lock

### 4.5. Cooldown

```python
# KHÔNG cần ADB lock
cooldown = SPIN_COOLDOWN   # 600 giây
for remaining in range(cooldown, 0, -1):
    if self._stop.is_set(): return
    if remaining % 60 == 0:
        log(f"⏳ Còn {remaining//60:02d}:00...")
    time.sleep(1)

# Mở lại popup (cần ADB)
with ADB_LOCK:
    self._tap(*self.POPUP_BTN, delay=2.0)
```

Cooldown cứng 10 phút, log mỗi phút. Không phụ thuộc OCR "Take a break" (vì OCR hay bị rác từ vòng quay).

---

## 5. OCR Strategy

### 5.1. Vùng crop

Popup Lucky Wheel chiếm gần toàn bộ màn hình. Vòng quay chứa nhiều text lộn xộn gây rác OCR. Giải pháp: **crop 1 vùng lớn phía dưới** (75-99% H) chứa tất cả text cần đọc, tránh vùng vòng quay ở giữa.

```
┌─────────────────────────────────┐
│                                 │
│         Lucky Wheel             │
│        (vòng quay - RÁC)       │   ← Không OCR vùng này
│                                 │
│                                 │
├─────────────────────────────────┤ ← 75% H
│  Today's chance(s) left: 4      │
│  Take a break 03:14    Skip     │   ← OCR vùng này (75-99% H)
│  Come back tomorrow              │
└─────────────────────────────────┘ ← 99% H
```

> **Tại sao 1 vùng thay vì 2?** Vị trí text thay đổi tuỳ thiết bị/resolution. Dùng 1 vùng rộng (75-99%) đảm bảo bắt được tất cả: chances, break timer, come back.

### 5.2. _read_spin_status()

**File**: `spin_engine.py` line 97-131

```python
def _read_spin_status(self) -> dict:
    img = self._screenshot_cv2()       # Chụp 1 lần
    text_all = self._ocr_region(img, 0.75, 0.99, 0.0, 1.0)   # 1 vùng lớn
    return {
        "chances":    ...,   # int: lượt còn lại (-1 nếu không đọc được)
        "break_secs": ...,   # int: giây còn lại của Take a break (0 nếu không có)
        "come_back":  ...,   # bool: có "Come back tomorrow" không
        "raw_top":    ...,   # str: text thô toàn vùng (debug)
        "raw_bot":    "",    # str: không dùng (backward compat)
    }
```

### 5.3. Regex patterns

| Pattern | Vùng | Mục đích | Ví dụ match |
|---------|------|----------|-------------|
| `chance.*?left\s*[:\.]?\s*(\d+)` | text_all | Đọc số lượt | `Today's chance(s) left:4` |
| `(\d{1,2}):(\d{2})` + `[Tt]ake\|break` | text_all | Đọc timer | `Take a break 03:14` |
| `[Cc]ome\s*back\|tomorrow` | text_all | Hết lượt | `Come back tomorrow` |

**_ocr_region()** xử lý:
1. Crop theo % toạ độ
2. Scale up **3x** (tăng độ chính xác)
3. Grayscale → OTSU threshold
4. pytesseract `--psm 6`

---

## 6. ADB Lock

SpinEngine dùng `ADB_LOCK` (global lock chia sẻ với tất cả engine) để tránh xung đột ADB.

| Hành động | Cần lock | Ghi chú |
|-----------|----------|---------|
| Thoát thành | ✅ | `with ADB_LOCK` |
| Mở popup | ✅ | `with ADB_LOCK` |
| OCR + Quay + Đóng popup | ✅ | `acquire()` → `finally: release()` |
| Chờ Take a break | ❌ | Trả lock trước khi chờ |
| Cooldown 10 phút | ❌ | Không dùng ADB |
| Mở lại popup sau cooldown | ✅ | `with ADB_LOCK` |

**Đặc biệt**: Trong branch Take a break, lock được release **giữa chừng** try block (trước khi chờ), nên `finally` block dùng `try/except RuntimeError` để tránh double-release.

---

## 7. Đếm số lần quay

`spins_done` được sync từ OCR thay vì đếm nội bộ:

```
OCR: "Today's chance(s) left: 4"
  → chances = 4
  → spins_done = MAX_SPINS - chances = 10 - 4 = 6
  → Log: "Đã quay: 6/10 | Còn lại: 4"
```

Sau mỗi lần quay, cập nhật tạm:
```python
spins_done += 1      # 6 → 7
spins_left -= 1       # 4 → 3
```

Vòng sau OCR sẽ sync lại chính xác từ game.

**Tại sao không đếm nội bộ?** Vì user có thể restart bot giữa chừng, hoặc đã quay tay trước khi bật bot. OCR luôn cho số đúng.

---

## 8. Trạng thái

### Engine status

| Status | Khi nào | UI Label | Màu |
|--------|---------|----------|-----|
| `idle` | Khởi tạo | ● IDLE | muted |
| `running` | Đang OCR / quay | ● RUNNING | green |
| `waiting` | Chờ break / cooldown | ● WAITING | yellow |
| `done` | Hết lượt (0 hoặc come back) | ● DONE | accent |
| `stopped` | User bấm dừng | ● STOPPED | red |

### Luồng trạng thái

```
idle → running → waiting → running → waiting → ... → done
                    ↑                    ↑
              (Take a break)      (Cooldown 10m)
              
idle → running → stopped   (user bấm Dừng bất kỳ lúc nào)
```

---

## 9. Toạ độ cố định

| Toạ độ | Mục đích | Hằng số |
|--------|----------|---------|
| `(54, 1216)` | Thoát thành | (shared) |
| `(414, 1216)` | Mở popup Lucky Wheel | `POPUP_BTN` |
| `(234, 1130)` | Nút Quay (Spin) | `SPIN_BTN` |
| `(342, 1216)` | Đóng popup sau quay | (shared) |
| `(360, 200)` | Đóng popup (tap ngoài) | `CLOSE_BTN` |

---

## 10. Safety & Edge Cases

### 10.1. OCR thất bại

Nếu `_read_spin_status()` không đọc được gì (`chances = -1`, `break_secs = 0`, `come_back = False`) → engine coi như sẵn sàng quay → tap spin. Cooldown cứng 10 phút đảm bảo không quay liên tục.

### 10.2. OCR rác từ vòng quay

Vòng quay chứa text ngược, xoay, chồng chéo → OCR full screen cho text rác. Giải pháp: crop 1 vùng phía dưới (§5.1) chỉ chứa text cần đọc, tránh vùng vòng quay.

### 10.3. Bot restart giữa chừng

`spins_done` sync từ OCR mỗi vòng lặp → không bị sai khi restart.  
`Take a break` timer được OCR kiểm tra → nếu game đang trong break, bot sẽ chờ đúng.

### 10.4. Popup không hiện / sai popup

Sau khi tap POPUP_BTN, `detect_popup_type()` kiểm tra:
- `lucky_wheel` → tiếp tục
- Khác → beep 5 lần → `status="stopped"` → dừng ngay

Nếu `det` là None (không có detector) → bỏ qua kiểm tra, chạy bình thường (backward compat).

### 10.5. Xung đột ADB

Spin dùng `ADB_LOCK` giống tất cả engine khác. Nếu Tab Tấn công đang attack → Spin chờ. Khi Spin đang cooldown 10 phút (không giữ lock) → các tab khác dùng ADB thoải mái.

### 10.6. User dừng giữa chừng

`_stop` event được check:
- Trong cooldown loop: `if self._stop.is_set(): return`
- Trong break wait: `if self._stop.is_set(): return`
- Đầu mỗi vòng lặp: `while not self._stop.is_set()`

---

## Phụ lục: Sequence Diagram (1 lần quay)

```
GUI Thread          SpinEngine Thread       ADB Lock        ADB Device
    │                      │                    │                │
    │  ▶ Bắt đầu quay    │                    │                │
    │  (đọc config UI)    │                    │                │
    │─────────────────────►│                    │                │
    │                      │  === Pre-checks == │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  is_in_city()──────────────────────►│
    │                      │  tap POPUP_BTN─────────────────────►│ Mở popup
    │                      │  screenshot────────────────────────►│
    │                      │  detect_popup_type()                │
    │                      │  → "lucky_wheel" ✅ │                │
    │                      │  release(lock)────►│                │
    │                      │                    │                │
    │                      │  === Vòng lặp ===  │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  screenshot────────────────────────►│
    │                      │  OCR top: "left:4" │                │
    │                      │  OCR bot: ""       │                │
    │                      │  → chances=4, sẵn sàng quay        │
    │  ◄─ on_refresh ──── │  (cập nhật done/left)               │
    │                      │                    │                │
    │                      │  tap SPIN_BTN──────────────────────►│ Quay!
    │                      │  sleep(3s)         │                │
    │                      │  sleep(5s)         │                │ Animation
    │                      │  tap 342,1216──────────────────────►│ Đóng popup
    │                      │  release(lock)────►│                │
    │  ◄─ on_refresh ──── │                    │  (lock rảnh)   │
    │  update UI           │                    │                │
    │                      │  ⏳ Cooldown       │                │
    │                      │  (không cần lock)  │                │
    │                      │  .......           │                │
    │                      │  log "Còn 09:00"   │                │
    │                      │  .......           │                │
    │                      │  log "Còn 01:00"   │                │
    │                      │                    │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  tap POPUP_BTN─────────────────────►│ Mở lại popup
    │                      │  release(lock)────►│                │
    │                      │                    │                │
    │                      │  === Vòng tiếp === │                │
    │                      │  ...               │                │
```
