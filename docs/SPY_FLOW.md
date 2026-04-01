# SPY_FLOW — Spy Tab: Flow & Features

## Overview

The Spy tab (`_build_spy_tab`) manages a list of enemy coordinates to scout, runs OCR-based recon on each, and displays results in a treeview.

---

## Main City & March Time

### UI Section: "🏰 Thành chính"

Located in the left panel, above "Điều khiển".

| Field | Description | Default |
|---|---|---|
| X | Main City map X coordinate | — |
| Y | Main City map Y coordinate | — |
| Tốc độ (s/ô) | March speed in seconds per tile | 30 |

Changing any of these fields auto-refreshes the treeview.

### March Time Calculation

Uses **Manhattan distance** (4-directional movement only):

```
distance = |target_x - city_x| + |target_y - city_y|
march_seconds = distance × speed_per_tile
```

Displayed in the **"Hành quân"** column (format: `Xg Yp Zs`). Blank if city X/Y not set.

### Persistence

`city_x`, `city_y`, `march_speed` are saved in the per-profile spy data JSON (`config/spy_data_<profile>.json`) and restored on load.

---

## Spy Coordinate Flow

```
User adds coords (X, Y, note, alliance)
        ↓
_spy_coords list (in-memory)
        ↓
▶ Bắt đầu do thám → SpyEngine iterates each coord
        ↓
OCR result → _spy_results[(x,y)] = {label, protection, truce, ...}
        ↓
_spy_refresh_tree() → renders treeview row with march time
        ↓
💾 Lưu danh sách → _spy_save() → spy_data_<profile>.json
```

## Treeview Columns

| Column | Description |
|---|---|
| # | Row index |
| X, Y | Map coordinates |
| Ghi chú | User note |
| LM | Alliance tag |
| Loại | Tile type from OCR |
| Bảo vệ | Protection / truce expiry |
| Hành quân | March time from Main City |
| 🗺 | Map link action |
