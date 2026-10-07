#!/bin/bash
# Push the current branch, retry a few times, and be LOUD if it never lands.
#
# WHY (2026-10-07): GitHub rejected every write for about 9 minutes ("Internal Server Error"). Arm A's persist
# step ended in `|| true`, so the 15:00Z run's status heartbeat push was rejected, the run finished green,
# and status.json froze with no line anywhere saying why. A healthy run looked dead and the laptop's
# dead-man's switch read a stale file as a stopped bot. Had that run placed a trade, its trade_log and holds
# ledger (the stop levels) would have been lost the same silent way, because the runner's disk is discarded.
#
# Usage: push_retry.sh "<what is being saved>" <fatal|warn>
#   fatal  the state is trade data. If it never lands the step FAILS (red run, annotation).
#   warn   a heartbeat or relay. If it never lands the step stays green but leaves a warning annotation.
# PUSH_RETRY_DELAYS (seconds, space separated) defaults to "0 5 15 30", about 50 seconds in the worst case,
# which keeps a normal 11-minute run inside the workflow's 14-minute timeout.
#
# ASCII only. Never force-pushes. A rejected non-fast-forward is handled with pull --rebase, as before.
what="${1:-state}"
mode="${2:-warn}"
delays="${PUSH_RETRY_DELAYS:-0 5 15 30}"
# The branch is read ONCE, before any rebase: a rebase that stops midway leaves HEAD detached, and reading it
# again would hand "HEAD" to every later attempt.
branch="$(git rev-parse --abbrev-ref HEAD)"
n=0
total=$(echo $delays | wc -w)
for d in $delays; do
  n=$((n + 1))
  [ "$d" -gt 0 ] && sleep "$d"
  if git push; then
    [ "$n" -gt 1 ] && echo "push of '$what' landed on attempt $n of $total"
    exit 0
  fi
  echo "push of '$what' failed (attempt $n of $total)"
  # A non-fast-forward is the common case; harmless when the failure was a server error. If the rebase itself
  # fails (a conflict, a missing git identity), ABORT it: left half-done it detaches HEAD and every later attempt
  # would push a broken state, or nothing, while the log looked busy.
  if ! git pull --rebase --autostash origin "$branch"; then
    echo "rebase onto origin/$branch failed; aborting it so the next attempt pushes the original commit"
    git rebase --abort 2>/dev/null
  fi
done
msg="could not push '$what' after $total attempts. The commit exists only on this runner and will be discarded. Check https://www.githubstatus.com"
if [ "$mode" = "fatal" ]; then
  echo "::error title=Arm A state not saved::$msg"
  exit 1
fi
echo "::warning title=Arm A heartbeat not saved::$msg"
exit 0
