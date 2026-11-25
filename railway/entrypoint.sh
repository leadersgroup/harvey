#!/usr/bin/env bash
# Runs as root: prepare the volume, then hand over to the harvey user.
set -euo pipefail

VOL="${HARVEY_VOLUME:-/data}"
HARVEY_HOME="$VOL/app"
SRC=/opt/harvey

mkdir -p "$HARVEY_HOME/data"

# Harvey keeps config, knowledge and state next to its package, so the package
# itself lives on the volume. Code is replaced from the image on every boot;
# everything else on the volume is left alone.
rm -rf "$HARVEY_HOME/harvey.new"
cp -a "$SRC/harvey" "$HARVEY_HOME/harvey.new"
rm -rf "$HARVEY_HOME/harvey"
mv "$HARVEY_HOME/harvey.new" "$HARVEY_HOME/harvey"

# Defaults are copied only where nothing exists yet, so trained prompts,
# skills and config survive redeploys.
[ -e "$HARVEY_HOME/harvey.yaml" ] || cp "$SRC/harvey.yaml" "$HARVEY_HOME/harvey.yaml"
for dir in prompts skills; do
  mkdir -p "$HARVEY_HOME/$dir"
  cp -rn "$SRC/$dir/." "$HARVEY_HOME/$dir/"
done

# Keep the loop's log from filling the volume.
LOG="$HARVEY_HOME/data/harvey.log"
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 52428800 ]; then
  tail -c 5242880 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

chown -R harvey:harvey "$VOL"

export HARVEY_HOME HOME=/home/harvey
exec setpriv --reuid=harvey --regid=harvey --init-groups "$SRC/railway/run.sh"
