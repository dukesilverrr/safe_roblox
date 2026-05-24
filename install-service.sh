#!/usr/bin/env bash
#
# install-service.sh — install the Roblox Whitelist Guardian as a
# systemd template service (one instance per kid config).
#
# Run as root (sudo). The service itself runs as your normal user
# account so it can read/write the configs and log files in place.
#
# Usage:
#   sudo ./install-service.sh install
#   sudo ./install-service.sh install --token '12345:ABC-DEF...'
#   sudo ./install-service.sh status
#   sudo ./install-service.sh uninstall
#

set -euo pipefail

# ── Locations ────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARDIAN_PY="$SCRIPT_DIR/roblox_whitelist_guardian.py"
SERVICE_NAME="roblox-guardian"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}@.service"
ENV_FILE="$SCRIPT_DIR/.env"

# When invoked under sudo, default to running the daemon as the human
# user who invoked sudo, not as root. Keeps configs/logs ownership sane
# and matches the "configs live in someone's home dir" reality.
DEFAULT_RUN_USER="${SUDO_USER:-$USER}"
RUN_AS_USER="$DEFAULT_RUN_USER"
TOKEN=""
SKIP_ENABLE=false
ACTION=""

# ── Output helpers ───────────────────────────────────────────────────────────

err()  { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf ' · %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m  %s\n' "$*"; }

usage() {
  cat <<EOF
Usage: sudo $0 <command> [options]

Installs the Roblox Whitelist Guardian as a systemd template service.
One instance per kid config in this directory is enabled and started
(roblox-guardian@<kidname>).

Commands:
  install      Write systemd unit, then enable + start every detected
               kid config (any *.json in this dir that has roblox_user_id
               set, excluding whitelist_universes.json).
  uninstall    Stop and disable every instance, remove the unit file.
               Leaves your configs and the .env file alone.
  status       Show systemctl status for every enabled instance.

Options:
  --user USER         Run service as this user (default: \$SUDO_USER or you).
  --token TOKEN       Telegram bot token to store in $ENV_FILE
                      (chmod 600). Skip if you already have a .env or
                      prefer to set TELEGRAM_BOT_TOKEN elsewhere.
  --skip-enable       Install the unit only; don't enable/start instances.
  -h, --help          Show this help.

Examples:
  sudo $0 install
  sudo $0 install --token '12345:ABC-DEF...'
  sudo $0 status
  sudo $0 uninstall
EOF
}

# ── Argument parsing ─────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|uninstall|status)
      ACTION="$1"; shift ;;
    --user)
      RUN_AS_USER="${2:-}"; [[ -n "$RUN_AS_USER" ]] || err "--user needs a value"; shift 2 ;;
    --token)
      TOKEN="${2:-}"; [[ -n "$TOKEN" ]] || err "--token needs a value"; shift 2 ;;
    --skip-enable)
      SKIP_ENABLE=true; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      err "Unknown argument: $1 (see --help)" ;;
  esac
done

[[ -n "$ACTION" ]] || { usage; exit 1; }

# ── Sanity checks ────────────────────────────────────────────────────────────

[[ "$(uname -s)" == "Linux" ]] \
  || err "This installer is Linux-only (uses systemctl)."
command -v systemctl >/dev/null \
  || err "systemctl not found; this script requires systemd."
[[ $EUID -eq 0 ]] \
  || err "Re-run with sudo (need root to write $UNIT_FILE)."
[[ -f "$GUARDIAN_PY" ]] \
  || err "Guardian script not found at $GUARDIAN_PY"
id "$RUN_AS_USER" >/dev/null 2>&1 \
  || err "User '$RUN_AS_USER' doesn't exist on this system."

PYTHON3="$(command -v python3)" \
  || err "python3 not found in PATH."

# Light validation of the token shape. Telegram tokens look like:
# "<digits>:<35-or-so-alnum-with-dashes-and-underscores>"
if [[ -n "$TOKEN" ]] && ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  warn "Token doesn't match the usual Telegram format '<digits>:<chars>'."
  warn "Continuing anyway, but double-check it."
fi

# ── Helpers ──────────────────────────────────────────────────────────────────

# Scan SCRIPT_DIR for valid kid configs. Echoes one basename per line.
# A "kid config" is any *.json whose top level has a non-zero
# integer roblox_user_id. whitelist_universes.json is skipped explicitly.
detect_kids() {
  local f name uid
  shopt -s nullglob
  for f in "$SCRIPT_DIR"/*.json; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f" .json)"
    [[ "$name" == "whitelist_universes" ]] && continue
    if "$PYTHON3" - "$f" <<'PY' 2>/dev/null; then
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
    uid = cfg.get("roblox_user_id", 0)
    sys.exit(0 if isinstance(uid, int) and uid > 0 else 1)
except Exception:
    sys.exit(1)
PY
      printf '%s\n' "$name"
    fi
  done
}

write_unit() {
  note "Writing systemd unit → $UNIT_FILE"
  cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Roblox Whitelist Guardian for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_AS_USER
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=$PYTHON3 $GUARDIAN_PY --config $SCRIPT_DIR/%i.json
Restart=always
RestartSec=10

# Conservative hardening — restrict the daemon's filesystem access
# without breaking its need to read configs and write logs in
# SCRIPT_DIR.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
  chmod 644 "$UNIT_FILE"
}

write_env_file() {
  local token="$1"
  note "Writing env file → $ENV_FILE (chmod 600)"
  cat > "$ENV_FILE" <<ENV
# Roblox Guardian environment variables — sourced by systemd.
# DO NOT commit this file. The token grants impersonation of the bot.
TELEGRAM_BOT_TOKEN=$token
ENV
  chown "$RUN_AS_USER:$RUN_AS_USER" "$ENV_FILE" 2>/dev/null \
    || chown "$RUN_AS_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# Find every roblox-guardian@*.service unit known to systemd
# (enabled, running, or just present on disk).
list_existing_instances() {
  systemctl list-units --type=service --all --no-legend --plain \
      --state=loaded 2>/dev/null \
    | awk -v p="^${SERVICE_NAME}@" '$1 ~ p {print $1}'
  systemctl list-unit-files --type=service --no-legend \
    | awk -v p="^${SERVICE_NAME}@" '$1 ~ p && $1 != "'"${SERVICE_NAME}"'@.service" {print $1}'
}

# ── Actions ──────────────────────────────────────────────────────────────────

action_install() {
  write_unit

  if [[ -n "$TOKEN" ]]; then
    write_env_file "$TOKEN"
  elif [[ -f "$ENV_FILE" ]]; then
    note "Existing env file at $ENV_FILE — leaving it alone."
  else
    echo
    warn "No Telegram bot token provided and no $ENV_FILE found."
    echo "   The daemon will start but won't send Telegram messages."
    echo "   Either re-run with --token '...', or create $ENV_FILE with:"
    echo "       TELEGRAM_BOT_TOKEN=12345:ABC-DEF..."
    echo
  fi

  systemctl daemon-reload
  ok "Unit installed; systemd reloaded."

  if [[ "$SKIP_ENABLE" == "true" ]]; then
    note "--skip-enable set; not touching instances."
    return 0
  fi

  mapfile -t kids < <(detect_kids)
  if [[ ${#kids[@]} -eq 0 ]]; then
    echo
    warn "No kid configs detected in $SCRIPT_DIR."
    echo "  Create one first:"
    echo "    $PYTHON3 $GUARDIAN_PY --config <kidname>.json --init"
    echo "  Then enable + start manually:"
    echo "    sudo systemctl enable --now ${SERVICE_NAME}@<kidname>"
    return 0
  fi

  echo
  echo "Detected ${#kids[@]} kid config(s): ${kids[*]}"
  for k in "${kids[@]}"; do
    local unit="${SERVICE_NAME}@${k}.service"
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      note "$unit already enabled — restarting"
      systemctl restart "$unit"
    else
      note "Enabling + starting $unit"
      systemctl enable --now "$unit"
    fi
  done

  echo
  ok "Install complete. Status:"
  echo
  for k in "${kids[@]}"; do
    local unit="${SERVICE_NAME}@${k}.service"
    if systemctl is-active --quiet "$unit"; then
      printf '  \033[32m●\033[0m %s — active\n' "$unit"
    else
      printf '  \033[31m●\033[0m %s — INACTIVE (check journalctl)\n' "$unit"
    fi
  done
  echo
  echo "Tail logs with:"
  for k in "${kids[@]}"; do
    echo "  sudo journalctl -u ${SERVICE_NAME}@${k} -f"
  done
}

action_uninstall() {
  mapfile -t instances < <(list_existing_instances | sort -u)
  if [[ ${#instances[@]} -eq 0 ]]; then
    note "No ${SERVICE_NAME}@* instances found."
  else
    for inst in "${instances[@]}"; do
      note "Stopping + disabling $inst"
      systemctl disable --now "$inst" 2>/dev/null || true
    done
  fi

  if [[ -f "$UNIT_FILE" ]]; then
    note "Removing $UNIT_FILE"
    rm -f "$UNIT_FILE"
  else
    note "$UNIT_FILE not present."
  fi

  systemctl daemon-reload
  systemctl reset-failed 2>/dev/null || true
  ok "Uninstall complete."

  if [[ -f "$ENV_FILE" ]]; then
    echo
    note "$ENV_FILE was NOT removed (contains your bot token)."
    echo "  Delete it manually if you want:  rm $ENV_FILE"
  fi
}

action_status() {
  mapfile -t kids < <(detect_kids)
  if [[ ${#kids[@]} -eq 0 ]]; then
    echo "No kid configs detected in $SCRIPT_DIR."
    return 0
  fi
  for k in "${kids[@]}"; do
    local unit="${SERVICE_NAME}@${k}.service"
    echo "=== $unit ==="
    systemctl status "$unit" --no-pager -l || true
    echo
  done
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "$ACTION" in
  install)   action_install ;;
  uninstall) action_uninstall ;;
  status)    action_status ;;
  *)         usage; exit 1 ;;
esac
