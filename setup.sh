#!/usr/bin/env bash
# SmartPark installer for Raspberry Pi OS (Bookworm / Trixie).
#   ./setup.sh gateway     on the Pi with the Touch Display 2
#   ./setup.sh bay         on each bay Pi
# Safe to re-run. It does NOT create keys or the vault - see README "First-time setup".
set -euo pipefail

ROLE="${1:-}"
if [[ "$ROLE" != "gateway" && "$ROLE" != "bay" ]]; then
  echo "usage: $0 gateway|bay"; exit 1
fi
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
PY="$DIR/.venv/bin/python"

echo "==> Installing system packages (needs sudo)"
PKGS="python3-venv python3-cryptography python3-requests"
if [[ "$ROLE" == "bay" ]]; then PKGS="$PKGS python3-sklearn python3-joblib python3-numpy"; fi
if [[ "$ROLE" == "gateway" ]]; then PKGS="$PKGS curl"; fi
sudo apt-get update -qq
# shellcheck disable=SC2086
sudo apt-get install -y $PKGS
# GPIO library: RPi.GPIO (Pi 4) or its lgpio-based drop-in replacement on newer images
sudo apt-get install -y python3-rpi.gpio || sudo apt-get install -y python3-rpi-lgpio
if [[ "$ROLE" == "gateway" ]] && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser
fi

echo "==> Creating virtual environment (re-uses the apt packages)"
python3 -m venv --system-site-packages "$DIR/.venv"
"$PY" -c "import cryptography, requests" 2>/dev/null || "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

CFG="$DIR/config/$ROLE.toml"
if [[ ! -f "$CFG" ]]; then
  cp "$DIR/config/$ROLE.example.toml" "$CFG"
  echo "==> Created $CFG - EDIT IT (device_id, addresses) before starting."
fi

echo "==> Installing systemd service smartpark-$ROLE"
sudo tee "/etc/systemd/system/smartpark-$ROLE.service" >/dev/null <<EOF
[Unit]
Description=SmartPark $ROLE
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$PY -m smartpark $ROLE --config $CFG
Restart=always
RestartSec=5
# hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "smartpark-$ROLE" >/dev/null

if [[ "$ROLE" == "gateway" ]]; then
  echo "==> Kiosk browser autostart on the touch display"
  chmod +x "$DIR/scripts/kiosk-browser.sh"
  mkdir -p "$HOME/.config/labwc" "$HOME/.config/autostart"
  # labwc (Wayland, current Raspberry Pi OS)
  grep -q kiosk-browser.sh "$HOME/.config/labwc/autostart" 2>/dev/null || \
    echo "$DIR/scripts/kiosk-browser.sh &" >> "$HOME/.config/labwc/autostart"
  # keyboard = "system": labwc hides on-screen keyboards behind fullscreen windows, so the kiosk then runs as a
  # maximised app window - this rule strips its title bar
  RC="$HOME/.config/labwc/rc.xml"
  [[ -f "$RC" ]] || printf '<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n</openbox_config>\n' > "$RC"
  grep -q 'title="SmartPark Kiosk"' "$RC" || sed -i 's#</openbox_config>#\t<windowRules>\n\t\t<windowRule title="SmartPark Kiosk" serverDecoration="no">\n\t\t\t<action name="Maximize"/>\n\t\t</windowRule>\n\t</windowRules>\n</openbox_config>#' "$RC"
  # auto-hide the Raspberry Pi taskbar so the kiosk window gets the whole screen (touch the top edge to show it)
  INI="$HOME/.config/wf-panel-pi/wf-panel-pi.ini"
  mkdir -p "$(dirname "$INI")"; touch "$INI"
  if ! grep -q '^autohide=' "$INI"; then
    if grep -q '^\[panel\]' "$INI"; then sed -i '/^\[panel\]/a autohide=true\nautohide_duration=300' "$INI"
    else printf '[panel]\nautohide=true\nautohide_duration=300\n' >> "$INI"; fi
  fi
  # X11 / older desktops
  cat > "$HOME/.config/autostart/smartpark-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SmartPark kiosk
Exec=$DIR/scripts/kiosk-browser.sh
EOF
fi

cat <<EOF

Done. Next steps (see README):
  1. edit $CFG
  2. first-time keys:  $PY -m smartpark init-$ROLE --config $CFG
  3. start:            sudo systemctl start smartpark-$ROLE
     logs:             journalctl -u smartpark-$ROLE -f
EOF
