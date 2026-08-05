#!/bin/bash
# Install the launchd agents. Run this on the mini and nowhere else.
#
# The guard below is not ceremony. CLAUDE.md's deployment rule is one writer:
# the mini's process is the only thing that opens the DB file. Two machines
# running these agents means two ingest runs hammering Indeed from different
# IPs, two scoring workers spending money on the same rows, and two processes
# writing one SQLite file — which is the corruption case WAL does not save you
# from when the file is reached over a share.
#
#   ./deploy/install.sh                 interactive, asks before doing anything
#   ./deploy/install.sh --yes           for when you have read this once already
#   ./deploy/install.sh --init-volume   stamp the external disk, once, before first use
#   ./deploy/install.sh --uninstall

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="${JOBHUNT_LOG_DIR:-$HOME/Library/Logs/jobhunt}"
PLISTS=(com.jobhunt.discover com.jobhunt.dashboard com.jobhunt.backup)

# A missing venv means the agents would fail silently twice a day forever.
[ -x "$PY" ] || { echo "  no venv at $REPO/.venv — run 'make venv' first"; exit 1; }

# Ask the app where the database is, rather than guessing from this shell's
# environment. The old version read ${JOBHUNT_DB:-$REPO/jobhunt.db} and baked the
# answer into the plists — and because config._load_env uses setdefault, that
# baked value then beat .env at runtime. Editing .env silently did nothing to the
# agents while `make` picked up the change: two databases, no warning. Now the
# plists carry no DB path at all and this is only ever used for display.
DB="$("$PY" -c 'import jobhunt.db as d; print(d.resolve())')"

uninstall() {
  for label in "${PLISTS[@]}"; do
    if [ -f "$AGENTS/$label.plist" ]; then
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      rm -f "$AGENTS/$label.plist"
      echo "  removed $label"
    fi
  done
  echo "Done. Logs in $LOGS are left alone."
}

init_volume() {
  # Writes the sentinel db.assert_volume_ready looks for. Without it the guard
  # cannot tell the real disk from any other volume mounted at the same path.
  local dir uuid
  dir="$(dirname "$DB")"
  "$PY" - "$DB" <<'CHECK' || exit 1
import os, pathlib, sys
from jobhunt import db
path = pathlib.Path(sys.argv[1])
root = db.volume_root(path)
if root is None:
    print(f"  {path} is not under /Volumes — nothing to stamp.")
    sys.exit(1)
if not os.path.ismount(root):
    print(f"  {root} is not mounted. Plug the disk in first.")
    sys.exit(1)
CHECK
  mkdir -p "$dir"
  uuid="$(uuidgen)"
  printf '%s\n' "$uuid" > "$dir/.jobhunt-volume"
  chmod 600 "$dir/.jobhunt-volume"
  echo "  wrote $dir/.jobhunt-volume"
  echo
  echo "  Add this line to $REPO/.env so a different disk mounted at the same"
  echo "  path is rejected rather than written to:"
  echo
  echo "    JOBHUNT_DB_VOLUME_ID=$uuid"
  echo
}

case "${1:-}" in
  --uninstall) uninstall; exit 0 ;;
  --init-volume) init_volume; exit 0 ;;
esac

cat <<BANNER

  About to install launchd agents for jobhunt.

    repo      $REPO
    database  $DB
    logs      $LOGS
    agents    ${PLISTS[*]}

    com.jobhunt.discover   ingest + score, 06:30 and 18:30 daily
    com.jobhunt.dashboard  always on, 127.0.0.1 only
    com.jobhunt.backup     snapshot to the internal disk, 03:30 daily

  ONLY the Mac mini should run these. It is the single writer: the DB file,
  the timers, and the dashboard all live there, and the laptops reach it over
  Tailscale. Installing on a second machine gives you two scrapers, two
  scoring workers spending real money on the same rows, and two processes
  writing one SQLite file.

  This machine is: $(hostname)

BANNER

if [ "${1:-}" != "--yes" ]; then
  read -r -p "  Is this the mini, and is it the only machine running these? [y/N] " reply
  case "$reply" in
    [yY]) ;;
    *) echo "  Nothing installed."; exit 1 ;;
  esac
fi

# The extras are imported lazily inside functions, so a venv without them
# installs clean, passes every check that only imports jobhunt, and then dies at
# 06:30 on `import jobspy`. Catch it here instead.
"$PY" - <<'CHECK' || exit 1
import importlib.util, sys
groups = {"anthropic": "llm", "jobspy": "ingest", "rendercv": "resume"}
missing = [m for m in groups if importlib.util.find_spec(m) is None]
if missing:
    print(f"  venv is missing {missing}")
    print(f"  run: uv pip install -e '.[{','.join(sorted({groups[m] for m in missing}))}]'")
    sys.exit(1)
CHECK

# Do not install agents onto a deployment that is already unhealthy — they would
# just write the same failure into a log twice a day.
"$PY" -m jobhunt.doctor || { echo; echo "  doctor failed — fix the above first."; exit 1; }

mkdir -p "$AGENTS" "$LOGS"
chmod +x "$REPO"/deploy/*.sh

for label in "${PLISTS[@]}"; do
  # The shell scripts self-locate now, so only the plists are templated.
  sed -e "s|__REPO__|$REPO|g" -e "s|__LOGS__|$LOGS|g" \
    "$REPO/deploy/$label.plist" > "$AGENTS/$label.plist"
  plutil -lint -s "$AGENTS/$label.plist" >/dev/null
  # bootout first so a reinstall replaces rather than erroring on a live label.
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "  installed $label"
done

cat <<NEXT

  Installed. Things this does not do for you:

    sudo pmset -a sleep 0 disablesleep 1 disksleep 0 autorestart 1
      Default sleep kills the scheduled runs; autorestart brings the machine
      back after a power cut. CLAUDE.md, Environment gotchas.

    tailscale serve --bg --https=443 http://127.0.0.1:${JOBHUNT_PORT:-8000}
      Publishes the dashboard on the tailnet over HTTPS. --bg persists across
      reboots. Disable key expiry for this node in the admin console, or it
      goes dark in 180 days.

    launchctl kickstart -k gui/$(id -u)/com.jobhunt.discover
      Runs discovery now rather than waiting for 06:30. Tail it:
      tail -f $LOGS/discover.log

  Dashboard: http://127.0.0.1:${JOBHUNT_PORT:-8000}
  Health:    make doctor
  Uninstall: ./deploy/install.sh --uninstall

NEXT
