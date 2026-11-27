"""A ticker change is one security, not a corporate-action break (2026-11-20).

WHY. 2026-11-16: BKH became HZN. Alpaca reported it as two NC activity rows. Every NC row counted as FIFO-breaking, so Arm A's realized block went
UNVERIFIED and stayed so, and the trade log's BKH buys never reached HZN, so the time stop (which dates a position from the log) had no buy date for
HZN and could never fire on it. LAPTOP BOT found both. The real rows were read from the broker on 2026-11-20 and are used below verbatim.

The rule is STRICT. Only a pair the broker itself shows as a pure rename counts; anything less (a merger, a cash-in-lieu, a split, a lone row) keeps
breaking FIFO exactly as before. These tests pin both halves: the rename is understood, and nothing else is let through.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402
import realized as R  # noqa: E402
from test_arm_a_cold_review import ET_NOW, broker, pos, sandbox  # noqa: E402

# The two rows exactly as the broker returned them on 2026-11-20 (account_id dropped).
NC_IN = {"symbol": "HZN", "qty": "0.143977258", "price": "76.75", "date": "2026-11-16", "description": "Name Change from BKH to HZN",
         "activity_sub_type": "SNC", "net_amount": "0", "activity_type": "NC", "corporate_action_id": "0bbf827c-e765-4887-a603-e5f5e4236212",
         "created_at": "2026-11-16T12:06:55.507987Z", "currency": "USD", "cusip": "092113109", "group_id": "e54ff29b-0935-4acf-99d5-aa6f955a8647",
         "id": "20261116000000000::8efd7046-f94e-439a-8d43-9c585e1386e7", "status": "executed"}
NC_OUT = dict(NC_IN, symbol="BKH", qty="-0.143977258", created_at="2026-11-16T12:06:17.071754Z",
              id="20261116000000000::fdfab492-a77c-4c5e-832e-fe6cfe5dbda4")
BOTH = [NC_OUT, NC_IN]


def fill(sym, qty, price, t, fid, order=None):
    return {"activity_type": "FILL", "symbol": sym, "side": "buy", "qty": str(qty), "price": str(price), "transaction_time": t,
            "id": fid, "order_id": order or ("o-" + fid)}


class RenameMap(unittest.TestCase):
    def test_the_real_bkh_to_hzn_rows_are_a_proven_rename(self):
        aliases, proven = R.rename_map(BOTH)
        self.assertEqual(aliases, {"BKH": "HZN"})
        self.assertEqual(proven, {NC_IN["id"], NC_OUT["id"]})

    def test_row_order_does_not_matter(self):
        self.assertEqual(R.rename_map([NC_IN, NC_OUT])[0], {"BKH": "HZN"})

    def test_no_rows_and_none_are_empty(self):
        self.assertEqual(R.rename_map([]), ({}, set()))
        self.assertEqual(R.rename_map(None), ({}, set()))

    def test_other_activity_types_are_ignored(self):
        self.assertEqual(R.rename_map([fill("BKH", 1, 75, "2026-10-07T14:00:00Z", "f1")] + BOTH)[0], {"BKH": "HZN"})

    def test_every_way_a_pair_can_fail_the_proof_leaves_it_unproven(self):
        bad = {
            "the same shape under another activity type (a reorganisation)": [dict(NC_OUT, activity_type="REORG"), dict(NC_IN, activity_type="REORG")],
            "a lone row": [NC_IN],
            "a lone row, the old side": [NC_OUT],
            "three rows in the group": BOTH + [dict(NC_IN, id="x3")],
            "quantities differ": [NC_OUT, dict(NC_IN, qty="0.15")],
            "both positive": [dict(NC_OUT, qty="0.143977258"), NC_IN],
            "both negative": [NC_OUT, dict(NC_IN, qty="-0.143977258")],
            "money moved on one row (cash in lieu)": [NC_OUT, dict(NC_IN, net_amount="1.25")],
            "money moved on the other row": [dict(NC_OUT, net_amount="-0.01"), NC_IN],
            "not a symbol-change sub type": [dict(NC_OUT, activity_sub_type="SMG"), dict(NC_IN, activity_sub_type="SMG")],
            "one sub type missing": [NC_OUT, {k: v for k, v in NC_IN.items() if k != "activity_sub_type"}],
            "the same symbol twice": [NC_OUT, dict(NC_IN, symbol="BKH")],
            "a blank symbol": [NC_OUT, dict(NC_IN, symbol="")],
            "different dates": [NC_OUT, dict(NC_IN, date="2026-11-17")],
            "different cusips": [NC_OUT, dict(NC_IN, cusip="999999999")],
            "a row not executed": [NC_OUT, dict(NC_IN, status="canceled")],
            "a quantity that is not a number": [NC_OUT, dict(NC_IN, qty="n/a")],
            "a net amount that is not a number": [NC_OUT, dict(NC_IN, net_amount="n/a")],
            "no group key on either row": [{k: v for k, v in r.items() if k not in ("corporate_action_id", "group_id")} for r in BOTH],
            "two different groups, one row each": [NC_OUT, dict(NC_IN, corporate_action_id="other", group_id="other")],
        }
        for label, rows in bad.items():
            self.assertEqual(R.rename_map(rows), ({}, set()), label)

    def test_a_missing_cusip_or_status_does_not_block_a_pair_that_is_otherwise_exact(self):
        rows = [{k: v for k, v in r.items() if k not in ("cusip", "status")} for r in BOTH]
        self.assertEqual(R.rename_map(rows)[0], {"BKH": "HZN"})

    def test_group_id_is_used_when_there_is_no_corporate_action_id(self):
        rows = [{k: v for k, v in r.items() if k != "corporate_action_id"} for r in BOTH]
        self.assertEqual(R.rename_map(rows)[0], {"BKH": "HZN"})

    def test_a_chain_resolves_to_the_newest_name(self):
        ab = [dict(NC_OUT, symbol="AAA", corporate_action_id="ab", id="ab1", qty="-1"), dict(NC_IN, symbol="BBB", corporate_action_id="ab", id="ab2", qty="1")]
        bc = [dict(NC_OUT, symbol="BBB", corporate_action_id="bc", id="bc1", qty="-1"), dict(NC_IN, symbol="CCC", corporate_action_id="bc", id="bc2", qty="1")]
        aliases, proven = R.rename_map(ab + bc)
        self.assertEqual(aliases, {"AAA": "CCC", "BBB": "CCC"})
        self.assertEqual(proven, {"ab1", "ab2", "bc1", "bc2"})

    def test_a_cycle_resolves_nothing_and_proves_nothing(self):
        ab = [dict(NC_OUT, symbol="AAA", corporate_action_id="ab", id="ab1", qty="-1"), dict(NC_IN, symbol="BBB", corporate_action_id="ab", id="ab2", qty="1")]
        ba = [dict(NC_OUT, symbol="BBB", corporate_action_id="ba", id="ba1", qty="-1"), dict(NC_IN, symbol="AAA", corporate_action_id="ba", id="ba2", qty="1")]
        self.assertEqual(R.rename_map(ab + ba), ({}, set()))

    def test_one_old_name_renamed_two_ways_is_ambiguous_and_left_alone(self):
        one = [dict(NC_OUT, symbol="AAA", corporate_action_id="x1", id="x11", qty="-1"), dict(NC_IN, symbol="BBB", corporate_action_id="x1", id="x12", qty="1")]
        two = [dict(NC_OUT, symbol="AAA", corporate_action_id="x2", id="x21", qty="-1"), dict(NC_IN, symbol="CCC", corporate_action_id="x2", id="x22", qty="1")]
        self.assertEqual(R.rename_map(one + two), ({}, set()))

    def test_a_proven_rename_beside_an_unproven_row_proves_only_its_own_rows(self):
        stray = dict(NC_IN, symbol="ZZZ", qty="2", corporate_action_id="stray", group_id="stray", id="stray1")
        aliases, proven = R.rename_map(BOTH + [stray])
        self.assertEqual(aliases, {"BKH": "HZN"})
        self.assertNotIn("stray1", proven)


class RelabelFills(unittest.TestCase):
    def test_old_names_become_new_and_everything_else_is_untouched(self):
        fills = [{"symbol": "BKH", "qty": "1"}, {"symbol": "SPY", "qty": "2"}]
        out = R.relabel_fills(fills, {"BKH": "HZN"})
        self.assertEqual([f["symbol"] for f in out], ["HZN", "SPY"])
        self.assertEqual(fills[0]["symbol"], "BKH")                         # the input is not mutated

    def test_no_aliases_returns_an_equal_copy(self):
        fills = [{"symbol": "BKH"}]
        self.assertEqual(R.relabel_fills(fills, {}), fills)
        self.assertIsNot(R.relabel_fills(fills, {}), fills)


# ------------------------------------------------------------------ the realized block, end to end on a fake broker
BUY1 = fill("BKH", "0.078", "75.76", "2026-10-07T13:45:00Z", "f1")
BUY2 = fill("BKH", "0.065977258", "77.9195", "2026-10-21T13:45:00Z", "f2")
DEPOSIT = {"activity_type": "CSD", "id": "d1", "net_amount": "100", "date": "2026-10-01"}


def block(acts, held, cash=None):
    """alpaca_realized_block against a fake broker. held: {symbol: (qty, price)}. Cash is solved so the independent identity holds."""
    cost = sum(float(a["qty"]) * float(a["price"]) for a in acts if a.get("activity_type") == "FILL")
    cash = 100.0 - cost if cash is None else cash
    positions = [{"symbol": s, "qty": str(q), "market_value": str(round(q * p, 6)), "avg_entry_price": str(p)} for s, (q, p) in held.items()]

    def get(path, _tries=3):
        if path == "/v2/account":
            return {"cash": str(cash), "equity": "1", "account_number": "PA0000008736"}
        if path == "/v2/positions":
            return list(positions)
        return {"message": "stub"}
    with mock.patch.object(bot, "alpaca_activity_history", lambda *a, **k: list(acts)), mock.patch.object(bot, "alpaca_get", get), \
            mock.patch.object(bot.time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
        return bot.alpaca_realized_block()


QTY = 0.078 + 0.065977258


class RealizedBlockAcrossARename(unittest.TestCase):
    HELD = {"HZN": (QTY, 76.75)}

    def test_before_a_rename_nothing_changes_the_baseline(self):
        summary, _ = block([DEPOSIT, BUY1, BUY2], {"BKH": (QTY, 76.75)})
        self.assertEqual(summary["state"], "ok", summary.get("reason"))
        self.assertNotIn("renames", summary)

    def test_the_real_rename_reconciles_and_the_block_is_ok(self):
        summary, ledger = block([DEPOSIT, BUY1, BUY2] + BOTH, self.HELD)
        self.assertEqual(summary["state"], "ok", summary.get("reason"))
        self.assertNotIn("reason", summary)
        self.assertEqual(summary["renames"], {"BKH": "HZN"})
        self.assertEqual(ledger["open"], {"HZN": round(QTY, 9)})                # the lots moved to the new name
        self.assertEqual({b["symbol"] for b in ledger["buys"]}, {"HZN"})

    def test_control_an_unpaired_nc_row_still_breaks_fifo(self):
        summary, _ = block([DEPOSIT, BUY1, BUY2, NC_IN], self.HELD)
        self.assertEqual(summary["state"], "unverified")
        self.assertIn("NC x1", summary["reason"])
        self.assertNotIn("renames", summary)

    def test_control_a_rename_that_moved_money_still_breaks_fifo(self):
        rows = [NC_OUT, dict(NC_IN, net_amount="0.40")]
        summary, _ = block([DEPOSIT, BUY1, BUY2] + rows, self.HELD, cash=100.0 - 11.050255 + 0.80)
        self.assertEqual(summary["state"], "unverified")
        self.assertIn("NC x2", summary["reason"])

    def test_control_another_corporate_action_beside_a_clean_rename_still_breaks_fifo(self):
        split = {"activity_type": "SSP", "id": "s1", "symbol": "SPY", "qty": "1", "net_amount": "0", "date": "2026-11-17"}
        summary, _ = block([DEPOSIT, BUY1, BUY2] + BOTH + [split], self.HELD)
        self.assertEqual(summary["state"], "unverified")
        self.assertIn("SSP x1", summary["reason"])
        self.assertNotIn("NC", summary["reason"])
        self.assertEqual(summary["renames"], {"BKH": "HZN"})

    def test_control_a_rename_with_shares_that_do_not_match_the_broker_is_still_flagged(self):
        summary, _ = block([DEPOSIT, BUY1, BUY2] + BOTH, {"HZN": (QTY + 0.5, 76.75)})
        self.assertEqual(summary["state"], "unverified")
        self.assertIn("positions do not reconcile", summary["reason"])

    def test_a_sale_after_the_rename_matches_the_old_names_lots(self):
        sell = dict(fill("HZN", "0.143977258", "80", "2026-11-18T15:00:00Z", "f3"), side="sell")
        proceeds = 0.143977258 * 80
        summary, ledger = block([DEPOSIT, BUY1, BUY2] + BOTH + [sell], {}, cash=100.0 - 11.050255 + proceeds)
        self.assertEqual(summary["state"], "ok", summary.get("reason"))
        self.assertEqual(len(ledger["closed"]), 2)                                   # both BKH lots were sold as HZN
        self.assertEqual({c["symbol"] for c in ledger["closed"]}, {"HZN"})
        self.assertEqual(ledger["open"], {})


# ------------------------------------------------------------------ the time stop
class LastBuyDates(unittest.TestCase):
    ALIASES = {"BKH": "HZN"}

    def dates(self, held, log, renames):
        calls = []

        def get_renames():
            calls.append(1)
            if isinstance(renames, Exception):
                raise renames
            return renames
        with sandbox(broker()):
            with open("trade_log.jsonl", "w") as f:
                for r in log:
                    f.write(json.dumps(r) + "\n")
            return bot.last_buy_dates(held, get_renames=get_renames), len(calls)

    LOG = [{"ts": "2026-10-07T09:45", "side": "buy", "symbol": "BKH"}, {"ts": "2026-10-21T09:45", "side": "buy", "symbol": "BKH"},
           {"ts": "2026-11-18T09:45", "side": "buy", "symbol": "CALM"}]

    def test_the_renamed_position_inherits_the_old_names_most_recent_buy(self):
        got, calls = self.dates({"HZN", "CALM"}, self.LOG, self.ALIASES)
        self.assertEqual(got, {"HZN": "2026-10-21T09:45", "CALM": "2026-11-18T09:45"})
        self.assertEqual(calls, 1)

    def test_without_the_alias_the_renamed_position_has_no_date_which_was_the_bug(self):
        got, _ = self.dates({"HZN", "CALM"}, self.LOG, {})
        self.assertEqual(got, {"CALM": "2026-11-18T09:45"})

    def test_a_buy_of_the_new_name_after_the_rename_resets_the_clock(self):
        log = self.LOG + [{"ts": "2026-11-19T09:45", "side": "buy", "symbol": "HZN"}]
        got, calls = self.dates({"HZN"}, log, self.ALIASES)
        self.assertEqual(got["HZN"], "2026-11-19T09:45")
        self.assertEqual(calls, 0)                                                 # every held symbol had a date: no lookup at all

    def test_no_lookup_is_made_when_every_held_symbol_already_has_a_buy(self):
        got, calls = self.dates({"CALM"}, self.LOG, self.ALIASES)
        self.assertEqual(got, {"CALM": "2026-11-18T09:45"})
        self.assertEqual(calls, 0)

    def test_a_failing_lookup_changes_nothing_and_raises_nothing(self):
        got, calls = self.dates({"HZN", "CALM"}, self.LOG, OSError("down"))
        self.assertEqual(got, {"CALM": "2026-11-18T09:45"})
        self.assertEqual(calls, 1)

    def test_a_sell_is_never_taken_for_a_buy(self):
        log = [{"ts": "2026-10-21T09:45", "side": "sell", "symbol": "BKH"}]
        got, _ = self.dates({"HZN"}, log, self.ALIASES)
        self.assertEqual(got, {})

    def test_the_default_lookup_reads_the_broker_and_returns_proven_renames_only(self):
        with mock.patch.object(bot, "alpaca_activity_history", lambda *a, **k: BOTH):
            self.assertEqual(bot._renames_from_broker(), {"BKH": "HZN"})
        with mock.patch.object(bot, "alpaca_activity_history", lambda *a, **k: [NC_IN]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bot._renames_from_broker(), {})
        with mock.patch.object(bot, "alpaca_activity_history", lambda *a, **k: None):
            self.assertEqual(bot._renames_from_broker(), {})
        with mock.patch.object(bot, "alpaca_activity_history", side_effect=OSError("down")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bot._renames_from_broker(), {})
        self.assertIn("rename lookup failed", out.getvalue())


def full_cycle(positions, price, log_rows=(), renames=None):
    """The real run_bot() (the 15-minute full cycle, where the time stop lives) against a fake world with one flat-priced stock."""
    sym = positions[0]["symbol"]
    with sandbox(broker(positions=positions), prices={sym: price}) as ns:
        with open("trade_log.jsonl", "w") as f:
            for r in log_rows:
                f.write(json.dumps(r) + "\n")
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(bot, "alpaca_bars_multi", lambda syms: {s: ([price] * 30, [1000] * 30) for s in syms}))
            st.enter_context(mock.patch.object(bot, "yf_vix", lambda: 15.0))
            if renames is not None:
                st.enter_context(mock.patch.object(bot, "_renames_from_broker", lambda: renames))
            bot.run_bot()
        return ns


class TimeStopOnARenamedPosition(unittest.TestCase):
    """HZN: 30 days since the last BKH buy, no signal, price inside 2% of cost: the time stop fires, but ONLY once the buy date crosses the rename."""
    PRICE = 76.0                                                                    # cost 76.75: -1%, above the -7% stop, below cost * 1.02
    LOG = [{"ts": "2026-10-21T09:45", "side": "buy", "symbol": "BKH"}]

    def test_with_the_rename_known_the_time_stop_sells_hzn(self):
        ns = full_cycle([pos("HZN", 0.143977258, 76.75, self.PRICE)], self.PRICE, self.LOG, {"BKH": "HZN"})
        self.assertIn("TIME-STOP HZN", ns.out.getvalue())
        self.assertEqual([o.get("symbol") for o in ns.sent], ["HZN"])

    def test_without_it_nothing_sells_which_is_the_bug(self):
        ns = full_cycle([pos("HZN", 0.143977258, 76.75, self.PRICE)], self.PRICE, self.LOG, {})
        self.assertNotIn("TIME-STOP", ns.out.getvalue())
        self.assertEqual(ns.sent, [])

    def test_control_a_young_position_is_not_time_stopped_even_with_the_rename(self):
        young = [{"ts": "2026-10-27T09:45", "side": "buy", "symbol": "BKH"}]           # one day before ET_NOW
        ns = full_cycle([pos("HZN", 0.143977258, 76.75, self.PRICE)], self.PRICE, young, {"BKH": "HZN"})
        self.assertNotIn("TIME-STOP", ns.out.getvalue())
        self.assertEqual(ns.sent, [])


class NoStopIsSaidOutLoud(unittest.TestCase):
    """A held position whose broker cost basis is 0 has no stop of any kind, and both passes used to skip it without a word."""

    def test_the_full_cycle_names_a_position_with_no_cost_basis(self):
        ns = full_cycle([pos("HZN", 0.143977258, 0, 76.0)], 76.0, [], {})
        self.assertIn("[NO STOP on HZN", ns.out.getvalue())
        self.assertEqual(ns.sent, [])

    def test_the_fast_pass_names_it_too(self):
        with sandbox(broker(positions=[pos("HZN", 0.143977258, 0, 76.0)]), prices={"HZN": 76.0}) as ns:
            bot.exit_pass(ET_NOW, set())
        self.assertIn("[NO STOP on HZN", ns.out.getvalue())
        self.assertEqual(ns.sent, [])

    def test_a_position_with_a_cost_basis_prints_no_such_line_in_either_pass(self):
        ns = full_cycle([pos("HZN", 0.143977258, 76.75, 76.0)], 76.0, [], {})
        self.assertNotIn("NO STOP", ns.out.getvalue())
        with sandbox(broker(positions=[pos("HZN", 0.143977258, 76.75, 76.0)]), prices={"HZN": 76.0}) as ns2:
            bot.exit_pass(ET_NOW, set())
        self.assertNotIn("NO STOP", ns2.out.getvalue())


if __name__ == "__main__":
    unittest.main()
