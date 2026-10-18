#!/usr/bin/env bash
# Opens the SmartPark kiosk full-screen on the Touch Display 2 once the gateway is up.
URL="http://127.0.0.1:8080"
until curl -s -o /dev/null "$URL"; do sleep 1; done
BROWSER="$(command -v chromium || command -v chromium-browser)"
CFG="$(dirname "$0")/../config/gateway.toml"
EXTRA=""
# keyboard = "system" -> let Chromium talk to the Wayland on-screen keyboard (wvkbd / squeekboard)
if grep -Eq '^\s*keyboard\s*=\s*"system"' "$CFG" 2>/dev/null; then
  EXTRA="--enable-wayland-ime --wayland-text-input-version=3"
fi
# shellcheck disable=SC2086
exec "$BROWSER" $EXTRA --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-pinch --overscroll-history-navigation=0 --incognito --check-for-update-interval=31536000 "$URL"
