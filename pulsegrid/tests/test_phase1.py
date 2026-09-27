"""不访问外网的 Phase-1 校验。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from pydantic import ValidationError

from telegram.ext import Application, CommandHandler

from bot.handlers import (
    StrategyRejected,
    compose_grid_proposal,
    execute_confirmed_grid,
    on_launch,
    on_text,
    register_handlers,
)
from bot.menu import (
    MENU_GRIDS,
    MENU_HELP,
    MENU_LABELS,
    MENU_NEW,
    MENU_STATUS,
    MENU_STOP,
    is_menu_text,
    main_menu_keyboard,
)
from bot.ui_cards import LAUNCH_BUTTON_TEXT, render_confirm_card
from config.settings import Settings
from core.llm.groq_client import (
    INTENT_SYSTEM_PROMPT,
    GroqAgent,
    IntentParseError,
    extract_json_object,
    fallback_rationale,
    narrative_respects_metrics,
    normalize_intent,
)
from core.okx.client import TRADE_PATHS, OkxClientError, OkxRestClient
from core.okx.strategy_algo import OkxStrategyAlgo
from core.quant.grid_engine import align_bounds_to_tick, calculate_adaptive_bounds
from core.quant.indicators import atr
from core.quant.sentinel import evaluate_orderbook_brake


class ScriptedCompleter:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if not self.answers:
            raise AssertionError("脚本化模型没有更多回复")
        return self.answers.pop(0)


def _candles(n: int = 48) -> list[list[str]]:
    """时间升序的 OKX K 线数组。前半段震荡，末尾缓步上行，保证 ATR 和价格斜率稳定。"""
    rows: list[list[str]] = []
    start_ts = 1_700_000_000_000
    for i in range(n):
        if i < n - 16:
            close = 150 + math.sin(i / 2) * 2.0
        else:
            close = 150 + (i - (n - 16)) * 0.15
        open_ = close - 0.2
        high = max(open_, close) + 0.55
        low = min(open_, close) - 0.55
        volume = 100 + i
        rows.append(
            [
                str(start_ts + i * 900_000),
                f"{open_:.4f}",
                f"{high:.4f}",
                f"{low:.4f}",
                f"{close:.4f}",
                str(volume),
                "0",
                "0",
                "1",
            ]
        )
    return rows


def _as_klines(rows: list[list[str]]) -> list[dict]:
    return [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "confirm": row[8],
        }
        for row in rows
    ]


def _ok(data: object) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


class QuantTests(unittest.TestCase):
    def test_wilder_atr(self) -> None:
        rows = [
            {"high": 10, "low": 8, "close": 9, "open": 9, "volume": 1},
            {"high": 11, "low": 9, "close": 10, "open": 9, "volume": 1},
            {"high": 12, "low": 10, "close": 11, "open": 10, "volume": 1},
            {"high": 11, "low": 8, "close": 9, "open": 11, "volume": 1},
            {"high": 10, "low": 7, "close": 8, "open": 9, "volume": 1},
        ]
        self.assertAlmostEqual(atr(rows, period=3), 23 / 9, places=6)

    def test_grid_covers_declared_drawdown(self) -> None:
        klines = _as_klines(_candles())
        bounds = calculate_adaptive_bounds(
            klines,
            {"risk_tolerance": "conservative", "max_drawdown_limit": 0.15},
        )
        price = bounds["last_price"]
        self.assertLessEqual(bounds["lower"], price * 0.85 + 1e-9)
        self.assertGreater(bounds["upper"], price)
        self.assertGreater(bounds["lower"], 0)
        self.assertGreaterEqual(bounds["grid_count"], 2)
        self.assertLessEqual(bounds["grid_count"], 100)
        self.assertGreater(bounds["per_grid_profit_rate"], 0)
        self.assertEqual(bounds["run_type"], "2")
        self.assertEqual(bounds["atr_period"], 14)

    def test_brake_on_oi_rise_and_cvd_divergence(self) -> None:
        closes = [{"high": 10 + i * 0.2, "low": 9 + i * 0.2, "close": 9.5 + i * 0.2, "open": 9.4 + i * 0.2, "volume": 1} for i in range(16)]
        oi = [100 + i * 2 for i in range(16)]
        cvd = [1000 - i * 5 for i in range(16)]
        result = evaluate_orderbook_brake(closes, oi, cvd, oi_rise_threshold=0.05, lookback=12)
        self.assertTrue(result["data_sufficient"])
        self.assertTrue(result["Emergency_Brake"])
        self.assertTrue(result["block_buys"])

    def test_brake_stays_off_without_both_signals(self) -> None:
        closes = [{"high": 11, "low": 9, "close": 10 + i * 0.1, "open": 10, "volume": 1} for i in range(16)]
        flat_oi = [100] * 16
        falling_cvd = [500 - i for i in range(16)]
        quiet = evaluate_orderbook_brake(closes, flat_oi, falling_cvd, lookback=12)
        self.assertFalse(quiet["Emergency_Brake"])

        rising_oi = [100 + i * 5 for i in range(16)]
        rising_cvd = [i * 3 for i in range(16)]
        crowded = evaluate_orderbook_brake(closes, rising_oi, rising_cvd, lookback=12)
        self.assertFalse(crowded["Emergency_Brake"])

        falling_price = [{"high": 20 - i, "low": 19 - i, "close": 19.5 - i, "open": 20 - i, "volume": 1} for i in range(16)]
        cascade = evaluate_orderbook_brake(falling_price, rising_oi, falling_cvd, lookback=12)
        self.assertFalse(cascade["Emergency_Brake"])
        self.assertTrue(cascade["data_sufficient"])

    def test_approximate_cvd_cannot_clear_or_brake(self) -> None:
        # 收盘逐根抬高，但每根收在开盘之下，K 线近似 CVD 会向下。旧逻辑会因此急刹车。
        rows = []
        price = 100.0
        for _ in range(16):
            open_ = price + 0.4
            close = price + 0.2
            rows.append(
                {
                    "open": open_,
                    "high": open_ + 0.1,
                    "low": close - 0.1,
                    "close": close,
                    "volume": 10,
                }
            )
            price = close
        oi = [100 + i * 8 for i in range(16)]
        result = evaluate_orderbook_brake(rows, oi, None, oi_rise_threshold=0.05, lookback=12)
        self.assertEqual(result["cvd_source"], "kline_tick_rule")
        self.assertFalse(result["data_sufficient"])
        self.assertFalse(result["Emergency_Brake"])
        self.assertFalse(result["block_buys"])
        self.assertGreater(result["oi_change_pct"], 0.05)
        self.assertLess(result["cvd_slope"], 0)
        self.assertGreaterEqual(result["price_slope"], 0)
        self.assertIn("不能作为放行或急刹车依据", result["reason"])

    def test_short_sample_is_not_a_silent_all_clear(self) -> None:
        closes = [{"high": 2, "low": 1, "close": 1.5, "open": 1.4, "volume": 1}] * 3
        result = evaluate_orderbook_brake(closes, [1, 2, 3], [3, 2, 1], lookback=12)
        self.assertFalse(result["data_sufficient"])
        self.assertFalse(result["Emergency_Brake"])
        self.assertIn("样本不足", result["reason"])


class GroqContractTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_and_normalize_example(self) -> None:
        raw = """```json
        {"symbol":"sol","budget":"2000 U","risk_tolerance":"稳健","direction":"持有","max_drawdown_limit":15}
        ```"""
        intent = normalize_intent(extract_json_object(raw))
        self.assertEqual(intent["symbol"], "SOL-USDT")
        self.assertEqual(intent["budget"], 2000.0)
        self.assertEqual(intent["risk_tolerance"], "conservative")
        self.assertFalse(intent["risk_defaulted"])
        self.assertEqual(intent["direction"], "neutral")
        self.assertEqual(intent["max_drawdown_limit"], 0.15)

    def test_nested_json_is_not_cut_at_the_first_brace(self) -> None:
        raw = """前文
        ```json
        {"symbol":"SOL","budget":10,"risk_tolerance":"balanced","direction":"neutral","nested":{"a":1,"b":{"c":2}}}
        ```
        后文 {不是对象"""
        parsed = extract_json_object(raw)
        self.assertEqual(parsed["nested"]["b"]["c"], 2)
        self.assertEqual(parsed["budget"], 10)

    def test_missing_risk_defaults_to_balanced(self) -> None:
        base = {"symbol": "SOL", "budget": 100, "direction": "neutral"}
        for raw_risk in ({}, {"risk_tolerance": None}, {"risk_tolerance": "  "}):
            intent = normalize_intent({**base, **raw_risk})
            self.assertEqual(intent["risk_tolerance"], "balanced")
            self.assertTrue(intent["risk_defaulted"])
        explicit = normalize_intent({**base, "risk_tolerance": "conservative"})
        self.assertEqual(explicit["risk_tolerance"], "conservative")
        self.assertFalse(explicit["risk_defaulted"])
        with self.assertRaises(IntentParseError):
            normalize_intent({**base, "risk_tolerance": "yolo"})

    async def test_parse_user_intent_uses_json_mode_and_does_not_price(self) -> None:
        payload = {
            "symbol": "SOL-USDT",
            "budget": 2000,
            "risk_tolerance": "conservative",
            "direction": "neutral",
            "max_drawdown_limit": 0.15,
        }
        completer = ScriptedCompleter([json.dumps(payload)])
        agent = GroqAgent(completer=completer)
        intent = await agent.parse_user_intent("我有 2000 U，打算持有 SOL，希望年化稳一点、能抗 15% 的暴跌")
        self.assertEqual(intent["symbol"], "SOL-USDT")
        self.assertEqual(intent["budget"], 2000.0)
        self.assertIn("不计算价格", INTENT_SYSTEM_PROMPT)
        self.assertEqual(completer.calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(completer.calls[0]["temperature"], 0)

    async def test_missing_budget_is_an_error(self) -> None:
        completer = ScriptedCompleter([json.dumps({"symbol": "SOL", "budget": None, "risk_tolerance": "balanced", "direction": "neutral"})])
        agent = GroqAgent(completer=completer)
        with self.assertRaises(IntentParseError):
            await agent.parse_user_intent("帮我做个网格")

    async def test_invented_number_is_replaced(self) -> None:
        metrics = {
            "symbol": "SOL-USDT",
            "last_price": 150.25,
            "atr_14": 2.5,
            "grid_upper": 160.0,
            "grid_lower": 127.7,
            "grid_count": 10,
            "per_grid_profit_rate": 0.0228,
            "emergency_brake": False,
            "risk_tolerance": "conservative",
            "max_drawdown_limit": 0.15,
        }
        self.assertFalse(narrative_respects_metrics("建议把上沿改到 99999。", metrics))
        # 14 是 ATR 周期，不是价格。指标值本身没有 14 时也不该因此打回本地模板。
        period_sentence = "SOL-USDT 的 14 周期 ATR 为 2.5000，区间 127.7000 到 160.0000。"
        self.assertTrue(narrative_respects_metrics(period_sentence, metrics))
        self.assertTrue(narrative_respects_metrics("14 周期 ATR", {"atr": 2.5, "symbol": "SOL-USDT"}))
        self.assertFalse(narrative_respects_metrics("21 周期", {"atr": 2.5, "symbol": "SOL-USDT"}))
        fallback = fallback_rationale("SOL-USDT", metrics)
        self.assertIn("14 周期", fallback)
        self.assertTrue(narrative_respects_metrics(fallback, metrics))
        kept = GroqAgent(completer=ScriptedCompleter([period_sentence]))
        kept_text = await kept.generate_strategy_rationale("SOL-USDT", metrics)
        self.assertIn("14 周期", kept_text)
        self.assertNotIn("模型不负责填写价格", kept_text)
        completer = ScriptedCompleter(["建议把上沿改到 99999，年化能到 80%。"])
        agent = GroqAgent(completer=completer)
        text = await agent.generate_strategy_rationale("SOL-USDT", metrics)
        self.assertNotIn("99999", text)
        self.assertIn("150.2500", text)
        self.assertIn("模型不负责填写价格", text)


def _sample_proposal(brake: dict, **intent_extra: object) -> dict:
    return {
        "plan_id": "abcd1234",
        "intent": {
            "symbol": "SOL-USDT",
            "direction": "neutral",
            "budget": 2000,
            "risk_tolerance": "balanced",
            "max_drawdown_limit": 0.15,
            **intent_extra,
        },
        "bounds": {
            "upper_str": "160",
            "lower_str": "120",
            "upper": 160.0,
            "lower": 120.0,
            "last_price": 150.0,
            "atr": 2.5,
            "grid_count": 10,
            "per_grid_profit_rate": 0.01,
            "run_type": "2",
            "quote_sz": "2000",
            "sl_trigger_px": "119",
        },
        "brake": brake,
        "rationale": "说明",
    }


class ConfirmCardTests(unittest.TestCase):
    def test_launch_button_only_when_brake_is_clear(self) -> None:
        clear, clear_markup = render_confirm_card(
            _sample_proposal({"data_sufficient": True, "Emergency_Brake": False, "block_buys": False, "reason": "未触发"})
        )
        self.assertIsNotNone(clear_markup)
        assert clear_markup is not None
        self.assertEqual(clear_markup.inline_keyboard[0][0].text, LAUNCH_BUTTON_TEXT)
        self.assertNotIn("请重新描述你的需求", clear)

        insufficient_text, insufficient_markup = render_confirm_card(
            _sample_proposal({"data_sufficient": False, "Emergency_Brake": False, "block_buys": False, "reason": "数据不足"})
        )
        self.assertIsNone(insufficient_markup)
        self.assertNotIn(LAUNCH_BUTTON_TEXT, insufficient_text)
        self.assertIn("请重新描述你的需求", insufficient_text)

        braking_text, braking_markup = render_confirm_card(
            _sample_proposal(
                {
                    "data_sufficient": True,
                    "Emergency_Brake": True,
                    "block_buys": True,
                    "reason": "禁止买入",
                }
            )
        )
        self.assertIsNone(braking_markup)
        self.assertNotIn(LAUNCH_BUTTON_TEXT, braking_text)

        blocked_text, blocked_markup = render_confirm_card(
            _sample_proposal({"data_sufficient": True, "Emergency_Brake": False, "block_buys": True, "reason": "禁买"})
        )
        self.assertIsNone(blocked_markup)
        self.assertNotIn(LAUNCH_BUTTON_TEXT, blocked_text)

    def test_risk_defaulted_is_shown(self) -> None:
        text, _markup = render_confirm_card(
            _sample_proposal(
                {"data_sufficient": True, "Emergency_Brake": False, "block_buys": False, "reason": "未触发"},
                risk_defaulted=True,
                direction_defaulted=True,
            )
        )
        self.assertIn("未说明风险偏好，按平衡处理。", text)
        self.assertIn("未说明方向，按中性网格处理。", text)


class _Query:
    def __init__(self, data: str, user_id: int | None) -> None:
        self.data = data
        self.from_user = None if user_id is None else SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, bool]] = []
        self.message = None

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class LaunchOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_user_cannot_launch(self) -> None:
        class Boom:
            async def fetch_market_bundle(self, *args, **kwargs):
                raise AssertionError("他人的确认单不应触发下单")

        runtime = SimpleNamespace(
            plans={
                "abcd1234": {
                    "proposal": {
                        "intent": {"direction": "neutral", "symbol": "SOL-USDT"},
                        "bounds": {},
                        "bar": "15m",
                    },
                    "chat_id": 10,
                    "user_id": 111,
                    "submitting": False,
                    "ts": time.time(),
                }
            },
            lock=asyncio.Lock(),
            okx=Boom(),
            algo=Boom(),
            settings=SimpleNamespace(is_demo=True),
        )
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"runtime": runtime}))
        query = _Query("pg:abcd1234", 222)
        await on_launch(SimpleNamespace(callback_query=query), context)  # type: ignore[arg-type]
        self.assertEqual(query.answers, [("这不是你的确认单", True)])
        self.assertFalse(runtime.plans["abcd1234"]["submitting"])

        missing = _Query("pg:abcd1234", None)
        await on_launch(SimpleNamespace(callback_query=missing), context)  # type: ignore[arg-type]
        self.assertEqual(missing.answers, [("这不是你的确认单", True)])
        self.assertFalse(runtime.plans["abcd1234"]["submitting"])


class SettingsTests(unittest.TestCase):
    def test_aliases_and_demo_flag(self) -> None:
        settings = Settings(
            GROQ_API_KEY="g",
            TELEGRAM_BOT_TOKEN="t",
            OKX_API_KEY="k",
            OKX_SECRET_KEY="s",
            OKX_PASSPHRASE="p",
            OKX_FLAG="0",
            OKX_AI_BUILDER_CODE="builder",
            _env_file=None,
        )
        self.assertEqual(settings.groq_model, "openai/gpt-oss-120b")
        overridden = Settings(
            GROQ_API_KEY="g",
            TELEGRAM_BOT_TOKEN="t",
            OKX_API_KEY="k",
            OKX_SECRET_KEY="s",
            OKX_PASSPHRASE="p",
            OKX_FLAG="0",
            OKX_AI_BUILDER_CODE="builder",
            GROQ_MODEL="custom-model",
            _env_file=None,
        )
        self.assertEqual(overridden.groq_model, "custom-model")
        self.assertEqual(settings.okx_ai_builder_code, "builder")
        self.assertFalse(settings.is_demo)
        self.assertEqual(settings.missing_runtime_keys(), [])

    def test_missing_keys_and_bad_flag(self) -> None:
        empty = Settings(
            GROQ_API_KEY="",
            TELEGRAM_BOT_TOKEN="",
            OKX_API_KEY="",
            OKX_SECRET_KEY="",
            OKX_PASSPHRASE="",
            OKX_AI_BUILDER_CODE="",
            OKX_FLAG="1",
            _env_file=None,
        )
        missing = set(empty.missing_runtime_keys())
        self.assertIn("GROQ_API_KEY", missing)
        self.assertIn("OKX_AI_BUILDER_CODE", missing)
        self.assertTrue(empty.is_demo)
        with self.assertRaises(ValidationError):
            Settings(OKX_FLAG="2", _env_file=None)


class OkxAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            await client.aclose()

    def _client(self, handler, *, flag: str = "1", code: str = "BUILDER1") -> OkxRestClient:
        self.client = OkxRestClient(
            api_key="key",
            secret_key="secret",
            passphrase="pass",
            flag=flag,
            ai_builder_code=code,
            transport=httpx.MockTransport(handler),
        )
        return self.client

    async def test_tag_is_ai_builder_code_on_every_trade_path(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _ok([{"sCode": "0", "sMsg": "", "algoId": "1", "ordId": "2", "tag": "BUILDER1"}])

        client = self._client(handler)
        algo = OkxStrategyAlgo(client)
        await algo.place_spot_grid(
            inst_id="SOL-USDT",
            max_px="160",
            min_px="120",
            grid_num=10,
            quote_sz="2000",
            sl_trigger_px="119",
        )
        await algo.rebalance_grid_bounds(algo_id="1", max_px="161", min_px="119", grid_num=8)
        await algo.stop_spot_grid(algo_id="1", inst_id="SOL-USDT")
        await client.place_order(inst_id="SOL-USDT", side="buy", ord_type="limit", sz="1", px="150")

        self.assertEqual(len(captured), 4)
        for request in captured:
            body = json.loads(request.content.decode())
            self.assertIn(request.url.path, TRADE_PATHS)
            rows = body if isinstance(body, list) else [body]
            for row in rows:
                self.assertEqual(row["tag"], "BUILDER1")
                self.assertNotIn("aiBuilderCode", row)
            self.assertEqual(request.headers["x-simulated-trading"], "1")
            signed = base64.b64encode(
                hmac.new(
                    b"secret",
                    f"{request.headers['OK-ACCESS-TIMESTAMP']}{request.method}{request.url.raw_path.decode()}{request.content.decode()}".encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            self.assertEqual(request.headers["OK-ACCESS-SIGN"], signed)

        overwritten = client.attach_ai_builder_code({"instId": "SOL-USDT", "tag": "WRONG"})
        self.assertEqual(overwritten["tag"], "BUILDER1")

    async def test_live_flag_omits_simulated_header_and_empty_code_refuses(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok([{"sCode": "0", "ordId": "9"}])

        live = self._client(handler, flag="0", code="BUILDER1")
        await live.place_order(inst_id="SOL-USDT", side="buy", ord_type="market", sz="1")
        self.assertNotIn("x-simulated-trading", seen[-1].headers)

        blocked = OkxRestClient(
            api_key="key",
            secret_key="secret",
            passphrase="pass",
            flag="1",
            ai_builder_code="",
            transport=httpx.MockTransport(handler),
        )
        self.client = blocked
        before = len(seen)
        with self.assertRaises(OkxClientError):
            await blocked.place_order(inst_id="SOL-USDT", side="buy", ord_type="market", sz="1")
        self.assertEqual(len(seen), before)
        with self.assertRaises(OkxClientError):
            await blocked._request("POST", "/api/v5/trade/order", body={"instId": "SOL-USDT"}, trade=False)


class ConfirmFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_uses_python_bounds_and_brake_blocks_order(self) -> None:
        chrono = _candles()
        state = {"stress": False, "orders": 0}
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.content:
                captured.append({"path": path, "body": json.loads(request.content.decode())})
            if path == "/api/v5/market/candles":
                return _ok(list(reversed(chrono)))
            if path == "/api/v5/public/instruments":
                return _ok([{"instId": "SOL-USDT", "tickSz": "0.01", "lotSz": "0.01", "minSz": "0.01"}])
            if path == "/api/v5/market/books":
                return _ok([{"bids": [["150", "3", "0", "1"]], "asks": [["150.1", "1", "0", "1"]], "ts": "1"}])
            if path == "/api/v5/rubik/stat/contracts/open-interest-history":
                if state["stress"]:
                    data = [[str(1_700_000_000_000 + i * 300_000), str(1000 + i * 80), "1", "1"] for i in range(20)]
                else:
                    data = [[str(1_700_000_000_000 + i * 300_000), "1000", "1", "1"] for i in range(20)]
                return _ok(list(reversed(data)))
            if path == "/api/v5/rubik/stat/taker-volume":
                if state["stress"]:
                    data = [[str(1_700_000_000_000 + i * 300_000), "30", "1"] for i in range(20)]
                else:
                    data = [[str(1_700_000_000_000 + i * 300_000), "10", "10"] for i in range(20)]
                return _ok(list(reversed(data)))
            if path == "/api/v5/tradingBot/grid/order-algo":
                state["orders"] += 1
                body = json.loads(request.content.decode())
                return _ok([{"algoId": "algo-1", "sCode": "0", "sMsg": "", "tag": body["tag"]}])
            return httpx.Response(404, json={"code": "404", "msg": path, "data": []})

        client = OkxRestClient(
            api_key="key",
            secret_key="secret",
            passphrase="pass",
            flag="1",
            ai_builder_code="BUILDER1",
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(client.aclose)
        intent_json = json.dumps(
            {
                "symbol": "SOL-USDT",
                "budget": 2000,
                "risk_tolerance": "conservative",
                "direction": "neutral",
                "max_drawdown_limit": 0.15,
            }
        )
        agent = GroqAgent(completer=ScriptedCompleter([intent_json, "区间由本地引擎给出，适合稳健持有。"]))
        proposal = await compose_grid_proposal("我有 2000 U，打算持有 SOL", agent=agent, okx=client, bar="15m", plan_id="abcd1234")
        expected = align_bounds_to_tick(
            calculate_adaptive_bounds(
                _as_klines(chrono),
                {"risk_tolerance": "conservative", "max_drawdown_limit": 0.15},
            ),
            "0.01",
        )
        self.assertEqual(proposal["bounds"]["upper_str"], expected["upper_str"])
        self.assertEqual(proposal["bounds"]["lower_str"], expected["lower_str"])
        self.assertEqual(proposal["bounds"]["grid_count"], expected["grid_count"])
        self.assertFalse(proposal["brake"]["Emergency_Brake"])

        text, markup = render_confirm_card({**proposal, "environment_label": "模拟盘"})
        self.assertEqual(markup.inline_keyboard[0][0].text, LAUNCH_BUTTON_TEXT)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "pg:abcd1234")
        self.assertIn(expected["upper_str"], text)
        self.assertIn(expected["lower_str"], text)
        self.assertIn("本地量化引擎", text)

        state["stress"] = True
        algo = OkxStrategyAlgo(client)
        from core.quant.sentinel import BrakeActiveError

        with self.assertRaises(BrakeActiveError):
            await execute_confirmed_grid(proposal, okx=client, algo=algo)
        self.assertEqual(state["orders"], 0)

        state["stress"] = False
        result = await execute_confirmed_grid(proposal, okx=client, algo=algo)
        self.assertEqual(state["orders"], 1)
        order = next(item["body"] for item in captured if item["path"].endswith("order-algo"))
        self.assertEqual(order["tag"], "BUILDER1")
        self.assertEqual(order["instId"], "SOL-USDT")
        self.assertEqual(order["algoOrdType"], "grid")
        self.assertEqual(order["maxPx"], expected["upper_str"])
        self.assertEqual(order["minPx"], expected["lower_str"])
        self.assertEqual(order["gridNum"], str(expected["grid_count"]))
        self.assertEqual(order["quoteSz"], "2000")
        self.assertEqual(order["runType"], "2")
        self.assertLess(float(order["slTriggerPx"]), float(order["minPx"]))
        self.assertEqual(result["aiBuilderCode"], "BUILDER1")
        self.assertEqual(result["data"][0]["algoId"], "algo-1")

    async def test_short_direction_never_places(self) -> None:
        class Boom:
            async def fetch_market_bundle(self, *args, **kwargs):
                raise AssertionError("做空不应拉取行情后下单")

        proposal = {"intent": {"direction": "short", "symbol": "SOL-USDT"}, "bounds": {}, "bar": "15m"}
        with self.assertRaises(StrategyRejected):
            await execute_confirmed_grid(proposal, okx=Boom(), algo=None)  # type: ignore[arg-type]


class _BotMessage:
    def __init__(self, text: str = "", chat_id: int = 7) -> None:
        self.text = text
        self.chat_id = chat_id
        self.chat = self
        self.replies: list[tuple[str, dict]] = []

    async def send_action(self, action: object) -> None:
        return None

    async def reply_text(self, text: str, **kwargs: object) -> "_BotMessage":
        self.replies.append((text, kwargs))
        return self


class _StopQuery:
    def __init__(self, data: str, user_id: int, message: _BotMessage) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup: object = None) -> None:
        return None


class _PendingAlgo:
    def __init__(self, rows: list[dict], *, stop_error: Exception | None = None) -> None:
        self.rows = rows
        self.stop_error = stop_error
        self.stops: list[dict[str, str]] = []
        self.listed: list[str] = []

    async def list_pending(self, algo_ord_type: str = "grid") -> dict:
        self.listed.append(algo_ord_type)
        return {"code": "0", "data": self.rows}

    async def stop_spot_grid(self, *, algo_id: str, inst_id: str, stop_type: str = "1") -> dict:
        if self.stop_error is not None:
            raise self.stop_error
        self.stops.append({"algo_id": algo_id, "inst_id": inst_id, "stop_type": stop_type})
        return {"data": [{"algoId": algo_id, "sCode": "0"}]}


class _IntentProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def parse_user_intent(self, text: str) -> dict:
        self.calls.append(text)
        raise IntentParseError("测试到此为止")


def _menu_context(algo: object, agent: object | None = None) -> tuple[SimpleNamespace, SimpleNamespace]:
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            is_demo=True,
            groq_model="openai/gpt-oss-120b",
            default_kline_bar="15m",
            okx_ai_builder_code="BUILDER1",
        ),
        agent=agent,
        okx=None,
        algo=algo,
        plans={},
        watches={"SOL-USDT": {"chat_ids": {7}, "brake": None}},
        stop_drafts={},
        lock=asyncio.Lock(),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"runtime": runtime}))
    return runtime, context


def _sample_pending() -> list[dict]:
    return [
        {
            "algoId": "998877",
            "instId": "SOL-USDT",
            "instType": "SPOT",
            "algoOrdType": "grid",
            "state": "running",
            "maxPx": "180",
            "minPx": "120",
            "gridNum": "20",
        },
        {
            "algoId": "555",
            "instId": "ETH-USDT",
            "instType": "SPOT",
            "algoOrdType": "grid",
            "state": "stopping",
            "maxPx": "4000",
            "minPx": "3000",
            "gridNum": "10",
        },
        {
            "algoId": "777",
            "instId": "BTC-USDT-SWAP",
            "instType": "SWAP",
            "algoOrdType": "grid",
            "state": "running",
            "maxPx": "90000",
            "minPx": "80000",
        },
        {
            "algoId": "666",
            "instId": "ETH-USDT",
            "instType": "SPOT",
            "algoOrdType": "contract_grid",
            "state": "running",
            "maxPx": "9",
            "minPx": "1",
        },
    ]


class MenuShellTests(unittest.TestCase):
    def test_main_menu_keyboard_labels(self) -> None:
        markup = main_menu_keyboard()
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertEqual(labels, list(MENU_LABELS))
        self.assertIn(MENU_NEW, labels)
        self.assertIn(MENU_GRIDS, labels)
        self.assertIn(MENU_STOP, labels)
        self.assertIn(MENU_STATUS, labels)
        self.assertIn(MENU_HELP, labels)
        self.assertTrue(markup.is_persistent)
        self.assertTrue(markup.resize_keyboard)
        for label in MENU_LABELS:
            self.assertTrue(is_menu_text(f"  {label}  "))
        self.assertFalse(is_menu_text("我有 2000 U，打算持有 SOL，希望年化稳一点"))

    def test_slash_aliases_are_registered(self) -> None:
        application = Application.builder().token("123456:TEST").build()
        register_handlers(application)
        commands: set[str] = set()
        patterns: list[str] = []
        for group in application.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    commands.update(handler.commands)
                pattern = getattr(handler, "pattern", None)
                if pattern is not None:
                    patterns.append(pattern.pattern if hasattr(pattern, "pattern") else str(pattern))
        self.assertIn("menu", commands)
        self.assertIn("grids", commands)
        self.assertIn("positions", commands)
        self.assertIn("stop", commands)
        self.assertIn("status", commands)
        self.assertIn("help", commands)
        self.assertTrue(any(item.startswith("^pg:[0-9a-f]{8}$") or item == "^pg:[0-9a-f]{8}$" for item in patterns))
        self.assertTrue(any("pgstop" in item for item in patterns))


class MenuRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_text_is_not_sent_to_groq(self) -> None:
        agent = _IntentProbe()
        _runtime, context = _menu_context(_PendingAlgo([]), agent)
        replies: dict[str, str] = {}
        for label in MENU_LABELS:
            message = _BotMessage(label)
            update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=111))
            await on_text(update, context)  # type: ignore[arg-type]
            self.assertTrue(message.replies, label)
            replies[label] = message.replies[0][0]
            keyboard = message.replies[0][1].get("reply_markup")
            self.assertIsNotNone(keyboard, label)
            pressed = [button.text for row in keyboard.keyboard for button in row]
            self.assertEqual(pressed, list(MENU_LABELS))
        self.assertEqual(agent.calls, [])
        self.assertIn("2000 U", replies[MENU_NEW])
        self.assertIn("当前没有未停止的现货网格", replies[MENU_GRIDS])
        self.assertIn("当前没有未停止的现货网格", replies[MENU_STOP])
        self.assertIn("监控中的交易对：1", replies[MENU_STATUS])
        self.assertIn("铁律", replies[MENU_HELP])
        self.assertIn("确认卡", replies[MENU_HELP])

        prose = _BotMessage("我有 2000 U，打算持有 SOL")
        await on_text(
            SimpleNamespace(effective_message=prose, effective_user=SimpleNamespace(id=111)),
            context,  # type: ignore[arg-type]
        )
        self.assertEqual(agent.calls, ["我有 2000 U，打算持有 SOL"])
        self.assertIn("没能理解这条需求", prose.replies[0][0])


class GridListStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_pending_shows_spot_bounds_and_skips_other_products(self) -> None:
        algo = _PendingAlgo(_sample_pending())
        _runtime, context = _menu_context(algo)
        message = _BotMessage()
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=111))
        from bot.menu import cmd_grids

        await cmd_grids(update, context)  # type: ignore[arg-type]
        self.assertEqual(algo.listed, ["grid"])
        text = message.replies[0][0]
        self.assertIn("SOL-USDT", text)
        self.assertIn("998877", text)
        self.assertIn("120", text)
        self.assertIn("180", text)
        self.assertIn("运行中", text)
        self.assertIn("ETH-USDT", text)
        self.assertIn("停止中", text)
        self.assertNotIn("BTC-USDT-SWAP", text)
        self.assertNotIn("90000", text)
        self.assertNotIn("666", text)

    async def test_stop_requires_owner_and_second_confirm(self) -> None:
        from bot.menu import cmd_stop, on_stop_callback

        algo = _PendingAlgo(_sample_pending())
        runtime, context = _menu_context(algo)
        message = _BotMessage()
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=111))
        await cmd_stop(update, context)  # type: ignore[arg-type]
        markup = message.replies[0][1]["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["pgstop:998877"])
        self.assertNotIn("555", "".join(callbacks))

        stranger = _StopQuery("pgstop:998877", 222, message)
        await on_stop_callback(SimpleNamespace(callback_query=stranger), context)  # type: ignore[arg-type]
        self.assertEqual(stranger.answers, [("这不是你的停止确认", True)])
        self.assertFalse(runtime.stop_drafts["998877"]["armed"])

        early = _StopQuery("pgstopok:998877", 111, message)
        await on_stop_callback(SimpleNamespace(callback_query=early), context)  # type: ignore[arg-type]
        self.assertEqual(early.answers, [("请先点选要停止的网格", True)])
        self.assertEqual(algo.stops, [])

        owner = _StopQuery("pgstop:998877", 111, message)
        await on_stop_callback(SimpleNamespace(callback_query=owner), context)  # type: ignore[arg-type]
        self.assertTrue(runtime.stop_drafts["998877"]["armed"])
        confirm = message.replies[-1][0]
        self.assertIn("确认停止", confirm)
        self.assertIn("SOL-USDT", confirm)
        self.assertIn("998877", confirm)
        confirm_buttons = [
            button.callback_data
            for row in message.replies[-1][1]["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("pgstopok:998877", confirm_buttons)

        other_ok = _StopQuery("pgstopok:998877", 222, message)
        await on_stop_callback(SimpleNamespace(callback_query=other_ok), context)  # type: ignore[arg-type]
        self.assertEqual(other_ok.answers, [("这不是你的停止确认", True)])
        self.assertEqual(algo.stops, [])

        confirmed = _StopQuery("pgstopok:998877", 111, message)
        await on_stop_callback(SimpleNamespace(callback_query=confirmed), context)  # type: ignore[arg-type]
        self.assertEqual(
            algo.stops,
            [{"algo_id": "998877", "inst_id": "SOL-USDT", "stop_type": "1"}],
        )
        self.assertNotIn("998877", runtime.stop_drafts)
        self.assertIn("停止请求已提交", message.replies[-1][0])
        self.assertIn("AI Builder Code", message.replies[-1][0])

    async def test_missing_builder_code_is_explained_in_chinese(self) -> None:
        from bot.menu import cmd_stop, on_stop_callback

        algo = _PendingAlgo(
            _sample_pending(),
            stop_error=OkxClientError(
                "拒绝发送交易请求：未配置 OKX_AI_BUILDER_CODE。"
                "OKX 成交归因需要把 aiBuilderCode 写入请求字段 tag。"
            ),
        )
        runtime, context = _menu_context(algo)
        message = _BotMessage()
        await cmd_stop(
            SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=111)),
            context,  # type: ignore[arg-type]
        )
        await on_stop_callback(
            SimpleNamespace(callback_query=_StopQuery("pgstop:998877", 111, message)),
            context,  # type: ignore[arg-type]
        )
        await on_stop_callback(
            SimpleNamespace(callback_query=_StopQuery("pgstopok:998877", 111, message)),
            context,  # type: ignore[arg-type]
        )
        self.assertEqual(algo.stops, [])
        text = message.replies[-1][0]
        self.assertIn("OKX_AI_BUILDER_CODE", text)
        self.assertIn("tag", text)
        self.assertIn("未配置", text)
        self.assertFalse(runtime.stop_drafts["998877"]["submitting"])
        self.assertTrue(runtime.stop_drafts["998877"]["armed"])


class ArchitectureTests(unittest.TestCase):
    def test_quant_layer_does_not_import_an_llm(self) -> None:
        root = ROOT / "core" / "quant"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("groq", text, path.name)
            self.assertNotIn("openai", text, path.name)

    def test_llm_layer_does_not_place_orders(self) -> None:
        text = (ROOT / "core" / "llm" / "groq_client.py").read_text(encoding="utf-8")
        self.assertNotIn("core.okx", text)
        self.assertNotIn("place_order", text)
        self.assertNotIn("place_spot_grid", text)


if __name__ == "__main__":
    unittest.main()
