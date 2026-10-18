#!/usr/bin/env bash
# Run ON THE GATEWAY after `init-gateway`. Copies the gateway public key to every bay,
# initialises each bay's identity, and trusts each bay's public key on the gateway.
# Needs SSH access to the bays (LAN or Tailscale names), e.g.:
#   ./scripts/exchange_keys.sh pi@bay1 pi@bay2 pi@bay3 pi@bay4
# Assumes the project is at ~/smart_parking on every Pi and each bay has config/bay.toml.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
GW_PUB="$HOME/smartpark_data/keys/gateway_public.pem"
[[ -f "$GW_PUB" ]] || { echo "Run init-gateway first ($GW_PUB missing)"; exit 1; }

for HOST in "$@"; do
  echo "==> $HOST"
  scp -q "$GW_PUB" "$HOST:/tmp/gateway_public.pem"
  ID=$(ssh "$HOST" "cd ~/smart_parking && .venv/bin/python -c \"from smartpark.config import load_config; print(load_config('config/bay.toml')['device_id'])\"")
  ssh "$HOST" "cd ~/smart_parking && .venv/bin/python -m smartpark init-bay --config config/bay.toml --gateway-key /tmp/gateway_public.pem && rm /tmp/gateway_public.pem"
  scp -q "$HOST:~/smartpark_data/keys/${ID}_public.pem" "/tmp/${ID}_public.pem"
  "$PY" -m smartpark trust --config "$DIR/config/gateway.toml" --peer "$ID" --key "/tmp/${ID}_public.pem"
  rm "/tmp/${ID}_public.pem"
done
echo "All keys exchanged. Restart the services: sudo systemctl restart smartpark-gateway (and smartpark-bay on each bay)"
