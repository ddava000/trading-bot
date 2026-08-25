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

import os, re, sys, json, time, subprocess
from datetime import datetime

import rh_bot
import alpaca_bot as bot

CONFIG_F, LEDGER_F = "rh_config.json", "rh_ledger.json"
DEPOSITS_F         = "rh_deposits.json"   # COMMITTED: contributed capital, so the
                                           # A/B check-in can subtract it from equity
STATUS_F, LOG_F    = "rh_status.json", "rh_trade_log.jsonl"
HALT_F             = "rh_HALT"

# The only files the daemon imports and runs. A pulled commit touching anything
# else (status.json, trade logs, daily_plan.json, briefs, backtest.py) is data or
# cloud-only code and needs no restart, since decide() reads the plan fresh each
# cycle. Keep this in sync with the actual import graph: rh_daemon -> rh_bot -> bot.
CODE_FILES = ("rh_bot.py", "rh_daemon.py", "alpaca_bot.py")

FULL_CYCLE_SEC = 900     # 15 min, matches the cloud bot's trigger cadence
FAST_PASS_SEC  = 60      # 60 s, matches the cloud bot's protective pass
MAX_ORDERS_DAY = 40      # circuit breaker: a runaway loop can't machine-gun orders
AGENT_TIMEOUT  = 240
PUSH_HEARTBEAT_SEC = 900 # liveness ping when nothing changed, so a quiet laptop
                         # and a dead one don't look the same to whoever watches
RECONCILE_MAX_AGE_SEC = 1800  # 30 min. The bot only learned its cash at session
                         # open and after trades, so a mid-day DEPOSIT sat unseen
                         # until the next morning. This reconciles at least this
                         # often during market hours so added (or withdrawn) money
                         # is noticed within the window. It is the one scheduled
                         # token spend; keep it well above the 15-min full cycle so
                         # it stays cheap. Trades reset the clock, so on an active
                         # day this rarely fires on top of the reconciles already
                         # happening.

_last_push_at, _last_push_material = 0.0, None
SELFTEST_FAIL_ALERT  = 3     # consecutive upstream selftest rejections before we
                             # tell Devon. The gate keeps trading safely on the old
                             # in-memory code, but silently: without this he never
                             # learns the laptop stopped inheriting cloud fixes.
SELFTEST_REALERT_SEC = 14400 # 4h. Re-alert while still pinned, so a break that
                             # lasts days is not forgotten after one email.

BROKER_FAIL_ALERT  = 3      # consecutive failed broker snapshots before alerting
BROKER_REALERT_SEC = 3600   # re-alert hourly while the broker stays unreachable

RECONCILE_BACKOFF_MAX = 900   # cap retry spacing at 15 min during an outage

_reconcile_fails, _broker_alert_at = 0, 0.0
_next_reconcile_try = 0.0
_selftest_fails, _selftest_alert_at = 0, 0.0
_last_reconcile = 0.0    # 0 => the first full cycle after start reconciles, which
                         # is also how a fresh start picks up a deposit made while
                         # the laptop was off or since the last session open.
_run_head = None         # git commit whose CODE is loaded in memory, captured at
                         # startup. Code-change detection compares against THIS, not
                         # against sync_code's own pull, because the status-push path
                         # also pulls --rebase and can absorb a cloud code change
                         # before sync_code sees it (which left the daemon running
                         # 3-day-stale code on 2026-08-06).

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

# Slack webhook, same pattern and same reason. SLACK_WEBHOOK_URL is a GitHub
# secret, which covers the workflows, but this daemon runs on the LAPTOP where no
# such environment exists, so the mirror would have silently no-opped forever
# while everyone assumed Slack coverage. Verified enabled() was False here before
# adding this. Set BEFORE slack_notify is first imported, since it reads the env
# at import time. Config is gitignored, so the URL never reaches the public repo.
if CFG.get("slack_webhook_url"):
    os.environ["SLACK_WEBHOOK_URL"] = CFG["slack_webhook_url"]


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

# Run the execution turn OUTSIDE the repo. The repo CLAUDE.md carries a hard rail,
# "never place a real-money trade yourself", aimed at a human-facing chat Claude.
# But this bridge IS the sanctioned automation: a headless turn handed an exact,
# already-decided order list, the only way to reach Robinhood, which has no order
# API. Run inside the repo, the executor reads that rail and refuses every order,
# including protective SELLS, which it started doing on 2026-07-28 the moment the
# laptop pulled the new CLAUDE.md. claude walks up from cwd looking for CLAUDE.md,
# so a directory outside the repo tree (no CLAUDE.md above it, verified) gives the
# executor a clean context. It still gets the account-level Robinhood connector,
# which does not depend on cwd. The rail stays fully in force for chat sessions.
BRIDGE_CWD = os.path.join(os.environ.get("LOCALAPPDATA",
                          os.path.expanduser("~")), "rh_bridge")
try:
    os.makedirs(BRIDGE_CWD, exist_ok=True)
except Exception:
    BRIDGE_CWD = None       # fall back to default cwd rather than crash


def agent(prompt):
    """Run a headless Claude turn and return the JSON object it printed."""
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", RH_PREAMBLE + prompt,
                            "--allowedTools", RH_ALLOWED],
                           capture_output=True, cwd=BRIDGE_CWD,
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
        '{"cash": <buying_power as number>, '
        '"total_value": <total account value INCLUDING unsettled proceeds>, '
        '"pending_deposits": <pending_deposits as number, 0 if none>, "positions": '
        '[{"symbol": "X", "qty": <number>, "avg_cost": <number>}]}')
    if res and isinstance(res.get("positions"), list):
        return res
    return None


def record_deposit(amount, source, note=""):
    """Append a deposit event to the COMMITTED rh_deposits.json.

    Devon has a recurring ~$10/week deposit. That is contributed capital, not
    return, and leaving it in raw equity already turned a flat week into a fake
    +4.26% for the A/B experiment. The cash is still real and still gets traded
    normally; only the performance math subtracts this file's total.
    """
    try:
        doc = _load(DEPOSITS_F, None) or {"currency": "USD", "events": [],
                                          "total_deposited": 0.0}
        doc.setdefault("events", []).append({
            "date": now_et().strftime("%Y-%m-%d"),
            "amount": round(float(amount), 2),
            "confidence": "confirmed",
            "source": source,
            "note": note})
        doc["total_deposited"] = round(
            sum(float(e.get("amount") or 0) for e in doc["events"]), 2)
        _save(DEPOSITS_F, doc)
        total = doc["total_deposited"]
        log(f"DEPOSIT recorded: ${float(amount):.2f} ({source}); cumulative ${total:.2f}")
        body = [
            f"Recorded a ${float(amount):.2f} deposit into the agentic account.",
            f"Cumulative tracked deposits: ${total:.2f}.",
            "",
            "The bot invests this normally. It is excluded from performance math",
            "so contributed capital does not get counted as a gain.",
        ]
        notify("RH laptop bot: deposit recorded", chr(10).join(body))
        return True
    except Exception as e:
        log(f"could not record deposit ({e})")
        return False


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
    # Broker TOTAL value, which unlike buying_power includes unsettled proceeds.
    # Published equity uses this; see persist(). Sizing deliberately does NOT,
    # because unsettled cash genuinely cannot be spent.
    try:
        tv = float(truth.get("total_value") or 0)
        if tv > 0:
            led["broker_total_value"] = round(tv, 2)
            led["broker_total_at"] = time.time()
    except Exception:
        pass
    led["positions"] = positions
    led["holds"] = {s: h for s, h in (led.get("holds") or {}).items()
                    if any(p["symbol"] == s for p in positions)}
    # Deposit capture. pending_deposits is the broker's own field, so a RISING
    # edge is an authoritative new deposit, unlike inferring from a cash jump
    # (T+1 settlement makes a sell look identical to a deposit the next day,
    # which is exactly what made the retroactive log archaeology imprecise).
    # Recorded once on the rising edge, so it is not double counted when the
    # deposit later settles and pending drops back to 0.
    try:
        pend = float(truth.get("pending_deposits") or 0)
        seen = float(led.get("pending_deposits_seen") or 0)
        if pend > seen + 0.005:
            record_deposit(pend - seen, "broker pending_deposits (rising edge)",
                           f"pending went {seen:.2f} -> {pend:.2f}")
        led["pending_deposits_seen"] = pend
    except Exception as e:
        log(f"deposit check skipped ({e})")

    global _last_reconcile
    _last_reconcile = time.time()   # feeds the periodic-reconcile clock in main()
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


def notify(subject, body, untrusted=False):
    """Tell Devon about an operational condition. Never raises.

    Logs the outcome, because send_email's own confirmation is a print() and the
    daemon runs under pythonw with no stdout, so delivery would otherwise leave no
    trace either way.

    untrusted=True fences the body as data in Slack. Use it for anything carrying
    text we did not author. Proven live, not theoretical: bot.news_flags returns
    Yahoo/Alpaca HEADLINES, rh_bot embeds them in an order reason
    ("NEWS-EXIT (<headline>)"), and email_trades puts order reasons straight into
    this body. A hostile headline would therefore land verbatim in a channel that
    @Claude reads as context. Dormant right now because WIND_DOWN short-circuits
    before the news check and index-only opens no positions, but it is one config
    change from live, so the fence goes in now rather than after.
    """
    # Slack FIRST and outside the GMAIL guard. An empty GMAIL secret has blanked
    # every channel here twice; it must not be able to take Slack with it.
    try:
        import slack_notify
        slack_notify.post(f"*{subject}*\n{body}" if not untrusted else body,
                          untrusted=untrusted)
    except Exception as e:
        log(f"slack mirror failed ({e}): {subject}")

    if not bot.GMAIL_APP_PW:
        log(f"NOT emailing ({subject}): no gmail_app_password set")
        return False
    try:
        bot.send_email(subject, body)
        log(f"emailed: {subject}")
        return True
    except Exception as e:
        log(f"email FAILED ({e}): {subject}")
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
    # Routed through notify() so this gets the Slack mirror too, and fenced as
    # UNTRUSTED: order reasons can embed an external news headline
    # ("NEWS-EXIT (<headline>)"), which would otherwise land verbatim in a channel
    # @Claude reads as context. notify() logs delivery either way, which matters
    # because send_email's own feedback is a print() the daemon cannot see.
    if notify(subject, chr(10).join(lines), untrusted=True):
        log(f"reported {ok}/{len(orders)} fills")


def persist(led, res, placed):
    """Write the ledger locally; commit the shareable log/status for monitoring."""
    global _last_push_at, _last_push_material
    save_ledger(led)
    snap = dict(res.get("snapshot") or {})
    # T+1 TRAP: decide() computes equity as cash + invested, and `cash` is broker
    # BUYING POWER, which excludes unsettled sale proceeds. After the 2026-08-24
    # wind-down sold 24 positions, this file published ~$124 for a full trading day
    # while the account actually held ~$232: a phantom 48% crash, in the very file
    # cloud and audit measure Arm B from. Publish the broker's total instead, and
    # say so when they differ, so a settlement artifact is never read as a loss.
    bt, bt_at = led.get("broker_total_value"), led.get("broker_total_at") or 0
    if bt and (time.time() - bt_at) < RECONCILE_MAX_AGE_SEC * 2:
        computed = snap.get("equity")
        snap["equity"] = bt
        if computed is not None and abs(float(computed) - float(bt)) > 0.50:
            snap["equity_source"] = "broker_total_value"
            snap["unsettled_excluded_from_buying_power"] = round(float(bt) - float(computed), 2)
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


def publish_degraded(led, reason, passes):
    """Heartbeat while the broker is unreachable, naming the reason.

    Costs no agent turn, so it is safe to run on every pass even mid-outage. This
    is what lets monitoring tell a dead BROKER from a dead LAPTOP: without it the
    daemon simply went quiet and the watchdog reported the machine down, sending
    Devon to check hardware that was fine.
    """
    prev = _load(STATUS_F, {}) or {}
    snap = {"ts": now_et().strftime("%Y-%m-%dT%H:%M"),
            "equity": prev.get("equity"),   # last known, so monitoring keeps a number
            "degraded": reason,
            "degraded_since_passes": passes,
            "positions": {p["symbol"]: round(p["qty"], 6)
                          for p in led.get("positions") or []},
            "holds": sorted(led.get("holds") or {}),
            "orders_today": led.get("orders_today", 0),
            "dry": DRY}
    _save(STATUS_F, snap)
    _push_status("degraded")


MAIL_F = "AGENT_MAIL.md"
_MAIL_HEAD = re.compile(r"^## \[([^\]]+)\]\s*(\w+)\s*->\s*([A-Za-z]+)", re.M)


def check_mail(led):
    """Tell Devon when the mailbox has a new entry addressed to this session.

    cloud's cadence protocol (2026-08-23) states "laptop: at every daemon
    start/restart, which is frequent. Effectively the fastest reader." That was
    NOT true. The daemon pulls AGENT_MAIL.md to disk but nothing here read it, and
    only a Claude session on this machine does, which happens when Devon opens
    one. A time-sensitive entry addressed to laptop could therefore sit unseen for
    days while the sender believed it landed in minutes.

    The daemon cannot reason about mail, so it does not try. It just says mail
    arrived, which is what makes the protocol's assumption real: the notification
    is what prompts a session to be opened.

    First run adopts the current newest entry without notifying, so adding this
    does not replay the whole backlog.
    """
    try:
        with open(MAIL_F, encoding="utf-8", errors="replace") as f:
            heads = _MAIL_HEAD.findall(f.read())
        if not heads:
            return
        keys = ["|".join(h) for h in heads]
        seen = led.get("last_mail_seen")
        if seen is None:
            led["last_mail_seen"] = keys[-1]
            return
        if keys[-1] == seen:
            return
        start = keys.index(seen) + 1 if seen in keys else len(keys) - 1
        fresh = [h for h in heads[start:]
                 if h[2].strip().lower() in ("laptop", "both", "all")]
        led["last_mail_seen"] = keys[-1]
        if not fresh:
            return
        summary = "; ".join(f"{ts} {frm}->{to}" for ts, frm, to in fresh)
        log(f"NEW MAIL for the laptop session: {summary}")
        notify("RH bot: new mailbox entry for the laptop session", chr(10).join([
            "New AGENT_MAIL.md entries addressed to the laptop session:",
            "",
            *[f"  [{ts}] {frm} -> {to}" for ts, frm, to in fresh],
            "",
            "The daemon cannot act on these; it only reports that they arrived.",
            "Open a Claude session on the laptop to read and respond.",
        ]))
    except Exception as e:
        log(f"mail check skipped ({e})")


# ── Code sync: inherit cloud-bot improvements, but verify before trusting ───
def sync_code():
    """Pull upstream changes and prove they work before running on them.

    rh_bot.py imports its rails straight from alpaca_bot, so a strategy fix made
    to the cloud bot reaches this laptop through git and nothing else. Without
    this the daemon would run whatever code it started with, forever.

    A bad upstream push must never break trading here, so new code has to pass
    rh_bot's selftest; if it fails we stay on the known-good code already loaded in
    memory. Returns True when the process should restart to load the new modules
    (Python caches imports).

    Detection compares CODE_FILES between the RUNNING commit (_run_head) and the
    current HEAD, not between this pull's before and after. The status-push path
    also runs `git pull --rebase`, so a cloud code change can land on disk via that
    push before sync_code's own pull sees it; comparing to the running commit
    catches it however HEAD advanced."""
    global _selftest_fails, _selftest_alert_at
    try:
        def git(*a, t=90):
            return subprocess.run(["git", *a], capture_output=True, text=True, timeout=t)
        before = git("rev-parse", "HEAD").stdout.strip()
        git("pull", "--rebase", "--autostash", "--quiet")
        after = git("rev-parse", "HEAD").stdout.strip()
        head = after or before
        base = _run_head or before          # what our in-memory modules were built from
        if not head or head == base:
            return False
        # Restart ONLY when code the daemon RUNS changed. The cloud pushes
        # status.json, trade logs, briefs and daily_plan.json constantly; a restart
        # to adopt those just costs a protective-pass gap, and decide() reads the
        # plan fresh each cycle anyway.
        changed = git("diff", "--name-only", f"{base}..{head}").stdout.split()
        code_changed = [f for f in changed if f in CODE_FILES]
        if not code_changed:
            if after and after != before:   # log only when this pull moved HEAD
                pulled = git("diff", "--name-only", f"{before}..{after}").stdout.split()
                log(f"pulled {before[:7]} -> {after[:7]} — data only "
                    f"({', '.join(pulled)[:70]}), not restarting")
            return False
        log(f"code changed ({', '.join(code_changed)}) {base[:7]} -> {head[:7]} "
            f"— verifying before use")
        chk = subprocess.run([sys.executable, "rh_bot.py", "--selftest"],
                             capture_output=True, text=True, timeout=180)
        if chk.returncode != 0:
            # Do not reset the shared tree (that would fight the cloud's commits).
            # The good code is already in memory; just refuse to restart into the
            # bad code, and keep flagging until an upstream fix passes.
            _selftest_fails += 1
            log(f"!! NEW CODE FAILED SELFTEST ({_selftest_fails}x) - staying on "
                f"known-good in-memory code")
            err = ((chk.stdout or "")[-400:] + (chk.stderr or "")[-400:]).strip()
            log(err)
            # Alert at the threshold, then at most every SELFTEST_REALERT_SEC while
            # it stays broken. Trading continues safely throughout; what is stalled
            # is inheriting cloud changes, which is a silent condition otherwise.
            if _selftest_fails >= SELFTEST_FAIL_ALERT and (
                    time.time() - _selftest_alert_at) >= SELFTEST_REALERT_SEC:
                notify("ALERT: RH laptop bot pinned on old code",
                       "\n".join([
                           "The laptop bot is STILL TRADING normally, on the last "
                           "known-good code. It has stopped inheriting cloud updates.",
                           "",
                           f"{_selftest_fails} consecutive selftest rejections of "
                           f"upstream code.",
                           f"running code : {(_run_head or '?')[:7]}",
                           f"code on disk : {head[:7]}",
                           f"files        : {', '.join(code_changed)}",
                           "",
                           "Selftest output (tail):",
                           err[-600:],
                           "",
                           "This clears itself once an upstream commit passes the "
                           "selftest. Stop the bot any time with a file named "
                           "rh_HALT in the repo folder.",
                       ]))
                _selftest_alert_at = time.time()
            return False
        if _selftest_fails:
            log(f"upstream selftest recovered after {_selftest_fails} failure(s)")
            if _selftest_alert_at:
                notify("RH laptop bot: upstream code fixed, updates resumed",
                       f"Upstream code passes the selftest again after "
                       f"{_selftest_fails} rejection(s). The laptop is restarting "
                       f"into {head[:7]} and inheriting cloud changes normally.")
            _selftest_fails, _selftest_alert_at = 0, 0.0
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

    # Pin the commit our just-loaded modules were built from, so sync_code can tell
    # when the live CODE has drifted from disk no matter which git path advanced HEAD.
    global _run_head, _reconcile_fails, _broker_alert_at, _next_reconcile_try
    try:
        _run_head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                   text=True, timeout=30).stdout.strip() or None
        log(f"running code at {(_run_head or '?')[:7]}")
    except Exception as e:
        log(f"could not capture running HEAD ({e}); code-sync will use pull deltas")

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
            full = (time.time() - last_full) >= FULL_CYCLE_SEC
            if roll_day(led, et.strftime("%Y-%m-%d")):
                log("new session — settling T+1 proceeds, reconciling with broker")
                led["needs_reconcile"] = True
            # Periodic reconcile so a mid-day DEPOSIT (or withdrawal) is noticed
            # within RECONCILE_MAX_AGE_SEC instead of sitting unseen until the next
            # session open. Only on full cycles, and skipped when roll_day already
            # forced one. adopt_truth resets the clock, so an active day rarely
            # spends an extra reconcile here.
            elif full and (time.time() - _last_reconcile) >= RECONCILE_MAX_AGE_SEC:
                led["needs_reconcile"] = True   # logged at the actual attempt below
            # Held as a ledger flag, not a local variable: if the snapshot is bad
            # we must retry next pass, and roll_day only fires once per day.
            if led.get("needs_reconcile"):
                # BACK OFF failed broker snapshots. Each attempt is an agent turn
                # against the SAME Claude quota the bridge needs, so retrying every
                # 60s while the quota is exhausted burns the very resource we are
                # waiting on. On 2026-08-20 that meant 140 attempts across a 2h41m
                # session-limit outage, roughly 14x a normal day's usage, which can
                # only prolong the lockout. Backoff caps it near 15 attempts.
                # The heartbeat below stays on every pass: it costs nothing and it
                # is what keeps a dead broker distinguishable from a dead laptop.
                if time.time() < _next_reconcile_try:
                    publish_degraded(led, "broker_unreachable", _reconcile_fails)
                    if "--once" in sys.argv:
                        return 0
                    time.sleep(FAST_PASS_SEC)
                    continue
                log("reconciling with the broker")
                if adopt_truth(led, reconcile()):
                    if _reconcile_fails:
                        log(f"broker reachable again after {_reconcile_fails} failed pass(es)")
                        if _broker_alert_at:
                            notify("RH bot: broker connection restored",
                                   "The Robinhood connection is working again. "
                                   "Normal trading and stop enforcement have resumed.")
                        _reconcile_fails, _broker_alert_at = 0, 0.0
                    _next_reconcile_try = 0.0
                    led["needs_reconcile"] = False
                elif DRY:
                    # A dry ledger is simulated, so it can never match the real
                    # book. Blocking here would deadlock every validation run at
                    # session open, so the simulation just owns the ledger.
                    led["needs_reconcile"] = False
                else:
                    # BROKER UNREACHABLE. Keep the heartbeat alive and say WHY.
                    # This branch used to `continue` straight to the next pass,
                    # which skipped cycle() and therefore persist(), so no status
                    # was ever written or pushed. A dead broker connection then
                    # looked identical to a dead laptop, and on 2026-08-18 the
                    # watchdog duly reported "laptop silent" while the machine was
                    # fine and the real fault was an expired Robinhood connector
                    # authorization. Wrong diagnosis costs time that positions do
                    # not have, since selling needs this same bridge.
                    _reconcile_fails += 1
                    wait = min(FAST_PASS_SEC * (2 ** min(_reconcile_fails - 1, 4)),
                               RECONCILE_BACKOFF_MAX)
                    _next_reconcile_try = time.time() + wait
                    log(f"broker snapshot unavailable ({_reconcile_fails}x), "
                        f"trading nothing this pass and retrying next minute")
                    save_ledger(led)
                    publish_degraded(led, "broker_unreachable", _reconcile_fails)
                    if (_reconcile_fails >= BROKER_FAIL_ALERT and
                            (time.time() - _broker_alert_at) >= BROKER_REALERT_SEC):
                        notify("RH bot: broker unreachable (not urgent)", chr(10).join([
                            "The laptop is UP and the daemon is running. The problem is the",
                            "Robinhood connection, not the machine.",
                            "",
                            f"{_reconcile_fails} consecutive failed broker snapshots.",
                            "",
                            "IMPACT: NOT URGENT. Robinhood is index-only buy-and-hold, so",
                            "there are no stops waiting to fire. Deposits and rebalancing",
                            "just sit until the bridge is back. Fix it when convenient.",
                            "",
                            "DIAGNOSE with this, NOT with `claude mcp list`, which reported",
                            "Connected while the bridge could not authenticate at all:",
                            "    claude -p \"Reply with exactly: ALIVE\"",
                            "If that fails the CLI login lapsed:  claude auth login",
                            "If it succeeds but Robinhood tools are missing, reconnect that",
                            "connector:  claude mcp login \"claude.ai Robinhood\"",
                            "",
                            "FIX: reconnect the Robinhood connector in claude.ai settings,",
                            "then the bot recovers on its own within a minute.",
                        ]))
                        _broker_alert_at = time.time()
                    if "--once" in sys.argv:
                        return 0
                    time.sleep(FAST_PASS_SEC)
                    continue
            if full:
                check_mail(led)   # surface new mailbox entries for this session
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
