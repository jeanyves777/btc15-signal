from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # NO SECOND PRICE SOURCE. The Binance spot and futures endpoints that used
    # to sit here are gone, along with the client that read them: a URL in a
    # settings file is a URL somebody can turn back on, and this system has
    # already shipped a retired Binance artefact answering live decisions
    # (FINDINGS 49). Kalshi serves the book, the executions, the settlements
    # and BRTI itself, so there is nothing left for a second feed to do.
    symbol: str = "BTCUSDT"
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
    # How long the exchange may list NO market before the operator is told.
    # A normal window boundary is seconds; on 2026-09-24 Kalshi listed nothing
    # for TWO HOURS and the only symptom was Telegram going quiet, which is
    # the silent stop this system is most exposed to.
    market_gap_alert_s: int = 600
    # SESSION HOURS, TAKEN FROM THE VENUE. Gold and silver keep New York hours:
    # they close at the New York close and reopen at the New York open, so they
    # are shut every weekend for about two days. On 2026-09-25 both went quiet
    # at 21:00 UTC and the next market Kalshi listed opened 53 hours later.
    #
    # A closure is NOT the outage `market_gap_alert_s` exists to catch, and
    # must not be reported as one - a false alarm on a Friday night teaches the
    # operator to ignore the alert that matters. It is told apart by asking
    # Kalshi when the series next lists a market: far away means closed, soon
    # or unknown means keep worrying.
    #
    # WHICH INSTRUMENTS OBSERVE SESSIONS AT ALL, declared rather than guessed.
    # Off by default, and set only on the metals. "The next unopened market is
    # far away" looked like sufficient evidence of a closure and is NOT: Kalshi
    # creates markets in daily batches, so on 2026-09-25 BTC and SOL both
    # reported their next UNOPENED market 5.1 hours out while trading normally,
    # because the near-term ones were already OPEN and an `unopened` listing
    # excludes those. Inferring a closure from that would have backed off a
    # 24/7 instrument during an outage and silenced the alert built for it -
    # the exact failure the two-hour gap of 2026-09-24 exists to catch.
    #
    # So the instrument declares that it has sessions and the EXCHANGE supplies
    # the reopen time. No hardcoded New York calendar, and nothing to misfire
    # on BTC, ETH or SOL.
    venue_has_sessions: bool = False
    # `venue_closed_after_s` is how long no open market may last before that
    # question is asked at all. A window boundary is seconds, so ten minutes
    # never triggers on a 24/7 instrument.
    venue_closed_after_s: int = 600
    # How far ahead the next market must be to count as a closure rather than
    # a boundary. Kalshi creates markets in daily batches, so on a 24/7 series
    # the next UNOPENED market can legitimately be a day out while trading is
    # continuous - which is why this is only consulted once there has already
    # been no open market for `venue_closed_after_s`.
    venue_closed_gap_s: int = 1800
    # The poll interval while closed. Not the whole closure: sleeping 53 hours
    # would miss an early reopen, a config change and the settlement sweep.
    # Ten minutes cuts a weekend from ~19,000 requests to ~320 and still
    # notices a reopen within ten minutes.
    venue_closed_poll_seconds: int = 600
    # THE REVERSAL GATE. Refuse an entry standing on a move that has already
    # turned over. Shipped as a BLOCKING gate on the operator's explicit
    # instruction, with the evidence recorded beside it in FINDINGS 58:
    # 79 markets, stable entries 85.5% against reversed 70-75%, separation
    # +13.3% at this window and threshold - and every band's interval overlaps
    # every other, with the separation collapsing at a 180s window. The
    # mechanism is sound and the parameters are not yet established.
    # CHOPPINESS. Confidence only, by the operator's instruction - it has no
    # threshold and appears in no gate. `choppiness_penalty` is on the same
    # 0-100 point scale `confidence_label` works in, applied in proportion to
    # how choppy the window was, and clamped with everything else.
    choppiness_window_s: int = 900
    choppiness_penalty: int = 15
    reversal_window_s: int = 120
    reversal_max_retrace: float = 0.60
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
    # THE ABSOLUTE TRIGGER, operator 2026-09-24: "that's already at max profit
    # ... no need to wait for expiry". Compared against the DISCOUNTED bid, so
    # 0.98 here means a quoted 0.99.
    #
    # It exists because `cash_out_capture` alone is unreachable on an
    # expensive entry: banking 90% of the profit above `paid` needs a quoted
    # bid of 0.10*paid + 0.91, which passes 0.999 once paid exceeds 0.89. The
    # live record shows the consequence - entries under 0.89 exit 33-58% of
    # the time, entries above it 4.2% (1 of 24), and a position quoted 99.9%
    # on a 0.895 entry needed a 0.9995 bid that does not exist.
    #
    # Measured over 146 cases that reached a 0.99 quoted bid: selling returns
    # 0.9938/contract against 0.9932 for holding. That is noise, the same as
    # the proportional rule's -$0.0006, and it is shipped for the same reason
    # - it buys certainty rather than money. At a 0.995 bid holding is very
    # slightly ahead (1.0000 vs 0.9968 over 140 cases, no losses), so this is
    # deliberately NOT set higher.
    cash_out_at_bid: float = 0.98
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
    # KALSHI ONLY. Quotes, books, executions, settlements and BRTI all come
    # from Kalshi; the Binance client is not constructed, the Binance-weighted
    # `predict()` does not run, and there is NO fallback - a missing or stale
    # reference is recorded as such and produces no signal.
    #
    # It is a setting rather than a deletion so the old path stays runnable
    # for the historical comparison in `scripts/`, not so it can be switched
    # back on in production. The Binance-trained policy is separately retired
    # and cannot act whatever this says.
    kalshi_only: bool = True
    # The BRTI-native entry rule. Its distance floor is the MEASURED 10x
    # (FINDINGS 43), not the 1.5 that belongs to Binance raw volatility.
    kalshi_strategy_path: str = "strategy_kalshi.json"
    # Contract spread, in CENTS. `max_spread_bps = 2.0` gated Binance SPOT
    # spread, whose 99th percentile over 10,094 archived observations is
    # 0.001 bps - it never rejected anything. Reusing it on a Kalshi book
    # would reject EVERYTHING (a 2c spread on a 79c mid is 253 bps). Measured
    # contract spreads: median 0.4c, p75 3c, p90 7c, p95 10c, p99 19c, so
    # this sits at ~p99 and keeps catching only a pathological book.
    max_contract_spread_cents: float = 20.0
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
    # WHO LISTENS FOR COMMANDS. Telegram's getUpdates is DESTRUCTIVE: it
    # acknowledges with an offset, so whichever process polls first consumes
    # the update and every other process never sees it. Two instances sharing
    # one bot token therefore race for every command - including the kill
    # switch, which is the one message that must never be lost.
    #
    # So exactly one instance listens. The BTC service keeps the default and
    # is unchanged; a second instance sets this false and still SENDS its
    # alerts, it just does not consume the command stream. Controlling that
    # instance is then a deliberate act rather than a coin toss - see ETH.md.
    telegram_commands_enabled: bool = True
    # WHO SENDS ALERTS (operator, 2026-09-28: "on telegram send just the alerts
    # for Gold and Btc. All the other should just come in as summary while in
    # shadow"). An instance whose instrument is listed here, OR which is
    # auto-trading, sends everything as before. Any other instance is a
    # shadow: it records everything and sends only money that actually moved
    # (fills, exits, a settled trade); its signals reach Telegram once a
    # session, in the combined SHADOW SUMMARY the command-listening instance
    # sends. Empty = every instance alerts, as before.
    telegram_alert_instruments: str = "BTC,GOLD"
    # THE ALL-SIGNAL STRATEGY (operator, 2026-09-28: "running alongside the
    # main strategy... trade at a pace one dollar... execute all their
    # generated signal every 15 minutes... two strategies in parallel"). On
    # every signal (the alert) of a listed instrument it buys that side for
    # `allsignal_stake` dollars, with no strategy gate. It keeps its own book
    # (store.allsignal_trades), is never mirrored, and stops with /auto off or
    # scripts/allsignal_switch.py. The main strategy is untouched.
    allsignal_instruments: str = "BTC,GOLD"
    allsignal_stake: float = 1.00
    # Dynamic entry-risk ceiling. When set, the day's opening capital replaces
    # the fixed stake: count * order limit + entry fee may not exceed this
    # fraction. The order limit (including retries/chases), not a stale quote,
    # sizes the trade. Zero retains fixed-dollar behavior.
    allsignal_stake_rate: float = 0.0
    # AFTER THE PRIMARY'S DAILY TARGET, A LOWER STAKE INSTEAD OF A PAUSE
    # (operator, 2026-09-30: "make primary account base size 6 and apply $3
    # after the 8% target hit only to mine the primary; the mirrors stay at the
    # pause when they hit target"). Once the primary has reached its daily
    # target it keeps trading at this stake while the day stays AT OR ABOVE the
    # target, and at `allsignal_stake` whenever losses bring it back below
    # (operator, 17:5x: "invalidate the daily target hit and trade size back to
    # the $6"); the day resets at 00:00 New York. Mirrors are untouched: their
    # own targets still pause them. 0 = pause at the target. FINDINGS 116-120.
    allsignal_after_target_stake: float = 0.0
    # AFTER A LOSS (operator, 2026-10-05: "Let implement I $30 boost only below 8%";
    # FINDINGS 160, 163): below the daily target, the next
    # `allsignal_after_loss_trades` taken $ trades after a known loss go at this
    # stake. The primary (the mirrors' own below). 0 = off.
    allsignal_after_loss_stake: float = 0.0
    allsignal_after_loss_stake_rate: float = 0.0
    allsignal_after_loss_trades: int = 2
    # AFTER A LOSS, WAIT FOR A CUSHION (operator, 2026-09-29: "adopt 5 and ship
    # it live"; FINDINGS 112). The signal after a losing trade (today) enters
    # only once the reference price is this far clear of the target line on
    # its side - at the alert if it already is, else at the first poll where
    # it gets there - and is skipped if that has not happened with
    # `allsignal_cushion_min_left_s` left. On the recorded signals 4-8 bps all
    # beat entering at once (+$56-67 vs +$43 over 7 days, no losing day);
    # 5 is the middle of that range, 10+ falls off. 0 turns it off.
    allsignal_after_loss_cushion_bps: float = 5.0
    allsignal_cushion_min_left_s: int = 120
    # Copied to the mirror accounts too, each sized $1 by its own
    # `allsignal_budget` (operator, 2026-09-28: "run on both my wife and mine
    # with the $1 trading all signals").
    allsignal_mirror: bool = True
    # THE LEARNING CORPUS, per instrument. These were function defaults inside
    # `learning_data` - not settings at all - so every instance fitted on BTC
    # whatever it traded. An arm is a statement about one instrument's
    # distribution, the same as a threshold (FINDINGS 43, 63), and
    # `_corpus_mismatch` now refuses a fit whose rows disagree with the series.
    # These are what let a second instance satisfy that guard rather than
    # simply being blocked by it.
    corpus_brti_path: str = "data/brti_history.db"
    corpus_market_path: str = "data/market_data.db"
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    execution_enabled: bool = False
    execution_entry_series: str = ""
    daily_profit_target_enabled: bool = False
    # Live policy (2026-10-07): each account stops new BTC entries at 3%
    # of its own recorded opening capital, net of fees, until midnight ET.
    # The guard remains opt-in; exits and shadow recording continue.
    daily_profit_target_rate: float = 0.03
    # Optional separate primary cap. Production sets this to 0.03 as well.
    daily_profit_stop_rate: float = 0.0
    # AFTER TWO LOSSES, NEVER AGAINST THE 15-MIN TREND (operator, 2026-10-02:
    # "the 15 minutes after 2 losses is the one I want live"; FINDINGS 139-142):
    # once the day's last N taken $ trades all lost, a BTC signal against the
    # Kalshi BRTI move of the last `minutes` by more than `bps` is skipped.
    # 0 = off.
    # THE CHASE (operator, 2026-10-05: "it's better to take it than just letting it
    # go"; FINDINGS 153): a $ order that missed because the price ran past its cap
    # is bought at the moved price - from `allsignal_chase_after_s` after the miss,
    # up to `allsignal_chase_max`, at most `allsignal_chase_attempts` times, while
    # the price is still on the signal's side with >= 2 min left. 0 = off.
    allsignal_chase_max: float = 0.0
    allsignal_chase_after_s: int = 10
    allsignal_chase_attempts: int = 3
    allsignal_trend_skip_after_losses: int = 0
    allsignal_trend_skip_minutes: int = 15
    allsignal_trend_skip_bps: float = 10.0
    # While a chop range is locked a BTC signal waits for either side's ask to reach `min_ask`
    # and enters that side (the opposite one if it gets there first); neither with
    # `allsignal_cushion_min_left_s` left: no entry. Every lock is logged to
    # runtime/lock_shadow.jsonl. 0 min_ask = log only.
    allsignal_ohlc_lock_wait: bool = False
    allsignal_ohlc_lock_min_ask: float = 0.85
    # BTC strategy skips and after-loss waits become price confirmation on either
    # side for EVERY BTC signal. Production uses .85. Zero retains the historical
    # cushion/skip policy.
    allsignal_skip_wait_min_ask: float = 0.0
    # Price confirmation also rejects an ask ABOVE this (production .90): such a side keeps
    # waiting until it is back in range. Orders and the chase never go above it. 0 = no ceiling.
    allsignal_skip_wait_max_ask: float = 0.0
    mirror_daily_profit_target_rate: float = 0.03     # each mirror
    # THE MIRRORS AFTER A LOSS, PAST A TARGET, AND ONCE THE PRIMARY IS DONE
    # (operator, 2026-10-05: "Mirrors: boost all three by $1 ... Affoue ... $6 base
    # and $8 after 1 loss and the after target hit $3"; FINDINGS 163). Inside the
    # after-a-loss boost (the primary's taken sequence) a copy goes at
    # `mirror_n_allsignal_after_loss_stake` if set, else the day's stake + this.
    # `mirror_n_allsignal_after_target_stake` > 0: that mirror is NOT paused at its
    # own target - it trades on at this stake while at or above it.
    # `mirror_after_primary_done`: once the primary is DONE for the day at its cap,
    # its $ signals still go to the mirrors still trading by their own day.
    mirror_allsignal_after_loss_add: float = 0.0
    mirror_after_primary_done: bool = False
    # THE TARGET SCALES WITH THE ACCOUNT (operator, 2026-10-01: "design a scale
    # mechanic that auto adjusts the % based on account growth"). A mirror's day
    # target is its rate x the opening, but never more than this many WINS at
    # its own stake - a win being what that stake makes at a typical 75c entry
    # (contracts_for_budget(stake, 0.75) x 0.25: $0.50 at $2, $1.00 at $3). So
    # as an account grows the % falls and the target stays reachable at the same
    # stake. Measured on the recorded copies 09-23..10-01: up to ~7 wins the
    # target was reached on 8 of 9 days at both $2 and $3; past ~8-9 the hit rate
    # fell (Wife missed 10-01 by 13c at $2 needing ~9 wins). 0 = off. FINDINGS 123.
    mirror_target_max_wins: float = 0.0
    target_win_price: float = 0.75
    # THE MIRRORS' STAKE SCALES WITH THE ACCOUNT (operator, 2026-10-01: "auto
    # scale for the mirrored account as the account balance changes every day at
    # midnight, but a nice safe scale. Only my primary is controlled manually on
    # aggressive"). At each 00:00:30 opening a mirror's $ stake for the day is
    # this fraction of its opening in whole dollars - never below its own
    # MIRROR_n_ALLSIGNAL_BUDGET (the operator's stake stays the floor) and never
    # above `mirror_stake_scale_max` (the primary's base). Its 7-win target cap
    # follows the same stake. The primary is untouched. 0 = off. FINDINGS 129.
    mirror_stake_scale_rate: float = 0.0
    mirror_stake_scale_max: float = 6.0
    # THE PRIMARY FUNDS ITS MARKET'S SHARD before each order, as the mirrors
    # and the Kalshi app do (operator, 2026-09-29: "the trades are failing").
    # At $5 a signal BTC's shard 2 held $4.38 while $98 sat on shard 0, and
    # every order was refused "insufficient balance".
    kalshi_auto_fund: bool = True
    # --- copy trading to other accounts ---------------------------------
    # Every order this system places on the primary account is forwarded to up
    # to two other Kalshi accounts, each with its own API key and ITS OWN
    # SIZING. The mirrors are write-only: no balance, fill, settlement or P&L is
    # ever read from them, so nothing here can reach the ledger.
    #
    # OFF BY DEFAULT, and off unless `mirror_enabled` is true AND a key pair is
    # configured AND execution itself is live. Dry run never mirrors.
    #
    # Sizing per account is base-through-upsize, in the same units as the
    # primary: `base_budget` is dollars PER CONTRACT (like `auto_budget`) and 0
    # means "ignore the budget, buy `base_contracts`"; `add_contracts` is the
    # upsize and 0 means "use whatever count the primary's add used";
    # `max_contracts` is a hard ceiling on every order and 0 means none.
    # A mirror does NOT inherit the primary's tier, so the growth controller
    # raising the primary's size leaves a small mirror account alone.
    # WHICH INSTANCES MAY MIRROR. Every instance reads this ONE .env file - the
    # per-instance launchers override only instrument values and deliberately
    # keep shared secrets here (see scripts/run_gold.ps1) - so `mirror_enabled`
    # alone would turn copy trading on for BTC, ETH, GOLD, SILVER and SOL at
    # once, and for every instrument added afterwards. Five instances forwarding
    # a base contract each is five times the exposure the operator configured on
    # the destination account.
    #
    # So mirroring is scoped by name and FAILS CLOSED: an instance not listed
    # here does not mirror, and a new instrument inherits nothing. Comma
    # separated, matched against BTC15_INSTANCE, where an empty instance - the
    # original BTC service - is named `btc`.
    mirror_instances: str = ""
    mirror_enabled: bool = False
    mirror_1_api_key_id: str = ""
    mirror_1_private_key_path: str = ""
    mirror_1_base_budget: float = 0.0
    mirror_1_base_contracts: int = 1
    mirror_1_allsignal_budget: float = 1.0
    mirror_1_allsignal_risk_rate: float = 0.0
    mirror_1_allsignal_after_loss_stake: float = 0.0
    mirror_1_allsignal_after_target_stake: float = 0.0
    mirror_1_add_contracts: int = 0
    mirror_1_max_contracts: int = 0
    mirror_2_api_key_id: str = ""
    mirror_2_private_key_path: str = ""
    mirror_2_base_budget: float = 0.0
    mirror_2_base_contracts: int = 1
    mirror_2_allsignal_budget: float = 1.0
    mirror_2_allsignal_risk_rate: float = 0.0
    mirror_2_allsignal_after_loss_stake: float = 0.0
    mirror_2_allsignal_after_target_stake: float = 0.0
    mirror_2_add_contracts: int = 0
    mirror_2_max_contracts: int = 0
    # A THIRD mirror (operator, 2026-09-30: "I added MIRROR 3 enable it to trade
    # same as the other MIRRORs"): the same fields, used only when both its key
    # id and key path are set.
    mirror_3_api_key_id: str = ""
    mirror_3_private_key_path: str = ""
    mirror_3_base_budget: float = 0.0
    mirror_3_base_contracts: int = 1
    mirror_3_allsignal_budget: float = 1.0
    mirror_3_allsignal_risk_rate: float = 0.0
    mirror_3_allsignal_after_loss_stake: float = 0.0
    mirror_3_allsignal_after_target_stake: float = 0.0
    mirror_3_add_contracts: int = 0
    mirror_3_max_contracts: int = 0
    # FUND THE MARKET'S EXCHANGE SHARD BEFORE EACH MIRROR ORDER, as the Kalshi
    # app does for a manual trade. The account's cash is split by
    # `exchange_index` and an API order can spend only its market's shard (the
    # 15-minute crypto markets are shard 2). Without this the wife's mirror ran
    # shard 2 to $0.09 on 2026-09-26 and was refused every entry for ~15 hours
    # while $30 sat in shard 0. Only the shortfall is moved, never a float.
    mirror_1_auto_fund: bool = True
    mirror_2_auto_fund: bool = True
    mirror_3_auto_fund: bool = True
    mirror_1_fund_source_shard: int = 0
    mirror_2_fund_source_shard: int = 0
    mirror_3_fund_source_shard: int = 0
    # RECOVERY SIZE ON THE MIRROR, operator 2026-09-27: "$2 as well". The same
    # dollar budget as the primary's `loss_step_budget`, applied only to an
    # entry the primary's loss step upsized. At 0.70-0.79 that is 2 contracts,
    # and `mirror_1_max_contracts` still caps it. Mirror 2 has no instruction
    # and no account, so it stays off.
    mirror_1_recovery_budget: float = 2.00
    mirror_2_recovery_budget: float = 0.0
    mirror_3_recovery_budget: float = 0.0
    # WHO each mirror account is, in every message that names it (operator,
    # 2026-09-28: m2 is Uncle George's account). Display only; nothing keys
    # on it except the balance baseline, which is stored under this name.
    mirror_1_label: str = "Wife"
    mirror_2_label: str = "Uncle George"
    mirror_3_label: str = "Mirror 3"      # MIRROR_3_LABEL in .env names it
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
    # THE FLOOR SCALES WITH THE BASE (operator, 2026-09-27: "loss limit must
    # scale"). `auto_daily_loss_limit` is the floor for this many base
    # contracts - 2, the base it was set for - and the day's floor is
    # limit x today's base / this. See main.scaled_loss_limit.
    loss_limit_scales_with_base: bool = True
    loss_limit_reference_base: int = 2
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
    # THE EARLY STAND-DOWN. Recovery stops UPSIZING well before the deficit
    # reaches zero, because late in a recovery the remaining deficit is small
    # but the position is still double size - so one loss more than undoes the
    # run of wins that got there, and arms recovery again, deeper. Recover,
    # lose bigger, recover.
    #
    # Operator instruction, 2026-09-23: "Even after a 50% recovery of the
    # initial loss, turn off recovery. That's enough, because we've seen that
    # even regular size is able to recover on its own." Stated as a
    # requirement, not a proposal, and implemented as one.
    #
    # Standing down NEVER zeroes the deficit. The money is still missing and
    # the ledger keeps saying so; only the upsize stops. See `recovery_exit`.
    recovery_partial_exit_enabled: bool = True
    # BOTH must hold. Four wins that barely moved the deficit leave real
    # ground to make up; half the money back after one lucky market says
    # nothing about whether the run is stable.
    recovery_exit_fraction: float = 0.50       # of the cycle's INITIAL deficit
    recovery_exit_required_wins: int = 4       # distinct profitable MARKETS
    # OFF. Recovery buys no larger BASE position; it acts only through the
    # conditional add-on, which rests ONE extra contract 2c below the actual
    # fill and only while the BRTI evidence holds. With both on they stack:
    # two contracts upfront plus a third resting behind them, for a deficit
    # that justified one. The base entry is one contract whether or not a
    # deficit is outstanding.
    recovery_upfront_upsize_enabled: bool = False

    # THE LOSS STEP. Operator's rule, 2026-09-24: after a market that lost,
    # the next trade is sized to a fixed DOLLAR budget; a win resets it to
    # base. Consecutive losses stay at the same budget, so it is a step and
    # not a martingale - exposure is bounded by `loss_step_max_contracts`
    # whatever the streak.
    #
    # This is keyed on the LAST MARKET'S RESULT, not on the deficit. It is a
    # different rule from `recovery_upfront_upsize_enabled` above, which sizes
    # while money is still outstanding; this one asks only whether the
    # previous market lost. Both may not run - see `loss_step_size`.
    #
    # THE EVIDENCE, recorded beside the decision because it does not support
    # the rule on its own (FINDINGS 61). On 146 reconciled live lifecycles the
    # $5 step returned +8.85 against -0.26 flat. But the whole gain comes from
    # the 25 trades that happened to follow a loss winning 92.0% against 82.9%
    # overall, which a permutation test could not separate from chance
    # (p=0.249); shuffling the SAME trades into a different order reproduces a
    # gain that large 12.9% of the time. The sample contains two 2-loss runs
    # and NO 3-loss run, so the shallow drawdown it shows has never been
    # tested by the thing that would move it: across shuffles the median max
    # drawdown is -6.84 and the worst -22.42, against the -5.80 observed.
    #
    # The operator has decided with that measurement in view. It is their
    # call - sizing always is - and it is implemented in full.
    #
    # REDUCED $5 -> $2 BY THE OPERATOR, 2026-09-24 the same day: "5 is too
    # risky just to make 50". That judgement is about the ratio the measurement
    # never addressed. The $5 step risks $5 per post-loss trade to chase a
    # total upside the backtest put at +8.85, and the permutation test could
    # not distinguish that gain from ordering luck (p=0.249) on a sample with
    # two 2-loss runs and NO 3-loss run. The drawdown it reported had never
    # been tested by the thing that would move it - across shuffles the median
    # worst drawdown was -6.84 against the -5.80 observed - so the downside was
    # the least-evidenced number in the whole result. Sizing down when the
    # evidence is weakest is the conservative reading of exactly that.
    #
    # $2 is also the size the two other upsize triggers use, so the three no
    # longer disagree about what "one step up" means.
    # NO RECOVERY AT ALL (operator, 2026-09-28: "does the system even need
    # recovery ... we need no recovery at all ... Gold and BTC can actually run
    # without recovery based on the report we have already seen"; FINDINGS
    # 108). The master switch: OFF, there is no loss step, no recovery
    # message (ARMED / SIZE ENDED) and no recovery line on any message. Every
    # entry is base size. The deficit is still folded each poll - that is
    # bookkeeping, not trading - and nothing reads it to size an order.
    recovery_enabled: bool = False
    loss_step_enabled: bool = True
    loss_step_budget: float = 2.00
    # IT WAITS FOR THE PRICE WHERE IT MATTERS. Operator instruction,
    # 2026-09-25: "the recovery after a loss does not need to be triggered
    # automatically, as we will make it wait for the best opportunity ...
    # around the 70 to 79 range ... so that we are not making 20 cent profit
    # on a recovery trade where normal sizing can offer the same on a better
    # opportunity."
    #
    # The arithmetic behind it: the extra contract wins `1 - ask` and loses
    # `ask`, so at 0.90 it risks 90c to make 10c and at 0.75 it risks 75c to
    # make 25c. Firing the step at the top of the band spends the whole upsize
    # for about 10c, which is the complaint.
    #
    # THE EVIDENCE, recorded beside the decision because it does NOT support the
    # band on its own. Expected value per extra contract is `p - ask`, which is
    # the calibration residual - so the payoff ratio cancels and the question
    # becomes where the market is most mispriced, not where the win pays most.
    # On BTC's 7,139 priced brti-4 points over 64 days, ungated:
    #
    #     0.70-0.75   73.1%   +0.0104 per contract   +0.0138 per dollar
    #     0.75-0.80   76.5%   -0.0053               -0.0067
    #     0.85-0.90   89.4%   +0.0242               +0.0284
    #     0.90-0.93   94.1%   +0.0240 [+0.0009]     +0.0261
    #
    #     in 0.70-0.79  +0.0026 per contract, 21.7% of setups
    #     outside       +0.0159 [+0.0004]
    #
    # So on that population the band the operator chose is where the extra
    # contract earns LEAST, and the top of the price band is where it earns
    # most. The measurement could not be repeated on the population that
    # actually matters - the setups the gates admit, which is all the step ever
    # sizes - because BTC's brti-2-era floors admit too few brti-4 points to
    # score. So the ungated table is suggestive, not decisive.
    #
    # The operator has decided with that in view and instructed it be shipped.
    # Sizing is theirs; it is implemented in full, and the interval above is
    # here so the decision can be revisited against live results rather than
    # re-argued.
    loss_step_band_lo: float = 0.70
    loss_step_band_hi: float = 0.79
    # HOW LONG IT MAY WAIT. The operator's "3 to 5 trades later" as a bound:
    # after this many settled bot markets the armed step expires unspent. A
    # wait with no bound is not a wait, it is a permanent upsize waiting for a
    # cheap ask - and the further from the loss it fires, the less it is a
    # recovery of anything.
    loss_step_wait_markets: int = 5
    # A HARD CEILING, not the rule. At the 0.70 floor of the price band $2
    # buys 2, so this is headroom against a cheap fill, and the guard that
    # stops a mispriced ask from turning a $2 budget into a large position.
    # Left at 8 deliberately: it bounds the OTHER upsize paths too, and
    # lowering a ceiling that is no longer reachable by this rule would only
    # look like it had been tightened.
    loss_step_max_contracts: int = 8
    # THE RECOVERY IS A COMBO AT BASE SIZE (operator, 2026-09-27: "replace the
    # single recover into a Combo with same base size, no more up scaling").
    # The loss step above still decides WHEN - armed by a loss, ask 0.70-0.79,
    # 5 markets, once per episode, never back to back - but its trade is no
    # longer the same market at 2x base: it is this entry plus a partner (SOL
    # for BTC/ETH, BTC for SOL) as one combo, sized at the BASE count. No
    # partner, no quote at or below the legs' product, or no fill: the entry
    # goes out as usual at base size. Nothing is upsized either way. See
    # combo_recovery.py and FINDINGS 105.
    recovery_combo_enabled: bool = True
    combo_partner_band_lo: float = 0.70
    combo_partner_band_hi: float = 0.85
    combo_partner_stale_s: float = 120.0
    # How long to wait for a maker's quote before falling back to the
    # single-leg base entry. The entry window is minutes; quotes arrive in
    # seconds or not at all.
    combo_quote_wait_s: float = 25.0
    # Accept the cheapest quote at or below the CHEAPER LEG's ask x this, same
    # or opposite direction (operator, 2026-09-27). A combo pays only if both
    # legs win, so it is never worth more than its cheaper leg. NOT the legs'
    # product: Kalshi prices correlation in, and a product cap refused every
    # same-direction pair it was offered (FINDINGS 105).
    combo_max_price_ratio: float = 1.00
    # FUND THE COMBO SHARD on the operator's own account (operator,
    # 2026-09-27: "auto-fund combos"). Combo markets settle in exchange shard
    # 1, which held $0.28 while shard 0 held $103; the app moves the cost
    # itself, the API does not. Only the recovery combo is funded this way -
    # the 15-minute orders are untouched.
    combo_auto_fund: bool = True

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
    recovery_add_max_contracts: int = 1  # PER BASE CONTRACT, on top of the base (+2 at base 2)
    # HOW OLD THE BROKER'S POSITION MARK MAY BE when the add-on checks
    # exposure. Not an exposure limit - the cap and the arithmetic are
    # unchanged - but a bound on the FRESHNESS of one of its inputs.
    # `open_mark` is written by the 60-second settlement sweep, so anything
    # under one full sweep is normal and this allows one and a half. Past it
    # the exposure is treated as unverified, which defers rather than places.
    recovery_add_exposure_max_age_ms: int = 90_000

    # THE DAILY SIZING CONTROLLER (capital.py). One authority for base entries
    # and recovery adds alike.
    #
    # The base tier changes ONLY at the daily review, from reconciled settled
    # cash - never from an open position's mark, because sizing on unrealised
    # gains compounds exposure exactly when a position is most likely to give
    # them back. $30 of capital per contract: one base contract per $30 of
    # reconciled capital. UNCAPPED since 2026-09-27 (operator: "contracts
    # should not be capped, it should scale as capital grows"):
    # `max_base_contracts` 0 means no ceiling; set a number to restore one.
    #
    # The day is NEW YORK because that is the exchange's own reset - Kalshi
    # documents its utilisation caps resetting at midnight New York time - and
    # a system keeping books on a different day from its venue will file trades
    # in the wrong one twice a year at the DST boundaries.
    # THE ADAPTIVE INTELLIGENCE LAYER. Learns from the outcomes of executed,
    # rejected and vetoed signals and feeds that evidence back into the live
    # decision. It may re-rate confidence always; it may change a decision
    # only where `runtime/intelligence_policy.json` was validated to, and that
    # artefact is written by `scripts/train_intelligence.py` and never by the
    # running service.
    #
    # It can NEVER change position size - that authority is the capital
    # controller's alone - and an exception it grants overrides ONE strategy
    # gate, never a capital, exposure, loss or execution protection.
    intelligence_enabled: bool = True
    intelligence_policy_path: str = "runtime/intelligence_policy.json"
    # Frozen candidates, evaluated forward on every eligible signal.
    # They control nothing - this is how one earns the right to.
    intelligence_candidates_path: str = "runtime/intelligence_candidates.json"
    # A policy older than this stops being applied. Stale evidence quietly
    # describing a market that has moved on is the failure mode here.
    intelligence_max_policy_age_ms: int = 30 * 86_400_000

    # ---------------------------------------------------- continuous learning
    #
    # The learning loop runs INSIDE the service. It ingests every settled
    # signal - accepted and rejected - reconciles the executed ones against
    # Kalshi fills and fees, refits on Kalshi-native features, evaluates the
    # new fit against the running one on a chronological validation slice, and
    # activates only what clears the promotion bar.
    #
    # TRAINING RUNNING AND AN ADJUSTMENT BEING LIVE ARE DIFFERENT THINGS. Most
    # runs will activate nothing; that is the loop working. `/learning` reports
    # them separately and so do the tables.
    learning_enabled: bool = True
    # Retrain once this many NEW settled markets have accumulated since the
    # last completed run. A 15-minute series produces 96 a day, so 24 is about
    # six hours of fresh evidence - enough to move a cell, short enough that a
    # regime change is not waited out.
    learning_min_new_settlements: int = 24
    # ...and on this schedule regardless, so a quiet market still refreshes.
    learning_interval_ms: int = 6 * 3_600_000
    # How often the cheap due-check runs. The poll is every 10s; checking a
    # file and a watermark 8,640 times a day to make an hourly decision is
    # waste, and it is waste on the same thread that places orders.
    learning_check_ms: int = 5 * 60_000
    # After a failed run, retry on this base delay with exponential backoff up
    # to the normal interval. A failure must not become a hot loop, and must
    # not become a permanent stop either.
    learning_retry_ms: int = 15 * 60_000
    # Evidence floor for an arm to be consulted at all, live. Matches the
    # promotion bar in `learning.MIN_PROMOTION_N`; stated here too because it
    # is the number the running policy is written with.
    #
    # 100 BY OPERATOR DECISION, 2026-09-26: learning and identifying start at
    # 100 signals for an instrument. Measured that day, BTC held a cell at
    # n=103 refused 213 times and ETH one at n=115 refused 126 times, so the
    # previous 120 was costing real evidence for the sake of a number that was
    # chosen rather than measured. The statistical tests are unchanged.
    learning_min_evidence: int = 100
    # How much worse, in dollars per contract over the validation slice, a new
    # fit may score than the running one and still activate. Zero: a fresher
    # fit is not automatically a better one.
    learning_regression_tolerance: float = 0.0
    # Forward changes an ACTIVE execution arm must have made before its record
    # can withdraw it. Below this a bad run is indistinguishable from bad luck.
    learning_min_withdrawal_n: int = 20
    # THE LOCAL MODEL, INSIDE EVERY LEARNING RUN (operator, 2026-09-28: "do so
    # the system learns and adapts"). After each completed run the instance's
    # recorded lifecycles go to the local model for hypotheses, and every one
    # is tested (hypotheses.py). Survivors are reported as candidates; nothing
    # the model says changes an order.
    learning_hypotheses_enabled: bool = True
    learning_hypotheses_url: str = "http://127.0.0.1:8080/v1"
    learning_hypotheses_model: str = "local"
    # Inside the quiet slot: a call must end before entries open (+240s).
    learning_hypotheses_timeout_s: float = 200.0

    capital_sizing_enabled: bool = True
    capital_per_contract: float = 30.0
    max_base_contracts: int = 0      # 0 = no ceiling (operator, 2026-09-27)

    high_confidence_distance_min: float = 2.0
    high_confidence_distance_max: float = 4.0
    dry_run: bool = True
