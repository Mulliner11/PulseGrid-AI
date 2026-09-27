"""Groq 适配层。

只做两件事：把用户的话收成结构化意图，以及用已经算好的指标写中文说明。
价格、ATR、网格上下界、格数、下单数量全部由 Python 计算，本模块不得推导这些数。
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Protocol

logger = logging.getLogger("pulsegrid.llm")

# 当前 Groq 账号请求 llama-3.3-70b-versatile 会 404。意图 JSON 实测可用的是下面这个模型。
# 它仍然只做 NLU 和策略说明，不计算价格或下单参数。
DEFAULT_MODEL = "openai/gpt-oss-120b"

# 说明文里允许出现、但不是行情算出来的结构常量。
# 14 是本项目写死的 ATR 周期，fallback 会写「14 周期 ATR」。不要往这里塞价格。
NARRATIVE_STRUCTURAL_INTS = frozenset({14})

INTENT_SYSTEM_PROMPT = """你是「脉冲智网」的意图解析器，只把用户的话整理成 JSON。
你不计算价格、网格上下界、格数、每格收益率或下单数量。这些一律由本地 Python 量化引擎计算。
只输出一个 JSON 对象，不要 Markdown，不要解释。

字段：
- symbol: 交易对，格式 BASE-USDT。用户说 SOL、sol/usdt 都写成 SOL-USDT。不要输出永续合约后缀。
- budget: 投入的计价货币数量（数字）。「2000 U」= 2000。不确定则为 null，禁止编造。
- risk_tolerance: conservative、balanced、aggressive 三者之一。
  稳、保守、抗跌、年化稳一点 => conservative
  平衡、适中 => balanced
  激进、进取 => aggressive
- direction: long、short、neutral 三者之一。只说持有、没说做多做空 => neutral。
- max_drawdown_limit: 用户能接受的最大跌幅，用 0 到 1 的小数。「抗 15% 暴跌」= 0.15。没提到则为 null，禁止编造。
"""

RATIONALE_SYSTEM_PROMPT = """你是「脉冲智网」的策略说明员。
用 2 到 3 句专业、干脆、有说服力的中文，向用户解释为什么可以采用这组网格。
铁律：
- 只能复述用户消息里 market_metrics 已经给出的数字，禁止心算，禁止改写出新的价格，禁止补充未提供的收益率、目标价或仓位。
- 不要给出新的下单参数，不要建议修改上下界或格数。
- 若 emergency_brake 为 true，必须明确说现在不要买入。
- 若某个字段的值是「未提供」，就不要提该数字。
- 不要输出 JSON、标题或项目符号。
"""


class GroqAgentError(RuntimeError):
    """模型调用或意图解析失败。消息可直接给用户。"""


class IntentParseError(GroqAgentError):
    """JSON 结构或字段不合法。"""


class _Completer(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float,
        response_format: dict[str, str] | None,
        max_tokens: int,
    ) -> str: ...


class _GroqSdkCompleter:
    """官方 groq SDK 的异步封装。测试可以换掉它，不必打到真实接口。"""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key.strip():
            raise GroqAgentError("缺少 GROQ_API_KEY")
        try:
            from groq import AsyncGroq
        except ImportError as exc:
            raise GroqAgentError("未安装 groq SDK，请先 pip install -r requirements.txt") from exc
        self._client = AsyncGroq(api_key=api_key, timeout=30.0)
        self._model = model

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float,
        response_format: dict[str, str] | None,
        max_tokens: int,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        completion = await self._client.chat.completions.create(**kwargs)
        content = completion.choices[0].message.content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, str):
                    chunks.append(part)
                elif isinstance(part, dict):
                    chunks.append(str(part.get("text", "")))
                else:
                    text = getattr(part, "text", "")
                    chunks.append(str(text))
            content = "".join(chunks)
        if not content or not str(content).strip():
            raise GroqAgentError("Groq 返回了空内容")
        return str(content)


def _strip_markdown_fence(text: str) -> str:
    """去掉 ```json 围栏，但不按花括号截断，避免嵌套对象被切坏。"""
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() if fenced else text


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型原文里取出一个 JSON 对象。容忍围栏和前后多余文字。

    用 JSONDecoder.raw_decode 从第一个「{」读完整值，嵌套对象不会停在第一个「}」。
    """
    raw = (text or "").strip()
    if not raw:
        raise IntentParseError("模型没有返回内容")
    candidate = _strip_markdown_fence(raw)
    decoder = json.JSONDecoder()
    index = 0
    last_error: json.JSONDecodeError | None = None
    while True:
        start = candidate.find("{", index)
        if start < 0:
            break
        try:
            parsed, _end = decoder.raw_decode(candidate, start)
        except json.JSONDecodeError as exc:
            # 这个「{」不是合法对象的起点，继续找下一个
            last_error = exc
            index = start + 1
            continue
        if isinstance(parsed, dict):
            return parsed
        index = start + 1
    if last_error is not None:
        raise IntentParseError(f"JSON 解析失败: {last_error}. 原文: {raw[:240]}") from last_error
    raise IntentParseError(f"模型输出不是 JSON: {raw[:240]}")


def _normalize_symbol(raw: object) -> str:
    if raw is None or not str(raw).strip():
        raise IntentParseError("没有识别到交易币种，请说明要持有的币，例如 SOL。")
    text = str(raw).strip().upper().replace("/", "-").replace("_", "-").replace(" ", "")
    for suffix in ("永续", "SWAP"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip("-")
    if text.endswith("-USDT-SWAP"):
        text = text[: -len("-SWAP")]
    if "-" not in text:
        text = f"{text}-USDT"
    if not re.fullmatch(r"[A-Z0-9]{1,20}-[A-Z0-9]{2,10}", text):
        raise IntentParseError(f"无法识别交易对: {raw}")
    return text


def _normalize_risk(raw: object) -> tuple[str, bool]:
    """返回 (风险偏好, 是否因缺失而默认成 balanced)。

    没填、null、空白按 balanced 处理。非空但对不上枚举的值仍然报错，避免把乱词悄悄收成平衡。
    """
    if raw is None:
        return "balanced", True
    original = str(raw).strip()
    if original == "" or original.lower() in {"null", "none", "未提供"}:
        return "balanced", True
    text = original.lower()
    if text in {"conservative", "low"} or original in {"稳健", "保守", "抗跌"}:
        return "conservative", False
    if text in {"balanced", "moderate"} or original in {"平衡", "适中"}:
        return "balanced", False
    if text in {"aggressive", "high"} or original in {"激进", "进取"}:
        return "aggressive", False
    if any(token in original for token in ("稳健", "保守", "抗跌")) or "conserv" in text:
        return "conservative", False
    if any(token in original for token in ("激进", "进取")) or "aggress" in text:
        return "aggressive", False
    if any(token in original for token in ("平衡", "适中")) or "balance" in text:
        return "balanced", False
    raise IntentParseError(f"无法识别风险偏好: {raw}")


def _normalize_direction(raw: object) -> tuple[str, bool]:
    if raw is None or str(raw).strip() == "":
        return "neutral", True
    text = str(raw).strip().lower()
    original = str(raw).strip()
    if text in {"neutral", "hold", "grid"} or original in {"中性", "持有", "网格"}:
        return "neutral", False
    if text == "long" or original in {"做多", "看涨"}:
        return "long", False
    if text == "short" or original in {"做空", "看跌"}:
        return "short", False
    if "空" in original:
        return "short", False
    if "多" in original or "涨" in original:
        return "long", False
    if "持有" in original or "中性" in original:
        return "neutral", False
    raise IntentParseError(f"无法识别方向: {raw}")


def _normalize_budget(raw: object) -> float:
    if raw is None or raw == "":
        raise IntentParseError("没有识别到投入金额。请说明资金，例如：我有 2000 U。")
    if isinstance(raw, str):
        cleaned = raw.strip().replace(",", "").replace(" ", "")
        cleaned = re.sub(r"(?i)(usdt|usd|u)$", "", cleaned)
        raw = cleaned
    try:
        budget = float(raw)
    except (TypeError, ValueError) as exc:
        raise IntentParseError(f"投入金额不是数字: {raw}") from exc
    if not math.isfinite(budget) or budget <= 0:
        raise IntentParseError(f"投入金额必须大于 0，当前是 {raw}")
    return budget


def _normalize_drawdown(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str) and raw.strip().lower() in {"null", "none", "未提供"}:
        return None
    text = str(raw).strip().replace("%", "")
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise IntentParseError(f"最大回撤不是数字: {raw}") from exc
    # 模型有时把 15% 写成 15。大于 1 时按百分数折算，这是单位整理，不是在算价。
    if value > 1:
        value = value / 100.0
    if not math.isfinite(value) or not 0 < value < 1:
        raise IntentParseError(f"最大回撤必须在 0 和 100% 之间，当前是 {raw}")
    return value


def normalize_intent(payload: dict[str, Any]) -> dict[str, Any]:
    """把模型 JSON 收成固定结构。多余字段直接丢掉。"""
    direction, direction_defaulted = _normalize_direction(payload.get("direction"))
    # 风险缺失时默认平衡，并用 risk_defaulted 标出来，调用方能区分「用户说了平衡」和「模型没填」。
    risk, risk_defaulted = _normalize_risk(payload.get("risk_tolerance"))
    intent = {
        "symbol": _normalize_symbol(payload.get("symbol")),
        "budget": _normalize_budget(payload.get("budget")),
        "risk_tolerance": risk,
        "direction": direction,
        "max_drawdown_limit": _normalize_drawdown(payload.get("max_drawdown_limit")),
        "direction_defaulted": direction_defaulted,
        "risk_defaulted": risk_defaulted,
    }
    return intent


def _allowed_numbers(metrics: dict[str, Any]) -> set[float]:
    """说明文字里允许出现的数：指标原值、常见舍入、比例的百分数，以及结构常量。"""
    allowed: set[float] = set()

    def _add(value: float) -> None:
        if not math.isfinite(value):
            return
        allowed.add(value)
        allowed.add(abs(value))
        for digits in range(0, 9):
            allowed.add(round(value, digits))
            allowed.add(round(abs(value), digits))
        if abs(value) <= 2:
            percent = value * 100.0
            for digits in range(0, 5):
                allowed.add(round(percent, digits))

    def _walk(node: Any) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            _add(float(node))
            return
        if isinstance(node, str):
            for match in re.findall(r"\d+(?:\.\d+)?", node):
                _add(float(match))
            return
        if isinstance(node, dict):
            for key, item in node.items():
                # atr_14 这类键名里的周期不是价格，说明里可以照着说
                for match in re.findall(r"\d+", str(key)):
                    _add(float(match))
                _walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    for period in NARRATIVE_STRUCTURAL_INTS:
        _add(float(period))
    _walk(metrics)
    return allowed


def narrative_respects_metrics(text: str, metrics: dict[str, Any]) -> bool:
    """模型如果写出指标里没有的数字，就判定为臆造。"""
    cleaned = text or ""
    symbol = str(metrics.get("symbol") or "")
    if symbol:
        cleaned = cleaned.replace(symbol, " ")
        base = symbol.split("-")[0]
        if base:
            cleaned = re.sub(rf"\b{re.escape(base)}\b", " ", cleaned, flags=re.IGNORECASE)
    allowed = _allowed_numbers(metrics)
    for match in re.findall(r"\d+(?:\.\d+)?", cleaned):
        value = float(match)
        if not any(math.isclose(value, item, rel_tol=1e-6, abs_tol=1e-6) for item in allowed):
            return False
    return True


def _metrics_for_prompt(metrics: dict[str, Any]) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in metrics.items():
        if value is None:
            prepared[key] = "未提供"
        elif isinstance(value, float):
            prepared[key] = round(value, 8)
        else:
            prepared[key] = value
    return prepared


def fallback_rationale(symbol: str, metrics: dict[str, Any]) -> str:
    """模型说明不可用时的本地文案。数字只从 metrics 里取。"""
    price = metrics.get("last_price")
    upper = metrics.get("grid_upper")
    lower = metrics.get("grid_lower")
    atr_value = metrics.get("atr_14")
    grids = metrics.get("grid_count")
    profit = metrics.get("per_grid_profit_rate")
    if all(isinstance(item, (int, float)) for item in (price, upper, lower, atr_value, grids, profit)):
        # 周期数字必须来自 NARRATIVE_STRUCTURAL_INTS，否则数字守卫会把本地模板自己判成臆造
        atr_period = min(NARRATIVE_STRUCTURAL_INTS)
        first = (
            f"{symbol} 现价 {float(price):.4f}，{atr_period} 周期 ATR {float(atr_value):.4f}，"
            f"网格放在 {float(lower):.4f} 到 {float(upper):.4f}，共 {int(grids)} 格，"
            f"等比单格收益约 {float(profit) * 100:.2f}%。"
        )
    else:
        first = f"{symbol} 的网格参数来自本地 ATR 引擎，确认卡上的区间就是将要提交的参数。"

    if metrics.get("emergency_brake"):
        oi_change = metrics.get("oi_change_pct")
        oi_text = f"{float(oi_change) * 100:.2f}%" if isinstance(oi_change, (int, float)) else "明显上行"
        second = f"永续持仓量变化 {oi_text}，同时 CVD 向下背离，急刹车已挡住买入。"
    else:
        risk = str(metrics.get("risk_tolerance") or "当前")
        drawdown = metrics.get("max_drawdown_limit")
        if isinstance(drawdown, (int, float)):
            second = f"按 {risk} 偏好，下沿覆盖约 {float(drawdown) * 100:.0f}% 的下跌空间，用来在震荡里高抛低吸。"
        else:
            second = f"按 {risk} 偏好，区间跟随近期波动张开，用来在震荡里高抛低吸。"
    third = "确认后才提交订单，模型不负责填写价格。"
    return first + second + third


def _is_transient(exc: BaseException) -> bool:
    return exc.__class__.__name__ in {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
    }


class GroqAgent:
    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        completer: _Completer | None = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self._completer = completer or _GroqSdkCompleter(api_key, self.model)

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        response_format: dict[str, str] | None,
        max_tokens: int,
    ) -> str:
        import asyncio

        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                return await self._completer.complete(
                    messages=messages,
                    temperature=temperature,
                    response_format=response_format,
                    max_tokens=max_tokens,
                )
            except GroqAgentError:
                raise
            except Exception as exc:
                last_error = exc
                # 超时和限流重试一次。解析错误不在这里吞掉。
                if attempt == 0 and _is_transient(exc):
                    logger.warning("Groq 暂时失败，准备重试: %s", exc)
                    await asyncio.sleep(0.6)
                    continue
                raise GroqAgentError(f"Groq 调用失败: {exc}") from exc
        raise GroqAgentError(f"Groq 调用失败: {last_error}")

    async def parse_user_intent(self, user_text: str) -> dict[str, Any]:
        """自然语言 → 固定 JSON。不做任何行情计算。"""
        text = (user_text or "").strip()
        if not text:
            raise IntentParseError("用户输入为空")
        if len(text) > 2000:
            raise IntentParseError("输入过长，请把需求压缩在 2000 字以内")
        raw = await self._complete(
            [
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=400,
        )
        try:
            payload = extract_json_object(raw)
            intent = normalize_intent(payload)
        except IntentParseError:
            raise
        except Exception as exc:
            raise IntentParseError(f"意图整理失败: {exc}") from exc
        logger.info(
            "意图解析完成 symbol=%s risk=%s direction=%s budget=%s",
            intent["symbol"],
            intent["risk_tolerance"],
            intent["direction"],
            intent["budget"],
        )
        return intent

    async def generate_strategy_rationale(self, symbol: str, market_metrics: dict[str, Any]) -> str:
        """根据 Python 已经算好的指标写 2 到 3 句中文。写出自造数字时改用本地模板。"""
        if not symbol or not str(symbol).strip():
            raise GroqAgentError("缺少 symbol，无法生成说明")
        if not isinstance(market_metrics, dict) or not market_metrics:
            raise GroqAgentError("market_metrics 为空，拒绝让模型自由发挥")
        metrics = dict(market_metrics)
        metrics.setdefault("symbol", symbol)
        prompt_payload = _metrics_for_prompt(metrics)
        try:
            raw = await self._complete(
                [
                    {"role": "system", "content": RATIONALE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"symbol": symbol, "market_metrics": prompt_payload},
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0.2,
                response_format=None,
                max_tokens=300,
            )
        except GroqAgentError as exc:
            logger.warning("策略说明生成失败，改用本地模板: %s", exc)
            return fallback_rationale(symbol, metrics)

        text = raw.strip().strip('"')
        # 模型若夹带了指标里没有的数字，丢弃原文，避免把幻觉价格展示给用户
        if len(text) < 8 or not narrative_respects_metrics(text, prompt_payload):
            logger.warning("策略说明含有未提供的数字或过短，已改用本地模板")
            return fallback_rationale(symbol, metrics)
        return text
