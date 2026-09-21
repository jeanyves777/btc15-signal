# BTC15 Signal

Paper-first BTC 15-minute Kalshi signal service with two candidate strategies,
Telegram alerts, and one-press manual order authorization.

**Status: a small positive edge that one tick of slippage erases.** Both rule
files stay disabled. Real-money test, $10 staked on every qualifying signal for
68 days, Kalshi's actual per-order fees, entry scanned from 10 minutes out:

| exit policy | trades | staked | net | ROI | $/day | max DD | 95% CI on net |
|---|---:|---:|---:|---:|---:|---:|---:|
| hold to expiry | 3,183 | $31,830 | $140 | 0.44% | $2.06 | $233 | -$267 to +$551 |
| **exit on reversal** | 3,183 | $31,830 | **$216** | 0.68% | $3.18 | **$116** | -$113 to +$541 |

Exiting when BTC crosses back through the strike adds ~54% more profit and
halves the drawdown - but no policy's interval clears zero (best p = 0.10).

**Slippage decides everything.** Break-even is about 0.6 cents:

| slippage | net | ROI |
|---|---:|---:|
| $0.000 | +$216 | 0.68% |
| $0.005 | +$42 | 0.13% |
| $0.010 | **-$122** | -0.38% |

Underneath it is a real favourite-longshot bias: longshots at 20-30c are
significantly overpriced, favourites at 85-99c mildly underpriced (+$0.0105 per
contract net over 6-11 minutes remaining, 95% CI +$0.0012 to +$0.0189). The edge
is negative at 3 minutes left, which is why the old 5-minute trigger sat in dead
space. It lives entirely on the UP side; the DOWN side is flat.

Grid search cannot find it: the best of 11,365 rules scores below the *median*
best rule found on shuffled data (p = 0.99). Only rules fixed in advance recover
it, which is why the nightly optimiser no longer writes `strategy.json`.

See [Validation](#validation) and `reports/VALIDATION_REPORT.md`.

## Strategies

1. **Primary non-return:** near five minutes remaining, evaluate whether BTC is likely
   to remain on its current side of the exact Kalshi target through settlement.
2. **Spike reversion:** with 12 to 10 minutes remaining, detect a BTC spike through the
   window target, require rejection from the observed high/low, and consider the opposite
   contract only when its executable ask is 30-35 cents.

The reversion entry attaches a reduce-only take-profit limit after the entry fills.
Kalshi's native bracket exit-trigger API is for margin/perpetual products; event contracts
use a normal opposite limit order. If take-profit placement fails after an entry fill,
Telegram sends an urgent UNPROTECTED warning.

No strategy can guarantee an 80% win rate. A 30-35% value in a reversion alert is the
contract entry price, not a proven win probability.

## Safety

- Both deployed rule files remain disabled until chronological train, validation, and
  untouched holdout gates pass.
- Every real order requires an inline Telegram **Execute** press from the configured
  Telegram user ID.
- The callback carries only a one-time proposal ID. It never contains credentials.
- Before submitting, the service rechecks the ticker, deadline, and live ask.
- Entries use immediate-or-cancel; stale unfilled quantity is canceled.
- Reversion exits are reduce-only and expire just before the market closes.
- SQLite atomically claims each proposal, preventing duplicate execution.
- Kalshi private keys stay in a local file and are never accepted through Telegram.

## Install

```bash
cd btc15-signal
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Set the Telegram bot token, chat ID, and authorized Telegram user ID in .env. Keep
DRY_RUN=true and EXECUTION_ENABLED=false until paper verification is complete.

Configure Kalshi credentials locally:

```bash
python scripts/configure_kalshi.py
```

The script stores the API key ID and private-key file path in the ignored .env file,
sets restrictive file permissions, and requires typing ENABLE before live execution
is switched on. In Telegram, /keys or /status reports configuration status without
showing either credential.

Start the service:

```bash
btc15-signal
```

## Telegram execution

Every message opens with the running scoreboard - win rate, W-L, P&L and a bar -
so the record is visible without scrolling back. Telegram has no text colour, so
state is carried by emoji chips (green live, blue manual, white pass, red exit,
tick/cross on settlement) and by its HTML subset for weight and monospace.
Outcomes are always written in words as well, never signalled by icon alone.

```
🎯 80% win rate · 12W-3L · +$2.80
▰▰▰▰▰▰▰▰▱▱ 15 settled
————————————————
🔵 MANUAL ENTRY · KXBTC15M-26SEP202015-15
📈 BTC UP at 91%
🎯 Target $81,158.49 · now $81,494.65 (+41 bps)
⏱ 7m 12s to settle
🧮 Model 96% · observed 80% (5 samples)
⚠️ Not auto-validated - your press is the decision.
      [ ✅ Execute 1 ]   [ ⏭ Skip ]
```

The **Execute** button appears whenever the setup qualifies, not only once the
rule is auto-validated: `enabled` governs automation, while a human may always
take the trade. A press is still required for every order, so the safety gate is
unchanged - this is the manual stage before full auto
(`manual_execution_enabled`).

After **Execute**, the bot revalidates the market and submits the disclosed order only.
For DOWN, the V2 single YES book is mapped to an economically equivalent YES ask; the
take-profit reverses that mapping to close the position.

## Validation

Validation is judged on **gross edge** — fees are excluded from every pass/fail gate.
A strategy with no edge before costs can never be rescued by a better fee tier, so
that question is settled first. Fee-adjusted figures are reported alongside, as
information rather than as a criterion.

The measure is mean realised P&L per contract, scored on **the exit each strategy
actually defines** — not on holding to settlement:

```
pnl    = payoff - entry_price
payoff = take_profit   if the strategy's exit would have filled before close
       = 1 if the market settled the strategy's way, else 0
```

Reversion buys at 30-35c intending to sell at a take-profit, so it is scored on that
sale; settlement only decides the trades whose offer never filled. The non-return
strategy really does hold to expiry, so for it the two definitions coincide. Scoring a
take-profit strategy at settlement measures a strategy nobody trades.

Zero is the null either way. Under an efficient market the contract price is a
martingale, so by optional stopping *every* exit rule has zero expected P&L — a
contract bought at its true probability and sold at any stopping time breaks even.
This is also why win rate alone was never enough: an 84% win rate bought at 90c is a
losing strategy, and the old reports gated on win rate.

```bash
python scripts/sync_data.py --days 70     # fills data/market_data.db, resumable
btc15-validate                            # writes reports/VALIDATION_REPORT.md
btc15-intel                               # queries the accumulated ledger
btc15-profit-test --stake 10              # dollars, not edge per contract
btc15-dashboard                           # reports/dashboard.html + terminal summary
```

### Performance dashboard

`btc15-dashboard` reads the service's own database - every alert it sent and how
each settled - and writes a self-contained HTML page (no server, no external
assets, light and dark). Re-run it to refresh.

It shows net P&L and return on stake, the record with its 95% floor, an equity
curve, per-day bars, and splits by side, entry-price band, and whether the rule
qualified or it was paper only. Gains and losses use a blue/red diverging pair
rather than the obvious green/red, which fails colour-vision separation
(deutan dE 4.1 against an 8 floor), and every outcome is stated in text as well.

Until the record passes ~100 settled signals it carries a standing warning: at
an 85c average entry a fair market wins ~85% of the time anyway, so a winning
streak is not evidence. The page prints how many signals are actually needed.

### What the validator does

- **Walk-forward.** Refits the whole rule grid on everything before each fold, trades
  the fold, and moves on. The concatenated fold trades measure the *selection
  procedure*, not a single rule chosen with hindsight over the full sample.
- **Moving-block bootstrap.** Confidence intervals that survive the serial correlation
  between neighbouring 15-minute windows, which an i.i.d. Wilson interval on win rate
  ignores.
- **Permutation test.** Outcomes are shuffled within 5-cent price buckets, preserving
  the market's own price/probability calibration and destroying only the link between
  features and result. The best edge the entire grid finds on shuffled data is the
  null, so a rule must beat not just chance but the best of several thousand chances.
- **Baselines.** Always-yes, always-favourite and coin-flip on the same markets. A
  calibrated market prices all three at zero edge; they are the bar.
- **Regime findings** are corrected at a 10% false-discovery rate across every bucket
  tested, because slicing a sample many ways always produces a best slice.

### Trade lifecycle

Every triggered trade is reconstructed minute by minute from entry to close, using the
side of the book the position must actually hit (long YES exits at the yes bid, long NO
at the no bid). That gives, per trade, the best and worst price it could genuinely have
been closed at, the first minute it was worth N cents, and a replay of take-profit,
stop-loss, trailing and timed exits over the identical path. The question it exists to
answer: **how many losing trades were ever winning, and could an early exit have taken
that profit instead of holding to expiry?**

### Intelligence ledger

`data/intelligence.db` accumulates across runs: every decision snapshot with its
features and regime labels, every simulated trade, the full price path of each trade,
every exit-policy result and every regime finding. Runs are timestamped and keep their
parameters and verdict, so later questions can be asked of earlier data.

```bash
btc15-intel --show runs,findings
btc15-intel --path KXBTC15M-26SEP201915-15    # one trade, minute by minute
```

### Legacy backtests

These predate the validator and gate on win rate after a flat fee buffer, which is
why they read as "close but for fees". They are kept for comparison only; the gross
edge numbers above supersede them.

```bash
pytest -q
ruff check .
btc15-backtest --days 180            # Binance-only directional accuracy
btc15-kalshi-backtest --days 30
btc15-reversion-backtest --days 30
```

## Continuous operation

Docker:

```bash
docker compose up -d --build
docker compose logs -f signal
```

The optimizer container reruns both Kalshi-aware strategies daily and atomically updates
their rule files. If Docker is used for live execution, the private-key file path must be
mounted read-only into the container.

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1
```

This installs the signal service plus daily primary and reversion optimizer tasks.

## Data and limitations

Live features use Binance book ticker, depth, aggregate trades, one-minute candles, and
optional futures basis. Kalshi supplies the exact target, contract asks, timestamps, and
settlement result.

Minute-candle backtests cannot prove queue position or guarantee that every displayed
price would fill. Binance prices may differ from the contract's settlement oracle, and
the configured fee buffer is not an account-specific fee calculation.
