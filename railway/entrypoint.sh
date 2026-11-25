#!/usr/bin/env bash
# Runs as root: prepare the volume, then hand over to the harvey user.
set -euo pipefail

VOL="${HARVEY_VOLUME:-/data}"
HARVEY_HOME="$VOL/app"
SRC=/opt/harvey

# Upstream renamed the package from harvey to sellbird; work with either.
if [ -d "$SRC/sellbird" ]; then PKG=sellbird; else PKG=harvey; fi

mkdir -p "$HARVEY_HOME/data/backups"

# Before new code touches the database, keep a copy of it. Nothing is running
# yet, so a plain file copy is consistent. The newest three are kept.
STAMP="$HARVEY_HOME/data/.deployed-commit"
NEW_COMMIT="${RAILWAY_GIT_COMMIT_SHA:-unknown}"
OLD_COMMIT="$(cat "$STAMP" 2>/dev/null || true)"
if [ -n "${RESTORE_BACKUP:-}" ]; then
  backup="$HARVEY_HOME/data/backups/$(basename "$RESTORE_BACKUP")"
  if [ -f "$backup" ]; then
    target="$HARVEY_HOME/data/$(basename "$RESTORE_BACKUP" | sed -E 's/^([a-z]+\.db)\..*$/\1/')"
    rm -f "$target-wal" "$target-shm"
    cp "$backup" "$target"
    echo "[railway] restored $(basename "$backup") to $(basename "$target"). Remove RESTORE_BACKUP now."
  else
    echo "[railway] RESTORE_BACKUP: no such backup. Available:"; ls -1 "$HARVEY_HOME/data/backups" || true
  fi
elif [ "$NEW_COMMIT" != "$OLD_COMMIT" ]; then
  for db in "$HARVEY_HOME"/data/harvey.db "$HARVEY_HOME"/data/sellbird.db; do
    [ -f "$db" ] || continue
    name="$(basename "$db").$(date -u +%Y%m%dT%H%M%SZ).before-${NEW_COMMIT:0:7}"
    # Fold any write-ahead log into the copy so it is a complete database.
    python3 - "$db" "$HARVEY_HOME/data/backups/$name" <<'PY' || cp "$db" "$HARVEY_HOME/data/backups/$name"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
src.close(); dst.close()
PY
    echo "[railway] code changed (${OLD_COMMIT:0:7} -> ${NEW_COMMIT:0:7}); database backed up as $name"
    ls -1t "$HARVEY_HOME"/data/backups/"$(basename "$db")".* 2>/dev/null | tail -n +4 | xargs -r rm -f
  done
fi
echo "$NEW_COMMIT" > "$STAMP"

# Harvey keeps config, knowledge and state next to its package, so the code
# lives on the volume too. Code is replaced from the image on every boot;
# everything else on the volume is left alone.
for pkg in harvey sellbird; do
  [ -d "$SRC/$pkg" ] || continue
  rm -rf "$HARVEY_HOME/$pkg.new"
  cp -a "$SRC/$pkg" "$HARVEY_HOME/$pkg.new"
  rm -rf "$HARVEY_HOME/$pkg"
  mv "$HARVEY_HOME/$pkg.new" "$HARVEY_HOME/$pkg"
done

# Defaults are copied only where nothing exists yet, so trained prompts,
# skills and config survive redeploys.
for cfg in harvey.yaml sellbird.yaml; do
  [ -f "$SRC/$cfg" ] && [ ! -e "$HARVEY_HOME/$cfg" ] && cp "$SRC/$cfg" "$HARVEY_HOME/$cfg"
done
for dir in prompts skills; do
  mkdir -p "$HARVEY_HOME/$dir"
  cp -r --update=none "$SRC/$dir/." "$HARVEY_HOME/$dir/"
done

# The dashboard's setup checklist only looks for a .venv folder. The image
# already has every dependency installed, so mark that step as done.
mkdir -p "$HARVEY_HOME/.venv"
[ -e "$HARVEY_HOME/.venv/README" ] || echo "Placeholder: dependencies are installed in the Railway image." > "$HARVEY_HOME/.venv/README"

# Keep the loop's log from filling the volume.
for LOG in "$HARVEY_HOME"/data/harvey.log "$HARVEY_HOME"/data/sellbird.log; do
  if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 52428800 ]; then
    tail -c 5242880 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
done

chown -R harvey:harvey "$VOL"

# SELLBIRD_HOME is how the renamed package finds its folder; the old one
# derives it from where the package sits, which is the same place.
export HARVEY_HOME HARVEY_PKG="$PKG" SELLBIRD_HOME="$HARVEY_HOME" HOME=/home/harvey
echo "[railway] package: $PKG, commit: ${NEW_COMMIT:0:7}"
exec setpriv --reuid=harvey --regid=harvey --init-groups "$SRC/railway/run.sh"
