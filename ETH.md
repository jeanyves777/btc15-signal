# ETH as a second instance

ETH runs as a **separate service process against its own database**, not as a
second loop inside the BTC service.

## Why a second process, not a second series

Every window-keyed query in `store.py` — 153 of them — assumes one series.
BTC and ETH windows close at the same instants, so they share `window_open`,
and `predictions` and `strategy_alerts` carry no ticker column at all. Two
series in one database would cross-attribute positions to the wrong
instrument, share the per-window alert throttle, and join the wrong
`predictions` row into the daily loss reconstruction.

Two processes share nothing. The trading path is unchanged and BTC is
untouched: an empty `BTC15_INSTANCE` keeps the original `runtime/` paths
exactly as they were.

## What differs from BTC, and why

| setting | BTC | ETH | reason |
|---|---|---|---|
| `KALSHI_SERIES` | KXBTC15M | KXETH15M | |
| `DATABASE_PATH` | btc15.db | eth15.db | full isolation |
| `KALSHI_STRATEGY_PATH` | strategy_kalshi.json | strategy_kalshi_eth.json | |
| `min_brti_normalized_distance` | 10.0 | **8.0** | measured on ETH |
| `BTC15_INSTANCE` | (unset) | eth | runtime-eth/ |

**The distance floor is 8x, not BTC's 10x.** A threshold is a statement about
one instrument's volatility distribution. Reusing 10x on ETH admits 1.8% of
markets against BTC's 4.0% — it selects a rarer tail, not the same setup.

Swept on ETH's own 6,395 settled markets, every floor from 4x to 10x cleared
both bars (day-clustered interval excluding zero, and a chronological split
whose out-of-sample half stayed positive). The edge is flat across that
range, so the threshold is not where the edge comes from. 8x was chosen
because it matches BTC operationally and statistically:

```
       markets   edge/trade   day-clustered CI        walk-forward test
BTC 10x   4.0%     +0.1088    [+0.0768, +0.1415]      +0.1016
ETH  8x   4.3%     +0.1034    [+0.0719, +0.1314]      +0.1046
ETH  4x  33.1%     +0.0714    [+0.0562, +0.0860]      +0.0786
```

A low floor does not ADD ETH to BTC. Each process holds one position at a
time, so a series qualifying 33% of the time would crowd the account with a
weaker edge. 8x keeps ETH as selective as BTC.

8x is the sweep maximum, which is recorded against it: its neighbours 7x
(+0.077) and 9x (+0.081) are both positive and clear both bars, so it is a
peak in a plateau rather than an isolated spike — but a swept maximum is
always the cell most likely to be flattered.

## Exposure: this is the operator's decision to make

Two processes do not share the account guards, because they do not share a
database. The practical effect:

* **two open positions** are possible, one per instrument, not one
* **two daily loss floors** of `AUTO_DAILY_LOSS_LIMIT` each — $40 total at
  the current $20, not $20
* trades-per-hour and per-day caps apply per instrument
* recovery, the deficit and the loss step are tracked per instrument

Set `AUTO_DAILY_LOSS_LIMIT=10` in both env files to keep the combined floor
at $20.

## What the backtest did NOT establish

* **The reversal gate is unvalidated on ETH.** Median retrace was 0.000 at
  minute granularity, so `max_brti_retrace` is carried over from BTC on
  faith. It only ever refuses, so the risk is missed trades, not bad ones.
* **The 60s band-hold timer is absent from the backtest**, and on BTC it is
  the single largest measured improvement in the deployed system.
* The harness returned +0.1088/trade for BTC against **+0.0193/contract
  live**. Absolute expectations should be scaled accordingly; ETH's ~+0.10
  through the same harness is not a forecast of +0.10 live.

## Running it

```powershell
# one-off, from the project root
$env:BTC15_INSTANCE = "eth"
.venv\Scripts\pythonw.exe scripts\run_service.py
```

Its runtime lives in `runtime-eth/` — own lock, own log, own pid. Verify the
same way as BTC: revision line timestamp, `runtime-eth/service.pid` matching
a process created after the start, and exactly one tree.

Roll back by stopping the ETH process. Nothing in the BTC instance changes.
