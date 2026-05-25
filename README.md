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

## Why "detection + Telegram" and not auto-kick?

Earlier versions of this script tried to programmatically invalidate the
child's Roblox session ("kick them out remotely"). Roblox has since
locked down those endpoints — they now require a step-up auth artifact
(`RBXBoundAuthToken`) that's only issued through the live browser login
flow. No cookie-only API client can obtain one. The "kick" path is dead
for everyone.

What still works reliably:
- **Detection**: the Presence API tells us exactly what game the kid is in.
- **Notification**: Telegram reaches the parent within seconds, for free.

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

> 💡 **If you're monitoring multiple kids, read [Recommended: one
> dedicated parent-account cookie](#recommended-one-dedicated-parent-account-cookie)
> first.** Using each kid's own cookie tends to die within hours
> because the kid's own device activity invalidates it. The
> parent-account pattern fixes that.

1. Log into the account in a web browser. (For a single kid you can
   use the kid's own account; for multi-kid use a dedicated bot
   account — see the link above.)
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

> **If the cookie does go 401**, the daemon does **not** exit. It sleeps
> in-process with long backoff (5 → 15 → 30 → 60 min, capped) and
> re-validates. This is deliberate: exiting + systemd respawning every
> few seconds against a dead cookie is exactly what gets Roblox's
> anti-abuse to start invalidating *more* cookies. When you paste a
> fresh cookie into the config file, the daemon picks it up at the next
> backoff wake — **no restart needed**.

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

The kid config holds per-kid stuff. The allowed games live in a
**separate file** (`whitelist_universes.json` by default) that one or
more kid configs can point at via `whitelist_file`. Running `--init`
creates both files.

```json
// emma.json
{
  "roblox_user_id": 123456789,
  "roblosecurity_cookie": "_|WARNING:-DO-NOT-SHARE-THIS...",
  "poll_interval_seconds": 10,
  "notify_parent": true,

  "child_display_name": "Emma",
  "telegram_bot_token": "123456789:ABC-DEF...",
  "parent_chat_ids": [987654321, 5550001111],

  "kill_local_process_on_violation": false,

  "whitelist_file": "whitelist_universes.json"
}
```

```json
// whitelist_universes.json (shared across all kid configs)
{
  "383310974": "Adopt Me!",
  "994732206": "Blox Fruits",
  "66654135": "Murder Mystery 2",
  "1686885941": "Brookhaven RP"
}
```

The path in `whitelist_file` resolves **relative to the kid config**,
not the current working directory — so `"whitelist_file": "shared.json"`
always means "next to this config", regardless of where you run the
script from. An absolute path works too.

If the file is missing or malformed, the daemon refuses to start
rather than silently allowing everything.

> **Hot reload**: every poll, the daemon stats `whitelist_universes.json`
> and reloads it if the mtime changed. Add a new game to the file and
> every running daemon picks it up within one poll cycle — no restarts.
> Mid-edit malformed JSON is handled gracefully: the in-memory whitelist
> stays in effect and a single warning is logged per bad save.

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

## Recommended: one dedicated parent-account cookie

The Roblox Presence API accepts any user ID as long as the requesting
session is *some* valid Roblox login. The cookie does **not** have to
belong to the kid being monitored — and using the kid's own cookie is
actually a bad idea when the kid plays on their own devices.

**Why the kid's own cookie keeps dying:**

When you grab the cookie from your browser, you and the kid now both
hold sessions on the same account. Every time the kid opens Roblox on
their iPad/phone, their device authenticates and may invalidate your
daemon's session as a side effect. In testing, kids' own-account
cookies died on rolling schedules tracking each kid's usage pattern
(morning device pickup, mid-day play, bedtime), not from anti-abuse
or the polling rate.

**The fix:**

1. Create one new Roblox account — e.g. `RyanGuardianBot`. Don't friend
   anyone, don't customize it. It's a passive observer.
2. Grab its `.ROBLOSECURITY` from a browser on the daemon's machine.
   Close the browser without logging out.
3. **Never use that account anywhere else.** No iPad, no phone, no
   second browser. That's the rule that keeps the cookie alive.
4. Save the cookie to a single shared file:

   ```bash
   echo '_|WARNING:-DO-NOT-SHARE-THIS...' > shared_cookie.txt
   chmod 600 shared_cookie.txt
   ```

5. Point every kid config at it via `cookie_file`:

   ```json
   {
     "roblox_user_id": 10492237089,
     "cookie_file": "shared_cookie.txt",
     "child_display_name": "Boys0",
     "telegram_bot_token": "",
     "parent_chat_ids": [...],
     "whitelist_file": "whitelist_universes.json"
   }
   ```

   (Omit `roblosecurity_cookie` entirely when using `cookie_file`.)

Each kid's **presence-privacy** must still be set to "Everyone" (Step
2a) for the parent account to see their game activity — that part is
account-side and unchanged.

Cookie rotations write back to the shared file atomically (chmod 600);
sibling daemons pick up the rotated value on their next auth retry.
When you eventually need to refresh the cookie, you update **one file**
and every daemon picks it up within a poll cycle.

---

## Monitoring Multiple Kids

Run **one daemon process per child**, each with its own config file
passed via `--config`. The Telegram bot token can (and should) be
shared via the `TELEGRAM_BOT_TOKEN` env var so you don't duplicate
the secret across files. The **allowed-games list is shared too** —
all kid configs point their `whitelist_file` at a single
`whitelist_universes.json`, so editing it once updates the whitelist
for every kid.

### Setup

1. Create one config file per kid, in the script's directory or
   wherever you want. The first `--init` also creates the shared
   `whitelist_universes.json`; the second `--init` reuses it:

   ```bash
   python roblox_whitelist_guardian.py --config emma.json --init
   python roblox_whitelist_guardian.py --config jacob.json --init
   ```

2. Fill in each kid file with that kid's `roblox_user_id`,
   `roblosecurity_cookie`, `child_display_name`, and
   `parent_chat_ids`. Both configs already point at the shared
   `whitelist_universes.json`, so edit that ONE file with the allowed
   games — every kid automatically inherits it. The log file path is
   automatic too (`emma.json` writes to `emma.log`,
   `jacob.json` writes to `jacob.log`), so kids never share a log.
   The `telegram_bot_token` can be left empty if you set
   `TELEGRAM_BOT_TOKEN` in the environment.

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
- **Whitelist is shared by default**: edit `whitelist_universes.json`
  once, all kids see it. If you want a more permissive list for an
  older kid, point that kid's `whitelist_file` at a separate file
  (e.g. `whitelist_older_kids.json`) instead.
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
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=600

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/roblox_whitelist_guardian.py --config /path/to/%i.json
Environment=TELEGRAM_BOT_TOKEN=123456789:ABC-DEF...
Restart=always
RestartSec=60
RestartSteps=5
RestartMaxDelaySec=900

[Install]
WantedBy=multi-user.target
```

> **Why `RestartSec=60` and not 10?** A `RestartSec=10` setting will
> respawn the daemon every 10 seconds against Roblox's auth endpoint
> if the cookie is dead — and the daemon's own auth backoff can't kick
> in if systemd keeps killing-and-relaunching it. The Python daemon
> handles auth failures itself (sleeps in-process for many minutes),
> so the systemd restart timer only needs to cover hard crashes,
> which are rare. The `RestartSteps`/`RestartMaxDelaySec` keys provide
> exponential backoff if a crash loop does occur (requires systemd ≥254;
> older versions silently ignore them).

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

### Linux (systemd) — automated installer

The repo ships `install-service.sh`, which writes a systemd template
unit (`roblox-guardian@.service`) and enables one instance per kid
config it finds in the current directory.

```bash
# Install: writes the unit, stashes the bot token in ./.env (chmod 600),
# then enables + starts roblox-guardian@<kidname> for every detected config.
sudo ./install-service.sh install --token '12345:ABC-DEF...'

# Check that every instance is alive
sudo ./install-service.sh status

# Or directly:
sudo systemctl status 'roblox-guardian@*'
sudo journalctl -u roblox-guardian@emma -f       # follow a kid's logs

# Tear down (stops + disables all instances, removes the unit file;
# leaves your configs and the .env alone)
sudo ./install-service.sh uninstall
```

The script auto-detects kid configs by scanning `*.json` for any file
with `roblox_user_id` set to a non-zero integer (so the shared
`whitelist_universes.json` is correctly skipped). The daemon runs as
the user who invoked `sudo`, not as root, so configs/logs keep their
normal ownership.

If you'd rather wire it up manually, the template the installer writes
looks like this:

```ini
[Unit]
Description=Roblox Whitelist Guardian for %i
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=600

[Service]
Type=simple
User=<your-user>
WorkingDirectory=<repo-dir>
EnvironmentFile=-<repo-dir>/.env
ExecStart=/usr/bin/python3 <repo-dir>/roblox_whitelist_guardian.py --config <repo-dir>/%i.json
Restart=always
RestartSec=60
RestartSteps=5
RestartMaxDelaySec=900
NoNewPrivileges=true
PrivateTmp=true

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
| `roblosecurity_cookie` | — | Inline auth cookie (auto-rotated on every response). Ignored when `cookie_file` is set. |
| `cookie_file` | `""` | Path to a plain-text file containing the .ROBLOSECURITY cookie (one line, no JSON). Multiple kid configs can share one — recommended for multi-kid setups, see [Recommended: one dedicated parent-account cookie](#recommended-one-dedicated-parent-account-cookie). Relative paths resolve next to this config. Rotations write back atomically (chmod 600). |
| `poll_interval_seconds` | `10` | How often to check (seconds). Sub-5s intervals across multiple daemons on one IP tend to get cookies invalidated by Roblox's anti-abuse — leave at 10+ unless you only run one daemon and want faster reaction time. |
| `notify_parent` | `true` | Desktop notification on the daemon's machine |
| `child_display_name` | `""` | Friendly name used in Telegram bodies (e.g. `"Emma"`). Falls back to Roblox username. |
| `telegram_bot_token` | `""` | Token from @BotFather. Env: `TELEGRAM_BOT_TOKEN` (preferred). |
| `parent_chat_ids` | `[]` | Telegram chat IDs that receive violation alerts (integers; negative for group chats). Discover with `--list-chats`. |
| `kill_local_process_on_violation` | `false` | If `true`, also kill the local Roblox process on violation. Only useful on the kid's actual play machine. |
| `whitelist_file` | `"whitelist_universes.json"` | Path to a JSON file containing the allowed-universes map (relative paths resolve next to this config). Multiple kid configs can share one. |
| `whitelisted_universes` | (deprecated) | Inline `{universe_id: name}` map. Backward-compat; if both `whitelist_file` and this are set, file wins. |

The shared whitelist file is just a flat JSON object — `{"<universe_id>": "<friendly name>"}`:

```json
{
  "383310974": "Adopt Me!",
  "994732206": "Blox Fruits"
}
```

Keys that aren't valid integers (e.g. `"# Example..."`) are silently
ignored, so you can inline-comment if you want.

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

### "My cookies keep getting invalidated every few hours"

Three causes, most → least likely:

1. **You're using each kid's own cookie, and the kids actively play
   their accounts.** This is the big one. When the kid opens Roblox on
   their iPad, their device authenticates and may invalidate the
   daemon's session as a side effect. Symptoms: cookies die on
   rolling schedules tracking each kid's usage (morning device
   pickup, mid-day play, bedtime), not all at the same moment.
   **Fix:** switch to a single dedicated parent-account cookie via
   `cookie_file`. See [Recommended: one dedicated parent-account
   cookie](#recommended-one-dedicated-parent-account-cookie). That
   cookie will be stable indefinitely because nothing else ever logs
   into the account.

2. **The daemon was respawning every ~10s against a dead cookie**
   (older `install-service.sh` had `RestartSec=10` and the daemon
   exited on 401). Result: hundreds of 401s/hour to
   `users.roblox.com/v1/users/authenticated` from one IP, which trips
   anti-abuse. **Fix:** re-run `sudo ./install-service.sh install` to
   pick up the new `RestartSec=60` + in-process auth backoff. The
   daemon now sleeps 5–60 min between auth retries on 401 instead of
   exiting. Symptom of this one: ALL cookies die at the same minute
   (an IP-wide event), not staggered.

3. **Poll interval too low across multiple daemons.** Six daemons
   polling every 3 seconds = ~2 req/s steady-state from one IP, which
   can also trip anti-abuse. Set `poll_interval_seconds` to **10** or
   higher in every config when running multiple kids.

### "Why doesn't the guardian kick the kid out automatically?"

It used to try. Roblox removed the ability — programmatic session
invalidation (`signoutfromallsessionsandreauthenticate`,
`auth.roblox.com/v2/logout`, etc.) now requires step-up auth via
`RBXBoundAuthToken`, a cookie that's only issued through the live
browser login flow. No cookie-only API client can obtain one, regardless
of which library you use. The script consequently focuses on **detection
and parent notification** instead — within seconds of the kid joining a
non-whitelisted game, every chat in `parent_chat_ids` gets a Telegram
message naming the child and the experience. You intervene manually.

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
  login from another device, etc.) — grab a fresh one and paste it into
  the config. The daemon doesn't need to be restarted; it'll reload the
  cookie at the next auth-backoff wake (worst case ~1 hour).
- **Polling gap**: there's a `poll_interval_seconds` window where the
  child could briefly be in a non-whitelisted game before the parent
  is notified. **Don't go below ~10s when running multiple daemons** —
  sub-5s polling across N kids has, in testing, been enough for Roblox's
  anti-abuse to start invalidating cookies on the shared IP. 10s is the
  sweet spot for a typical multi-kid household; if you're only running
  one daemon, you can lower it.
- **Re-joining**: after the parent intervenes, the child can try to
  rejoin. The guardian will detect and re-notify on the next poll cycle.
  Persistent re-joining gets logged with a violation counter.
- **Multiple children**: see [Monitoring Multiple Kids](#monitoring-multiple-kids)
  above — one config file + one process per child, sharing the bot token
  via `TELEGRAM_BOT_TOKEN` env var.
- **Network-level backup**: pair with router controls (OpenDNS Family
  Shield, Pi-hole with blocklists) for defense in depth.
