param([switch]$Supervise)

# Launch the ETH instance.
#
# CREDENTIALS ARE NOT DUPLICATED. pydantic-settings gives real environment
# variables precedence over the .env file, so the shared secrets stay in the
# one .env and only the per-instance values are set here. On 2026-09-24 three
# .env backups carrying live keys reached a public repo; a second credential
# file is the same mistake waiting to happen.
#
# Everything this sets is already a Settings field. Nothing in the trading
# path changes, and the BTC instance does not move: BTC15_INSTANCE is unset
# for it, which keeps its original runtime/ paths exactly.

Set-Location $PSScriptRoot\..

$env:BTC15_INSTANCE       = "eth"
$env:KALSHI_SERIES        = "KXETH15M"
$env:DATABASE_PATH        = "eth15.db"
$env:KALSHI_STRATEGY_PATH = "strategy_kalshi_eth.json"

# Own reference and ladder stores. Sharing either would mix ETH observations
# into the BTC archive, which is the cross-contamination this whole design
# exists to avoid.
$env:REFERENCE_DATABASE_PATH = "runtime-eth/settlement_reference.db"
$env:HOURLY_DATABASE_PATH    = "runtime-eth/hourly.db"
$env:HOURLY_ENABLED          = "false"   # the ladder is a BTC product

# EXPOSURE. The two processes do not share account guards, so each carries its
# own daily loss floor and its own one-position limit. Halved here so the
# COMBINED floor stays at the $20 the operator set, rather than silently
# becoming $40. Raise deliberately, not by leaving this out.
$env:AUTO_DAILY_LOSS_LIMIT = "10"
# BACK TO SHADOW, by the operator's decision of 2026-09-28 ("only gold and
# BTC are allowed to trade live"; FINDINGS 108). Live record at the time:
# -$12.31 over 141 trades (fee-free, combos included), -4.4c a contract, and it
# lost in 54 of BTC's 89 losing alert windows - it deepened BTC's bad windows
# rather than offsetting them. The stored flag was set OFF with
# scripts/auto_switch.py; nothing here sets AUTO_TRADE_ENABLED.

# EXACTLY ONE INSTANCE LISTENS FOR COMMANDS. Telegram getUpdates is
# destructive - it acknowledges with an offset - so two processes on one bot
# token race for every message, and the one they race for could be the kill
# switch. BTC keeps the command stream; ETH still SENDS all of its alerts.
#
# To control this instance, set its auto state in eth15.db directly, or give
# it its own bot token and turn this back on.
$env:TELEGRAM_COMMANDS_ENABLED = "false"

# ITS OWN INTELLIGENCE ARTEFACTS. These default to runtime/, which is BTC's.
# The learning runner WRITES the policy path, so without this the ETH
# instance would have overwritten BTC's live policy at its first scheduled
# fit - and in the meantime was reading BTC-fitted arms and applying them to
# ETH decisions. Neither would have looked wrong: the arms are keyed on
# distance-price-momentum, strings that exist for both instruments.
$env:INTELLIGENCE_POLICY_PATH     = "runtime-eth/intelligence_policy.json"
$env:INTELLIGENCE_CANDIDATES_PATH = "runtime-eth/intelligence_candidates.json"

# ITS OWN CORPUS. These defaulted to BTC inside learning_data - not settings
# at all - so every instance fitted on BTC whatever it traded.
# data/brti_history_eth.db is 7,674 BRTI decision points across 1,279 settled
# ETH markets, backfilled from the same /live_data/events endpoint the live
# path reads, at the same per-second resolution.
#
# `_corpus_mismatch` in learning_runner.py independently refuses any fit whose
# rows disagree with KALSHI_SERIES, so a wrong path here is caught rather than
# silently trained on.
$env:CORPUS_BRTI_PATH   = "data/brti_history_eth.db"
$env:CORPUS_MARKET_PATH = "data/market_data_kxeth15m.db"

# Learning ON: ETH now has an instrument-correct corpus to fit.
$env:LEARNING_ENABLED = "true"

New-Item -ItemType Directory -Force runtime-eth | Out-Null
# SUPERVISED OR ONE-SHOT. With -Supervise this hands off to watchdog.py
# instead of the service directly, and watchdog.py relaunches it whenever
# it exits. The environment above is already set and watchdog.py runs
# run_service.py as a CHILD, so the instance variables are inherited -
# which is why the switch lives here rather than in a second script. A
# copied env block is how one instance ends up writing another's database
# (FINDINGS 43, 63).
#
# The boot+logon scheduled task passes -Supervise. Run it bare to start
# the instance by hand.
if ($Supervise) {
    & .venv\Scripts\pythonw.exe scripts\watchdog.py
} else {
    & .venv\Scripts\pythonw.exe scripts\run_service.py
}
