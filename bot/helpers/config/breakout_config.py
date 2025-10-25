# bot/helpers/config_breakout.py
"""
BREAKOUT STRATEGY CONFIG

Key differences from mean-reversion:
- Higher ADX floor (we want strong trends)
- Stoch checks flipped (don't buy overbought, don't sell oversold)
- Wider R:R (breakouts can run further)
- Removed HTF proximity filter (we break levels, not fade them)
"""

from typing import List
import asyncio
from collections import defaultdict

# Global drop stats
GLOBAL_DROP_STATS = defaultdict(lambda: defaultdict(int))
GLOBAL_DROP_STATS_LOCK = asyncio.Lock()

# Basic settings
PAIR = "BTCUSDT"  # Bybit symbol: SOL/ATOM/WAVES/XRP
TF_SECONDS = 60 * 60  # 1‑minute bars
INTERVAL = "60"  # stream interval, string
LOOKBACK_BARS = 800
TRAIL_ENABLE = True  # More important for breakouts

# Portfolio guards
MAX_TRADES_PER_DAY = 3
MAX_SIGNAL_AGE_SEC = 30
COALESCE_SEC = 2
MAX_PER_SIDE_OPEN = 1
MAX_TOTAL_RISK_PCT = 0.30
MAX_OPEN_CONCURRENT = 3

# Position sizing
RISK_PCT = 0.1
STAKE_SIZE_USD = 1_000
LEVERAGE = 20

# ═══════════════════════════════════════════════════════════════════════════
# BREAKOUT STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════

# Risk/Reward (breakouts can run further)
RR_TARGET = 2.5  # Up from 1.5 - let winners run
MIN_ATR_RATIO = 1.1  # High volatility only
SL_CUSHION_MULT = 1.2  # Less cushion needed
WICK_BUFFER = 0.2

# Trend strength (breakouts need strong trends)
ADX_HARD_FLOOR = 50  # Higher than mean-reversion's 25
MIN_H1_SLOPE = 0.08  # Need clear directional bias

# Momentum filters (FLIPPED from mean-reversion)
# For LONGS: Don't buy if already too overbought
MOMENTUM_STO_K_LONG_MAX = 90  # Max k_slow for long entries

# For SHORTS: Don't sell if already too oversold  
MOMENTUM_STO_K_SHORT_MIN = 10  # Min k_slow for short entries

# Breakout-specific
BREAKOUT_THRESHOLD_ATR = 0.3  # How far past level = confirmed break
PULLBACK_PROXIMITY_ATR = 0.4  # How close to level = valid pullback
PULLBACK_LOOKBACK_BARS = 8  # How many bars to look for pullback

# Cooldowns (shorter for breakouts - they're faster)
COOLDOWN_DAYS_AFTER_SL = 0.25  # 6 hours
COOLDOWN_DAYS_AFTER_TP = 0.1   # 2.4 hours

# Sessions (keep all hours for breakouts)
SESSION_WINDOWS = {
    "asia": (0, 24),   # Trade 24/7 for breakouts
}

# HTF settings
HTF_DAYS = 15
HTF_LEVEL_TF = "1H"

# Execution
SLIP_BPS = 0.0002
FEE_BPS = 10

# Tick sizes (same as before)
TICK_SIZE = {
    "ETHUSDT": 0.01,
    "AAVEUSDT": 0.01,
    "SOLUSDT": 0.01,
    "ATOMUSDT": 0.001,
    "AVAXUSDT": 0.001,
    "RENDERUSDT": 0.001,
    "NEARUSDT": 0.001,
    "APTUSDT": 0.001,
    "LINKUSDT": 0.001,
    "XRPUSDT": 0.0001,
    "DOTUSDT": 0.0001,
    "ARBUSDT": 0.0001,
    "DOGEUSDT": 0.00001,
    "ADAUSDT": 0.0001,
    "SUIUSDT": 0.0001,
}

# Pairs (same as before)
PAIRS_LRS: List[str] = [
    "ATOMUSDT",
    "DOTUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "APTUSDT",
    "NEARUSDT",
    "SUIUSDT",
    "ARBUSDT",
    "DOGEUSDT",
    "AAVEUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "RENDERUSDT",
]

# Clusters (same as before)
CLUSTER = {
    "NEARUSDT": "L1",
    "AVAXUSDT": "L1",
    "SOLUSDT": "L1",
    "ADAUSDT": "L1",
    "APTUSDT": "L1",
    "SUIUSDT": "L1",
    "ARBUSDT": "L2",
    "DOTUSDT": "INTEROP",
    "ATOMUSDT": "INTEROP",
    "XRPUSDT": "PAYMENTS",
    "LINKUSDT": "ORACLE",
    "AAVEUSDT": "DEFI",
    "RENDERUSDT": "AI",
    "DOGEUSDT": "MEME",
}