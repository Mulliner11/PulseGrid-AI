<p align="center">
  <img src="assets/pulsegrid-logo-icon.png" alt="PulseGrid AI" width="128" />
</p>

# PulseGrid AI（脉冲智网）

OKX 上的自适应防破网网格交易引擎。Telegram 是操作界面，Python 负责行情、网格和执行，Groq 只负责听懂人话和写中文说明。

PulseGrid AI is an adaptive anti-break grid engine for OKX. The bot never asks the model for a price.

## 它做什么

你用一句话描述资金和持有计划，例如：

> 我有 2000 U，打算持有 SOL，希望年化稳一点、能抗 15% 的暴跌。

机器人会：

1. 用 Groq 把这句话收成结构化意图（交易对、预算、风险偏好、方向、回撤约束）。
2. 用 OKX 行情在本地计算 ATR、VWAP、支撑压力、订单簿失衡，以及 OI + CVD 急刹车。
3. 生成自适应现货网格的上下沿、格数、单格收益和止损。
4. 发一张 HTML 确认卡。你点「⚡ 一键在 OKX 开启网格」之后才下单。
5. 用同一套菜单查看未停止的现货网格，或二次确认后停止。

Phase-1 的一键开启只提交**现货网格**。意图里的做空不会自动开仓。

## 架构和铁律

```text
Telegram 菜单 / 自然语言
        │
        ▼
Groq ── 只输出意图 JSON，以及确认卡上的中文说明
        │
        ▼
Python 量化层 ── K 线、ATR、VWAP、CVD、OI、网格上下界、格数、止损
        │
        ▼
OKX REST ── 行情只读；交易请求强制写入 tag = AI Builder Code
```

铁律：

- **LLM 不算价格。** Groq 不计算现价、上下沿、格数、每格收益率或下单数量，也不许在说明里编造确认卡上没有的数字。
- **Python 拥有行情和下单参数。** 自适应网格、急刹车、再查一次哨兵，都在 `pulsegrid/core` 里完成。
- **先确认再下单。** 确认卡大约 15 分钟有效，而且只有生成它的用户能点。急刹车或 OI/CVD 不齐时，没有下单按钮。
- **每条交易路径都带 Builder Code。** 下单、改网格、停止网格都会把 `OKX_AI_BUILDER_CODE` 写入 OKX 字段 `tag`。没配置就拒绝发送，不会发出空 tag。
- **默认模拟盘。** `OKX_FLAG` 默认为 `1`。

默认模型是 `openai/gpt-oss-120b`。`llama-3.3-70b-versatile` 在当前用法下会 404，不要把它当默认模型。换模型也仍然只做意图和说明。

## Telegram 怎么用

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，拿到令牌。
2. 在 OKX 创建 API 密钥。先用**模拟盘**密钥。交易权限按你要跑的网格来开，不要把密钥提交到仓库。
3. 在 [OKX AI Builder](https://www.okx.com/zh-hans/agent-tradekit/builder) 申请 AI Builder Code，并登记本仓库地址（见下一节）。
4. 安装并启动：

```bash
cd pulsegrid
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填写下面的变量，不要提交 .env
python main.py --check # 本地单测，不访问外网
python main.py
```

5. 在 Telegram 打开你的机器人，发送 `/start`。底部会出现常驻主菜单。

| 按钮 | 命令 | 作用 |
| --- | --- | --- |
| 📝 新建策略 | 直接发一句话即可 | 提示你输入自然语言。按钮本身不会送给 Groq |
| 📊 我的网格 | `/grids` 或 `/positions` | 列出 OKX 上未停止的现货网格：交易对、algoId、上下界、状态 |
| 🛑 停止网格 | `/stop` | 点选一个网格，再点「确认停止」才会调用停止 |
| 📡 状态 | `/status` | 模拟盘/实盘、模型、K 线周期、监控中的交易对数量 |
| ❓ 帮助 | `/help` | 示例说法、铁律、确认卡各字段是什么意思 |
| 主菜单 | `/menu` | 重新展开菜单说明和键盘 |

示例说法可以直接发给机器人（不必先点菜单）：

```text
我有 2000 U，打算持有 SOL，希望年化稳一点、能抗 15% 的暴跌。
```

确认卡上的现价、上沿、下沿、格数、止损来自本地量化引擎。说明段落才是模型写的。点一键开启后，机器人会再拉一次 OI/CVD；急刹车或数据不足就拒绝下单。停止网格同样要二次确认，并且只有打开这次停止列表的用户能点确认。停止时会卖出网格里的基础货币。

菜单上的五个中文按钮是导航，不会被当成策略原文送给模型。

## 环境变量

变量定义在 `pulsegrid/.env.example`。进程从 `pulsegrid/.env` 或真实环境变量读取，仓库里不要放密钥。

| 变量 | 作用 |
| --- | --- |
| `GROQ_API_KEY` | Groq 密钥。只用于意图和中文说明 |
| `GROQ_MODEL` | 默认 `openai/gpt-oss-120b` |
| `TELEGRAM_BOT_TOKEN` | BotFather 发放的机器人令牌 |
| `OKX_API_KEY` | OKX API Key |
| `OKX_SECRET_KEY` | OKX Secret |
| `OKX_PASSPHRASE` | 创建 API 时设置的口令 |
| `OKX_FLAG` | `1` 模拟盘（默认），`0` 实盘 |
| `OKX_AI_BUILDER_CODE` | AI Builder Code。启动可留空；确认下单和停止网格前必须填上，并写入 `tag` |
| `OKX_BASE_URL` | 默认 `https://www.okx.com` |
| `DEFAULT_KLINE_BAR` | 默认 K 线周期，`15m` |
| `MONITOR_INTERVAL_SEC` | 订单簿哨兵轮询间隔，默认 8 秒 |
| `LOG_LEVEL` | 日志级别，默认 `INFO` |

启动时若 Groq、Telegram 或 OKX 三件套为空，`python main.py` 会退出并指出缺哪一项。`OKX_AI_BUILDER_CODE` 可以先留空：Builder 申请还在审核时机器人仍能启动。`/status` 和 `/help` 会写明「未配置」。确认下单和停止网格在填上码并重启之前会失败，而且不会发出交易请求。

## AI Builder Code 和 GitHub 地址

OKX AI Builder 计划用项目的公开仓库标明这套代理是谁的实现。申请或登记 Builder 时填写：

`https://github.com/Mulliner11/PulseGrid-AI`

这个地址是项目身份，用来让 OKX 把代理和这份代码对应起来。它**不会**被写进订单。

成交归因用的是另一份凭证：AI Builder Code。把它放到 `OKX_AI_BUILDER_CODE`。本仓库的 OKX 客户端在每条会改变订单或网格的请求里强制写入字段 `tag`（OpenAPI 不接受名为 `aiBuilderCode` 的字段）。调用方如果自己塞了别的 `tag`，也会被覆盖成这份码。

码还没下来时可以先启动。未配置时，确认卡上的一键下单和「停止网格」都会用中文说明失败原因，请求不会发到 OKX。只读的「我的网格」不写 `tag`。拿到码后写入 `OKX_AI_BUILDER_CODE` 并重启。

集成说明：<https://www.okx.com/zh-hans/help/ai-builder-program-integration-guide>

## 模拟盘和实盘

- `OKX_FLAG=1`（默认）：私有请求带 `x-simulated-trading: 1`，打到模拟盘。请使用模拟盘 API 密钥。
- `OKX_FLAG=0`：实盘，确认后的网格使用真实资金。启动时会打警告日志。请换实盘 API 密钥，不要拿模拟盘密钥打实盘。

确认卡和状态里都会写明当前是模拟盘还是实盘。

## 项目结构

```text
.
├── README.md
├── assets/                      # 标志
└── pulsegrid/
    ├── main.py                  # 启动机器人；--check 跑测试
    ├── .env.example
    ├── requirements.txt
    ├── bot/
    │   ├── handlers.py          # 意图 → 确认卡 → 下单
    │   ├── menu.py              # 主菜单、我的网格、停止网格
    │   └── ui_cards.py          # HTML 确认卡
    ├── config/settings.py
    ├── core/
    │   ├── llm/groq_client.py   # 意图 + 中文说明
    │   ├── okx/client.py        # REST；交易路径强制 tag
    │   ├── okx/strategy_algo.py # 现货网格下单 / 停止 / 查询 / 改界
    │   └── quant/               # ATR、VWAP、CVD、哨兵、网格
    └── tests/test_phase1.py
```

量化层不引用 Groq。LLM 层不引用 OKX，也不拼下单参数。

## 自检

在 `pulsegrid` 目录：

```bash
python main.py --check
```

这会跑 `pulsegrid/tests` 里的单元测试：网格数学、急刹车、意图 JSON、确认卡、Builder Code 的 `tag`、菜单路由，以及用假的 OKX 客户端走通「确认后再下单」和「确认后再停止」。测试不访问 Groq 或 OKX。

## 当前范围

Phase-1 已经有的：

- 自然语言意图、OKX 行情包、自适应网格和 OI/CVD 急刹车
- 确认卡，以及确认后才提交的现货网格
- 常驻中文菜单：新建策略、我的网格、停止网格、状态、帮助
- 每条交易路径写入 AI Builder Code
- 同一事件循环里的订单簿哨兵通知

还没有的：

- 没有 K 线、收益曲线或其他图表界面。网格信息是文本和确认卡。
- 一键开启只做现货网格，不做合约网格。
- 用户说到做空时，意图会记下来，但不会自动开空。
- `rebalance_grid_bounds` 已在 OKX 封装里，Telegram 还没有改界入口。
- 机器人使用一份 OKX API，没有按 Telegram 用户拆成多个交易账户。停止确认只保证「谁打开的列表，谁才能点确认」。
