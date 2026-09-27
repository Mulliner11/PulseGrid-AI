"""从环境变量读取运行配置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# pulsegrid/.env，与 main.py 同级
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 让 OKX / Groq SDK 以外的代码也能读到同一份环境变量
load_dotenv(ENV_FILE, override=False)


class Settings(BaseSettings):
    """进程配置。密钥只从环境或 .env 读取，仓库里不放真实值。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    groq_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GROQ_API_KEY", "groq_api_key"),
    )
    # 默认 openai/gpt-oss-120b：llama-3.3-70b-versatile 在当前账号上返回 404。
    # 仍只用于 NLU 和中文说明，禁止拿它算价。环境变量 GROQ_MODEL 可以覆盖。
    groq_model: str = Field(
        default="openai/gpt-oss-120b",
        validation_alias=AliasChoices("GROQ_MODEL", "groq_model"),
    )

    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
    )

    okx_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OKX_API_KEY", "okx_api_key"),
    )
    okx_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("OKX_SECRET_KEY", "okx_secret_key"),
    )
    okx_passphrase: str = Field(
        default="",
        validation_alias=AliasChoices("OKX_PASSPHRASE", "okx_passphrase"),
    )
    # 0=实盘，1=模拟盘。默认模拟盘，避免未改配置时直接交易真实资金。
    okx_flag: str = Field(
        default="1",
        validation_alias=AliasChoices("OKX_FLAG", "okx_flag"),
    )
    # AI Builder Code。启动时可以留空（申请审核期间机器人仍可运行）。
    # 交易请求会把它写入 OKX 的 tag；留空时下单和停止网格在客户端被拒绝。
    okx_ai_builder_code: str = Field(
        default="",
        validation_alias=AliasChoices("OKX_AI_BUILDER_CODE", "okx_ai_builder_code"),
    )

    okx_base_url: str = Field(
        default="https://www.okx.com",
        validation_alias=AliasChoices("OKX_BASE_URL", "okx_base_url"),
    )
    default_kline_bar: str = Field(
        default="15m",
        validation_alias=AliasChoices("DEFAULT_KLINE_BAR", "default_kline_bar"),
    )
    # 订单簿 / 哨兵后台轮询间隔（秒）。与 Telegram 收消息、下单协程并行，互不阻塞。
    monitor_interval_sec: float = Field(
        default=8.0,
        validation_alias=AliasChoices("MONITOR_INTERVAL_SEC", "monitor_interval_sec"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "log_level"),
    )

    @field_validator("okx_flag", mode="before")
    @classmethod
    def _normalize_flag(cls, value: object) -> str:
        text = str(value).strip()
        if text not in {"0", "1"}:
            raise ValueError("OKX_FLAG 只能是 0（实盘）或 1（模拟盘）")
        return text

    @field_validator("monitor_interval_sec")
    @classmethod
    def _positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("MONITOR_INTERVAL_SEC 必须大于 0")
        return value

    @field_validator(
        "groq_api_key",
        "groq_model",
        "telegram_bot_token",
        "okx_api_key",
        "okx_secret_key",
        "okx_passphrase",
        "okx_ai_builder_code",
        "okx_base_url",
        "default_kline_bar",
        "log_level",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def is_demo(self) -> bool:
        return self.okx_flag == "1"

    def missing_runtime_keys(self) -> list[str]:
        """启动前检查。返回未填写的环境变量名。

        OKX_AI_BUILDER_CODE 不在这里。审核未通过时也可以启动；
        确认下单和停止网格仍由 OKX 客户端拒绝空码。
        """
        required = {
            "GROQ_API_KEY": self.groq_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "OKX_API_KEY": self.okx_api_key,
            "OKX_SECRET_KEY": self.okx_secret_key,
            "OKX_PASSPHRASE": self.okx_passphrase,
        }
        return [name for name, value in required.items() if not str(value).strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
