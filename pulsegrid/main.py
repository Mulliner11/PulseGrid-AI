"""启动 Telegram 机器人，并在同一事件循环里跑订单簿哨兵。"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram import Update
from telegram.ext import Application

from bot.handlers import Runtime, _brake_from_bundle, register_handlers
from config.settings import Settings, get_settings
from core.llm.groq_client import GroqAgent
from core.okx.client import OkxRestClient
from core.okx.strategy_algo import OkxStrategyAlgo

logger = logging.getLogger("pulsegrid")


async def run_orderbook_monitor(runtime: Runtime, bot: object, stop_event: asyncio.Event) -> None:
    """后台刷新已关注交易对的 OI/CVD。与收消息、下单互不阻塞。"""
    while not stop_event.is_set():
        async with runtime.lock:
            snapshot = [(inst_id, set(meta["chat_ids"])) for inst_id, meta in runtime.watches.items()]
        for inst_id, chat_ids in snapshot:
            if stop_event.is_set() or not chat_ids:
                continue
            try:
                bundle = await runtime.okx.fetch_market_bundle(inst_id, runtime.settings.default_kline_bar)
                brake = _brake_from_bundle(bundle)
                tripped = bool(brake.get("Emergency_Brake") and brake.get("data_sufficient"))
            except Exception:
                logger.exception("订单簿监控失败 %s", inst_id)
                continue
            should_notify = False
            async with runtime.lock:
                meta = runtime.watches.get(inst_id)
                if meta is None:
                    continue
                was_tripped = meta.get("brake")
                meta["brake"] = tripped
                should_notify = tripped and was_tripped is not True
            if not should_notify:
                continue
            text = f"脉冲智网哨兵：{inst_id} 急刹车触发。{brake['reason']} 解除前不会买入开网格。"
            for chat_id in chat_ids:
                try:
                    await bot.send_message(chat_id=chat_id, text=text)  # type: ignore[attr-defined]
                except Exception:
                    logger.exception("哨兵通知失败 chat=%s", chat_id)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=runtime.settings.monitor_interval_sec)
        except TimeoutError:
            continue


def build_runtime(settings: Settings) -> Runtime:
    okx = OkxRestClient(
        api_key=settings.okx_api_key,
        secret_key=settings.okx_secret_key,
        passphrase=settings.okx_passphrase,
        flag=settings.okx_flag,
        ai_builder_code=settings.okx_ai_builder_code,
        base_url=settings.okx_base_url,
    )
    return Runtime(
        settings=settings,
        agent=GroqAgent(settings.groq_api_key, settings.groq_model),
        okx=okx,
        algo=OkxStrategyAlgo(okx),
    )


def build_application(runtime: Runtime) -> Application:
    stop_event = asyncio.Event()

    async def post_init(application: Application) -> None:
        # 监控是独立 task。下单在回调里 await，不会卡住轮询线程之外的协程。
        application.bot_data["monitor_task"] = asyncio.create_task(
            run_orderbook_monitor(runtime, application.bot, stop_event),
            name="orderbook-monitor",
        )

    async def post_shutdown(application: Application) -> None:
        stop_event.set()
        task = application.bot_data.get("monitor_task")
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except TimeoutError:
                task.cancel()
        await runtime.okx.aclose()

    application = (
        Application.builder()
        .token(runtime.settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["runtime"] = runtime
    register_handlers(application)
    return application


def run_self_check() -> int:
    import unittest

    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if "--check" in sys.argv:
        raise SystemExit(run_self_check())

    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())
    missing = settings.missing_runtime_keys()
    if missing:
        raise SystemExit(
            "缺少环境变量: "
            + ", ".join(missing)
            + "。请进入 pulsegrid 目录，复制 .env.example 为 .env 并填写后再运行 python main.py。"
        )
    if not settings.is_demo:
        logger.warning("OKX_FLAG=0，当前连接实盘，确认后的网格会使用真实资金")
    runtime = build_runtime(settings)
    application = build_application(runtime)
    logger.info("脉冲智网启动，模型=%s，OKX flag=%s", settings.groq_model, settings.okx_flag)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
