# Luồng hoạt động Tab Auto Lv (Auto Level Up Flow)

> **Phiên bản**: 2.1  
> **Module liên quan**: `gui.py` (method `_autolv_run`)  
> **Không có engine riêng** — logic chạy trực tiếp trong GUI thread  
> **Entry point**: `GUI._autolv_start()` → `threading.Thread(target=_autolv_run)`

---

## Mục lục

1. [Tổng quan luồng chính](#1-tổng-quan-luồng-chính)
2. [Cấu hình UI](#2-cấu-hình-ui)
3. [Vòng lặp chính (Chu kỳ)](#3-vòng-lặp-chính-chu-kỳ)
   - 3.1 [Check CAPTCHA](#31-check-captcha)
   - 3.2 [Vào thành](#32-vào-thành)
   - 3.3 [Mở menu Training](#33-mở-menu-training)
   - 3.4 [Scroll phải + Tap Row 1](#34-scroll-phải--tap-row-1)
   - 3.5 [Scroll trái + Tap Row 2](#35-scroll-trái--tap-row-2)
   - 3.6 [OCR thời gian hoàn thành](#36-ocr-thời-gian-hoàn-thành)
   - 3.7 [Thoát thành](#37-thoát-thành)
   - 3.8 [Chờ hết thời gian](#38-chờ-hết-thời-gian)
4. [Toạ độ cố định](#4-toạ-độ-cố-định)
5. [Tham số cấu hình](#5-tham-số-cấu-hình)
6. [Safety Gates](#6-safety-gates)
7. [State & UI Updates](#7-state--ui-updates)

---

## 1. Tổng quan luồng chính

```
User bấm ▶ Bắt đầu
  │
  ├─ Validate ADB kết nối
  │
  ├─ Thread mới → _autolv_run()
  │    │
  │    └─ WHILE không dừng:
  │         │
  │         ├─ Check CAPTCHA → nếu có → dừng tất cả
  │         │
  │         ├─ Vào thành (nếu đang ở ngoài)
  │         │    └─ Tap (54, 1216), chờ 2s
  │         │
  │         ├─ Mở menu Training
  │         │    ├─ Tap (414, 704), chờ 1.5s
  │         │    └─ Tap (522, 234), chờ 1.5s
  │         │
  │         ├─ Scroll phải + Tap Row 1 (4 nút + confirm)
  │         │    ├─ Swipe 10%→85% ngang (kéo trái→phải), chờ 1s
  │         │    ├─ Tap confirm (558, 789)
  │         │    └─ Tap 4 nút: (198,490)(306,490)(414,490)(522,490)
  │         │       mỗi nút → tap confirm
  │         │
  │         ├─ Scroll trái + Tap Row 2 (4 nút + confirm)
  │         │    ├─ Swipe 85%→10% ngang (kéo phải→trái), chờ 2s
  │         │    └─ Tap 4 nút: (270,490)(378,490)(486,490)(594,490)
  │         │       mỗi nút → tap confirm
  │         │
  │         ├─ OCR đọc thời gian hoàn thành
  │         │    ├─ Crop vùng 60-85% H
  │         │    ├─ Tìm pattern H:MM:SS
  │         │    └─ Tính wait_secs
  │         │
  │         ├─ Thoát thành
  │         │    ├─ Back (342, 1216)
  │         │    └─ Tap thoát (54, 1216) nếu vẫn trong thành
  │         │
  │         └─ Chờ wait_secs
  │              ├─ Nếu > 10s → chờ, log countdown
  │              ├─ Check CAPTCHA mỗi 30s
  │              └─ Nếu ≤ 10s → lặp lại ngay (2s delay)
  │
  └─ Kết thúc → reset UI
```

---

## 2. Cấu hình UI

**File**: `gui.py` — method `_build_autolv_tab()` (line ~3363)

### Layout

```
┌─────────────────────────────────────────────────┐
│ Left Panel (220px)    │ Right Panel              │
│ ┌─────────────────┐   │ ┌─────────────────────┐  │
│ │ Auto Update Lv  │   │ │ Log                 │  │
│ │                 │   │ │                     │  │
│ │ Delay giữa bước│   │ │ [HH:MM:SS] msg...   │  │
│ │ [  800  ] ms    │   │ │ [HH:MM:SS] msg...   │  │
│ │                 │   │ │ ...                  │  │
│ │ Delay sau       │   │ │                     │  │
│ │ confirm         │   │ │                     │  │
│ │ [ 1200  ] ms    │   │ │                     │  │
│ │                 │   │ └─────────────────────┘  │
│ │ [▶ Bắt đầu]    │   │                          │
│ │ [⏹ Dừng]       │   │                          │
│ │                 │   │                          │
│ │ ● Chờ          │   │                          │
│ │ Lần tiếp: ...  │   │                          │
│ └─────────────────┘   │                          │
└─────────────────────────────────────────────────┘
```

### Widgets

| Widget | Biến | Mặc định | Mô tả |
|--------|------|----------|-------|
| Entry delay giữa bước | `_alv_delay_var` | `800` ms | Thời gian chờ giữa mỗi tap |
| Entry delay sau confirm | `_alv_confirm_delay_var` | `1200` ms | Thời gian chờ sau khi tap confirm |
| Button Bắt đầu | `_alv_btn_start` | — | Gọi `_autolv_start()` |
| Button Dừng | — | — | Gọi `_autolv_stop()` |
| Label trạng thái | `_alv_status_lbl` | `● Chờ` | Hiện trạng thái hiện tại |
| Label lần tiếp | `_alv_next_lbl` | `""` | Hiện thời gian thức dậy tiếp theo |
| Text log | `_alv_log_txt` | — | Log chi tiết từng bước |

---

## 3. Vòng lặp chính (Chu kỳ)

**File**: `gui.py` — method `_autolv_run()` (line ~3440)

Mỗi chu kỳ thực hiện upgrade tất cả skill trong menu Training, sau đó chờ đến khi xong.

### 3.1. Check CAPTCHA

```python
sc = adb.screenshot_cv2()
if self.bot.det.has_captcha_popup(sc):
    self._stop_all_captcha()  # dừng TẤT CẢ engine
    return
```

Kiểm tra đầu mỗi chu kỳ. Nếu phát hiện CAPTCHA → dừng tất cả bot (attack, wave, build, auto lv).

### 3.2. Vào thành

```python
if not adb.is_in_city():
    _tap(54, 1216, 2.0)         # tap nút vào thành
    if not adb.is_in_city():
        time.sleep(3); continue  # thử lại sau 3s
```

- Kiểm tra `is_in_city()` (pixel color check)
- Nếu ở ngoài → tap (54, 1216), chờ 2s
- Nếu vẫn không vào được → chờ 3s, bắt đầu chu kỳ mới

### 3.3. Mở menu Training

```python
_tap(414, 704, 1.5)    # Tap nhà Training
_tap(522, 234, 1.5)    # Tap tab Training trong popup
```

2 bước tap cố định để mở màn hình Training bên trong thành.

### 3.4. Scroll phải + Tap Row 1

```
 Bước │ Thao tác                           │ Chi tiết
──────┼────────────────────────────────────┼────────────────────────
  1   │ Scroll ngang sang phải              │ swipe(10%→85%, y=490, 1500ms)
  2   │ Chờ 1s                              │ kéo ngón tay trái→phải
  3   │ Tap confirm trực tiếp              │ (558, 789) — upgrade đầu tiên
  4   │ Tap (198, 490) → confirm            │ Skill 2
  5   │ Tap (306, 490) → confirm            │ Skill 3
  6   │ Tap (414, 490) → confirm            │ Skill 4
  7   │ Tap (522, 490) → confirm            │ Skill 5
```

**ROW1**: 4 nút tại `y=490`, x = `[198, 306, 414, 522]`  
**CONFIRM**: `(558, 789)` — nút xác nhận upgrade  

Mỗi nút: tap skill → chờ `delay` → tap confirm → chờ `confirm_delay`

### 3.5. Scroll trái + Tap Row 2

```
 Bước │ Thao tác                           │ Chi tiết
──────┼────────────────────────────────────┼────────────────────────
  1   │ Scroll ngang sang trái              │ swipe(85%→10%, y=490, 1500ms)
  2   │ Chờ 2s                              │ kéo ngón tay phải→trái
  3   │ Tap (270, 490) → confirm            │ Skill 6
  4   │ Tap (378, 490) → confirm            │ Skill 7
  5   │ Tap (486, 490) → confirm            │ Skill 8
  6   │ Tap (594, 490) → confirm            │ Skill 9
```

**ROW2**: 4 nút tại `y=490`, x = `[270, 378, 486, 594]`

### 3.6. OCR thời gian hoàn thành

```python
# Crop vùng 60%-85% chiều cao, toàn bộ chiều ngang
crop = img[int(hh*0.60):int(hh*0.85), 0:ww]
# Scale 3x + grayscale + OTSU threshold
text = pytesseract.image_to_string(thresh, config="--psm 6")
# Tìm pattern H:MM:SS hoặc "requires H:MM:SS"
m = re.search(r'requires[:\s]+(\d+):(\d{2}):(\d{2})', text)
if not m:
    m = re.search(r'(\d+):(\d{2}):(\d{2})', text)
# Tính giây: H*3600 + MM*60 + SS
```

- Crop vùng giữa-dưới màn hình (60-85% H)
- Scale up 3x để OCR rõ hơn
- Tìm format `H:MM:SS` hoặc `requires H:MM:SS`
- Chuyển sang giây (`wait_secs`)

### 3.7. Thoát thành

```python
_tap(342, 1216, 1.0)     # Back
if adb.is_in_city():
    _tap(54, 1216, 1.5)  # Tap thoát thành
```

- Tap Back trước, nếu vẫn trong thành thì tap thoát thành
- Đảm bảo bot ở ngoài trước khi chờ

### 3.8. Chờ hết thời gian

```python
if wait_secs > 10:
    # Chờ đến deadline
    while time.time() < deadline:
        # UI: hiện countdown + thời gian thức dậy
        # Check CAPTCHA mỗi 30s
        time.sleep(5)
else:
    # Không đọc được TG → thử lại ngay (2s delay)
    time.sleep(2)
```

- Nếu OCR đọc được TG > 10s → chờ đúng số giây đó
- Trong lúc chờ:
  - Cập nhật UI mỗi 5s (countdown + thời gian thức dậy)
  - Check CAPTCHA mỗi 30s
- Nếu không đọc được TG → lặp lại chu kỳ ngay (delay 2s)

---

## 4. Toạ độ cố định

| Toạ độ | Mục đích | Ghi chú |
|--------|----------|---------|
| `(54, 1216)` | Nút vào/thoát thành | Dùng chung với các tab khác |
| `(342, 1216)` | Nút Back | Dùng chung |
| `(414, 704)` | Tap nhà Training | Trong thành |
| `(522, 234)` | Tap tab Training | Trong popup nhà |
| `(558, 789)` | Nút Confirm upgrade | Nút xác nhận |
| `(198, 490)` | ROW1 - Skill 2 | Sau scroll lần 1 |
| `(306, 490)` | ROW1 - Skill 3 | |
| `(414, 490)` | ROW1 - Skill 4 | |
| `(522, 490)` | ROW1 - Skill 5 | |
| `(270, 490)` | ROW2 - Skill 6 | Sau scroll lần 2 |
| `(378, 490)` | ROW2 - Skill 7 | |
| `(486, 490)` | ROW2 - Skill 8 | |
| `(594, 490)` | ROW2 - Skill 9 | |

---

## 5. Tham số cấu hình

| Tham số | Mặc định | Mô tả |
|---------|----------|-------|
| `delay` | `800` ms | Thời gian chờ giữa mỗi tap (ms) |
| `confirm_delay` | `1200` ms | Thời gian chờ sau tap confirm (ms) |

> Cả hai đều nhập từ UI, không lưu vào config file.

---

## 6. Safety Gates

### 6.1. CAPTCHA

Kiểm tra tại 2 thời điểm:
1. **Đầu mỗi chu kỳ** — trước khi vào thành
2. **Trong lúc chờ** — mỗi 30s

Nếu phát hiện → gọi `_stop_all_captcha()`:
- Dừng BotEngine (tab Tấn công)
- Dừng tất cả WaveGroupEngine (tab Nhiều đợt)
- Dừng BuildEngine (tab Xây dựng)
- Dừng Auto Lv
- Beep cảnh báo

### 6.2. Không vào được thành

Nếu `is_in_city()` vẫn `False` sau khi tap → chờ 3s → bắt đầu chu kỳ mới (không dừng bot).

### 6.3. OCR thất bại

Nếu không đọc được thời gian → `wait_secs = 0` → bot lặp lại chu kỳ ngay (delay 2s), không dừng.

---

## 7. State & UI Updates

### Trạng thái UI

| Trạng thái | Label | Màu | Khi nào |
|------------|-------|-----|---------|
| Chờ | `● Chờ` | muted | Khởi tạo |
| Đang chạy | `⚙ Chu kỳ N` | yellow | Đang tap upgrade |
| Chờ hết TG | `⏳ Còn Xg XXp XXs` | accent | Đang countdown |
| Đã dừng | `⏹ Đã dừng` | muted | User bấm Dừng |
| Lỗi ADB | `❌ Chưa kết nối ADB` | red | ADB chưa connect |

### Log

- Log vào cả 2 nơi: `_alv_log_txt` (tab Auto Lv) và log chung (`_log`)
- Format: `[HH:MM:SS] message`
- Log chi tiết từng tap, scroll, OCR result, countdown

---

## Phụ lục: Sơ đồ thao tác trong thành

```
Màn hình ngoài
  │
  ├─ Tap (54, 1216) → VÀO THÀNH
  │
  ├─ Tap (414, 704) → MỞ NHÀ TRAINING
  │
  ├─ Tap (522, 234) → TAB TRAINING
  │
  ├─ Swipe phải (10%→85%) → HIỆN ROW 1
  │    ├─ Tap Confirm (558, 789)
  │    ├─ Tap (198,490) → Confirm
  │    ├─ Tap (306,490) → Confirm
  │    ├─ Tap (414,490) → Confirm
  │    └─ Tap (522,490) → Confirm
  │
  ├─ Swipe trái (85%→10%) → HIỆN ROW 2
  │    ├─ Tap (270,490) → Confirm
  │    ├─ Tap (378,490) → Confirm
  │    ├─ Tap (486,490) → Confirm
  │    └─ Tap (594,490) → Confirm
  │
  ├─ OCR đọc thời gian (crop 60-85% H)
  │
  ├─ Back (342, 1216)
  │
  └─ Tap (54, 1216) → THOÁT THÀNH → chờ wait_secs
```
