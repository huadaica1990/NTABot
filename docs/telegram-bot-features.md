# Telegram Bot – Features

## Commands

| Command | Description |
|---|---|
| `/devices` | List all connected ADB devices (inline keyboard) |
| `/status` | Status of active profile + wave groups |
| `/screenshot` | Capture and send current screen |
| `/tap` | Open quick-tap inline keyboard |
| `/tap X Y` | Tap specific coordinate |
| `/wave_start <name>` | Start a wave group |
| `/wave_stop <name>` | Stop a wave group |
| `/wave_skip <name>` | Skip cooldown for a group |
| `/stop_all` | Stop all running groups |
| `/help` | Show help |

## Multi-Device (Multiple App Instances)

**Each app instance must use a separate bot token.**  
Telegram only allows one active `getUpdates` polling session per token.  
Using the same token across two instances causes HTTP 409 Conflict.

**Setup:**
1. Create a new bot via `@BotFather` for each device/instance
2. Use the same `chat_id` (same Telegram user receives all messages)
3. Configure each instance with its own token in Settings → Telegram

**409 Handling:**  
If a conflict is detected, the polling loop backs off 30s and logs a clear message instead of spamming error lines.

## Instance Label

Each bot announces itself on startup with `instance_label` (format: `profile @ serial`).  
This lets you tell which device sent the message when running multiple bots in the same chat.

## Device Selection Flow

```
/devices
  → inline keyboard: [📱 127.0.0.1:5555 ✅]  [📱 127.0.0.1:5556]
      ↓ tap a device
  → status for that device + tap keyboard with [📋 ← Danh sách thiết bị]
```
