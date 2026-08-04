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
#   ./deploy/install.sh            interactive, asks before doing anything
#   ./deploy/install.sh --yes      for when you have read this once already
#   ./deploy/install.sh --uninstall

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/jobhunt"
DB="${JOBHUNT_DB:-$REPO/jobhunt.db}"
PLISTS=(com.jobhunt.discover com.jobhunt.dashboard)

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

if [ "${1:-}" = "--uninstall" ]; then
  uninstall
  exit 0
fi

cat <<BANNER

  About to install launchd agents for jobhunt.

    repo      $REPO
    database  $DB
    logs      $LOGS
    agents    ${PLISTS[*]}

    com.jobhunt.discover   ingest + score, 06:30 and 18:30 daily
    com.jobhunt.dashboard  always on, 127.0.0.1 only

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

# A missing venv means the agents would fail silently twice a day forever.
[ -x "$REPO/.venv/bin/python" ] || { echo "  no venv at $REPO/.venv — run 'make venv' first"; exit 1; }

mkdir -p "$AGENTS" "$LOGS"
chmod +x "$REPO/deploy/discover.sh"

for label in "${PLISTS[@]}"; do
  sed -e "s|__REPO__|$REPO|g" -e "s|__DB__|$DB|g" -e "s|__LOGS__|$LOGS|g" \
    "$REPO/deploy/$label.plist" > "$AGENTS/$label.plist"
  # bootout first so a reinstall replaces rather than erroring on a live label.
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "  installed $label"
done

cat <<'NEXT'

  Installed. Two things this does not do for you:

    sudo pmset -a sleep 0 disablesleep 1
      Default sleep kills the scheduled runs. CLAUDE.md, Environment gotchas.

    launchctl kickstart -k gui/$(id -u)/com.jobhunt.discover
      Runs discovery now rather than waiting for 06:30, if you want to watch
      the first one. Tail it:  tail -f ~/Library/Logs/jobhunt/discover.log

  Dashboard: http://127.0.0.1:8000
  Uninstall: ./deploy/install.sh --uninstall

NEXT
