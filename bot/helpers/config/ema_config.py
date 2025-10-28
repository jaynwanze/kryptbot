# bot/helpers/ema_config.py
"""
EMA TREND FOLLOWING with 200 EMA FILTER

Key Addition: Only trade WITH the major trend
- Longs: Only when price > 200 EMA (bull market)
- Shorts: Only when price < 200 EMA (bear market)

This filters out choppy, sideways markets.
"""

from typing import List
import asyncio
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL STATS & BASIC SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

GLOBAL_DROP_STATS = defaultdict(lambda: defaultdict(int))
GLOBAL_DROP_STATS_LOCK = asyncio.Lock()

PAIR = "ETHUSDT, BNBUSDT"
TF_SECONDS = 60 * 60  # 1 hours
INTERVAL = "60"          # 1H (60 minutes)
LOOKBACK_BARS = 400       # ~66 days of 1H bars (enough for 200 EMA)
TRAIL_ENABLE = True

# ═══════════════════════════════════════════════════════════════════════════
# PORTFOLIO MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

MAX_TRADES_PER_DAY = 5
MAX_SIGNAL_AGE_SEC = 30
COALESCE_SEC = 2
MAX_PER_SIDE_OPEN = 1
MAX_TOTAL_RISK_PCT = 0.30
MAX_OPEN_CONCURRENT = 3

# ═══════════════════════════════════════════════════════════════════════════
# POSITION SIZING
# ═══════════════════════════════════════════════════════════════════════════

RISK_PCT = 0.08
STAKE_SIZE_USD = 1_000
LEVERAGE = 20

# ═══════════════════════════════════════════════════════════════════════════
# EMA TREND FOLLOWING PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════

# EMA Settings
EMA_FAST = 21
EMA_SLOW = 50
EMA_TREND_FILTER = 200  # NEW: Major trend filter
EMA_CROSSOVER_LOOKBACK = 20 # bars
MIN_EMA_SEPARATION = 0.01 # 1.5%
# Trend Strength Filters
ADX_HARD_FLOOR =50  # hard ground
# Use regime as SECONDARY filter (not primary)
REGIME_FILTER_ENABLED = True
REGIME_DAILY_ADX_MIN = 10  # Very permissive
REGIME_BB_WIDTH_MIN = 0.04  # Very permissive
REGIME_REQUIRE_ATR_EXPANDING = False
MIN_ATR_RATIO = 1.1

# Risk/Reward
RR_TARGET = 3.0
ATR_MULT_SL = 1.2
SL_CUSHION_MULT = 1.3
WICK_BUFFER = 0.25

# Trailing Stop
BE_ARM_R = 0.5
ATR_TRAIL_K = 1.0
REMOVE_TP_WHEN_TRAIL = True

# Cooldowns
COOLDOWN_DAYS_AFTER_SL = 0.1
COOLDOWN_DAYS_AFTER_TP = 0.05

# ═══════════════════════════════════════════════════════════════════════════
# SESSION WINDOWS
# ═══════════════════════════════════════════════════════════════════════════

SESSION_WINDOWS = {
    "asia": (0, 24),
}

# ═══════════════════════════════════════════════════════════════════════════
# HTF SETTINGS (backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════

HTF_DAYS = 15
HTF_LEVEL_TF = "1H"

# ═══════════════════════════════════════════════════════════════════════════
# EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

SLIP_BPS = 0.0002
FEE_BPS = 10

# ═══════════════════════════════════════════════════════════════════════════
# TICK SIZES
# ═══════════════════════════════════════════════════════════════════════════

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
    'BNBUSDT': 0.01,
    'BTCUSDT': 0.01,
    'LTCUSDT': 0.01,
    'UNIUSDT': 0.001,
}

# ═══════════════════════════════════════════════════════════════════════════
# TRADING PAIRS
# ═══════════════════════════════════════════════════════════════════════════

PAIRS_LRS: List[str] = [
    'XRPUSDT',"ETHUSDT",'BNBUSDT','DOGEUSDT'
]

# ═══════════════════════════════════════════════════════════════════════════
# CLUSTER GROUPING
# ═══════════════════════════════════════════════════════════════════════════

CLUSTER = {
    "ETHUSDT": "L1",
    "BNBUSDT": "L1",
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