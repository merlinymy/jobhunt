#!/bin/bash
# One scheduled discovery run: ingest, then score.
#
# A wrapper rather than two launchd agents on a timer offset, because `score`
# genuinely depends on `ingest` having finished — staggering two agents by an
# hour and hoping is the kind of thing that works until Indeed is slow one
# morning. Here the dependency is `&&`.
#
# Runs under launchd, which means: no shell profile, a minimal PATH, and no
# terminal. Everything is absolute, and both streams go to the log.

set -o pipefail

REPO="__REPO__"
PY="$REPO/.venv/bin/python"
LOG_DIR="$HOME/Library/Logs/jobhunt"

mkdir -p "$LOG_DIR"
cd "$REPO" 2>/dev/null || { echo "$(date -u +%FT%TZ) repo missing: $REPO"; exit 1; }

stamp() { date -u +%FT%TZ; }

echo "===== $(stamp) discovery run starting ====="

# Ingest is the one that can be refused by Indeed. It exits 0 even when the
# scrape aborts early — the board poll is a separate source and still ran — so
# score follows regardless, and the reason is in the log above it.
echo "--- $(stamp) ingest"
"$PY" -m jobhunt.ingest
ingest_status=$?

echo "--- $(stamp) score"
"$PY" -m jobhunt.score
score_status=$?

echo "===== $(stamp) done (ingest=$ingest_status score=$score_status) ====="

# Keep the log from growing without bound. launchd has no rotation of its own,
# and a run a day appending forever is a slow leak nobody notices until the disk
# is full. Truncate to the most recent 5000 lines after each run.
LOG="$LOG_DIR/discover.log"
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Non-zero only if both failed. One dead source is a bad morning, not a broken
# install, and a red agent in every log for a week trains you to ignore it.
[ $ingest_status -ne 0 ] && [ $score_status -ne 0 ] && exit 1
exit 0
