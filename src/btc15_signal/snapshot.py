"""The market snapshot shape the strategy and its backtests read.

THIS USED TO LIVE IN `binance.py`, BESIDE THE CLIENT THAT FETCHED IT, and that
is the whole reason this module exists. The dataclass is a neutral container -
prices, momentum, volatility - and nothing about it is Binance. But while it
sat in that file, every module that needed the shape imported it FROM THERE,
so eight live modules imported a module
named for a feed the system had already stopped using, and a reader tracing
the code could not tell a shape from a source.

The fields carry NO feed in their names on purpose. Under Kalshi-only they are
populated from BRTI and the Kalshi book; the fields are what a snapshot IS,
not where it came from. If a second source is ever added, it does not get to
reuse these names without saying which one it is - the mislabelling that cost
this system FINDINGS 42, 47 and 49 all began with a number whose provenance
was implied rather than stated.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSnapshot:
    price: float
    target: float
    bid_imbalance: float
    taker_imbalance: float
    momentum_5m_bps: float
    volatility_5m_bps: float
    futures_basis_bps: float
    spread_bps: float
    window_high: float = 0.0
    window_low: float = 0.0
    elapsed_minutes: int = 0
