#!/data/data/com.termux/files/usr/bin/bash
# Starts a free public tunnel AND Driverline, and prints the link only when it really works.
# Run from Termux:  cd ~/driverline && bash start_public.sh
cd "$(dirname "$0")" || exit 1
termux-wake-lock 2>/dev/null

# Stop leftovers from an earlier run so two copies don't fight each other
pkill -f "cloudflared tunnel" 2>/dev/null
pkill -f "uvicorn app.main" 2>/dev/null
sleep 1

: > tunnel.log
# http2 works on mobile networks that block the default UDP (QUIC) connection
cloudflared tunnel --no-autoupdate --protocol http2 --url http://localhost:8000 > tunnel.log 2>&1 &
TUN=$!
trap 'kill $TUN $SRV 2>/dev/null' EXIT INT TERM

echo "Starting the tunnel..."
URL=""
for i in $(seq 1 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
  if [ -n "$URL" ] && grep -q "Registered tunnel connection" tunnel.log; then break; fi
  sleep 1
done
if [ -z "$URL" ] || ! grep -q "Registered tunnel connection" tunnel.log; then
  echo "The tunnel did not connect to Cloudflare. Last lines of tunnel.log:"
  tail -n 15 tunnel.log
  echo "Try another network (Wi-Fi or mobile data) and run this script again."
  exit 1
fi

export DRIVERLINE_BASE_URL="$URL"
export DRIVERLINE_SECURE=1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
SRV=$!

echo "Checking the app is reachable..."
OK=""
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL/api/auth/me")
  if [ "$CODE" = "401" ] || [ "$CODE" = "200" ]; then OK=1; break; fi
  sleep 2
done

echo "=============================================="
if [ -n "$OK" ]; then
  echo " Share this link: $URL"
  echo " (it changes every time you run this script)"
else
  echo " The tunnel is up but the link did not answer yet."
  echo " Wait a minute and try: $URL"
fi
echo "=============================================="
wait $SRV
