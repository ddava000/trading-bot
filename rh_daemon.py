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

DRY = "--dry" in sys.argv


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _load(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    with open(path, "w") as f: json.dump(obj, f, indent=1, sort_keys=True)


CFG = _load(CONFIG_F, {})
ACCOUNT    = CFG.get("account", "")
CLAUDE_BIN = CFG.get("claude", "claude")


# ── Execution bridge: one short headless agent turn, MCP tools only ──────────
def agent(prompt):
    """Run a headless Claude turn and return the JSON object it printed."""
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", prompt], capture_output=True,
                           text=True, timeout=AGENT_TIMEOUT)
        out = (r.stdout or "").strip()
        i, j = out.find("{"), out.rfind("}")
        if i >= 0 and j > i:
            return json.loads(out[i:j + 1])
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
    today = datetime.now().strftime("%Y-%m-%d")
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
    led["orders_today"] = led.get("orders_today", 0) + len(orders)


def track_peaks(led, prices):
    """Hold-sleeve peak ratchet, same as the cloud bot's holds.json."""
    for sym, h in led.get("holds", {}).items():
        px = prices.get(sym)
        if px and px > float(h.get("peak") or 0):
            h["peak"] = round(px, 4)


def persist(led, res, placed):
    """Write the ledger locally; commit the shareable log/status for monitoring."""
    _save(LEDGER_F, led)
    snap = dict(res.get("snapshot") or {})
    snap.update({"ts": datetime.now().strftime("%Y-%m-%dT%H:%M"),
                 "positions": {p["symbol"]: round(p["qty"], 6) for p in led["positions"]},
                 "holds": sorted(led.get("holds") or {}),
                 "orders_today": led.get("orders_today", 0), "dry": DRY})
    _save(STATUS_F, snap)
    if placed or res.get("orders"):
        with open(LOG_F, "a") as f:
            for o in res["orders"]:
                st = next((p.get("status") for p in placed
                           if p.get("symbol") == o["symbol"] and p.get("action") == o["action"]),
                          "dry" if DRY else "unknown")
                f.write(json.dumps({**o, "ts": datetime.now().strftime("%Y-%m-%dT%H:%M"),
                                    "status": st, "venue": "robinhood"}) + "\n")
    try:
        subprocess.run(["git", "add", STATUS_F, LOG_F], capture_output=True, timeout=30)
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          capture_output=True, timeout=30).returncode != 0:
            subprocess.run(["git", "commit", "-m",
                            f"rh bot {datetime.now().strftime('%Y-%m-%dT%H:%M')}"],
                           capture_output=True, timeout=60)
            if subprocess.run(["git", "push"], capture_output=True, timeout=90).returncode != 0:
                subprocess.run(["git", "pull", "--rebase", "--autostash"],
                               capture_output=True, timeout=90)
                subprocess.run(["git", "push"], capture_output=True, timeout=90)
    except Exception as e:
        log(f"git persist skipped: {e}")


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
            after = reconcile()                    # truth up after every trade
            if after:
                led["cash"] = float(after.get("cash") or led["cash"])
                led["positions"] = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                                     "avg_cost": float(p.get("avg_cost") or 0)}
                                    for p in after["positions"] if float(p["qty"]) > 0]
                led["holds"] = {s: h for s, h in led.get("holds", {}).items()
                                if any(p["symbol"] == s for p in led["positions"])}
    for n in res.get("notes", []):
        log(f"  note: {n}")
    persist(led, res, placed)
    return res


def main():
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
            log("rh_HALT present — trading paused"); time.sleep(FAST_PASS_SEC); continue
        open_, et = bot.check_market()
        if not open_:
            if "--once" in sys.argv:
                log(f"market closed ({et.strftime('%H:%M ET')})"); return 0
            time.sleep(FAST_PASS_SEC); continue

        try:
            if roll_day(led, et.strftime("%Y-%m-%d")):
                log("new session — settling T+1 proceeds, reconciling with broker")
                truth = reconcile()
                if truth:
                    led["cash"] = float(truth.get("cash") or led["cash"])
                    led["positions"] = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                                         "avg_cost": float(p.get("avg_cost") or 0)}
                                        for p in truth["positions"] if float(p["qty"]) > 0]
            full = (time.time() - last_full) >= FULL_CYCLE_SEC
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
