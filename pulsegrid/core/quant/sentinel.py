"""订单簿侧的防破网哨兵。

触发条件（同时满足才刹车，且 CVD 必须是显式主动买卖量）：
1. 持仓量在回看窗口内快速上升；
2. 价格没有下行，但 CVD 斜率为负（向下背离）。

K 线涨跌近似出来的 CVD 只留诊断字段，不能放行，也不能当成可执行的急刹车。
满足显式条件时 Emergency_Brake = True，调用方必须拒绝买入。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from core.quant.indicators import (
    QuantError,
    approximate_cvd_from_klines,
    klines_to_frame,
    linreg_slope,
)


class BrakeActiveError(RuntimeError):
    """哨兵不允许开仓。"""


def _series_numbers(data: Sequence[Any], *keys: str) -> list[float]:
    values: list[float] = []
    for item in data:
        if isinstance(item, Mapping):
            raw = None
            for key in keys:
                if key in item and item[key] is not None:
                    raw = item[key]
                    break
            if raw is None:
                raise QuantError(f"序列缺少 {keys}: {item!r}")
            values.append(float(raw))
        else:
            values.append(float(item))
    return values


def evaluate_orderbook_brake(
    kline_data: Sequence[Mapping[str, Any]],
    oi_data: Sequence[Any],
    cvd_data: Sequence[Any] | None = None,
    *,
    oi_rise_threshold: float = 0.05,
    lookback: int = 12,
) -> dict[str, Any]:
    """判断是否禁止买入。

    oi_data: 持仓量序列，元素可以是数字，或含 oi 的字典。按时间升序。
    cvd_data: CVD 序列，元素可以是数字，或含 cvd 的字典。None 时用 K 线近似。
    只有 cvd_source 为 explicit 且样本够，才可能 data_sufficient=True 并触发急刹车。
    近似 CVD 一律 data_sufficient=False、Emergency_Brake=False。
    """
    if oi_rise_threshold <= 0:
        raise QuantError("oi_rise_threshold 必须大于 0")
    if lookback < 2:
        raise QuantError("lookback 必须 >= 2")

    result: dict[str, Any] = {
        "Emergency_Brake": False,
        "block_buys": False,
        "data_sufficient": False,
        "oi_change_pct": None,
        "cvd_slope": None,
        "price_slope": None,
        "cvd_source": "missing",
        "lookback": lookback,
        "oi_rise_threshold": oi_rise_threshold,
        "reason": "OI/CVD 数据不足，不能判断急刹车。",
    }

    try:
        frame = klines_to_frame(kline_data)
        closes = frame["close"].astype(float).tolist()
    except QuantError as exc:
        result["reason"] = f"K 线无法用于哨兵: {exc}"
        return result

    if cvd_data is None:
        try:
            cvd_points = approximate_cvd_from_klines(frame)
        except QuantError as exc:
            result["reason"] = f"无法近似 CVD: {exc}"
            return result
        cvd_values = [point["cvd"] for point in cvd_points]
        result["cvd_source"] = "kline_tick_rule"
    else:
        try:
            cvd_values = _series_numbers(cvd_data, "cvd", "value")
        except (QuantError, TypeError, ValueError) as exc:
            result["reason"] = f"CVD 数据无法解析: {exc}"
            return result
        result["cvd_source"] = "explicit"

    try:
        oi_values = _series_numbers(oi_data, "oi", "oi_usd", "value")
    except (QuantError, TypeError, ValueError) as exc:
        result["reason"] = f"OI 数据无法解析: {exc}"
        return result

    # 比较「现在」和 lookback 根之前，序列长度至少 lookback + 1
    if len(oi_values) <= lookback or len(cvd_values) <= lookback or len(closes) <= lookback:
        result["reason"] = (
            f"样本不足：OI {len(oi_values)}、CVD {len(cvd_values)}、价格 {len(closes)}，"
            f"回看 {lookback} 至少需要 {lookback + 1} 个点。"
        )
        return result

    base_oi = oi_values[-1 - lookback]
    last_oi = oi_values[-1]
    if base_oi == 0:
        result["reason"] = "回看起点持仓量为 0，无法计算上升速度。"
        return result

    oi_change = (last_oi - base_oi) / abs(base_oi)
    price_slope = linreg_slope(closes[-lookback:])
    cvd_slope = linreg_slope(cvd_values[-lookback:])
    # 背离：价格没在跌，主动成交差额却在走低。同向下跌不算背离。
    oi_rising_fast = oi_change >= oi_rise_threshold
    cvd_diverging_down = cvd_slope < 0 and price_slope >= 0
    # 背离诊断先算出来。近似 CVD 只带到 reason 里，不参与放行或急刹车。
    if result["cvd_source"] != "explicit":
        result.update(
            {
                "Emergency_Brake": False,
                "block_buys": False,
                "data_sufficient": False,
                "oi_change_pct": oi_change,
                "cvd_slope": cvd_slope,
                "price_slope": price_slope,
                "reason": (
                    "CVD 由 K 线涨跌近似，不能作为放行或急刹车依据。"
                    f"诊断：持仓量变化 {oi_change * 100:.2f}%，"
                    f"价格斜率 {price_slope:.6f}，近似 CVD 斜率 {cvd_slope:.6f}。"
                ),
            }
        )
        return result

    emergency = bool(oi_rising_fast and cvd_diverging_down)
    if emergency:
        reason = (
            f"持仓量在 {lookback} 个点内上升 {oi_change * 100:.2f}%（阈值 {oi_rise_threshold * 100:.2f}%），"
            "同时 CVD 向下背离，禁止买入。"
        )
    elif oi_rising_fast:
        reason = "持仓量上升较快，但 CVD 没有向下背离，急刹车未触发。"
    elif cvd_diverging_down:
        reason = "CVD 偏弱，但持仓量没有快速上升，急刹车未触发。"
    else:
        reason = "持仓量与 CVD 未形成向上堆积加向下背离，急刹车未触发。"

    result.update(
        {
            "Emergency_Brake": emergency,
            "block_buys": emergency,
            "data_sufficient": True,
            "oi_change_pct": oi_change,
            "cvd_slope": cvd_slope,
            "price_slope": price_slope,
            "reason": reason,
        }
    )
    return result
