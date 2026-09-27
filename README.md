<p align="center">
  <img src="assets/pulsegrid-logo-icon.png" alt="PulseGrid AI" width="128" />
</p>

# PulseGrid AI (pulse-grid trading engine)

An adaptive anti-break grid trading engine for OKX. Telegram is the control surface. Python owns market data, the grid, and execution. Groq only parses natural language and writes the Chinese explanation on the confirmation card. The bot never asks the model for a price.

## What it does

Describe your capital and hold plan in one sentence, for example:

> I have 2000 USDT. I plan to hold SOL, prefer a conservative profile, and want the grid to withstand about a 15% drawdown.

The bot will:

1. Use Groq to turn that sentence into a structured intent (pair, budget, risk preference, direction, drawdown constraint).
2. Use OKX market data to compute ATR, VWAP, support and resistance, order-book imbalance, and an OI + CVD emergency brake locally.
3. Produce the adaptive spot grid's upper and lower bounds, grid count, per-grid profit, and stop-loss.
4. Send an HTML confirmation card. An order is placed only after you tap the one-tap launch button on that card.
5. Use the same menu to list spot grids that have not been stopped, or stop one after a second confirmation.

Phase-1 one-tap launch submits a **spot grid** only. A short recorded in the intent does not open a short automatically.

## Architecture and iron rules

```text
Telegram menu / natural language
        │
        ▼
Groq ── emits intent JSON only, plus the Chinese explanation on the confirmation card
        │
        ▼
Python quant layer ── candles, ATR, VWAP, CVD, OI, grid bounds, grid count, stop-loss
        │
        ▼
OKX REST ── market data is read-only; trading requests must write tag = AI Builder Code
```

Iron rules:

- **The LLM does not price.** Groq does not compute last price, upper or lower bounds, grid count, per-grid yield, or order size, and it must not invent numbers that are absent from the confirmation card.
- **Python owns market data and order parameters.** The adaptive grid, the emergency brake, and the second sentinel check all run in `pulsegrid/core`.
- **Confirm before any order.** A confirmation card stays valid for about 15 minutes, and only the user who generated it can tap it. There is no order button when the emergency brake is on or when OI/CVD data is incomplete.
- **Every trading path carries the Builder Code.** Place, amend-grid, and stop-grid requests write `OKX_AI_BUILDER_CODE` into the OKX field `tag`. If it is unset, the client refuses to send the request and never sends an empty tag.
- **Demo trading by default.** `OKX_FLAG` defaults to `1`.

The default model is `openai/gpt-oss-120b`. `llama-3.3-70b-versatile` returns 404 under the current usage, so do not use it as the default. Switching models still limits the model to intent and the written explanation.

## How to use Telegram

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. Create an OKX API key. Start with a **demo trading** key. Enable trade permissions for the grid you intend to run. Do not commit the key to the repository.
3. Apply for an AI Builder Code on [OKX AI Builder](https://www.okx.com/en-us/agent-tradekit/builder) and register this repository URL (see the next section).
4. Install and start:

```bash
cd pulsegrid
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the variables below; do not commit .env
python main.py --check # local unit tests; no network
python main.py
```

5. Open your bot in Telegram and send `/start`. A persistent main menu appears at the bottom.

Button labels below are the literal strings the bot shows. The rest of this guide is English.

| Button | Command | What it does |
| --- | --- | --- |
| 📝 新建策略 | Send a sentence directly | Asks you to type a natural-language request. The button itself is not sent to Groq |
| 📊 我的网格 | `/grids` or `/positions` | Lists spot grids on OKX that have not been stopped: pair, algoId, bounds, and state |
| 🛑 停止网格 | `/stop` | Pick a grid, then confirm a second time before stop is called |
| 📡 状态 | `/status` | Demo or live, model, candle interval, and how many pairs are being watched |
| ❓ 帮助 | `/help` | Example phrasing, the iron rules, and what each confirmation-card field means |
| Main menu | `/menu` | Shows the menu text and keyboard again |

You can send an example sentence straight to the bot. You do not have to tap the menu first:

```text
I have 2000 USDT. I plan to hold SOL, prefer a conservative profile, and want the grid to withstand about a 15% drawdown.
```

Last price, upper bound, lower bound, grid count, and stop-loss on the confirmation card come from the local quant engine. Only the explanation paragraph is written by the model, and that paragraph is Chinese. After you tap one-tap launch, the bot fetches OI/CVD once more. An emergency brake or insufficient data refuses the order. Stopping a grid also requires a second confirmation, and only the user who opened that stop list can confirm. Stopping sells the base currency held in the grid.

The five Chinese buttons on the menu are navigation. They are not passed to the model as strategy text.

## Environment variables

The variables are defined in `pulsegrid/.env.example`. The process reads `pulsegrid/.env` or the real environment. Do not put secrets in the repository.

| Variable | Role |
| --- | --- |
| `GROQ_API_KEY` | Groq key. Used only for intent and the written explanation |
| `GROQ_MODEL` | Default `openai/gpt-oss-120b` |
| `TELEGRAM_BOT_TOKEN` | Bot token issued by BotFather |
| `OKX_API_KEY` | OKX API key |
| `OKX_SECRET_KEY` | OKX secret |
| `OKX_PASSPHRASE` | Passphrase set when the API key was created |
| `OKX_FLAG` | `1` demo trading (default), `0` live trading |
| `OKX_AI_BUILDER_CODE` | AI Builder Code. May be empty at startup. Required before confirm-to-order and stop-grid, and written into `tag` |
| `OKX_BASE_URL` | Default `https://www.okx.com` |
| `DEFAULT_KLINE_BAR` | Default candle interval, `15m` |
| `MONITOR_INTERVAL_SEC` | Order-book sentinel poll interval, default 8 seconds |
| `LOG_LEVEL` | Log level, default `INFO` |

If the Groq key, the Telegram token, or the OKX key trio is empty at startup, `python main.py` exits and names the missing item. `OKX_AI_BUILDER_CODE` may stay empty so the bot can still start while a Builder application is under review. `/status` and `/help` report that it is not configured. Confirm-to-order and stop-grid fail until the code is set and the bot is restarted, and no trading request is sent.

## AI Builder Code and the GitHub URL

The OKX AI Builder program uses the project's public repository to identify whose implementation this agent is. When you apply or register a Builder, enter:

`https://github.com/Mulliner11/PulseGrid-AI`

That URL is the project identity OKX uses to match the agent to this code. It is **not** written onto orders.

Trade attribution uses a separate credential: the AI Builder Code. Put it in `OKX_AI_BUILDER_CODE`. This repository's OKX client forces the field `tag` on every request that changes an order or a grid. OpenAPI does not accept a field named `aiBuilderCode`. If a caller supplies a different `tag`, it is overwritten with this code.

You can start before the code arrives. While it is unset, one-tap order on the confirmation card and stop-grid both fail with an explanation, and the request is not sent to OKX. The read-only grid list does not write `tag`. After you receive the code, set `OKX_AI_BUILDER_CODE` and restart.

Integration guide: <https://www.okx.com/en-us/help/ai-builder-program-integration-guide>

## Demo and live

- `OKX_FLAG=1` (default): private requests send `x-simulated-trading: 1` and hit demo trading. Use a demo-trading API key.
- `OKX_FLAG=0`: live trading. A confirmed grid uses real funds. Startup logs a warning. Switch to a live API key. Do not send a demo key to the live endpoint.

The confirmation card and the status view both state whether the current environment is demo or live.

## Project layout

```text
.
├── README.md
├── assets/                      # logo
└── pulsegrid/
    ├── main.py                  # start the bot; --check runs tests
    ├── .env.example
    ├── requirements.txt
    ├── bot/
    │   ├── handlers.py          # intent → confirmation card → order
    │   ├── menu.py              # main menu, my grids, stop grid
    │   └── ui_cards.py          # HTML confirmation card
    ├── config/settings.py
    ├── core/
    │   ├── llm/groq_client.py   # intent + written explanation
    │   ├── okx/client.py        # REST; trading paths force tag
    │   ├── okx/strategy_algo.py # spot grid place / stop / query / amend bounds
    │   └── quant/               # ATR, VWAP, CVD, sentinel, grid
    └── tests/test_phase1.py
```

The quant layer does not import Groq. The LLM layer does not import OKX and does not assemble order parameters.

## Self-check

From the `pulsegrid` directory:

```bash
python main.py --check
```

This runs the unit tests under `pulsegrid/tests`: grid math, the emergency brake, intent JSON, the confirmation card, the Builder Code `tag`, menu routing, and a fake OKX client walking through order-only-after-confirm and stop-only-after-confirm. The tests do not call Groq or OKX.

## Current scope

Already in Phase-1:

- Natural-language intent, an OKX market-data bundle, an adaptive grid, and an OI/CVD emergency brake
- A confirmation card, and a spot grid that is submitted only after confirmation
- A persistent Chinese menu: new strategy, my grids, stop grid, status, and help
- The AI Builder Code written on every trading path
- Order-book sentinel notifications on the same event loop

Not in Phase-1 yet:

- No candle chart, equity curve, or other chart UI. Grid information is text plus the confirmation card.
- One-tap launch places a spot grid only. It does not place a contract grid.
- When the user asks for a short, the intent records it. A short is not opened automatically.
- `rebalance_grid_bounds` exists on the OKX wrapper. Telegram has no amend-bounds entry yet.
- The bot uses one OKX API. It does not split trading accounts by Telegram user. Stop confirmation only checks that the user who opened the list is the user who confirms.
