# Luồng hoạt động Telegram Bot (Telegram Remote Control)

> **Phiên bản**: 1.0  
> **Modules liên quan**: `telegram_bot.py`, `gui.py`, `config.py`  
> **Entry point**: `GUI.__init__()` → `TelegramBot.start()` → polling loop

---

## Mục lục

1. [Tổng quan](#1-tổng-quan)
2. [Kiến trúc](#2-kiến-trúc)
3. [Thiết lập](#3-thiết-lập)
4. [Lệnh Telegram](#4-lệnh-telegram)
5. [Thông báo tự động](#5-thông-báo-tự-động)
6. [Luồng xử lý](#6-luồng-xử-lý)
7. [Bảo mật](#7-bảo-mật)
8. [TAP_PRESETS](#8-tap_presets)
9. [Sequence Diagram](#9-sequence-diagram)

---

## 1. Tổng quan

Module Telegram cho phép điều khiển bot từ xa, bao gồm:

- **Xem trạng thái** tất cả nhóm wave
- **Chụp screenshot** màn hình emulator
- **Tap ADB** qua inline keyboard hoặc lệnh text → gửi screenshot kết quả
- **Điều khiển wave**: start/stop/skip nhóm
- **Nhận thông báo** tự động: CAPTCHA, error, done (kèm screenshot)

```
Telegram App ◄──► Telegram API ◄──► TelegramBot (polling) ◄──► GUI ◄──► ADB
```

Không cần cài thêm thư viện — dùng `urllib` có sẵn trong Python.

---

## 2. Kiến trúc

### 2.1. TelegramBot (module)

**File**: `telegram_bot.py`

```python
class TelegramBot:
    token:      str          # Bot token từ @BotFather
    chat_id:    str          # Chat ID được phép điều khiển
    _stop:      Event        # Dừng polling
    _thread:    Thread       # Polling thread
    _offset:    int          # Last update_id

    # Callbacks (GUI gán)
    on_command: Callable     # (cmd, args) → reply_text
    on_tap:     Callable     # (x, y) → png_bytes | None
```

**Phụ thuộc**: Chỉ stdlib (`urllib`, `json`, `threading`). Không cần `python-telegram-bot`.

### 2.2. Config

**File**: `config.py` — `BotConfig`

| Tham số | Mặc định | Mô tả |
|---------|----------|-------|
| `telegram_enabled` | `False` | Bật/tắt Telegram |
| `telegram_token` | `""` | Bot token từ @BotFather |
| `telegram_chat_id` | `""` | Chat ID của user |

### 2.3. GUI integration

**File**: `gui.py`

```python
# Khởi tạo
self.tg_bot = TelegramBot(token, chat_id, self._log)
self.tg_bot.on_command = self._telegram_on_command
self.tg_bot.on_tap = self._telegram_on_tap

# Error callbacks
bot.on_error = lambda msg: self._tg_notify_error("Tab Tấn công", msg)
eng.on_error = lambda name, msg: self._tg_notify_error(name, msg)
```

UI trong tab Config: section Telegram gồm checkbox On/Off, ô Token, ô Chat ID, nút Test.

---

## 3. Thiết lập

### Bước 1: Tạo bot Telegram

1. Mở Telegram → tìm **@BotFather**
2. Gửi `/newbot` → đặt tên → nhận **Token**

### Bước 2: Lấy Chat ID

1. Tìm **@userinfobot** hoặc **@getmyid_bot**
2. Gửi tin nhắn bất kỳ → nhận **Chat ID** (dãy số)

### Bước 3: Cấu hình trong app

1. Vào tab Config → section **📱 Telegram**
2. Nhập Token + Chat ID
3. Bật checkbox → bot tự kết nối
4. Bấm **📩 Test gửi tin** để kiểm tra

### Bước 4: Auto-start

Nếu `telegram_enabled = True` và đã có token → bot tự khởi động khi mở app (delay 500ms).

---

## 4. Lệnh Telegram

### 4.1. Lệnh text

| Lệnh | Mô tả | Trả về |
|-------|-------|--------|
| `/help` | Danh sách lệnh | Text |
| `/status` | Trạng thái tất cả nhóm wave | Text |
| `/screenshot` | Chụp màn hình | Ảnh PNG |
| `/tap X Y` | Tap toạ độ cụ thể | Ảnh PNG sau tap |
| `/wave_start <tên>` | Chạy nhóm wave | Text xác nhận |
| `/wave_stop <tên>` | Dừng nhóm wave | Text xác nhận |
| `/wave_skip <tên>` | Skip cooldown nhóm | Text xác nhận |
| `/stop_all` | Dừng tất cả engine | Text xác nhận |
| `/tap` | Hiển thị inline keyboard | Keyboard |

Tên nhóm so sánh case-insensitive.

### 4.2. Inline keyboard (/tap)

Gõ `/tap` → hiện bàn phím inline:

```
┌──────────────┬──────────────┬────────────────┐
│ 👆 Giữa      │ 🔙 Back      │ 🚪 Thoát thành │
├──────────────┼──────────────┼────────────────┤
│ 🪖 All Armies│ ⚔ Chiếm      │ 🏇 March       │
├──────────────┼──────────────┼────────────────┤
│ 🗺 Bản đồ    │ 🎰 Quay      │                │
├──────────────┴──────────────┴────────────────┤
│              📷 Screenshot                     │
└────────────────────────────────────────────────┘
```

**Flow khi bấm nút:**

1. Bấm nút → `answerCallbackQuery` (xoá loading)
2. `on_tap(x, y)` → ADB tap → sleep 0.8s → screenshot
3. Gửi ảnh PNG kèm caption + keyboard lại
4. User tap tiếp mà không cần gõ `/tap` lại

Bấm "📷 Screenshot" → `on_tap(None, None)` → chỉ chụp, không tap.

### 4.3. Tap toạ độ tùy ý

```
/tap 350 700
```

→ ADB tap (350, 700) → chờ 0.8s → screenshot → gửi ảnh.

---

## 5. Thông báo tự động

### 5.1. CAPTCHA

**Trigger**: `_stop_all_captcha()` trong GUI

```
CAPTCHA phát hiện
  → Dừng tất cả engine + beep
  → Chụp screenshot → Telegram gửi ảnh + "🚨 CAPTCHA!"
```

### 5.2. Error (dừng bot)

**Trigger**: `on_error` callback từ engine

| Engine | Lỗi | Message |
|--------|------|---------|
| WaveGroupEngine | Nhóm chưa có tên quân | "Nhóm chưa có tên quân" |
| WaveGroupEngine | Quân chính không tìm thấy | "Không tìm thấy quân 'X'" |
| WaveGroupEngine | Tên quân không khớp | "Không có quân nào khớp tên [X,Y]" |
| BotEngine | Quân chính không tìm thấy | "Không tìm thấy quân 'X'" |
| BotEngine | Điểm tấn công thất bại | "Điểm N thất bại" |

Flow: `engine error` → `_fire_error(msg)` → `GUI._tg_notify_error()` → thread riêng → screenshot + send_photo.

### 5.3. Done (hoàn thành)

```python
tg_bot.notify_done(group_name)
# → "✅ Nhóm {tên} hoàn thành tất cả điểm."
```

---

## 6. Luồng xử lý

### 6.1. Polling loop

```python
def _run(self):
    send_message("🟢 NTA Bot đã kết nối!")
    while not _stop:
        updates = getUpdates(offset, timeout=10)
        for update in updates:
            _handle_update(update)
```

Long-polling timeout 10s. `allowed_updates`: `["message", "callback_query"]`.

### 6.2. Message handler

```python
msg = update["message"]
from_chat = msg["chat"]["id"]
if from_chat != self.chat_id: return  # bảo mật

cmd, args = parse(text)
if cmd == "/help": send_message(help_text)
elif cmd == "/tap" and not args: send keyboard
else: reply = on_command(cmd, args)
```

### 6.3. Callback query handler

```python
data = cb["data"]  # "tap_center", "cb_screenshot", ...
if data == "cb_screenshot":
    png = on_tap(None, None)
    send_photo(png, reply_markup=keyboard)
elif data in TAP_PRESETS:
    label, x, y = TAP_PRESETS[data]
    png = on_tap(x, y)
    send_photo(png, caption=label, reply_markup=keyboard)
```

### 6.4. ADB tap + screenshot

**File**: `gui.py` — `_telegram_on_tap(x, y)`

```python
def _telegram_on_tap(x, y):
    if x is not None and y is not None:
        adb.tap(x, y)
        time.sleep(0.8)
    raw = adb.screenshot_bytes()
    return cv2.imencode(".png", img).tobytes()
```

`x=None, y=None` → chỉ chụp. `x, y` có giá trị → tap trước, chờ 0.8s, rồi chụp.

---

## 7. Bảo mật

- **Chat ID check**: Mọi message và callback đều kiểm tra `from_chat == self.chat_id`
- **Token không log**: Token lưu trong settings.json, không hiển thị trong log
- **Không remote escalation**: Thêm chat_id chỉ qua GUI, không qua Telegram

---

## 8. TAP_PRESETS

**File**: `telegram_bot.py`

| Key | Label | Toạ độ | Chức năng |
|-----|-------|--------|-----------|
| `tap_center` | 👆 Giữa | (270, 480) | Tap giữa màn hình |
| `tap_back` | 🔙 Back | (342, 1216) | Đóng popup |
| `tap_exit_city` | 🚪 Thoát thành | (54, 1216) | Thoát thành |
| `tap_all_armies` | 🪖 All Armies | (666, 1216) | Mở quân |
| `tap_capture` | ⚔ Chiếm | (486, 704) | Click Chiếm |
| `tap_march` | 🏇 March | (486, 746) | Click March |
| `tap_map` | 🗺 Bản đồ | (558, 1216) | Navigate |
| `tap_spin` | 🎰 Quay | (414, 1216) | Lucky Wheel |

Tuỳ chỉnh: sửa dict `TAP_PRESETS`.

---

## 9. Sequence Diagram

### 9.1. Lệnh /tap (inline keyboard)

```
User (Telegram)         TelegramBot              GUI                ADB
    │                      │                      │                  │
    │  /tap               │                      │                  │
    │─────────────────────►│                      │                  │
    │  ◄─ keyboard ───────│                      │                  │
    │                      │                      │                  │
    │  [👆 Giữa]          │                      │                  │
    │─────────────────────►│                      │                  │
    │  ◄─ answer "Tap..."  │                      │                  │
    │                      │  on_tap(270, 480) ──►│                  │
    │                      │                      │  tap(270,480) ──►│
    │                      │                      │  sleep(0.8)      │
    │                      │                      │  screenshot ────►│
    │                      │  ◄── png_bytes ──────│                  │
    │  ◄─ photo + keyboard │                      │                  │
```

### 9.2. Error notify

```
Engine                  GUI                    TelegramBot         Telegram
    │                      │                      │                  │
    │  status = "error"    │                      │                  │
    │  on_error(msg) ─────►│                      │                  │
    │                      │  _tg_notify_error()  │                  │
    │                      │  screenshot ─────────►│                 │
    │                      │  notify_error ────────►│  ──► User      │
    │                      │  send_photo ──────────►│                │
```
