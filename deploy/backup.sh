#!/bin/bash
# Nightly snapshot, 03:30. After any evening work, before the 06:30 discovery run.

set -o pipefail

# shellcheck source=deploy/_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
jh_log_setup backup

cd "$JH_REPO" || { echo "$(jh_stamp) repo missing: $JH_REPO"; exit 1; }

echo "===== $(jh_stamp) backup ====="

# Note the asymmetry with discover.sh: a skipped scrape is a bad morning and
# exits 0, but a skipped backup deserves a red agent. The night the disk was
# unplugged is exactly the night you want to have noticed.
if ! jh_require_db "${JOBHUNT_WAIT_DB:-300}"; then
  echo "$(jh_stamp) database unavailable, no backup taken"
  exit 1
fi

"$JH_PY" -m jobhunt.backup
status=$?

# Sunday: prove a restore would actually work, not just that a file exists.
if [ "$(date -u +%u)" = "7" ]; then
  echo "--- $(jh_stamp) weekly drill"
  "$JH_PY" -m jobhunt.backup --drill || status=1
fi

echo "===== $(jh_stamp) done (status=$status) ====="
exit $status
