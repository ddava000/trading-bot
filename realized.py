#!/usr/bin/env python3
"""Realized gain and loss from broker fills. Pure functions, no network, ASCII only.

WHY (Devon, 2026-09-29): he wants a running total of realized gains versus realized
losses in every report, to track tax implications.

THIS IS NOT THE SAME NUMBER AS pnl.py. pnl.py reports EQUITY change, which includes
unrealized paper losses that are not taxable. Only a position actually SOLD creates a
taxable gain or loss, so the realized total is usually quite different, and on a
portfolio that mostly buys and holds it can be far smaller.

NOT TAX ADVICE, and not a substitute for the broker's 1099-B, which is authoritative
and is what Devon's tax preparer should use. Expect cents-level differences from
rounding and from the broker's own lot selection. The purpose is a visible running
total, so tax time holds fewer surprises.

METHOD, stated so a cold reader can challenge it:
  1. FIFO lot matching, the usual default. The 1099-B can differ if another lot
     method was selected.
  2. Trade date is the New York calendar date of the fill.
  3. A lot is LONG term only if sold MORE than one year after it was bought.
  4. Gains and losses are grouped PER SALE, like a broker's rows. A sale is one ORDER
     (fills carry an optional "order" id; without one, each fill is its own sale). A
     sale that closes two lots, or that executes in two pieces, is netted before it
     counts as a gain or a loss. This was learned on Robinhood's NOK order, which
     executed as 1.0 + 0.065359 shares: counting each execution as a sale split one
     -3.17 loss into -2.97 and -0.19 and disagreed with the broker's own row.
  5. Dividends, interest and fees are excluded. Realized capital gain/loss only.

WASH SALES: a loss is not deductible (yet) if the same security was bought within 30
days before or after the sale. This module only WATCHES for them: it reports an UPPER
BOUND on the affected loss so the number is not read as a clean deduction. It does not
adjust any total, and only Devon's tax preparer can decide what applies. The rule also
spans ALL of a taxpayer's accounts, including IRAs, and a broker only reports the
wash sales it can see within one account.
"""
import re
from collections import deque
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                      # pragma: no cover - zoneinfo missing
    _ET = None

EPS = 1e-9
WASH_DAYS = 30
_FRAC = re.compile(r"(\.\d{6})\d+")


def trade_date(t):
    """New York calendar date of an ISO timestamp. A bare YYYY-MM-DD or a naive
    timestamp is taken as already being an ET date."""
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, date):
        return t
    else:
        s = str(t).strip()
        if len(s) == 10:
            return date.fromisoformat(s)
        s = _FRAC.sub(r"\1", s.replace("Z", "+00:00"))   # nanoseconds break fromisoformat
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        if _ET is None:
            # NEVER fall back to the UTC date. A trade at 8pm New York time is already
            # tomorrow in UTC, so guessing would put it on the wrong day and could move
            # it across a tax-year boundary. Refusing makes the block report UNKNOWN,
            # which is honest, instead of a confident total with a wrong date in it.
            raise RuntimeError("timezone database unavailable (install tzdata); "
                               "refusing to guess the New York trade date")
        dt = dt.astimezone(_ET)
    return dt.date()


def term(open_d, close_d):
    """LT only if sold MORE than one year after purchase, so the anniversary itself is
    still short term."""
    try:
        anniv = date(open_d.year + 1, open_d.month, open_d.day)
    except ValueError:                 # bought on Feb 29
        anniv = date(open_d.year + 1, 3, 1)
    return "LT" if close_d > anniv else "ST"


def fifo(fills):
    """Match sells against buys, first in first out, per symbol.

    fills: list of dicts {t, symbol, side ('buy'|'sell'), qty, price}. The list order
    is significant to callers: every index returned here refers to it.

    Returns {"closed": [lot, ...], "open": {symbol: qty},
    "open_basis": {symbol: cost of what is still held}, "unmatched": [...]}.
    A sell with no buy behind it (a short, or history that starts mid-position) lands
    in "unmatched" and must make the caller report UNVERIFIED, never a clean number.
    """
    order = sorted(range(len(fills)), key=lambda i: (str(fills[i]["t"]), i))
    books, closed, unmatched = {}, [], []
    for i in order:
        f = fills[i]
        sym, side = f["symbol"], str(f["side"]).lower()
        qty, px = float(f["qty"]), float(f["price"])
        if qty <= 0:
            continue
        d = trade_date(f["t"])
        if side == "buy":
            books.setdefault(sym, deque()).append([qty, px, d, i])
        elif side == "sell":
            need, book = qty, books.setdefault(sym, deque())
            while need > EPS and book:
                lot = book[0]
                take = min(need, lot[0])
                basis, proceeds = take * lot[1], take * px
                closed.append({
                    "symbol": sym, "qty": round(take, 9),
                    "open_date": lot[2].isoformat(), "close_date": d.isoformat(),
                    "basis": round(basis, 6), "proceeds": round(proceeds, 6),
                    "gain": round(proceeds - basis, 6), "term": term(lot[2], d),
                    "open_fill": lot[3], "close_fill": i,
                    "sale": str(f.get("order") or ("fill-%d" % i))})
                lot[0] -= take
                need -= take
                if lot[0] <= EPS:
                    book.popleft()
            if need > 1e-7:
                unmatched.append({"symbol": sym, "qty": round(need, 9),
                                  "date": d.isoformat(), "fill": i})
        else:                          # sell_short and anything unexpected
            unmatched.append({"symbol": sym, "qty": round(qty, 9),
                              "date": d.isoformat(), "fill": i, "side": side})
    open_qty, open_basis = {}, {}
    for sym, book in books.items():
        q = sum(l[0] for l in book)
        if q > EPS:
            open_qty[sym] = round(q, 9)
            open_basis[sym] = round(sum(l[0] * l[1] for l in book), 6)
    return {"closed": closed, "open": open_qty, "open_basis": open_basis,
            "unmatched": unmatched}


def summarize_sales(sales):
    """sales: iterable of (term, gain). Each gain is rounded to cents FIRST, as a broker
    row is, then classified. Losses are reported as NEGATIVE numbers."""
    out = {"st": {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0},
           "lt": {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0}}
    for t, g in sales:
        b = out["lt" if t == "LT" else "st"]
        g = round(float(g), 2)
        b["sales"] += 1
        if g > 0:
            b["gains"] += g
        elif g < 0:
            b["losses"] += g
        b["net"] += g
    for b in out.values():
        for k in ("gains", "losses", "net"):
            b[k] = round(b[k], 2)
    out["net"] = round(out["st"]["net"] + out["lt"]["net"], 2)
    out["sales"] = out["st"]["sales"] + out["lt"]["sales"]
    return out


def summarize(closed):
    """Summary from FIFO lots, grouped per sale (fill) and per term."""
    per = {}
    for l in closed:
        k = (l["sale"], l["term"])
        per[k] = per.get(k, 0.0) + l["gain"]
    return summarize_sales([(k[1], g) for k, g in per.items()])


def summarize_by_year(closed):
    """{year: summary}. Tax is per CALENDAR year, so a single all-time running total
    would be wrong from January 1. The year is that of the sale's New York trade date."""
    years = sorted({l["close_date"][:4] for l in closed})
    return {y: summarize([l for l in closed if l["close_date"][:4] == y]) for y in years}


def cross_watch(ledger_x, ledger_y, days=WASH_DAYS):
    """Loss sales in account X that MAY be wash sales because account Y bought the same
    symbol within 30 days either side. The rule spans accounts, but a broker only sees
    its own, so nobody reports these on a 1099-B. Buys come from Y's ledger.
    Returns the same list shape as wash_watch, all scope 'cross-account'."""
    buys = [{"t": b["t"], "symbol": b["symbol"], "side": "buy", "qty": b["qty"]}
            for b in (ledger_y.get("buys") or [])]
    return [w for w in wash_watch(ledger_x.get("closed") or [], [], buys, days)
            if w["scope"] == "cross-account"]


def wash_watch(closed, fills, other_fills=None, days=WASH_DAYS):
    """Loss sales that MAY be wash sales. An UPPER BOUND, not a determination.

    A loss sale is flagged when the same symbol was bought within `days` either side.
    The lots the sale itself consumed are not replacements. one purchase can cover
    several loss sales, so the total can overstate; that is why it is an upper bound.
    other_fills is the OTHER account's fills, checked as 'cross-account': the rule
    spans accounts but no broker will report a cross-account wash sale.
    """
    by_sale = {}
    for l in closed:
        by_sale.setdefault(l["sale"], []).append(l)
    out = []
    for lots in by_sale.values():
        loss = round(sum(l["gain"] for l in lots), 2)
        if loss >= 0:
            continue
        sym = lots[0]["symbol"]
        cd = date.fromisoformat(lots[0]["close_date"])
        sold = sum(l["qty"] for l in lots)
        own = {l["open_fill"] for l in lots}
        for scope, src in (("same-account", fills), ("cross-account", other_fills or [])):
            rq, n = 0.0, 0
            for i, f in enumerate(src):
                if str(f["side"]).lower() != "buy" or f["symbol"] != sym:
                    continue
                if scope == "same-account" and i in own:
                    continue
                if abs((trade_date(f["t"]) - cd).days) <= days:
                    rq += float(f["qty"])
                    n += 1
            if rq > EPS:
                frac = min(1.0, rq / sold) if sold > EPS else 1.0
                out.append({"symbol": sym, "sale_date": cd.isoformat(), "loss": loss,
                            "sold_qty": round(sold, 6), "replacement_qty": round(rq, 6),
                            "replacement_buys": n, "upper_bound": round(loss * frac, 2),
                            "scope": scope})
    return sorted(out, key=lambda w: (w["sale_date"], w["symbol"], w["scope"]))


def wash_totals(watch):
    """Upper bound of affected loss, split by scope. Negative numbers."""
    tot = {"same-account": 0.0, "cross-account": 0.0}
    cnt = {"same-account": 0, "cross-account": 0}
    for w in watch:
        tot[w["scope"]] = round(tot[w["scope"]] + w["upper_bound"], 2)
        cnt[w["scope"]] += 1
    return {"same_account": {"sales": cnt["same-account"], "upper_bound": tot["same-account"]},
            "cross_account": {"sales": cnt["cross-account"], "upper_bound": tot["cross-account"]}}


def verify_positions(open_qty, live, tol=1e-6):
    """Compare quantities implied by the fills against what the broker actually holds.
    A mismatch means fills are missing (a transfer, a split, a spinoff) and the
    realized total cannot be trusted for that symbol."""
    bad = []
    for s in sorted(set(open_qty) | set(live)):
        a, b = float(open_qty.get(s, 0.0)), float(live.get(s, 0.0))
        if abs(a - b) > tol + tol * abs(b):
            bad.append({"symbol": s, "from_fills": round(a, 6), "broker": round(b, 6)})
    return bad


def combine(*blocks):
    """Sum several arms' summaries into one running total."""
    tot = {"st": {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0},
           "lt": {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0}}
    for b in blocks:
        for t in ("st", "lt"):
            for k in ("gains", "losses", "net", "sales"):
                tot[t][k] += (b.get(t) or {}).get(k, 0)
    for t in ("st", "lt"):
        for k in ("gains", "losses", "net"):
            tot[t][k] = round(tot[t][k], 2)
    tot["net"] = round(tot["st"]["net"] + tot["lt"]["net"], 2)
    tot["sales"] = tot["st"]["sales"] + tot["lt"]["sales"]
    return tot


def tax_year():
    """The current tax year by the NEW YORK date, not the machine's clock. A GitHub runner
    is on UTC, so at 8pm New York time on December 31 it is already January 1 there and a
    line would jump to the new year while that evening's trades still belong to the old
    one. (Without a timezone database this falls back to the local clock.)"""
    return datetime.now(_ET).year if _ET is not None else datetime.now().year


def _today_et():
    """Today's NEW YORK date (the same clock tax_year uses)."""
    return datetime.now(_ET).date() if _ET is not None else date.today()


def watch_for_year(watch, year):
    """The wash_watch entries whose SALE fell in `year`. A loss belongs to the year it was
    sold in, even when the replacement purchase lands in the next one, so a December loss
    with a January repurchase is a prior-year matter and must not leak into the new year."""
    y = str(year)
    return [w for w in (watch or []) if str(w.get("sale_date", ""))[:4] == y]


def money(x):
    x = float(x or 0.0)
    return ("-$%s" % format(abs(x), ",.2f")) if x < 0 else ("$%s" % format(x, ",.2f"))


def report_line(label, s, year=None, watch=None):
    """One line any report can print. Never renders a missing or failed number as zero.

    Reports the CURRENT TAX YEAR (or `year`) when the block carries by_year, because the
    IRS nets per calendar year and a running total that never resets would mislead after
    January 1. Falls back to the all-time figures for a block without by_year.

    watch is the ledger's wash_watch LIST. Given it, the wash-sale flag is that year's own
    exposure. Without it, a block that spans several years only has all-time wash totals,
    and those are labelled "all years" rather than passed off as this year's.
    """
    if not s:
        return "%s: realized total NOT AVAILABLE (never computed)" % label
    state = s.get("state", "ok")
    if state == "unknown":
        return "%s: realized total UNKNOWN (%s)" % (label, s.get("reason", "check failed"))
    yr = str(year or tax_year())
    by = s.get("by_year")
    if by is not None:
        blk = by.get(yr) or {"st": {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0},
                             "lt": {"sales": 0}}
        tag = "%s realized" % yr
    else:
        blk, tag = s, "realized"
    st, lt = blk.get("st") or {}, blk.get("lt") or {}
    line = "%s %s, short-term: gains %s, losses %s, net %s (%d sales)" % (
        label, tag, money(st.get("gains")), money(st.get("losses")),
        money(st.get("net")), st.get("sales", 0))
    if lt.get("sales"):
        line += "; long-term net %s" % money(lt.get("net"))
    flags = []
    if state == "stale":
        flags.append("STALE, last good %s" % s.get("as_of", "?"))
    if state == "unverified":
        flags.append("UNVERIFIED: %s" % s.get("reason", "does not reconcile"))
    spans_years = by is not None and bool(set(by) - {yr})
    if watch is not None:
        w, suffix = wash_totals(watch_for_year(watch, yr)), ""
    else:
        w, suffix = s.get("wash_watch") or {}, (" (all years)" if spans_years else "")
    same = (w.get("same_account") or {}).get("upper_bound", 0.0)
    cross = (w.get("cross_account") or {}).get("upper_bound", 0.0)
    if same or cross:
        bits = []
        if same:
            bits.append("up to %s possibly wash-sale%s" % (money(abs(same)), suffix))
        if cross:
            bits.append("up to %s cross-account%s" % (money(abs(cross)), suffix))
        flags.append("; ".join(bits))
    if flags:
        line += " [" + " | ".join(flags) + "]"
    return line


def load_ledger(path):
    """A committed ledger, or None if missing or unreadable."""
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def rh_sells_logged(log_path="rh_trade_log.jsonl"):
    """Sells the laptop daemon has logged as done. This is the tripwire for a STALE Arm B
    ledger: its figures come from the broker, so someone has to re-pull them after a
    sale, and nothing forces that. The log is committed, so ANY session can compare."""
    import json
    n = 0
    try:
        with open(log_path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                # Count every sell EXCEPT an explicit rejection. The daemon's statuses are
                # ok, rejected and unknown (the bridge could not confirm), and a sell logged
                # "unknown" may really have filled. Missing a real sale would leave a
                # silently wrong tax number; a false alarm costs one cheap refresh.
                if r.get("action") == "sell" and r.get("status") != "rejected":
                    n += 1
    except OSError:
        return None
    return n


def arm_b_block(ledger_path="realized_b.json", log_path="rh_trade_log.jsonl",
                status_path="rh_status.json"):
    """Arm B's realized block, with staleness detected rather than assumed away.

    Arm B is index-only, so it rarely sells and the ledger can sit unchanged for weeks
    and still be right. That is exactly why it can go silently wrong: when it does sell,
    nothing in the ledger changes on its own. Two INDEPENDENT tripwires, because either
    one alone has a blind spot (laptop found both, 2026-09-29):

      1. THE SELLS LOG. Compare the sells the daemon has LOGGED against the number the
         ledger accounted for. More logged means a sale the ledger has not seen. FEWER
         logged is also a fault, not a clean bill: the log was rewritten or truncated,
         so this tripwire cannot be trusted. (The first version tested only "more" and
         would have stayed silent.)
      2. THE PUBLISHED HOLDINGS. A sale made BY HAND in the Robinhood app never appears
         in rh_trade_log.jsonl, so tripwire 1 cannot see it. But it lowers the holdings
         the daemon publishes. Buys and dividend reinvestments only RAISE holdings, so a
         holding BELOW what the ledger says is still held can only mean a sale the ledger
         does not know about. Skipped while the status is degraded: those snapshots carry
         last-known values forward and prove nothing.
    """
    led = load_ledger(ledger_path)
    if not led or not led.get("summary"):
        return None
    s = dict(led["summary"])
    reasons = []

    counted = s.get("rh_log_sells_counted")
    logged = rh_sells_logged(log_path)
    if logged is None or counted is None:
        reasons.append("cannot compare against the sells log")
    elif logged > counted:
        reasons.append("%d sell(s) logged since this ledger was computed; "
                       "the laptop must re-pull realized P&L from the broker" % (logged - counted))
    elif logged < counted:
        reasons.append("the sells log holds FEWER sells (%d) than the ledger accounted for (%d); "
                       "it was rewritten or truncated, so that tripwire cannot be trusted"
                       % (logged, counted))

    st = load_ledger(status_path) or {}
    pos = st.get("positions")
    held = led.get("open") or {}
    if isinstance(pos, dict) and pos and held and not st.get("degraded"):
        short = ["%s %.6f now vs %.6f in the ledger" % (sym, float(pos.get(sym, 0.0)), float(q))
                 for sym, q in sorted(held.items())
                 if float(pos.get(sym, 0.0)) < float(q) - 1e-6 - 1e-6 * abs(float(q))]
        if short:
            reasons.append("holdings fell below what the ledger says is held (%s): a sale the "
                           "ledger does not know about, possibly made by hand in the Robinhood app"
                           % "; ".join(short[:3]))

    if reasons:
        if s.get("state") in (None, "ok"):
            s["state"] = "stale"
            s["reason"] = "; ".join(reasons)
        else:
            s["reason"] = "; ".join(filter(None, [s.get("reason")] + reasons))
    return s


def combined_line(a, b, year=None):
    """Both arms, one tax year. Refuses to add a number that is unknown."""
    yr = str(year or tax_year())
    parts, bad = [], []
    for name, s in (("A", a), ("B", b)):
        if not s or s.get("state") == "unknown" or s.get("by_year") is None:
            return "BOTH ARMS %s realized: NOT AVAILABLE (Arm %s has no usable figure)" % (yr, name)
        parts.append(s["by_year"].get(yr) or {"st": {}, "lt": {}})
        if s.get("state") in ("stale", "unverified"):
            bad.append("%s=%s" % (name, s["state"]))
    t = combine(*parts)
    line = "BOTH ARMS %s realized, short-term: gains %s, losses %s, net %s (%d sales)" % (
        yr, money(t["st"]["gains"]), money(t["st"]["losses"]), money(t["st"]["net"]),
        t["st"]["sales"])
    if t["lt"]["sales"]:
        line += "; long-term net %s" % money(t["lt"]["net"])
    if bad:
        line += " [component not confirmed: %s]" % ", ".join(bad)
    return line


def cross_totals(ledger_a, ledger_b, year=None):
    """Loss-sale exposure BETWEEN the accounts, upper bound, both directions. With `year`,
    only losses SOLD in that year (see watch_for_year)."""
    if not ledger_a or not ledger_b:
        return None
    ab = cross_watch(ledger_a, ledger_b)      # A's losses vs B's later/earlier buys
    ba = cross_watch(ledger_b, ledger_a)
    if year is not None:
        ab, ba = watch_for_year(ab, year), watch_for_year(ba, year)
    return {"a_losses_vs_b_buys": {"sales": len(ab), "upper_bound": round(sum(w["upper_bound"] for w in ab), 2)},
            "b_losses_vs_a_buys": {"sales": len(ba), "upper_bound": round(sum(w["upper_bound"] for w in ba), 2)}}


def _has_sales(block, year):
    by = (block or {}).get("by_year") or {}
    y = by.get(str(year)) or {}
    return bool((y.get("st") or {}).get("sales") or (y.get("lt") or {}).get("sales"))


# Last day, in the year AFTER the sales, that the prior tax year's lines are still printed.
# Tax returns are prepared in Jan-Apr and extended ones are due mid-October, so the final prior-year
# numbers must not vanish from every report on January 1, which is when the current-year lines
# restart from zero.
PRIOR_YEAR_THROUGH = (10, 31)


def repo_report_lines(a_block=None, year=None, status_path="status.json",
                      a_ledger="realized_a.json", b_ledger="realized_b.json",
                      log_path="rh_trade_log.jsonl", today=None):
    """THE ONE PLACE every report gets its realized-gain/loss lines from.

    Devon (2026-09-29) asked for a running realized total in every report, to track tax
    implications. Every report calls this rather than formatting the numbers itself, so
    the bot email, the weekly review, the daily checks, the audit and pnl.py cannot
    drift apart. Reads COMMITTED files only, so it works from any session or runner.

    a_block lets the bot pass its fresh in-memory block instead of last cycle's.

    Prints the current tax year, and ALSO the prior tax year through PRIOR_YEAR_THROUGH when
    it had any sales, so last year's final figures stay in view while returns are open.
    """
    la, lb = load_ledger(a_ledger), load_ledger(b_ledger)
    if a_block is None:
        st = load_ledger(status_path) or {}
        a_block = st.get("realized") or ((la or {}).get("summary"))
    b_block = arm_b_block(b_ledger, log_path)
    today = today or _today_et()
    yr = int(year or today.year)
    wa, wb = [(led or {}).get("wash_watch") for led in (la, lb)]
    wa, wb = (wa if isinstance(wa, list) else None), (wb if isinstance(wb, list) else None)

    def year_lines(y):
        out = [report_line("Arm A (Alpaca)", a_block, y, wa),
               report_line("Arm B (Robinhood)", b_block, y, wb),
               combined_line(a_block, b_block, y)]
        x = cross_totals(la, lb, y)
        if x:
            a, b = x["a_losses_vs_b_buys"], x["b_losses_vs_a_buys"]
            if a["sales"] or b["sales"]:
                out.append(
                    "Cross-account wash-sale watch %d (upper bound; no broker reports these): "
                    "Arm A losses up to %s (%d sales), Arm B losses up to %s (%d sales)"
                    % (y, money(abs(a["upper_bound"])), a["sales"], money(abs(b["upper_bound"])), b["sales"]))
        return out

    lines = year_lines(yr)
    prior = yr - 1
    if (today.year == yr and (today.month, today.day) <= PRIOR_YEAR_THROUGH
            and (_has_sales(a_block, prior) or _has_sales(b_block, prior))):
        lines.append("Prior tax year %d (still open for filing):" % prior)
        lines += year_lines(prior)
    lines.append("Realized = positions actually SOLD, not the equity change. Not a tax "
                 "document: the broker 1099-B is authoritative, and wash sales are for "
                 "your tax preparer to decide.")
    return lines
