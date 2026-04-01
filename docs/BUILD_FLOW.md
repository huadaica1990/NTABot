# Luồng hoạt động Tab Xây dựng (Build Flow)

> **Phiên bản**: 2.1  
> **Modules liên quan**: `build_engine.py`, `gui.py`, `config.py`  
> **Entry point**: `GUI._start_build()` → `BuildEngine.start()` → `BuildEngine._run()`

---

## Mục lục

1. [Tổng quan](#1-tổng-quan)
2. [Kiến trúc](#2-kiến-trúc)
   - 2.1 [BuildConfig](#21-buildconfig)
   - 2.2 [BuildEngine](#22-buildengine)
   - 2.3 [Task (dict)](#23-task-dict)
3. [Giao diện (GUI)](#3-giao-diện-gui)
   - 3.1 [Layout](#31-layout)
   - 3.2 [Treeview — Danh sách nhà](#32-treeview--danh-sách-nhà)
   - 3.3 [Nút điều khiển](#33-nút-điều-khiển)
4. [Luồng tự động xây (_run)](#4-luồng-tự-động-xây-_run)
   - 4.1 [Bước 1: Vào thành](#41-bước-1-vào-thành)
   - 4.2 [Bước 2-4: Duyệt nhà + xây](#42-bước-2-4-duyệt-nhà--xây)
   - 4.3 [Bước 5: Ra thành](#43-bước-5-ra-thành)
   - 4.4 [Bước 6: Chờ xây xong](#44-bước-6-chờ-xây-xong)
   - 4.5 [Lặp lại](#45-lặp-lại)
5. [Chi tiết _try_build()](#5-chi-tiết-_try_build)
6. [OCR đọc thời gian xây](#6-ocr-đọc-thời-gian-xây)
7. [Xây nhanh — build_single()](#7-xây-nhanh--build_single)
8. [ADB Lock](#8-adb-lock)
9. [Kiểm tra mục tiêu Level](#9-kiểm-tra-mục-tiêu-level)
10. [Toạ độ cố định](#10-toạ-độ-cố-định)
11. [Tham số cấu hình](#11-tham-số-cấu-hình)
12. [Safety & Edge Cases](#12-safety--edge-cases)

---

## 1. Tổng quan

Tab Xây dựng tự động nâng cấp các nhà trong thành. Mỗi chu kỳ: vào thành → duyệt danh sách nhà → tap xây → ra thành → chờ hết thời gian xây → lặp lại.

```
User bấm ▶ Bắt đầu xây
  │
  └─ WHILE không dừng:
       │
       ├─ Lọc nhà enabled
       │    └─ Không có → dừng
       │
       ├─ [ADB Lock]
       │    ├─ Vào thành
       │    ├─ FOR mỗi nhà enabled:
       │    │    ├─ lv >= target? → bỏ qua (không tắt enabled)
       │    │    ├─ Đủ max_builders? → dừng thêm
       │    │    ├─ _try_build(task)
       │    │    │    ├─ Tap nhà → Tap Info → OCR thời gian → Tap Build
       │    │    │    └─ Trả về secs (>0 = OK, 0 = fail)
       │    │    ├─ OK → level+1, ghi build_done, tính total_wait
       │    │    └─ Back (342, 1216)
       │    └─ Ra thành
       │    [Trả ADB Lock]
       │
       └─ Chờ total_wait giây (KHÔNG cần lock)
            ├─ Log mỗi 5 phút + 10s cuối
            └─ Hết → lặp lại
```

---

## 2. Kiến trúc

### 2.1. BuildConfig

**File**: `config.py` line 151

```python
@dataclass
class BuildConfig:
    city_enter_x:    int   = 54       # Toạ độ vào thành
    city_enter_y:    int   = 1216
    city_exit_x:     int   = 54       # Toạ độ ra thành
    city_exit_y:     int   = 1216
    info_btn_x:      int   = 198      # Nút Info nhà
    info_btn_y:      int   = 192
    build_btn_x:     int   = 378      # Nút Build/Upgrade
    build_btn_y:     int   = 1045
    max_builders:    int   = 2        # Xây tối đa cùng lúc
    check_interval:  int   = 60       # Interval check (chưa dùng)
    speed_pct:       float = 0.0      # Buff tốc độ xây (%)
    delay_tap_house: float = 1.5      # Delay sau tap nhà
    delay_tap_info:  float = 1.5      # Delay sau tap Info
    delay_tap_build: float = 2.5      # Delay sau tap Build
    delay_back:      float = 1.2      # Delay sau tap Back
    tasks:           list  = [...]    # Danh sách nhà
```

### 2.2. BuildEngine

**File**: `build_engine.py`

```python
class BuildEngine:
    adb:        ADB              # Chia sẻ với các engine khác
    log:        Callable         # Callback ghi log
    cfg:        BuildConfig      # Config xây dựng
    _stop:      threading.Event  # Dừng engine
    _pause:     threading.Event  # Tạm dừng (khi trong thành)
    on_refresh: Callable         # Callback cập nhật UI
    in_city:    threading.Event  # Trạng thái trong/ngoài thành (shared)
```

### 2.3. Task (dict)

Mỗi nhà trong `cfg.tasks` là 1 dict:

| Key | Kiểu | Mô tả |
|-----|------|-------|
| `name` | str | Tên nhà (vd: "Nhà chính") |
| `tap_x` | int | Toạ độ X tap nhà trong thành |
| `tap_y` | int | Toạ độ Y tap nhà trong thành |
| `enabled` | bool | Bật/tắt (user control) |
| `level` | int | Level hiện tại |
| `target_lv` | int | Level mục tiêu |
| `build_done` | float | Timestamp xây xong (0 = chưa xây) |

---

## 3. Giao diện (GUI)

### 3.1. Layout

```
┌──────────────────────────────────────────────────────┐
│ Cấu hình Xây dựng                                    │
│   Vào thành X: [54 ]  Y: [1216]                      │
│   Info btn X:  [198]  Y: [192 ]                      │
│   Xây btn X:   [378]  Y: [1045]                      │
│   Max xây cùng lúc: [2]                              │
│   Check interval(s): [60]                             │
│   Tốc độ xây (%): [0]                                │
│   ⏰ Hẹn giờ: [✓] [30] phút  ⏰ 29:45                │
├──────────────────────────────────────────────────────┤
│ Danh sách nhà                                         │
│ ┌──────────────────────────────────────────────────┐  │
│ │ # │ Tên nhà  │ Lv │Mục tiêu│Tap X│Tap Y│Bật│Hết │🔨│
│ │ 1 │ Nhà chính│ Lv3│  Lv20  │ 342 │ 490 │ ✓ │ -  │Xây│
│ │ 2 │ Nhà rèn  │ Lv5│  Lv20  │ 234 │ 704 │ ✓ │còn │Xây│
│ └──────────────────────────────────────────────────┘  │
│ [➕ Thêm][✏ Sửa][🗑 Xóa][🔄 Reset][⬆ Lên][⬇ Xuống] │
├──────────────────────────────────────────────────────┤
│ Điều khiển                                            │
│ [▶ Bắt đầu xây][⏹ Dừng][💰 Check tài nguyên]       │
│ [🔄 Cài lại Lv1]                                     │
├──────────────────────────────────────────────────────┤
│ Log                                                   │
│   [10:30:00] [Build] 🏠 Xây Nhà chính (Lv3 → Lv20) │
│   [10:30:05] [Build] ⏱ Nhà chính: 00:37:20          │
└──────────────────────────────────────────────────────┘
```

### 3.2. Treeview — Danh sách nhà

Cột **🔨** ở cuối: click vào ô "Xây" → gọi `build_single(task)` xây nhanh 1 nhà đó (không cần ▶ Bắt đầu).

| Cột | Rộng | Mô tả |
|-----|------|-------|
| # | 30 | Số thứ tự |
| Tên nhà | 100 | Tên nhà |
| Lv | 40 | Level hiện tại |
| Mục tiêu | 50 | Level mục tiêu |
| Tap X | 50 | Toạ độ X |
| Tap Y | 50 | Toạ độ Y |
| Bật | 35 | ✓ / ✗ |
| Hết xây | 120 | Countdown / "✅ Xong" / "-" |
| 🔨 | 40 | Click = xây nhanh nhà đó |

### 3.3. Nút điều khiển

| Nút | Hành động |
|-----|-----------|
| ▶ Bắt đầu xây | `_start_build()` → `BuildEngine.start()` |
| ⏹ Dừng | `_stop_build()` → `BuildEngine.stop()` |
| 💰 Check tài nguyên | OCR đọc food/wood/stone từ góc trái |
| 🔄 Cài lại Lv1 | Reset tất cả nhà về level=1, build_done=0 |
| ➕ Thêm | Thêm nhà mới vào danh sách |
| ✏ Sửa | Sửa nhà đang chọn (dialog) |
| 🗑 Xóa | Xóa nhà đang chọn |
| 🔄 Reset | Reset build_done của nhà đang chọn |
| ⬆⬇ | Di chuyển thứ tự nhà |

---

## 4. Luồng tự động xây (_run)

**File**: `build_engine.py` — `_run()` (line ~143)

### 4.1. Bước 1: Vào thành

```python
with ADB_LOCK:
    if self.adb.is_in_city():
        # Đã trong thành → bỏ qua
    else:
        self._enter_city()
        # Tap (city_enter_x, city_enter_y), chờ 1.5s
        # Set in_city event
```

### 4.2. Bước 2-4: Duyệt nhà + xây

```python
enabled = [t for t in cfg.tasks if t.get("enabled", True)]
builders = 0
total_wait = 0

for task in enabled:
    if builders >= cfg.max_builders: break
    
    lv = task["level"]
    target = task["target_lv"]
    
    # Kiểm tra mục tiêu TRƯỚC khi xây
    if lv >= target:
        log("🏆 Đã đạt mục tiêu, bỏ qua")
        continue
    
    secs = _try_build(task)
    if secs > 0:
        # Áp dụng buff tốc độ
        if speed_pct > 0:
            secs = secs * (1 - speed_pct / 100)
        task["build_done"] = time.time() + secs
        task["level"] = lv + 1
        builders += 1
        total_wait += secs
    
    # Back về màn hình thành
    tap(342, 1216)
```

**Quan trọng**:
- `max_builders` giới hạn số nhà xây cùng lúc
- Check `lv >= target` **trước** khi gọi `_try_build()` → không xây thừa
- Không tắt `enabled` khi đạt mục tiêu → user tự quản lý

### 4.3. Bước 5: Ra thành

```python
self._exit_city()
# Tap (city_exit_x, city_exit_y), chờ 1.5s
# Clear in_city event
```

**Trả ADB Lock** trong `finally` block ngay sau ra thành.

### 4.4. Bước 6: Chờ xây xong

```python
# KHÔNG cần ADB Lock
if total_wait > 0:
    deadline = time.time() + total_wait
    while time.time() < deadline:
        if _stop: return
        remaining = deadline - time.time()
        # Log mỗi 5 phút + 10s cuối
        time.sleep(1)
else:
    # Không xây được nhà nào → chờ 60s rồi thử lại
    time.sleep(60)
```

### 4.0. Hẹn giờ (Timer — trước bước 1)

Nếu user check ⏰ **Hẹn giờ** + nhập số phút > 0 khi bấm ▶:

```
User check ⏰ + nhập 30 phút + bấm ▶
  │
  ├─ Đếm ngược 30:00 → 29:59 → ... → 00:01 → 00:00
  │    (hiện countdown trên UI, check _stop mỗi giây)
  │
  ├─ Hết hẹn:
  │    ├─ Uncheck ⏰ tự động
  │    ├─ Xoá label countdown
  │    └─ Bắt đầu engine.start() → vào luồng _run() bình thường
  │
  └─ User bấm ⏹ Dừng giữa chừng:
       └─ _stop.set() → thoát countdown → không start engine
```

**Mục đích**: Khi nhà đang xây (đã xây tay trước đó), user biết còn X phút mới xong → hẹn giờ để bot tự bắt đầu chu kỳ mới đúng lúc.

### 4.5. Lặp lại

Sau khi hết thời gian chờ → quay lại đầu vòng `while`:
- Lọc lại danh sách `enabled`
- Nếu tất cả nhà đã đạt mục tiêu hoặc tắt → không còn `enabled` → dừng
- Nếu còn → vào thành → xây tiếp

---

## 5. Chi tiết _try_build()

**File**: `build_engine.py` line 89-104

```
 Bước │ Thao tác                          │ Chi tiết
──────┼────────────────────────────────────┼────────────────────────
  1   │ Tap nhà                            │ (task.tap_x, task.tap_y)
  2   │ Chờ delay_tap_house               │ Mặc định 1.5s
  3   │ Tap nút Info                       │ (info_btn_x, info_btn_y)
  4   │ Chờ delay_tap_info                │ Mặc định 1.5s
  5   │ OCR đọc thời gian xây             │ _read_build_time() → secs
  6   │ Tap nút Build/Upgrade             │ (build_btn_x, build_btn_y)
  7   │ Chờ delay_tap_build               │ Mặc định 2.5s
```

Trả về `secs` (>0 = thành công, 0 = không đọc được thời gian).

---

## 6. OCR đọc thời gian xây

**File**: `build_engine.py` — `_read_build_time()` (line 56-87)

```python
# Crop vùng phía trên nút Build (200px)
y1 = build_btn_y - 200
y2 = build_btn_y - 20
crop = img[y1:y2, 0:w]

# Scale 2x + grayscale + OTSU
text = pytesseract.image_to_string(thresh, config="--psm 6")

# Tìm pattern H:MM:SS hoặc MM:SS
m3 = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', text)  # H:MM:SS
m2 = re.search(r'(\d{1,2}):(\d{2})', text)            # MM:SS
```

| Pattern | Ví dụ | Giây |
|---------|-------|------|
| `H:MM:SS` | `1:23:45` | 5025 |
| `MM:SS` | `37:20` | 2240 |
| Không match | — | 0 |

---

## 7. Xây nhanh — build_single()

**File**: `build_engine.py` line 106-141

Gọi khi user click cột 🔨 trong treeview. Xây **1 nhà duy nhất** mà không cần ▶ Bắt đầu.

```
Check lv >= target? → return (bỏ qua)
  │
  └─ [ADB Lock]
       ├─ Vào thành (nếu chưa trong)
       ├─ _try_build(task)
       │    ├─ OK → level+1, ghi build_done
       │    └─ Fail → log cảnh báo
       ├─ Back (342, 1216)
       └─ Ra thành
       [Trả ADB Lock]
```

**Khác với _run()**: chỉ xây 1 nhà, không loop, không chờ timer.

---

## 8. ADB Lock

BuildEngine dùng `ADB_LOCK` (global lock) để tránh xung đột với các engine khác.

| Hành động | Cần lock | Ghi chú |
|-----------|----------|---------|
| Vào thành | ✅ | Bao trong `ADB_LOCK.acquire()` |
| Tap nhà + Info + Build | ✅ | Trong cùng block lock |
| Ra thành | ✅ | Cuối block lock, trước `finally: release()` |
| Chờ xây xong | ❌ | `total_wait` giây, không dùng ADB |
| Chờ 60s (không xây được) | ❌ | Sleep, không dùng ADB |
| `build_single()` | ✅ | Toàn bộ `with ADB_LOCK` |

---

## 9. Kiểm tra mục tiêu Level

Kiểm tra `lv >= target` **TRƯỚC** khi gọi `_try_build()`:

```python
lv = task.get("level", 1)
target = task.get("target_lv", 20)
if lv >= target:
    log("🏆 Đã đạt mục tiêu, bỏ qua")
    continue   # KHÔNG tắt enabled, KHÔNG tap xây
```

| Trường hợp | Hành vi |
|------------|---------|
| `lv < target` | Xây bình thường, `lv += 1` |
| `lv == target` | Bỏ qua, log "đã đạt" |
| `lv > target` | Bỏ qua (user hạ target) |

**Không tắt `enabled`** khi đạt mục tiêu → user tự quyết định bật/tắt trên UI.

---

## 10. Toạ độ cố định

| Toạ độ | Mục đích | Config key |
|--------|----------|------------|
| `(54, 1216)` | Vào/ra thành (mặc định) | `city_enter_x/y`, `city_exit_x/y` |
| `(198, 192)` | Nút Info | `info_btn_x/y` |
| `(378, 1045)` | Nút Build/Upgrade | `build_btn_x/y` |
| `(342, 1216)` | Nút Back | hardcoded |
| `(tap_x, tap_y)` | Vị trí nhà trong thành | Per task |

> Tất cả toạ độ (trừ Back) đều configurable từ UI.

---

## 11. Tham số cấu hình

| Tham số | Mặc định | Mô tả |
|---------|----------|-------|
| `city_enter_x/y` | `54, 1216` | Toạ độ tap vào thành |
| `city_exit_x/y` | `54, 1216` | Toạ độ tap ra thành |
| `info_btn_x/y` | `198, 192` | Toạ độ nút Info |
| `build_btn_x/y` | `378, 1045` | Toạ độ nút Build |
| `max_builders` | `2` | Số nhà xây đồng thời tối đa |
| `speed_pct` | `0.0` | Buff tốc độ xây (%). VD: 50 → thời gian giảm 50% |
| `delay_tap_house` | `1.5s` | Chờ sau tap nhà |
| `delay_tap_info` | `1.5s` | Chờ sau tap Info |
| `delay_tap_build` | `2.5s` | Chờ sau tap Build |
| `delay_back` | `1.2s` | Chờ sau tap Back |

### Buff tốc độ xây

```python
if speed_pct > 0:
    secs = int(secs * (1 - speed_pct / 100))
```

| OCR đọc | speed_pct | Thời gian thực chờ |
|---------|-----------|-------------------|
| 01:00:00 (3600s) | 0 | 3600s |
| 01:00:00 (3600s) | 30 | 2520s |
| 01:00:00 (3600s) | 50 | 1800s |

---

## 12. Safety & Edge Cases

### 12.1. OCR thất bại

`_try_build()` return 0 → nhà bị bỏ qua trong chu kỳ này. Nút Build vẫn được tap (dòng 97) nên game có thể vẫn xây, chỉ là bot không biết thời gian.

Nếu **tất cả** nhà đều OCR fail → `total_wait = 0` → chờ 60s → thử lại.

### 12.2. Đã trong thành

`is_in_city()` check trước → nếu đã trong thành → bỏ qua bước vào.  
`in_city` event được set/clear để các engine khác biết (vd: bot tấn công chờ build ra thành).

### 12.3. Tất cả nhà đạt mục tiêu

Mỗi vòng lặp filter `enabled` → nếu tất cả nhà `lv >= target` → tất cả bị `continue` → `builders = 0`, `total_wait = 0` → chờ 60s → lặp lại (vô hạn).

Để dừng hẳn: user tắt `enabled` hoặc bấm ⏹ Dừng.

### 12.4. User bấm Dừng

`_stop` event check tại:
- Đầu vòng `while`
- Giữa vòng `for` (mỗi nhà)
- Trong vòng chờ `total_wait`
- Trong vòng chờ 60s (OCR fail)

### 12.5. Xung đột ADB

Toàn bộ phần dùng ADB (vào thành → xây → ra thành) nằm trong `ADB_LOCK`. Phần chờ (sleep) nằm ngoài lock → các engine khác dùng ADB thoải mái.

---

## Phụ lục: Sequence Diagram (1 chu kỳ)

```
GUI Thread          BuildEngine Thread      ADB Lock        ADB Device
    │                      │                    │                │
    │  ▶ Bắt đầu xây     │                    │                │
    │─────────────────────►│                    │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  is_in_city()──────────────────────►│
    │                      │  tap vào thành─────────────────────►│ Vào thành
    │                      │                    │                │
    │                      │  === Nhà 1 ===     │                │
    │                      │  lv < target? ✅   │                │
    │                      │  tap nhà───────────────────────────►│
    │                      │  tap Info──────────────────────────►│
    │                      │  screenshot + OCR──────────────────►│ Đọc TG
    │                      │  tap Build─────────────────────────►│ Xây!
    │                      │  lv++ (3→4)        │                │
    │                      │  tap Back──────────────────────────►│
    │                      │                    │                │
    │                      │  === Nhà 2 ===     │                │
    │                      │  lv >= target? 🏆  │                │
    │                      │  skip (continue)   │                │
    │                      │                    │                │
    │                      │  === Nhà 3 ===     │                │
    │                      │  builders >= max?  │                │
    │                      │  break             │                │
    │                      │                    │                │
    │                      │  tap ra thành──────────────────────►│ Ra thành
    │                      │  release(lock)────►│                │
    │  ◄─ on_refresh ──── │                    │  (lock rảnh)   │
    │  update treeview     │                    │                │
    │                      │  ⏳ Chờ total_wait │                │
    │                      │  (không cần lock)  │                │
    │                      │  .......           │                │
    │                      │  log "Còn 00:05:00"│                │
    │                      │  .......           │                │
    │                      │  log "Hết TG xây"  │                │
    │                      │                    │                │
    │                      │  === Chu kỳ mới == │                │
    │                      │  acquire(lock)─────►│                │
    │                      │  ...               │                │
```
