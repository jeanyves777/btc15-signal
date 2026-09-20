# BTC15 Signal

Paper-first BTC 15-minute Kalshi signal service with two independently validated
strategies, Telegram alerts, and one-press manual order authorization.

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

Qualified alerts contain the exact ticker, strategy, UP/DOWN side, entry limit, quantity,
take-profit when applicable, deadline, and **Execute**/**Skip** buttons.

After **Execute**, the bot revalidates the market and submits the disclosed order only.
For DOWN, the V2 single YES book is mapped to an economically equivalent YES ask; the
take-profit reverses that mapping to close the position.

## Verification and backtests

```bash
pytest -q
ruff check .
btc15-backtest --days 180
btc15-kalshi-backtest --days 30
btc15-reversion-backtest --days 30
```

The reversion test joins Binance one-minute candles to actual Kalshi contract candles,
simulates an executable contract bid reaching take-profit, applies a two-cent fee/slippage
buffer, and otherwise settles the position using the recorded market result.

Latest reversion result: 2,847 settled markets and 8,533 decision snapshots over 30 days.
The baseline produced 443 entries, a 58.01% take-profit rate, and **-16.90% buffered ROI**.
No optimized rule passed the validation gates, so reversion_strategy.json remains
enabled: false. Full results are in reports/REVERSION_BACKTEST_REPORT.md and
reports/reversion-backtest.json.

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
