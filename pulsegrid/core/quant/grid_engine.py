"""自适应网格边界。全部是本地算术，禁止把价格交给模型计算。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from core.quant.indicators import QuantError, atr, klines_to_frame

# 网格数量上限按 OKX 现货网格常见限制收紧，避免发出必被拒绝的参数
_MIN_GRIDS = 2
_MAX_GRIDS = 100

# atr_mult 决定波动带宽度；min_profit 是单格等比收益下限，用来反推格数
_PROFILES: dict[str, dict[str, float | int]] = {
    "conservative": {"atr_mult": 2.8, "grids": 10, "min_profit": 0.0045},
    "balanced": {"atr_mult": 2.0, "grids": 16, "min_profit": 0.0030},
    "aggressive": {"atr_mult": 1.3, "grids": 24, "min_profit": 0.0018},
}

_RISK_ALIASES = {
    "conservative": "conservative",
    "low": "conservative",
    "稳健": "conservative",
    "保守": "conservative",
    "抗跌": "conservative",
    "balanced": "balanced",
    "moderate": "balanced",
    "平衡": "balanced",
    "适中": "balanced",
    "aggressive": "aggressive",
    "high": "aggressive",
    "激进": "aggressive",
    "进取": "aggressive",
}


def _normalize_risk_name(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if text in _RISK_ALIASES:
        return _RISK_ALIASES[text]
    # 中文别名大小写折叠后对不上，再查原文
    original = str(raw or "").strip()
    if original in _RISK_ALIASES:
        return _RISK_ALIASES[original]
    raise QuantError(f"无法识别风险偏好: {raw}")


def _parse_preference(risk_preference: str | Mapping[str, Any]) -> tuple[str, float | None]:
    max_drawdown: float | None
    if isinstance(risk_preference, str):
        name = _normalize_risk_name(risk_preference)
        max_drawdown = None
    elif isinstance(risk_preference, Mapping):
        name = _normalize_risk_name(
            risk_preference.get("risk_tolerance", risk_preference.get("risk_preference"))
        )
        raw_dd = risk_preference.get("max_drawdown_limit")
        if raw_dd is None or raw_dd == "":
            max_drawdown = None
        else:
            max_drawdown = float(raw_dd)
            if not 0 < max_drawdown < 1:
                raise QuantError("max_drawdown_limit 必须在 (0, 1) 之间，例如 15% 写成 0.15")
    else:
        raise QuantError("risk_preference 必须是字符串，或包含 risk_tolerance 的字典")
    return name, max_drawdown


def _geometric_profit(upper: float, lower: float, grid_count: int) -> float:
    if lower <= 0 or upper <= lower or grid_count < 1:
        raise QuantError("网格区间或格数无效，无法计算单格收益")
    return (upper / lower) ** (1.0 / grid_count) - 1.0


def calculate_adaptive_bounds(
    kline_data: Any,
    risk_preference: str | Mapping[str, Any],
) -> dict[str, Any]:
    """用 14 周期 ATR 和现价给出上沿、下沿、格数、单格收益率。

    若 risk_preference 带 max_drawdown_limit，下沿还会下探到「现价 × (1 - 回撤)」，
    让网格在用户声明的暴跌幅度内不被价格直接跑出区间。
    """
    risk_name, max_drawdown = _parse_preference(risk_preference)
    profile = _PROFILES[risk_name]
    frame = klines_to_frame(kline_data)
    price = float(frame.iloc[-1]["close"])
    if not math.isfinite(price) or price <= 0:
        raise QuantError(f"现价无效: {price}")

    atr_value = atr(frame, period=14)
    atr_mult = float(profile["atr_mult"])
    # 波动极小时仍保留 1% 半宽，避免网格挤在买卖价差里
    half_width = max(atr_mult * atr_value, price * 0.01)
    upper = price + half_width
    lower = price - half_width
    if max_drawdown is not None:
        crash_floor = price * (1.0 - max_drawdown)
        lower = min(lower, crash_floor)
    if lower <= 0 or upper <= lower or not (lower < price < upper):
        raise QuantError(f"网格区间无效: lower={lower} price={price} upper={upper}")

    grid_count = int(profile["grids"])
    min_profit = float(profile["min_profit"])
    profit = _geometric_profit(upper, lower, grid_count)
    # 区间已经定死，单格收益不够手续费缓冲时减少格数，而不是让模型改价格
    while profit < min_profit and grid_count > _MIN_GRIDS:
        grid_count -= 1
        profit = _geometric_profit(upper, lower, grid_count)
    grid_count = max(_MIN_GRIDS, min(_MAX_GRIDS, grid_count))
    profit = _geometric_profit(upper, lower, grid_count)
    if not math.isfinite(profit) or profit <= 0:
        raise QuantError("单格收益率无效")

    return {
        "last_price": price,
        "atr": atr_value,
        "atr_period": 14,
        "atr_mult": atr_mult,
        "upper": upper,
        "lower": lower,
        "grid_count": grid_count,
        "per_grid_profit_rate": profit,
        "min_profit_rate": min_profit,
        "risk_preference": risk_name,
        "spacing_mode": "geometric",
        "run_type": "2",
        "max_drawdown_limit": max_drawdown,
        "range_pct": (upper - lower) / price,
    }


def to_plain_decimal_str(value: Decimal | float | str) -> str:
    """转成 OKX 接受的普通十进制字符串，不带科学计数法。"""
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _to_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    if tick <= 0:
        raise QuantError("tickSz 必须大于 0")
    units = (price / tick).to_integral_value(rounding=rounding)
    return units * tick


def align_bounds_to_tick(bounds: Mapping[str, Any], tick_size: str) -> dict[str, Any]:
    """上沿向上取整、下沿向下取整，对齐后重新计算单格收益。"""
    tick = Decimal(str(tick_size))
    if tick <= 0:
        raise QuantError(f"非法 tickSz: {tick_size}")
    upper = _to_tick(Decimal(str(bounds["upper"])), tick, ROUND_CEILING)
    lower = _to_tick(Decimal(str(bounds["lower"])), tick, ROUND_FLOOR)
    if lower <= 0 or upper <= lower:
        raise QuantError("按 tick 对齐后网格区间无效")

    grid_count = int(bounds["grid_count"])
    min_profit = float(bounds.get("min_profit_rate") or 0)
    profit = _geometric_profit(float(upper), float(lower), grid_count)
    while profit < min_profit and grid_count > _MIN_GRIDS:
        grid_count -= 1
        profit = _geometric_profit(float(upper), float(lower), grid_count)

    aligned = dict(bounds)
    aligned.update(
        {
            "upper": float(upper),
            "lower": float(lower),
            "upper_str": to_plain_decimal_str(upper),
            "lower_str": to_plain_decimal_str(lower),
            "grid_count": grid_count,
            "per_grid_profit_rate": profit,
            "tick_size": to_plain_decimal_str(tick),
        }
    )
    return aligned


def suggest_stop_loss_str(
    lower_str: str,
    last_price: float,
    max_drawdown_limit: float | None,
    tick_size: str,
) -> str:
    """止损放在网格下沿之下，跌破区间就停，避免网格在单边行情里一直接。"""
    lower = Decimal(lower_str)
    tick = Decimal(str(tick_size))
    candidates = [lower * Decimal("0.995")]
    if max_drawdown_limit is not None and 0 < float(max_drawdown_limit) < 1:
        drawdown_px = Decimal(str(last_price)) * (Decimal(1) - Decimal(str(max_drawdown_limit)))
        candidates.append(drawdown_px)
    raw = min(candidates)
    stop = _to_tick(raw, tick, ROUND_FLOOR)
    if stop >= lower:
        stop = lower - tick
    if stop <= 0:
        raise QuantError("止损价无效")
    return to_plain_decimal_str(stop)
