"""Telegram 确认卡。卡片上的价格全部来自已经算好的 proposal，这里不再计算。"""

from __future__ import annotations

from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

LAUNCH_BUTTON_TEXT = "⚡ 一键在 OKX 开启网格"


def _esc(value: object) -> str:
    return escape(str(value), quote=False)


def _pct(value: object, digits: int = 2) -> str:
    if value is None:
        return "未提供"
    return f"{float(value) * 100:.{digits}f}%"


def _num(value: object) -> str:
    if value is None:
        return "未提供"
    if isinstance(value, str):
        return value
    number = float(value)
    absolute = abs(number)
    if absolute >= 100:
        return f"{number:.2f}"
    if absolute >= 1:
        return f"{number:.4f}"
    return f"{number:.6f}"


def _launch_allowed(brake: dict[str, Any]) -> bool:
    """只有哨兵数据够、且没有急刹车或禁买时，才给出下单按钮。"""
    if not brake.get("data_sufficient"):
        return False
    if brake.get("Emergency_Brake") or brake.get("block_buys"):
        return False
    return True


def render_confirm_card(proposal: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup | None]:
    """生成确认文案。不能下单时不附带一键开网格按钮。"""
    intent = proposal["intent"]
    bounds = proposal["bounds"]
    brake = proposal["brake"]
    risk_labels = {"conservative": "稳健", "balanced": "平衡", "aggressive": "激进"}
    direction_labels = {"neutral": "中性持有", "long": "偏多", "short": "偏空"}
    symbol = _esc(intent["symbol"])
    upper = _esc(bounds.get("upper_str") or _num(bounds["upper"]))
    lower = _esc(bounds.get("lower_str") or _num(bounds["lower"]))
    environment = _esc(proposal.get("environment_label") or "模拟盘")
    rationale = _esc(proposal.get("rationale") or "说明暂缺，请以量化参数为准。")

    if not brake.get("data_sufficient"):
        brake_title = "数据不足"
    elif brake.get("Emergency_Brake"):
        brake_title = "已触发，禁止买入"
    else:
        brake_title = "未触发"

    lines = [
        "<b>脉冲智网 · 网格确认</b>",
        "<i>价格、格数和止损由本地量化引擎计算。模型只理解需求并写说明。</i>",
        "",
        "<b>意图</b>",
        f"标的：<code>{symbol}</code>",
        f"方向：{_esc(direction_labels.get(intent['direction'], intent['direction']))}",
        f"投入：{_esc(bounds.get('quote_sz', intent['budget']))} USDT",
        f"风险偏好：{_esc(risk_labels.get(intent['risk_tolerance'], intent['risk_tolerance']))}",
        f"回撤约束：{_pct(intent.get('max_drawdown_limit'), 0)}",
    ]
    if intent.get("direction_defaulted"):
        lines.append("未说明方向，按中性网格处理。")
    if intent.get("risk_defaulted"):
        lines.append("未说明风险偏好，按平衡处理。")
    if intent.get("direction") == "short":
        lines.append("一键开启不会提交开空单。")

    lines.extend(
        [
            "",
            "<b>量化参数</b>",
            f"现价：{_num(bounds['last_price'])}",
            f"ATR(14)：{_num(bounds['atr'])}",
            f"上沿：<code>{upper}</code>",
            f"下沿：<code>{lower}</code>",
            f"格数：{int(bounds['grid_count'])}",
            f"单格收益：{_pct(bounds['per_grid_profit_rate'])}",
            f"间距：{_esc('等比' if bounds.get('run_type') == '2' else bounds.get('spacing_mode', '等比'))}",
            f"止损触发：<code>{_esc(bounds.get('sl_trigger_px') or '未设置')}</code>",
        ]
    )
    if proposal.get("metrics", {}).get("vwap") is not None:
        lines.append(f"VWAP：{_num(proposal['metrics']['vwap'])}")
    if proposal.get("book_imbalance") is not None:
        lines.append(f"订单簿失衡：{_num(proposal['book_imbalance'])}")

    lines.extend(
        [
            "",
            "<b>防破网哨兵</b>",
            f"急刹车：{_esc(brake_title)}",
            f"OI 变化：{_pct(brake.get('oi_change_pct'))}",
            _esc(brake.get("reason") or ""),
            "",
            "<b>策略说明</b>",
            rationale,
            "",
            f"环境：{environment}",
            f"确认号：<code>{_esc(proposal['plan_id'])}</code>",
        ]
    )
    # 数据不足或急刹车时不放开会下单的按钮，避免误触。
    if _launch_allowed(brake):
        lines.append("点击后会再拉一次 OI/CVD。急刹车或数据不足时不会下单。")
        lines.append("下单请求会把 AI Builder Code 写入 OKX 的 tag 字段。")
        markup: InlineKeyboardMarkup | None = InlineKeyboardMarkup(
            [[InlineKeyboardButton(LAUNCH_BUTTON_TEXT, callback_data=f"pg:{proposal['plan_id']}")]]
        )
    else:
        lines.append("当前不能一键开网格。请重新描述你的需求，以生成新的确认卡。")
        markup = None
    return "\n".join(lines), markup
