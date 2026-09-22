"""Turning recorded recommendations into money, honestly.

Kept in the package rather than in the report script because these three rules
are the difference between a promotion report and a sales pitch, and a rule
that lives in a script is a rule nothing tests.

  * AN UNFILLED ORDER IS NO TRADE. Not a simulated win, not a simulated loss.
    Counting one manufactures edge out of an order that never existed, and it
    flatters in a specific direction: the orders that fail to fill are the ones
    where the book moved away, which correlates with the trade going wrong.
  * AN EARLY EXIT IS SCORED AT WHAT IT REALISED. Scoring it by who eventually
    won credits back a loss already taken - and the reverse, a position sold at
    100c on a market that later settled against us is a profit.
  * THE FEE IS KALSHI'S FEE. `kalshi_fee_charged` is the published formula,
    used wherever a real `fee_cost` was not recorded.
"""

from __future__ import annotations

from .validation import kalshi_fee_charged


def decision_pnl(row: dict) -> float | None:
    """Net dollars per contract for one recorded ENTER NOW, or None.

    None means "this is not an executable outcome" - a recommendation that was
    not ENTER NOW, an order that did not fill, or a market with no result yet.
    It is not zero: a zero would enter the average and drag it toward nothing,
    which is a claim about a trade that never happened.
    """
    if row.get("action") != "ENTER NOW":
        return None
    filled = row.get("filled")
    if filled is not None and not filled:
        return None                      # no fill, no trade
    realised = row.get("realised_pnl")
    if realised is not None:
        return float(realised)           # early exits, at what they realised
    ask = row.get("ask")
    won = row.get("won")
    if ask is None or won is None:
        return None
    charged = row.get("fee_cost")
    if charged is None:
        charged = kalshi_fee_charged(float(ask), 1)
    return (1.0 if won else 0.0) - float(ask) - float(charged)


def executable_pnl(rows: list[dict]) -> list[float]:
    """Every executable outcome in `rows`, in order."""
    out = []
    for row in rows:
        value = decision_pnl(row)
        if value is not None:
            out.append(value)
    return out


def drawdown(pnl: list[float]) -> float:
    """Worst peak-to-trough on the equity curve these decisions would produce."""
    equity = peak = trough = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        trough = min(trough, equity - peak)
    return trough
