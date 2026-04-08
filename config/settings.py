from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OKXConfig(BaseSettings):
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""
    is_demo: bool = True  # True = paper trading (x-simulated-trading: 1)

    model_config = SettingsConfigDict(env_prefix="OKX__", env_file=".env", extra="ignore")


class RiskConfig(BaseSettings):
    max_position_pct: float = 0.1       # max % of account equity per position
    max_daily_loss_pct: float = 0.02    # daily loss limit triggers strategy pause
    max_drawdown_pct: float = 0.05      # account drawdown triggers emergency stop
    order_rate_limit: int = 10          # max orders per second across all strategies
    max_open_orders: int = 20           # max total open orders

    model_config = SettingsConfigDict(env_prefix="RISK__", env_file=".env", extra="ignore")


class Settings(BaseSettings):
    log_level: str = "INFO"
    db_path: str = "trade.db"
    strategy_config: str = "config/strategies.yaml"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def okx(self) -> OKXConfig:
        return OKXConfig()

    @property
    def risk(self) -> RiskConfig:
        return RiskConfig()


settings = Settings()
