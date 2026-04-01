# Tab Debug — Công cụ kiểm tra & Debug

> **Phiên bản**: 2.1  
> **File**: `gui.py` — method `_build_debug_tab()` + các method `_check_*`, `_test_*`, `_detect_*`  
> **Không có engine riêng** — tất cả chạy trực tiếp trong GUI thread (qua `threading.Thread`)

---

## Mục lục

1. [Tổng quan](#1-tổng-quan)
2. [Giao diện](#2-giao-diện)
3. [Nhóm 1: Kiểm tra ADB & Màn hình](#3-nhóm-1-kiểm-tra-adb--màn-hình)
   - 3.1 [⚔️ Check icon ATK](#31-️-check-icon-atk)
   - 3.2 [🧩 Check CAPTCHA](#32--check-captcha)
   - 3.3 [🪟 Detect Popup](#33--detect-popup)
   - 3.4 [🏙 Test City](#34--test-city)
   - 3.5 [🪖 Check quân](#35--check-quân)
   - 3.6 [🧠 Check máu quân](#36--check-máu-quân)
4. [Nhóm 2: AI & OCR](#4-nhóm-2-ai--ocr)
   - 4.1 [🤖 Test AI (Ollama)](#41--test-ai-ollama)
   - 4.2 [🔍 Test OCR](#42--test-ocr)
5. [Tuỳ chọn: Debug mode](#5-tuỳ-chọn-debug-mode)
6. [Debug Log](#6-debug-log)
7. [Thông tin nhanh](#7-thông-tin-nhanh)
8. [File debug được tạo](#8-file-debug-được-tạo)

---

## 1. Tổng quan

Tab Debug gom tất cả công cụ kiểm tra thủ công vào 1 nơi. Không tự động chạy — user bấm nút → chụp ảnh → xử lý → hiện kết quả trên log. Mỗi nút chạy trong thread riêng, không block UI.

**Mục đích**: kiểm tra ADB, template matching, OCR, AI, popup detection trước khi chạy bot. Giúp debug khi bot gặp lỗi.

---

## 2. Giao diện

```
┌─────────────────────────────────────────────────────────────────┐
│ 🐛  Công cụ Debug & Kiểm tra                                    │
├────────────────────────┬────────────────────────────────────────┤
│ Left (340px)           │ Right                                   │
│                        │                                         │
│ Kiểm tra ADB &        │ Debug Log                               │
│ Màn hình               │ ┌─────────────────────────────────┐    │
│ ┌────────────────────┐ │ │ [Popup] ✅ detect = territory   │    │
│ │ [⚔️ Check icon ATK]│ │ │ [CAPTCHA] ✅ Không thấy         │    │
│ │ [🧩 Check CAPTCHA] │ │ │ [City] 🏰 Đang TRONG thành     │    │
│ │ [🪟 Detect Popup]  │ │ │ [Army] auto1 → Standby          │    │
│ │ [🏙 Test City]     │ │ │ [ArmyHP] auto1 | HP 85.3%      │    │
│ │ [🪖 Check quân]    │ │ │ [OCR Test] 12 dòng:             │    │
│ │ [🧠 Check máu quân]│ │ │ ...                              │    │
│ └────────────────────┘ │ └─────────────────────────[🗑 Xóa]┘    │
│                        │                                         │
│ AI & OCR               │ Thông tin nhanh                         │
│ ┌────────────────────┐ │ ┌─────────────────────────────────┐    │
│ │ [🤖 Test AI]       │ │ │ assets/  Template PNG            │    │
│ │ [🔍 Test OCR]      │ │ │ debug/   Ảnh debug               │    │
│ └────────────────────┘ │ │ config/  settings.json            │    │
│                        │ └─────────────────────────────────┘    │
│ Tuỳ chọn              │                                         │
│ [✓] Lưu ảnh debug     │                                         │
└────────────────────────┴────────────────────────────────────────┘
```

Mỗi nút có tooltip mô tả ngắn bên cạnh.

---

## 3. Nhóm 1: Kiểm tra ADB & Màn hình

### 3.1. ⚔️ Check icon ATK

**Method**: `_check_atk_icon()`  
**Luồng**: Chụp ảnh → `detector.find_atk_btn(screen)` → template matching  
**Kết quả**: Toạ độ icon ATK hoặc "Không tìm thấy (threshold 0.65)"

```
[ATK] Chụp màn hình...
[ATK] Tìm thấy icon ATK tại: (234, 567)
```

**Dùng khi**: Kiểm tra bot có nhận ra icon tấn công trên bản đồ không.

---

### 3.2. 🧩 Check CAPTCHA

**Method**: `_check_captcha_debug()`  
**Luồng**:
1. Chụp ảnh
2. `detector.has_captcha_popup(screen)` — OCR vùng giữa tìm "Random Test"
3. Lưu vùng ROI (15-55% H, 10-90% W) ra `debug/captcha_check_HHMMSS.png`

**Kết quả**:
```
[CAPTCHA] 🚨 PHÁT HIỆN popup CAPTCHA / Random Test
  — hoặc —
[CAPTCHA] ✅ Không thấy popup CAPTCHA
[CAPTCHA] ROI debug: debug/captcha_check_113500.png
```

**Dùng khi**: Kiểm tra detector CAPTCHA có hoạt động đúng không. Ảnh ROI giúp xem vùng OCR thực tế.

---

### 3.3. 🪟 Detect Popup

**Method**: `_detect_popup_type()`  
**Luồng**:
1. Chụp ảnh
2. `detector.detect_popup_type(screen)` — phân loại popup hiện tại
3. Lưu ảnh ra `debug/popup_type_HHMMSS.png`

**Các loại popup nhận diện:**

| Kết quả | Ý nghĩa | Detector method |
|---------|---------|-----------------|
| `captcha` | Popup CAPTCHA / Random Test | `has_captcha_popup()` |
| `territory_map` | Popup bản đồ lãnh thổ | `_is_territory_map_popup()` |
| `army_selection` | Popup chọn quân (sau Click Chiếm) | `_is_army_selection_popup()` |
| `all_armies` | Popup All Armies | `_is_all_armies_popup()` |
| `lucky_wheel` | Popup Lucky Wheel (vòng quay) | `_is_lucky_wheel_popup()` |
| `other` | Có popup nhưng không nhận ra loại | `_has_generic_popup()` |
| `""` (rỗng) | Không có popup nào | — |

**Kết quả**:
```
[Popup] ✅ detect_popup_type = army_selection
  — hoặc —
[Popup] ℹ Không thấy popup
```

**Dùng khi**: Debug flow tấn công — kiểm tra bot nhận popup đúng loại không. Quan trọng cho bước "Click Chiếm → chờ army_selection".

---

### 3.4. 🏙 Test City

**Method**: `_test_city()`  
**Luồng**: `adb.is_in_city()` — check pixel color tại vị trí cố định

**Kết quả**:
```
[City] 🏰 Đang TRONG thành (nút xanh lá)
  — hoặc —
[City] 🗺 Đang NGOÀI thành (nút nhiều màu)
```

**Dùng khi**: Kiểm tra logic vào/ra thành. Các engine (attack, build, spin, autolv) đều dựa vào `is_in_city()` để quyết định có cần tap vào/ra thành không.

---

### 3.5. 🪖 Check quân

**Method**: `_check_army()`  
**Luồng**:
1. Tap (666, 1216) — mở All Armies
2. `adb.read_army_status(required_names)` — OCR đọc tên + trạng thái
3. Tap (342, 1216) — Back
4. Log từng quân + tổng hợp

**Kết quả**:
```
[Army] 🔍 Mở màn hình quân...
[Army] auto1       → Standby
[Army] auto2       → Marching
[Army] auto3       → Standby
[Army] Tổng: 3 | Standby:2 | Marching:1
```

**Dùng khi**: Kiểm tra OCR đọc đúng tên quân + trạng thái. Cần khớp với `troop_names` trong config.

---

### 3.6. 🧠 Check máu quân

**Method**: `_check_army_health()`  
**Luồng**:
1. Lưu config UI trước
2. Tap (666, 1216) — mở All Armies
3. `adb.read_army_health(required_names)` — đọc thanh máu đỏ
4. Tap (342, 1216) — Back
5. `brain.summarize(troops, next_label)` — chạy Brain rule engine
6. Log chi tiết từng quân + khuyến nghị

**Kết quả**:
```
[ArmyHP] 🔍 Mở màn hình quân để đọc thanh máu...
[ArmyHP] auto1     | HP  85.3% | min  72.0% | crit 0/5 | risk 1 | continue
[ArmyHP] auto2     | HP  45.2% | min  20.0% | crit 2/5 | risk 4 | heal_now
[ArmyHP] 🩸 Khuyến nghị: HEAL NOW
```

**Các chỉ số:**

| Chỉ số | Mô tả |
|--------|-------|
| HP | HP trung bình (%) |
| min | HP unit thấp nhất |
| crit | Số unit critical / tổng unit |
| risk | Điểm rủi ro (0-5) |
| action | `continue` / `low_risk_only` / `heal_now` |

**Khuyến nghị tổng hợp:**

| Kết quả | Ý nghĩa |
|---------|---------|
| ✅ tiếp tục bình thường | Máu đủ, đánh thoải mái |
| ⚠️ chỉ đánh mục tiêu nhẹ | HP < `health_warn_avg` (55%) |
| 🩸 HEAL NOW | HP < `health_force_heal_avg` (30%) hoặc ≥ 2 unit critical |

**Dùng khi**: Kiểm tra Brain hoạt động đúng, calibrate ngưỡng heal.

---

## 4. Nhóm 2: AI & OCR

### 4.1. 🤖 Test AI (Ollama)

**Method**: `_test_ai()`  
**Luồng**:
1. Chụp ảnh → lưu `debug/ai_test_HHMMSS.png`
2. Tạo `OllamaVision(url, model)` → gọi `is_occupying(raw)`
3. Log kết quả + raw response

**Kết quả**:
```
[AI Test] Chụp ảnh...
[AI Test] Ảnh lưu: debug/ai_test_113500.png
[AI Test] Gửi lên qwen2-vl:7b...
[AI Test] ✅ KẾT QUẢ: Đang chiếm (YES)
[AI Test] Raw: The image shows troops occupying territory...
```

**Yêu cầu**: Ollama đang chạy local, model đã pull.

**Dùng khi**: Kiểm tra AI vision nhận diện đúng trạng thái chiếm đất.

---

### 4.2. 🔍 Test OCR

**Method**: `_test_ocr()`  
**Luồng**:
1. Chụp ảnh → scale 2x → grayscale → OTSU threshold
2. `pytesseract.image_to_string(thresh, config="--psm 6")`
3. Lưu `debug/ocr_test_HHMMSS.png` (ảnh threshold) + `debug/ocr_test_HHMMSS.txt` (text)
4. Log từng dòng text

**Kết quả**:
```
[OCR Test] Chụp ảnh...
[OCR Test] 📄 12 dòng:
[OCR Test]   Today's chance(s) left:4
[OCR Test]   Take a break 03:14
[OCR Test]   Skip
...
```

**Dùng khi**: Xem OCR đọc được gì từ màn hình hiện tại. Debug khi bot OCR sai (vd: không nhận timer, tên quân).

---

## 5. Tuỳ chọn: Debug mode

```
[✓] Lưu ảnh debug (debug/)
    Khi bật: lưu screenshot, OCR result,
    template match vào thư mục debug/
```

**Checkbox sync** giữa tab Debug và tab Tấn công (cùng biến `cfg.debug_mode`). Bật ở tab nào cũng cập nhật tab kia.

**Khi bật**: các engine (attack, build, wave) lưu thêm ảnh debug vào `debug/` tại mỗi bước quan trọng. Tắt = không lưu (tiết kiệm disk).

---

## 6. Debug Log

Panel phải hiển thị log riêng cho các message debug. Lọc theo tag prefix:

```python
_is_debug_msg(msg) → True nếu msg chứa bất kỳ tag nào:
  [Popup], [CAPTCHA], [ATK], [Army], [ArmyHP],
  [City], [AI Test], [OCR Test], [OCR], [💰 OCR],
  [Test], [TestNav]
```

| Thành phần | Mô tả |
|------------|-------|
| Text widget | `debug_log_txt`, height=18, Consolas 8 |
| Nút xoá | 🗑 Xóa log debug — xoá toàn bộ text |
| Filter | Chỉ hiện message có tag debug (không hiện log attack/build/wave) |

---

## 7. Thông tin nhanh

| Thư mục | Mô tả |
|---------|-------|
| `assets/` | Chứa template PNG (btn_atk.png, btn_capture.png, ...) |
| `debug/` | Ảnh debug được lưu khi bật Debug mode |
| `config/` | File cấu hình settings.json, scout map, spy data |

---

## 8. File debug được tạo

Các nút debug tạo file trong thư mục `debug/`:

| Nút | File tạo | Nội dung |
|-----|----------|---------|
| 🧩 Check CAPTCHA | `captcha_check_HHMMSS.png` | Ảnh vùng ROI (15-55% H) |
| 🪟 Detect Popup | `popup_type_HHMMSS.png` | Ảnh toàn màn hình |
| 🤖 Test AI | `ai_test_HHMMSS.png` | Ảnh gốc gửi lên Ollama |
| 🔍 Test OCR | `ocr_test_HHMMSS.png` | Ảnh sau threshold |
| 🔍 Test OCR | `ocr_test_HHMMSS.txt` | Text OCR kết quả |

> `HHMMSS` = giờ phút giây tại thời điểm chụp. VD: `popup_type_113500.png`

---

## Phụ lục: Tổng hợp tất cả nút Debug

| # | Nút | Method | Modules dùng | Cần ADB | Lưu file |
|---|------|--------|-------------|---------|----------|
| 1 | ⚔️ Check icon ATK | `_check_atk_icon` | `detector.find_atk_btn` | ✅ | ❌ |
| 2 | 🧩 Check CAPTCHA | `_check_captcha_debug` | `detector.has_captcha_popup` | ✅ | ✅ `.png` |
| 3 | 🪟 Detect Popup | `_detect_popup_type` | `detector.detect_popup_type` | ✅ | ✅ `.png` |
| 4 | 🏙 Test City | `_test_city` | `adb.is_in_city` | ✅ | ❌ |
| 5 | 🪖 Check quân | `_check_army` | `adb.read_army_status` | ✅ | ❌ |
| 6 | 🧠 Check máu quân | `_check_army_health` | `adb.read_army_health` + `brain.summarize` | ✅ | ❌ |
| 7 | 🤖 Test AI | `_test_ai` | `OllamaVision.is_occupying` | ✅ | ✅ `.png` |
| 8 | 🔍 Test OCR | `_test_ocr` | `pytesseract.image_to_string` | ✅ | ✅ `.png` + `.txt` |

> Tất cả nút đều chạy trong `threading.Thread(daemon=True)` → không block UI.  
> Tất cả đều kiểm tra `adb.ok` trước, nếu chưa kết nối thì thử `adb.connect()`.
