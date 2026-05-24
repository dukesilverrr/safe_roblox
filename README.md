# Roblox Whitelist Guardian — Setup Guide

A parental **detection + alerting** script for your child's Roblox account.
Monitors what game they're in. The moment they join an experience that
isn't on your whitelist, it **sends a Telegram message to every parent
chat** with the child's name and the experience name so you can intervene.

Works with the **native Roblox app**, **web browser**, and across **multiple
devices** — detection is device-independent because it polls Roblox's
account-level Presence API. Telegram is free (no per-message charges,
no carrier hassles); parents just need the Telegram app installed.

---

## Why "detection + SMS" and not auto-kick?

Earlier versions of this script tried to programmatically invalidate the
child's Roblox session ("kick them out remotely"). Roblox has since
locked down those endpoints — they now require a step-up auth artifact
(`RBXBoundAuthToken`) that's only issued through the live browser login
flow. No cookie-only API client can obtain one. The "kick" path is dead
for everyone.

What still works reliably:
- **Detection**: the Presence API tells us exactly what game the kid is in.
- **Notification**: Twilio SMS reaches the parent within seconds.

The parent then takes the device, talks to the kid, or whatever's
appropriate. This is more intervention than the script can do alone, but
it's the only path Roblox still allows.

---

## Requirements

- Python 3.9+ (no external packages — uses only the standard library)
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
5. **Don't log into the account anywhere else afterward** — logging in
   from another browser, device, or app can invalidate the cookie you
   just grabbed. If that happens you'll have to repeat this step.

> ⚠️ **Keep this cookie private.** It grants full access to the account.
> Treat it like a password. Do **not** commit `whitelist_config.json`
> to git — add it to `.gitignore`.

> **Cookie rotation**: Since Roblox's May 2026 changes, the server
> rotates `.ROBLOSECURITY` opportunistically via `Set-Cookie` on any
> response — not only on logout. The script intercepts these on every
> API call and writes the new value back to `whitelist_config.json`
> automatically. You don't need to manually refresh unless the cookie
> has been invalidated externally (password change, login elsewhere,
> or an outdated-format cookie that the server refuses).

### 2a. Open up presence privacy on the account

The Presence API will silently report `"Website"` (instead of the actual
game) unless the account allows others to see its online status. Without
this, the guardian can never detect a non-whitelisted game and will sit
idle forever.

In Roblox: **Settings → Privacy → "Who can see my online status?" →
Everyone**. Then run `--check` (see below) to confirm.

### 3. Find Universe IDs for games you want to allow

**Option A — Use the built-in search:**
```bash
python roblox_whitelist_guardian.py --lookup "Adopt Me"
python roblox_whitelist_guardian.py --lookup "Natural Disaster Survival"
```

**Option B — From the game URL:**
```
https://www.roblox.com/games/920587237/Adopt-Me
                              ─────────
                              Place ID
```
Convert it:
```bash
python roblox_whitelist_guardian.py --place-to-universe 920587237
```

### 4. Create the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot`
   and answer its prompts. It gives you a **bot token** that looks like
   `123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`.
2. Each parent should now find your bot in Telegram (search by the bot's
   username) and tap **Start** (which sends `/start` to the bot). This
   gives the bot permission to message them.
3. Set the token. Either as an environment variable (preferred):

   ```bash
   export TELEGRAM_BOT_TOKEN='123456789:ABC-DEF...'
   ```

   Or in `whitelist_config.json` under `telegram_bot_token`.

4. Discover each parent's chat ID:

   ```bash
   python roblox_whitelist_guardian.py --list-chats
   ```

   This long-polls for 60 seconds. While it's running, every parent
   sends any message to the bot (e.g. `/start` again, or "hi"). When
   the command finishes it prints a `parent_chat_ids` list you can
   paste straight into the config.

### 5. Edit the config file

```json
{
  "roblox_user_id": 123456789,
  "roblosecurity_cookie": "_|WARNING:-DO-NOT-SHARE-THIS...",
  "poll_interval_seconds": 5,
  "notify_parent": true,

  "child_display_name": "Emma",
  "telegram_bot_token": "123456789:ABC-DEF...",
  "parent_chat_ids": [987654321, 5550001111],

  "kill_local_process_on_violation": false,

  "whitelisted_universes": {
    "383310974": "Adopt Me!",
    "994732206": "Blox Fruits"
  }
}
```

> **Where do logs go?** The log file path is derived from the config
> filename: `whitelist_config.json` → `whitelist_config.log`,
> `emma.json` → `emma.log`, both written next to the config. No
> setup required — and switching to per-kid configs (next section)
> automatically gives each kid their own log.

> **Prefer env vars for the bot token.** Setting `TELEGRAM_BOT_TOKEN`
> in the environment takes precedence over the config field, so you
> can leave `telegram_bot_token` empty in the file and keep the token
> off-disk. Treat the bot token like a password — whoever has it can
> impersonate the bot.

### 6. Run it

```bash
# Preflight: verify cookie, presence privacy, AND Telegram config
python roblox_whitelist_guardian.py --check

# Discover parent chat IDs (long-polls for 60s)
python roblox_whitelist_guardian.py --list-chats

# Verify delivery to every configured chat
python roblox_whitelist_guardian.py --test-telegram

# Detection only — won't send Telegram, useful for tuning whitelist
python roblox_whitelist_guardian.py --dry-run

# Live daemon — polls + notifies on violation
python roblox_whitelist_guardian.py

# Verbose mode — log every poll's raw presence data
python roblox_whitelist_guardian.py --debug
```

**Always run `--check` and `--test-telegram` the first time you set
things up** (or after refreshing the cookie / changing the bot).
`--check` confirms Roblox can see the kid's presence; `--test-telegram`
confirms every parent chat actually receives a message.

---

## Telegram message format

When a violation is detected, every chat in `parent_chat_ids` gets a
message like:

> 🚫 Roblox Guardian: Emma joined a non-whitelisted experience: "Slime RNG".

(`Emma` comes from `child_display_name`; falls back to the Roblox
username if you leave that empty. `"Slime RNG"` is the experience name
as Roblox reports it.)

Telegram delivers these as real push notifications on the parent's
phone — no carrier costs, no per-message limits, no Twilio trial
verification dance. You can also point one of the `parent_chat_ids` at
a group chat (a negative integer) instead of an individual chat to
notify multiple people in one place.

---

## Optional: kill local Roblox process too

If the daemon happens to run on the same machine the kid plays on
(Windows/Mac desktop), you can additionally have it kill the local
Roblox process when a violation fires. Set in the config:

```json
"kill_local_process_on_violation": true
```

This is **independent of SMS** — both fire if both are configured. On
iOS / Android / Xbox, the local kill is a no-op, so it's only worth
enabling if the daemon is on the actual play machine.

---

## Monitoring Multiple Kids

Run **one daemon process per child**, each with its own config file
passed via `--config`. The Telegram bot token can (and should) be
shared via the `TELEGRAM_BOT_TOKEN` env var so you don't duplicate
the secret across files.

### Setup

1. Create one config file per kid, in the script's directory or
   wherever you want:

   ```bash
   python roblox_whitelist_guardian.py --config emma.json --init
   python roblox_whitelist_guardian.py --config jacob.json --init
   ```

2. Fill in each file with that kid's `roblox_user_id`,
   `roblosecurity_cookie`, `child_display_name`, `parent_chat_ids`,
   and `whitelisted_universes`. The log file path is automatic —
   `emma.json` writes to `emma.log`, `jacob.json` writes to `jacob.log`
   — so kids never share a log. The `telegram_bot_token` can be left
   empty if you set `TELEGRAM_BOT_TOKEN` in the environment.

3. Verify each independently before starting either daemon:

   ```bash
   export TELEGRAM_BOT_TOKEN='123456789:ABC-DEF...'
   python roblox_whitelist_guardian.py --config emma.json --check
   python roblox_whitelist_guardian.py --config emma.json --test-telegram
   python roblox_whitelist_guardian.py --config jacob.json --check
   python roblox_whitelist_guardian.py --config jacob.json --test-telegram
   ```

4. Run them as separate processes (one terminal each, or via systemd /
   launchd / Task Scheduler — see [Running at Startup](#running-at-startup)
   below). **Each kid needs its own `.ROBLOSECURITY` cookie**, grabbed
   from that kid's own logged-in browser session.

### Per-kid config tips

- **Logs auto-separate**: each config writes to a sibling `.log` file
  derived from its filename (`emma.json` → `emma.log`). No setup
  needed, no clobbering.
- **Cookies don't share**: each `.ROBLOSECURITY` is account-specific.
  Refresh each kid's cookie independently when it dies.
- **Whitelists can differ**: an older kid might have a longer allowed
  list than a younger sibling — that's the whole point of per-kid configs.
- **Same `parent_chat_ids`**: usually you want both parents notified
  regardless of which kid triggered it, so the chat-ID list will be
  identical across files. (Or use a single Telegram group chat that
  contains both parents and reference its negative chat ID in every
  config — cleaner.)

### systemd template (one service per kid)

```ini
# /etc/systemd/system/roblox-guardian@.service
[Unit]
Description=Roblox Whitelist Guardian for %i
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/roblox_whitelist_guardian.py --config /path/to/%i.json
Environment=TELEGRAM_BOT_TOKEN=123456789:ABC-DEF...
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start one instance per kid:

```bash
sudo systemctl enable --now roblox-guardian@emma
sudo systemctl enable --now roblox-guardian@jacob
sudo systemctl status roblox-guardian@emma
journalctl -u roblox-guardian@emma -f
```

The `%i` placeholder in the template gets substituted with whatever
follows the `@` in the instance name, so `roblox-guardian@emma` reads
`emma.json`.

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

### Linux (systemd)
```ini
[Unit]
Description=Roblox Whitelist Guardian
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/roblox_whitelist_guardian.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Cloud / Raspberry Pi
Since detection is purely API polling, you can run the script on any
always-on machine. A Raspberry Pi or a $5/month VPS works perfectly —
it just needs Python and internet access. Note: the cookie's IP
binding means the *script's* host needs to be on the same network the
cookie was grabbed from (see Troubleshooting).

---

## Config Reference

| Field | Default | Description |
|---|---|---|
| `roblox_user_id` | — | Your child's numeric Roblox user ID |
| `roblosecurity_cookie` | — | Auth cookie (auto-rotated on every response) |
| `poll_interval_seconds` | `5` | How often to check (seconds) |
| `notify_parent` | `true` | Desktop notification on the daemon's machine |
| `child_display_name` | `""` | Friendly name used in Telegram bodies (e.g. `"Emma"`). Falls back to Roblox username. |
| `telegram_bot_token` | `""` | Token from @BotFather. Env: `TELEGRAM_BOT_TOKEN` (preferred). |
| `parent_chat_ids` | `[]` | Telegram chat IDs that receive violation alerts (integers; negative for group chats). Discover with `--list-chats`. |
| `kill_local_process_on_violation` | `false` | If `true`, also kill the local Roblox process on violation. Only useful on the kid's actual play machine. |
| `whitelisted_universes` | `{}` | Map of Universe ID → friendly name |

Log file path is **not** a config field — it's derived from the config
filename: `<name>.json` → `<name>.log`, next to the config. (For backward
compatibility, an explicit `log_file` field still wins if present.)

## Troubleshooting

### "The script runs but never blocks anything"

This is almost always one of two things. Run `--check` to find out which:

```bash
python roblox_whitelist_guardian.py --check
```

**If `--check` says the cookie is INVALID (401):**
The cookie has been invalidated. Grab a fresh one (step 2) and don't
log into the account anywhere else after. The guardian now refuses to
start on a dead cookie, but if you set up before this safeguard existed
you may have been running blind.

**If `--check` says the cookie is valid but presence reports
`type=1 (website)` when the child is actually in a game:**
The account's presence privacy is locked down. Fix it under
**Settings → Privacy → "Who can see my online status?" → Everyone**
(see step 2a). Roblox returns "Website" with a null universe ID for
any locked-down account, even to the account holder's own cookie.

### "I want to see what the API is returning each poll"

Run with `--debug`. Every poll cycle will log a line like:

```
[2026-05-23 19:50:41] DEBUG  poll → type=2 (in-game), universe=13822889, place=920587237, location='Adopt Me'
```

This is the fastest way to see whether Roblox is reporting the child's
actual game or just "Website".

### "I got a fresh cookie and it STILL 401s immediately"

This is almost always one of the following, in order of likelihood:

1. **IP binding.** Since March 2022, Roblox locks each `.ROBLOSECURITY`
   to the IP region where it was issued. If you grabbed the cookie at
   home and the script runs anywhere else (different network, VPN,
   mobile hotspot, work, cloud VPS), every authenticated call returns
   401 instantly. **Fix:** grab the cookie in a browser running on the
   *same machine and network* as the daemon. Use an incognito window,
   close it without logging out, paste the value into the config. Run
   `--check` to see this machine's public IP — make sure it matches
   where you grabbed the cookie.

2. **You clicked Logout in the browser.** Pressing "Log Out" on
   roblox.com immediately invalidates the cookie you just copied. Use
   an incognito window and *close it without logging out*.

3. **Another login invalidated it.** Logging into the same Roblox
   account from a second browser, device, or app after grabbing the
   cookie rotates it server-side and your copy becomes stale. Grab
   fresh and don't touch the account elsewhere.

4. **Cookie is in the deprecated format** (hex-only or `GgIQAQ.<hex>`
   from before May 2026). The guardian warns about this at startup
   and `--check` rejects it explicitly.

The guardian auto-rotates cookies on every API response and writes the
new value back to `whitelist_config.json`, so once you're past the
initial 401, manual refreshes should be rare.

### "Why doesn't the guardian kick the kid out automatically?"

It used to try. Roblox removed the ability — programmatic session
invalidation (`signoutfromallsessionsandreauthenticate`,
`auth.roblox.com/v2/logout`, etc.) now requires step-up auth via
`RBXBoundAuthToken`, a cookie that's only issued through the live
browser login flow. No cookie-only API client can obtain one, regardless
of which library you use. The script consequently focuses on **detection
and parent notification** instead — within seconds of the kid joining a
non-whitelisted game, every number in `parent_phone_numbers` gets an SMS
naming the child and the experience. You intervene manually.

### "Telegram messages aren't arriving"

Run `python roblox_whitelist_guardian.py --test-telegram` first — it
prints the exact error per chat. Common causes:

- **Parent never `/start`ed the bot.** Telegram bots can only message
  users who have explicitly opened a chat with them. Each parent must
  search for your bot, hit Start, and send any message *before* their
  chat ID will work. Failure looks like: `HTTP 400 — "chat not found"`
  or `"bot was blocked by the user"`.
- **Wrong chat_id.** Personal chats are positive integers, group chats
  are negative (`-123456789`). Re-run `--list-chats` to confirm.
- **Bot token wrong / revoked.** Failure looks like:
  `HTTP 401 — "Unauthorized"`. Regenerate via @BotFather → `/token`.
- **Bot was kicked from a group.** If you used a group chat ID and
  someone removed the bot, sends fail. Re-add the bot.
- **Network blocking `api.telegram.org`.** Some restrictive networks
  block it. Check from the daemon's host: `curl https://api.telegram.org`.

### "Running on a VPS / cloud — how do I deal with IP binding?"

The official workaround is to grab the cookie *from* the VPS by
tunneling your browser through it (SSH SOCKS5 proxy → incognito browser
→ log into Roblox). After that, the VPS IP is the cookie's bound IP and
the daemon works. From then on the VPS must be the only thing touching
that account; logging in from your laptop will rotate the cookie and
break the tunnel.

### "Warning about an OLD cookie format"

If you see `Your .ROBLOSECURITY cookie uses the OLD format (deprecated
May 2026)` at startup — or `--check` reports the same — your cookie is
in the legacy hex-only / `GgIQAQ.<hex>` format that Roblox no longer
accepts. It will return 401 for every authenticated call. Grab a fresh
cookie from a current browser session and paste it into the config.
After that the guardian's auto-rotation will keep it current.

---

## Limitations & Hardening Tips

- **Cookie expiration**: cookies auto-rotate on every API response, so
  the script self-heals against routine rotations. If you still see
  repeated 401s, the cookie was invalidated externally (password change,
  login from another device, etc.) — grab a fresh one.
- **Polling gap**: there's a `poll_interval_seconds` window where the
  child could briefly be in a non-whitelisted game before the parent
  is notified. Reduce to 2-3 for tighter latency at the cost of slightly
  more API traffic.
- **Re-joining**: after the parent intervenes, the child can try to
  rejoin. The guardian will detect and re-notify on the next poll cycle.
  Persistent re-joining gets logged with a violation counter.
- **Multiple children**: see [Monitoring Multiple Kids](#monitoring-multiple-kids)
  above — one config file + one process per child, sharing the bot token
  via `TELEGRAM_BOT_TOKEN` env var.
- **Network-level backup**: pair with router controls (OpenDNS Family
  Shield, Pi-hole with blocklists) for defense in depth.
