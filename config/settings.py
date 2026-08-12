from functools import cached_property

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

    # cached_property 需要 __dict__，pydantic 默认不允许给实例设未声明的属性
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", ignored_types=(cached_property,)
    )

    # 用 cached_property 而非 property：后者每次访问都会重建配置对象并重读 .env 文件，
    # 而 settings.okx / settings.risk 在下单、风控等热路径上被反复访问。
    @cached_property
    def okx(self) -> OKXConfig:
        return OKXConfig()

    @cached_property
    def risk(self) -> RiskConfig:
        return RiskConfig()


settings = Settings()
