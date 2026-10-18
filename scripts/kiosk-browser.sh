#!/usr/bin/env bash
# Opens the SmartPark kiosk full-screen on the Touch Display 2 once the gateway is up.
URL="http://127.0.0.1:8080"
until curl -s -o /dev/null "$URL"; do sleep 1; done
BROWSER="$(command -v chromium || command -v chromium-browser)"
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-pinch --overscroll-history-navigation=0 --incognito --check-for-update-interval=31536000 "$URL"
