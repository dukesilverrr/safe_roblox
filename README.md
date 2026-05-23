# Roblox Whitelist Guardian — Setup Guide

A parental control script that restricts your child's Roblox account to **only** whitelisted experiences. If they join anything not on the list, the Roblox process is automatically killed.

Works with both the **native Roblox app** and **web browser**.

---

## Requirements

- Python 3.9+ (no external packages needed — uses only the standard library)
- Your child's Roblox **User ID**
- A `.ROBLOSECURITY` cookie from their account

## Quick Start

### 1. Get your child's User ID

Go to their Roblox profile page. The URL looks like:
```
https://www.roblox.com/users/123456789/profile
```
The number (`123456789`) is their User ID.

### 2. Get the .ROBLOSECURITY cookie

1. Log into your child's Roblox account in a web browser.
2. Open DevTools: press **F12** (or Cmd+Option+I on Mac).
3. Go to **Application** → **Cookies** → `https://www.roblox.com`.
4. Find `.ROBLOSECURITY` and copy the full value.

> ⚠️ **Keep this cookie private.** It grants full access to the account.
> Rotate it periodically by logging out and back in.

### 3. Find Universe IDs for games you want to allow

**Option A — Use the built-in search:**
```bash
python roblox_whitelist_guardian.py --lookup "Adopt Me"
python roblox_whitelist_guardian.py --lookup "Natural Disaster Survival"
```

**Option B — From the game URL:**

Every Roblox game URL has a Place ID:
```
https://www.roblox.com/games/920587237/Adopt-Me
                              ─────────
                              Place ID
```
Convert it to a Universe ID:
```bash
python roblox_whitelist_guardian.py --place-to-universe 920587237
```

### 4. Edit the config file

Open `whitelist_config.json` and fill in your values:

```json
{
  "roblox_user_id": 123456789,
  "roblosecurity_cookie": "_|WARNING:-DO-NOT-SHARE-THIS...",
  "poll_interval_seconds": 5,
  "auto_kill": true,
  "notify_parent": true,
  "log_file": "guardian.log",

  "whitelisted_universes": {
    "13822889": "Adopt Me!",
    "2753915549": "Blox Fruits"
  }
}
```

### 5. Run it

```bash
# Test mode — monitors and logs but doesn't kill anything
python roblox_whitelist_guardian.py --dry-run

# Enforcement mode — will kill Roblox if a non-whitelisted game is joined
python roblox_whitelist_guardian.py
```

---

## Running at Startup

### Windows (Task Scheduler)
1. Open Task Scheduler → Create Basic Task
2. Trigger: "When I log on"
3. Action: Start a program
   - Program: `python`
   - Arguments: `C:\path\to\roblox_whitelist_guardian.py`
4. Check "Run with highest privileges"

### macOS (launchd)
Create `~/Library/LaunchAgents/com.roblox.guardian.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.roblox.guardian</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/roblox_whitelist_guardian.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```
Then: `launchctl load ~/Library/LaunchAgents/com.roblox.guardian.plist`

---

## Config Options

| Field | Description |
|---|---|
| `roblox_user_id` | Your child's numeric Roblox user ID |
| `roblosecurity_cookie` | Auth cookie from their browser session |
| `poll_interval_seconds` | How often to check (5 = every 5 seconds) |
| `auto_kill` | `true` to kill Roblox on violation, `false` to only log |
| `notify_parent` | `true` to send desktop notifications |
| `whitelisted_universes` | Map of Universe ID → friendly name |
| `log_file` | Path to the log file |

## Limitations & Hardening Tips

- **Cookie expiration**: The `.ROBLOSECURITY` cookie can expire or be
  invalidated. If you see repeated API errors, get a fresh one.
- **Tech-savvy kids**: A child who knows how to find and kill Python
  processes could stop this. Consider running it as a system service
  with a non-obvious name, or under a separate admin account.
- **Network-level backup**: Pair this with router-level controls
  (e.g., OpenDNS Family Shield) for defense in depth.
- **Multiple devices**: Run this on each device, or use the
  presence API approach from a single always-on machine (it monitors
  the account, not the device).
- **Browser play**: The script kills native Roblox processes. For
  browser-based play, it will still detect and log violations via the
  API, but killing the browser tab requires a browser extension
  (not included). The native app is the more common vector.
