# ═══════════════════════════════════════════════════════════════
# FVG ORDER FLOW - 15m CONFIGURATION (OPTIMAL)
# ═══════════════════════════════════════════════════════════════

from typing import List

EQUITY = 20

INTERVAL = "60"  # 1-hour bars
# LOOKBACK_BARS = 500  # ~20 days

# # Volume Profile (24 hours = clean daily profile)
# VOLUME_PROFILE_BINS = 50
# Volume Profile
VP_PERIOD: int = 48  # 12 hours of 15m bars (vs 24 hours on 1H)

# FVG Detection
FVG_LOOKBACK: int = 100  # Look back 25 hours for FVGs
FVG_MIN_GAP_PCT: float = 0.2  # 0.2% minimum gap (tighter for 15m)

# # Order Block Detection
# ORDER_BLOCK_LOOKBACK = 30  # 30 hours

# # Location Filters (Phase 2)
# NEAR_LVN_THRESHOLD_ATR = 0.5  # Must be within 0.5 ATR of LVN
# NEAR_POC_THRESHOLD_ATR = 1.0

# Momentum Filters
ADX_FLOOR = 25  # Strong trend required
MIN_ATR_RATIO = 0.8  # Volatility check

# Order Flow Settings
OF_LOOKBACK: int = 5  # 5 bars = 75 minutes (vs 10 bars = 10 hours on 1H)
OF_MULTIPLIER: float = 2.0  # Boost scores for 15m dynamics
MIN_OF_SCORE: float = 30  # Lower threshold (was 40 on 1H)

# Position Management
RISK_PCT: float = 0.10  # 10% risk per trade
TARGET_R1: float = 1.5  # First target at 1.5R (quick scalp)
TARGET_R2: float = 3  # Second target at 2.5R (runner)
ATR_MULT_SL: float = 0.5  # Tighter stops for 15m

# Quality Filters
MIN_ADX: float = 20  # Lower ADX requirement for 15m
MIN_VOLUME_RATIO: float = 1.2  # Require volume surge

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
    "BNBUSDT": 0.01,
    "BTCUSDT": 0.01,
    "LTCUSDT": 0.01,
    "UNIUSDT": 0.001,
}

# ═══════════════════════════════════════════════════════════════════════════
# TRADING PAIRS
# ═══════════════════════════════════════════════════════════════════════════

PAIRS: List[str] = [
    "DOGEUSDT",
    # 'XRPUSDT',"ETHUSDT",'BNBUSDT',
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
