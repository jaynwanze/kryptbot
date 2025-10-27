# ═══════════════════════════════════════════════════════════════
# FVG ORDER FLOW - 1H CONFIGURATION (OPTIMAL)
# ═══════════════════════════════════════════════════════════════

INTERVAL = "60"  # 1-hour bars
LOOKBACK_BARS = 500  # ~20 days

# Volume Profile (24 hours = clean daily profile)
VOLUME_PROFILE_PERIOD = 24
VOLUME_PROFILE_BINS = 50

# FVG Detection
MIN_FVG_SIZE = 0.0003  # 0.03% minimum gap
FVG_LOOKBACK = 20  # Look back 20 hours for FVGs

# Order Block Detection  
ORDER_BLOCK_LOOKBACK = 30  # 30 hours

# Order Flow Scoring
ORDER_FLOW_LOOKBACK = 10  # 10 hours of sustained pressure
MIN_ORDER_FLOW_SCORE = 40  # Lower for 1H (was 60 on 15m)

# Location Filters (Phase 2)
NEAR_LVN_THRESHOLD_ATR = 0.5  # Must be within 0.5 ATR of LVN
NEAR_POC_THRESHOLD_ATR = 1.0

# Momentum Filters
ADX_FLOOR = 25  # Strong trend required
MIN_ATR_RATIO = 0.8  # Volatility check

# Risk Management
RR_TARGET = 2.0  # 2R targets
SL_CUSHION_MULT = 1.2
WICK_BUFFER = 0.25

# Pairs (proven on 1H)
PAIRS = [
    "ETHUSDT",  # 53% WR, 2.61 PF
    "SOLUSDT",  # 38% WR, 1.45 PF  
]