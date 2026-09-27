# PulseGrid AI（脉冲智网）

OKX-ecosystem adaptive anti-break-grid AI trading engine.

- **Telegram Bot** frontend (`python-telegram-bot`, async)
- **Groq** for NLU + Chinese rationale only (never prices / order params)
- **Python** owns OKX market data, ATR/VWAP/CVD/OI brakes, adaptive grid math, and execution
- Every trade path attaches **AI Builder Code** as OKX order field `tag`

## Quick start

```bash
cd pulsegrid
cp .env.example .env   # fill keys
pip install -r requirements.txt
python main.py --check
python main.py
```

Default sim trading (`OKX_FLAG=1`). Set `OKX_AI_BUILDER_CODE` before live or demo requests that require the Builder tag.

## Layout

- `pulsegrid/config` — settings
- `pulsegrid/core/llm` — Groq intent / rationale
- `pulsegrid/core/quant` — indicators, sentinel, grid engine
- `pulsegrid/core/okx` — OKX client + strategy algo
- `pulsegrid/bot` — Telegram handlers and confirm cards
