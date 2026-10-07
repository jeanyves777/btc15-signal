param([switch]$Supervise)

# Launch the SOL instance (KXNEAR15M).
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
# WHY SOL AT ALL, given BTC's rule rejects 99.4% of its setups: because the
# rejection was a measurement error, not a verdict. Gold's per-second reference
# jitters ~7x more than BRTI but that jitter mean-reverts - 4.0% of it survives
# to settlement against BTC's 30.5% - so `brti_normalized_distance` divides by
# noise and reads 0.83 where BTC reads 6.92, while the two are equivalent in
# risk-adjusted terms. Measured on sol's own scale the edge is larger than
# BTC's: +0.0987 residual against +0.0375. FINDINGS 73 has the full derivation
# and the tests that it survived, including a disjoint sub-period split.

Set-Location $PSScriptRoot\..

$env:BTC15_INSTANCE       = "near"
$env:KALSHI_SERIES        = "KXNEAR15M"
$env:DATABASE_PATH        = "near15.db"
$env:KALSHI_STRATEGY_PATH = "strategy_kalshi_sol.json"

# Own reference and ladder stores. Sharing either would mix sol observations
# into the BTC archive, which is the cross-contamination this design exists to
# avoid.
$env:REFERENCE_DATABASE_PATH = "runtime-near/settlement_reference.db"
$env:HOURLY_DATABASE_PATH    = "runtime-near/hourly.db"
$env:HOURLY_ENABLED          = "false"   # the ladder is a BTC product

# EXPOSURE. The three processes do not share account guards, so each carries
# its own daily loss floor. The combined floor was $20 across two instances at
# EXPOSURE. Each instance carries its own daily loss floor because the
# processes share no account guard. NEAR's is set but unreachable while
# automation is off; it exists so arming it later cannot leave it without
# one. The five-instance aggregate is the operator's to set, not this file's
# to assume - the arithmetic that used to be repeated here was copied from
# gold's launcher and was wrong for every instrument that inherited it.
# SHADOW ONLY - IT RECORDS AND NEVER TRADES. Automation is OFF, deliberately
# and by default, and the reason is the backtest rather than caution. NEAR is the
# WEAKEST of the six instruments measured:
#
#   Its UNGATED calibration residual is NEGATIVE - -0.0022 [-0.0253, +0.0206] at
#   a 0.758 mean ask over 1,066 markets and 46 days. Taking every setup loses
#   money before fees, which no other instrument here does (gold +0.0346,
#   XRP +0.0130, SOL +0.0013). Of 19,440 complete candidate gate sets only 50
#   cleared the 15% volume floor and the baseline - XRP managed 673 - and NOT ONE
#   reached tier A or B.
#
#   The best set reads +0.0192 [-0.0182, +0.0558], spanning zero. Its first
#   disjoint half is NEGATIVE (-0.0145) and its second HOLDS (+0.0697) - the
#   opposite shape from silver and SOL, and no more trustworthy: a result carried
#   entirely by the later half is either a regime that has just begun or noise
#   that will revert, and 23 days cannot tell those apart.
#
# So there is nothing here to trade on. What the instance is FOR is the record:
# live per-second reference data and a gate set admitting 15.2% of decision
# points, which no backtest can reconstruct because Kalshi serves no historical
# order-book depth.
#
# TO TURN IT ON LATER, both of these, deliberately:
#     $env:AUTO_TRADE_ENABLED = "true"
#     python scripts/auto_switch.py --db near15.db --on
# The STORED row wins over this default, so a restart cannot arm it by itself.
# The Telegram kill switch does NOT reach this instance - only BTC consumes
# getUpdates - so the off switch is:
#     python scripts/auto_switch.py --db near15.db --off
#
# The daily loss floor is set even though nothing can trade, so arming it later
# cannot leave the account without one.
$env:AUTO_DAILY_LOSS_LIMIT = "5"

# EXACTLY ONE INSTANCE LISTENS FOR COMMANDS. Telegram getUpdates is
# destructive - it acknowledges with an offset - so three processes on one bot
# token would race for every message, including the kill switch. BTC keeps the
# command stream; near still SENDS all of its alerts.
$env:TELEGRAM_COMMANDS_ENABLED = "false"

# ITS OWN INTELLIGENCE ARTEFACTS. These default to runtime/, which is BTC's,
# and the learning runner WRITES the policy path - so without this the sol
# instance would overwrite BTC's live policy at its first scheduled fit, and in
# the meantime would apply BTC-fitted arms to sol decisions. The arms are
# keyed on distance-price-momentum, strings that exist for every instrument, so
# neither failure would look wrong from outside.
$env:INTELLIGENCE_POLICY_PATH     = "runtime-near/intelligence_policy.json"
$env:INTELLIGENCE_CANDIDATES_PATH = "runtime-near/intelligence_candidates.json"

# ITS OWN CORPUS. `_corpus_mismatch` in learning_runner.py independently
# refuses any fit whose rows disagree with KALSHI_SERIES, so a wrong path here
# is caught rather than silently trained on.
# THE MERGED CORPUS, 2026-09-25. Re-fetching the reference series to
# capture brti_retrace landed on a near-disjoint set of markets, so the
# samples were unioned rather than one discarded. It is the same
# instrument throughout, and it is the only corpus carrying retrace -
# which the live rule gates on and no earlier fit could see.
$env:CORPUS_BRTI_PATH   = "data/brti_history_near.db"
$env:CORPUS_MARKET_PATH = "data/market_data_kxnear15m.db"

# LEARNING ON, on a corpus that carries every gate the live rule reads.
# `brti_retrace` and `brti_choppiness` were computed and then discarded by the
# backfill until 2026-09-25, so fits before that scored candidates as though two
# live gates were absent (FINDINGS 75). 5,715 of NEAR's 6,396 points carry
# retrace, and the fit CHOSE to require it - the second instrument to do so,
# after XRP.
#
# The promotion bar is unchanged - Holm-Bonferroni at FWER 0.05 on a
# chronological validation slice, plus no contradicting forward evidence - and it
# has never promoted anything on any instrument. With a NEGATIVE ungated baseline
# to start from, that bar matters more here, not less.
$env:LEARNING_ENABLED = "true"

New-Item -ItemType Directory -Force runtime-near | Out-Null
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
