"""OKX REST v5 异步客户端。

交易与再平衡请求一律把配置里的 aiBuilderCode 写入 OKX 字段 tag。
OpenAPI 不接受名为 aiBuilderCode 的字段；归因值仍然是这一个专用码，只是字段名必须是 tag。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("pulsegrid.okx")

# 这些路径会改变订单或网格，禁止在未写入 Builder Code 时调用
TRADE_PATHS = frozenset(
    {
        "/api/v5/trade/order",
        "/api/v5/trade/amend-order",
        "/api/v5/tradingBot/grid/order-algo",
        "/api/v5/tradingBot/grid/amend-order-algo",
        "/api/v5/tradingBot/grid/amend-algo-basic-param",
        "/api/v5/tradingBot/grid/stop-order-algo",
    }
)


class OkxClientError(RuntimeError):
    """网络、签名或 OKX 业务错误。"""


def _timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _spot_to_swap(inst_id: str) -> str:
    if inst_id.endswith("-SWAP"):
        return inst_id
    return f"{inst_id}-SWAP"


class OkxRestClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        flag: str = "1",
        ai_builder_code: str = "",
        base_url: str = "https://www.okx.com",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        if str(flag) not in {"0", "1"}:
            raise OkxClientError("OKX flag 只能是 0（实盘）或 1（模拟盘）")
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.passphrase = passphrase.strip()
        self.flag = str(flag)
        # 语义上的 aiBuilderCode。发到 OKX 时字段名是 tag。
        self.ai_builder_code = ai_builder_code.strip()
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            kwargs: dict[str, Any] = {
                "base_url": self.base_url,
                "timeout": httpx.Timeout(self._timeout, connect=5.0),
                "headers": {"User-Agent": "PulseGridAI/0.1"},
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.AsyncClient(**kwargs)
        return self._http

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}"
        digest = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def attach_ai_builder_code(self, body: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any] | list[dict[str, Any]]:
        """强制写入 AI Builder Code。

        调用方传入的 tag 会被覆盖，避免漏记返佣。
        未配置码时直接拒绝，不会发出交易请求。
        """
        code = self.ai_builder_code
        if not code:
            raise OkxClientError(
                "拒绝发送交易请求：未配置 OKX_AI_BUILDER_CODE。"
                "OKX 成交归因需要把 aiBuilderCode 写入请求字段 tag。"
            )

        def _one(item: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(item, dict):
                raise OkxClientError("交易请求体必须是 JSON 对象")
            merged = dict(item)
            merged["tag"] = code
            return merged

        if isinstance(body, list):
            if not body:
                raise OkxClientError("交易请求列表为空")
            return [_one(item) for item in body]
        if isinstance(body, dict):
            return _one(body)
        raise OkxClientError("交易请求体类型不支持")

    def _auth_headers(self, timestamp: str, method: str, request_path: str, body_text: str) -> dict[str, str]:
        if not self.api_key or not self.secret_key or not self.passphrase:
            raise OkxClientError("OKX API Key / Secret / Passphrase 不完整，无法签名")
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body_text),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }
        # 模拟盘私有接口必须带这个头，否则会打到实盘账户
        if self.flag == "1":
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        auth: bool = False,
        trade: bool = False,
    ) -> dict[str, Any]:
        if path in TRADE_PATHS and not trade:
            raise OkxClientError(f"{path} 是交易接口，必须附带 aiBuilderCode 后才能调用")
        method = method.upper()
        if trade:
            if body is None:
                body = {}
            body = self.attach_ai_builder_code(body)  # type: ignore[arg-type]
            auth = True

        query = ""
        if params:
            pairs = [(key, value) for key, value in params.items() if value is not None]
            encoded = urlencode(pairs)
            if encoded:
                query = f"?{encoded}"
        request_path = f"{path}{query}"
        body_text = ""
        if body is not None:
            body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers.update(self._auth_headers(_timestamp(), method, request_path, body_text))

        if trade:
            logger.info("OKX 交易请求 %s %s，已写入 aiBuilderCode → tag", method, path)

        try:
            response = await self._http_client().request(
                method,
                request_path,
                content=body_text.encode() if body is not None else None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise OkxClientError(f"OKX 网络错误 {method} {path}: {exc}") from exc

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise OkxClientError(
                f"OKX 返回非 JSON {path}: HTTP {response.status_code} {response.text[:300]}"
            ) from exc
        if not isinstance(payload, dict):
            raise OkxClientError(f"OKX 响应结构异常 {path}: {payload!r}"[:500])
        if response.status_code >= 400 or str(payload.get("code", "0")) != "0":
            raise OkxClientError(
                f"OKX {path} 失败: HTTP {response.status_code} code={payload.get('code')} "
                f"msg={payload.get('msg')} data={payload.get('data')}"
            )
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and str(item.get("sCode", "0")) not in {"0", ""}:
                    raise OkxClientError(
                        f"OKX {path} 业务拒绝: sCode={item.get('sCode')} sMsg={item.get('sMsg')}"
                    )
        return payload

    def _trade_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "aiBuilderCode": self.ai_builder_code,
            "okx_attribution_field": "tag",
            "data": payload.get("data"),
            "raw": payload,
        }

    async def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 120) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit)},
        )
        rows = payload.get("data") or []
        candles: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise OkxClientError(f"K 线格式异常: {row!r}")
            candles.append(
                {
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "volume_quote": float(row[7]) if len(row) > 7 and row[7] not in ("", None) else 0.0,
                    "confirm": str(row[8]) if len(row) > 8 else "1",
                }
            )
        candles.sort(key=lambda item: item["ts"])
        # 丢掉未收盘的最后一根，避免 ATR 被半根 K 线带着跑
        if candles and candles[-1]["confirm"] == "0" and len(candles) > 20:
            candles = candles[:-1]
        if not candles:
            raise OkxClientError(f"{inst_id} 没有可用 K 线")
        return candles

    async def get_orderbook(self, inst_id: str, depth: int = 20) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/api/v5/market/books",
            params={"instId": inst_id, "sz": str(depth)},
        )
        data = payload.get("data") or []
        if not data:
            raise OkxClientError(f"{inst_id} 订单簿为空")
        book = data[0]
        return {
            "bids": book.get("bids") or [],
            "asks": book.get("asks") or [],
            "ts": book.get("ts"),
        }

    async def get_instrument(self, inst_id: str, inst_type: str = "SPOT") -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/api/v5/public/instruments",
            params={"instType": inst_type, "instId": inst_id},
        )
        data = payload.get("data") or []
        if not data:
            raise OkxClientError(f"找不到交易对 {inst_id}")
        item = data[0]
        if not item.get("tickSz"):
            raise OkxClientError(f"{inst_id} 缺少 tickSz")
        return item

    async def get_open_interest_history(self, inst_id: str, period: str = "5m") -> list[dict[str, float]]:
        """永续持仓量历史。inst_id 应为 SWAP，例如 SOL-USDT-SWAP。"""
        payload = await self._request(
            "GET",
            "/api/v5/rubik/stat/contracts/open-interest-history",
            params={"instId": inst_id, "period": period},
        )
        series: list[dict[str, float]] = []
        for row in payload.get("data") or []:
            if not isinstance(row, list) or len(row) < 2:
                raise OkxClientError(f"OI 格式异常: {row!r}")
            series.append({"ts": float(row[0]), "oi": float(row[1])})
        series.sort(key=lambda item: item["ts"])
        return series

    async def get_taker_volume(self, ccy: str, inst_type: str = "CONTRACT", period: str = "5m") -> list[dict[str, float]]:
        """主动买卖量。OKX 数组顺序是 [ts, sellVol, buyVol]，卖在买前面。"""
        payload = await self._request(
            "GET",
            "/api/v5/rubik/stat/taker-volume",
            params={"ccy": ccy, "instType": inst_type, "period": period},
        )
        series: list[dict[str, float]] = []
        for row in payload.get("data") or []:
            if not isinstance(row, list) or len(row) < 3:
                raise OkxClientError(f"主动买卖量格式异常: {row!r}")
            series.append({"ts": float(row[0]), "sell": float(row[1]), "buy": float(row[2])})
        series.sort(key=lambda item: item["ts"])
        return series

    async def fetch_market_bundle(self, inst_id: str, bar: str = "15m") -> dict[str, Any]:
        """并行拉取 K 线、永续 OI、合约主动买卖量和现货订单簿。K 线失败则整体失败。"""
        import asyncio

        inst_id = inst_id.upper()
        swap_id = _spot_to_swap(inst_id)
        ccy = inst_id.split("-")[0]
        candles, oi, taker, book = await asyncio.gather(
            self.get_candles(inst_id, bar=bar, limit=120),
            self.get_open_interest_history(swap_id, period="5m"),
            self.get_taker_volume(ccy, inst_type="CONTRACT", period="5m"),
            self.get_orderbook(inst_id, depth=20),
            return_exceptions=True,
        )
        if isinstance(candles, Exception):
            raise OkxClientError(f"获取 {inst_id} K 线失败: {candles}") from candles

        def _optional(name: str, value: Any) -> tuple[Any, str | None]:
            if isinstance(value, Exception):
                logger.warning("%s 获取失败，哨兵将视为数据不足: %s", name, value)
                return ([] if name != "订单簿" else None), str(value)
            return value, None

        oi_rows, oi_error = _optional("OI", oi)
        taker_rows, taker_error = _optional("CVD", taker)
        book_data, book_error = _optional("订单簿", book)
        return {
            "inst_id": inst_id,
            "swap_id": swap_id,
            "candles": candles,
            "oi": oi_rows,
            "oi_error": oi_error,
            "taker": taker_rows,
            "taker_error": taker_error,
            "book": book_data,
            "book_error": book_error,
        }

    async def place_order(
        self,
        *,
        inst_id: str,
        side: str,
        ord_type: str,
        sz: str,
        px: str | None = None,
        td_mode: str = "cash",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if px is not None:
            body["px"] = px
        payload = await self._request("POST", "/api/v5/trade/order", body=body, trade=True)
        return self._trade_result(payload)

    async def place_grid_algo(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request("POST", "/api/v5/tradingBot/grid/order-algo", body=body, trade=True)
        return self._trade_result(payload)

    async def rebalance_grid(self, body: dict[str, Any]) -> dict[str, Any]:
        """修改网格上下界。文档未单列 tag，仍按归因要求写入。若 OKX 返回 51000，错误会原样抛出。"""
        payload = await self._request(
            "POST",
            "/api/v5/tradingBot/grid/amend-algo-basic-param",
            body=body,
            trade=True,
        )
        return self._trade_result(payload)

    async def amend_grid_algo(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/api/v5/tradingBot/grid/amend-order-algo",
            body=body,
            trade=True,
        )
        return self._trade_result(payload)

    async def stop_grid_algo(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/api/v5/tradingBot/grid/stop-order-algo",
            body=orders,
            trade=True,
        )
        return self._trade_result(payload)

    async def get_pending_grid_orders(self, algo_ord_type: str = "grid") -> dict[str, Any]:
        """查询未停止的网格。只读，不写 tag。"""
        return await self._request(
            "GET",
            "/api/v5/tradingBot/grid/orders-algo-pending",
            params={"algoOrdType": algo_ord_type},
            auth=True,
        )
