param([switch]$Supervise)

# Launch the BNB instance (KXBNB15M) - SHADOW ONLY (operator, 2026-10-01: "in
# the shadow tracking signal and life circle let add BNB"). It records the
# lifecycle, signals and outcomes like ETH/SOL/XRP/NEAR and NEVER trades: auto,
# main, $ and mirror switches are stored OFF in bnb15.db, AUTO_TRADE_ENABLED is
# false here, and BNB is in neither ALLSIGNAL_INSTRUMENTS nor MIRROR_INSTANCES.
#
# CREDENTIALS ARE NOT DUPLICATED: real environment variables override the one
# .env, so only per-instance values are set here.

Set-Location $PSScriptRoot\..

$env:BTC15_INSTANCE       = "bnb"
$env:KALSHI_SERIES        = "KXBNB15M"
$env:DATABASE_PATH        = "bnb15.db"
$env:KALSHI_STRATEGY_PATH = "strategy_kalshi_bnb.json"

# Own reference and ladder stores - nothing shared with another instrument.
$env:REFERENCE_DATABASE_PATH = "runtime-bnb/settlement_reference.db"
$env:HOURLY_DATABASE_PATH    = "runtime-bnb/hourly.db"
$env:HOURLY_ENABLED          = "false"   # the ladder is a BTC product

# NEVER TRADES. The stored rows in bnb15.db decide and are OFF; this is the
# default under them.
$env:AUTO_TRADE_ENABLED    = "false"
$env:AUTO_DAILY_LOSS_LIMIT = "5"
$env:TELEGRAM_COMMANDS_ENABLED = "false"
$env:INTELLIGENCE_POLICY_PATH     = "runtime-bnb/intelligence_policy.json"
$env:INTELLIGENCE_CANDIDATES_PATH = "runtime-bnb/intelligence_candidates.json"
# NO BNB HISTORY YET: learning is OFF until a corpus exists. The paths name
# BNB's own (future) files, never another instrument's.
$env:CORPUS_BRTI_PATH   = "data/brti_history_bnb.db"
$env:CORPUS_MARKET_PATH = "data/market_data_kxbnb15m.db"
$env:LEARNING_ENABLED = "false"

New-Item -ItemType Directory -Force runtime-bnb | Out-Null
if ($Supervise) {
    & .venv\Scripts\pythonw.exe scripts\watchdog.py
} else {
    & .venv\Scripts\pythonw.exe scripts\run_service.py
}
