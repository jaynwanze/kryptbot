# KryptBot &nbsp;
![Pair](https://img.shields.io/badge/Pair-SOL%2FUSDT-blueviolet?logo=solidity&logoColor=white&labelColor=292D3E)
![Python](https://img.shields.io/badge/python-3.8+-3776AB.svg?logo=python&logoColor=white&labelColor=292D3E)
![Docker Image](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white&labelColor=292D3E)
![CI](https://img.shields.io/github/actions/workflow/status/jaynwanze/kryptbot/ci.yml?label=CI&logo=githubactions&labelColor=292D3E)  
![Last commit](https://img.shields.io/github/last-commit/jaynwanze/kryptbot?color=informational)
![License](https://img.shields.io/github/license/jaynwanze/kryptbot?color=informational)

> **15-minute SOL/USDT signal bot** — EMA 7/14 cross · Stoch-RSI · ADX · multi-TF trend filter.  
> Back-test, stream live alerts to Telegram, run anywhere with Docker.

---

## Table&nbsp;of&nbsp;Contents
1. [What it does](#1-what-it-does)  
2. [Strategy logic](#2-strategy-logic-tldr)  
3. [Local usage](#3-local-usage)  
4. [Docker](#4-docker)  
5. [AWS EC2 quick-start](#5-deploying-to-aws-ec2)  
6. [Requirements](#6-requirements)  
7. [Road-map / TODO](#7-road-map--todo)  
8. [Disclaimer](#8-disclaimer)

---


## 1. What it does

| Mode | Purpose |
|------|---------|
| **Back-test (`backtest.py`)** | Quickly replay one or many months of 15-minute candles and print (✅ wins / ❌ losses / equity curve). |
| **Live stream (`LIVE_BOT_rsi_stoch_7_ma_cross_14_ma_strat.py`)** | Subscribes to Bybit’s WebSocket (`kline.15.SOLUSDT`). On every *closed* candle:<br>• computes indicators<br>• fires LONG/SHORT alerts to Telegram (optional screenshot)<br>• (placeholder) place real orders. |
| **Docker image** | Reproducible runtime with Python 3.8-slim + only the libraries we need. |

---

## 2. Strategy logic (TL;DR)

*15-minute chart*  

| Component | Rule |
|-----------|------|
| **Entry trigger** | EMA-7 crosses EMA-14 **and** both are on the correct side of EMA-28. |
| **Momentum** | Stoch-RSI %K must be > 45 (long) / > 30 (short). |
| **Trend filter (1-hour)** | Price > EMA-50 **and** EMA-50 slope > 0 (long) – inverse for short. |
| **ADX filter** | ADX ≥ 20. |
| **Risk / reward** | Stop = 2 × ATR (plus 25 % wick buffer) · Target = 4 × ATR (RR 2 : 1). |
| **Position size** | Fixed risk = 2 % of current equity. |

---

## 3. Live alerts
*Environment file*  
```bash
python backtest.py              # default = last 180 days
python backtest.py --days 30    # June 2025 example
```

*Run Locally*
```bash
python LIVE_BOT_rsi_stoch_7_ma_cross_14_ma_strat.py
```

## 4. Docker
*Environment file*  
```bash
docker build -t kryptbot:1.0 .
```

*Run (detached, auto-restart)*
```bash
docker run -d --name kryptbot \
           --env-file .env \
           --restart unless-stopped \
           kryptbot:1.0
```

*Update image*
```bash
git pull
docker build -t kryptbot:1.1 .
docker stop kryptbot && docker rm kryptbot
docker run -d --name kryptbot --env-file .env --restart unless-stopped kryptbot:1.1
```
(Or simply tag :latest and docker pull in production.)


## 5. Deploying to AWS EC2
```bash
# on fresh Ubuntu 24.04 t2.micro
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker ubuntu && newgrp docker

git clone https://github.com/jaynwanze/kryptbot.git
cd kryptbot
cp .env.example .env        # fill in your tokens
docker build -t kryptbot:1.0 .
docker run -d --name kryptbot --env-file .env --restart unless-stopped kryptbot:1.0

```

## 6. Requirements (inside requirements.txt)
```txt
ccxt
pandas
numpy
matplotlib
python-telegram-bot==13.*
websockets
python-dotenv
ta              # technical indicators for back-test
```

## 7. Road-map / TODO
- [ ] **Bybit REST trading** – wire in order-creating/position endpoints → *fully automated* mode  
- [ ] **Funding-rate & fee accounting** inside the back-tester  
- [ ] **CLI parameters** for symbol, leverage and timeframe (`--pair`, `--lev`, `--tf`, …)  
- [ ] **CI / CD** – GitHub Actions workflow to build & push a Docker image on every git **tag**  


## 8&nbsp;. Disclaimer 🚨
This repository is **for educational purposes only** and does **not** constitute financial advice.  
Trading leveraged instruments is risky. Double-check the logic, run on Bybit **test-net** first, and
use capital you can afford to lose. The code is provided *as-is* with **no warranty**.

Happy trading 🚀

---

