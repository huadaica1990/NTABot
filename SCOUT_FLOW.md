# Luồng hoạt động Tab Do thám (Scout & Spy Flow)

> **Phiên bản**: 2.2  
> **Modules liên quan**: `scout_engine.py`, `tile_ocr.py`, `gui.py`  
> **Tab chứa 2 tính năng**: Dò bản đồ (Scout) + Do thám toạ độ (Spy)

---

## Mục lục

**PHẦN A — DÒ BẢN ĐỒ (SCOUT)**
1. [Tổng quan Scout](#1-tổng-quan-scout)
2. [Kiến trúc Scout](#2-kiến-trúc-scout)
3. [Giao diện Scout](#3-giao-diện-scout)
4. [Luồng dò cơ bản](#4-luồng-dò-cơ-bản)
5. [Pattern xoắn ốc (Spiral)](#5-pattern-xoắn-ốc-spiral)
6. [Tự dò đường thực tế (Auto Path)](#6-tự-dò-đường-thực-tế-auto-path)
7. [Tìm đường (Pathfinding)](#7-tìm-đường-pathfinding)
8. [Bản đồ Canvas](#8-bản-đồ-canvas)
9. [Bảng toạ độ đã dò](#9-bảng-toạ-độ-đã-dò)

**PHẦN B — DO THÁM TOẠ ĐỘ (SPY)**
10. [Tổng quan Spy](#10-tổng-quan-spy)
11. [Giao diện Spy](#11-giao-diện-spy)
12. [Luồng do thám](#12-luồng-do-thám)
13. [Phát hiện Truce (bảo vệ)](#13-phát-hiện-truce-bảo-vệ)

**CHUNG**
14. [OCR & Nhãn ô](#14-ocr--nhãn-ô)
15. [Lưu/Load dữ liệu](#15-lưuload-dữ-liệu)
16. [Toạ độ cố định](#16-toạ-độ-cố-định)

---

# PHẦN A — DÒ BẢN ĐỒ (SCOUT)

## 1. Tổng quan Scout

Dò toàn bộ bản đồ xung quanh 1 điểm mốc (origin). Mỗi ô: navigate → tap → OCR → phân loại (lv1-lv5, ally, enemy, terrain...). Kết quả hiện trên bản đồ canvas + bảng toạ độ.

```
User nhập mốc (574, 310) + phạm vi ↑5 ↓5 ←5 →5
  │
  └─ ScoutEngine duyệt (5+5+1) × (5+5+1) = 121 ô
       │
       FOR mỗi ô (gx, gy):
       │  ├─ navigate_to(gx, gy)
       │  ├─ tap giữa màn hình
       │  ├─ screenshot → analyze_tile_image()
       │  ├─ → label: lv1/lv2/.../enemy/ally/terrain/?
       │  └─ on_tile(gx, gy, label) → cập nhật canvas + bảng
       │
       └─ on_done() → "✅ Dò xong!"
```

---

## 2. Kiến trúc Scout

### 2.1. ScoutEngine

**File**: `scout_engine.py`

```python
class ScoutEngine:
    bot:       BotEngine         # Dùng navigate_to() + adb
    ox, oy:   int                # Toạ độ mốc
    up, down, left, right: int   # Phạm vi dò (ô)
    on_log:   Callable           # fn(str)
    on_tile:  Callable           # fn(gx, gy, label, tile_name)
    on_done:  Callable           # fn()
    _stop:    threading.Event
```

### 2.2. Nhãn ô (TILE_LABELS)

```python
TILE_LABELS = {
    "wasteland": "lv1",
    "lv.2": "lv2", "lv2": "lv2",
    "lv.3": "lv3", "lv3": "lv3",
    "lv.4": "lv4", "lv4": "lv4",
    "lv.5": "lv5", "lv5": "lv5",
}
```

Bổ sung trong quá trình OCR: `ally`, `enemy`, `enemy_city`, `terrain`, `?` (chưa rõ).

---

## 3. Giao diện Scout

```
┌──────────────────────────┬──────────────────────────────────────┐
│ LEFT                     │ RIGHT                                │
│                          │                                      │
│ Cấu hình dò bản đồ      │ Bản đồ phác thảo                    │
│  Mốc X: [574] Y: [310]  │ ┌─────────────────────────────────┐  │
│  Lên:  [5]  Xuống: [5]  │ │ Canvas 20×20                    │  │
│  Trái: [5]  Phải:  [5]  │ │  ■ ■ □ ■ ■                      │  │
│  [▶ Bắt đầu][⏹ Dừng]   │ │  ■ ● ■ □ ■   (● = mốc)         │  │
│  [🗑 Xóa][📤 CSV]       │ │  □ ■ ■ ■ □                      │  │
│  [💾 Lưu cấu hình]      │ └─────────────────────────────────┘  │
│  📐 121 ô · ⏱ ~60p      │ Legend: ■lv1 ■lv2 ■lv3 □terrain ... │
│                          │                                      │
│ 🤖 Tự dò đường thực tế  │ Danh sách ô đã dò                   │
│  Đầu: [X][Y] Cuối: [X][Y│ ┌────────────────────────────────┐  │
│  ⚡ Nhanh / 🛡 An toàn   │ │ X   │ Y   │ Loại │ Mô tả     │  │
│  [▶ Bắt đầu][⏹ Dừng]   │ │ 574 │ 310 │ lv1  │ Wasteland  │  │
│                          │ │ 575 │ 310 │ lv2  │ Lv.2       │  │
│ 🌀 Pattern Dò Xoắn Ốc  │ └────────────────────────────────┘  │
│  Gốc: [X][Y] Vòng: [3]  │ Lọc: [Tất cả ▼] [📋 Copy]         │
│  Hướng đầu: ↑↓←→        │                                      │
│  [📋 Tính nhanh]         │ Tìm đường tới tọa độ                │
│  [▶ Bắt đầu][⏹][👁]    │ Từ:  [X][Y] (trống=mốc)             │
│                          │ Đến: [X][Y]                          │
│ Log dò bản đồ            │ ⚡ Nhanh / 🛡 An toàn               │
│ ┌──────────────────────┐ │ [🔍 Tìm đường][✖ Xóa đường]        │
│ │ [Scout] 1/121 → ...  │ │                                      │
│ │ [Scout] OCR → lv1    │ │                                      │
│ └──────────────────────┘ │                                      │
└──────────────────────────┴──────────────────────────────────────┘
```

---

## 4. Luồng dò cơ bản

**File**: `scout_engine.py` — `_run()`

### 4.1. Tạo danh sách toạ độ

```python
coords = []
for dy in range(-up, down + 1):
    for dx in range(-left, right + 1):
        coords.append((ox + dx, oy - dy))   # Y giảm = lên map
```

Quét dạng lưới hình chữ nhật xung quanh mốc.

### 4.2. Duyệt từng ô

```python
for i, (gx, gy) in enumerate(coords):
    if _stop: break
    label, tile_name = _ocr_tile(gx, gy)
    on_tile(gx, gy, label, tile_name)
    time.sleep(0.3)
```

### 4.3. OCR từng ô (_ocr_tile)

```
navigate_to(gx, gy)
  → tap giữa màn hình (1 lần đầu, 2 lần sau)
  → chờ 0.4s
  → screenshot
  → analyze_tile_image(img)
  → return (label, name)
```

`analyze_tile_image()` (file `tile_ocr.py`) crop vùng title + button, OCR, phân loại nhãn.

### 4.4. Ước tính thời gian

```python
total = (up + down + 1) * (left + right + 1)
time_estimate = total * 30   # ~30s mỗi ô
```

Hiển thị real-time: `📐 121 ô · ⏱ ~60p 30s (30s/ô)`

---

## 5. Pattern xoắn ốc (Spiral)

### 5.1. Thuật toán

**Method**: `_gen_spiral(ox, oy, rings, start_dir)`

Sinh toạ độ xoắn ốc CW (chiều kim đồng hồ) từ gốc:

```
Pattern cạnh: D1, L3, U5, R5, D7, L7, U9, R9...
side_len(n) = 1 nếu n=0, còn lại = 2*(n//2+1)+1
```

4 hướng đầu: `↓ Xuống`, `↑ Lên`, `← Trái`, `→ Phải`

### 5.2. Tính nhanh (không ADB)

Bấm **📋 Tính nhanh** → tính toạ độ → hiện dialog với danh sách `x,y` có thể copy.

### 5.3. Chạy dò xoắn ốc

Bấm **▶ Bắt đầu** → dùng ScoutEngine với danh sách toạ độ spiral thay vì lưới.

### 5.4. Xem trước

Bấm **👁 Xem trước** → vẽ đường spiral lên canvas bản đồ (không dò).

---

## 6. Tự dò đường thực tế (Auto Path)

Nhập điểm đầu + điểm cuối → bot dùng BFS tìm đường trên bản đồ đã dò → di chuyển thực tế trên game dọc theo đường.

### Config

| Trường | Mô tả |
|--------|-------|
| Điểm đầu X/Y | Toạ độ bắt đầu |
| Điểm cuối X/Y | Toạ độ đích |
| Chế độ | ⚡ Nhanh (chỉ chặn enemy/ally/terrain) hoặc 🛡 An toàn (chặn thêm lv3-5) |

---

## 7. Tìm đường (Pathfinding)

### 7.1. Thuật toán BFS

**Method**: `_scout_find_path()`

```python
# BFS 4 hướng (Manhattan distance)
for dx, dy in [(0,1),(0,-1),(1,0),(-1,0)]:
    nb = (cx+dx, cy+dy)
    if nb in visited: continue
    lbl = tiles.get(nb, "")
    if lbl in blocked: continue   # ô bị chặn
    visited[nb] = cur             # parent map
    queue.append(nb)
```

### 7.2. Ô bị chặn

| Chế độ | Ô bị chặn |
|--------|-----------|
| ⚡ Nhanh | `enemy_city`, `enemy`, `ally`, `terrain`, `?`, `empty`, `""` |
| 🛡 An toàn | Như Nhanh + `lv3`, `lv4`, `lv5` |

> `""` và `empty` = ô chưa dò → không đi qua (an toàn). Chỉ đi trên ô đã dò có nhãn hợp lệ.

### 7.3. Kết quả

```
✅ ⚡ Nhanh | 17 bước
(574,310) → (575,310) → (576,310) → ... → (580,315)
```

Hiển thị đường trên canvas (màu vàng) + dialog xuất danh sách → import vào Tab Tấn công.

### 7.4. Import vào Tab Tấn công

Dialog sau khi tìm đường cho phép:
- Xem danh sách toạ độ + mô tả
- Copy CSV
- **Import trực tiếp** vào danh sách điểm Tab Tấn công / Tab Nhiều đợt

---

## 8. Bản đồ Canvas

### 8.1. Hiển thị

Canvas vẽ lưới ô vuông, mỗi ô tô màu theo nhãn:

| Nhãn | Màu | Ý nghĩa |
|------|-----|---------|
| `lv1` | `#55efc4` | Wasteland |
| `lv2` | `#00b894` | Lv.2 |
| `lv3` | `#00cec9` | Lv.3 |
| `lv4` | `#ffeaa7` | Lv.4 |
| `lv5` | `#fdcb6e` | Lv.5 |
| `ally` | `#0984e3` | Đồng minh |
| `enemy_city` | `#d63031` | Thành địch |
| `enemy` | `#ff7675` | Địch |
| `terrain` | `#dfe6e9` | Địa hình |
| `?` | `#b2bec3` | Chưa rõ |
| `""` | `#636e72` | Chưa dò |

### 8.2. Tương tác

- **Click ô** → mở dialog sửa nhãn (dropdown) + ghi chú
- Ô mốc đánh dấu `×` đỏ
- Đường pathfinding hiện đường vàng nối các ô

---

## 9. Bảng toạ độ đã dò

Treeview với 4 cột: **X**, **Y**, **Loại**, **Mô tả**.

| Tính năng | Mô tả |
|-----------|-------|
| Lọc | Dropdown: Tất cả / lv1 / lv2 / ... / terrain / ? |
| Sắp xếp | Click heading (X, Y, Loại, Mô tả) |
| Copy | 📋 Copy danh sách (clipboard) |
| Double-click | Mở dialog sửa nhãn ô |
| Màu tag | Mỗi loại có foreground color khác nhau |

---

# PHẦN B — DO THÁM TOẠ ĐỘ (SPY)

## 10. Tổng quan Spy

Khác với Scout (quét vùng), Spy theo dõi **danh sách toạ độ cụ thể** — kiểm tra loại ô + trạng thái bảo vệ (Truce) + chụp ảnh. Dùng để theo dõi thành địch, kiểm tra hết bảo vệ chưa.

```
User cấu hình:
  🏰 Thành mình: (574, 310)
  🏴 Liên minh: Darkblade, BLACK DRAGON, GLORY
  Thêm toạ độ: (500,300,"Thành A","GLORY"), (600,400,"Thành B","DRG")

  └─ Bấm ▶ Bắt đầu do thám
       │
       FOR mỗi toạ độ (x, y):
       │  ├─ navigate_to(x, y)
       │  ├─ screenshot → lưu config/{profile}/scout/{x}_{y}.png
       │  ├─ analyze_tile_image() → label, name
       │  ├─ OCR title → tìm "Truce HH:MM:SS"
       │  ├─ Phân loại: 🛡 Còn bảo vệ / ⚠️ Hết bảo vệ / 🏳️ Đã thoát
       │  └─ Cập nhật treeview (kèm KC) + auto-save
       │
       └─ ✅ Xong (N/M)

  Bấm 📐 Tính KC → cập nhật cột KC = |x-574| + |y-310|
  Click hàng → hiện ảnh screenshot + chi tiết (LM, KC, Truce...)
```

---

## 11. Giao diện Spy

```
┌───────────────────────────┬──────────────────────────────────────┐
│ LEFT                      │ RIGHT                                │
│                           │                                      │
│ Danh sách toạ độ do thám  │ Chi tiết ô đang xem                 │
│ 🏰 Thành mình X:[574]    │  📍 (500, 300) Thành A               │
│    Y:[310] [📐 Tính KC]   │  Liên minh: GLORY                    │
│ 🏴 Liên minh:             │  Loại: enemy_city                    │
│ [Darkblade, BLACK DRAGON, │  Bảo vệ: 🛡 Còn BV 05:30:00         │
│  闪耀之星, Ma Vương, GLORY]│  Khoảng cách: 17 ô                  │
│                           │  ┌────────────────────────┐          │
│  X:[  ] Y:[  ]            │  │                        │          │
│  Ghi chú:[  ] LM:[▼]     │  │  (ảnh screenshot)      │          │
│ ┌───────────────────────┐ │  │  config/prf/scout/     │          │
│ │#│ X │ Y │Note│LM │Loại│BV│KC│🗺│  │  500_300.png          │          │
│ │1│500│300│Th.A│GLORY│🛡│17│🗺│ │  └────────────────────────┘          │
│ │2│600│400│Th.B│DRG │⚠️│26│🗺│ │                                      │
│ └───────────────────────┘ │ Log                                  │
│ [✏ Sửa][🗑 Xóa][🗑 Hết] │ ┌──────────────────────────────────┐ │
│ [📋 Import CSV]           │ │ [Spy] 1/5 → (500,300) Thành A   │ │
│ [💾 Xuất CSV]             │ │ [Spy] = enemy_city | 🛡 Còn BV  │ │
│                           │ └──────────────────────────────────┘ │
│ Điều khiển                │                                      │
│  [▶ Bắt đầu][⏹ Dừng]    │                                      │
│  [💾 Lưu danh sách]       │                                      │
└───────────────────────────┴──────────────────────────────────────┘
```

### Cấu hình Thành mình

| Trường | Mô tả |
|--------|-------|
| 🏰 Thành mình X | Toạ độ X thành của mình |
| 🏰 Thành mình Y | Toạ độ Y thành của mình |
| 📐 Tính KC | Tính lại khoảng cách Manhattan tất cả điểm → cập nhật cột KC |

**Khoảng cách Manhattan**: `KC = |x - city_x| + |y - city_y|` — số ô di chuyển theo 4 hướng (lên/xuống/trái/phải).

Toạ độ thành mình lưu/load cùng file `spy_data_{profile}.json` (`city_x`, `city_y`).

### Danh sách Liên minh

Ô text nhập danh sách liên minh, phân cách bằng **dấu phẩy**:

```
🏴 Liên minh: [Darkblade, BLACK DRAGON, 闪耀之星, Ma Vương, GLORY, Phoenix]
```

- Khi nhập/sửa text → tự động parse → cập nhật dropdown Combobox "LM:" ở dòng thêm toạ độ + dialog sửa
- Lưu nguyên chuỗi vào `spy_data_{profile}.json` (`"alliances": "Darkblade, BLACK DRAGON, ..."`)
- Backward compat: nếu file cũ lưu dạng list → tự join thành chuỗi

### Treeview Spy

| Cột | Mô tả |
|-----|-------|
| # | Số thứ tự |
| X | Toạ độ game X |
| Y | Toạ độ game Y |
| Ghi chú | Note user nhập |
| LM | Liên minh (chọn từ dropdown hoặc nhập tay) |
| Loại | Nhãn OCR (enemy_city, lv1, ...) |
| Bảo vệ | 🛡 Còn BV 05:30:00 → 22:15 / ⚠️ Hết BV / 🏳️ Đã thoát |
| KC | Khoảng cách Manhattan tới thành mình (ô). Bấm 📐 để tính |
| 🗺 | Click → navigate tới toạ độ trên map |

### Ảnh screenshot

Khi do thám, mỗi điểm được chụp ảnh lưu vào:
```
config/{profile}/scout/{x}_{y}.png
```

Click vào hàng trong treeview → panel "Chi tiết ô đang xem" hiện:
- Thông tin text (loại, bảo vệ, KC, OCR raw...)
- Ảnh screenshot thu nhỏ (max 300px rộng, dùng PIL resize)

---

## 12. Luồng do thám

**File**: `gui.py` — `_spy_run()` (line ~3326)

### 12.1. Pre-check

```python
# Dừng tất cả engine đang chạy (attack, wave, scout, build)
bot.stop()
wave_engines.stop()
scout_engine.stop()
build_eng.stop()
```

### 12.2. Duyệt từng toạ độ

```python
for i, (x, y, note) in enumerate(coords):
    if _stop: break
    
    # Back (trừ điểm đầu)
    if i > 0: adb.tap(342, 1216)
    
    # Navigate + tap
    bot.navigate_to(x, y, force_tap=1)
    time.sleep(0.6)
    
    # Screenshot + OCR
    img = screenshot()
    # Lưu ảnh vào config/{profile}/scout/{x}_{y}.png
    cv2.imwrite(f"config/{profile}/scout/{x}_{y}.png", img)
    info = analyze_tile_image(img)
    label = info["label"]
    name = info["name"]
    
    # Detect Truce
    truce_m = re.search(r'truce\s+(\d{1,2}:\d{2}:\d{2})', text_title)
    if "quit" or "out" → "🏳️ Already quit"
    elif truce_m → "🛡 Còn bảo vệ HH:MM:SS"
    else → "⚠️ Hết bảo vệ"
    
    # Auto-save sau mỗi kết quả
    _spy_save()
```

### 12.3. Kết thúc

```python
adb.tap(342, 1216)   # Back lần cuối
status = f"✅ Xong ({done}/{total})"
```

---

## 13. Phát hiện Truce (bảo vệ)

### 13.1. OCR Pattern

```python
# Pattern chính
re.search(r'truce\s+(\d{1,2}:\d{2}:\d{2})', text_title)

# Fallback cho OCR sai chữ
re.search(r'(?:truce|truee|iruce|lruce)\s+(\d{1,3}[:\s]\d{2}[:\s]\d{2})', text_title)

# Đã thoát chiến
re.search(r'\b(quit|out)\b', text_title)
```

### 13.2. Phân loại

| Pattern match | Protection | Ý nghĩa |
|---------------|-----------|---------|
| `quit` hoặc `out` | 🏳️ Already quit the battle | Đã rời trận, an toàn |
| `truce HH:MM:SS` | 🛡 Còn bảo vệ 05:30:00 | Đang có truce, chờ hết |
| Không match | ⚠️ Hết bảo vệ | Có thể tấn công |

### 13.3. Tính thời gian hết hạn

`_calc_truce_expiry(truce_str)` → parse HH:MM:SS → cộng vào thời gian hiện tại → hiện "→ 22:15" (thời điểm hết truce).

---

# CHUNG

## 14. OCR & Nhãn ô

**File**: `tile_ocr.py` — `analyze_tile_image(img)`

Crop 2 vùng:
1. **Title** (vùng trên popup): tên ô, level, player name
2. **Button** (vùng dưới popup): nút "Capture", "Scout", v.v.

Kết quả:

```python
{
    "label":     "lv2",              # Nhãn phân loại
    "name":      "PlayerName",       # Tên player/alliance (nếu có)
    "raw_title": "Lv.2 Wasteland",   # Text thô vùng title
    "raw_btn":   "Capture Scout",    # Text thô vùng button
}
```

| Label | Điều kiện OCR |
|-------|--------------|
| `lv1` | "Wasteland" trong title |
| `lv2`-`lv5` | "Lv.N" trong title |
| `ally` | Tên alliance trùng config |
| `enemy` | Có tên player nhưng không phải ally |
| `enemy_city` | "City" hoặc "Castle" trong title |
| `terrain` | "Mountain", "River", "Forest"... |
| `?` | Không match gì |

---

## 15. Lưu/Load dữ liệu

### 15.1. Bản đồ Scout

```
config/scout_map_{profile}.json
```

```python
{
    "tiles": {"574,310": "lv1", "575,310": "lv2", ...},
    "names": {"574,310": "Wasteland", ...},
    "origin": [574, 310]
}
```

### 15.2. Dữ liệu Spy

```
config/spy_data_{profile}.json
```

```python
{
    "coords": [[500,300,"Thành A","GLORY"], [600,400,"Thành B","DRG"], ...],
    "results": {
        "500,300": {"label":"enemy_city", "name":"Player1",
                    "truce":"05:30:00", "protection":"🛡 Còn BV",
                    "raw_title":"...", "raw_btn":"..."},
        ...
    },
    "city_x": 574,
    "city_y": 310,
    "alliances": "Darkblade, BLACK DRAGON, 闪耀之星, Ma Vương, GLORY"
}
```

Ảnh screenshot lưu riêng:
```
config/{profile}/scout/{x}_{y}.png
```

### 15.3. Cấu hình Scout

Lưu trong autosave (`_scout_cfg` key):

```python
{
    "ox": "574", "oy": "310",
    "up": "5", "down": "5",
    "left": "5", "right": "5"
}
```

---

## 16. Toạ độ cố định

| Toạ độ | Mục đích |
|--------|----------|
| `(342, 1216)` | Nút Back |
| `(666, 1216)` | Mở All Armies (dùng trong Check quân) |
| Giữa màn hình | Tap chọn ô (tính từ screen size) |

---

## Phụ lục A: Bảng tổng hợp nút điều khiển

### Scout

| Nút | Hành động |
|-----|-----------|
| ▶ Bắt đầu dò | Tạo ScoutEngine → quét lưới |
| ⏹ Dừng | `_stop.set()` |
| 🗑 Xóa bản đồ | Xoá `_scout_tiles` + canvas |
| 📤 Xuất CSV | Export toạ độ + nhãn ra CSV |
| 💾 Lưu cấu hình | Autosave mốc + phạm vi |
| 📋 Tính nhanh | Tính spiral không cần ADB |
| ▶ Bắt đầu (spiral) | Dò theo pattern xoắn ốc |
| 👁 Xem trước | Vẽ spiral lên canvas |
| ▶ Bắt đầu tự dò | Auto path thực tế trên game |
| 🔍 Tìm đường | BFS pathfinding |
| ✖ Xóa đường | Xoá highlight path trên canvas |

### Spy

| Nút / Widget | Hành động |
|--------------|-----------|
| 🏰 Thành mình X/Y | Nhập toạ độ thành của mình |
| 📐 Tính KC | Tính khoảng cách Manhattan tới tất cả điểm → cập nhật cột KC |
| 🏴 Liên minh | Ô text nhập danh sách phân cách dấu phẩy → auto parse → cập nhật dropdown LM |
| ➕ Thêm | Thêm toạ độ vào danh sách (LM chọn từ dropdown) |
| ✏ Sửa | Mở dialog sửa X, Y, Ghi chú, Liên minh (Combobox) |
| 🗑 Xóa đã chọn | Xóa row đang select |
| 🗑 Xóa tất cả | Xóa toàn bộ |
| 📋 Import CSV | Import danh sách toạ độ từ CSV (cột 4 = alliance) |
| 💾 Xuất CSV | Export danh sách + kết quả (thêm cột Liên minh) |
| ▶ Bắt đầu do thám | Duyệt từng toạ độ, OCR + detect truce + chụp ảnh |
| ⏹ Dừng | `_spy_stop_evt.set()` |
| 💾 Lưu danh sách | Lưu spy data + city + alliances vào file |
| 🗺 (cột treeview) | Navigate tới toạ độ đó trên map |
| Click hàng | Hiện chi tiết (bao gồm LM, KC) + ảnh screenshot |

## Phụ lục B: Sequence Diagram (Scout 1 ô)

```
GUI Thread          ScoutEngine Thread       ADB Device
    │                      │                      │
    │  ▶ Bắt đầu dò      │                      │
    │─────────────────────►│                      │
    │                      │  navigate_to(gx,gy) ─►│ Di chuyển map
    │                      │  tap giữa ────────────►│ Mở popup ô
    │                      │  screenshot ──────────►│
    │                      │  analyze_tile_image()  │
    │                      │  → label="lv2"         │
    │  ◄─ on_tile ──────── │                      │
    │  update canvas + tree│                      │
    │                      │  sleep(0.3)           │
    │                      │  → ô tiếp theo       │
    │                      │  ...                  │
    │  ◄─ on_done ──────── │                      │
    │  "✅ Dò xong!"       │                      │
```
