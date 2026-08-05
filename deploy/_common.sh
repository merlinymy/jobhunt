# Sourced by discover.sh and backup.sh. Never executed directly.
#
# launchd gives these scripts no shell profile, a minimal PATH, and no terminal.
# Everything here is about surviving that, plus the two things that bit us:
# a log that vanished on rotation, and a run that wrote to a decoy database.

# Where we are, without a template step. install.sh used to sed __REPO__ into the
# plists but not into the scripts, so discover.sh kept the literal placeholder and
# every scheduled run died on `cd __REPO__` before ingesting anything. launchd
# execs these by absolute path, so BASH_SOURCE already knows.
JH_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JH_PY="$JH_REPO/.venv/bin/python"

jh_stamp() { date -u +%FT%TZ; }

jh_log_setup() {
  # $1 = log basename, e.g. "discover"
  local name="$1" tmp
  JH_LOG_DIR="${JOBHUNT_LOG_DIR:-$HOME/Library/Logs/jobhunt}"
  JH_LOG="$JH_LOG_DIR/$name.log"
  mkdir -p "$JH_LOG_DIR"

  # Truncate in place, before we open our own descriptor. The old version did
  # `tail > tmp && mv tmp log`, which swaps the inode while launchd still holds
  # the original open on StandardOutPath — so after the first rotation every
  # line went to a deleted file. Copying the tail back into the same inode keeps
  # the file identity, and opening our fd afterwards avoids a stale offset.
  if [ -f "$JH_LOG" ] && [ "$(wc -l < "$JH_LOG")" -gt 5000 ]; then
    tmp="$(mktemp "${TMPDIR:-/tmp}/jobhunt.XXXXXX")"
    if tail -n 5000 "$JH_LOG" > "$tmp"; then cat "$tmp" > "$JH_LOG"; fi
    rm -f "$tmp"
  fi

  exec >> "$JH_LOG" 2>&1
}

jh_require_db() {
  # $1 = seconds to wait for the volume. One implementation of "is the disk
  # there", in Python, rather than mount rules re-derived in bash. The callers
  # read JOBHUNT_WAIT_DB first, so the skip paths can actually be exercised
  # without sitting through five minutes of the real budget.
  "$JH_PY" -m jobhunt.doctor --require-db --wait "${1:-120}" --quiet
}
