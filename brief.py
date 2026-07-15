"""
Morning + intraday research brief for the Alpaca bot.

Calls Claude (Anthropic API) with market news + your live portfolio + the running
journal, and produces a STRUCTURED daily plan (daily_plan.json) that the trading
bot follows, plus a running journal (journal.md) for day-to-day memory.

Runs in GitHub Actions (cloud — no PC). Emails the brief. Fails safe: if the LLM
call errors, it writes a NEUTRAL plan (risk 1.0, no avoid-list) so trading simply
proceeds at default risk — the brief can never break or over-restrict the bot.

The plan only TUNES WITHIN the bot's hard risk rails: risk_scale is clamped to
[0,1] (can scale down, never up), and avoid/favor adjust the symbol set. The loss
cap, position cap, and PDT limits in alpaca_bot.py remain absolute.
"""

import os, json, re, html
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import quote_plus

import anthropic
import alpaca_bot as bot   # reuse: config, send_email, alpaca_get, yf_vix, MODE

MODEL = "claude-opus-4-8"

# Schema the model must fill. additionalProperties:false keeps output clean.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "regime":        {"type": "string", "enum": ["risk-on", "neutral", "risk-off"]},
        "risk_scale":    {"type": "number"},               # 0.25–1.0; clamped in code
        "avoid_symbols": {"type": "array", "items": {"type": "string"}},
        "favor_symbols": {"type": "array", "items": {"type": "string"}},
        "notes":         {"type": "string"},               # short rationale
        "journal_entry": {"type": "string"},               # 1–2 sentences for memory
    },
    "required": ["regime", "risk_scale", "avoid_symbols", "favor_symbols", "notes", "journal_entry"],
    "additionalProperties": False,
}

SYSTEM = """You are the risk-management analyst for an automated US-equities day-trading bot.
A few times per trading day you produce a short, structured PLAN the bot will follow.

Reality check you must respect:
- Public news is priced in within seconds. You are NOT here to chase headlines for alpha.
- Your real job is RISK POSTURE: avoid landmines (a holding reporting earnings today,
  a macro event like a Fed decision/CPI, a name in free-fall on bad news) and dial
  overall risk up or down to match conditions.
- Your plan only tunes WITHIN hard limits the bot enforces itself (loss cap, position
  cap, PDT). You CANNOT force trades; you set posture and an avoid/favor list.

Fill the schema:
- regime: "risk-off" when conditions are dangerous (high VIX, ugly tape, major event
  today), "risk-on" when calm and constructive, else "neutral".
- risk_scale: 1.0 = full normal sizing; lower (down to ~0.25) to shrink new buys on
  elevated risk. Never above 1.0. Be conservative when uncertain.
- avoid_symbols: tickers the bot should NOT buy today (e.g. reporting earnings within
  ~2 days, event risk, sharp adverse news). Use the actual position/universe tickers.
- favor_symbols: a few tickers that look constructive and worth considering (optional).
- notes: 1–2 sentences explaining the posture (what you saw, why this risk level).
- journal_entry: 1–2 sentences for the running journal — what changed since last check,
  what you're watching. This is your memory; write it for your future self.

Be concise and decisive. When in doubt, lean defensive (lower risk_scale)."""


def fetch_headlines(query, n=5):
    """Free market headlines via Google News RSS. Fail-safe — returns [] on any error."""
    url = (f"https://news.google.com/rss/search?q={quote_plus(query)}"
           "&hl=en-US&gl=US&ceid=US:en")
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        xml = urlopen(req, timeout=12).read().decode("utf-8", "ignore")
        titles = re.findall(r"<title>(.*?)</title>", xml, re.S)
        return [html.unescape(t).strip() for t in titles[1:n + 1]]   # skip feed title
    except Exception:
        return []


def gather_context(et, mode):
    lines = [f"=== {mode} BRIEF — {et.strftime('%A %Y-%m-%d %H:%M ET')} ({bot.MODE} account) ==="]

    # Account + positions
    try:
        a = bot.alpaca_get("/v2/account")
        eq = float(a.get("equity", 0)); cash = float(a.get("cash", 0))
        leq = float(a.get("last_equity", eq) or eq)
        lines.append(f"Equity ${eq:,.0f} | Cash ${cash:,.0f} | Day P&L ${eq-leq:+,.0f}")
    except Exception:
        lines.append("Account: (unavailable)")

    held = []
    try:
        for p in bot.alpaca_get("/v2/positions"):
            held.append(p["symbol"])
            lines.append(f"  HOLD {p['symbol']:6} ${float(p.get('market_value',0)):>9,.0f}  "
                         f"{float(p.get('unrealized_plpc',0))*100:+.1f}%")
    except Exception:
        pass
    if not held:
        lines.append("  (no open positions)")

    # VIX
    try:
        lines.append(f"VIX: {bot.yf_vix():.1f}")
    except Exception:
        pass

    # News
    lines.append("\n-- MARKET NEWS --")
    for h in fetch_headlines("US stock market today", 6):
        lines.append(f"  • {h}")
    lines.append("-- MACRO/EVENTS --")
    for h in fetch_headlines("Federal Reserve OR CPI OR jobs report OR earnings this week", 4):
        lines.append(f"  • {h}")
    for sym in held[:6]:
        hs = fetch_headlines(f"{sym} stock", 2)
        if hs:
            lines.append(f"-- {sym} --")
            lines += [f"  • {h}" for h in hs]
        try:
            if bot.earnings_within(sym, 7):
                lines.append(f"  NOTE: {sym} reports earnings within ~7 days — weigh for the avoid list")
        except Exception:
            pass

    # Memory: prior plan + recent journal
    if os.path.exists("daily_plan.json"):
        try:
            lines.append("\n-- PRIOR PLAN --\n" + json.dumps(json.load(open("daily_plan.json")))[:600])
        except Exception:
            pass
    if os.path.exists("journal.md"):
        try:
            j = open("journal.md", encoding="utf-8").read()
            lines.append("\n-- RECENT JOURNAL (memory) --\n" + j[-2500:])
        except Exception:
            pass

    lines.append("\nProduce today's plan now. Be conservative; favor risk management over reach.")
    return "\n".join(lines)


def get_plan(user_text):
    """Call Claude for a structured plan. Returns (plan_dict, ok_bool)."""
    client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), True


def main():
    et   = datetime.now(timezone.utc) - timedelta(hours=4)
    mode = "MORNING" if (et.hour, et.minute) < (9, 45) else "INTRADAY"
    today = et.strftime("%Y-%m-%d")

    try:
        plan, ok = get_plan(gather_context(et, mode))
    except Exception as e:
        print(f"[brief failed: {e}] — writing NEUTRAL plan (bot trades at default risk).")
        plan = {"regime": "neutral", "risk_scale": 1.0, "avoid_symbols": [],
                "favor_symbols": [], "notes": f"LLM brief unavailable ({e}); default risk.",
                "journal_entry": "Brief failed; defaults in effect."}
        ok = False

    # Clamp risk to [0,1] (can only scale DOWN — never amplify beyond the bot's rails)
    try:
        plan["risk_scale"] = max(0.0, min(1.0, float(plan.get("risk_scale", 1.0))))
    except Exception:
        plan["risk_scale"] = 1.0

    plan["date"] = today
    plan["mode"] = mode
    plan["updated_at"] = et.strftime("%Y-%m-%dT%H:%M ET")

    # Write the plan the bot reads
    with open("daily_plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    # Append to the journal (memory)
    entry = (f"\n### {plan['updated_at']} — {mode} ({'ok' if ok else 'FALLBACK'})\n"
             f"- regime: {plan['regime']} | risk_scale: {plan['risk_scale']}\n"
             f"- avoid: {plan['avoid_symbols']} | favor: {plan['favor_symbols']}\n"
             f"- notes: {plan['notes']}\n"
             f"- journal: {plan['journal_entry']}\n")
    with open("journal.md", "a", encoding="utf-8") as f:
        f.write(entry)

    # Email the brief
    body = (f"{mode} research brief ({bot.MODE}) — {plan['updated_at']}\n\n"
            f"Regime: {plan['regime']}\nRisk scale: {plan['risk_scale']} "
            f"(1.0 = full size; lower = smaller new buys)\n"
            f"Avoid: {plan['avoid_symbols']}\nFavor: {plan['favor_symbols']}\n\n"
            f"Notes: {plan['notes']}\n\nJournal: {plan['journal_entry']}\n")
    print(body)
    bot.send_email(f"{mode} brief ({bot.MODE}) - {plan['regime']} / risk {plan['risk_scale']}", body)


if __name__ == "__main__":
    main()
