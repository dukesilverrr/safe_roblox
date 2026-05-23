#!/usr/bin/env python3
"""
Roblox Whitelist Guardian
─────────────────────────
Parental control script that restricts a child's Roblox account to only
whitelisted experiences. Works for both the native Roblox app and browser.

How it works:
  1. Polls the Roblox Presence API every few seconds to see what game
     the monitored account is currently in.
  2. If the game is NOT on your whitelist, it kills the Roblox process
     (native app) or browser tab, pulling the child out of the game.
  3. Logs every action and optionally sends desktop notifications.

Setup:
  1. Get your child's Roblox User ID (from their profile URL).
  2. Get a .ROBLOSECURITY cookie (log into their account in a browser,
     open DevTools → Application → Cookies → .ROBLOSECURITY).
  3. Find the Universe IDs of games you want to allow (see helper below).
  4. Fill in whitelist_config.json and run this script.

Usage:
  python roblox_whitelist_guardian.py                  # normal mode
  python roblox_whitelist_guardian.py --lookup "Adopt Me"  # find a game's Universe ID
  python roblox_whitelist_guardian.py --dry-run         # monitor without killing
"""

import argparse
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import quote

# ── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "whitelist_config.json"
LOG_FILE = Path(__file__).parent / "guardian.log"

DEFAULT_CONFIG = {
    "roblox_user_id": 0,
    "roblosecurity_cookie": "PASTE_YOUR_COOKIE_HERE",
    "poll_interval_seconds": 5,
    "auto_kill": True,
    "notify_parent": True,
    "whitelisted_universes": {
        "# Example — replace with your own": "",
        "# Run:  python roblox_whitelist_guardian.py --lookup \"Game Name\"": "",
        "# to find Universe IDs for games you want to allow.": ""
    },
    "log_file": str(LOG_FILE)
}

# ── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(log_path: str) -> logging.Logger:
    logger = logging.getLogger("guardian")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    # File
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

# ── Roblox API helpers ───────────────────────────────────────────────────────

API_PRESENCE = "https://presence.roblox.com/v1/presence/users"
API_GAME_SEARCH = "https://games.roblox.com/v1/games/list"
API_UNIVERSE_DETAILS = "https://games.roblox.com/v1/games"
API_SEARCH_GAMES = "https://apis.roblox.com/search-api/omni-search"


def api_request(url: str, cookie: str, method: str = "GET",
                data: dict = None) -> dict:
    """Make an authenticated request to the Roblox API."""
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers,
                  method="POST" if data else method)
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Roblox API {e.code}: {error_body}") from e
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def get_presence(user_id: int, cookie: str) -> dict:
    """Get the current presence/game status for a user."""
    result = api_request(API_PRESENCE, cookie, data={"userIds": [user_id]})
    presences = result.get("userPresences", [])
    if not presences:
        raise RuntimeError(f"No presence data for user {user_id}")
    return presences[0]


def get_universe_details(universe_ids: list[int], cookie: str) -> dict:
    """Get details (name, etc.) for universe IDs."""
    ids_param = ",".join(str(uid) for uid in universe_ids)
    url = f"{API_UNIVERSE_DETAILS}?universeIds={ids_param}"
    return api_request(url, cookie)


def search_games(keyword: str, cookie: str) -> list[dict]:
    """Search for Roblox games by keyword. Returns basic results."""
    url = (f"https://games.roblox.com/v1/games/list?"
           f"model.keyword={quote(keyword)}"
           f"&model.startRows=0&model.maxRows=10")
    try:
        result = api_request(url, cookie)
        return result.get("games", [])
    except Exception:
        # Fallback: use the catalog search
        url2 = (f"https://www.roblox.com/games/list-json?"
                f"keyword={quote(keyword)}&startRows=0&maxRows=10")
        try:
            result = api_request(url2, cookie)
            return result.get("games", [])
        except Exception:
            return []


def search_games_simple(keyword: str) -> list[dict]:
    """
    Search for Roblox games using the public search endpoint.
    No authentication required.
    """
    url = (f"https://games.roblox.com/v1/games/list?"
           f"model.keyword={quote(keyword)}"
           f"&model.startRows=0&model.maxRows=10")
    headers = {"Accept": "application/json"}
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("games", [])
    except Exception:
        pass

    # Alternate: try the catalog search API
    url2 = (f"https://catalog.roblox.com/v1/search/items?"
            f"category=9&keyword={quote(keyword)}&limit=10")
    req2 = Request(url2, headers=headers)
    try:
        with urlopen(req2, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("data", [])
    except Exception:
        return []


# ── Process management ───────────────────────────────────────────────────────

def kill_roblox() -> list[str]:
    """Kill all Roblox-related processes. Returns list of killed process names."""
    system = platform.system()
    killed = []

    if system == "Windows":
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

    elif system == "Darwin":  # macOS
        targets = ["Roblox", "RobloxPlayer"]
        for proc in targets:
            try:
                result = subprocess.run(
                    ["pkill", "-f", proc],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    killed.append(proc)
            except Exception:
                pass

    else:  # Linux
        targets = ["Roblox", "roblox"]
        for proc in targets:
            try:
                result = subprocess.run(
                    ["pkill", "-f", proc],
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
            # PowerShell toast notification
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
        pass  # Notifications are best-effort


# ── Core monitor loop ────────────────────────────────────────────────────────

class WhitelistGuardian:
    def __init__(self, config: dict, dry_run: bool = False):
        self.user_id = config["roblox_user_id"]
        self.cookie = config["roblosecurity_cookie"]
        self.poll_interval = config.get("poll_interval_seconds", 5)
        self.auto_kill = config.get("auto_kill", True)
        self.notify = config.get("notify_parent", True)
        self.dry_run = dry_run

        # Build whitelist: {universe_id: friendly_name}
        raw = config.get("whitelisted_universes", {})
        self.whitelist: dict[int, str] = {}
        for key, val in raw.items():
            try:
                uid = int(key)
                self.whitelist[uid] = str(val) if val else f"Universe {uid}"
            except (ValueError, TypeError):
                pass  # skip comment entries

        self.logger = setup_logging(config.get("log_file", str(LOG_FILE)))
        self.running = True
        self._last_universe = None

    def stop(self, *_):
        self.running = False
        self.logger.info("Shutting down guardian.")

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        mode = "DRY-RUN" if self.dry_run else "ENFORCING"
        self.logger.info(f"Guardian started [{mode}] — monitoring user {self.user_id}")
        self.logger.info(f"Whitelisted universes: {len(self.whitelist)}")
        for uid, name in self.whitelist.items():
            self.logger.info(f"  ✓ {uid} — {name}")

        if not self.whitelist:
            self.logger.warning("⚠  Whitelist is EMPTY — ALL games will be blocked!")

        consecutive_errors = 0

        while self.running:
            try:
                presence = get_presence(self.user_id, self.cookie)
                consecutive_errors = 0

                presence_type = presence.get("userPresenceType", 0)
                universe_id = presence.get("universeId")
                place_id = presence.get("placeId")
                last_location = presence.get("lastLocation", "")

                # presenceType: 0=offline, 1=online(website), 2=in-game, 3=in-studio
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
                            self.logger.warning(
                                f"🚫 BLOCKED — Joined non-whitelisted game "
                                f"(universe={universe_id}, place={place_id}, "
                                f"location=\"{last_location}\")"
                            )
                            if self.notify:
                                send_notification(
                                    "Roblox Guardian",
                                    f"Blocked non-whitelisted game "
                                    f"(Universe {universe_id})"
                                )
                            if self.auto_kill and not self.dry_run:
                                killed = kill_roblox()
                                if killed:
                                    self.logger.info(
                                        f"   Killed processes: {', '.join(killed)}"
                                    )
                                else:
                                    self.logger.warning(
                                        "   Could not find Roblox process to kill"
                                    )

                elif presence_type != 2:
                    if self._last_universe is not None:
                        self.logger.info("Player left game / went offline.")
                        self._last_universe = None

            except RuntimeError as e:
                consecutive_errors += 1
                self.logger.error(f"API error: {e}")
                if consecutive_errors >= 5:
                    self.logger.error(
                        "5 consecutive errors — check your cookie and network."
                    )
                    # Back off
                    time.sleep(min(self.poll_interval * consecutive_errors, 60))

            except Exception as e:
                consecutive_errors += 1
                self.logger.error(f"Unexpected error: {e}")

            time.sleep(self.poll_interval)

        self.logger.info("Guardian stopped.")


# ── Game lookup helper ───────────────────────────────────────────────────────

def lookup_game(keyword: str, cookie: str = ""):
    """Search for a Roblox game and print its Universe ID."""
    print(f"\nSearching for: \"{keyword}\"\n")

    # Try with the universe search API (public, no auth needed)
    url = (f"https://games.roblox.com/v1/games/list?"
           f"model.keyword={quote(keyword)}"
           f"&model.startRows=0&model.maxRows=10")
    headers = {
        "Accept": "application/json",
    }
    if cookie and cookie != "PASTE_YOUR_COOKIE_HERE":
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"

    req = Request(url, headers=headers)
    games = []
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            games = data.get("games", [])
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Monitor and log but don't kill processes")
    parser.add_argument("--lookup", type=str, metavar="GAME_NAME",
                        help="Search for a game's Universe ID")
    parser.add_argument("--place-to-universe", type=int, metavar="PLACE_ID",
                        help="Convert a Place ID to Universe ID")
    parser.add_argument("--init", action="store_true",
                        help="Create a default config file")
    args = parser.parse_args()

    # ── Lookup mode
    if args.lookup:
        cookie = ""
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
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
        if CONFIG_FILE.exists():
            print(f"Config already exists: {CONFIG_FILE}")
            return
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Created config: {CONFIG_FILE}")
        print("Edit it with your child's User ID, cookie, and whitelisted games.")
        return

    # ── Monitor mode
    if not CONFIG_FILE.exists():
        print(f"No config file found at {CONFIG_FILE}")
        print(f"Run:  python {sys.argv[0]} --init")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    if config.get("roblox_user_id", 0) == 0:
        print("ERROR: Set your child's roblox_user_id in the config file.")
        sys.exit(1)

    if config.get("roblosecurity_cookie") == "PASTE_YOUR_COOKIE_HERE":
        print("ERROR: Set the .ROBLOSECURITY cookie in the config file.")
        print("  1. Log into your child's Roblox account in a browser")
        print("  2. Open DevTools (F12) → Application → Cookies → .ROBLOSECURITY")
        print("  3. Copy the value into the config file")
        sys.exit(1)

    guardian = WhitelistGuardian(config, dry_run=args.dry_run)
    guardian.run()


if __name__ == "__main__":
    main()
