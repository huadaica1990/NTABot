# Bot Engine – Features

## Attack Flow

```
TAPPING      → navigate to coordinate
WAIT_POPUP   → wait for capture button
SELECTING    → select troops + tap OK
             → ADB lock released here ✅
WAIT_DONE    → sleep until march time (no ADB needed)
             → pt.status = "process"
COOLDOWN     → wait between points
```

## ADB Lock Behaviour

`ADB_LOCK` is acquired before `_attack()` and released **immediately after troops are selected** (after `SELECTING` state), before `WAIT_DONE`.

`_wait_done()` only sleeps until `march_deadline` — it never touches ADB — so holding the lock during march time was wasteful.

**`_attack()` return value:** `(ok: bool, march_deadline: float)`  
Caller is responsible for calling `_wait_done(march_deadline)` outside the lock.

## States (State enum)

| State | Description |
|---|---|
| IDLE | Not running |
| TAPPING | Navigating to target |
| WAIT_POPUP | Waiting for capture popup |
| SELECTING | Selecting troops |
| WAIT_DONE | Waiting for march time (outside ADB lock) |
| COOLDOWN | Cooldown between attacks |
| DONE | All points finished |
