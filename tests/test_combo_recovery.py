"""The recovery as a combo at base size (operator, 2026-09-27).

"Replace the single recover into a Combo with same base size, no more up
scaling ... everything is kept just as design." The loss step still decides
WHEN; the trade it asks for is this entry plus a partner (SOL for BTC/ETH, BTC
for SOL) as one combo at the base count, bought only at or below the legs'
product. No partner, no quote, too dear or no fill: the entry goes out single-
leg at base size. An UNKNOWN outcome sends nothing else.

The exchange side runs the real `combo_recovery.buy` against a fake Kalshi
(httpx.MockTransport) that records every request, so the tests pin what is
actually sent: the legs and their sides, the size, and `accepted_side: no` -
the way every combo on this account was bought.
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import btc15_signal.main as main  # noqa: E402
from btc15_signal import combo_recovery as cr  # noqa: E402
from btc15_signal import execution as ex  # noqa: E402
from btc15_signal import messages, surface  # noqa: E402
from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.store import Store  # noqa: E402

# THE MAP IN FORCE, captured before any test swaps it. Since 2026-09-28 BTC has
# no partner (operator: "BTC AND GOLD ONLY I SAID"; FINDINGS 108). The combo
# MECHANICS below are still tested on the BTC->SOL pair they were written on,
# because ETH->SOL and SOL->BTC run exactly the same code.
LIVE_PARTNERS = dict(cr.PARTNERS)
MECHANICS_PARTNERS = {"BTC": ("SOL",), "ETH": ("SOL",), "SOL": ("BTC",)}


@pytest.fixture(autouse=True)
def _mechanics_map(monkeypatch):
    monkeypatch.setattr(cr, "PARTNERS", MECHANICS_PARTNERS)

BASE = "https://external-api.kalshi.com/trade-api/v2"
W = 1_790_000_100_000
BTC = "KXBTC15M-26SEP271830-30"
SOL = "KXSOL15M-26SEP271830-30"
COMBO = "KXMVECROSSCATEGORY-S2026ABC-123"


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    async def no_wait(_s):
        return None
    monkeypatch.setattr(cr, "_sleep", no_wait)
    monkeypatch.setattr(ex, "_sleep", no_wait)


@pytest.fixture
def key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return str(p)


class Exchange:
    """A fake Kalshi combo desk. `quotes`: list of (no_bid, status)."""

    def __init__(self, quotes=(("0.4500", "open"),), accept_status=200,
                 confirm="executed", fills=((2.0, "0.5500", "0.03"),),
                 accept_raises=False, partner_no_ask="0.8000",
                 fill_time="2099-01-01T00:00:00Z"):
        self.partner_no_ask = partner_no_ask
        self.fill_time = fill_time
        self.quotes = list(quotes)
        self.accept_status = accept_status
        self.confirm = confirm
        self.fills = list(fills)
        self.accept_raises = accept_raises
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/trade-api/v2", "")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if path == "/portfolio/orders":
            return httpx.Response(200, json={"orders": [{"user_id": "me"}]})
        if path.startswith("/multivariate_event_collections/"):
            return httpx.Response(200, json={"market_ticker": COMBO})
        if path == "/communications/rfqs":
            return httpx.Response(201, json={"rfq": {"id": "rfq-1"}})
        if path == "/communications/quotes":
            return httpx.Response(200, json={"quotes": [
                {"id": f"q{i}", "rfq_id": "rfq-1", "status": st,
                 "no_bid_dollars": nb, "yes_bid_dollars": "0.0010"}
                for i, (nb, st) in enumerate(self.quotes)]})
        if path.endswith("/accept"):
            if self.accept_raises:
                raise httpx.ReadTimeout("lost")
            return httpx.Response(self.accept_status, json={})
        if path.startswith("/communications/quotes/"):
            return httpx.Response(200, json={"quote": {"status": self.confirm}})
        if path == "/portfolio/fills":
            return httpx.Response(200, json={"fills": [
                {"count_fp": f"{n:.2f}", "yes_price_dollars": p, "fee_cost": fee,
                 "created_time": self.fill_time}
                for n, p, fee in self.fills]})
        if path.startswith("/markets/"):
            return httpx.Response(200, json={"market": {
                "exchange_index": 2, "no_ask_dollars": self.partner_no_ask,
                "yes_ask_dollars": "0.2000"}})
        return httpx.Response(404, json={})

    def sent(self, suffix):
        return [b for m, p, b in self.requests if p.endswith(suffix)]


def client_for(xc, key_path):
    c = ex.KalshiExecutionClient(BASE, "key", key_path)
    c.client = httpx.AsyncClient(transport=httpx.MockTransport(xc.handler))
    return c


LEGS = [cr.Leg("BTC", BTC, "UP", 0.75), cr.Leg("SOL", SOL, "DOWN", 0.80)]
# product 0.60; the cap is the cheaper leg, 0.75


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- the exchange

def test_a_quote_under_the_product_is_bought_at_base_size(key_path):
    xc = Exchange(quotes=[("0.4500", "open")])        # buys at 0.55 < 0.60
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2))
    assert res.bought and res.filled == 2.0 and res.price == 0.55
    rfq = xc.sent("/communications/rfqs")[0]
    assert rfq["contracts_fp"] == "2.00", "base size, never upsized"
    assert rfq["market_ticker"] == COMBO
    assert [(l["market_ticker"], l["side"]) for l in rfq["mve_selected_legs"]] == \
        [(BTC, "yes"), (SOL, "no")], "UP is yes, DOWN is no"
    assert rfq["mve_selected_legs"][0]["event_ticker"] == "KXBTC15M-26SEP271830"
    assert xc.sent("/accept") == [{"accepted_side": "no"}], \
        "a combo is BOUGHT by accepting the maker's NO bid"
    run(c.close())


def test_a_quote_above_the_cheaper_leg_is_refused(key_path):
    """A combo pays only if BOTH legs win, so it is never worth more than its
    cheaper leg. 0.80 for a pair whose cheaper leg is 0.75 is refused."""
    xc = Exchange(quotes=[("0.2000", "open")])        # 0.80 > 0.75
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2, wait_s=0.01))
    assert res.outcome == "none" and "no quote at or below 0.7500" in res.reason
    assert xc.sent("/accept") == []
    assert res.quotes_seen == [0.80]
    run(c.close())


def test_a_quote_above_the_product_but_under_the_cheaper_leg_is_bought(key_path):
    """Operator, 2026-09-27: "Same direct do not get rejected - combo accept
    any direct". Kalshi prices correlation in, so a same-direction pair is
    quoted ABOVE the legs' product (the app: BTC 76 + SOL 88 at 0.719 against
    0.669). The first cut capped at the product and refused all of them."""
    same = [cr.Leg("BTC", BTC, "UP", 0.76), cr.Leg("SOL", SOL, "UP", 0.88)]
    xc = Exchange(quotes=[("0.2810", "open")])        # 0.719
    c = client_for(xc, key_path)
    res = run(cr.buy(c, same, COMBO, 2))
    assert res.bought and res.limit == 0.76 and res.product == pytest.approx(0.6688)
    run(c.close())


def test_the_cheapest_valid_quote_is_taken(key_path):
    xc = Exchange(quotes=[("0.4200", "open"), ("0.4700", "open"), ("0.3000", "open")])
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2))
    assert res.quote_id == "q1" and res.bought
    run(c.close())


def test_the_not_quoting_placeholder_is_ignored(key_path):
    xc = Exchange(quotes=[("0.0010", "open")])
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2, wait_s=0.01))
    assert res.outcome == "none" and res.quotes_seen == []
    run(c.close())


def test_no_quote_at_all_buys_nothing(key_path):
    xc = Exchange(quotes=[])
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2, wait_s=0.01))
    assert res.outcome == "none" and "quotes seen: none" in res.reason
    run(c.close())


def test_a_maker_who_does_not_confirm_is_nothing_bought(key_path):
    xc = Exchange(confirm="cancelled")
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2))
    assert res.outcome == "none" and "did not confirm" in res.reason
    run(c.close())


def test_a_lost_accept_answer_is_unknown_not_nothing(key_path):
    xc = Exchange(accept_raises=True)
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2))
    assert res.outcome == "unknown"
    run(c.close())


@pytest.mark.parametrize("status", ["accepted", "confirmed", None])
def test_an_accept_still_pending_at_the_deadline_is_unknown(key_path, status):
    """Review finding (high): a quote still 'accepted'/'confirmed' when the
    confirmation window ran out was booked as NOTHING BOUGHT, and the single-
    leg order went out on top of a combo that could still execute."""
    xc = Exchange(confirm=status, fills=[])
    c = client_for(xc, key_path)
    assert run(cr.buy(c, LEGS, COMBO, 2)).outcome == "unknown"
    run(c.close())


def test_a_server_error_on_the_accept_is_unknown_a_refusal_is_nothing(key_path):
    xc = Exchange(accept_status=502)
    c = client_for(xc, key_path)
    assert run(cr.buy(c, LEGS, COMBO, 2)).outcome == "unknown"
    xc.accept_status = 409
    assert run(cr.buy(c, LEGS, COMBO, 2)).outcome == "none"
    run(c.close())


def test_only_fills_from_this_accept_are_counted(key_path):
    """The same combo market can already be held (the other instance buys
    BTC+SOL too); an older fill on the ticker is not this purchase."""
    xc = Exchange(fill_time="2001-01-01T00:00:00Z")
    c = client_for(xc, key_path)
    assert run(cr.buy(c, LEGS, COMBO, 2)).outcome == "unknown"
    run(c.close())


def test_executed_but_no_fill_readable_is_unknown(key_path):
    xc = Exchange(fills=[])
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2))
    assert res.outcome == "unknown", "money may have moved; send nothing else"
    run(c.close())


def test_verify_mode_never_accepts(key_path):
    xc = Exchange()
    c = client_for(xc, key_path)
    res = run(cr.buy(c, LEGS, COMBO, 2, accept=False))
    assert res.outcome == "none" and "would buy 2 at 0.5500" in res.reason
    assert xc.sent("/accept") == []
    run(c.close())


def test_the_price_ratio_setting_moves_the_limit(key_path):
    xc = Exchange(quotes=[("0.2400", "open")])        # 0.76 > 0.75
    c = client_for(xc, key_path)
    assert not run(cr.buy(c, LEGS, COMBO, 2, wait_s=0.01)).bought
    assert run(cr.buy(c, LEGS, COMBO, 2, max_ratio=1.02)).bought   # limit 0.765
    run(c.close())


# --------------------------------------------------------------- the partner

def decisions_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE intelligence_decisions (window_open INTEGER, "
                "decided_ms INTEGER, ticker TEXT, side TEXT, ask REAL)")
    con.executemany("INSERT INTO intelligence_decisions VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_the_partner_is_read_fresh_in_band_and_never_from_the_future(tmp_path):
    decisions_db(tmp_path / "sol15.db", [
        (W, W + 100_000, SOL, "DOWN", 0.80),
        (W, W + 400_000, SOL, "UP", 0.82),       # AFTER now: must not be used
    ])
    now = W + 150_000
    leg = cr.partner_now(tmp_path, "BTC", W, now)
    assert (leg.asset, leg.side, leg.ask) == ("SOL", "DOWN", 0.80)
    assert cr.partner_now(tmp_path, "BTC", W, W + 100_000 + 121_000) is None, "stale"
    assert cr.partner_now(tmp_path, "BTC", W + 900_000, now) is None, "other window"


@pytest.mark.parametrize("ask", [0.69, 0.86, 0.95])
def test_a_partner_outside_070_085_is_no_partner(tmp_path, ask):
    decisions_db(tmp_path / "sol15.db", [(W, W, SOL, "UP", ask)])
    assert cr.partner_now(tmp_path, "BTC", W, W + 1_000) is None


def test_sol_pairs_with_btc_and_nothing_else_pairs_with_itself(tmp_path):
    decisions_db(tmp_path / "btc15.db", [(W, W, BTC, "UP", 0.78)])
    assert cr.partner_now(tmp_path, "SOL", W, W + 1_000).asset == "BTC"
    assert LIVE_PARTNERS == {"ETH": ("SOL",), "SOL": ("BTC",)}
    assert "BTC" not in LIVE_PARTNERS and "GOLD" not in LIVE_PARTNERS
    assert cr.partner_now(tmp_path, "GOLD", W, W + 1_000) is None


def test_a_missing_or_broken_partner_store_is_no_partner(tmp_path):
    assert cr.partner_now(tmp_path, "BTC", W, W) is None
    (tmp_path / "sol15.db").write_text("not a database")
    assert cr.partner_now(tmp_path, "BTC", W, W) is None


# ------------------------------------------------------- the order path

class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send(self, text, buttons=None):
        self.sent.append(text)
        return True


class Mirrors:
    def __init__(self, client):
        self._primary = client
        self.dispatched = []

    def dispatch_combo(self, *a):
        self.dispatched.append(a)


def setup_order_path(tmp_path, key_path, xc):
    decisions_db(tmp_path / "sol15.db", [(W, W + 100_000, SOL, "DOWN", 0.80)])
    settings = Settings(database_path=str(tmp_path / "btc15.db"),
                        kalshi_series="KXBTC15M")
    store = Store(str(tmp_path / "btc15.db"))
    store.configure_instrument(settings)
    trader = Mirrors(client_for(xc, key_path))
    contract = SimpleNamespace(ticker=BTC, close_ms=W + 900_000)
    return settings, store, trader, contract


def place(settings, store, trader, contract, tg):
    """(stop, note) - stop means nothing else goes out this poll."""
    return run(main.place_combo_recovery(
        store, settings, tg, trader, contract, "UP", 0.75, 2, W, 480,
        W + 150_000))


def test_btc_recovery_never_touches_sol_since_09_28(tmp_path, key_path, monkeypatch):
    """Operator, 2026-09-28: "BTC AND GOLD ONLY I SAID". A SOL partner priced
    in band is RIGHT THERE, and still nothing is asked and nothing recorded:
    BTC's recovery goes out single-leg at base size."""
    monkeypatch.setattr(cr, "PARTNERS", LIVE_PARTNERS)
    xc = Exchange(quotes=[("0.4500", "open")])
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    stop, note = place(settings, store, trader, contract, FakeTelegram())
    assert stop is False
    assert note == "recovery due - no combo partner; single-leg at base size"
    assert xc.requests == [], "no RFQ, no market creation, nothing"
    assert store.db.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0
    run(trader._primary.close())


def test_a_bought_combo_is_the_windows_trade_and_is_recorded(tmp_path, key_path):
    xc = Exchange(quotes=[("0.4500", "open")])
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    tg = FakeTelegram()
    assert place(settings, store, trader, contract, tg)[0] is True
    row = store.db.execute(
        "SELECT strategy, ticker, status, count, fill_price, base_count, "
        "result_note FROM trade_proposals").fetchone()
    assert row[:6] == ("combo_recovery", COMBO, "filled", 2.0, 0.55, 2.0)
    note = json.loads(row[6])
    assert [(l["asset"], l["side"]) for l in note["legs"]] == [("BTC", "UP"), ("SOL", "DOWN")]
    # it spends the step, and it is a held position until the window closes
    assert store.upsized_since(W - 900_000) is True
    assert store.auto_state(W + 200_000)[4] == 1
    # the mirror is told, with the legs' product as her limit
    assert trader.dispatched and trader.dispatched[0][1] == COMBO
    assert trader.dispatched[0][2] == pytest.approx(0.75), "the cap, the cheaper leg"
    # and the operator is told, labelled
    assert tg.sent and "RECOVERY COMBO BOUGHT" in tg.sent[0]
    assert "BTC" in tg.sent[0].split("\n")[0]
    run(trader._primary.close())


def test_no_quote_defers_the_single_leg_entry_to_a_fresh_poll(tmp_path, key_path):
    """The quote wait can take half a minute. The single-leg entry must not go
    out now on that stale decision, crossing to the ceiling: this poll stops,
    and the next poll - one quote round per window - decides it afresh."""
    xc = Exchange(quotes=[])
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    settings = Settings(database_path=settings.database_path,
                        kalshi_series="KXBTC15M", combo_quote_wait_s=0.01)
    tg = FakeTelegram()
    assert place(settings, store, trader, contract, tg) == (True, "")
    assert store.db.execute("SELECT status FROM trade_proposals").fetchone()[0] == "unfilled"
    assert store.upsized_since(W - 900_000) is False, "nothing bought, nothing spent"
    assert store.combo_attempted(W) is True
    assert trader.dispatched == [] and tg.sent == []
    run(trader._primary.close())


def test_the_order_path_skips_a_second_quote_round_in_the_window():
    import inspect

    flat = " ".join(inspect.getsource(main.primary_signal).split())
    guard = flat.index("combo_status = store.combo_status(opened)")
    assert 'if combo_status is not None and combo_status != "unfilled": return' in flat
    skip = flat.index('if combo_due and combo_status == "unfilled":')
    assert guard < skip
    place_ = flat.index("stop, note = await place_combo_recovery(")
    assert skip < place_ and "if stop: return" in flat
    assert "size_reason = note or size_reason" in flat


def test_a_partner_whose_price_left_the_band_is_no_partner(tmp_path, key_path):
    """Chosen on a reading up to 120s old; re-read before it can set the cap."""
    xc = Exchange(partner_no_ask="0.9100")
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    stop, note = place(settings, store, trader, contract, FakeTelegram())
    assert stop is False and "no SOL partner" in note
    assert xc.sent("/communications/rfqs") == []
    run(trader._primary.close())


def test_an_unknown_outcome_is_held_and_nothing_else_is_sent(tmp_path, key_path):
    xc = Exchange(accept_raises=True)
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    tg = FakeTelegram()
    assert place(settings, store, trader, contract, tg)[0] is True, \
        "stop = no single-leg order on top of a combo that may exist"
    assert store.db.execute("SELECT status FROM trade_proposals").fetchone()[0] == "unprotected"
    assert store.auto_state(W + 200_000)[4] == 1, "the guard sees it as held"
    assert trader.dispatched == [], "the mirror never copies a maybe"
    assert "OUTCOME UNKNOWN" in tg.sent[0]
    run(trader._primary.close())


def test_no_partner_means_no_combo_and_no_row(tmp_path, key_path):
    xc = Exchange()
    settings = Settings(database_path=str(tmp_path / "btc15.db"),
                        kalshi_series="KXBTC15M")
    store = Store(str(tmp_path / "btc15.db"))
    trader = Mirrors(client_for(xc, key_path))
    contract = SimpleNamespace(ticker=BTC, close_ms=W + 900_000)
    stop, note = place(settings, store, trader, contract, FakeTelegram())
    assert stop is False and note.startswith("recovery due - no SOL partner")
    assert store.db.execute("SELECT COUNT(*) FROM trade_proposals").fetchone()[0] == 0
    assert xc.requests == [], "no partner: nothing is even asked"
    run(trader._primary.close())


# ------------------------------------------------------------- tracking

def combo_row(store, window=W, status="filled", ticker=COMBO):
    p = store.create_proposal("combo_recovery", window, ticker, "UP", 0.56, 0,
                              2, window + 60_000, window + 900_000, window,
                              base_count=2)
    store.record_fill(p.id, window, 0.56, 2, 0.03)
    store.finish_proposal(p.id, status, json.dumps({"legs": [
        {"asset": "BTC", "ticker": BTC, "side": "UP", "ask": 0.75},
        {"asset": "SOL", "ticker": SOL, "side": "DOWN", "ask": 0.80}]}))
    return p.id


def test_the_combo_counts_as_this_instruments_money(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    store.configure_instrument(SimpleNamespace(kalshi_series="KXBTC15M"))
    combo_row(store)
    store.record_realised(COMBO, W, -1.12, False, "exchange", W)
    store.record_realised("KXMVECROSSCATEGORY-OPERATORS", W, -5.0, False, "exchange", W)
    life = store.lifetime_record()
    assert life.markets == 1 and life.dollars == pytest.approx(-1.12)
    assert life.foreign_markets == 1 and life.foreign_dollars == pytest.approx(-5.0)


def test_open_positions_count_our_combo_but_not_the_operators():
    per = {BTC: 0.10, COMBO: -0.30, "KXMVECROSSCATEGORY-OPS": -2.89}
    assert main._scoped_open(per, {COMBO}) == (2, pytest.approx(-0.20))
    assert main._scoped_open(per) == (1, pytest.approx(0.10))


def test_the_combo_ledger_row_is_dated_by_its_trigger_window(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    combo_row(store)
    assert store._window_of(COMBO, 123) == W
    assert store._window_of("KXMVECROSSCATEGORY-UNKNOWN", 123) == 123


def test_the_local_loss_rebuild_leaves_combos_to_the_exchange(tmp_path):
    """`predictions.won` is the trigger leg's result; a combo whose trigger
    won and partner lost would be booked as a win. The exchange's figure is
    the one that counts for a combo."""
    store = Store(str(tmp_path / "btc15.db"))
    combo_row(store, window=W)
    sql = [r[0] for r in store.db.execute("SELECT strategy FROM trade_proposals")]
    assert sql == ["combo_recovery"]
    import inspect
    src = inspect.getsource(Store.auto_state)
    assert "t.strategy != 'combo_recovery'" in src


def test_the_window_recap_steps_aside_for_a_combo_and_the_combo_reports_once(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    settings = Settings(database_path=str(tmp_path / "btc15.db"),
                        kalshi_series="KXBTC15M")
    combo_row(store, window=W)
    tg = FakeTelegram()
    pending = (W, "UP", BTC, 0.75, 1, 64000.0)
    run(main.report_settlement(store, tg, pending, "yes", settings, now_ms=W + 1))
    assert tg.sent == [], "no 'not traded' recap for a window a combo traded"
    store.record_realised(COMBO, W, 0.85, True, "exchange", W + 900_000)
    now = W + 1_000_000
    run(main.report_combo_results(store, tg, settings, now))
    run(main.report_combo_results(store, tg, settings, now + 60_000))
    assert len(tg.sent) == 1 and "RECOVERY COMBO WON" in tg.sent[0]


def test_an_unfilled_combo_is_not_a_trade_anywhere(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    combo_row(store, status="unfilled")
    assert store.combo_tickers() == set()
    assert store.combo_for_window(W) is None
    assert store.upsized_since(W - 900_000) is False


# ------------------------------------------------------------- messages

def test_the_bought_message_states_legs_price_check_and_both_outcomes():
    res = cr.ComboResult("bought", "", market_ticker=COMBO, price=0.55,
                         filled=2, fee=0.03, product=0.60, limit=0.75)
    surface.set_instrument("")
    text = messages.combo_recovery_message(legs=LEGS, market=COMBO, result=res,
                                           contracts=2, remaining=480)
    assert text.split("\n")[0].startswith("<b>BTC</b>")
    assert "RECOVERY COMBO BOUGHT" in text
    assert "BTC UP" in text and "SOL DOWN" in text
    assert "Bought at 0.5500 · cap 0.7500 (the cheaper leg)" in text
    assert "if BOTH legs win" in text and "nothing upsized" in text


def test_the_result_message_carries_the_brokers_figure():
    legs = [{"asset": "BTC", "side": "UP", "ticker": BTC},
            {"asset": "SOL", "side": "DOWN", "ticker": SOL}]
    won = messages.combo_result_message(market=COMBO, legs=legs, contracts=2,
                                        price=0.55, pnl=0.87)
    lost = messages.combo_result_message(market=COMBO, legs=legs, contracts=2,
                                         price=0.55, pnl=-1.13)
    assert "RECOVERY COMBO WON" in won and "+$0.87" in won
    assert "RECOVERY COMBO LOST" in lost and "−$1.13" in lost   # a true minus sign


# --------------------------------------------------------------- mirror

def test_the_mirror_copies_the_combo_at_her_own_base(key_path, monkeypatch):
    from btc15_signal.mirror import MirrorTarget, _Mirror

    calls = []

    async def fake_buy(client, legs, market, count, **kw):
        calls.append((market, count, kw.get("max_ratio")))
        return cr.ComboResult("bought", "ok", market_ticker=market, price=0.55,
                              filled=count)

    monkeypatch.setattr(cr, "buy", fake_buy)
    logged = []
    m = _Mirror(MirrorTarget(name="m1", api_key_id="k", private_key_path=key_path,
                             base_budget=1.0, base_contracts=1, max_contracts=2),
                BASE, lambda *a: logged.append(a))
    run(m._apply("combo", {"legs": LEGS, "market": COMBO, "product": 0.60,
                           "max_ratio": 1.0, "wait_s": 1}))
    assert calls == [(COMBO, 1, 1.0)], "her base is 1 contract"
    assert logged[0][2] == "bought"
    run(m.client.close())


# ------------------------------------------------------- reconciliation

def fill(store, ticker, n, price, ms):
    store.db.execute(
        "INSERT INTO fills (fill_id, ticker, order_id, action, side, count, "
        "yes_price, no_price, fee_cost, is_taker, filled_ms, window_ms, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"f{ms}", ticker, "o", "buy", "yes", n, price, 1 - price, 0.02, 1, ms, W, ms))
    store.db.commit()


def test_an_unknown_combo_that_filled_is_booked_from_the_brokers_fills(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    pid = combo_row(store, status="unprotected")
    fill(store, COMBO, 2, 0.57, W + 5_000)
    out = store.reconcile_combos(W + 300_000)
    row = store.db.execute("SELECT status, count, fill_price FROM trade_proposals "
                           "WHERE id=?", (pid,)).fetchone()
    assert row == ("filled", 2.0, 0.57) and out == [f"{COMBO}: filled 2"]


def test_an_unknown_combo_with_no_fill_is_released_after_its_window(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    pid = combo_row(store, status="unprotected")
    assert store.reconcile_combos(W + 300_000) == [], "window still open: keep holding"
    store.reconcile_combos(W + 900_000 + 700_000)
    assert store.db.execute("SELECT status FROM trade_proposals WHERE id=?",
                            (pid,)).fetchone()[0] == "unfilled"
    assert store.combo_for_window(W) is None
    note = json.loads(store.db.execute("SELECT result_note FROM trade_proposals "
                                       "WHERE id=?", (pid,)).fetchone()[0])
    assert note["legs"] and "no fill at the broker" in note["reconciled"]


def test_ledger_books_a_combo_at_the_brokers_figure(tmp_path):
    store = Store(str(tmp_path / "btc15.db"))
    combo_row(store)
    store.record_realised(COMBO, W, -1.12, False, "exchange", W)
    rows = store.ledger()
    assert rows[-1]["how"] == "recovery combo" and rows[-1]["pnl"] == -1.12


def test_a_combo_in_any_state_but_unfilled_blocks_a_single_leg_on_top(tmp_path):
    """Review finding: a local write failing after a confirmed buy left the row
    'executing', which the position guard does not count - so the next poll
    could send a single leg on top of a real combo."""
    store = Store(str(tmp_path / "btc15.db"))
    for status in ("executing", "filled", "unprotected", "failed"):
        combo_row(store, window=W, status=status)
        assert store.combo_status(W) == status
    combo_row(store, window=W + 900_000, status="unfilled")
    assert store.combo_status(W + 900_000) == "unfilled"
    assert store.combo_status(W + 1_800_000) is None


# ------------------------------------------ closing the review's test gaps

def test_the_order_path_asks_for_exactly_the_base_count(tmp_path, key_path):
    """The fake fills report 2 whatever was asked, so the ASK is what is pinned."""
    xc = Exchange(quotes=[("0.4500", "open")])
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    place(settings, store, trader, contract, FakeTelegram())
    assert [b["contracts_fp"] for b in xc.sent("/communications/rfqs")] == ["2.00"]
    run(trader._primary.close())


def test_the_order_path_passes_the_price_cap_through(tmp_path, key_path):
    """A quote above the cheaper leg (0.75 here) must not be bought from the
    order path either - dropping the cap there would leave buy() tests green."""
    xc = Exchange(quotes=[("0.2000", "open")])                 # 0.80 > 0.75
    settings, store, trader, contract = setup_order_path(tmp_path, key_path, xc)
    settings = Settings(database_path=settings.database_path,
                        kalshi_series="KXBTC15M", combo_quote_wait_s=0.01)
    stop, _ = place(settings, store, trader, contract, FakeTelegram())
    assert stop is True and xc.sent("/accept") == []
    assert store.combo_status(W) == "unfilled"
    run(trader._primary.close())


def test_the_service_wires_combos_into_open_positions_and_the_result_sweep():
    import inspect

    source = inspect.getsource(main.service)
    assert source.count("_scoped_open(per_ticker, store.combo_tickers())") >= 1
    assert "_scoped_open(per_ticker)" not in source
    sweep = source[source.index("store.record_settlements(await trader.settlements(), now_ms)"):]
    assert "await report_combo_results(store, telegram, settings, now_ms)" in sweep[:200]


# ------------------------------------------------ funding the combo shard

class FundedExchange(Exchange):
    """Combo markets live in shard 1; balances and transfers are modelled."""

    def __init__(self, balances, **kw):
        super().__init__(**kw)
        self.balances = dict(balances)
        self.transfers = []

    def handler(self, request):
        path = request.url.path.replace("/trade-api/v2", "")
        if path == f"/markets/{COMBO}":
            self.requests.append((request.method, path, None))
            return httpx.Response(200, json={"market": {"exchange_index": 1}})
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance_breakdown": [
                {"exchange_index": k, "balance": f"{v:.4f}"}
                for k, v in self.balances.items()]})
        if path == ex.FUND_TRANSFER_PATH and request.method == "POST":
            body = json.loads(request.content)
            self.transfers.append(body)
            dollars = body["amount"] / 10000
            self.balances[body["source_exchange_shard"]] -= dollars
            self.balances[body["destination_exchange_shard"]] = \
                self.balances.get(body["destination_exchange_shard"], 0) + dollars
            return httpx.Response(200, json={"transfer_id": "t-1"})
        if path.startswith(ex.FUND_STATUS_PATH):
            return httpx.Response(200, json={"transfer": {"status": "complete"}})
        return super().handler(request)


def test_the_main_account_funds_the_combo_shard_before_asking(key_path):
    """Operator, 2026-09-27: "auto-fund combos". Shard 1 held $0.28; a 2-lot
    capped at 0.75 needs 2 x (0.75 + 0.02) = 1.54, so 1.26 moves from shard 0
    - BEFORE the RFQ, so a good quote is accepted without waiting on it."""
    xc = FundedExchange({0: 103.38, 1: 0.28, 2: 29.07})
    c = client_for(xc, key_path)
    assert c.auto_fund is False, "the main account's 15-minute orders stay unfunded"
    res = run(cr.buy(c, LEGS, COMBO, 2, fund=True))
    assert res.bought
    assert [(t["source_exchange_shard"], t["destination_exchange_shard"], t["amount"])
            for t in xc.transfers] == [(0, 1, 12600)]
    paths = [p for _, p, _ in xc.requests]
    assert paths.index(f"/markets/{COMBO}") < paths.index("/communications/rfqs")
    run(c.close())


def test_without_the_flag_nothing_moves(key_path):
    xc = FundedExchange({0: 103.38, 1: 0.28})
    c = client_for(xc, key_path)
    run(cr.buy(c, LEGS, COMBO, 2))
    assert xc.transfers == []
    run(c.close())


def test_a_funded_combo_shard_moves_nothing(key_path):
    xc = FundedExchange({0: 103.38, 1: 5.00})
    c = client_for(xc, key_path)
    run(cr.buy(c, LEGS, COMBO, 2, fund=True))
    assert xc.transfers == []
    run(c.close())


def test_the_order_path_funds_combos_by_default():
    import inspect

    assert Settings().combo_auto_fund is True
    flat = " ".join(inspect.getsource(main.place_combo_recovery).split())
    assert "fund=settings.combo_auto_fund" in flat
