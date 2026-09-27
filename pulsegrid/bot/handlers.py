"""Telegram 流程：意图 → 量化网格与哨兵 → 说明 → 确认卡 → 用户点按后才下单。"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.menu import (
    cmd_grids,
    cmd_help,
    cmd_menu,
    cmd_status,
    cmd_stop,
    format_okx_trade_error,
    main_menu_keyboard,
    on_stop_callback,
    route_menu_text,
)
from bot.ui_cards import render_confirm_card
from config.settings import Settings
from core.llm.groq_client import GroqAgent, GroqAgentError, IntentParseError
from core.okx.client import OkxClientError, OkxRestClient
from core.okx.strategy_algo import OkxStrategyAlgo
from core.quant.grid_engine import (
    align_bounds_to_tick,
    calculate_adaptive_bounds,
    suggest_stop_loss_str,
    to_plain_decimal_str,
)
from core.quant.indicators import (
    QuantError,
    cumulative_volume_delta,
    orderbook_imbalance,
    support_resistance,
    vwap,
)
from core.quant.sentinel import BrakeActiveError, evaluate_orderbook_brake

logger = logging.getLogger("pulsegrid.bot")

PLAN_TTL_SEC = 15 * 60


class StrategyRejected(RuntimeError):
    """用户确认了，但当前状态不允许提交。"""


class Runtime:
    """机器人、行情客户端和待确认方案的共享状态。"""

    def __init__(
        self,
        settings: Settings,
        agent: GroqAgent,
        okx: OkxRestClient,
        algo: OkxStrategyAlgo,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.okx = okx
        self.algo = algo
        self.plans: dict[str, dict[str, Any]] = {}
        self.watches: dict[str, dict[str, Any]] = {}
        # algoId -> 打开「停止网格」的用户。确认回调要核对这份归属。
        self.stop_drafts: dict[str, dict[str, Any]] = {}
        # 保护确认单和监控名单。网络请求不要放在这把锁里。
        self.lock = asyncio.Lock()


def _brake_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    cvd = None
    if bundle.get("taker") and not bundle.get("taker_error"):
        cvd = cumulative_volume_delta(bundle["taker"])
    oi = bundle["oi"] if not bundle.get("oi_error") else []
    brake = evaluate_orderbook_brake(bundle["candles"], oi, cvd)
    if bundle.get("oi_error") or bundle.get("taker_error") or cvd is None:
        detail = bundle.get("oi_error") or bundle.get("taker_error") or "缺少主动买卖量"
        brake = {
            **brake,
            "Emergency_Brake": False,
            "block_buys": False,
            "data_sufficient": False,
            "reason": f"OI 或 CVD 没有取全（{detail}）。急刹车无法确认，一键开启会被拒绝。",
        }
    return brake


async def compose_grid_proposal(
    user_text: str,
    *,
    agent: GroqAgent,
    okx: OkxRestClient,
    bar: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """先解析意图，再用 OKX 行情做网格和哨兵，最后才让模型写说明。"""
    intent = await agent.parse_user_intent(user_text)
    bundle = await okx.fetch_market_bundle(intent["symbol"], bar)
    instrument = await okx.get_instrument(intent["symbol"])
    raw_bounds = calculate_adaptive_bounds(
        bundle["candles"],
        {
            "risk_tolerance": intent["risk_tolerance"],
            "max_drawdown_limit": intent["max_drawdown_limit"],
        },
    )
    bounds = align_bounds_to_tick(raw_bounds, str(instrument["tickSz"]))
    # 预算来自用户原话的意图字段，价格来自上面的 Python 结果
    bounds["quote_sz"] = to_plain_decimal_str(intent["budget"])
    bounds["sl_trigger_px"] = suggest_stop_loss_str(
        bounds["lower_str"],
        float(bounds["last_price"]),
        intent["max_drawdown_limit"],
        str(instrument["tickSz"]),
    )
    brake = _brake_from_bundle(bundle)
    try:
        vwap_value = vwap(bundle["candles"])
    except QuantError as exc:
        logger.warning("VWAP 跳过: %s", exc)
        vwap_value = None
    levels = support_resistance(bundle["candles"])
    imbalance = None
    if bundle.get("book"):
        try:
            imbalance = orderbook_imbalance(bundle["book"]["bids"], bundle["book"]["asks"])
        except QuantError as exc:
            logger.warning("订单簿失衡跳过: %s", exc)
    metrics = {
        "symbol": intent["symbol"],
        "last_price": float(bounds["last_price"]),
        "atr_14": float(bounds["atr"]),
        "grid_upper": float(bounds["upper_str"]),
        "grid_lower": float(bounds["lower_str"]),
        "grid_count": int(bounds["grid_count"]),
        "per_grid_profit_rate": float(bounds["per_grid_profit_rate"]),
        "budget_usdt": float(intent["budget"]),
        "risk_tolerance": intent["risk_tolerance"],
        "max_drawdown_limit": intent["max_drawdown_limit"],
        "vwap": vwap_value,
        "emergency_brake": bool(brake["Emergency_Brake"]),
        "oi_change_pct": brake.get("oi_change_pct"),
        "book_imbalance": imbalance,
        "support": levels["support"],
        "resistance": levels["resistance"],
    }
    rationale = await agent.generate_strategy_rationale(intent["symbol"], metrics)
    return {
        "plan_id": plan_id or secrets.token_hex(4),
        "intent": intent,
        "bounds": bounds,
        "brake": brake,
        "metrics": metrics,
        "rationale": rationale,
        "bar": bar,
        "book_imbalance": imbalance,
        "levels": levels,
    }


async def execute_confirmed_grid(
    proposal: dict[str, Any],
    *,
    okx: OkxRestClient,
    algo: OkxStrategyAlgo,
) -> dict[str, Any]:
    """用确认单里的 Python 参数下单。下单前只刷新哨兵，不接受模型改价。"""
    intent = proposal["intent"]
    if intent["direction"] == "short":
        raise StrategyRejected("Phase-1 一键开启只提交现货网格，不会自动开空。")
    bounds = proposal["bounds"]
    bundle = await okx.fetch_market_bundle(intent["symbol"], proposal.get("bar") or "15m")
    brake = _brake_from_bundle(bundle)
    if not brake.get("data_sufficient"):
        raise BrakeActiveError(str(brake.get("reason") or "哨兵数据不足，已拒绝下单。"))
    if brake.get("Emergency_Brake"):
        raise BrakeActiveError(str(brake["reason"]))

    last = float(bundle["candles"][-1]["close"])
    lower = float(bounds["lower"])
    upper = float(bounds["upper"])
    if last <= lower or last >= upper:
        raise StrategyRejected(
            f"现价 {last} 已不在确认区间 {bounds['lower_str']} – {bounds['upper_str']} 内，请重新生成。"
        )

    algo_cl_ord_id = "pg" + secrets.token_hex(8)
    result = await algo.place_spot_grid(
        inst_id=intent["symbol"],
        max_px=str(bounds["upper_str"]),
        min_px=str(bounds["lower_str"]),
        grid_num=int(bounds["grid_count"]),
        quote_sz=str(bounds["quote_sz"]),
        run_type=str(bounds["run_type"]),
        sl_trigger_px=str(bounds["sl_trigger_px"]) if bounds.get("sl_trigger_px") else None,
        algo_cl_ord_id=algo_cl_ord_id,
    )
    result["bounds"] = bounds
    result["brake"] = brake
    return result


def _runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime:
    runtime = context.application.bot_data.get("runtime")
    if runtime is None:
        raise RuntimeError("机器人运行时未初始化")
    return runtime


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    mode = "模拟盘" if runtime.settings.is_demo else "实盘"
    await message.reply_text(
        "脉冲智网 PulseGrid AI\n\n"
        "用下方菜单操作，或直接发你的资金和持有计划，例如：\n"
        "我有 2000 U，打算持有 SOL，希望年化稳一点、能抗 15% 的暴跌。\n\n"
        "我会先理解这句话，再用 OKX 行情计算自适应网格和急刹车，然后给你一张确认卡。"
        "价格和下单参数只由本地量化引擎计算。菜单上的按钮不会送给模型。\n\n"
        f"当前环境：{mode}。点确认后才会向 OKX 提交网格。",
        reply_markup=main_menu_keyboard(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    if await route_menu_text(message.text, update, context):
        return
    runtime = _runtime(context)
    await message.chat.send_action(ChatAction.TYPING)
    try:
        proposal = await compose_grid_proposal(
            message.text,
            agent=runtime.agent,
            okx=runtime.okx,
            bar=runtime.settings.default_kline_bar,
        )
    except IntentParseError as exc:
        await message.reply_text(f"没能理解这条需求：{exc}")
        return
    except GroqAgentError as exc:
        await message.reply_text(f"模型调用失败：{exc}")
        return
    except (OkxClientError, QuantError) as exc:
        await message.reply_text(f"行情或网格计算失败：{exc}")
        return

    proposal["environment_label"] = "模拟盘" if runtime.settings.is_demo else "实盘"
    owner = update.effective_user
    async with runtime.lock:
        runtime.plans[proposal["plan_id"]] = {
            "proposal": proposal,
            "chat_id": message.chat_id,
            "user_id": owner.id if owner is not None else None,
            "submitting": False,
            "ts": time.time(),
        }
        watch = runtime.watches.setdefault(
            proposal["intent"]["symbol"],
            {"chat_ids": set(), "brake": None},
        )
        watch["chat_ids"].add(message.chat_id)
    text, markup = render_confirm_card(proposal)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_launch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    match = re.fullmatch(r"pg:([0-9a-f]{8})", query.data)
    if not match:
        await query.answer("确认单无效", show_alert=True)
        return
    runtime = _runtime(context)
    plan_id = match.group(1)
    busy = False
    owner_mismatch = False
    async with runtime.lock:
        entry = runtime.plans.get(plan_id)
        if entry is None or time.time() - float(entry["ts"]) > PLAN_TTL_SEC:
            runtime.plans.pop(plan_id, None)
            entry = None
        else:
            # 确认单只属于生成它的用户。先核对身份，再占提交锁，避免别人把单锁死。
            caller_id = query.from_user.id if query.from_user is not None else None
            if caller_id is None or caller_id != entry.get("user_id"):
                owner_mismatch = True
            elif entry["submitting"]:
                busy = True
            else:
                entry["submitting"] = True
    if entry is None:
        await query.answer("确认单已过期，请重新描述需求", show_alert=True)
        return
    if owner_mismatch:
        await query.answer("这不是你的确认单", show_alert=True)
        return
    if busy:
        await query.answer("正在提交，请稍候")
        return

    await query.answer()
    message = query.message
    if message is None:
        async with runtime.lock:
            current = runtime.plans.get(plan_id)
            if current is not None:
                current["submitting"] = False
        return
    proposal = entry["proposal"]
    try:
        result = await execute_confirmed_grid(proposal, okx=runtime.okx, algo=runtime.algo)
    except (BrakeActiveError, StrategyRejected) as exc:
        async with runtime.lock:
            current = runtime.plans.get(plan_id)
            if current is not None:
                current["submitting"] = False
        await message.reply_text(f"未下单：{exc}")
        return
    except OkxClientError as exc:
        async with runtime.lock:
            current = runtime.plans.get(plan_id)
            if current is not None:
                current["submitting"] = False
        logger.warning("OKX 下单失败: %s", exc)
        await message.reply_text(format_okx_trade_error(exc, action="下单"), reply_markup=main_menu_keyboard())
        return

    async with runtime.lock:
        runtime.plans.pop(plan_id, None)
    data = (result.get("data") or [{}])[0]
    algo_id = data.get("algoId") or "未知"
    bounds = result["bounds"]
    mode = "模拟盘" if runtime.settings.is_demo else "实盘"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.warning("移除确认按钮失败", exc_info=True)
    await message.reply_text(
        "网格已提交。\n"
        f"环境：{mode}\n"
        f"algoId：{algo_id}\n"
        f"区间：{bounds['lower_str']} – {bounds['upper_str']}\n"
        f"格数：{bounds['grid_count']}\n"
        f"投入：{bounds['quote_sz']} USDT\n"
        "AI Builder Code 已写入 OKX tag。",
        reply_markup=main_menu_keyboard(),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Telegram 处理失败", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(f"处理失败：{context.error}")
        except Exception:
            logger.exception("错误提示发送失败")


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler(["grids", "positions"], cmd_grids))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CallbackQueryHandler(on_launch, pattern=r"^pg:[0-9a-f]{8}$"))
    application.add_handler(
        CallbackQueryHandler(on_stop_callback, pattern=r"^pgstop(?:ok|no)?:[A-Za-z0-9]{1,40}$")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)
