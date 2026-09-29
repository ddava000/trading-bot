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
  4. Gains and losses are grouped PER SALE (one sell fill), like a broker's rows, so a
     sale that closes two lots is netted before it counts as a gain or a loss.
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
    if dt.tzinfo is not None and _ET is not None:
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

    Returns {"closed": [lot, ...], "open": {symbol: qty}, "unmatched": [...]}.
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
                    "open_fill": lot[3], "close_fill": i})
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
    open_qty = {}
    for sym, book in books.items():
        q = sum(l[0] for l in book)
        if q > EPS:
            open_qty[sym] = round(q, 9)
    return {"closed": closed, "open": open_qty, "unmatched": unmatched}


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
        k = (l["close_fill"], l["term"])
        per[k] = per.get(k, 0.0) + l["gain"]
    return summarize_sales([(k[1], g) for k, g in per.items()])


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
        by_sale.setdefault(l["close_fill"], []).append(l)
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


def money(x):
    x = float(x or 0.0)
    return ("-$%s" % format(abs(x), ",.2f")) if x < 0 else ("$%s" % format(x, ",.2f"))


def report_line(label, s):
    """One line any report can print. Never renders a missing or failed number as zero."""
    if not s:
        return "%s: realized total NOT AVAILABLE (never computed)" % label
    state = s.get("state", "ok")
    if state == "unknown":
        return "%s: realized total UNKNOWN (%s)" % (label, s.get("reason", "check failed"))
    st, lt = s.get("st") or {}, s.get("lt") or {}
    line = "%s realized, short-term: gains %s, losses %s, net %s (%d sales)" % (
        label, money(st.get("gains")), money(st.get("losses")),
        money(st.get("net")), st.get("sales", 0))
    if lt.get("sales"):
        line += "; long-term net %s" % money(lt.get("net"))
    flags = []
    if state == "stale":
        flags.append("STALE, last good %s" % s.get("as_of", "?"))
    if state == "unverified":
        flags.append("UNVERIFIED: %s" % s.get("reason", "does not reconcile"))
    w = s.get("wash_watch") or {}
    same = (w.get("same_account") or {}).get("upper_bound", 0.0)
    cross = (w.get("cross_account") or {}).get("upper_bound", 0.0)
    if same or cross:
        bits = []
        if same:
            bits.append("up to %s possibly wash-sale" % money(abs(same)))
        if cross:
            bits.append("up to %s cross-account" % money(abs(cross)))
        flags.append("; ".join(bits))
    if flags:
        line += " [" + " | ".join(flags) + "]"
    return line
