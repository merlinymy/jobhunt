#!/bin/bash
# One scheduled discovery run: ingest, then score.
#
# A wrapper rather than two launchd agents on a timer offset, because `score`
# genuinely depends on `ingest` having finished — staggering two agents by an
# hour and hoping is the kind of thing that works until Indeed is slow one
# morning. Here the dependency is `&&`.

set -o pipefail

# shellcheck source=deploy/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
jh_log_setup discover

cd "$JH_REPO" || { echo "$(jh_stamp) repo missing: $JH_REPO"; exit 1; }

echo "===== $(jh_stamp) discovery run starting ====="

# A missed run is a bad morning. A run against a decoy database is a corrupted
# history, so this gate is not optional — and it exits 0, because an unplugged
# disk is not a broken install and should not paint the agent red forever.
if ! jh_require_db "${JOBHUNT_WAIT_DB:-120}"; then
  echo "$(jh_stamp) database unavailable, skipping this run"
  echo "===== $(jh_stamp) done (skipped) ====="
  exit 0
fi

# Ingest is the one that can be refused by Indeed. It exits 0 even when the
# scrape aborts early — the board poll is a separate source and still ran — so
# score follows regardless, and the reason is in the log above it.
echo "--- $(jh_stamp) ingest"
"$JH_PY" -m jobhunt.ingest
ingest_status=$?

echo "--- $(jh_stamp) score"
"$JH_PY" -m jobhunt.score
score_status=$?

echo "===== $(jh_stamp) done (ingest=$ingest_status score=$score_status) ====="

# Non-zero only if both failed. One dead source is a bad morning, not a broken
# install, and a red agent in every log for a week trains you to ignore it.
[ $ingest_status -ne 0 ] && [ $score_status -ne 0 ] && exit 1
exit 0
