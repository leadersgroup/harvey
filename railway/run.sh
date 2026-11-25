#!/usr/bin/env bash
# Runs as the harvey user: proxy, import endpoint, dashboard, optional loop.
set -uo pipefail

cd "$HARVEY_HOME"
export PYTHONPATH="$HARVEY_HOME"
export PATH="/home/harvey/.local/bin:$PATH"
export PORT="${PORT:-8080}"
RW=/opt/harvey/railway

if [ -n "${PUBLIC_ORIGIN:-}" ]; then
  :
elif [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
  PUBLIC_ORIGIN="https://$RAILWAY_PUBLIC_DOMAIN"
else
  PUBLIC_ORIGIN="https://no-public-domain.invalid"
  echo "[railway] WARNING: no public domain yet. Generate one in Railway, then redeploy."
fi
export PUBLIC_ORIGIN

# An API key would silently take priority over the subscription token and
# bill per token, so it is never passed on.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN

python "$RW/set_heartbeat.py"

if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  if out="$(cd /tmp && timeout 90 claude -p "Reply with the single word OK" 2>&1)"; then
    echo "[railway] Claude subscription sign-in works: $(printf '%s' "$out" | head -c 80)"
  else
    echo "[railway] WARNING: Claude call failed with the subscription token: $(printf '%s' "$out" | head -c 300)"
  fi
else
  echo "[railway] WARNING: CLAUDE_CODE_OAUTH_TOKEN is not set. Harvey cannot think until it is."
  echo "[railway]   On your own computer run: claude setup-token   then add the token as that variable in Railway."
fi

pids=()
caddy run --config "$RW/Caddyfile" --adapter caddyfile &
pids+=($!)
python "$RW/import_server.py" &
pids+=($!)

# The dashboard prints its unlock link for 127.0.0.1; show the public one.
python -m harvey dashboard 2>&1 \
  | sed -u "s#http://127\.0\.0\.1:5555/#$PUBLIC_ORIGIN/#" &
pids+=($!)

autorun() {
  local tries=0 code
  while [ "$tries" -lt 20 ]; do
    if python -c "import sys; from harvey.main import _needs_setup; sys.exit(1 if _needs_setup() else 0)"; then
      echo "[railway] starting the heartbeat loop"
      python -m harvey run >> data/harvey.log 2>&1
      code=$?
      echo "[railway] heartbeat loop exited with code $code"
      # A stop from the dashboard is a normal exit; leave it stopped.
      case "$code" in 0|130|143) return ;; esac
      tries=$((tries + 1))
    else
      echo "[railway] Harvey is not set up yet (no .env/config). Import your data first."
      return
    fi
    sleep 60
  done
}
if [ "${HARVEY_AUTORUN:-0}" = "1" ]; then
  autorun &
fi

# Mirror the loop's log into Railway's log stream.
touch data/harvey.log
tail -n 0 -F data/harvey.log &

trap 'kill "${pids[@]}" 2>/dev/null; exit 1' TERM INT
wait -n "${pids[@]}"
echo "[railway] a core process exited; restarting the container"
kill "${pids[@]}" 2>/dev/null
exit 1
