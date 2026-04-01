"""
Config, Constants, Data Classes, State Machine, Theme.
"""
import os
import threading
from dataclasses import dataclass, field
from typing import Optional, List, Callable
from enum import Enum, auto


# ──────────────────────────────────────────────
#  PATHS & DEFAULTS
# ──────────────────────────────────────────────

def _find_adb() -> str:
    for p in [
        r"E:\LDPlayer\LDPlayer9\adb.exe",
        r"C:\LDPlayer\LDPlayer9\adb.exe",
        r"D:\LDPlayer\LDPlayer9\adb.exe",
        r"C:\Program Files\LDPlayer\LDPlayer9\adb.exe",
        "adb",
    ]:
        if p == "adb" or os.path.isfile(p):
            return p
    return "adb"


BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR  = os.path.join(BASE_DIR, "assets")
DEBUG_DIR   = os.path.join(BASE_DIR, "debug")
CONFIG_DIR  = os.path.join(BASE_DIR, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
DEFAULT_ADB = _find_adb()

os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(DEBUG_DIR,  exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# Lock ADB chung cho TẤT CẢ engine (attack, build, wave, spin, autolv)
# Chỉ 1 engine được dùng ADB tại 1 thời điểm
ADB_LOCK = threading.Lock()

# Backward compat
_WAVE_ADB_LOCK = ADB_LOCK

# Optional imports
try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ──────────────────────────────────────────────
#  STATE MACHINE
# ──────────────────────────────────────────────

class State(Enum):
    IDLE            = auto()
    TAPPING         = auto()
    WAIT_POPUP      = auto()
    SELECTING       = auto()
    WAIT_DONE       = auto()
    COOLDOWN        = auto()
    HEALING         = auto()
    DONE            = auto()


# ──────────────────────────────────────────────
#  DATA CLASSES
# ──────────────────────────────────────────────

@dataclass
class AttackPoint:
    idx:    int
    game_x: int
    game_y: int
    label:  str = ""
    status: str = "waiting"  # waiting | going | process | done

@dataclass
class WaveGroup:
    """Nhom tan cong: quan chinh (ten dau tien) + nhieu auto."""
    name:        str       = ""
    troop_names: List[str] = field(default_factory=list)
    points:      List[dict]= field(default_factory=list)
    enabled:     bool      = True
    heal_every:  int       = 0    # Auto heal sau N lan tan cong (0=tat)
    heal_step:   int       = 0    # Dat diem hoi mau moi N step (0=tat)
    heal_x:      int       = 500  # Toa do X diem hoi mau
    heal_y:      int       = 300  # Toa do Y diem hoi mau
    heal_on_done: bool     = False # Tu dong hoi mau khi het diem cuoi
    cooldown_sec:     int  = 0    # Cooldown Lv1 rieng nhom (0=dung global)
    lv2_cooldown_sec: int  = 0    # Cooldown Lv2 rieng nhom (0=dung global)
    lv3_cooldown_sec: int  = 0    # Cooldown Lv3 rieng nhom (0=dung global)
    lv4_cooldown_sec: int  = 0    # Cooldown Lv4 rieng nhom (0=dung global)

@dataclass
class BotConfig:
    adb_path:       str   = DEFAULT_ADB
    device_serial:  str   = "emulator-5554"
    screen_w:      int   = 540
    screen_h:      int   = 960
    tap_delay:     float = 0.4
    troop_names:   List[str] = field(
        default_factory=lambda: ["auto1","auto2","auto3","auto4","auto5"])
    max_troops:    int   = 5
    cooldown_sec:     int   = 300
    lv2_cooldown_sec: int   = 600
    lv3_cooldown_sec: int   = 900
    lv4_cooldown_sec: int   = 1200
    popup_timeout: float = 10.0
    game_start:    str   = ""
    troop_timeout: float = 8.0
    done_timeout:  float = 3600.0
    debug_mode:       bool  = False
    ollama_enabled:   bool  = False
    ollama_url:       str   = "http://localhost:11434"
    ollama_model:     str   = "qwen2-vl:7b"
    nav_btn_x:   int   = 558
    nav_btn_y:   int   = 1216
    x_field_x:   int   = 378
    x_field_y:   int   = 1002
    y_field_x:   int   = 486
    y_field_y:   int   = 1002
    xem_x:       int   = 630
    xem_y:       int   = 1002
    nav_wait:    float = 1.5
    heal_every:  int   = 5
    heal_x:      int   = 500
    heal_y:      int   = 300
    heal_step:   int   = 0
    army_health_enabled: bool = True
    health_warn_avg: float = 0.55
    health_force_heal_avg: float = 0.30
    health_critical_ratio: float = 0.25
    health_force_heal_critical_units: int = 2
    anthropic_api_key: str  = ""
    telegram_enabled:  bool = False
    telegram_token:    str  = ""     # Bot token tu @BotFather
    telegram_chat_id:  str  = ""     # Chat ID cua user

@dataclass
class BuildTask:
    name:       str
    tap_x:      int
    tap_y:      int
    enabled:    bool  = True
    build_done: float = 0.0

@dataclass
class BuildConfig:
    city_enter_x:   int   = 54
    city_enter_y:   int   = 1216
    city_exit_x:    int   = 54
    city_exit_y:    int   = 1216
    info_btn_x:     int   = 198
    info_btn_y:     int   = 192
    build_btn_x:    int   = 378
    build_btn_y:    int   = 1045
    max_builders:   int   = 2
    check_interval: int   = 60
    speed_pct:      float = 0.0
    delay_tap_house: float = 1.5
    delay_tap_info:  float = 1.5
    delay_tap_build: float = 2.5
    delay_back:      float = 1.2
    ollama_url:     str   = "http://localhost:11434"
    ollama_model:   str   = "qwen2-vl:7b"
    tasks: list = None

    def __post_init__(self):
        if self.tasks is None:
            self.tasks = [
                {"name": "Nha chinh", "tap_x": 342, "tap_y": 490,
                 "enabled": True, "build_done": 0.0, "level": 1, "target_lv": 20},
                {"name": "Nha ren",   "tap_x": 234, "tap_y": 704,
                 "enabled": True, "build_done": 0.0, "level": 1, "target_lv": 20},
            ]


# ──────────────────────────────────────────────
#  THEME
# ──────────────────────────────────────────────

C = dict(
    bg     = "#12141f",
    panel  = "#1c1f30",
    card   = "#232640",
    border = "#2e3356",
    accent = "#6c8ef5",
    green  = "#4ade80",
    yellow = "#fbbf24",
    red    = "#f87171",
    text   = "#e2e8f0",
    muted  = "#4a5580",
    entry  = "#2a2d45",
)

STATE_COLOR = {
    State.IDLE:       "#4a5580",
    State.TAPPING:    "#fbbf24",
    State.WAIT_POPUP: "#fbbf24",
    State.SELECTING:  "#6c8ef5",
    State.WAIT_DONE:  "#6c8ef5",
    State.COOLDOWN:   "#fb923c",
    State.HEALING:    "#a78bfa",
    State.DONE:       "#4ade80",
}

GRID_COLS = 20
GRID_ROWS = 30
