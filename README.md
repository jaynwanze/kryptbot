# KryptBot

![Pairs](https://img.shields.io/badge/Pairs-Multi--pair%20(Linear)-blueviolet?labelColor=292D3E)
![TF](https://img.shields.io/badge/Timeframe-15m-informational?labelColor=292D3E)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white&labelColor=292D3E)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white&labelColor=292D3E)
![CI](https://img.shields.io/github/actions/workflow/status/<YOUR_GH_USER>/kryptbot/ci.yml?label=CI&logo=githubactions&labelColor=292D3E)
![Last commit](https://img.shields.io/github/last-commit/<YOUR_GH_USER>/kryptbot?color=informational)
![License](https://img.shields.io/github/license/<YOUR_GH_USER>/kryptbot?color=informational)

> **15‑minute crypto signal engine** – BOS → FVG alignment · Stoch/RSI momentum · ADX/ATR volatility veto · Hourly liquidity bias · ATR‑based SL/TP · Telegram alerts · (optional) Bybit auto‑execution via RiskRouter.

---

## Table of Contents

1. [What it does](#1-what-it-does)  
2. [Strategy logic](#2-strategy-logic-tldr)  
3. [Local usage](#3-local-usage)  
4. [Docker](#4-docker)  
5. [AWS EC2 quick-start](#5-deploying-to-aws-ec2)
6. [Env / Config Parameters](#6-env--config-parameters)  
7. [Requirements](#7-requirements)
8. [Risk Sizing Cheat‑Sheet](#8-risk-sizing-cheat-sheet)
9. [Road-map / TODO](#9-road-map--todo)  
10. [Disclaimer](#10-disclaimer)

---

## 1. What It Does

| Mode | File(s) | Purpose |
|------|---------|---------|
| **Back‑test** | `bot/backtest/backtest.py` | Fast replay of historical 15m bars. Prints every trade, win/loss stats, profit factor, optional equity curve. |
| **Live Signal Engine (multi‑pair)** | `bot/engines/lrs_pair_engine.py` | Subscribes to Bybit WS (`kline.{TF}.{SYMBOL}`) for each symbol, computes indicators, evaluates signals, pushes alerts to Telegram, and enqueues `Signal` objects. |
| **Risk Router (auto‑trade)** | `bot/engines/risk_router.py` + `bot/infra/*` | Consumes `Signal`s, sizes the position by % risk, places MARKET+TP/SL brackets via Bybit REST, and reconciles fills/cancels from the private WS. |
| **Telegram helper** | `bot/helpers/telegram.py` | Async `alert_side()` wrapper to post MarkdownV2‑escaped messages. |
| **Dockerized runtime** | `docker-compose.yaml`, `Dockerfile` | Single service (engine) container with .env injection and auto‑restart. |

---

## 2. Strategy Logic (TL;DR)

**Base TF:** 15‑minute.

| Component | Long Rule | Short Rule |
|-----------|-----------|------------|
| Break of Structure (BOS) | Current candle takes out previous swing **high** | Takes out previous swing **low** |
| Fair Value Gap (FVG) | FVG ≥ `FVG_MIN_PX` forms immediately after BOS | Same, mirrored |
| HTF Bias (1H map) | Price trades **above** last hourly support; no fresh hourly high “raids” above us | Price trades **below** last hourly resistance; no fresh hourly low “raids” below us |
| Momentum | %K > %D and %K ≥ `STO_K_MIN_LONG` | %K < %D and %K ≤ (100 - `STO_K_MIN_SHORT`) |
| Volatility Veto | `atr >= atr_veto * atr30` (dynamic) | Same |
| Trend/Strength | `adx >= min_adx` (dynamic) | Same |
| Risk | SL = `(ATR_MULT_SL * 1.6 + WICK_BUFFER) * ATR` | Mirrored |
| Reward | TP = `ATR_MULT_TP * ATR` (default RR ≈ 2:1) | Mirrored |
| Sizing | Position risk = `equity * RISK_PCT` | Same |

> The HTF map is **updated after** signal logic each bar to avoid “peeking” at the current bar.

---

## 3. Local Usage
*Quick Start* 
```bash
git clone https://github.com/<YOUR_GH_USER>/kryptbot.git
cd kryptbot
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

cp .env.example .env        # fill tokens & chat id
python -m bot.engines.lrs_pair_engine
```

*Backtesting* 
```bash
python backtest/backtest.py              # default = last 30 days
python backtest/backtest.py --days 30    # June 2025 example
```

*Live Engine* - Run directly
```bash
python -m bot.engines.lrs_pair_engine
```

It will:
- Preload 1 000 15m candles/pair (REST)
- Subscribe to Bybit public WS (kline.{TF}.{SYMBOL})
- For each confirmed bar:
- Compute indicators
- Lookup previous HTF row
- Check vetoes & signal rules
- Send Telegram alert (if any)
- Push a Signal into SIGNAL_Q
- RiskRouter consumes queue, places bracket orders if you enabled it (and testnet=False).

## 4. Docker
*Build & run*  
```bash
docker build -t kryptbot:latest .
docker compose up -d           # or: docker-compose up -d
```

*docker-compose.yaml*
```yml
version: "3.9"

services:
  lrs_pair_engine:
    image: kryptbot:latest
    command: python -m bot.engines.lrs_pair_engine
    container_name: krypt_pair
    env_file: .env
    restart: unless-stopped
    volumes:
      - .:/app          # live-mount for dev; remove in prod for immutable image
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

*Common Commands*
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
docker logs -f krypt_pair
```

## 5. Deploying to AWS EC2
```bash
# on fresh Ubuntu 24.04 t2.micro
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker ubuntu && newgrp docker

git clone https://github.com/jaynwanze/kryptbot.git
cd kryptbot
cp .env.example .env        # fill in your tokens
docker compose build --no-cache
docker compose up -d
```

## 6. Env / Config Parameters
*env*
```bash
# Telegram
TELE_TOKEN=123456789:ABC-DEF...
TG_CHAT_ID=123456789

# Bybit
BYBIT_KEY=live_key_here
BYBIT_SEC=live_secret_here
BYBIT_KEY_TEST=test_key_here
BYBIT_SEC_TEST=test_secret_here
```

*config*
```bash
| Name              | Meaning                                 | Example                     |
| ----------------- | --------------------------------------- | --------------------------- |
| `INTERVAL`        | Chart TF in minutes (string)            | `"15"`                      |
| `PAIRS_LRS_MULTI` | Symbols to stream concurrently          | `["SOLUSDT","ETHUSDT",...]` |
| `RISK_PCT`        | Fraction of equity to lose on SL hit    | `0.20` (20%)                |
| `ATR_MULT_SL`     | SL distance in ATRs (before 1.6 factor) | `1.0`                       |
| `ATR_MULT_TP`     | TP distance in ATRs                     | `2.0`                       |
| `LEVERAGE`        | Bybit leverage to set once per symbol   | `25`                        |
| `HTF_DAYS`        | How far back to find swing levels       | `15`                        |
| `LOOKBACK_BARS`   | Bars kept in memory per pair            | `1000`                      |
```

## 7. Requirements 
(inside requirements.txt)
```txt
numpy
pandas>=2.0
ta                # or pandas-ta, whichever you actually call in helpers
matplotlib        # only needed for back‑test plots
websockets>=11.0
ccxt>=4.0         # 4.x has the async_support sub‑package
python-telegram-bot==13.*
python-dotenv
pybit==5.*
```

## 8. Risk Sizing Cheat Sheet
```python
risk_usd = equity * RISK_PCT
qty      = risk_usd / stop_dist            # stop_dist = |entry - sl|
pnl_tp   = risk_usd * (ATR_MULT_TP / ATR_MULT_SL)
```

## 9. Road-map / TODO
- [ ] **Full auto trade mode: track PnL, move SL → BE at +1R, ATR trailing remainder
- [ ] **Funding-rate & fee accounting** inside the back-tester  
- [ ] **CLI parameters** for symbol, leverage and timeframe (`--pair`, `--lev`, `--tf`, …)  
- [ ] **CI / CD** – GitHub Actions workflow to build & push a Docker image on every git **tag**
- [ ]  Better HTF engine (dynamic 4h/daily mixed)
- [ ] Web UI dashboard (FastAPI + websocket) for monitoring


## 10. Disclaimer
This repository is **for educational purposes only** and does **not** constitute financial advice.  
Trading leveraged instruments is risky. Double-check the logic, run on Bybit **test-net** first, and
use capital you can afford to lose. The code is provided *as-is* with **no warranty**.

Happy trading 🚀

---

