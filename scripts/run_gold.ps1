param([switch]$Supervise)

# Launch the GOLD instance (KXGOLD15M).
#
# The third instance. BTC keeps runtime/, ETH keeps runtime-eth/, and nothing
# here touches either: BTC15_INSTANCE is unset for BTC, so its paths are
# exactly what they were.
#
# CREDENTIALS ARE NOT DUPLICATED. pydantic-settings gives real environment
# variables precedence over the .env file, so shared secrets stay in the one
# .env and only per-instance values are set here. On 2026-09-24 three .env
# backups carrying live keys reached a public repo; a second credential file is
# the same mistake waiting to happen.
#
# WHY GOLD AT ALL, given BTC's rule rejects 99.4% of its setups: because the
# rejection was a measurement error, not a verdict. Gold's per-second reference
# jitters ~7x more than BRTI but that jitter mean-reverts - 4.0% of it survives
# to settlement against BTC's 30.5% - so `brti_normalized_distance` divides by
# noise and reads 0.83 where BTC reads 6.92, while the two are equivalent in
# risk-adjusted terms. Measured on gold's own scale the edge is larger than
# BTC's: +0.0987 residual against +0.0375. FINDINGS 73 has the full derivation
# and the tests that it survived, including a disjoint sub-period split.

Set-Location $PSScriptRoot\..

$env:BTC15_INSTANCE       = "gold"
$env:KALSHI_SERIES        = "KXGOLD15M"
$env:DATABASE_PATH        = "gold15.db"
$env:KALSHI_STRATEGY_PATH = "strategy_kalshi_gold.json"

# Own reference and ladder stores. Sharing either would mix gold observations
# into the BTC archive, which is the cross-contamination this design exists to
# avoid.
$env:REFERENCE_DATABASE_PATH = "runtime-gold/settlement_reference.db"
$env:HOURLY_DATABASE_PATH    = "runtime-gold/hourly.db"
# NEW YORK HOURS. gold closes at the New York close and reopens at the New
# York open, so it is shut every weekend for about two days - on 2026-09-25
# it went quiet at 21:00 UTC and the next market Kalshi listed opened 48
# hours later. This tells the service to ask the exchange when it reopens and
# stop polling and fetching the reference meanwhile. Declared per instrument
# because it is NOT inferable: BTC and SOL also list their next UNOPENED
# market hours out while trading normally.
$env:VENUE_HAS_SESSIONS      = "true"
$env:HOURLY_ENABLED          = "false"   # the ladder is a BTC product

# EXPOSURE. The three processes do not share account guards, so each carries
# its own daily loss floor. The combined floor was $20 across two instances at
# $10 each; a third instance at $10 would silently make it $30. Set to $7 so
# the three sum to $27 rather than $30 - and raise it deliberately, not by
# leaving this line out.
$env:AUTO_DAILY_LOSS_LIMIT = "7"
# LIVE TRADING, by the operator's decision of 2026-09-28 ("only gold and BTC
# are allowed to trade live"; FINDINGS 108). The evidence beside it: over 4
# days, gold's alerts made +5.9c a contract (131 signals, 77.9% won) and the
# trades its rules would have placed +3.6c (49); BTC+GOLD beat BTC alone on
# their shared days, and gold lost in only 10 of BTC's 33 losing alert windows.
# Every interval spans zero - 4 days is not proof. The stored flag was set ON
# with scripts/auto_switch.py and is what decides. The floor is scaled with
# the base (x base/2): $14 a day at base 4.
$env:AUTO_TRADE_ENABLED    = "true"

# EXACTLY ONE INSTANCE LISTENS FOR COMMANDS. Telegram getUpdates is
# destructive - it acknowledges with an offset - so three processes on one bot
# token would race for every message, including the kill switch. BTC keeps the
# command stream; gold still SENDS all of its alerts.
$env:TELEGRAM_COMMANDS_ENABLED = "false"

# ITS OWN INTELLIGENCE ARTEFACTS. These default to runtime/, which is BTC's,
# and the learning runner WRITES the policy path - so without this the gold
# instance would overwrite BTC's live policy at its first scheduled fit, and in
# the meantime would apply BTC-fitted arms to gold decisions. The arms are
# keyed on distance-price-momentum, strings that exist for every instrument, so
# neither failure would look wrong from outside.
$env:INTELLIGENCE_POLICY_PATH     = "runtime-gold/intelligence_policy.json"
$env:INTELLIGENCE_CANDIDATES_PATH = "runtime-gold/intelligence_candidates.json"

# ITS OWN CORPUS. `_corpus_mismatch` in learning_runner.py independently
# refuses any fit whose rows disagree with KALSHI_SERIES, so a wrong path here
# is caught rather than silently trained on.
# THE MERGED CORPUS, 2026-09-25. Re-fetching the reference series to
# capture brti_retrace landed on a near-disjoint set of markets, so the
# samples were unioned rather than one discarded. It is the same
# instrument throughout, and it is the only corpus carrying retrace -
# which the live rule gates on and no earlier fit could see.
$env:CORPUS_BRTI_PATH   = "data/brti_history_gold_merged.db"
$env:CORPUS_MARKET_PATH = "data/market_data_kxgold15m.db"

# LEARNING ON, with one caveat recorded rather than hidden: gold's corpus is
# 584 of 3,907 settled markets, because `live_data` serves only recent events.
# That is enough to fit on and NOT enough to be confident the fit generalises,
# so the promotion bar matters more here than on BTC. It is the same bar -
# Holm-Bonferroni at FWER 0.05 on a chronological validation slice - and it has
# never promoted anything on any instrument.
$env:LEARNING_ENABLED = "true"

New-Item -ItemType Directory -Force runtime-gold | Out-Null
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
