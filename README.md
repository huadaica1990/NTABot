# Nine Acres Bot — Grid Attack Edition (v2.2)

Công cụ tự động hoá game trên Android emulator (LDPlayer) qua ADB.  
Phiên bản 2.2: Telegram điều khiển từ xa, cooldown per-group, popup check trước đóng, auto-continue wave.

---

## Yêu cầu

- **Python** 3.10+
- **LDPlayer9**: Settings → Others → Enable ADB (port 5555)
- **Packages**: `pip install opencv-python pillow pytesseract`

---

## Cấu trúc thư mục

```
nine_acres_bot/
│
├── __init__.py            # Package init, re-exports chính
├── __main__.py            # Cho phép chạy: cd NTA && python main.py
├── main.py                # Entry point
│
│── ─── CẤU HÌNH ──────────────────────────────────────────
├── config.py              # Paths, State enum, data classes, theme
│                          #   State, AttackPoint, WaveGroup
│                          #   BotConfig, BuildConfig, BuildTask
│                          #   Theme colors (C), STATE_COLOR
│                          #   GRID_COLS, GRID_ROWS
│
│── ─── TẦNG THẤP (Low-level) ─────────────────────────────
├── adb_controller.py      # class ADB
│                          #   connect, tap, swipe, hold
│                          #   screenshot_bytes, screenshot_cv2
│                          #   clear_and_type (nhập số qua keyevent)
│                          #   is_in_city (check pixel màu)
│                          #   read_army_status (OCR trạng thái quân)
│                          #   read_army_health (phân tích thanh máu đỏ)
│                          #   get_screen_size
│
├── ollama_vision.py       # class OllamaVision
│                          #   is_occupying() — check đang chiếm qua AI
│
├── detector.py            # class Detector
│                          #   Template matching: find_capture_btn, find_build_btn,
│                          #     find_ok_btn, find_xem_btn, find_hanh_quan_btn,
│                          #     find_atk_btn
│                          #   Color detection: has_error_popup, has_captcha_popup
│                          #   Popup detection: detect_popup_type()
│                          #     → captcha / territory_map / army_selection
│                          #     → all_armies / other / rỗng
│                          #   Checkbox: detect_checkboxes, is_checked
│                          #   OCR: read_troop_name, fuzzy_match_name,
│                          #     read_march_time, find_xy_fields
│
├── tile_ocr.py            # Tile OCR helpers (hàm thuần)
│                          #   _tile_extract_name, _tile_ocr_region
│                          #   _tile_ocr_buttons, _classify_tile_from_text
│                          #   analyze_tile_image (hàm chính)
│
│── ─── LOGIC NGHIỆP VỤ ──────────────────────────────────
├── brain.py               # class TroopBrain
│                          #   analyze() — phân tích 1 đạo quân
│                          #   summarize() — tổng hợp tất cả quân
│                          #   Quyết định: continue / low_risk_only / heal_now
│
├── troop_selector.py      # class TroopSelector
│                          #   select() — chọn quân tự động
│                          #   OCR tên + fuzzy match → click checkbox
│                          #   Fallback positional khi OCR fail
│                          #   Đọc thời gian hành quân
│
│── ─── ENGINE (Bot cores) ────────────────────────────────
├── bot_engine.py          # class BotEngine — Engine tấn công chính
│                          #   start/stop, navigate_to, _attack
│                          #   _cooldown (theo level: lv1-lv4)
│                          #   _do_heal (hồi máu tự động)
│                          #   _setup_heal_point (đặt điểm hồi)
│                          #   _check_captcha, _check_required_armies
│                          #   Brain integration (scan HP trước mỗi điểm)
│
├── build_engine.py        # class BuildEngine — Xây dựng tự động
│                          #   Vào thành → tap nhà → info → build
│                          #   OCR đọc thời gian xây
│                          #   Auto loop: xây → chờ → xây tiếp
│                          #   build_single(task) — xây nhanh 1 nhà
│
├── wave_engine.py         # class WaveGroupEngine — Nhiều đợt song song
│                          #   Mỗi nhóm có quân + điểm riêng
│                          #   _WAVE_ADB_LOCK: chỉ 1 nhóm dùng ADB/lần
│                          #   Cooldown theo nhãn level
│                          #   skip_cooldown() — bỏ qua cooldown/hành quân
│                          #   CAPTCHA detection
│
├── scout_engine.py        # class ScoutEngine — Dò bản đồ
│                          #   TILE_LABELS dict
│                          #   Spiral scan từ mốc ra ngoài
│                          #   OCR nhận dạng loại ô
│
├── spin_engine.py         # class SpinEngine — Quay thưởng tự động
│                          #   Lucky Wheel: OCR vùng cụ thể
│                          #   _read_spin_status() — crop 2 vùng riêng
│                          #   Cooldown cứng 10 phút giữa mỗi lần
│                          #   Sync spins_done từ OCR chance(s) left
│
├── telegram_bot.py        # class TelegramBot — Điều khiển từ xa qua Telegram
│                          #   Long-polling + inline keyboard
│                          #   Lệnh: /status, /screenshot, /tap, /wave_start, /stop_all
│                          #   Inline buttons: tap ADB → gửi screenshot
│                          #   Notify: CAPTCHA (kèm ảnh), error (kèm ảnh), done
│
│── ─── GIAO DIỆN ─────────────────────────────────────────
├── grid_picker.py         # class GridPicker (Toplevel)
│                          #   Click chọn điểm trên ảnh screenshot
│                          #   Snap to grid, add/remove points
│
└── gui.py                 # class GUI (Tk) — Cửa sổ chính
                           #   Tab 1: ⚔ Tấn công (attack)
                           #     - Treeview: cột ✅ Done / ⏳ Wait (click nhanh)
                           #   Tab 2: 🏗 Xây dựng (build)
                           #     - Treeview: cột 🔨 Xây (click = xây nhanh 1 nhà)
                           #     - Nút 🔄 Cài lại Lv1
                           #   Tab 3: 📦 Tài nguyên (resources)
                           #   Tab 4: 🌊 Nhiều đợt (wave)
                           #   Tab 5: 🗺 Dò bản đồ (scout)
                           #   Tab 6: 🔍 Do thám (spy)
                           #   Tab 7: ⬆ Auto Lv (auto update level)
                           #   Tab 8: 🎰 Quay thưởng (spin)
                           #   Tab 9: 🐛 Debug
                           #   Config load/save, profile management
```

---

## Sơ đồ phụ thuộc (Dependency Graph)

```
config.py  ←──────────────────────────────── (tất cả module đều import)
   │
   ├── adb_controller.py
   ├── ollama_vision.py
   ├── detector.py
   ├── tile_ocr.py
   ├── brain.py
   │
   ├── troop_selector.py  ←── adb_controller, detector
   │
   ├── bot_engine.py      ←── adb_controller, detector, troop_selector,
   │                          ollama_vision, brain, tile_ocr
   │
   ├── build_engine.py    ←── (config only, dùng ADB qua injection)
   │
   ├── wave_engine.py     ←── adb_controller, detector, troop_selector, tile_ocr
   │
   ├── scout_engine.py    ←── tile_ocr
   │
   ├── spin_engine.py     ←── (config only, dùng ADB qua injection)
   │
   ├── telegram_bot.py    ←── (stdlib only: urllib, json, threading)
   │
   ├── grid_picker.py     ←── (config only)
   │
   └── gui.py             ←── (tất cả modules trên)
         └── main.py
```

---

## Cách chạy

```bash
# Cách 1: Module mode
cd NTA && python main.py

# Cách 2: Chạy trực tiếp
python main.py
```

---

## Workflow chính

### Tab Tấn công (Attack)
1. **Connect ADB** — kết nối emulator
2. **Screenshot** — chụp màn hình, hiện grid
3. **Chọn điểm** — click trên bản đồ hoặc nhập tọa độ
4. **RUN** — bot tự động:
   - Navigate đến tọa độ → chờ popup Chiếm
   - Click Chiếm → chọn quân (OCR + fuzzy match)
   - OK → chờ hành quân → cooldown → điểm tiếp

### Tab Xây dựng (Build)
- Vào thành → tap nhà → info → build → chờ timer → lặp lại
- Tự tăng level, dừng khi đạt mục tiêu

### Tab Nhiều đợt (Wave)
- Tạo nhiều nhóm quân, mỗi nhóm có danh sách điểm riêng
- Chạy song song, dùng lock ADB để tránh xung đột
- Mỗi nhóm có nút: ▶ Chạy / ⏭️ Tiếp (skip cooldown) / ⏹ Dừng
- Auto heal: cấu hình heal_every, heal_x, heal_y riêng cho từng nhóm
- Cooldown riêng từng nhóm: CD(s), Lv2, Lv3, Lv4 (0 = dùng global config)
- Checkbox "Hồi máu khi hết điểm cuối" — tự heal sau điểm cuối cùng
- Nút 🧹 Xóa done — xóa nhanh tất cả điểm đã hoàn thành
- Auto-continue: hết điểm → poll 2s chờ điểm mới, không tự thoát thread
- Treeview: click cột ✅ Done / ⏳ Wait để đổi trạng thái điểm nhanh

### Telegram (Điều khiển từ xa)
- Kết nối qua Bot Token + Chat ID từ @BotFather
- Lệnh text: `/status`, `/screenshot`, `/tap X Y`, `/wave_start`, `/stop_all`
- Inline keyboard (`/tap`): các nút tap nhanh (Giữa, Back, Chiếm, March...)
- Mỗi tap → tự gửi screenshot lại kèm bàn phím → tap liên tục
- Thông báo tự động: CAPTCHA (kèm ảnh), lỗi engine (kèm ảnh), nhóm done

### Tab Dò bản đồ (Scout)
- Dò spiral từ điểm mốc, OCR nhận dạng tile
- Lưu kết quả ra file JSON theo profile

### Tab Quay thưởng (Spin)
- Tự động quay Lucky Wheel, tối đa 10 lượt/ngày
- Luồng: thoát thành → mở popup (414,1216) → OCR đọc lượt còn lại
- Mỗi lần quay: tap (234,1130) → chờ 5s → đóng popup (342,1216) → cooldown 10 phút
- OCR crop 2 vùng riêng (80-90% H, 90-99% H) để tránh rác từ vòng quay
- Dừng khi `Come back tomorrow` hoặc `chance(s) left: 0`

### Tab Debug
Công cụ kiểm tra & debug, chia 2 nhóm:

**Kiểm tra ADB & Màn hình:**
- **⚔️ Check icon ATK** — Tìm icon ATK trên màn hình bằng template matching
- **🧩 Check CAPTCHA** — Chụp ảnh và kiểm tra popup CAPTCHA / Random Test
- **🪟 Detect Popup** — Nhận diện loại popup hiện tại trên màn hình. Gọi `detector.detect_popup_type(screen)` phân loại 4 loại chính: `all_armies`, `army_selection`, `territory_map`, `captcha`; popup khác = `other`; không có popup = rỗng. Lưu ảnh vào `debug/popup_type_HHMMSS.png`
- **🏙 Test City** — Kiểm tra đang trong thành hay ngoài thành (pixel color check)
- **🪖 Check quân** — Mở All Armies, OCR đọc trạng thái từng quân
- **🧠 Check máu quân** — Đọc thanh máu + chạy Brain rule engine

**AI & OCR:**
- **🤖 Test AI (Ollama)** — Chụp ảnh → gửi Ollama → log kết quả
- **🔍 Test OCR** — Chụp ảnh → pytesseract → log text

**Tuỳ chọn:** Toggle lưu ảnh debug vào thư mục `debug/`

---

## Cấu hình (Config)

Lưu tại `config/settings.json`. Các profile riêng: `config/settings_<name>.json`.

### Các tham số quan trọng trong BotConfig:

| Tham số | Mặc định | Mô tả |
|---------|----------|-------|
| `adb_path` | auto-detect | Đường dẫn adb.exe |
| `device_serial` | emulator-5554 | Serial thiết bị ADB |
| `cooldown_sec` | 300 | Cooldown giữa 2 điểm (Lv1) |
| `lv2_cooldown_sec` | 600 | Cooldown cho điểm Lv2 |
| `lv3_cooldown_sec` | 900 | Cooldown cho điểm Lv3 |
| `lv4_cooldown_sec` | 1200 | Cooldown cho điểm Lv4 |
| `heal_every` | 5 | Hồi máu sau N lần tấn công |
| `heal_step` | 0 | Đặt điểm hồi máu mỗi N step (0=tắt) |
| `army_health_enabled` | True | Bật Brain phân tích máu quân |
| `health_force_heal_avg` | 0.30 | HP trung bình < 30% → heal ngay |
| `health_warn_avg` | 0.55 | HP < 55% → chỉ đánh mục tiêu nhẹ |
| `ollama_enabled` | False | Bật AI Ollama (local) |
| `telegram_enabled` | False | Bật điều khiển qua Telegram |
| `telegram_token` | "" | Bot token từ @BotFather |
| `telegram_chat_id` | "" | Chat ID của user |

---

## Hướng dẫn nâng cấp từng module

### Thêm loại nút mới (Detector)
Sửa `detector.py`:
- Thêm template `.png` vào `assets/`
- Thêm method `find_xxx_btn()` tương tự `find_capture_btn()`

### Thay đổi logic chọn quân
Sửa `troop_selector.py`:
- `_do_select()` — logic chính OCR + fuzzy match
- `fuzzy_match_name()` (trong Detector) — thêm rule match

### Thay đổi logic tấn công
Sửa `bot_engine.py`:
- `_attack()` — flow chính 1 điểm
- `_cooldown()` — logic chờ giữa 2 điểm
- `_do_heal()` — flow hồi máu (chờ 3s trước tap March)

### Thay đổi logic xây dựng
Sửa `build_engine.py`:
- `_run()` — vòng lặp xây tự động (tất cả nhà enabled)
- `build_single(task)` — xây nhanh 1 nhà (gọi từ GUI click cột 🔨)
- `_try_build(task)` — thao tác tap nhà → info → build

### Thay đổi logic quay thưởng
Sửa `spin_engine.py`:
- `_read_spin_status()` — crop 2 vùng OCR (top: chance left, bot: break timer)
- `_ocr_region()` — OCR 1 vùng cụ thể (% tọa độ), scale 3x
- `SPIN_COOLDOWN` — thời gian chờ giữa mỗi lần quay (mặc định 600s)

### Thêm tab mới vào GUI
Sửa `gui.py`, method `_build()`:
```python
tab_new = tk.Frame(nb, bg=C["bg"])
nb.add(tab_new, text="🆕  Tab mới")
self._build_new_tab(tab_new)
```

### Thay thế OCR engine
Sửa `adb_controller.py`:
- `read_army_status()` — thay pytesseract bằng engine khác
- `read_army_health()` — tương tự

### Thay thế AI Vision
Sửa `ollama_vision.py`:
- Thay API endpoint, model, prompt

---

## Files dữ liệu

| File | Vị trí | Mô tả |
|------|--------|-------|
| `config/settings.json` | CONFIG_DIR | Config mặc định |
| `config/settings_<name>.json` | CONFIG_DIR | Config theo profile |
| `config/scout_map_<name>.json` | CONFIG_DIR | Bản đồ dò được |
| `config/spy_data_<name>.json` | CONFIG_DIR | Dữ liệu do thám |
| `assets/*.png` | ASSETS_DIR | Template buttons |
| `debug/*.png` | DEBUG_DIR | Ảnh debug |
| `resources_data.json` | BASE_DIR | Data tài nguyên xây dựng |

### Tài liệu kỹ thuật

| File | Mô tả |
|------|-------|
| `README.md` | Tổng quan dự án, cấu trúc, workflow |
| `ATTACK_FLOW.md` | Luồng chi tiết Tab Tấn công |
| `BUILD_FLOW.md` | Luồng chi tiết Tab Xây dựng |
| `WAVE_FLOW.md` | Luồng chi tiết Tab Nhiều đợt |
| `SPIN_FLOW.md` | Luồng chi tiết Tab Quay thưởng |
| `AUTO_LV_FLOW.md` | Luồng chi tiết Tab Auto Lv |
| `DEBUG_FLOW.md` | Tài liệu Tab Debug — công cụ kiểm tra |
| `TELEGRAM_FLOW.md` | Luồng chi tiết Telegram điều khiển từ xa |

---

## Ghi chú phát triển

- **Phím tắt**: `Ctrl+S` = lưu tất cả cài đặt + profile hiện tại

- **GUI lớn (6000+ dòng)**: Nếu cần tách tiếp, có thể chia theo tab:
  - `gui_attack.py` — Tab Tấn công
  - `gui_build.py` — Tab Xây dựng  
  - `gui_wave.py` — Tab Nhiều đợt
  - `gui_scout.py` — Tab Dò bản đồ
  - `gui_spy.py` — Tab Do thám
  - `gui_resources.py` — Tab Tài nguyên
  - `gui_autolv.py` — Tab Auto Lv
  - `gui_spin.py` — Tab Quay thưởng

- **Thread safety**: `ADB_LOCK` (global lock trong `config.py`) đảm bảo chỉ 1 engine dùng ADB tại 1 thời điểm. Tất cả engine (attack, build, wave, spin, autolv) đều acquire lock trước khi dùng ADB, release trước khi chờ (cooldown/timer). Khi tab A đang dùng ADB, tab B đến hẹn sẽ chờ tab A xong rồi mới chạy.

- **CAPTCHA**: Tất cả engine đều check CAPTCHA, dừng + beep + gửi Telegram (nếu bật) khi phát hiện.

- **Popup check**: Tất cả engine (wave, bot, build, spin) đều check popup trước khi tap Back (342,1216) — tránh tap nhầm khi không có popup.

- **Error notify**: Mọi lỗi dẫn tới dừng bot đều gửi screenshot + thông báo qua Telegram (nếu bật).
