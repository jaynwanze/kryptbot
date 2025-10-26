# bot/helpers/config/breakout_config.py
"""
BREAKOUT STRATEGY CONFIG - OPTIMIZED FOR ETH + SOL

Based on 100-day backtest results:
- ETHUSDT: 53.3% WR, 2.61 PF, +$3,600 (45 trades)
- SOLUSDT: 38.5% WR, 1.45 PF, +$2,273 (78 trades)
- Higher ADX floor (we want strong trends)
- Stoch checks flipped (don't buy overbought, don't sell oversold)
- Wider R:R (breakouts can run further)
- Trade on 1H timeframe (not 15m)
"""

from typing import List
import asyncio
from collections import defaultdict

# Global drop stats
GLOBAL_DROP_STATS = defaultdict(lambda: defaultdict(int))
GLOBAL_DROP_STATS_LOCK = asyncio.Lock()

# ═══════════════════════════════════════════════════════════════════════════
# CRITICAL: TIMEFRAME SETTINGS
# ═══════════════════════════════════════════════════════════════════════════
TF_SECONDS = 60 * 60  # 1-HOUR bars (not 15m!)
INTERVAL = "60"  # "60" for 1H
LOOKBACK_BARS = 800

# ═══════════════════════════════════════════════════════════════════════════
# PAIRS - OPTIMIZED (Only proven winners)
# ═══════════════════════════════════════════════════════════════════════════
PAIRS_LRS: List[str] = [
    "ETHUSDT",   # 53.3% WR, 2.61 PF, +$3,600 (EXCELLENT)
    'DOTUSDT',  
    'ADAUSDT',  
]

# Portfolio guards
MAX_TRADES_PER_DAY = 3
MAX_SIGNAL_AGE_SEC = 60  # Longer for 1H (was 30 for 15m)
COALESCE_SEC = 5
MAX_PER_SIDE_OPEN = 1
MAX_TOTAL_RISK_PCT = 0.30
MAX_OPEN_CONCURRENT = 2  # Max 2 since we only have 2 pairs

# Position sizing
RISK_PCT = 0.1  # 10% risk per trade
STAKE_SIZE_USD = 1_000
LEVERAGE = 20

# ═══════════════════════════════════════════════════════════════════════════
# BREAKOUT STRATEGY PARAMETERS (from backtest)
# ═══════════════════════════════════════════════════════════════════════════

# Risk/Reward
RR_TARGET = 1.5  # Standard for breakouts
MIN_ATR_RATIO = 1.0
SL_CUSHION_MULT = 1.3  # From backtest
WICK_BUFFER = 0.25

# Trend strength (breakouts need strong momentum)
ADX_HARD_FLOOR = 50  # High ADX for clean breakouts
MIN_H1_SLOPE = 0.08  # Need directional bias

# Momentum filters (FLIPPED from mean-reversion)
# For LONGS: Don't buy if already overbought
MOMENTUM_STO_K_LONG_MAX = 70  # k_slow must be < 70 for longs

# For SHORTS: Don't sell if already oversold  
MOMENTUM_STO_K_SHORT_MIN = 30  # k_slow must be > 30 for shorts

# Breakout detection
BREAKOUT_THRESHOLD_ATR = 0.3  # How far past level = confirmed break
PULLBACK_PROXIMITY_ATR = 0.4  # How close to level = valid pullback
PULLBACK_LOOKBACK_BARS = 8  # Bars to search for pullback

# Cooldowns (from backtest - matched to performance)
COOLDOWN_DAYS_AFTER_SL = 0.5  # 12 hours (0.5 days)
COOLDOWN_DAYS_AFTER_TP = 0.25  # 6 hours (0.25 days)

# Sessions - Trade 24/7 for breakouts (don't restrict by time)
SESSION_WINDOWS = {
    "all": (0, 24),  # 24/7 trading
}

# HTF settings (use 1H for breakout levels)
HTF_DAYS = 15
HTF_LEVEL_TF = "1H"

# Execution
SLIP_BPS = 0.0002
FEE_BPS = 10

# ═══════════════════════════════════════════════════════════════════════════
# TICK SIZES (Exchange filters)
# ═══════════════════════════════════════════════════════════════════════════
TICK_SIZE = {
    "ETHUSDT": 0.01,
    "SOLUSDT": 0.01,
    # Keep others for future expansion
    "ATOMUSDT": 0.001,
    "LINKUSDT": 0.001,
    "BTCUSDT": 0.1,
    "BNBUSDT": 0.01,
    'ADAUSDT': 0.001,
    'AVAXUSDT': 0.01,
    'DOTUSDT': 0.001,
    'NEARUSDT': 0.0001,
}

# Clusters (for position management)
CLUSTER = {
    "ETHUSDT": "ETH",
    "SOLUSDT": "L1",
}

# ═══════════════════════════════════════════════════════════════════════════
# TRAILING STOP SETTINGS
# ═══════════════════════════════════════════════════════════════════════════
TRAIL_ENABLE = True  # Important for breakouts - let winners run
BE_ARM_R = 0.8  # Move to BE after +0.8R profit
ATR_TRAIL_K = 0.9  # Trail distance = 0.9 * ATR
REMOVE_TP_WHEN_TRAIL = False  # Keep 1.5R TP even when trailing

# ═══════════════════════════════════════════════════════════════════════════
# PROXIMITY FILTER (different from mean-reversion)
# ═══════════════════════════════════════════════════════════════════════════
# For breakouts, we're not as strict about HTF proximity since we're 
# breaking levels, not fading them
NEAR_HTF_MAX_ATR_MOM = 2.0  # Wider tolerance (mean-reversion uses 0.7)
NEAR_HTF_MAX_ATR_MOM_BACKTEST = 2.0

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════
LOG_DIR = "./logs"