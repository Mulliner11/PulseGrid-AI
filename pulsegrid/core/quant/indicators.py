"""ATR、VWAP、CVD、支撑阻力。只做数值计算，不访问网络，也不调用模型。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd


class QuantError(RuntimeError):
    """指标或网格输入不合法。"""


def klines_to_frame(klines: pd.DataFrame | Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """把 K 线统一成按时间升序的 DataFrame。"""
    if isinstance(klines, pd.DataFrame):
        frame = klines.copy()
    else:
        frame = pd.DataFrame(list(klines))
    required = {"high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        raise QuantError(f"K 线缺少字段: {sorted(missing)}")
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[["high", "low", "close"]].isna().any().any():
        raise QuantError("K 线价格含有无法解析的值")
    if "ts" in frame.columns:
        frame = frame.sort_values("ts")
    return frame.reset_index(drop=True)


def atr(klines: pd.DataFrame | Sequence[Mapping[str, Any]], period: int = 14) -> float:
    """Wilder ATR。首值是前 period 根 TR 的均值，之后按 RMA 递推。"""
    if period < 1:
        raise QuantError("ATR 周期必须 >= 1")
    frame = klines_to_frame(klines)
    if len(frame) < period + 1:
        raise QuantError(f"K 线不足：ATR({period}) 至少需要 {period + 1} 根，当前 {len(frame)} 根")

    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    prev_close = close[:-1]
    # TR = max(高-低, |高-前收|, |低-前收|)
    true_range = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    value = float(np.mean(true_range[:period]))
    for item in true_range[period:]:
        value = (value * (period - 1) + float(item)) / period
    if not math.isfinite(value) or value < 0:
        raise QuantError("ATR 计算结果无效")
    return value


def vwap(
    klines: pd.DataFrame | Sequence[Mapping[str, Any]],
    window: int | None = None,
) -> float:
    """典型价按成交量加权。volume 使用基础货币成交量。"""
    frame = klines_to_frame(klines)
    if "volume" not in frame.columns:
        raise QuantError("K 线缺少 volume，无法计算 VWAP")
    if window is not None:
        if window < 1:
            raise QuantError("VWAP window 必须 >= 1")
        frame = frame.tail(window)
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["volume"].astype(float)
    if volume.isna().any():
        raise QuantError("成交量含有无法解析的值")
    denominator = float(volume.sum())
    if denominator <= 0:
        raise QuantError("成交量为 0，无法计算 VWAP")
    value = float((typical * volume).sum() / denominator)
    if not math.isfinite(value):
        raise QuantError("VWAP 计算结果无效")
    return value


def cumulative_volume_delta(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    """由主动买量、主动卖量累加 CVD。调用方必须保证买卖量来自行情，而不是模型。"""
    if not rows:
        raise QuantError("CVD 输入为空")
    running = 0.0
    series: list[dict[str, float]] = []
    for row in rows:
        try:
            buy = float(row["buy"])
            sell = float(row["sell"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QuantError(f"CVD 行缺少 buy/sell: {row!r}") from exc
        running += buy - sell
        point = {"cvd": running, "buy": buy, "sell": sell}
        if "ts" in row and row["ts"] is not None:
            point["ts"] = float(row["ts"])
        series.append(point)
    return series


def approximate_cvd_from_klines(
    klines: pd.DataFrame | Sequence[Mapping[str, Any]],
) -> list[dict[str, float]]:
    """没有主动买卖量时，用 K 线涨跌给成交量定符号。只可展示，不能当作放行依据。"""
    frame = klines_to_frame(klines)
    if "volume" not in frame.columns:
        raise QuantError("K 线缺少 volume，无法近似 CVD")
    running = 0.0
    series: list[dict[str, float]] = []
    for row in frame.to_dict(orient="records"):
        volume = float(row["volume"])
        signed = volume if float(row["close"]) >= float(row.get("open", row["close"])) else -volume
        running += signed
        point = {"cvd": running, "buy": max(signed, 0.0), "sell": max(-signed, 0.0)}
        if row.get("ts") is not None and not (isinstance(row.get("ts"), float) and math.isnan(row["ts"])):
            point["ts"] = float(row["ts"])
        series.append(point)
    return series


def support_resistance(
    klines: pd.DataFrame | Sequence[Mapping[str, Any]],
    lookback: int = 20,
) -> dict[str, float | int]:
    """近端高低点与最后一根 K 线的枢轴价。"""
    if lookback < 1:
        raise QuantError("lookback 必须 >= 1")
    frame = klines_to_frame(klines).tail(lookback)
    if frame.empty:
        raise QuantError("没有可用于支撑阻力的 K 线")
    last = frame.iloc[-1]
    pivot = (float(last["high"]) + float(last["low"]) + float(last["close"])) / 3.0
    return {
        "support": float(frame["low"].min()),
        "resistance": float(frame["high"].max()),
        "pivot": pivot,
        "lookback": int(len(frame)),
    }


def linreg_slope(values: Sequence[float]) -> float:
    """一元线性回归斜率。点太少时返回 0，由调用方决定数据是否够用。"""
    series = np.asarray(list(values), dtype=float)
    if series.size < 2:
        return 0.0
    if not np.isfinite(series).all():
        raise QuantError("斜率输入含有非数值")
    x_axis = np.arange(series.size, dtype=float)
    x_mean = float(x_axis.mean())
    y_mean = float(series.mean())
    denominator = float(np.sum((x_axis - x_mean) ** 2))
    if denominator == 0:
        return 0.0
    return float(np.sum((x_axis - x_mean) * (series - y_mean)) / denominator)


def orderbook_imbalance(
    bids: Sequence[Sequence[Any]],
    asks: Sequence[Sequence[Any]],
    depth: int = 20,
) -> float:
    """(买盘量 - 卖盘量) / 总量，范围约 [-1, 1]。正数表示买盘更厚。"""
    if depth < 1:
        raise QuantError("depth 必须 >= 1")

    def _size(levels: Sequence[Sequence[Any]]) -> float:
        total = 0.0
        for level in list(levels)[:depth]:
            if len(level) < 2:
                raise QuantError(f"订单簿档位格式错误: {level!r}")
            total += float(level[1])
        return total

    bid_size = _size(bids)
    ask_size = _size(asks)
    total = bid_size + ask_size
    if total <= 0:
        raise QuantError("订单簿深度为 0")
    return (bid_size - ask_size) / total
