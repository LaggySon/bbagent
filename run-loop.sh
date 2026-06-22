#!/usr/bin/env bash
# Watcher: poll on a short interval and run a full management pass every tick.
# Lineups, injuries, incoming/declined offers and waiver claims all move on
# their own clock, so this watches continuously rather than batching once a day.
# Each pass is safe to repeat — `plan` reads ESPN's pending transactions and
# won't re-offer players already in a pending trade, and waivers are bounded by
# max_waivers — so re-running every few minutes doesn't spam offers or churn.
#
# Used as the container's default CMD (see Dockerfile); also works standalone.
# Override via env vars:
#   RUN_CMD  — the bbagent command to run   (default: plan --execute --commit)
#   INTERVAL — seconds between passes        (default: 600 = poll every 10 min)
set -euo pipefail
cd "$(dirname "$0")"

RUN_CMD="${RUN_CMD:-python espn_agent.py plan --execute --commit}"
INTERVAL="${INTERVAL:-600}"

echo "watcher: '$RUN_CMD' every ${INTERVAL}s" >> loop.log
while true; do
  # shellcheck disable=SC2086
  $RUN_CMD >> loop.log 2>&1 || echo "watcher: pass exited non-zero" >> loop.log
  sleep "$INTERVAL"
done
