#!/data/data/com.termux/files/usr/bin/bash
# Starts Driverline AND a free public tunnel, then prints the link to share.
# Run from Termux:  cd ~/driverline && bash start_public.sh
cd "$(dirname "$0")" || exit 1
termux-wake-lock 2>/dev/null

: > tunnel.log
cloudflared tunnel --url http://localhost:8000 > tunnel.log 2>&1 &
TUN=$!
trap 'kill $TUN 2>/dev/null' EXIT

URL=""
for i in $(seq 1 40); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "The tunnel did not start. Last lines of tunnel.log:"
  tail -n 15 tunnel.log
  exit 1
fi

echo "=============================================="
echo " Share this link: $URL"
echo " (it changes every time you run this script)"
echo "=============================================="

export DRIVERLINE_BASE_URL="$URL"
export DRIVERLINE_SECURE=1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
