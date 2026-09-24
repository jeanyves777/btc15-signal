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

# EXACTLY ONE INSTANCE LISTENS FOR COMMANDS. Telegram getUpdates is
# destructive - it acknowledges with an offset - so two processes on one bot
# token race for every message, and the one they race for could be the kill
# switch. BTC keeps the command stream; ETH still SENDS all of its alerts.
#
# To control this instance, set its auto state in eth15.db directly, or give
# it its own bot token and turn this back on.
$env:TELEGRAM_COMMANDS_ENABLED = "false"

New-Item -ItemType Directory -Force runtime-eth | Out-Null
& .venv\Scripts\pythonw.exe scripts\run_service.py
