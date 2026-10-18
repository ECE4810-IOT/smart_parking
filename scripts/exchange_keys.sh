#!/usr/bin/env bash
# Run ON THE GATEWAY after `init-gateway`. Copies the gateway public key to every bay,
# initialises each bay's identity, and trusts each bay's public key on the gateway.
# Arguments are SSH logins for the bay Pis (their real hostnames / Tailscale names / IPs,
# NOT the bay1..bay4 device ids), e.g.:
#   bash scripts/exchange_keys.sh user@leanjieberrypi user@chloeberrypi
# Tip: run `ssh-copy-id user@host` first so you are not asked for the password 4 times per bay.
# Assumes the project is at ~/smart_parking on every Pi and each bay has config/bay.toml.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
GW_PUB="$HOME/smartpark_data/keys/gateway_public.pem"
[[ -f "$GW_PUB" ]] || { echo "Run init-gateway first ($GW_PUB missing)"; exit 1; }

[[ $# -gt 0 ]] || { echo "usage: $0 user@bay-hostname [...]"; exit 1; }

for HOST in "$@"; do
  echo "==> $HOST"
  if ! ssh -o ConnectTimeout=10 "$HOST" true; then
    echo "Cannot SSH to $HOST - check the hostname (use the Pi's hostname, not bay1) and that you can 'ssh $HOST'"
    exit 1
  fi
  if ! ssh "$HOST" "test -f ~/smart_parking/config/bay.toml && test -x ~/smart_parking/.venv/bin/python"; then
    echo "$HOST: ~/smart_parking is not set up - run 'bash setup.sh bay' and edit config/bay.toml there first"
    exit 1
  fi
  scp "$GW_PUB" "$HOST:/tmp/gateway_public.pem"
  ID=$(ssh "$HOST" "cd ~/smart_parking && .venv/bin/python -c \"from smartpark.config import load_config; print(load_config('config/bay.toml')['device_id'])\"")
  ssh "$HOST" "cd ~/smart_parking && .venv/bin/python -m smartpark init-bay --config config/bay.toml --gateway-key /tmp/gateway_public.pem && rm /tmp/gateway_public.pem"
  scp "$HOST:~/smartpark_data/keys/${ID}_public.pem" "/tmp/${ID}_public.pem"
  echo "    $HOST is $ID"
  "$PY" -m smartpark trust --config "$DIR/config/gateway.toml" --peer "$ID" --key "/tmp/${ID}_public.pem"
  rm "/tmp/${ID}_public.pem"
done
echo "All keys exchanged. Restart the services: sudo systemctl restart smartpark-gateway (and smartpark-bay on each bay)"
