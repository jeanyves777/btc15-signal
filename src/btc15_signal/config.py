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
    min_calibration_samples: int = 100
    min_win_probability: float = 0.80
    min_raw_probability: float = 0.50
    validated_lower_probability: float = 0.8290
    min_contract_edge: float = 0.03
    entry_alerts_enabled: bool = True
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
    trade_contract_count: int = 1
    dry_run: bool = True
