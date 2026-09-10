#!/usr/bin/env python3
"""Fail if a shared module could crash a Windows console.

WHY THIS IS AUTOMATED RATHER THAN WRITTEN DOWN: on 2026-09-10 a U+2192 arrow in a
success print raised UnicodeEncodeError on the laptop's cp1252 stdout and turned a
DELIVERED email into `return False`. The same day, GMAIL_USER was correct in Actions
and empty on the laptop. Both are one shape: SHARED CODE HAS TWO EXECUTION CONTEXTS
AND WE KEEP VERIFYING ONLY THE UTF-8 ONE. A GitHub runner can never reproduce either.

Documenting it would not have helped. Both sessions had already agreed in writing
that they do not re-read the documents, so this is a check that runs.

Comment banner rules (U+2500) are exempt: never printed, and rewriting hundreds of
them is churn in a live-money engine for no safety gain.
"""
import io, sys, tokenize

SHARED = ["alpaca_bot.py", "slack_notify.py", "rh_watchdog.py", "mail_check.py"]
EXEMPT = {"\u2500"}
# f-strings tokenize as FSTRING_MIDDLE on 3.12+, NOT as STRING. Missing that is how
# the first sweep reported "0 dangerous" while every order-placement and halt message
# still carried an arrow. Check both, by name so it works across versions.
STRINGISH = {"STRING", "FSTRING_MIDDLE"}


def offenders(path):
    out = []
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return out
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if tokenize.tok_name[t.type] not in STRINGISH:
            continue
        bad = sorted({c for c in t.string if ord(c) > 127 and c not in EXEMPT})
        if bad:
            out.append((t.start[0], "".join(bad), t.string.strip()[:60]))
    return out


def main():
    total = 0
    for f in SHARED:
        for line, chars, snippet in offenders(f):
            total += 1
            print(f"{f}:{line}: non-ASCII {chars!r} in a string that may be printed: {snippet!r}")
    if total:
        print(f"\n{total} occurrence(s). These crash a cp1252 console (the laptop), "
              f"never a UTF-8 runner. Use ASCII in anything printable.")
        return 1
    print(f"ascii check OK: {len(SHARED)} shared modules, no printable non-ASCII")
    return 0


if __name__ == "__main__":
    sys.exit(main())
