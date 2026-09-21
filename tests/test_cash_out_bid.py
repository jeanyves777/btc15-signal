"""The cash-out must judge itself on a price it could actually get.

KXBTC15M-26SEP211400-00 on 2026-09-21: bought DOWN at 0.76, the quoted NO bid
read 0.979, the capture gate needed 0.976, so it fired - and the
immediate-or-cancel sell filled nothing, because the Kalshi app was offering
0.93 to cash out at that moment. The quote was 5c better than anything
executable, which is the 1-10c book-to-quote offset FINDINGS section 7 records
as unresolved.

Entries already had `entry_slippage` for exactly this reason: an IOC at the
touch only fills if the quote is real and still there. Exits had nothing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btc15_signal.config import Settings  # noqa: E402
from btc15_signal.kalshi import KalshiMarket  # noqa: E402
from btc15_signal.messages import cash_out  # noqa: E402


def market(yes_ask: float, no_ask: float, yes_bid: float, no_bid: float) -> KalshiMarket:
    return KalshiMarket(
        ticker="KXBTC15M-26SEP211400-00", target=85_910.29, open_ms=0, close_ms=1,
        yes_ask=yes_ask, no_ask=no_ask, yes_bid=yes_bid, no_bid=no_bid,
    )


def would_fire(paid: float, quoted_bid: float, settings: Settings) -> bool:
    """The deployed gate, as `cash_out_exit` applies it."""
    bid = round(quoted_bid - settings.exit_slippage, 4)
    if not 0.0 < bid < 1.0 or bid < settings.cash_out_min_bid:
        return False
    available = 1.0 - paid
    return available > 0 and (bid - paid) >= settings.cash_out_capture * available


def test_the_real_bid_is_read_from_the_quote_not_inferred():
    """`no_bid = 1 - yes_ask` is arithmetically right on Kalshi, but carrying
    the real field means a fabricated number can no longer masquerade as one."""
    quote = market(yes_ask=0.021, no_ask=0.98, yes_bid=0.02, no_bid=0.979)
    assert quote.bid("DOWN") == 0.979
    assert quote.bid("UP") == 0.02
    assert quote.ask("DOWN") == 0.98


def test_the_trade_that_fired_on_a_phantom_price_now_declines():
    settings = Settings()
    # Exactly the live numbers. Undiscounted it clears the gate by 0.003 -
    # which is how it came to sell into a bid that was not there.
    assert settings.cash_out_capture * (1.0 - 0.76) <= (0.979 - 0.76)
    assert not would_fire(paid=0.76, quoted_bid=0.979, settings=settings)


def test_a_genuinely_rich_bid_still_cashes_out():
    """The fix must not simply switch the feature off."""
    settings = Settings()
    assert would_fire(paid=0.76, quoted_bid=0.995, settings=settings)


def test_a_failed_cash_out_does_not_claim_to_have_banked_anything():
    text = cash_out(
        head="h", ticker="KXBTC15M-26SEP211400-00", side="DOWN", paid=0.76,
        bid=0.98, count=1, captured=0.91, remaining=211,
        note="No bid at 98%; holding to settlement", sold=False,
    )
    assert "STILL HOLDING" in text
    assert "Banked" not in text, "a miss must not report a sale"
    assert "Nothing sold" in text
    assert "rides to" in text


def test_a_real_cash_out_still_reports_what_it_banked():
    text = cash_out(
        head="h", ticker="KXBTC15M-26SEP211400-00", side="DOWN", paid=0.76,
        bid=0.98, count=1, captured=0.91, remaining=211,
        note="Sold 1 at 98%", sold=True,
    )
    assert "CASHED OUT EARLY" in text
    assert "Banked" in text
