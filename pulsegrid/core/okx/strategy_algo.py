"""OKX 网格策略委托的薄封装。价格参数由量化引擎传入，这里只组请求。"""

from __future__ import annotations

from typing import Any

from core.okx.client import OkxRestClient


class OkxStrategyAlgo:
    def __init__(self, client: OkxRestClient) -> None:
        self.client = client

    async def place_spot_grid(
        self,
        *,
        inst_id: str,
        max_px: str,
        min_px: str,
        grid_num: int,
        quote_sz: str,
        run_type: str = "2",
        sl_trigger_px: str | None = None,
        algo_cl_ord_id: str | None = None,
    ) -> dict[str, Any]:
        """现货网格。run_type 2 为等比，与本地单格收益率口径一致。tag 由客户端强制写入。"""
        body: dict[str, Any] = {
            "instId": inst_id,
            "algoOrdType": "grid",
            "maxPx": max_px,
            "minPx": min_px,
            "gridNum": str(int(grid_num)),
            "runType": str(run_type),
            "quoteSz": quote_sz,
        }
        if sl_trigger_px:
            body["slTriggerPx"] = sl_trigger_px
        if algo_cl_ord_id:
            body["algoClOrdId"] = algo_cl_ord_id
        return await self.client.place_grid_algo(body)

    async def rebalance_grid_bounds(
        self,
        *,
        algo_id: str,
        max_px: str,
        min_px: str,
        grid_num: int,
    ) -> dict[str, Any]:
        body = {
            "algoId": algo_id,
            "maxPx": max_px,
            "minPx": min_px,
            "gridNum": str(int(grid_num)),
        }
        return await self.client.rebalance_grid(body)

    async def stop_spot_grid(self, *, algo_id: str, inst_id: str, stop_type: str = "1") -> dict[str, Any]:
        # stopType 1：停止时卖出基础货币。需要留币时由调用方传 2。
        orders = [
            {
                "algoId": algo_id,
                "instId": inst_id,
                "algoOrdType": "grid",
                "stopType": stop_type,
            }
        ]
        return await self.client.stop_grid_algo(orders)

    async def list_pending(self, algo_ord_type: str = "grid") -> dict[str, Any]:
        return await self.client.get_pending_grid_orders(algo_ord_type)
