"""Telegram 主菜单、网格列表和停止流程。

菜单按钮只做导航，不会把按钮文案送给 Groq。
停止网格沿用确认卡的归属校验：只有打开列表的用户能确认，确认后才调用已有的 stop_spot_grid。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from core.okx.client import OkxClientError

logger = logging.getLogger("pulsegrid.bot.menu")

# 与确认单相同的有效期。过期后必须重新打开停止列表。
STOP_DRAFT_TTL_SEC = 15 * 60
DISPLAY_LIMIT = 20

MENU_NEW = "📝 新建策略"
MENU_GRIDS = "📊 我的网格"
MENU_STOP = "🛑 停止网格"
MENU_STATUS = "📡 状态"
MENU_HELP = "❓ 帮助"

# 键盘上的顺序。测试和文案都用这一份，避免按钮和路由各写一遍。
MENU_LABELS: tuple[str, ...] = (MENU_NEW, MENU_GRIDS, MENU_STOP, MENU_STATUS, MENU_HELP)

MENU_ACTIONS = {
    MENU_NEW: "new",
    MENU_GRIDS: "grids",
    MENU_STOP: "stop",
    MENU_STATUS: "status",
    MENU_HELP: "help",
}

STATE_LABELS = {
    "starting": "启动中",
    "running": "运行中",
    "stopping": "停止中",
    "pending_signal": "等待信号",
    "no_close_position": "未平仓",
}

# 现货网格查询已经带 algoOrdType=grid。这里再挡掉明确的合约类型。
_NON_SPOT_INST_TYPES = frozenset({"SWAP", "FUTURES", "OPTION", "CONTRACT"})
_ALGO_ID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
_STOP_CB_RE = re.compile(r"^pgstop(?P<action>ok|no)?:(?P<algo>[A-Za-z0-9]{1,40})$")

HELP_TEXT = (
    "脉冲智网 · 使用说明\n\n"
    "怎么开一个网格\n"
    "1. 点「📝 新建策略」，或直接发一句话。\n"
    "2. 例如：我有 2000 U，打算持有 SOL，希望年化稳一点、能抗 15% 的暴跌。\n"
    "3. 机器人回复一张确认卡。现价、上下沿、格数、止损都来自本地量化引擎。\n"
    "4. 只有你点「⚡ 一键在 OKX 开启网格」之后才会下单。\n\n"
    "确认卡\n"
    "· 模型只写中文说明，不能改卡上的价格。\n"
    "· 急刹车或 OI/CVD 数据不足时，没有下单按钮。\n"
    "· 有按钮时，点下去还会再查一次哨兵，不通过就不下单。\n"
    "· 确认号大约 15 分钟内有效，而且只有生成这张卡的用户能点。\n"
    "· 方向是做空时，不会自动开空。Phase-1 只提交现货网格。\n\n"
    "菜单\n"
    "📝 新建策略：请你输入自然语言需求，按钮本身不会送给模型。\n"
    "📊 我的网格：列出 OKX 上还未停止的现货网格（交易对、algoId、区间、状态）。\n"
    "🛑 停止网格：点选一个网格，再确认一次，才会调用停止。\n"
    "📡 状态：模拟盘或实盘、模型、监控中的交易对数量。\n"
    "❓ 帮助：本说明。\n\n"
    "铁律\n"
    "· Groq 只做意图理解和中文说明，不计算价格、网格或下单参数。\n"
    "· 行情、ATR、VWAP、CVD、OI 急刹车和网格数学都在 Python 里完成。\n"
    "{builder_status}\n\n"
    "命令\n"
    "/start  /menu  打开主菜单\n"
    "/grids  /positions  我的网格\n"
    "/stop  停止网格\n"
    "/status  状态\n"
    "/help  帮助"
)

NEW_STRATEGY_TEXT = (
    "请直接发送你的资金和持有计划。下一句普通需求才会生成确认卡，这个菜单按钮不会送给模型。\n\n"
    "例如：\n"
    "我有 2000 U，打算持有 SOL，希望年化稳一点、能抗 15% 的暴跌。\n\n"
    "价格和下单参数只由本地量化引擎计算。确认卡上点过之前不会下单。"
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """常驻回复键盘。/start 和各菜单动作都会带上它。"""
    return ReplyKeyboardMarkup(
        [
            [MENU_NEW, MENU_GRIDS],
            [MENU_STOP, MENU_STATUS],
            [MENU_HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="描述资金和持有计划，或点菜单",
    )


def is_menu_text(text: str) -> bool:
    """按钮文案是否应走菜单，而不是意图解析。"""
    return text.strip() in MENU_ACTIONS


MISSING_BUILDER_CODE_TEXT = (
    "AI Builder Code：未配置。\n"
    "确认下单和停止网格会失败，直到在 pulsegrid/.env 设置 OKX_AI_BUILDER_CODE 并重启机器人。\n"
    "在此之前不会向 OKX 发送交易请求。"
)


def builder_code_missing(settings: object) -> bool:
    """空字符串表示未配置。没有该字段的测试替身不拦截，避免误伤旧用例。"""
    if not hasattr(settings, "okx_ai_builder_code"):
        return False
    return not str(getattr(settings, "okx_ai_builder_code") or "").strip()


def format_builder_code_status(code: object) -> str:
    text = str(code or "").strip()
    if not text:
        return MISSING_BUILDER_CODE_TEXT
    masked = (text[:2] + "…" + text[-2:]) if len(text) > 4 else "已配置"
    return f"AI Builder Code：{masked}\n确认下单和停止网格会把该码写入 OKX 的 tag。"


def render_help(code: object) -> str:
    return HELP_TEXT.format(builder_status=format_builder_code_status(code))


def format_okx_trade_error(exc: OkxClientError, *, action: str = "请求") -> str:
    """把交易路径的异常收成中文。缺 Builder Code 时说明确认下单和停止网格都会失败。"""
    text = str(exc)
    if "OKX_AI_BUILDER_CODE" in text or "未配置" in text and "tag" in text:
        return MISSING_BUILDER_CODE_TEXT
    return f"OKX {action}失败：{text}"


def _runtime(context: ContextTypes.DEFAULT_TYPE) -> Any:
    runtime = context.application.bot_data.get("runtime")
    if runtime is None:
        raise RuntimeError("机器人运行时未初始化")
    return runtime


def _mode_label(runtime: Any) -> str:
    return "模拟盘" if runtime.settings.is_demo else "实盘"


def _plain(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _state_label(state: object) -> str:
    raw = _plain(state)
    if not raw:
        return "未知"
    return STATE_LABELS.get(raw, raw)


def spot_grid_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 orders-algo-pending 响应里取出现货网格。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        algo_type = _plain(item.get("algoOrdType")) or "grid"
        if algo_type != "grid":
            continue
        inst_type = _plain(item.get("instType")).upper()
        if inst_type in _NON_SPOT_INST_TYPES:
            continue
        rows.append(item)
    return rows


def format_grid_block(item: dict[str, Any], index: int) -> str:
    inst = _plain(item.get("instId")) or "未知交易对"
    algo_id = _plain(item.get("algoId")) or "未知"
    lines = [
        f"{index}. {inst}",
        f"   algoId：{algo_id}",
    ]
    lower = _plain(item.get("minPx"))
    upper = _plain(item.get("maxPx"))
    if lower or upper:
        lines.append(f"   区间：{lower or '—'} – {upper or '—'}")
    grid_num = _plain(item.get("gridNum"))
    if grid_num:
        lines.append(f"   格数：{grid_num}")
    lines.append(f"   状态：{_state_label(item.get('state'))}")
    return "\n".join(lines)


def format_pending_grids(rows: list[dict[str, Any]], *, mode_label: str, hidden: int = 0) -> str:
    if not rows:
        return (
            f"当前没有未停止的现货网格（{mode_label}）。\n"
            "点「📝 新建策略」，用一句话描述资金和持有计划，确认后再开启。"
        )
    lines = [f"现货网格（{mode_label}）共 {len(rows) + hidden} 个：", ""]
    for index, item in enumerate(rows, start=1):
        lines.append(format_grid_block(item, index))
        lines.append("")
    if hidden:
        lines.append(f"还有 {hidden} 个未在这里展开。")
    lines.append("区间来自 OKX 返回的上下界，不是模型算出来的。")
    return "\n".join(lines).strip()


def _actionable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """停止中的单不再给按钮。algoId 必须能放进 callback_data。"""
    picked: list[dict[str, Any]] = []
    for item in rows:
        if _plain(item.get("state")) == "stopping":
            continue
        algo_id = _plain(item.get("algoId"))
        inst_id = _plain(item.get("instId"))
        if not inst_id or not _ALGO_ID_RE.fullmatch(algo_id):
            continue
        picked.append(item)
    return picked


def stop_inline_keyboard(rows: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    actionable = _actionable(rows)
    if not actionable:
        return None
    inst_counts: dict[str, int] = {}
    for item in actionable:
        inst = _plain(item.get("instId"))
        inst_counts[inst] = inst_counts.get(inst, 0) + 1
    buttons: list[list[InlineKeyboardButton]] = []
    for item in actionable:
        inst = _plain(item.get("instId"))
        algo_id = _plain(item.get("algoId"))
        if inst_counts.get(inst, 0) > 1:
            label = f"🛑 {inst} · {algo_id[-4:]}"
        else:
            label = f"🛑 {inst}"
        if len(label.encode("utf-8")) > 60:
            label = f"🛑 {algo_id[-6:]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pgstop:{algo_id}")])
    return InlineKeyboardMarkup(buttons)


def _confirm_keyboard(algo_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("确认停止", callback_data=f"pgstopok:{algo_id}"),
            InlineKeyboardButton("先不停止", callback_data=f"pgstopno:{algo_id}"),
        ]]
    )


async def _reply_menu(message: Any, text: str, **kwargs: Any) -> None:
    markup = kwargs.pop("reply_markup", main_menu_keyboard())
    await message.reply_text(text, reply_markup=markup, **kwargs)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await _reply_menu(
        message,
        "主菜单\n\n"
        "📝 新建策略 — 用一句话描述资金和持有计划\n"
        "📊 我的网格 — 查看未停止的现货网格\n"
        "🛑 停止网格 — 点选并再次确认后停止\n"
        "📡 状态 — 环境、模型、监控数量\n"
        "❓ 帮助 — 用法、铁律和确认卡说明",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    await _reply_menu(message, render_help(runtime.settings.okx_ai_builder_code))


async def cmd_new_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await _reply_menu(message, NEW_STRATEGY_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    settings = runtime.settings
    async with runtime.lock:
        watch_count = len(runtime.watches)
        plan_count = len(runtime.plans)
    await _reply_menu(
        message,
        f"环境：{'模拟盘' if settings.is_demo else '实盘'}\n"
        f"模型：{settings.groq_model}\n"
        f"K 线周期：{settings.default_kline_bar}\n"
        f"监控中的交易对：{watch_count}\n"
        f"待确认策略：{plan_count}\n"
        f"{format_builder_code_status(settings.okx_ai_builder_code)}\n"
        "订单簿监控与下单在同一个事件循环里并发执行。",
    )


async def _fetch_spot_grids(runtime: Any) -> list[dict[str, Any]]:
    payload = await runtime.algo.list_pending("grid")
    return spot_grid_rows(payload)


def _slice_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    shown = rows[:DISPLAY_LIMIT]
    return shown, len(rows) - len(shown)


async def cmd_grids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context)
    await message.chat.send_action(ChatAction.TYPING)
    try:
        rows = await _fetch_spot_grids(runtime)
    except OkxClientError as exc:
        logger.warning("读取网格失败: %s", exc)
        await _reply_menu(message, f"读取网格失败：{exc}")
        return
    shown, hidden = _slice_rows(rows)
    await _reply_menu(message, format_pending_grids(shown, mode_label=_mode_label(runtime), hidden=hidden))


async def _remember_stop_drafts(
    runtime: Any,
    *,
    user_id: int,
    chat_id: int,
    rows: list[dict[str, Any]],
) -> None:
    """同一 algoId 以最近一次打开停止列表的用户为准。"""
    now = time.time()
    async with runtime.lock:
        drafts: dict[str, dict[str, Any]] = runtime.stop_drafts
        expired = [key for key, item in drafts.items() if now - float(item.get("ts", 0)) > STOP_DRAFT_TTL_SEC]
        for key in expired:
            drafts.pop(key, None)
        owned = [key for key, item in drafts.items() if item.get("user_id") == user_id]
        for key in owned:
            drafts.pop(key, None)
        for item in _actionable(rows):
            algo_id = _plain(item.get("algoId"))
            drafts[algo_id] = {
                "user_id": user_id,
                "chat_id": chat_id,
                "inst_id": _plain(item.get("instId")),
                "min_px": _plain(item.get("minPx")),
                "max_px": _plain(item.get("maxPx")),
                "state": _plain(item.get("state")),
                "armed": False,
                "submitting": False,
                "ts": now,
            }


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    owner = update.effective_user
    if owner is None:
        await _reply_menu(message, "无法确认你的身份，不能停止网格。")
        return
    runtime = _runtime(context)
    await message.chat.send_action(ChatAction.TYPING)
    try:
        rows = await _fetch_spot_grids(runtime)
    except OkxClientError as exc:
        logger.warning("读取待停止网格失败: %s", exc)
        await _reply_menu(message, f"读取网格失败：{exc}")
        return
    shown, hidden = _slice_rows(rows)
    await _remember_stop_drafts(runtime, user_id=owner.id, chat_id=message.chat_id, rows=shown)
    listing = format_pending_grids(shown, mode_label=_mode_label(runtime), hidden=hidden)
    keyboard = stop_inline_keyboard(shown)
    if keyboard is None:
        if shown:
            listing += "\n\n这些网格正在停止，或编号无法放进按钮，当前没有可确认的停止操作。"
        await _reply_menu(message, listing)
        return
    text = (
        "选择要停止的现货网格。点选后还要再确认一次，确认前不会向 OKX 发送停止请求。\n"
        "停止时会卖出网格里的基础货币。\n\n"
        f"{listing}"
    )
    await message.reply_text(text, reply_markup=keyboard)


def _bounds_line(entry: dict[str, Any]) -> str:
    lower = _plain(entry.get("min_px"))
    upper = _plain(entry.get("max_px"))
    if not lower and not upper:
        return ""
    return f"区间：{lower or '—'} – {upper or '—'}\n"


async def _load_draft(
    runtime: Any,
    algo_id: str,
    user_id: int | None,
    *,
    arm: bool = False,
    disarm: bool = False,
    submit: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    now = time.time()
    async with runtime.lock:
        drafts: dict[str, dict[str, Any]] = runtime.stop_drafts
        entry = drafts.get(algo_id)
        if entry is None or now - float(entry.get("ts", 0)) > STOP_DRAFT_TTL_SEC:
            drafts.pop(algo_id, None)
            return "expired", None
        if user_id is None or user_id != entry.get("user_id"):
            return "owner", None
        if submit:
            if not entry.get("armed"):
                return "unarmed", None
            if entry.get("submitting"):
                return "busy", None
            entry["submitting"] = True
            return "ok", dict(entry)
        if arm:
            entry["armed"] = True
            entry["ts"] = now
        if disarm:
            entry["armed"] = False
        return "ok", dict(entry)


async def _reset_submitting(runtime: Any, algo_id: str) -> None:
    async with runtime.lock:
        entry = runtime.stop_drafts.get(algo_id)
        if entry is not None:
            entry["submitting"] = False


async def on_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    match = _STOP_CB_RE.fullmatch(query.data)
    if not match:
        await query.answer("停止确认无效", show_alert=True)
        return
    action = match.group("action")
    algo_id = match.group("algo")
    runtime = _runtime(context)
    caller_id = query.from_user.id if query.from_user is not None else None

    if action is None:
        status, entry = await _load_draft(runtime, algo_id, caller_id, arm=True)
        if status == "expired":
            await query.answer("停止确认已过期，请重新打开停止网格", show_alert=True)
            return
        if status == "owner" or entry is None:
            await query.answer("这不是你的停止确认", show_alert=True)
            return
        await query.answer()
        message = query.message
        if message is None:
            return
        await message.reply_text(
            "确认停止这个现货网格？\n"
            f"交易对：{entry['inst_id']}\n"
            f"algoId：{algo_id}\n"
            f"{_bounds_line(entry)}"
            f"状态：{_state_label(entry.get('state'))}\n"
            "确认后才会向 OKX 发送停止请求，并卖出网格里的基础货币。\n"
            "AI Builder Code 会写入 OKX 的 tag。",
            reply_markup=_confirm_keyboard(algo_id),
        )
        return

    if action == "no":
        status, entry = await _load_draft(runtime, algo_id, caller_id, disarm=True)
        if status == "expired":
            await query.answer("停止确认已过期，请重新打开停止网格", show_alert=True)
            return
        if status == "owner" or entry is None:
            await query.answer("这不是你的停止确认", show_alert=True)
            return
        await query.answer("已取消")
        message = query.message
        if message is not None:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                logger.warning("移除停止按钮失败", exc_info=True)
            await _reply_menu(message, f"已取消停止 {entry['inst_id']}（algoId {algo_id}）。网格仍在运行。")
        return

    status, entry = await _load_draft(runtime, algo_id, caller_id, submit=True)
    if status == "expired":
        await query.answer("停止确认已过期，请重新打开停止网格", show_alert=True)
        return
    if status == "owner":
        await query.answer("这不是你的停止确认", show_alert=True)
        return
    if status == "unarmed":
        await query.answer("请先点选要停止的网格", show_alert=True)
        return
    if status == "busy":
        await query.answer("正在提交，请稍候")
        return
    if entry is None:
        await query.answer("停止确认无效", show_alert=True)
        return

    await query.answer()
    message = query.message
    if message is None:
        await _reset_submitting(runtime, algo_id)
        return
    if builder_code_missing(runtime.settings):
        await _reset_submitting(runtime, algo_id)
        await _reply_menu(message, MISSING_BUILDER_CODE_TEXT)
        return
    try:
        await runtime.algo.stop_spot_grid(algo_id=algo_id, inst_id=str(entry["inst_id"]), stop_type="1")
    except OkxClientError as exc:
        await _reset_submitting(runtime, algo_id)
        logger.warning("停止网格失败: %s", exc)
        await _reply_menu(message, format_okx_trade_error(exc, action="停止网格"))
        return
    except Exception:
        await _reset_submitting(runtime, algo_id)
        logger.exception("停止网格失败")
        raise

    async with runtime.lock:
        runtime.stop_drafts.pop(algo_id, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.warning("移除停止确认按钮失败", exc_info=True)
    await _reply_menu(
        message,
        "停止请求已提交。\n"
        f"环境：{_mode_label(runtime)}\n"
        f"交易对：{entry['inst_id']}\n"
        f"algoId：{algo_id}\n"
        "网格里的基础货币会在停止时卖出。\n"
        "AI Builder Code 已写入 OKX tag。",
    )


async def route_menu_text(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """菜单文案在这里结束。返回 False 时，调用方才可以把原文交给意图解析。"""
    action = MENU_ACTIONS.get(text.strip())
    if action is None:
        return False
    if action == "new":
        await cmd_new_strategy(update, context)
    elif action == "grids":
        await cmd_grids(update, context)
    elif action == "stop":
        await cmd_stop(update, context)
    elif action == "status":
        await cmd_status(update, context)
    elif action == "help":
        await cmd_help(update, context)
    else:
        return False
    return True
