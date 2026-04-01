"""
TelegramBot - Dieu khien bot tu xa qua Telegram.

Cau lenh:
  /status      — Trang thai tat ca nhom + bot
  /screenshot  — Chup man hinh gui qua Telegram
  /tap         — Hien thi ban phim tap nhanh (inline keyboard)
  /tap X Y     — Tap toa do cu the + gui screenshot
  /wave_start <ten nhom>  — Chay nhom wave
  /wave_stop <ten nhom>   — Dung nhom wave
  /wave_skip <ten nhom>   — Skip cooldown nhom
  /stop_all    — Dung tat ca
  /help        — Hien thi cac lenh

Inline keyboard:
  - Tap giữa, Tap Back, Thoát thành, All Armies...
  - Sau mỗi tap → tự gửi screenshot

Notify tu dong:
  - CAPTCHA phat hien
  - Nhom hoan thanh / loi
  - Screenshot khi CAPTCHA
"""
import threading
import time
import json
import io
from typing import Optional, Callable, Dict, Any
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode


class _ConflictError(Exception):
    """Raised when Telegram returns 409 Conflict (another instance is polling)."""


# Inline keyboard presets: key → (label, x, y)
TAP_PRESETS = {
    "tap_center":      ("👆 Giữa",          342, 661),
    "tap_close_popup": ("❌ Đóng popup",     342, 1216),
    "tap_enter_city":  ("🏰 Vào thành",      54,  1216),
    "tap_exit_city":   ("🚪 Thoát thành",    54,  1216),
}


class TelegramBot:
    """Telegram bot polling + send messages + inline keyboard."""

    def __init__(self, token: str, chat_id: str, log: Callable):
        self.token   = token
        self.chat_id = chat_id
        self.log     = log
        self._stop   = threading.Event()
        self._thread = None
        self._offset = 0  # last update_id

        # Callbacks — GUI se gan cac ham nay
        self.on_command: Optional[Callable[[str, str], str]] = None
        # on_command(cmd, args) -> reply_text

        # ADB callback: on_tap(x, y) -> png_bytes hoac None
        # x=None, y=None → chỉ chụp screenshot, không tap
        self.on_tap: Optional[Callable] = None

        # Multi-device callbacks
        # on_list_devices() -> list[{"serial": str, "active": bool}]
        self.on_list_devices: Optional[Callable] = None
        # on_device_status(serial) -> str (formatted status text)
        self.on_device_status: Optional[Callable] = None

        self._selected_device: Optional[str] = None  # device selected via Telegram
        self.instance_label: str = ""  # e.g. "profile @ serial" — shown in startup message

    @property
    def api(self):
        return f"https://api.telegram.org/bot{self.token}"

    def _request(self, method: str, data: dict = None, files: dict = None) -> Optional[dict]:
        """Goi Telegram API. Tra ve dict hoac None."""
        url = f"{self.api}/{method}"
        try:
            if files:
                boundary = "----NTA_BOUNDARY"
                body = b""
                if data:
                    for k, v in data.items():
                        body += f"--{boundary}\r\n".encode()
                        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                        body += f"{v}\r\n".encode()
                for fname, (filename, fdata, ctype) in files.items():
                    body += f"--{boundary}\r\n".encode()
                    body += f'Content-Disposition: form-data; name="{fname}"; filename="{filename}"\r\n'.encode()
                    body += f"Content-Type: {ctype}\r\n\r\n".encode()
                    body += fdata + b"\r\n"
                body += f"--{boundary}--\r\n".encode()
                req = Request(url, data=body)
                req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            elif data:
                encoded = json.dumps(data).encode("utf-8")
                req = Request(url, data=encoded)
                req.add_header("Content-Type", "application/json")
            else:
                req = Request(url)
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except URLError as e:
            if "409" in str(e):
                raise _ConflictError() from e
            self.log(f"[Telegram] ⚠️ API lỗi: {e}")
            return None
        except Exception as e:
            self.log(f"[Telegram] ⚠️ Request lỗi: {e}")
            return None

    def send_message(self, text: str, reply_markup: dict = None):
        """Gui tin nhan text den chat_id."""
        if not self.token or not self.chat_id:
            return
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            data["reply_markup"] = reply_markup
        self._request("sendMessage", data)

    def send_photo(self, png_bytes: bytes, caption: str = "", reply_markup: dict = None):
        """Gui anh PNG den chat_id."""
        if not self.token or not self.chat_id:
            return
        data = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        self._request("sendPhoto", data=data, files={
            "photo": ("screenshot.png", png_bytes, "image/png")
        })

    def answer_callback(self, callback_query_id: str, text: str = ""):
        """Tra loi callback query (xoa loading state tren nut)."""
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        self._request("answerCallbackQuery", data)

    def _build_tap_keyboard(self) -> dict:
        """Tao inline keyboard voi cac nut tap nhanh."""
        keys = list(TAP_PRESETS.keys())
        rows = []
        row = []
        for k in keys:
            label, x, y = TAP_PRESETS[k]
            row.append({"text": label, "callback_data": k})
            if len(row) >= 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "📷 Screenshot", "callback_data": "cb_screenshot"}])
        return {"inline_keyboard": rows}

    def _build_device_keyboard(self, devices: list) -> dict:
        """Inline keyboard: one button per ADB device."""
        rows = []
        for d in devices:
            serial = d["serial"]
            mark = " ✅" if d.get("active") else ""
            rows.append([{"text": f"📱 {serial}{mark}",
                          "callback_data": f"cb_dev:{serial}"}])
        rows.append([{"text": "🔄 Làm mới", "callback_data": "cb_devices"}])
        return {"inline_keyboard": rows}

    def _build_device_cmd_keyboard(self) -> dict:
        """Tap keyboard + back-to-devices button at top."""
        kb = self._build_tap_keyboard()
        kb["inline_keyboard"].insert(0, [
            {"text": "📋 ← Danh sách thiết bị", "callback_data": "cb_devices"}
        ])
        return kb

    def _poll(self):
        """Long-polling lay updates tu Telegram."""
        result = self._request("getUpdates", {
            "offset": self._offset,
            "timeout": 10,
            "allowed_updates": ["message", "callback_query"]
        })
        if not result or not result.get("ok"):
            return []
        updates = result.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def _handle_update(self, update: dict):
        """Xu ly 1 update (message hoac callback_query)."""
        # Callback query (inline keyboard button press)
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        # Text message
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        from_chat = str(msg.get("chat", {}).get("id", ""))

        if from_chat != self.chat_id:
            self.log(f"[Telegram] ⚠️ Tin nhắn từ chat_id lạ: {from_chat}")
            return

        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        args = parts[1].strip() if len(parts) > 1 else ""

        self.log(f"[Telegram] 📩 Lệnh: {cmd} {args}")

        if cmd == "/help":
            reply = (
                "🤖 <b>NTA Bot — Lệnh Telegram</b>\n\n"
                "📱 <b>Thiết bị:</b>\n"
                "/devices — Danh sách thiết bị ADB\n\n"
                "📋 <b>Thông tin:</b>\n"
                "/status — Trạng thái tất cả\n"
                "/screenshot — Chụp màn hình\n\n"
                "🎮 <b>Điều khiển ADB:</b>\n"
                "/tap — Bàn phím tap nhanh\n"
                "/tap X Y — Tap toạ độ cụ thể\n\n"
                "🌊 <b>Wave:</b>\n"
                "/wave_start &lt;tên&gt; — Chạy nhóm\n"
                "/wave_stop &lt;tên&gt; — Dừng nhóm\n"
                "/wave_skip &lt;tên&gt; — Skip cooldown\n"
                "/stop_all — Dừng tất cả\n\n"
                "/help — Hiện trợ giúp"
            )
            self.send_message(reply)
            return

        if cmd == "/devices":
            self._send_device_list()
            return

        if cmd == "/tap" and not args:
            self.send_message(
                "🎮 <b>Tap nhanh</b> — chọn nút:",
                reply_markup=self._build_tap_keyboard()
            )
            return

        # Delegate to GUI handler
        if self.on_command:
            try:
                reply = self.on_command(cmd, args)
                if reply:
                    self.send_message(reply)
            except Exception as e:
                self.send_message(f"❌ Lỗi: {e}")
        else:
            self.send_message("⚠️ Bot chưa sẵn sàng, thử lại sau.")

    def _send_device_list(self):
        """Fetch and send the ADB device list as an inline keyboard."""
        if not self.on_list_devices:
            self.send_message("⚠️ Chức năng liệt kê thiết bị chưa được cấu hình.")
            return
        devices = self.on_list_devices()
        if not devices:
            self.send_message("⚠️ Không tìm thấy thiết bị ADB nào đang kết nối.")
            return
        count = len(devices)
        self.send_message(
            f"📱 <b>{count} thiết bị ADB đang kết nối:</b>\n"
            "(✅ = thiết bị đang được điều khiển)",
            reply_markup=self._build_device_keyboard(devices)
        )

    def _handle_callback(self, cb: dict):
        """Xu ly callback query tu inline keyboard."""
        cb_id = cb.get("id", "")
        data  = cb.get("data", "")
        from_chat = str(cb.get("message", {}).get("chat", {}).get("id", ""))

        if from_chat != self.chat_id:
            self.answer_callback(cb_id, "⛔ Không có quyền")
            return

        # Device list (refresh or back)
        if data == "cb_devices":
            self.answer_callback(cb_id, "📱 Đang tải danh sách...")
            self._send_device_list()
            return

        # Device selected
        if data.startswith("cb_dev:"):
            serial = data[7:]
            self._selected_device = serial
            self.answer_callback(cb_id, f"📱 {serial}")
            if self.on_device_status:
                status = self.on_device_status(serial)
                self.send_message(status, reply_markup=self._build_device_cmd_keyboard())
            return

        # Screenshot button
        if data == "cb_screenshot":
            self.answer_callback(cb_id, "📷 Đang chụp...")
            if self.on_tap:
                png = self.on_tap(None, None)
                if png:
                    self.send_photo(png, caption="📷 Screenshot",
                                    reply_markup=self._build_tap_keyboard())
                else:
                    self.send_message("⚠️ Không chụp được",
                                      reply_markup=self._build_tap_keyboard())
            return

        # Tap preset buttons
        if data in TAP_PRESETS:
            label, x, y = TAP_PRESETS[data]
            self.answer_callback(cb_id, f"👆 Tap ({x},{y})...")
            self.log(f"[Telegram] 👆 Tap {label} ({x},{y})")
            if self.on_tap:
                png = self.on_tap(x, y)
                caption = f"👆 {label} ({x},{y})"
                if png:
                    self.send_photo(png, caption=caption,
                                    reply_markup=self._build_tap_keyboard())
                else:
                    self.send_message(f"{caption}\n⚠️ Không chụp được",
                                      reply_markup=self._build_tap_keyboard())
            return

        self.answer_callback(cb_id, "❓ Không rõ lệnh")

    def _run(self):
        """Main polling loop."""
        self.log("[Telegram] ▶ Bắt đầu polling...")
        label = f" — {self.instance_label}" if self.instance_label else ""
        self.send_message(f"🟢 NTA Bot đã kết nối{label}!\nGõ /tap để mở bàn phím điều khiển")
        _conflict_backoff = 0   # seconds to wait after 409
        while not self._stop.is_set():
            if _conflict_backoff > 0:
                # Another instance is polling — wait, then retry
                time.sleep(min(_conflict_backoff, 60))
                _conflict_backoff = 0
            try:
                updates = self._poll()
                for u in updates:
                    if self._stop.is_set():
                        break
                    self._handle_update(u)
            except _ConflictError:
                _conflict_backoff = 30
                self.log(
                    "[Telegram] ⚠️ 409 Conflict — một phiên khác đang dùng cùng token. "
                    "Mỗi instance cần một bot token riêng. "
                    "Thử lại sau 30s..."
                )
            except Exception as e:
                self.log(f"[Telegram] ⚠️ Poll lỗi: {e}")
                time.sleep(5)

    def start(self):
        if not self.token or not self.chat_id:
            self.log("[Telegram] ⚠️ Chưa cấu hình token/chat_id")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.log("[Telegram] ⏹ Dừng polling")

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # ── Notify helpers ──

    def notify_captcha(self, group_name: str = "", png_bytes: bytes = None):
        text = f"🚨 <b>CAPTCHA phát hiện!</b>"
        if group_name:
            text += f"\nNhóm: {group_name}"
        text += "\n⏹ Tất cả đã dừng."
        if png_bytes:
            self.send_photo(png_bytes, caption=text)
        else:
            self.send_message(text)

    def notify_done(self, group_name: str):
        self.send_message(f"✅ Nhóm <b>{group_name}</b> hoàn thành tất cả điểm.")

    def notify_error(self, group_name: str, error: str):
        self.send_message(f"❌ Nhóm <b>{group_name}</b> lỗi: {error}")
