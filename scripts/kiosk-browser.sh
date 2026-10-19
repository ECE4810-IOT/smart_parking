#!/usr/bin/env bash
# Opens the SmartPark kiosk full-screen on the Touch Display 2 once the gateway is up.
# only one copy: both the labwc autostart and the XDG .desktop entry start this script
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/smartpark-kiosk.lock"
flock -n 9 || exit 0
URL="http://127.0.0.1:8080"
until curl -s -o /dev/null "$URL"; do sleep 1; done
BROWSER="$(command -v chromium || command -v chromium-browser)"
CFG="$(dirname "$0")/../config/gateway.toml"
MODE="--kiosk $URL"
# keyboard = "system" -> let Chromium talk to the Wayland on-screen keyboard (squeekboard).
# labwc draws on-screen keyboards *under* fullscreen windows, so use a maximised app window instead of
# --kiosk; the labwc windowRule for title "SmartPark Kiosk" (see README) removes its title bar.
if grep -Eq '^\s*keyboard\s*=\s*"system"' "$CFG" 2>/dev/null; then
  MODE="--enable-wayland-ime --wayland-text-input-version=3 --start-maximized --app=$URL"
fi
# shellcheck disable=SC2086
exec "$BROWSER" --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-pinch --overscroll-history-navigation=0 --incognito --password-store=basic --disable-features=PasswordManagerOnboarding,AutofillServerCommunication \
  --check-for-update-interval=31536000 $MODE
