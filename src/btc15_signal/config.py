from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    symbol: str = "BTCUSDT"
    spot_base_url: str = "https://data-api.binance.vision"
    futures_base_url: str = "https://fapi.binance.com"
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_series: str = "KXBTC15M"
    poll_seconds: int = 10
    entry_seconds_remaining: int = 300
    entry_tolerance_seconds: int = 20
    # How long an Execute button stays live. Twenty seconds is fine for an
    # automated press but far too short for a human who has to pick up a phone.
    # A longer window is safe because the press re-checks the ticker and the
    # live ask before submitting and rejects the order if either moved - the
    # expiry is a convenience, the price check is the actual guard.
    proposal_seconds: int = 150
    # The non-return edge is concentrated well before the old 5-minute trigger:
    # measured over 68 days it is negative at 3 minutes left and positive from
    # about 6. Scan the whole window and take the first minute that qualifies.
    #
    # These MUST match the window every measurement uses, which is
    # `6 <= remaining <= 11` in `scripts/compare_series.py`. Backtest snapshots
    # sit on exact minute boundaries (`remaining = 15 - elapsed`), so that is
    # 360-660 seconds. They were 630/330 - half a minute adrift at both ends -
    # so the bot was not running the rule that was measured: it acted in a
    # 330-359s band nothing had ever been tested in, and stopped 30s before the
    # tested range ended. Measured 6-11: +0.0220 [+0.0077, +0.0360] on 1,359
    # entries. If these change, re-measure; do not let them drift again.
    entry_from_seconds: int = 660  # start looking with 11 minutes left
    entry_to_seconds: int = 360  # stop looking at 6 minutes left
    # OFF, and it should stay off. Measured on 5,753 paired historical trades
    # (same entries, exit vs hold): exiting costs -$0.0162/contract, 95% CI
    # [-0.0233, -0.0089]. It fired on 50% of trades and was WORSE than holding
    # in 79% of them, averaging -$0.0325 each time it fired - against a gross
    # entry edge of about +$0.017. It was destroying the whole edge.
    #
    # Every variant was tested and every one lost to holding: requiring the
    # cross to persist 2 or 3 minutes, requiring it to clear the strike by
    # 5-40bps, and only cutting while the bid was still 0.55 or 0.70. The best
    # of them still lost $0.011/contract.
    #
    # The reason is not a bad threshold, it is arithmetic. The contract price is
    # the market's own probability, so selling at the market is EV-neutral
    # before costs by optional stopping - the same argument validation.py uses
    # to set zero as the null. An exit therefore cannot add expected value; it
    # can only pay the spread a second time and a second fee. The only thing it
    # buys is lower variance, which is worth nothing at $1 a trade.
    exit_on_reversal: bool = False
    # Offer the Execute button whenever the setup qualifies, even before the
    # rule is auto-validated. Every order still needs the Telegram press, so
    # this is the manual stage on the way to automation, not a bypass of it.
    # Local LLM commentary. Reads the most recent order-book snapshot the
    # recorder captured and writes a few sentences about it, sent as a
    # follow-up AFTER the alert. Purely descriptive: it never gates, sizes or
    # vetoes a trade, and if the runtime is down nothing else changes.
    brain_enabled: bool = True
    brain_url: str = "http://127.0.0.1:8080/v1"
    brain_model: str = "local"
    brain_timeout_s: float = 240.0
    microstructure_path: str = "data/microstructure.db"
    manual_execution_enabled: bool = True
    # Floor for offering a manual override button. The measured edge lives at
    # 0.85 and up; 0.80 leaves room for a near-miss like 0.84 to still be a
    # judgement call, while a 0.64 setup - flat to the strike, no measured edge
    # either way - gets logged for the record but offers nothing to press.
    # This buffer is discretion, not a validated band.
    manual_min_ask: float = 0.65
    # How much above the quoted price an entry order may pay. Orders posted at
    # exactly the touch missed 40% of the time - an immediate-or-cancel only
    # fills if the resting size survives the round trip. A limit still fills at
    # the best available price, so this is a ceiling, not a cost: it is paid
    # only when the book moved, in cases that were otherwise no trade at all.
    # The price must have held inside the band this long before we buy.
    #
    # Measured over 3,394 markets: entering on the FIRST qualifying minute gives
    # +0.0080/contract, 95% CI [-0.0019, +0.0180] - it does not clear zero. The
    # same rule after two minutes in the band gives +0.0219 [+0.0090, +0.0347].
    # It halves the number of markets and nearly triples the edge, which is the
    # better trade: 1,674 x 0.0219 beats 3,394 x 0.0080 in total as well as per
    # trade. Three and four minutes score higher still but on far fewer markets.
    #
    # The mechanism is plain in the live alerts: the price walks up from the 60s,
    # clips the band, and we buy into a market that has not agreed on a price -
    # which is also why those orders so often fail to fill.
    # 60, not 120. The entry window is 660-360s - a 300-second span - so a
    # 120s hold could only ever complete for a price already in the band by
    # 480s remaining, leaving the last two minutes of every window dead.
    # KXBTC15M-26SEP211445-45 on 2026-09-21 reached the band at 422s, held,
    # showed five green ticks and was refused to the cutoff.
    #
    # Measured together over 71 days (`scripts/measure_window_settle.py`),
    # paired on markets, which is the test section 16 says to run:
    #   settle 60s   n=3841  +0.0149/ct [+0.0032, +0.0260]  total +57.42
    #   settle 120s  n=2681  +0.0147/ct [+0.0012, +0.0278]  total +39.53
    #   difference   +0.0002/ct [-0.0082, +0.0084], p=0.479
    # Indistinguishable per contract, +43% trades, 1,160 fewer dead setups.
    # Dropping the settle entirely does NOT clear zero at any window bound,
    # so the rule stays - it is only half as long.
    entry_band_settle_s: int = 60
    # The separate LLM commentary message. Off since 2026-09-21: the entry
    # alert now carries the checks, the context, the confidence arithmetic
    # and the similar-regime read, so the second message repeated it.
    brain_commentary_enabled: bool = False
    # THE INTELLIGENCE LAYER'S AUTHORITY. Three values, and the default is the
    # only one that is safe without evidence:
    #
    #   shadow - infer, record, grade. NEVER touches an order. (default)
    #   assist - may adjust confidence and entry TIMING, never the decision to
    #            trade and never the size. Requires a passed promotion report.
    #   live   - may return ENTER NOW / WAIT / PASS as the decision. Requires a
    #            SECOND explicit authorisation on top of the assist review.
    #
    # Nothing in the code promotes this. It is changed by a person, having read
    # `scripts/promotion_report.py`, and `intelligence_authorised` must be set
    # in the same breath - two independent switches, so a single stray edit or
    # a copied .env cannot hand a shadow model control of real money.
    intelligence_mode: str = "shadow"
    intelligence_authorised: bool = False
    # How far above the quoted ask the entry limit is set. An IOC limit fills
    # at the BEST AVAILABLE price, never at the limit - our own fills prove it
    # (limit 0.87 filled 0.84, limit 0.80 filled 0.75, limit 0.81 filled 0.76)
    # - so a wider allowance costs nothing on an order that would have filled
    # anyway. It only spends when the book actually moved, which is precisely
    # when the old 1c allowance bought nothing at all.
    #
    # Measured over the signed ask drift across the ~1.93s submit lag, at
    # qualifying polls: 1c covered 89.1%, 3c covered 99.1%, 5c covered 100.0%
    # of every move recorded (max observed drift 4.13c).
    entry_slippage: float = 0.05
    # And a hard ceiling, because "take whatever price" has a floor of sanity:
    # above 0.93 every measured bucket's interval includes zero, and on
    # 2026-09-21 a retry chain chased 0.85 to 0.963. The limit may cross the
    # book; it may not cross out of the region where an edge was ever shown.
    max_entry_price: float = 0.95
    # How many entry orders one window may attempt. An immediate-or-cancel that
    # does not fill costs nothing, so a single miss should not end an
    # opportunity that is still valid - but each retry re-runs every gate at the
    # new price, and this caps how far a running market can be followed.
    auto_retry_limit: int = 3
    # ZERO. A retry may never pay more than the first order did.
    #
    # This was an 8c "chase allowance" and that was the wrong idea. Observing
    # costs nothing and a window has ten minutes in it, so there is no reason to
    # pay up: if the price runs away, wait to see whether it comes back, and
    # accept that some markets leave without us. Missing a winner is better than
    # risking 96c to make 4c.
    #
    #   85c  first order, unfilled
    #   92c  observe, no order
    #   98c  observe, no order
    #   89c  still worse than 85c, wait
    #   84c  at or below the first price -> second attempt allowed
    auto_retry_max_drift: float = 0.0
    # Time to let the book settle after a failed order. Firing again on the very
    # next poll re-reads the same disturbed book and misses for the same reason.
    auto_retry_cooldown_s: int = 30
    # Basis for the P&L shown in alerts and the dashboard.
    #
    # Kalshi sizes a position by MAX PAYOUT, not by cash spent: a "$10 position"
    # is 10 contracts, and since each pays $1 the cost is 10 x price. At 86.8c
    # that is $8.68 at risk, not $10 - confirmed against a real closed position
    # showing COST $8.68 / MAX PAYOUT $10.00.
    #
    # This sizes the PAPER figure only - the per-signal number covering every
    # settled signal, traded or not. Real money is reported separately from the
    # orders that actually filled and never passes through here.
    #
    # "contracts"- exactly trade_contract_count contracts, what the bot orders
    # "payout"   - report_payout dollars of max payout (the Kalshi convention)
    # "cash"     - spend dashboard_stake dollars
    #
    # Defaults to one contract: the unit the edge was measured in and the unit
    # a $1 budget actually buys. It was "payout" (ten contracts) left over from
    # paper testing, which reported ten times the real money on every message.
    report_basis: str = "contracts"
    report_payout: float = 10.0
    dashboard_stake: float = 1.0
    exit_min_seconds: int = 60  # never chase an exit inside the last minute
    # Cash out once the position has already earned nearly everything it can.
    #
    # This is the opposite rule from exit_on_reversal and measures far better,
    # but it still does not make money: on the same 5,753 paired trades,
    # banking 90% of the available profit came to -$0.0006/contract against
    # holding - six hundredths of a cent, inside the noise. Cashing out earlier
    # costs real money: -$0.0075 at a 0.95 bid, -$0.0127 at 0.90.
    #
    # It cannot beat holding, for two reasons. The spread plus a second fee is
    # a fixed toll of about a cent. And the favourite-longshot edge means a
    # contract bid at 0.97 is genuinely worth nearer 0.98, so selling it is
    # selling cheap. What it buys is certainty: banking 90% of the profit
    # instead of risking the whole stake for the last 10%.
    cash_out_enabled: bool = True
    cash_out_capture: float = 0.90  # fraction of the available profit to bank
    cash_out_min_bid: float = 0.90  # and never sell into a thin, low bid
    # How far BELOW the quoted bid a cash-out is priced and judged. Entries
    # have `entry_slippage` because an immediate-or-cancel order at exactly the
    # touch only fills if that quote is real and still there - on 2026-09-21
    # three entries missed that way. Exits had no equivalent, so the cash-out
    # on KXBTC15M-26SEP211400-00 was submitted AT a quoted 0.979 bid, filled
    # nothing, and the Kalshi app was offering 0.93 at that moment. Discounting
    # first makes the order marketable AND stops the capture test firing on a
    # price that is not there: at paid=0.76 the gate needs 0.976, and
    # 0.979 - 0.01 = 0.969 correctly declines.
    exit_slippage: float = 0.01
    # The mirror of `max_entry_price`. A sell IOC fills at the BEST AVAILABLE
    # bid, not at its limit, so pricing the exit AT the quoted bid meant a
    # stale or thin top-of-book killed it - exactly the entry defect, in
    # reverse. KXBTC15M-26SEP211700-00 held UP bought at 0.72, the quote said
    # 0.98, the order went out at 0.98 and filled nothing.
    #
    # The exit now crosses DOWN to this floor and takes whatever real bid is
    # there above it. `cash_out_min_bid` (0.90) already refuses to even try
    # below this level, so the floor cannot sell into a collapse - it only
    # stops the order being priced at a number nobody is actually bidding.
    min_exit_price: float = 0.90
    min_calibration_samples: int = 100
    min_win_probability: float = 0.80
    min_raw_probability: float = 0.50
    validated_lower_probability: float = 0.8290
    min_contract_edge: float = 0.03
    entry_alerts_enabled: bool = True
    # Reversion is measured at -$0.042/contract over 68 days. It keeps recording
    # so the verdict can be revisited with evidence, but it sends no alert and
    # offers no button while the main strategy is still being proven.
    reversion_alerts_enabled: bool = False
    strategy_path: str = "strategy.json"
    reversion_strategy_path: str = "reversion_strategy.json"
    max_spread_bps: float = 2.0
    database_path: str = "btc15.db"
    # --- hourly strike ladder (KXBTCD), SHADOW ONLY ----------------------
    # The hourly series is a ladder of ~188 "or above" thresholds sharing one
    # settlement time, not a single contract. It has never been measured, so it
    # records and does not trade. `hourly_trading_enabled` is read by nothing:
    # it exists so that turning hourly trading on is a code change someone has
    # to make deliberately, not a config flag someone can flip by accident.
    # SETTLEMENT REFERENCE RECORDER (FINDINGS 40). Shadow only.
    #
    # The contract settles on the average of sixty CF Benchmarks BRTI prices in
    # the final minute - confirmed from Kalshi's own `rules_primary`, not
    # assumed - while every decision this bot makes reads Binance spot. Section
    # 40 measured the combined gap at a median 6 bps and a 40.5% outcome flip
    # inside 5 bps of the strike, but compared one Binance minute-close against
    # a 60-second average and so could not say how much was the FEED and how
    # much was the AVERAGING. This recorder separates them.
    #
    # It records and nothing else. There is deliberately no flag here that
    # turns any of it into a trading input: promoting it has to be a code
    # change someone makes on purpose, the same reasoning as
    # `hourly_trading_enabled`.
    reference_enabled: bool = True
    reference_database_path: str = "runtime/settlement_reference.db"
    # Fast enough that a 60-second mean has real samples in it, slow enough to
    # stay off the 10-second trading beat.
    reference_poll_seconds: float = 5.0
    # Beyond this the value describes a market that has already moved, so the
    # row is marked stale rather than averaged in as if it were current.
    #
    # 3,000 ms was wrong and the live smoke test caught it: Kalshi's BRTI
    # series runs 2-3 seconds behind wall clock by nature, so a 3-second
    # threshold marks perfectly good official data stale and discards its
    # price. A recorder that throws away the reference it exists to record is
    # worse than one that records it late. 15,000 ms flags a feed that has
    # genuinely stopped while leaving normal publication lag alone.
    reference_stale_ms: int = 15_000
    reference_reconcile_seconds: float = 300.0
    reference_debug: bool = False
    # CF Benchmarks gates index values behind an entitlement; with no key the
    # values endpoint answers "Unknown id" for every ticker. Absent a key the
    # recorder writes `missing` rows with the reason and keeps the official
    # 60-second averages coming from Kalshi. It never substitutes an exchange.
    cfb_base_url: str = "https://www.cfbenchmarks.com/api/v1"
    cfb_index_id: str = "BRTI"
    cfb_api_key: str = ""

    hourly_enabled: bool = True
    hourly_trading_enabled: bool = False
    hourly_series: str = "KXBTCD"
    hourly_database_path: str = "runtime/hourly.db"
    # Slower than the 15-minute poll: an hour-long window does not need 12s
    # resolution, and each poll writes ~50 rows instead of one.
    hourly_poll_seconds: float = 60.0
    # Archive rungs within this many dollars of spot. At $100 spacing that is
    # ~51 rungs. The far tails are pinned at 0.00/0.01 and carry no
    # information; the chain row records how many were left out.
    hourly_archive_window: float = 2500.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_authorized_user_id: int = 0
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    execution_enabled: bool = False
    # Dollar budget per order, not a contract count. These are only the
    # defaults: /size and /autosize change them from Telegram at runtime and
    # the stored value wins, so sizing never needs a restart.
    manual_budget: float = 1.0
    auto_budget: float = 1.0
    # --- unattended trading ---------------------------------------------
    # Auto mode places orders with nobody watching, so every limit here is a
    # hard stop, not a preference. All are overridable from Telegram EXCEPT the
    # kill switch, which can only ever be turned off from there, never on.
    auto_trade_enabled: bool = False
    # Stop for the day once down this much. Raised 10 -> 20 alongside
    # confidence sizing: a 2-contract loss is about $1.87 against $0.93 at one
    # contract, so the old floor tripped after roughly half as many bad trades
    # and would have stopped a day that the strategy was merely having a normal
    # losing run in. A floor that stops trading early is not a safe floor, it
    # is a silent stop - the failure mode that has already cost this system
    # four hours once. The measured max drawdown at 2 contracts is -37.05 over
    # 68 days, so this is a day limit, not a strategy limit.
    auto_daily_loss_limit: float = 20.0
    auto_max_trades_per_day: int = 40
    auto_max_trades_per_hour: int = 6
    auto_min_seconds_between: int = 120
    max_budget: float = 100.0  # ceiling for /size, a guard against a fat finger
    trade_contract_count: int = 1
    # CONFIDENCE SIZING. The edge is not flat across the distance gate: it
    # peaks between 2x and 4x volatility and decays above, because a very
    # distant strike is already priced for the safety it offers.
    #
    # Measured on 3,841 deployed entries over 68 days, momentum aligned:
    #   all entries          +0.0149/ct [+0.0032, +0.0260]
    #   distance 2.0-4.0x    +0.0359/ct [+0.0166, +0.0541]   n=1282, 33%
    #   everything else      +0.0157/ct [+0.0023, +0.0286]
    #
    # So size up where the edge is 2.4x, and only there. Above 4x the edge
    # fades (5x+ measures +0.0064), which is why this is a BAND and not a
    # floor - the old ">= 3x is better" reading had it backwards.
    #
    # OFF since 2026-09-22, by the operator's decision. Intelligence must not
    # change size: the band doubled exposure on
    # KXBTC15M-26SEP221330-30 ("3.0x vol is inside the measured 2-4x edge
    # band", 2 contracts at 81c) and the market settled against us for -$1.64
    # instead of about -$0.82. The measurement above is not withdrawn, but the
    # distance it was measured on is Binance-derived, and FINDINGS 41/43 show
    # that quantity disagrees with Kalshi's official BRTI reference on about
    # 20% of markets - so the evidence behind the band is itself in question.
    # Sizing returns to one contract until it has independent evidence.
    #
    # This flag gates the BAND ONLY. `high_confidence_contracts` is left alone
    # because the loss-recovery path reads it too, and recovery is the
    # operator's separate decision.
    confidence_sizing_enabled: bool = False
    high_confidence_contracts: int = 2
    # LOSS RECOVERY, the operator's 2026-09-22 rule. The deficit is realised
    # net dollars still missing; it is divided across this many upsized trades
    # to get the share one trade has to be able to win before the upsize is
    # allowed to apply at all. Four is the operator's own worked example:
    # $1.64 over 4 trades is $0.41 a trade, which 2 contracts at 75c can cover
    # and 2 at 90c cannot.
    #
    # It is a plan length, not a limit: recovery ends when the money is back,
    # not when the steps run out, and the divisor floors at 1.
    recovery_steps: int = 4
    # OFF. Recovery buys no larger BASE position; it acts only through the
    # conditional add-on, which rests ONE extra contract 2c below the actual
    # fill and only while the BRTI evidence holds. With both on they stack:
    # two contracts upfront plus a third resting behind them, for a deficit
    # that justified one. The base entry is one contract whether or not a
    # deficit is outstanding.
    recovery_upfront_upsize_enabled: bool = False

    # THE CONDITIONAL RECOVERY ADD-ON (recovery_add.py).
    #
    # Live-test authorisation: $30 total, and `recovery_test_budget` is a
    # CUMULATIVE spend ceiling, not a concurrent-exposure one. It counts every
    # dollar the add-on has ever committed and does not reset on a loss, a new
    # day or a restart - a cap that resets is not a cap, it is a per-episode
    # allowance that can be spent repeatedly.
    recovery_add_enabled: bool = False
    # The TESTING ACCOUNT size, not a lifetime spend cap: what the add-on may
    # have committed at any one moment, checked against live cash and live
    # exposure before every order.
    recovery_add_test_budget: float = 30.0
    recovery_add_dip: float = 0.02  # rest this far below the ACTUAL fill
    recovery_add_min_seconds: int = 120  # add-entry deadline before close
    recovery_add_distance_floor: float = 10.0  # BRTI normalized distance
    recovery_add_max_contracts: int = 1  # per position, on top of the base

    # THE DAILY SIZING CONTROLLER (capital.py). One authority for base entries
    # and recovery adds alike.
    #
    # The base tier changes ONLY at the daily review, from reconciled settled
    # cash - never from an open position's mark, because sizing on unrealised
    # gains compounds exposure exactly when a position is most likely to give
    # them back. $30 of capital per contract matches the authorised test
    # account: one contract now, two if the account doubles, and never more
    # than `max_base_contracts` whatever the balance says.
    #
    # The day is NEW YORK because that is the exchange's own reset - Kalshi
    # documents its utilisation caps resetting at midnight New York time - and
    # a system keeping books on a different day from its venue will file trades
    # in the wrong one twice a year at the DST boundaries.
    capital_sizing_enabled: bool = True
    capital_per_contract: float = 30.0
    max_base_contracts: int = 2

    high_confidence_distance_min: float = 2.0
    high_confidence_distance_max: float = 4.0
    dry_run: bool = True
