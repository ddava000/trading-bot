#!/bin/bash
# File new Slack messages into AGENT_MAIL.md, and never lose them to another session's append.
#
# WHY (2026-10-22): the Slack step appended to the end of AGENT_MAIL.md on the runner's checkout, committed, and
# pushed through push_retry.sh, whose answer to a rejected push is "pull --rebase". Another session had appended to the
# SAME END of the SAME FILE since that checkout (the run's relay step comes about ten minutes after it starts),
# so the rebase CONFLICTED, was aborted, and the relay was discarded: a green run, one warning annotation, and a
# reply of Devon's that had been read but not filed. The cursor lives in the mailbox itself, so a dropped relay is
# read again next run, but only if that run does not conflict too, and on a busy day it can.
#
# So this never rebases. It works in a throwaway worktree of the NEWEST origin/main (the runner's own working tree,
# with the trade log and the holds ledger in it, is not touched), files the messages there, and pushes. If the push
# is rejected it throws the commit away and files them again on the newest main; the cursor makes the second filing
# exactly the messages that are still unfiled. If it never lands it says so in a warning, not in silence.
#
# Usage: slack_relay.sh      (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID come from the environment)
# RELAY_TRIES (default 5), RELAY_SLEEP seconds between tries (default 3, multiplied by the try number),
# RELAY_BRANCH (default main), PYTHON (default python), RELAY_STRICT.
# ASCII only. Never force-pushes.
#
# EXIT CODE. Default 0 always: inside the Arm A run a relay that cannot land must not fail a trading run. The standalone
# SLACK RELAY workflow sets RELAY_STRICT=1, so there a relay that cannot fetch, cannot make its worktree or never lands
# exits 1: a RED run, which GitHub emails and the daily digest lists. A green run with a warning annotation is how the
# first thread reply was lost twice without anyone looking.
branch="${RELAY_BRANCH:-main}"
tries="${RELAY_TRIES:-5}"
pause="${RELAY_SLEEP:-3}"
py="${PYTHON:-python}"
strict="${RELAY_STRICT:-}"

# $1 = the annotation title, $2 = the message. Always says it; fails the run only in strict mode.
giveup() {
  echo "::warning title=$1::$2"
  if [ -n "$strict" ]; then
    echo "::error title=$1::$2"
    exit 1
  fi
  exit 0
}

git fetch -q origin "$branch" || giveup "Slack relay not run" "could not fetch origin/$branch"
repo="$(pwd)"
tmpd="$(mktemp -d)"
wt="$tmpd/relay"
git worktree add -q --detach "$wt" "origin/$branch" || giveup "Slack relay not run" "could not make a worktree of origin/$branch"
cleanup() { cd / 2>/dev/null; git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1; rm -rf "$tmpd"; }
trap cleanup EXIT
cd "$wt" || giveup "Slack relay not run" "could not enter the worktree"
git config user.name  "alpaca-bot"
git config user.email "alpaca-bot@users.noreply.github.com"

n=0
while [ "$n" -lt "$tries" ]; do
  n=$((n + 1))
  pull_failed=""
  "$py" slack_notify.py --pull-ingest 25 || { pull_failed=1; echo "slack pull errored (non-fatal)"; }
  if [ -z "$(git status --porcelain -- AGENT_MAIL.md)" ]; then
    # nothing to file. Calm if the read worked; if it did not (no token, a refused channel, an API error, more messages
    # than it will page through) say so, and in strict mode fail the run: "nothing" from a read that failed is not calm.
    [ -n "$pull_failed" ] && giveup "Slack read failed" "slack_notify.py --pull-ingest could not read the channel; see its output above"
    exit 0
  fi
  git add -- AGENT_MAIL.md
  git commit -q -m "AGENT_MAIL: relay Slack messages from Devon $(date -u +%FT%TZ)"
  if git push -q origin "HEAD:$branch"; then
    [ "$n" -gt 1 ] && echo "slack relay landed on attempt $n of $tries"
    exit 0
  fi
  echo "slack relay push rejected (attempt $n of $tries); filing again on the newest $branch"
  [ "$pause" -gt 0 ] && sleep $((pause * n))
  if ! git fetch -q origin "$branch" || ! git reset -q --hard "origin/$branch"; then
    echo "could not move to the newest $branch; trying again from where it is"
  fi
done
giveup "Slack relay not filed" "could not push the relay after $tries attempts; the messages stay unfiled and the next run reads them again. Check https://www.githubstatus.com"
