#!/usr/bin/env python3
"""
Roblox Whitelist Guardian
─────────────────────────
Parental detection script: monitors a child's Roblox account, and when
they join an experience that's NOT on the whitelist, sends a Telegram
message to every parent chat ID with the child's name and the experience.

How it works:
  1. Polls the Roblox Presence API every few seconds to see what game
     the monitored account is currently in.
  2. If the game is NOT on your whitelist, the bot sends a Telegram
     message to every chat in parent_chat_ids. The parent then
     intervenes manually (talk to the kid, take the device, etc.).
  3. Logs every detection and optionally pops a desktop notification
     on the machine running the daemon.

  Note: Roblox no longer permits programmatic session invalidation
  (the old endpoint requires step-up auth that cookie-only API clients
  can't obtain). Detection + parent notification is the realistic
  path. See README "Troubleshooting" for details.

Setup:
  1. Get your child's Roblox User ID (from their profile URL).
  2. Get a .ROBLOSECURITY cookie (log into their account in a browser,
     open DevTools → Application → Cookies → .ROBLOSECURITY).
  3. Find the Universe IDs of games you want to allow (see helper below).
  4. Create a Telegram bot via @BotFather, paste the token into
     telegram_bot_token (or set TELEGRAM_BOT_TOKEN env var).
  5. Have each parent send /start to the bot, then run --list-chats
     to discover their chat IDs. Paste into parent_chat_ids.
  6. Run --test-telegram to verify delivery, then start the daemon.

Usage:
  python roblox_whitelist_guardian.py                       # normal mode
  python roblox_whitelist_guardian.py --check                # preflight diagnostic
  python roblox_whitelist_guardian.py --list-chats           # discover parent chat IDs
  python roblox_whitelist_guardian.py --test-telegram        # verify delivery
  python roblox_whitelist_guardian.py --debug                # verbose per-poll logging
  python roblox_whitelist_guardian.py --lookup "Adopt Me"    # find Universe ID
  python roblox_whitelist_guardian.py --dry-run              # monitor only, don't notify
"""

import argparse
import json
import logging
import os
import platform
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote

# Backoff schedule (seconds) used whenever Roblox returns 401 — either at
# startup or mid-session. Long, capped, with jitter applied at use-site.
# The point is to stop hammering Roblox's auth endpoint while the cookie
# is dead; aggressive retry from a single IP is what gets cookies
# auto-invalidated in the first place.
AUTH_BACKOFF_STEPS = (5 * 60, 15 * 60, 30 * 60, 60 * 60)

# ── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "whitelist_config.json"
DEFAULT_WHITELIST_FILENAME = "whitelist_universes.json"

DEFAULT_CONFIG = {
    "roblox_user_id": 0,
    "roblosecurity_cookie": "PASTE_YOUR_COOKIE_HERE",
    # 10s is conservative — with N kid daemons polling the Presence API
    # against a single shared IP, sub-5s intervals tend to get cookies
    # invalidated by Roblox's anti-abuse. Lower it if you only run one
    # daemon and want faster reaction time.
    "poll_interval_seconds": 10,
    "notify_parent": True,
    "child_display_name": "",
    "telegram_bot_token": "",
    "parent_chat_ids": [],
    "kill_local_process_on_violation": False,
    "whitelist_file": DEFAULT_WHITELIST_FILENAME
    # Log file path is derived from the config path:
    # `--config emma.json` → `emma.log` next to the config.
}

# Default contents of the shared universe-whitelist file. Keys that
# don't parse as int are silently skipped, so the `# Example` lines
# act as inline documentation for first-time users.
DEFAULT_WHITELIST = {
    "# Example — replace these with the Universe IDs you allow": "",
    "# Run:  python roblox_whitelist_guardian.py --lookup \"Game Name\"": "",
    "# to find Universe IDs for games you want to whitelist.": ""
}

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(log_path: str, debug: bool = False) -> logging.Logger:
    logger = logging.getLogger("guardian")
    target_level = logging.DEBUG if debug else logging.INFO
    if logger.handlers:
        logger.setLevel(target_level)
        return logger
    logger.setLevel(target_level)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# ── Whitelist loading ────────────────────────────────────────────────────────

def _parse_whitelist_dict(raw: dict) -> dict:
    """
    Turn a raw JSON dict ({str_id: name}) into the int-keyed whitelist
    the guardian uses. Keys that don't parse as int (e.g. `"# Example"`
    comment-style entries) are silently dropped — useful for inline
    documentation inside the JSON file.
    """
    result: dict = {}
    for key, val in raw.items():
        try:
            uid = int(key)
            result[uid] = str(val) if val else f"Universe {uid}"
        except (ValueError, TypeError):
            pass
    return result


def _read_whitelist_file(wl_path: Path) -> dict:
    """
    Read and parse a whitelist JSON file. Raises RuntimeError on missing,
    malformed, or wrong-type contents. Shared between initial load and
    hot-reload so behavior matches in both cases.
    """
    try:
        with open(wl_path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"whitelist_file not found: {wl_path}. Create it with "
            f"--init or point whitelist_file at an existing JSON map."
        )
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"whitelist_file {wl_path} is not valid JSON: {e}"
        )
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"whitelist_file {wl_path} must contain a JSON object "
            f"of universe_id → name, got {type(raw).__name__}."
        )
    return _parse_whitelist_dict(raw)


def load_whitelist(config: dict, config_path: Path,
                   logger: logging.Logger = None):
    """
    Resolve the universe whitelist for a kid's config. Returns a tuple
    of (whitelist_dict, source_path_or_None).

    The source path is the resolved file path when `whitelist_file` is
    set, or None for inline `whitelisted_universes` configs. The
    guardian uses it to watch for mtime changes and hot-reload added
    games without a restart.

    Source priority:
      1. `whitelist_file` — path to a JSON file containing the whitelist
         map directly (top-level keys ARE the universe IDs). Recommended
         when running multiple kids — point each kid's config at the same
         shared file. Relative paths resolve next to the kid's config.
      2. Inline `whitelisted_universes` dict in the kid's config
         (backward compat). If both are set, file wins with a warning.

    Raises RuntimeError if `whitelist_file` is set but unreadable —
    we'd rather fail loud than silently allow every game.
    """
    wl_file = config.get("whitelist_file", "")
    inline = config.get("whitelisted_universes")

    if wl_file:
        if inline and logger:
            logger.warning(
                "Config has both whitelist_file and whitelisted_universes; "
                "using whitelist_file and ignoring the inline list."
            )
        wl_path = Path(wl_file)
        if not wl_path.is_absolute():
            wl_path = Path(config_path).parent / wl_path
        return _read_whitelist_file(wl_path), wl_path

    if inline is not None:
        return _parse_whitelist_dict(inline), None

    return {}, None


# ── Cookie source resolution ─────────────────────────────────────────────────

def resolve_cookie_source(config: dict, config_path: Path):
    """
    Figure out where the .ROBLOSECURITY cookie comes from. Returns
    (cookie_value, cookie_path_or_None).

    Priority:
      1. `cookie_file` — path to a plain-text file containing JUST the
         cookie value on a single line. This is the recommended form
         when running multiple kid daemons against ONE dedicated
         parent-account cookie (which is the only architecture that
         actually keeps a cookie alive long-term, since the kid's own
         account sessions invalidate cookies when the kid plays on
         their own device). All kid configs point cookie_file at the
         same file; cookie rotation writes back to that file and every
         daemon picks the new value up on its next auth retry.
      2. Inline `roblosecurity_cookie` field in the kid config
         (backward compat). cookie_path is None in this case so
         rotation continues to write back to the kid JSON.

    Relative cookie_file paths resolve next to the kid config (same
    convention as whitelist_file).
    """
    cookie_file = config.get("cookie_file", "")
    inline = config.get("roblosecurity_cookie", "")

    if cookie_file:
        cookie_path = Path(cookie_file)
        if not cookie_path.is_absolute():
            cookie_path = Path(config_path).parent / cookie_path
        try:
            value = cookie_path.read_text().strip()
        except FileNotFoundError:
            value = ""
        # If both are set, file wins but we keep inline as a usable
        # bootstrap if the file is empty (lets users transition without
        # racing the daemon's first read).
        if not value and inline and inline != "PASTE_YOUR_COOKIE_HERE":
            value = inline
        return value, cookie_path

    return inline, None


# ── Roblox API helpers ───────────────────────────────────────────────────────

API_PRESENCE = "https://presence.roblox.com/v1/presence/users"
API_UNIVERSE_DETAILS = "https://games.roblox.com/v1/games"
API_AUTHENTICATED = "https://users.roblox.com/v1/users/authenticated"
# Note: Roblox's session-invalidation endpoints (logoutfromallsessions...,
# /v2/logout, etc.) require step-up auth (RBXBoundAuthToken cookie issued
# only via the live browser login flow) that cookie-only API clients can't
# obtain. The guardian therefore notifies parents via SMS rather than
# attempting programmatic kicks. See README "Troubleshooting".

PRESENCE_TYPE_NAMES = {0: "offline", 1: "website", 2: "in-game", 3: "in-studio"}


class RobloxSession:
    """
    Manages a Roblox API session with automatic CSRF token handling
    and cookie rotation (required since May 2026 format changes).

    Roblox now periodically rotates .ROBLOSECURITY cookies via Set-Cookie
    response headers. If a script ignores these, the old cookie eventually
    returns 401. This class intercepts Set-Cookie on EVERY response and
    persists the new cookie to the config file automatically.
    """

    def __init__(self, cookie: str, config_path: Path = CONFIG_FILE,
                 logger: logging.Logger = None,
                 cookie_path: Path = None):
        self.cookie = cookie
        self.csrf_token = ""
        self.config_path = config_path
        # When cookie_path is set, rotated cookies are written there
        # (raw text, chmod 600) instead of being patched into the kid
        # config JSON. Lets multiple kid daemons share one cookie file.
        self.cookie_path = cookie_path
        self.logger = logger or logging.getLogger("guardian")

    def _build_headers(self, extra: dict = None) -> dict:
        headers = {
            "Cookie": f".ROBLOSECURITY={self.cookie}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Roblox filters out requests with Python-urllib/* default UA
            # on several endpoints (omni-search returns empty, some auth
            # endpoints can 401). A Mozilla-prefixed UA is enough to
            # pass the filter.
            "User-Agent": "Mozilla/5.0 (RobloxWhitelistGuardian)",
        }
        if self.csrf_token:
            headers["X-CSRF-TOKEN"] = self.csrf_token
        if extra:
            headers.update(extra)
        return headers

    def _check_cookie_rotation(self, headers):
        """
        Check every API response for Set-Cookie headers containing a
        rotated .ROBLOSECURITY value. If found, update in memory and
        persist to config. This is REQUIRED since the May 2026 changes —
        Roblox rotates cookies opportunistically on any response, not
        only on logout-and-reauth.

        Defensively reject anything that doesn't look like a real cookie
        (logout responses can carry `.ROBLOSECURITY=deleted; Expires=...`
        or similar invalidation sentinels — saving those would clobber
        the user's working cookie with garbage).
        """
        new_cookie = self._extract_cookie(headers)
        if not new_cookie or new_cookie == self.cookie:
            return
        if not self._looks_like_real_cookie(new_cookie):
            self.logger.debug(
                f"Ignoring suspect Set-Cookie value "
                f"(len={len(new_cookie)}, prefix={new_cookie[:20]!r}) — "
                f"likely a logout sentinel, not a rotation."
            )
            return
        old_prefix = self.cookie[:20]
        self.cookie = new_cookie
        self._save_cookie(new_cookie)
        self.logger.info(
            f"🔑 Cookie rotated by Roblox "
            f"({old_prefix}... → {new_cookie[:20]}...)"
        )

    @staticmethod
    def _looks_like_real_cookie(value: str) -> bool:
        """
        A real .ROBLOSECURITY is hundreds of characters long and starts
        with the WARNING preamble Roblox embeds in every issued cookie.
        Logout/invalidation sentinels (`deleted`, empty, `""`, short
        opaque blobs) all fail this check.
        """
        if not value or len(value) < 100:
            return False
        return "WARNING" in value

    def request(self, url: str, method: str = "GET",
                data: dict = None, retry_csrf: bool = True) -> dict:
        """
        Make an authenticated Roblox API request.
        Handles both CSRF token rotation (403 → retry with new token)
        and cookie rotation (Set-Cookie on any response).
        """
        body = json.dumps(data).encode() if data else None
        effective_method = "POST" if data and method == "GET" else method
        req = Request(url, data=body, headers=self._build_headers(),
                      method=effective_method)
        try:
            with urlopen(req, timeout=15) as resp:
                self._check_cookie_rotation(resp.headers)
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except HTTPError as e:
            # Check for cookie rotation even on error responses
            self._check_cookie_rotation(e.headers)
            # CSRF token rotation: Roblox returns 403 with new token
            if e.code == 403 and retry_csrf:
                new_token = e.headers.get("x-csrf-token", "")
                if new_token:
                    self.csrf_token = new_token
                    return self.request(url, method, data,
                                        retry_csrf=False)
            error_body = ""
            try:
                error_body = e.read().decode()
            except Exception:
                pass
            raise RuntimeError(
                f"Roblox API {e.code} @ {url}: {error_body}"
            ) from e
        except URLError as e:
            raise RuntimeError(f"Network error: {e.reason}") from e

    def request_raw(self, url: str, method: str = "POST",
                    data: dict = None, retry_csrf: bool = True,
                    extra_headers: dict = None, raw_body: bytes = None):
        """
        Like request() but returns the raw HTTPResponse so we can read
        Set-Cookie headers (needed for session re-auth).
        Returns (status_code, headers, body_str).

        `extra_headers` adds/overrides headers (e.g. a Referer, or a
        non-JSON Content-Type for endpoints that demand form-urlencoded).
        `raw_body` lets the caller send a literal byte body instead of
        json-encoding `data` — needed for form-urlencoded POSTs.
        """
        if raw_body is not None:
            body = raw_body
        else:
            body = json.dumps(data).encode() if data else None
        req = Request(url, data=body,
                      headers=self._build_headers(extra=extra_headers),
                      method=method)
        try:
            with urlopen(req, timeout=15) as resp:
                self._check_cookie_rotation(resp.headers)
                return resp.status, resp.headers, resp.read().decode()
        except HTTPError as e:
            self._check_cookie_rotation(e.headers)
            if e.code == 403 and retry_csrf:
                new_token = e.headers.get("x-csrf-token", "")
                if new_token:
                    self.csrf_token = new_token
                    return self.request_raw(
                        url, method, data,
                        retry_csrf=False,
                        extra_headers=extra_headers,
                        raw_body=raw_body,
                    )
            body_text = ""
            try:
                body_text = e.read().decode()
            except Exception:
                pass
            return e.code, e.headers, body_text

    def whoami(self) -> dict | None:
        """
        Validate the cookie by hitting the /authenticated endpoint.
        Returns user info dict on success, or None if the cookie is
        invalid (401). Raises on other errors (network, unexpected status).
        """
        status, _headers, body = self.request_raw(
            API_AUTHENTICATED, method="GET"
        )
        if status == 401:
            return None
        if 200 <= status < 300:
            return json.loads(body) if body.strip() else {}
        raise RuntimeError(
            f"whoami got unexpected status {status} from "
            f"{API_AUTHENTICATED}: {body}"
        )

    def get_presence(self, user_id: int) -> dict:
        """Get current presence/game status for a user."""
        result = self.request(API_PRESENCE, data={"userIds": [user_id]})
        presences = result.get("userPresences", [])
        if not presences:
            raise RuntimeError(f"No presence data for user {user_id}")
        return presences[0]

    def _extract_cookie(self, headers) -> str:
        """Extract .ROBLOSECURITY from Set-Cookie headers."""
        # headers can have multiple Set-Cookie entries
        cookies = headers.get_all("Set-Cookie") or []
        if isinstance(cookies, str):
            cookies = [cookies]
        for cookie_str in cookies:
            match = re.search(
                r'\.ROBLOSECURITY=([^;]+)', cookie_str
            )
            if match:
                return match.group(1)
        return ""

    def _save_cookie(self, new_cookie: str):
        """
        Persist the rotated cookie atomically. If we're configured to
        use a shared cookie file (cookie_path), write raw text there;
        otherwise patch the value into the kid config JSON. Both paths
        write via a sibling temp + os.replace so a crash mid-write
        can't truncate the destination.

        Non-fatal on failure: the in-memory cookie is already updated,
        so the running process keeps working with the new value even
        if we can't persist.
        """
        try:
            if self.cookie_path is not None:
                tmp_path = f"{self.cookie_path}.tmp"
                with open(tmp_path, "w") as f:
                    f.write(new_cookie + "\n")
                # chmod BEFORE replace so the live file is never
                # world-readable, even for a moment.
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
                os.replace(tmp_path, self.cookie_path)
                return

            with open(self.config_path) as f:
                config = json.load(f)
            config["roblosecurity_cookie"] = new_cookie
            tmp_path = f"{self.config_path}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, self.config_path)
        except Exception:
            pass


# ── Process management ───────────────────────────────────────────────────────

def kill_roblox() -> list[str]:
    """
    Kill all Roblox-related processes. Returns list of killed process names.

    IMPORTANT: never use `pkill -f` here. The -f flag matches the full
    command line, which means it would match our OWN Python process
    (because this script's filename literally contains "roblox") and
    the guardian would terminate itself the moment local enforcement
    fires. Always match on exact basename via `pkill -x` (POSIX) or
    `taskkill /IM <image.exe>` (Windows).
    """
    system = platform.system()
    killed = []

    if system == "Windows":
        # /IM matches exact image name — already self-safe.
        targets = [
            "RobloxPlayerBeta.exe",
            "RobloxPlayerLauncher.exe",
            "RobloxCrashHandler.exe",
        ]
        for proc in targets:
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/IM", proc],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    killed.append(proc)
            except Exception:
                pass
        return killed

    # POSIX (macOS / Linux): pkill -x matches the process basename
    # exactly, not the command line. Our Python script (basename
    # "python3") will not match "Roblox", "RobloxPlayer", or "sober".
    if system == "Darwin":
        targets = ["Roblox", "RobloxPlayer"]
    else:
        # Native Roblox doesn't exist on Linux; users typically run
        # Sober (the Linux Roblox wrapper). RobloxPlayerBeta.exe covers
        # Wine setups, though Linux truncates process comm to 15 chars
        # so it may not match exactly — keep it for best-effort.
        targets = ["sober", "RobloxPlayerBeta"]

    for proc in targets:
        try:
            result = subprocess.run(
                ["pkill", "-x", proc],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                killed.append(proc)
        except Exception:
            pass
    return killed


def send_notification(title: str, message: str):
    """Send a desktop notification (best-effort, cross-platform)."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run([
                "osascript", "-e",
                f'display notification "{message}" with title "{title}"'
            ], timeout=5)
        elif system == "Windows":
            ps_script = (
                f'[Windows.UI.Notifications.ToastNotificationManager,'
                f'Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null; '
                f'$template = [Windows.UI.Notifications.ToastNotificationManager]::'
                f'GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::'
                f'ToastText02); '
                f'$text = $template.GetElementsByTagName("text"); '
                f'$text[0].AppendChild($template.CreateTextNode("{title}")); '
                f'$text[1].AppendChild($template.CreateTextNode("{message}")); '
                f'$notifier = [Windows.UI.Notifications.ToastNotificationManager]::'
                f'CreateToastNotifier("Roblox Guardian"); '
                f'$notifier.Show([Windows.UI.Notifications.ToastNotification]::new($template))'
            )
            subprocess.run(["powershell", "-Command", ps_script],
                           capture_output=True, timeout=5)
        else:
            subprocess.run(["notify-send", title, message], timeout=5)
    except Exception:
        pass


# ── Telegram notifications ───────────────────────────────────────────────────

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def send_telegram_message(bot_token: str, chat_id, text: str,
                          logger: logging.Logger) -> bool:
    """
    Send a Telegram message to a single chat_id via the bot's sendMessage
    endpoint. Returns True on success.

    chat_id is an integer (positive for private chats, negative for
    groups). bot_token comes from @BotFather when the bot is created.
    Telegram has no per-message cost and no carrier hassles, so this
    is free as long as recipients have started a chat with the bot.
    """
    if not bot_token:
        logger.error("   Telegram: bot token missing; skipping.")
        return False

    url = f"{TELEGRAM_API_BASE.format(token=bot_token)}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4096],   # Telegram caps message text at 4096 chars
        "disable_web_page_preview": True,
    }).encode()
    req = Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, method="POST")
    try:
        with urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
            if body.get("ok"):
                return True
            logger.error(
                f"   Telegram to chat {chat_id} returned not-ok: "
                f"{body.get('description', '<no description>')}"
            )
            return False
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            pass
        # Common failure: 400 "chat not found" → recipient never /started
        # the bot, OR the chat_id is wrong.
        logger.error(
            f"   Telegram to chat {chat_id} failed: HTTP {e.code} — {err_body}"
        )
        return False
    except URLError as e:
        logger.error(
            f"   Telegram to chat {chat_id} network error: {e.reason}"
        )
        return False


def telegram_get_updates(bot_token: str, offset: int = 0,
                         timeout_seconds: int = 30) -> list:
    """
    Long-poll the bot's getUpdates endpoint. Returns the `result` array
    (list of update dicts) — used by --list-chats to discover the chat
    IDs of parents who have messaged the bot.
    """
    url = (
        f"{TELEGRAM_API_BASE.format(token=bot_token)}/getUpdates"
        f"?offset={offset}&timeout={timeout_seconds}"
    )
    # urlopen's timeout must exceed Telegram's long-poll window.
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_seconds + 5) as r:
        body = json.loads(r.read().decode())
    if not body.get("ok"):
        raise RuntimeError(
            f"getUpdates failed: {body.get('description', body)}"
        )
    return body.get("result", [])


# ── Core monitor loop ────────────────────────────────────────────────────────

class WhitelistGuardian:
    def __init__(self, config: dict, dry_run: bool = False,
                 debug: bool = False, config_path: Path = None):
        self.user_id = config["roblox_user_id"]
        self.config_path = config_path or CONFIG_FILE
        # Log file is derived from the config path: `emma.json` → `emma.log`.
        # An explicit `log_file` in the config still wins (backward compat).
        default_log_path = str(Path(self.config_path).with_suffix(".log"))
        log_path = config.get("log_file") or default_log_path
        self.logger = setup_logging(log_path, debug=debug)
        cookie_value, self._cookie_path = resolve_cookie_source(
            config, self.config_path
        )
        self.session = RobloxSession(
            cookie_value,
            config_path=self.config_path,
            logger=self.logger,
            cookie_path=self._cookie_path,
        )
        self.poll_interval = config.get("poll_interval_seconds", 5)
        self.notify = config.get("notify_parent", True)
        self.dry_run = dry_run
        self.debug = debug

        # Telegram notification config. The bot token is a secret —
        # env var TELEGRAM_BOT_TOKEN takes precedence over the config
        # file so it can stay off-disk if you'd like.
        self.child_display_name = config.get("child_display_name", "")
        self.parent_chat_ids = list(config.get("parent_chat_ids", []))
        self.telegram_bot_token = (
            os.environ.get("TELEGRAM_BOT_TOKEN")
            or config.get("telegram_bot_token", "")
        )
        self.kill_local_process = bool(
            config.get("kill_local_process_on_violation", False)
        )

        # We'll be ready to identify the child in SMS bodies once
        # whoami() lands at startup. Pre-seed from config as fallback.
        self.account_name = ""

        # Resolve the universe whitelist (file pointer preferred over
        # inline). load_whitelist will raise if the referenced file is
        # missing or malformed — that's intentional; the daemon should
        # not silently allow everything if its whitelist is broken.
        # The source path (if any) is kept so the main loop can watch
        # it for mtime changes and hot-reload added games without a
        # daemon restart.
        self.whitelist: dict[int, str]
        self.whitelist, self._whitelist_path = load_whitelist(
            config, self.config_path, self.logger
        )
        self._whitelist_mtime = self._stat_whitelist_mtime()
        # Track the mtime of the last failed reload so we only log the
        # error once per mid-edit save, not on every poll thereafter.
        self._whitelist_failed_mtime = None

        self.running = True
        self._last_universe = None
        self._violation_count = 0

        # Warn about outdated cookie format (May 2026 changes). Use
        # the resolved value, not config[...] — cookie_file configs
        # don't have an inline roblosecurity_cookie.
        self._check_cookie_format(cookie_value)

        # Deprecation notice for old config fields.
        deprecated_used = [k for k in (
            "enforcement_mode", "parent_phone_numbers", "twilio_account_sid",
            "twilio_auth_token", "twilio_from_number",
        ) if k in config]
        if deprecated_used:
            self.logger.warning(
                f"Config has deprecated field(s) {deprecated_used}; "
                "they're ignored. The guardian now notifies via Telegram "
                "(parent_chat_ids + telegram_bot_token). See README."
            )

    def stop(self, *_):
        self.running = False
        self.logger.info("Shutting down guardian.")

    def _interruptible_sleep(self, seconds: float):
        """
        Sleep up to `seconds`, but wake every second to check self.running
        so SIGTERM/SIGINT during a long auth backoff exits within ~1s
        rather than waiting out the full delay.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        """
        Detect a 401-shaped RuntimeError raised by RobloxSession.request().
        The message is f"Roblox API {code} @ {url}: ..." so a substring
        match on " 401 " is the cleanest test without restructuring the
        exception class hierarchy.
        """
        return " 401 " in f" {exc} "

    def _auth_backoff_seconds(self, attempt: int) -> float:
        """
        Pick the next auth-backoff delay. Saturates at the last step and
        applies ±20% jitter so multiple daemons that fall into 401 at
        once don't all wake at the same instant and re-hammer.
        """
        step = AUTH_BACKOFF_STEPS[min(attempt, len(AUTH_BACKOFF_STEPS) - 1)]
        return step * random.uniform(0.8, 1.2)

    def _stat_whitelist_mtime(self):
        """
        Return the whitelist file's current mtime, or None if it's not
        a file-backed whitelist (inline configs) or the file is gone.
        """
        if not self._whitelist_path:
            return None
        try:
            return self._whitelist_path.stat().st_mtime
        except OSError:
            return None

    def _maybe_reload_whitelist(self):
        """
        If the whitelist file's mtime has changed since we last loaded
        it, re-parse it and atomically swap in the new map. Log added/
        removed universes so the change is auditable.

        On parse error (file is mid-edit and not yet valid JSON), keep
        the in-memory whitelist intact and log a single warning per
        bad-mtime — we don't want to spam the log every 10s while the
        user is composing a multi-line edit.

        No-op for inline whitelists (no file to watch).
        """
        if not self._whitelist_path:
            return
        current_mtime = self._stat_whitelist_mtime()
        if current_mtime is None or current_mtime == self._whitelist_mtime:
            return

        try:
            new_wl = _read_whitelist_file(self._whitelist_path)
        except RuntimeError as e:
            if self._whitelist_failed_mtime != current_mtime:
                self.logger.warning(
                    f"Whitelist reload failed (keeping previous): {e}"
                )
                self._whitelist_failed_mtime = current_mtime
            return

        added = sorted(set(new_wl) - set(self.whitelist))
        removed = sorted(set(self.whitelist) - set(new_wl))
        if not added and not removed:
            # File touched but contents unchanged (rename/save no-op).
            # Bump the mtime so we don't keep re-reading.
            self._whitelist_mtime = current_mtime
            return

        self.whitelist = new_wl
        self._whitelist_mtime = current_mtime
        self._whitelist_failed_mtime = None

        self.logger.info(
            f"🔄 Whitelist reloaded: +{len(added)} / -{len(removed)} "
            f"({len(self.whitelist)} total)"
        )
        for uid in added:
            self.logger.info(f"  + {uid} — {self.whitelist[uid]}")
        for uid in removed:
            # We no longer have the friendly name; log just the ID.
            self.logger.info(f"  - {uid}")

    def _reload_cookie_from_disk(self):
        """
        Pick up a fresh .ROBLOSECURITY the user may have edited in
        while we were sleeping in auth backoff. Reads from the shared
        cookie file if one is configured, otherwise from the kid
        config JSON. Without this, a user updating the cookie wouldn't
        take effect until the daemon was restarted — defeating the
        point of staying running through a 401 storm.

        Also picks up rotations written by SIBLING daemons sharing the
        same cookie file: when daemon A's traffic causes Roblox to
        rotate, A writes the new value to the shared file and B (now
        in auth backoff with a stale in-memory copy) picks it up on
        its next attempt.
        """
        try:
            if self._cookie_path is not None:
                try:
                    disk_cookie = self._cookie_path.read_text().strip()
                except FileNotFoundError:
                    return
            else:
                with open(self.config_path) as f:
                    disk_cfg = json.load(f)
                disk_cookie = disk_cfg.get("roblosecurity_cookie", "")

            if disk_cookie and disk_cookie != self.session.cookie:
                self.logger.info(
                    "Detected updated cookie on disk; loading fresh value."
                )
                self.session.cookie = disk_cookie
        except Exception as e:
            self.logger.debug(f"Could not re-read cookie from disk: {e}")

    def _auth_loop(self, start_attempt: int = 0):
        """
        Validate the cookie via whoami(), retrying with long backoff on
        401 or network errors. Returns the user dict on success, or None
        if the daemon was asked to stop during backoff.

        Crucially this does NOT exit the process on 401 — systemd would
        restart us in ~60s and we'd hammer Roblox's auth endpoint with
        a dead cookie. Sleeping in-process for minutes-to-an-hour gives
        the human time to grab a fresh cookie (or for Roblox to stop
        being upset with the IP) without flooding their servers.
        """
        attempt = start_attempt
        while self.running:
            # Each retry re-reads the cookie from disk in case the user
            # pasted a fresh one in while we were sleeping.
            self._reload_cookie_from_disk()
            try:
                me = self.session.whoami()
            except RuntimeError as e:
                delay = self._auth_backoff_seconds(attempt)
                self.logger.error(
                    f"whoami network/API error: {e}. "
                    f"Sleeping {delay/60:.1f} min before retry."
                )
                self._interruptible_sleep(delay)
                attempt += 1
                continue

            if me is None:
                delay = self._auth_backoff_seconds(attempt)
                self.logger.error(
                    "Cookie is INVALID (Roblox returned 401)."
                )
                if attempt == 0:
                    self.logger.error(
                        "→ Grab a fresh .ROBLOSECURITY from DevTools and "
                        "update the config file."
                    )
                    self.logger.error(
                        "→ The daemon will NOT exit; it'll keep re-checking "
                        "with long backoff so it doesn't hammer Roblox."
                    )
                self.logger.error(
                    f"Sleeping {delay/60:.1f} min before re-checking cookie."
                )
                self._interruptible_sleep(delay)
                attempt += 1
                continue

            return me
        return None

    @staticmethod
    def _is_outdated_cookie(cookie: str) -> bool:
        """
        Check if a cookie matches the deprecated format that Roblox
        began rejecting after May 1, 2026.

        Outdated formats:
          <Warning><Hex String>
          <Warning>GgIQAQ.<Hex String>
        """
        warning = (
            r"_\|WARNING:-DO-NOT-SHARE-THIS\.--Sharing-this-will-allow-"
            r"someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-"
            r"items\.\|_"
        )
        outdated_re = re.compile(
            rf"^({warning})(GgIQAQ\.)?([0-9A-F]+)$"
        )
        return bool(outdated_re.match(cookie))

    def _check_cookie_format(self, cookie: str):
        """Warn at startup if the cookie uses a deprecated format."""
        if self._is_outdated_cookie(cookie):
            self.logger.warning(
                "⚠  Your .ROBLOSECURITY cookie uses the OLD format "
                "(deprecated May 2026). This will cause 401 errors."
            )
            self.logger.warning(
                "   Fix: Log into roblox.com in a fresh browser session, "
                "copy the new cookie from DevTools, and update the config."
            )
            self.logger.warning(
                "   The new cookie format will look different from the "
                "old hex-only format. The script will auto-rotate it "
                "going forward once you provide a valid one."
            )

    def _child_label(self) -> str:
        """Pick the most parent-friendly name to identify the child in
        SMS bodies: explicit display name > Roblox username > user ID."""
        return (
            self.child_display_name
            or self.account_name
            or f"User {self.user_id}"
        )

    def _notify_parents_telegram(self, universe_id, last_location):
        """
        Send a violation Telegram message to every chat in
        parent_chat_ids. Non-blocking on failure — one bad chat_id
        doesn't stop the others.
        """
        if not self.parent_chat_ids:
            self.logger.warning(
                "   No parent_chat_ids configured — skipping Telegram. "
                "Run '--list-chats' to discover chat IDs for each parent."
            )
            return
        if not self.telegram_bot_token:
            self.logger.warning(
                "   Telegram bot token not configured — skipping. "
                "Set telegram_bot_token in the config, or the "
                "TELEGRAM_BOT_TOKEN env var."
            )
            return

        child = self._child_label()
        experience = last_location.strip() or f"Universe {universe_id}"
        text = (
            f"🚫 Roblox Guardian: {child} joined a non-whitelisted "
            f'experience: "{experience}".'
        )

        sent = 0
        for chat_id in self.parent_chat_ids:
            ok = send_telegram_message(
                self.telegram_bot_token, chat_id, text, self.logger
            )
            if ok:
                sent += 1
                self.logger.info(f"   ✓ Telegram sent to chat {chat_id}")
        self.logger.info(
            f"   Notified {sent}/{len(self.parent_chat_ids)} parent chat(s)."
        )

    def _enforce(self, universe_id, place_id, last_location):
        """
        Handle a non-whitelisted-game detection: log it, fire desktop
        notification, Telegram every parent, and (optionally) kill the
        local Roblox process. Roblox no longer permits programmatic
        remote session invalidation so the parent must intervene — the
        Telegram message gets them informed within seconds.
        """
        self._violation_count += 1

        self.logger.warning(
            f"🚫 BLOCKED — Non-whitelisted game detected "
            f"(universe={universe_id}, place={place_id}, "
            f"location=\"{last_location}\") "
            f"[violation #{self._violation_count}]"
        )

        if self.notify:
            send_notification(
                "Roblox Guardian",
                f"{self._child_label()} joined non-whitelisted "
                f"\"{last_location or universe_id}\""
            )

        if self.dry_run:
            self.logger.info(
                "   [DRY-RUN] Would notify parents + kill local, skipping."
            )
            return

        self._notify_parents_telegram(universe_id, last_location)

        if self.kill_local_process:
            killed = kill_roblox()
            if killed:
                self.logger.info(
                    f"   ✓ Killed local Roblox process: "
                    f"{', '.join(killed)}"
                )
            else:
                self.logger.info(
                    "   No local Roblox process found "
                    "(child likely on another device)."
                )

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        actions = ["TELEGRAM"] if self.parent_chat_ids else []
        if self.kill_local_process:
            actions.append("local-kill")
        mode_label = "DRY-RUN" if self.dry_run else (
            "+".join(actions) if actions else "DETECT-ONLY"
        )
        self.logger.info(
            f"Guardian started [{mode_label}] — monitoring user {self.user_id}"
        )
        self.logger.info(
            f"Parent Telegram chats: {len(self.parent_chat_ids)}"
        )

        # When systemd batch-starts N kid daemons together, they'd
        # otherwise all hit Roblox in the same second. A small random
        # delay desynchronises them; ±0–10s is plenty given a 10s+
        # steady-state poll interval.
        jitter = random.uniform(0, 10)
        self.logger.info(f"Startup jitter: sleeping {jitter:.1f}s")
        self._interruptible_sleep(jitter)
        if not self.running:
            return

        # Validate cookie. _auth_loop sleeps in-process with long
        # backoff on 401 rather than letting the daemon exit — exiting
        # means systemd respawns us in ~60s and we hammer
        # /users/authenticated with a dead cookie, which is exactly
        # what makes Roblox roll cookies in the first place.
        self.logger.info("Validating .ROBLOSECURITY cookie...")
        me = self._auth_loop()
        if me is None:
            self.logger.info(
                f"Guardian stopped. Total violations blocked: "
                f"{self._violation_count}"
            )
            return

        self.account_name = me.get("name", "") or ""
        self.logger.info(
            f"Authenticated as {me.get('name')} (id={me.get('id')})"
        )
        if me.get("id") != self.user_id:
            # With a shared parent-account cookie (recommended for
            # multi-kid setups) this mismatch is expected: one bot
            # account watches N kids. Log at info, not warning, when
            # cookie_file is in use; warning otherwise.
            level = (self.logger.info if self._cookie_path is not None
                     else self.logger.warning)
            level(
                f"Cookie belongs to user {me.get('id')} ({me.get('name')}); "
                f"this daemon monitors user {self.user_id} via the Presence "
                f"API. (Expected when sharing a parent-account cookie file.)"
            )

        self.logger.info(f"Whitelisted universes: {len(self.whitelist)}")
        for uid, name in self.whitelist.items():
            self.logger.info(f"  ✓ {uid} — {name}")

        if not self.whitelist:
            self.logger.warning(
                "⚠  Whitelist is EMPTY — ALL games will be blocked!"
            )

        consecutive_errors = 0
        auth_backoff_attempt = 0

        while self.running:
            # Cheap stat() — picks up edits to whitelist_universes.json
            # within one poll cycle, no daemon restart needed.
            self._maybe_reload_whitelist()

            try:
                presence = self.session.get_presence(self.user_id)
                consecutive_errors = 0
                auth_backoff_attempt = 0

                presence_type = presence.get("userPresenceType", 0)
                universe_id = presence.get("universeId")
                place_id = presence.get("placeId")
                last_location = presence.get("lastLocation", "")

                self.logger.debug(
                    f"poll → type={presence_type} "
                    f"({PRESENCE_TYPE_NAMES.get(presence_type, '?')}), "
                    f"universe={universe_id}, place={place_id}, "
                    f"location={last_location!r}"
                )

                # presenceType: 0=offline, 1=website, 2=in-game, 3=in-studio
                if presence_type == 2 and universe_id:
                    if universe_id != self._last_universe:
                        self._last_universe = universe_id

                        if universe_id in self.whitelist:
                            name = self.whitelist[universe_id]
                            self.logger.info(
                                f"✅ ALLOWED — Joined \"{name}\" "
                                f"(universe={universe_id}, place={place_id})"
                            )
                        else:
                            self._enforce(
                                universe_id, place_id, last_location
                            )

                elif presence_type != 2:
                    if self._last_universe is not None:
                        self.logger.info("Player left game / went offline.")
                        self._last_universe = None

            except RuntimeError as e:
                # A mid-session 401 means the cookie just went dead.
                # Re-validate via the same long-backoff auth loop instead
                # of churning poll cycles against an endpoint that's
                # already saying no.
                if self._is_auth_error(e):
                    self.logger.error(
                        f"Mid-session 401 — cookie invalidated. {e}"
                    )
                    me = self._auth_loop(start_attempt=auth_backoff_attempt)
                    auth_backoff_attempt += 1
                    if me is None:
                        break
                    self.account_name = me.get("name", "") or ""
                    self.logger.info(
                        f"Cookie re-validated after 401: {me.get('name')}"
                    )
                    consecutive_errors = 0
                    # _auth_loop already slept; skip the bottom sleep.
                    continue

                consecutive_errors += 1
                self.logger.error(f"API error: {e}")
                if consecutive_errors >= 5:
                    self.logger.error(
                        f"{consecutive_errors} consecutive transient "
                        f"errors — extra backoff."
                    )
                    self._interruptible_sleep(
                        min(self.poll_interval * consecutive_errors, 60)
                    )

            except Exception as e:
                consecutive_errors += 1
                self.logger.error(f"Unexpected error: {e}")

            self._interruptible_sleep(self.poll_interval)

        self.logger.info(
            f"Guardian stopped. Total violations blocked: "
            f"{self._violation_count}"
        )


# ── Game lookup helper ───────────────────────────────────────────────────────

def lookup_game(keyword: str, cookie: str = ""):
    """
    Search for a Roblox game and print its Universe ID.

    Uses the modern omni-search endpoint (apis.roblox.com/search-api/
    omni-search). The legacy /v1/games/list?model.keyword endpoint was
    removed by Roblox and now returns 404.

    Two quirks of omni-search:
      - It requires a non-browser-like response refusal unless a real
        User-Agent header is set; otherwise it returns empty results.
      - It requires a `sessionId` query parameter, but any non-empty
        value works for one-off searches.
    """
    print(f"\nSearching for: \"{keyword}\"\n")

    url = (f"https://apis.roblox.com/search-api/omni-search?"
           f"searchQuery={quote(keyword)}"
           f"&pageType=all&sessionId=guardian-lookup")
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (RobloxWhitelistGuardian)",
    }
    if cookie and cookie != "PASTE_YOUR_COOKIE_HERE":
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"

    req = Request(url, headers=headers)
    games = []
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            # omni-search returns a list of result groups; each group
            # has "contents" with the actual games. Flatten and keep
            # only the Game-typed entries.
            for group in data.get("searchResults", []):
                if group.get("contentGroupType") != "Game":
                    continue
                for entry in group.get("contents", []):
                    if entry.get("universeId"):
                        games.append(entry)
    except Exception as e:
        print(f"  Search API error: {e}")
        print("  Try finding the Universe ID manually:")
        print("  1. Go to the game's page on roblox.com")
        print("  2. The URL will be: roblox.com/games/PLACE_ID/...")
        print("  3. Use --place-to-universe PLACE_ID to convert it\n")
        return

    if not games:
        print("  No results found. Try a different search term,")
        print("  or use --place-to-universe with the Place ID from the URL.\n")
        return

    print(f"  {'Universe ID':<15} {'Name'}")
    print(f"  {'─'*15} {'─'*50}")
    for g in games[:10]:
        uid = g.get("universeId", "?")
        name = g.get("name", "Unknown")
        creator = g.get("creatorName", "?")
        playing = g.get("playerCount", 0)
        print(f"  {uid:<15} {name}  (by {creator}, {playing:,} playing)")

    print(f"\nAdd the Universe ID to your whitelist_config.json like this:")
    if games:
        ex = games[0]
        print(f'  "{ex.get("universeId", "ID")}": "{ex.get("name", "Name")}"\n')


def cmd_check(config: dict, config_path: Path = CONFIG_FILE) -> int:
    """
    Preflight diagnostic: verify the cookie is valid and that Roblox is
    reporting enough presence data for the guardian to actually work.
    Run this BEFORE starting the daemon if things seem broken — it pins
    down whether the problem is the cookie, the account's privacy
    settings, or something else.
    """
    print("=" * 60)
    print("Roblox Guardian — preflight check")
    print("=" * 60)

    user_id = config.get("roblox_user_id", 0)
    cookie, cookie_path = resolve_cookie_source(config, config_path)
    if not user_id:
        print("\n  ✗ No roblox_user_id set in config.\n")
        return 1
    if not cookie or cookie == "PASTE_YOUR_COOKIE_HERE":
        if cookie_path is not None:
            print(f"\n  ✗ cookie_file points at {cookie_path} but it's")
            print( "    missing or empty. Paste the .ROBLOSECURITY value")
            print( "    (just the cookie string, one line) into that file.\n")
        else:
            print("\n  ✗ No .ROBLOSECURITY cookie set in config.\n")
        return 1

    if cookie_path is not None:
        print(f"\nCookie source: {cookie_path} (shared)")
    else:
        print(f"\nCookie source: inline in {config_path.name}")

    session = RobloxSession(cookie)

    # Report this machine's public egress IP. Roblox locks cookies
    # to the IP region where they were issued (active since Mar 2022),
    # so if you grabbed the cookie elsewhere this IP won't match and
    # every authenticated call will 401 immediately.
    try:
        with urlopen("https://api.ipify.org?format=json", timeout=5) as r:
            ip = json.loads(r.read().decode()).get("ip", "?")
        print(f"\nThis machine's public IP: {ip}")
        print("(Cookie must have been grabbed from a browser on an IP")
        print(" in the same region as this one — Roblox 401s otherwise.)")
    except Exception:
        print("\n(Could not determine public IP. If you see a 401 below,")
        print(" verify the cookie was grabbed from this same machine/network.)")

    # 0. Cookie format (deprecated formats won't authenticate at all)
    if WhitelistGuardian._is_outdated_cookie(cookie):
        print()
        print("  ✗ Cookie uses the OLD format (deprecated May 2026).")
        print("    Roblox no longer accepts hex-only / GgIQAQ.<hex> cookies.")
        print("    Grab a fresh cookie from a current browser session and")
        print("    paste it into whitelist_config.json.\n")
        return 1

    # Track overall pass/fail but always continue so the SMS section
    # gets reported even when the cookie or presence checks bail —
    # fixing SMS config doesn't require an authenticated cookie.
    exit_code = 0

    # 1. Cookie validity
    print("\n[1/2] Validating .ROBLOSECURITY cookie...")
    me = None
    try:
        me = session.whoami()
    except RuntimeError as e:
        print(f"  ✗ Network/API error: {e}")
        exit_code = 1
    if me is None and exit_code == 0:
        print("  ✗ Cookie is INVALID — Roblox returned 401.")
        print("    Get a fresh cookie:")
        print("    1. Log into the account in a fresh browser window.")
        print("    2. DevTools (F12) → Application → Cookies → .ROBLOSECURITY")
        print("    3. Paste the value into whitelist_config.json.")
        print("    4. Don't log in elsewhere afterward — that invalidates it.")
        exit_code = 1
    elif me:
        print(
            f"  ✓ Cookie is valid. Authenticated as: "
            f"{me.get('name')} (id={me.get('id')})"
        )
        if me.get("id") != user_id:
            marker = "·" if cookie_path is not None else "⚠"
            print(
                f"  {marker}  Cookie belongs to user {me.get('id')} "
                f"({me.get('name')}); config monitors {user_id}."
            )
            if cookie_path is not None:
                print("     (Expected — shared parent-account cookie pattern.)")
            else:
                print("     Presence works for any user, but confirm this is intentional.")

    # 2. Presence (skip if cookie failed)
    print(f"\n[2/2] Fetching presence for user {user_id}...")
    if me is None:
        print("  · skipped (cookie invalid)")
        _cmd_check_sms_section(config)
        return exit_code
    try:
        p = session.get_presence(user_id)
    except RuntimeError as e:
        print(f"  ✗ Presence API error: {e}")
        _cmd_check_sms_section(config)
        return 1
    pt = p.get("userPresenceType", 0)
    print(f"  · presenceType: {pt} ({PRESENCE_TYPE_NAMES.get(pt, '?')})")
    print(f"  · universeId:   {p.get('universeId')}")
    print(f"  · placeId:      {p.get('placeId')}")
    print(f"  · lastLocation: {p.get('lastLocation')!r}")

    if pt == 2 and p.get("universeId"):
        print("\n  ✓ Player is in-game — guardian can see the universe ID.")
    elif pt == 2:
        print("\n  ⚠  In-game flag set but universeId is null — unusual.")
    else:
        print()
        print("  ⚠  Player is NOT reported as in-game.")
        print("     If they ARE in a game right now (any device), the cause is")
        print("     almost certainly the account's presence privacy setting:")
        print("       Roblox → Settings → Privacy →")
        print("       'Who can see my online status?' → set to 'Everyone'.")
        print("     Without that, presence reports 'Website' even mid-game.")

    _cmd_check_sms_section(config)
    return exit_code


def _cmd_check_sms_section(config: dict):
    """Telegram-config part of the preflight, factored so we always
    print it even if the cookie/presence checks bailed early."""
    print(f"\n[Telegram] Notification configuration check...")
    chat_ids = config.get("parent_chat_ids", []) or []
    token = (os.environ.get("TELEGRAM_BOT_TOKEN")
             or config.get("telegram_bot_token", ""))
    if not chat_ids:
        print("  ⚠  parent_chat_ids is empty — no Telegram messages will be sent.")
        print(f"     → After creating the bot, run "
              f"'python {sys.argv[0]} --list-chats'")
        print( "       and have each parent send /start to the bot.")
    else:
        print(f"  · {len(chat_ids)} parent chat(s) configured:")
        for c in chat_ids:
            print(f"      {c}")
    if not token:
        print("  ⚠  Telegram bot token not set.")
        print( "     Create a bot via @BotFather in Telegram, then set")
        print( "     telegram_bot_token in the config OR TELEGRAM_BOT_TOKEN env var.")
    else:
        # Token format is <bot_id>:<auth_token> — show only the bot_id half.
        bot_id = token.split(":", 1)[0] if ":" in token else token[:8]
        print(f"  ✓ Bot token present (bot_id={bot_id}).")
        if chat_ids:
            print(f"     → Run 'python {sys.argv[0]} --test-telegram' to "
                  f"verify delivery.")
    print()


def cmd_test_telegram(config: dict) -> int:
    """
    Send a test Telegram message to every chat in parent_chat_ids so
    the user can verify the bot token + recipient chat IDs work before
    relying on the daemon to deliver during a real violation.
    """
    print("=" * 60)
    print("Roblox Guardian — Telegram delivery test")
    print("=" * 60)

    chat_ids = config.get("parent_chat_ids", []) or []
    if not chat_ids:
        print("\n  ✗ No parent_chat_ids configured. Run --list-chats")
        print("    to discover them after each parent has sent /start")
        print("    to the bot.\n")
        return 1

    token = (os.environ.get("TELEGRAM_BOT_TOKEN")
             or config.get("telegram_bot_token", ""))
    if not token:
        print("\n  ✗ Telegram bot token not set.")
        print("    Set telegram_bot_token in the config, or the")
        print("    TELEGRAM_BOT_TOKEN env var. Token comes from")
        print("    @BotFather when you create the bot.\n")
        return 1

    # Minimal stdout logger so send_telegram_message's error messages
    # surface nicely without polluting guardian.log.
    test_logger = logging.getLogger("guardian.tgtest")
    if not test_logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        test_logger.addHandler(h)
    test_logger.setLevel(logging.INFO)

    child = (config.get("child_display_name", "")
             or f"User {config.get('roblox_user_id', '?')}")
    text = (
        f"✅ Roblox Guardian test message for {child}. "
        f"If you received this, Telegram notifications are wired up correctly."
    )

    print(f"\nSending test message to {len(chat_ids)} chat(s)...\n")
    ok_count = 0
    for cid in chat_ids:
        ok = send_telegram_message(token, cid, text, test_logger)
        status = "✓ sent" if ok else "✗ failed"
        print(f"  {status}: chat {cid}")
        if ok:
            ok_count += 1
    print(f"\n{ok_count}/{len(chat_ids)} test message(s) delivered.")
    return 0 if ok_count == len(chat_ids) else 1


def cmd_list_chats(config: dict) -> int:
    """
    Discover the Telegram chat IDs of parents who have messaged the
    bot. Long-polls getUpdates for 60 seconds; each parent should send
    any message to the bot (e.g. /start) while this is running. Prints
    the unique chat IDs at the end so they can be pasted into
    parent_chat_ids in the config.
    """
    print("=" * 60)
    print("Roblox Guardian — Telegram chat ID discovery")
    print("=" * 60)

    token = (os.environ.get("TELEGRAM_BOT_TOKEN")
             or config.get("telegram_bot_token", ""))
    if not token:
        print("\n  ✗ Telegram bot token not set.")
        print("    Create a bot via @BotFather in Telegram, paste the")
        print("    token into telegram_bot_token (or TELEGRAM_BOT_TOKEN env),")
        print("    then re-run this command.\n")
        return 1

    print("\nListening for the next 60 seconds.")
    print("Each parent should now open Telegram, find the bot, and")
    print("send any message (e.g. /start). Chat IDs will appear here.\n")

    found: dict = {}   # chat_id → display name (first_name [last_name])

    def absorb(updates):
        new = False
        last_update_id = None
        for u in updates:
            last_update_id = u.get("update_id")
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            name_parts = [chat.get("first_name", ""),
                          chat.get("last_name", "")]
            label = " ".join(p for p in name_parts if p).strip() \
                or chat.get("title") or chat.get("username") or "?"
            if cid not in found:
                found[cid] = label
                print(f"  · chat_id={cid}  ({label})")
                new = True
        return last_update_id, new

    deadline = time.time() + 60
    offset = 0
    while time.time() < deadline:
        try:
            updates = telegram_get_updates(token, offset=offset,
                                           timeout_seconds=10)
        except (HTTPError, URLError, RuntimeError) as e:
            print(f"\n  ✗ Telegram API error: {e}")
            return 1
        last_id, _ = absorb(updates)
        if last_id is not None:
            offset = last_id + 1

    print()
    if not found:
        print("  No chats received any messages in the window.")
        print("  Make sure each parent sent /start to the bot while this")
        print("  command was running, then try again.\n")
        return 1

    print(f"  Found {len(found)} chat(s). Add this to whitelist_config.json:\n")
    print(f'    "parent_chat_ids": [{", ".join(str(c) for c in found)}]\n')
    return 0


def place_to_universe(place_id: int):
    """Convert a Place ID (from the URL) to a Universe ID."""
    url = f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            uid = data.get("universeId")
            if uid:
                print(f"\n  Place {place_id} → Universe {uid}")
                print(f'\n  Add to whitelist: "{uid}": "Game Name"\n')
            else:
                print(f"\n  Could not resolve Place {place_id}\n")
    except Exception as e:
        print(f"\n  Error: {e}\n")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Roblox Whitelist Guardian — parental control monitor"
    )
    parser.add_argument("--config", type=str, metavar="PATH",
                        help="Config file to use (default: whitelist_config.json "
                             "next to the script). Use one config per child "
                             "when monitoring multiple kids.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Monitor and log but don't enforce")
    parser.add_argument("--lookup", type=str, metavar="GAME_NAME",
                        help="Search for a game's Universe ID")
    parser.add_argument("--place-to-universe", type=int, metavar="PLACE_ID",
                        help="Convert a Place ID to Universe ID")
    parser.add_argument("--init", action="store_true",
                        help="Create a default config file")
    parser.add_argument("--check", action="store_true",
                        help="Run preflight diagnostics (cookie + Telegram config) and exit")
    parser.add_argument("--test-telegram", action="store_true",
                        help="Send a test message to every parent_chat_ids entry and exit")
    parser.add_argument("--list-chats", action="store_true",
                        help="Long-poll the bot for 60s to discover parent chat IDs and exit")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG logging (logs every poll's raw presence data)")
    args = parser.parse_args()

    # Resolve config path: --config overrides the default. All subcommands
    # honor this, so you can have e.g. emma.json + jacob.json side by side
    # and run two daemon instances (one per kid) with different --config
    # values plus a shared TELEGRAM_BOT_TOKEN env var.
    config_path = Path(args.config) if args.config else CONFIG_FILE

    # ── Lookup mode
    if args.lookup:
        cookie = ""
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            cookie = cfg.get("roblosecurity_cookie", "")
        lookup_game(args.lookup, cookie)
        return

    # ── Place → Universe conversion
    if args.place_to_universe:
        place_to_universe(args.place_to_universe)
        return

    # ── Init mode
    if args.init:
        if config_path.exists():
            print(f"Config already exists: {config_path}")
        else:
            with open(config_path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            print(f"Created config: {config_path}")

        # Also create the shared whitelist file next to the config if
        # missing. When you --init a SECOND kid's config, the first
        # kid's whitelist file is reused — that's the whole point of
        # splitting it out.
        wl_path = config_path.parent / DEFAULT_WHITELIST_FILENAME
        if wl_path.exists():
            print(f"Whitelist file already exists (reusing): {wl_path}")
        else:
            with open(wl_path, "w") as f:
                json.dump(DEFAULT_WHITELIST, f, indent=2)
            print(f"Created whitelist: {wl_path}")

        print()
        print("Next:")
        print(f"  1. Edit {config_path.name} with the kid's user_id, cookie,")
        print(f"     child_display_name, telegram_bot_token, parent_chat_ids.")
        print(f"  2. Edit {wl_path.name} with the allowed universe IDs.")
        print(f"     (Use --lookup or --place-to-universe to find them.)")
        return

    # ── Monitor mode
    if not config_path.exists():
        print(f"No config file found at {config_path}")
        if args.config:
            print(f"Run:  python {sys.argv[0]} --config {args.config} --init")
        else:
            print(f"Run:  python {sys.argv[0]} --init")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    # ── Preflight diagnostic
    if args.check:
        sys.exit(cmd_check(config, config_path))

    # ── Test Telegram delivery
    if args.test_telegram:
        sys.exit(cmd_test_telegram(config))

    # ── Discover Telegram chat IDs
    if args.list_chats:
        sys.exit(cmd_list_chats(config))

    if config.get("roblox_user_id", 0) == 0:
        print("ERROR: Set your child's roblox_user_id in the config file.")
        sys.exit(1)

    cookie_value, _ = resolve_cookie_source(config, config_path)
    if not cookie_value or cookie_value == "PASTE_YOUR_COOKIE_HERE":
        print("ERROR: No .ROBLOSECURITY cookie available.")
        if config.get("cookie_file"):
            print(f"  cookie_file={config['cookie_file']} is empty or missing.")
            print( "  Paste the cookie value into that file (one line, no JSON).")
        else:
            print("  1. Log into the Roblox account in a browser")
            print("  2. DevTools (F12) → Application → Cookies → .ROBLOSECURITY")
            print("  3. Copy the value into the config file, or set cookie_file")
            print("     to a shared cookie file (recommended for multi-kid).")
        sys.exit(1)

    guardian = WhitelistGuardian(config, dry_run=args.dry_run,
                                 debug=args.debug, config_path=config_path)
    guardian.run()


if __name__ == "__main__":
    main()
