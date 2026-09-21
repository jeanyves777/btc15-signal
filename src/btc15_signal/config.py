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
    entry_from_seconds: int = 630  # start looking with ~10.5 minutes left
    entry_to_seconds: int = 330  # stop looking at ~5.5 minutes left
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
    entry_slippage: float = 0.01
    # How many entry orders one window may attempt. An immediate-or-cancel that
    # does not fill costs nothing, so a single miss should not end an
    # opportunity that is still valid - but each retry re-runs every gate at the
    # new price, and this caps how far a running market can be followed.
    auto_retry_limit: int = 3
    # How far the price may run away before a retry is abandoned. Chasing a
    # gapping book is how you end up paying 92c for something you wanted at 76c,
    # where the most it can make is 8c against 92c at risk.
    auto_retry_max_drift: float = 0.08
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
    auto_daily_loss_limit: float = 10.0   # stop for the day once down this much
    auto_max_trades_per_day: int = 40
    auto_max_trades_per_hour: int = 6
    auto_min_seconds_between: int = 120
    max_budget: float = 100.0  # ceiling for /size, a guard against a fat finger
    trade_contract_count: int = 1
    dry_run: bool = True
