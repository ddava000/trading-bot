"""
rh_daemon.py — the always-on Robinhood laptop bot.

IDENTICAL cadence to the cloud bot:
  * full strategy cycle every 15 minutes
  * protective pass (hard stops, hold floors, news tripwire) every 60 seconds

Why this is affordable: the DECISION side is pure Python and free — Yahoo prices
plus a local ledger — so it can run every 60s forever. Tokens are spent only to
PLACE an order or to reconcile with the broker, which happens a handful of times
a day. Robinhood has no usable programmatic order API (their agentic MCP is the
supported path), so a short headless agent turn is the execution bridge.

The agent is a DUMB EXECUTOR. It is handed an exact order list and told not to
add, skip, or modify anything. All judgment lives in rh_bot.decide().

Files:
  rh_config.json     local only, gitignored — {"account": "...", "claude": "claude"}
  rh_ledger.json     local only, gitignored — positions/cash/holds/unsettled
  rh_status.json     COMMITTED — equity + sleeve snapshot, no account numbers
  rh_trade_log.jsonl COMMITTED — every fill, so Claude can monitor from anywhere
  rh_HALT            create this file to stop all trading immediately

Run:  python rh_daemon.py            (live)
      python rh_daemon.py --dry      (decide + log, place nothing)
      python rh_daemon.py --once     (one cycle, then exit)
"""

import os, sys, json, time, subprocess
from datetime import datetime

import rh_bot
import alpaca_bot as bot

CONFIG_F, LEDGER_F = "rh_config.json", "rh_ledger.json"
STATUS_F, LOG_F    = "rh_status.json", "rh_trade_log.jsonl"
HALT_F             = "rh_HALT"

FULL_CYCLE_SEC = 900     # 15 min, matches the cloud bot's trigger cadence
FAST_PASS_SEC  = 60      # 60 s, matches the cloud bot's protective pass
MAX_ORDERS_DAY = 40      # circuit breaker: a runaway loop can't machine-gun orders
AGENT_TIMEOUT  = 240
PUSH_HEARTBEAT_SEC = 900 # liveness ping when nothing changed, so a quiet laptop
                         # and a dead one don't look the same to whoever watches

_last_push_at, _last_push_material = 0.0, None

DRY = "--dry" in sys.argv


_singleton_handle = None


def acquire_singleton():
    """Guarantee ONE daemon, via a Windows named mutex the OS frees on exit.

    On 2026-07-23 three live daemons ran at once against the same account. Cause:
    os.execv on Windows SPAWNS a new process instead of replacing the current one
    (unlike Unix), and each code-sync restart after a push stacked another copy
    that Task Scheduler's MultipleInstances tracking never saw. A shared ledger
    with three writers is a real way to double-place orders.

    A named mutex is the canonical Windows singleton: held for the process
    lifetime, released automatically even on a hard kill, so there is no stale
    lock to clean up. Fails OPEN on any error: better to run than to refuse to
    trade over a bug in the guard itself.
    """
    global _singleton_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "rh_trading_bot_singleton")
        ERROR_ALREADY_EXISTS = 183
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _singleton_handle = h        # keep the handle for the process lifetime
        return True
    except Exception:
        return True


def now_et():
    """Always ET, never local time.

    This laptop runs on US Central. Stamping rh_status.json with local time made
    remote monitoring read a file committed one minute ago as an hour stale and
    report the bot dead. The cloud bot stamps ET, so this matches it exactly,
    including the format, because that side compares these as strings.
    """
    return datetime.now(bot.ET_TZ)


DAEMON_LOG = "rh_daemon.log"


def log(msg):
    """Write to rh_daemon.log directly, and to stdout when there is one.

    The task used to run "cmd.exe /c python ... >> rh_daemon.log", which meant a
    console had to exist for logging to work. That console is what kept killing
    the bot. Owning the log file here lets the task run pythonw.exe with no
    console at all, and pythonw gives the process no stdout, hence the None check.
    """
    line = f"[{now_et().strftime('%Y-%m-%d %H:%M:%S')} ET] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with open(DAEMON_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load(path, default):
    # utf-8-sig, not utf-8: PowerShell 5.1 writes JSON with a BOM, and a bare
    # json.load chokes on it, which used to look identical to a missing file.
    try:
        with open(path, encoding="utf-8-sig") as f: return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"WARNING: {path} exists but could not be parsed ({e}), using default")
        return default


def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f: json.dump(obj, f, indent=1, sort_keys=True)


def save_ledger(led):
    """Stamp the mode INTO the ledger, then write it.

    rh_status.json also carries a dry flag, but it describes the last CYCLE, not
    the ledger. One stray `rh_daemon.py --dry --once` against a live install
    rewrites status to dry:true while the real ledger sits there untouched, and
    -Live keys off exactly that flag to decide whether wiping the ledger is safe.
    Done that once already. Keeping the flag with the data it describes makes the
    desync impossible.
    """
    led["dry"] = DRY
    _save(LEDGER_F, led)


CFG = _load(CONFIG_F, {})
ACCOUNT    = CFG.get("account", "")
CLAUDE_BIN = CFG.get("claude", "claude")

# Reuse alpaca_bot.send_email rather than reimplementing SMTP here, so the laptop
# and the cloud bot mail through one code path. The cloud gets its password from a
# GitHub secret; this laptop has no secrets store, so it comes from rh_config.json,
# which is gitignored and never leaves the machine. Unset just means no mail:
# send_email already no-ops without a password and never raises.
if CFG.get("gmail_app_password"):
    bot.GMAIL_APP_PW = CFG["gmail_app_password"]


# ── Execution bridge: one short headless agent turn, MCP tools only ──────────
# The Robinhood connector is a claude.ai MCP server, so its tools are DEFERRED:
# they are not in the headless model's tool list until ToolSearch loads them.
# Without the preamble the agent reports "no trading tools available" and
# cheerfully returns an empty plan — which reads exactly like a flat market.
RH_SERVER = "mcp__claude_ai_Robinhood"

# ToolSearch's select: takes EXACT tool names. Passing the bare server prefix
# matched only about one run in three, so name every tool the daemon uses.
# place_option_order is deliberately absent: the allowlist is where the
# "equities only, never options" rail is actually enforced, not the prompt.
RH_TOOLS = [f"{RH_SERVER}__{t}" for t in (
    "get_portfolio", "get_equity_positions", "get_equity_quotes",
    "place_equity_order", "cancel_equity_order", "get_equity_orders",
)]
RH_PREAMBLE = (
    "FIRST, before anything else, call ToolSearch with this exact query to load "
    "the Robinhood tools (they are deferred and uncallable until you do):\n"
    f"select:{','.join(RH_TOOLS)}\n"
    "If ToolSearch returns no Robinhood tools, output {\"error\":\"no_tools\"} "
    "and stop. Never guess, estimate, or fabricate account or market data.\n\n"
)
# ToolSearch must itself be allowed, or the agent cannot load anything.
RH_ALLOWED = " ".join(["ToolSearch"] + RH_TOOLS)


def agent(prompt):
    """Run a headless Claude turn and return the JSON object it printed."""
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", RH_PREAMBLE + prompt,
                            "--allowedTools", RH_ALLOWED],
                           capture_output=True,
                           text=True, timeout=AGENT_TIMEOUT)
        out = (r.stdout or "").strip()
        i, j = out.find("{"), out.rfind("}")
        if i >= 0 and j > i:
            res = json.loads(out[i:j + 1])
            # Loud, because a silent None here reads downstream as "flat market"
            # rather than "the bridge is broken", which is how a dead bridge
            # spent a whole session looking like a calm day.
            if isinstance(res, dict) and res.get("error") == "no_tools":
                log("agent could NOT load the Robinhood MCP tools, skipping "
                    "this pass rather than acting on missing data")
                return None
            return res
        log(f"agent returned no JSON: {out[:200]}")
    except Exception as e:
        log(f"agent call failed: {e}")
    return None


def reconcile():
    """Ask the broker for truth. Returns {cash, positions:[...]} or None."""
    res = agent(
        f"Using the Robinhood MCP tools, call get_portfolio and get_equity_positions "
        f"for account {ACCOUNT}. Place no orders. Reply with ONLY a JSON object, no prose:\n"
        '{"cash": <buying_power as number>, "positions": '
        '[{"symbol": "X", "qty": <number>, "avg_cost": <number>}]}')
    if res and isinstance(res.get("positions"), list):
        return res
    return None


def adopt_truth(led, truth):
    """Take the broker's view into the ledger, unless the snapshot looks corrupt.

    Returns True when adopted. A real Alpaca blip on 2026-07-07 returned an empty
    position list and wiped the cloud bot's hold ledger, which cost real money.
    alpaca_bot guards that on both its passes; the laptop had no equivalent, so a
    single flaky MCP response would clear positions AND the hold basis/peak
    history here, and the next cycle would re-buy the whole book from scratch.

    Holds are pruned to match real positions on every adopted snapshot, so a hold
    entry for a name we no longer own cannot linger and keep ratcheting.
    """
    if not truth:
        return False
    positions = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                  "avg_cost": float(p.get("avg_cost") or 0)}
                 for p in truth.get("positions") or [] if float(p["qty"]) > 0]
    if not positions and led.get("positions"):
        if DRY:
            # Dry fills are simulated, so the real book disagreeing is expected,
            # not a fault. Keeping the simulated ledger is also what stops a dry
            # run re-deciding the same buys every cycle forever.
            log(f"dry run: {len(led['positions'])} simulated position(s) vs the "
                f"broker's empty book, as expected; keeping the simulated ledger")
        else:
            log(f"!! broker reported ZERO positions but the ledger holds "
                f"{len(led['positions'])} name(s): corrupt snapshot, keeping the ledger")
        return False
    led["cash"] = float(truth.get("cash") or led["cash"])
    led["positions"] = positions
    led["holds"] = {s: h for s, h in (led.get("holds") or {}).items()
                    if any(p["symbol"] == s for p in positions)}
    return True


def place(orders):
    """Hand the agent an exact order list. Returns the fills it reports."""
    if DRY:
        log(f"DRY RUN — would place {len(orders)} order(s)")
        return []
    res = agent(
        f"Place these EXACT orders on Robinhood account {ACCOUNT} using the MCP order "
        f"tools. Do NOT add, skip, resize, or substitute any order, and do not place "
        f"anything not listed. Equities only, never options. Use market orders; for "
        f'buys use the "notional" dollar amount, for sells use "qty".\n\n'
        f"ORDERS:\n{json.dumps(orders, indent=1)}\n\n"
        'Then reply with ONLY a JSON object, no prose:\n'
        '{"placed": [{"symbol": "X", "action": "buy|sell", "status": "ok|rejected", '
        '"detail": "<broker message if rejected>"}]}')
    return (res or {}).get("placed") or []


# ── Ledger ──────────────────────────────────────────────────────────────────
def fresh_ledger():
    return {"cash": 0.0, "positions": [], "holds": {}, "last_buy": {},
            "sold_today": 0.0, "day": "", "orders_today": 0}


def roll_day(led, today):
    """New day: T+1 proceeds settle, daily order budget resets."""
    if led.get("day") != today:
        led.update({"day": today, "sold_today": 0.0, "orders_today": 0})
        return True
    return False


def apply_fills(led, orders, placed, prices):
    """Update the local ledger from what actually got placed."""
    ok = {(p.get("symbol"), p.get("action")) for p in placed if p.get("status") == "ok"}
    today = now_et().strftime("%Y-%m-%d")     # ET: roll_day stamps the ET date too,
                                              # and last_buy feeds the time stop
    pos = {p["symbol"]: p for p in led["positions"]}
    for o in orders:
        sym, act = o["symbol"], o["action"]
        if placed and (sym, act) not in ok:
            continue                                  # rejected — ledger untouched
        px = prices.get(sym) or 0
        if act == "buy" and px > 0:
            qty = o["notional"] / px
            cur = pos.get(sym)
            if cur:
                tot = cur["qty"] + qty
                cur["avg_cost"] = (cur["avg_cost"] * cur["qty"] + px * qty) / tot if tot else px
                cur["qty"] = tot
            else:
                pos[sym] = {"symbol": sym, "qty": qty, "avg_cost": px}
            led["last_buy"][sym] = today
            if o.get("sleeve") == "HOLD":
                led["holds"].setdefault(sym, {"basis": px, "peak": px})
            led["cash"] = max(0.0, led["cash"] - o["notional"])
        elif act == "sell":
            cur = pos.get(sym)
            if cur:
                sell_qty = min(float(o.get("qty") or cur["qty"]), cur["qty"])
                proceeds = sell_qty * px
                cur["qty"] -= sell_qty
                led["cash"] += proceeds
                led["sold_today"] += proceeds         # T+1: unspendable until tomorrow
                if cur["qty"] <= 1e-9:
                    pos.pop(sym, None); led["holds"].pop(sym, None)
    led["positions"] = [p for p in pos.values() if p["qty"] > 1e-9]
    # The cap is a circuit breaker on REAL submissions, so simulated orders must
    # not consume it. Dry cycles never reach the broker, and reconcile() resets
    # positions to the broker's (empty) truth every cycle, so a dry run re-decides
    # the same buys forever. At ~7/cycle that burns the 40/day budget in about an
    # hour, after which cycle() goes quiet in a way that reads as "found nothing".
    if not DRY:
        led["orders_today"] = led.get("orders_today", 0) + len(orders)


def track_peaks(led, prices):
    """Hold-sleeve peak ratchet, same as the cloud bot's holds.json."""
    for sym, h in led.get("holds", {}).items():
        px = prices.get(sym)
        if px and px > float(h.get("peak") or 0):
            h["peak"] = round(px, 4)


def _material(snap):
    """The parts of a snapshot worth pushing over.

    Deliberately excludes equity/index/hold/trade and ts: those are price-derived
    and move every single pass, which is why the old code committed once a minute.
    Positions, holds and the order count only change when something real happened.
    """
    return {"positions": snap.get("positions"), "holds": snap.get("holds"),
            "orders_today": snap.get("orders_today"), "dry": snap.get("dry")}


def _push_status(reason):
    """Commit and push the monitoring files. Bounded so a stall cannot eat passes.

    This runs INSIDE the trading loop, so every second spent here is a second the
    exit rails are not checking prices. Timeouts total ~140s worst case instead of
    the old ~390s, and it now runs a couple of dozen times a day rather than ~370.
    """
    try:
        subprocess.run(["git", "add", STATUS_F, LOG_F], capture_output=True, timeout=15)
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          capture_output=True, timeout=15).returncode == 0:
            return False                      # nothing staged, nothing to say
        subprocess.run(["git", "commit", "-m",
                        f"rh bot {now_et().strftime('%Y-%m-%dT%H:%M')} ET ({reason})"],
                       capture_output=True, timeout=20)
        if subprocess.run(["git", "push"], capture_output=True, timeout=30).returncode != 0:
            subprocess.run(["git", "pull", "--rebase", "--autostash"],
                           capture_output=True, timeout=30)
            subprocess.run(["git", "push"], capture_output=True, timeout=30)
        return True
    except Exception as e:
        log(f"git persist skipped: {e}")
        return False


def email_trades(res, placed, led):
    """Tell Devon what the laptop bot just did. No subject-line emoji: he prints
    these to PDF and the subject becomes the filename."""
    orders = res.get("orders") or []
    if not orders:
        return
    ok = sum(1 for p in placed if p.get("status") == "ok")
    bad = [p for p in placed if p.get("status") != "ok"]
    lines = [f"Robinhood laptop bot ({'DRY' if DRY else 'LIVE'})",
             f"{now_et().strftime('%Y-%m-%d %H:%M ET')}", "",
             f"{ok}/{len(orders)} order(s) accepted.", ""]
    for o in orders:
        st = next((p.get("status") for p in placed
                   if p.get("symbol") == o["symbol"] and p.get("action") == o["action"]),
                  "dry" if DRY else "unknown")
        amt = o.get("notional")
        size = f"${amt:.2f}" if amt else f"{o.get('qty')} sh"
        lines.append(f"  [{st:8}] {o['action'].upper():4} {o['symbol']:6} {size:>9}  {o['reason']}")
    if bad:
        lines += ["", "NOT ACCEPTED:"] + [f"  {p.get('symbol')}: {p.get('detail') or p.get('status')}"
                                          for p in bad]
    snap = res.get("snapshot") or {}
    lines += ["", f"equity ${snap.get('equity', 0):.2f} | cash ${led.get('cash', 0):.2f} "
                  f"| orders today {led.get('orders_today', 0)}/{MAX_ORDERS_DAY}",
              f"positions: {', '.join(p['symbol'] for p in led['positions']) or 'none'}",
              "", "Stop it: create a file named rh_HALT in the repo folder."]
    subject = (f"RH laptop bot: {ok}/{len(orders)} order(s) "
               f"{'placed' if not DRY else 'simulated'}")
    # Log the attempt. send_email's only feedback is a print() to a stream the
    # daemon does not capture, so email delivery was previously invisible: a trade
    # went out with zero trace of whether the notification did. If no password is
    # configured say so, since that is the silent-no-mail case.
    if not bot.GMAIL_APP_PW:
        log(f"NOT emailing {ok}/{len(orders)} fills: no gmail_app_password set")
        return
    try:
        bot.send_email(subject, "\n".join(lines))
        log(f"emailed {ok}/{len(orders)} fills to {bot.ALERT_TO}")
    except Exception as e:
        log(f"email FAILED ({e}) — trade still placed, notification lost")


def persist(led, res, placed):
    """Write the ledger locally; commit the shareable log/status for monitoring."""
    global _last_push_at, _last_push_material
    save_ledger(led)
    snap = dict(res.get("snapshot") or {})
    snap.update({"ts": now_et().strftime("%Y-%m-%dT%H:%M"),
                 "positions": {p["symbol"]: round(p["qty"], 6) for p in led["positions"]},
                 "holds": sorted(led.get("holds") or {}),
                 "orders_today": led.get("orders_today", 0), "dry": DRY})
    _save(STATUS_F, snap)                     # local write every pass: free, instant
    traded = False
    if placed or res.get("orders"):
        traded = True
        with open(LOG_F, "a") as f:
            for o in res["orders"]:
                st = next((p.get("status") for p in placed
                           if p.get("symbol") == o["symbol"] and p.get("action") == o["action"]),
                          "dry" if DRY else "unknown")
                f.write(json.dumps({**o, "ts": now_et().strftime("%Y-%m-%dT%H:%M"),
                                    "status": st, "venue": "robinhood"}) + "\n")

    # Push on: a trade (monitoring must see fills immediately), a structural change,
    # or the heartbeat. The heartbeat is not optional: without it a quiet laptop and
    # a dead laptop look identical to whoever is watching the repo.
    material = _material(snap)
    due = (time.time() - _last_push_at) >= PUSH_HEARTBEAT_SEC
    reason = ("trade" if traded else
              "change" if material != _last_push_material else
              "heartbeat" if due else None)
    if reason and _push_status(reason):
        _last_push_at, _last_push_material = time.time(), material


# ── Code sync: inherit cloud-bot improvements, but verify before trusting ───
def sync_code():
    """Pull upstream changes and prove they work before running on them.

    rh_bot.py imports its rails straight from alpaca_bot, so a strategy fix made
    to the cloud bot reaches this laptop through git and nothing else. Without
    this the daemon would run whatever code it started with, forever.

    A bad upstream push must never break trading here, so new code has to pass
    rh_bot's selftest; if it fails we hard-reset to the commit we were already
    running and keep trading on known-good code. Returns True when the process
    should restart to load the new modules (Python caches imports)."""
    try:
        def git(*a, t=90):
            return subprocess.run(["git", *a], capture_output=True, text=True, timeout=t)
        before = git("rev-parse", "HEAD").stdout.strip()
        git("pull", "--rebase", "--autostash", "--quiet")
        after = git("rev-parse", "HEAD").stdout.strip()
        if not after or after == before:
            return False
        log(f"upstream code changed {before[:7]} -> {after[:7]} - verifying before use")
        chk = subprocess.run([sys.executable, "rh_bot.py", "--selftest"],
                             capture_output=True, text=True, timeout=180)
        if chk.returncode != 0:
            log("!! NEW CODE FAILED SELFTEST - rolling back, staying on known-good code")
            log((chk.stdout or "")[-400:] + (chk.stderr or "")[-400:])
            git("reset", "--hard", before)
            return False
        log("new code passed selftest - restarting to load it")
        return True
    except Exception as e:
        log(f"code sync skipped ({e}) - continuing on current code")
        return False


# ── Main loop ───────────────────────────────────────────────────────────────
def cycle(led, fast):
    prices = {}
    state = {"cash": led["cash"], "unsettled": led.get("sold_today", 0.0),
             "positions": led["positions"], "holds": led.get("holds") or {},
             "last_buy": led.get("last_buy") or {}}
    res = rh_bot.decide(state, fast=fast)

    for p in led["positions"]:
        live, _s, _c = rh_bot._quote(p["symbol"], [])
        if live: prices[p["symbol"]] = live
    for o in res["orders"]:
        if o["symbol"] not in prices:
            live, _s, _c = rh_bot._quote(o["symbol"], [])
            if live: prices[o["symbol"]] = live
    track_peaks(led, prices)

    placed = []
    if res["orders"]:
        if led.get("orders_today", 0) + len(res["orders"]) > MAX_ORDERS_DAY:
            log(f"ORDER CAP hit ({MAX_ORDERS_DAY}/day) — skipping {len(res['orders'])} order(s)")
            res["orders"] = []
        else:
            for o in res["orders"]:
                log(f"  {o['action'].upper()} {o['symbol']} "
                    f"{o.get('notional') or o.get('qty')} — {o['reason']}")
            placed = place(res["orders"])
            apply_fills(led, res["orders"], placed, prices)
            # Truth up after every trade. The orders are already away, so a
            # corrupt snapshot here means keep the local ledger, not skip.
            adopt_truth(led, reconcile())
            email_trades(res, placed, led)      # after reconcile: report real state
    for n in res.get("notes", []):
        log(f"  note: {n}")
    persist(led, res, placed)
    return res


def _detach_from_console():
    """Survive the console that launched us going away.

    The scheduled task runs with LogonType Interactive, so it attaches to whatever
    console started it. Closing that window delivers CTRL_C to the whole group and
    kills the daemon: observed twice on 2026-07-23, exit 0xC000013A
    (STATUS_CONTROL_C_EXIT), leaving seven live positions with no protective pass.

    Only ignored when stdout is NOT a terminal, i.e. when running under the task
    with output redirected to rh_daemon.log. Run it by hand in a real terminal and
    Ctrl+C still works. Stop-ScheduledTask terminates rather than signalling, so
    the task remains stoppable, and rh_HALT is the real kill switch regardless.
    """
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            import signal
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            return True
    except Exception:
        pass
    return False


def main():
    if not acquire_singleton():
        log("another rh_daemon is already running — this instance is exiting")
        return 0
    if _detach_from_console():
        log("running detached: console Ctrl+C ignored, use rh_HALT to stop")
    if not ACCOUNT:
        log(f"No account configured. Create {CONFIG_F}: "
            '{"account": "<robinhood agentic account number>", "claude": "claude"}')
        return 1
    log(f"rh_daemon starting | account …{ACCOUNT[-4:]} | "
        f"{'DRY RUN' if DRY else 'LIVE'} | full {FULL_CYCLE_SEC}s / fast {FAST_PASS_SEC}s")

    led = _load(LEDGER_F, fresh_ledger())
    last_full = 0.0
    while True:
        if os.path.exists(HALT_F):
            log("rh_HALT present — trading paused")
            if "--once" in sys.argv:      # otherwise --once spins here forever
                return 0
            time.sleep(FAST_PASS_SEC); continue
        open_, et = bot.check_market()
        if not open_:
            if "--once" in sys.argv:
                log(f"market closed ({et.strftime('%H:%M ET')})"); return 0
            time.sleep(FAST_PASS_SEC); continue

        try:
            if roll_day(led, et.strftime("%Y-%m-%d")):
                log("new session — settling T+1 proceeds, reconciling with broker")
                led["needs_reconcile"] = True
            # Held as a ledger flag, not a local variable: if the snapshot is bad
            # we must retry next pass, and roll_day only fires once per day.
            if led.get("needs_reconcile"):
                if adopt_truth(led, reconcile()):
                    led["needs_reconcile"] = False
                elif DRY:
                    # A dry ledger is simulated, so it can never match the real
                    # book. Blocking here would deadlock every validation run at
                    # session open, so the simulation just owns the ledger.
                    led["needs_reconcile"] = False
                else:
                    log("session-open snapshot not trustworthy, trading nothing "
                        "this pass and retrying next minute")
                    save_ledger(led)
                    if "--once" in sys.argv:
                        return 0
                    time.sleep(FAST_PASS_SEC)
                    continue
            full = (time.time() - last_full) >= FULL_CYCLE_SEC
            if full and sync_code():
                save_ledger(led)          # ledger is the source of truth across restarts
                # Do NOT execv on Windows: it spawns rather than replaces, which is
                # how three daemons ended up running at once. Exit non-zero and let
                # Task Scheduler restart exactly one managed instance. The singleton
                # mutex backs this up if a restart ever races.
                if sys.platform == "win32":
                    log("updated code pulled — exiting 42 for a clean task restart")
                    return 42
                log("restarting into updated code")
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception as e:
                    log(f"execv failed ({e}) - exiting 42 so the scheduled task restarts us")
                    return 42
            res = cycle(led, fast=not full)
            if full:
                last_full = time.time()
                s = res.get("snapshot") or {}
                log(f"FULL | EQ ${s.get('equity', 0):.2f} | index ${s.get('index', 0):.2f} "
                    f"| hold ${s.get('hold', 0):.2f} | trade ${s.get('trade', 0):.2f} "
                    f"| {len(res['orders'])} order(s)")
        except Exception as e:
            log(f"cycle error (loop continues): {e}")

        if "--once" in sys.argv:
            return 0
        time.sleep(FAST_PASS_SEC)


if __name__ == "__main__":
    sys.exit(main())
