# 🤖 KryptBot - Multi-Strategy Crypto Trading System

Advanced algorithmic trading system for cryptocurrency markets with multiple strategy engines, AI-powered forecasting, and comprehensive risk management.

---

## 📊 **Active Trading Strategies**

### **1. FVG Order Flow Engine** (Primary - 1H Timeframe)
**Status**: ✅ Live Production

Advanced institutional-style strategy using Fair Value Gaps (FVG) and Order Flow analysis.

#### **Core Concepts**
- **Fair Value Gaps (FVG)**: Price imbalances where market moved too fast, leaving unfilled orders
- **Order Flow Momentum**: Directional strength calculated from recent volume-weighted price action
- **Volume Profile**: Identifies high-volume nodes (POC) and low-volume nodes (LVN) as targets
- **Dual Take-Profit Ladder**: TP1 → Breakeven → TP2 for optimal risk/reward

#### **Entry Logic**
```
LONG Setup:
1. Bullish FVG detected (low[i-2] > high[i])
2. Order Flow Score > 30 (positive momentum)
3. Price near Volume Profile value area (POC/VAH)
4. ADX > 25 (trending market)
5. Volume > 1.5x average
6. No conflicting HTF resistance

SHORT Setup:
1. Bearish FVG detected (high[i-2] < low[i])
2. Order Flow Score < -30 (negative momentum)
3. Price near Volume Profile value area (POC/VAL)
4. ADX > 25 (trending market)
5. Volume > 1.5x average
6. No conflicting HTF support
```

#### **Exit Management**
```
TP1 Hit (1.5R):
├─ Take 50% profit
├─ Move SL to Breakeven + 2 ticks
└─ Let runner target TP2

TP2 Hit (2.5R):
└─ Close full position

SL Hit:
└─ Close position (now at BE after TP1, or -1R if before TP1)
```

#### **Backtest Results** (DOGEUSDT 15m, 30 days)
```
Trades:           71
Win Rate:         60.56%
Profit Factor:    1.40
Net P&L:          +$1,967 (+196.7%)
Avg Win:          $159.59
Avg Loss:         $174.82
Risk per Trade:   10%
```

#### **Order Flow Calculation**
```python
OF_Score = Σ(Volume[i] × Sign(Close[i] - Close[i-1])) / Total_Volume
- Lookback: 5 bars (15m) or 10 bars (1H)
- Multiplier: 2.0x for amplification
- Threshold: |30| minimum for signals
```

---

### **2. EMA Pair Engine** (Secondary - 1H Timeframe)
**Status**: ✅ Live Production

Classic trend-following with HTF confirmation and LTF precision entries.

#### **Entry Logic**
```
LONG Setup:
1. EMA9 > EMA21 > EMA50 (bullish alignment)
2. H1 slope positive (HTF trend confirmation)
3. Price near HTF support level
4. LTF confirmation (1/3): BOS, FVG, or Fib retracement
5. Stochastic K < 35 (oversold for longs)
6. ADX > 28 (strong trend)

SHORT Setup:
1. EMA9 < EMA21 < EMA50 (bearish alignment)
2. H1 slope negative (HTF trend confirmation)
3. Price near HTF resistance level
4. LTF confirmation (1/3): BOS, FVG, or Fib retracement
5. Stochastic K > 65 (overbought for shorts)
6. ADX > 28 (strong trend)
```

#### **Risk Management**
- **Stop Loss**: 1% below/above entry
- **Take Profit**: 3R (3% away)
- **Trailing**: ATR-based once +2R in profit
- **Position Size**: 10% equity per trade
- **Max Concurrent**: 3 positions
- **Daily Cap**: 5 trades/day

---

## 🛡️ **Risk Management System**

### **Portfolio-Level Controls**
```python
MAX_OPEN_CONCURRENT = 3      # Max simultaneous positions
MAX_TOTAL_RISK_PCT = 30%     # Max portfolio risk exposure
MAX_PER_SIDE = 1             # Max longs OR shorts at once
MAX_TRADES_PER_DAY = 5       # Daily trade cap
RISK_PER_TRADE = 10%         # Equity risked per trade
```

### **Signal Quality Gates**
```
Phase 1: Market Quality
├─ ADX > 28 (strong trend)
├─ Volume > 1.5x average
└─ ATR > 0.8x 30-bar ATR

Phase 2: HTF Alignment
├─ H1 slope matches direction
├─ Near HTF S/R level (within 2 ATR)
└─ No conflicting higher timeframe

Phase 3: LTF Confirmation (FVG only)
├─ Fair Value Gap present
├─ Order Flow Score > threshold
└─ Volume Profile confluence
```

### **Cooldown Periods**
```
After SL Hit:  0.5 days (12 hours)
After TP Hit:  0.25 days (6 hours)
Purpose: Avoid revenge trading and overtrading
```

---

## 🎯 **Trading Pairs** (18 Total)

### **L1 Blockchains** (6)
- SOLUSDT, AVAXUSDT, ADAUSDT, NEARUSDT, APTUSDT, SUIUSDT

### **L2 Solutions** (2)
- OPUSDT, ARBUSDT

### **DeFi** (3)
- AAVEUSDT, UNIUSDT, LDOUSDT

### **Interoperability** (2)
- ATOMUSDT, DOTUSDT

### **Payments** (1)
- XRPUSDT

### **AI/Compute** (1)
- RENDERUSDT

### **Meme** (1)
- DOGEUSDT

### **Oracle** (1)
- LINKUSDT

---

## 🤖 **AI Forecast System**

### **GPT-4o Integration**
```python
async def generate_forecast(router, pairs, drop_stats):
    """
    AI-powered market analysis using:
    - Recent trade performance
    - Current open positions
    - Signal rejection stats
    - HTF level analysis
    - Cluster correlation patterns
    """
```

### **Forecast Content**
1. **Market Bias**: Overall directional sentiment
2. **Hot Pairs**: Top 3-5 pairs likely to signal
3. **Trade Outlook**: Expected signals and win rate
4. **Key Levels**: HTF S/R being tested
5. **Risk Assessment**: Trending vs. choppy conditions

### **Delivery**
- Telegram alerts every 6 hours
- On-demand via `/forecast` command
- Pre-session briefings (Asia, London, NY open)

---

## 📡 **Infrastructure**

### **Data Pipeline**
```
Bybit WebSocket (Live Klines)
    ↓
Indicator Calculation (TA-Lib)
    ↓
HTF Level Analysis (4H/D1)
    ↓
Signal Generation (FVG/EMA)
    ↓
Quality Gates (ADX/Volume/HTF)
    ↓
Risk Router (Position Sizing)
    ↓
Order Execution (Bybit API)
```

### **Components**
- **Data Layer**: `bot/data/` - Historical/live data management
- **Indicators**: `bot/helpers/indicators.py` - TA-Lib wrappers
- **Signals**: `bot/helpers/signals/` - Strategy-specific logic
- **Engines**: `bot/engines/` - Live trading loops
- **Risk Router**: `bot/engines/risk_router.py` - Order management
- **Backtests**: `bot/backtest/` - Historical validation

---

## 🚀 **Quick Start**

### **Installation**
```bash
git clone https://github.com/yourusername/kryptbot.git
cd kryptbot
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### **Configuration**
```bash
# Create .env file
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
BYBIT_TESTNET=false
OPENAI_API_KEY=your_openai_key
TELE_TOKEN=your_telegram_token
TG_CHAT_ID=your_chat_id
```

### **Run FVG Order Flow Engine**
```bash
# Testnet first!
python -m bot.engines.fvg_orderflow_engine
```

### **Run EMA Pair Engine**
```bash
python -m bot.engines.ema_pair_engine
```

### **Run Backtest**
```bash
python -m bot.backtest.fvg_orderflow_backtest
```

---

## 📊 **Monitoring & Commands**

### **Telegram Bot Commands**
```
/status       - Current positions and equity
/forecast     - AI market analysis
/stats        - Performance metrics
/pairs        - Active trading pairs
/drop_stats   - Signal rejection breakdown
/equity       - P&L curve
/open         - Open positions details
/closed       - Recent closed trades
```

### **Live Logs**
```bash
tail -f logs/signals_fvg.csv       # Signal generation
tail -f logs/trades.csv            # Completed trades
tail -f logs/decisions_fvg.csv     # Entry/exit decisions
tail -f logs/orders.csv            # Order execution
```

---

## 📈 **Performance Tracking**

### **Logged Metrics**
- **Entry/Exit**: Timestamp, price, reason (TP1/TP2/SL)
- **Signal Quality**: OF score, ADX, volume, level type
- **Drop Stats**: Why signals were rejected (ADX/HTF/volume)
- **Trade Stats**: Win rate, profit factor, avg R-multiple
- **Equity Curve**: Real-time balance tracking

### **CSV Exports**
```
signals_fvg.csv     - All generated signals
trades.csv          - Completed round trips
decisions_fvg.csv   - Bar-by-bar decision log
orders.csv          - Bybit order API responses
```

---

## 🔬 **Backtesting Framework**

### **Features**
- Bar-by-bar replay (no look-ahead bias)
- Realistic slippage/fees (0.055% per side)
- FVG tracking and expiration (24h window)
- Volume Profile recalculation
- Dual TP ladder simulation
- Drop stats tracking

### **Run Custom Backtest**
```python
from bot.backtest.fvg_orderflow_backtest import backtest

summary, trades, curve = backtest(
    df=hist,
    equity0=1000,
    risk_pct=0.10,
    pair="DOGEUSDT"
)
```

---

## 🛠️ **Technical Stack**

### **Core Dependencies**
- **Python 3.11+**
- **TA-Lib**: Technical indicators
- **Pandas**: Data manipulation
- **ccxt**: Exchange abstraction
- **websockets**: Real-time data
- **pybit**: Bybit API client
- **python-telegram-bot**: Alerts
- **openai**: AI forecasting

### **Architecture Patterns**
- **Async/await**: Non-blocking I/O
- **Dataclasses**: Type-safe models
- **Queue-based**: Signal coalescing
- **Singleton**: Config management
- **Observer**: WebSocket streams

---

## ⚠️ **Risk Disclaimer**

**IMPORTANT**: This software is for educational purposes only.

- ❌ Not financial advice
- ❌ No guarantees of profit
- ❌ Past performance ≠ future results
- ✅ Test thoroughly on testnet first
- ✅ Never risk more than you can afford to lose
- ✅ Understand the code before running live

**Cryptocurrency trading is highly risky and volatile.**

---

## 📝 **Development Roadmap**

### **Completed** ✅
- [x] FVG Order Flow strategy
- [x] EMA Pair strategy
- [x] Dual TP ladder system
- [x] Risk Router with portfolio caps
- [x] AI forecast integration
- [x] HTF level analysis
- [x] Drop stats tracking
- [x] Telegram bot commands

### **In Progress** 🔄
- [ ] Machine learning signal scoring
- [ ] Dynamic position sizing (Kelly Criterion)
- [ ] Multi-exchange support
- [ ] Mobile app for dashboards

### **Planned** 📋
- [ ] Options strategy engine
- [ ] Correlation-based hedging
- [ ] Sentiment analysis (Twitter/Reddit)
- [ ] Backtesting UI

---

## 🤝 **Contributing**

Contributions welcome! Please:
1. Fork the repo
2. Create feature branch
3. Add tests for new strategies
4. Submit PR with detailed description

---

## 📄 **License**

MIT License - See [LICENSE](LICENSE) file

---

## 📞 **Support**

- **Issues**: [GitHub Issues](https://github.com/jaynwanze/kryptbot/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/kryptbot/discussions)
- **Telegram**: @kryptbot

---

## 🙏 **Acknowledgments**

- **ICT Concepts**: Fair Value Gaps, Order Blocks, Market Structure
- **Volume Profile**: VPVR methodology from Peter Reznicek
- **Risk Management**: Van Tharp position sizing principles
- **Backtesting**: Inspired by Backtrader framework

---

**Built with ❤️ by algorithmic traders, for algorithmic traders.**

*Last Updated: October 28, 2025*